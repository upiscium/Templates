from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "tools" / "source_collaboration.py"
spec = importlib.util.spec_from_file_location("source_collaboration", MODULE)
assert spec and spec.loader
source = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = source
spec.loader.exec_module(source)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


class SourceCollaborationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Source Collaboration Test")
        (self.root / "file.txt").write_text("one\n", encoding="utf-8")
        for path in source.SOURCE_AUTHORITY:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("trusted\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        git(self.root, "branch", "-M", "main")
        git(self.root, "remote", "add", "origin", "git@github.com:upiscium/Templates.git")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.approved_base = git(self.root, "rev-parse", "HEAD")
        git_path = Path(shutil.which("git") or "missing-git").resolve()
        self.base_override = mock.patch.multiple(
            source, TRUSTED_SOURCE_BASE=self.approved_base,
            TRUSTED_GIT=str(git_path), TRUSTED_GH=sys.executable,
            TRUSTED_PATH=f"{git_path.parent}:{Path(sys.executable).resolve().parent}:/usr/bin:/bin",
        )
        self.base_override.start()
        self.addCleanup(self.base_override.stop)
        git(self.root, "checkout", "-qb", "feat/215-source-collaboration")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def issue(self, root: Path, number: int):
        return {"number": number, "state": "OPEN", "title": "Source collaboration", "url": "x"}

    def ctx(self):
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            return source.context(215, self.root)

    def pr(self, **changes):
        record = {
            "id": 700, "number": 7, "html_url": "https://github.com/upiscium/Templates/pull/7",
            "state": "open", "draft": True, "body": "Closes #215",
            "head": {"ref": "feat/215-source-collaboration",
                     "sha": git(self.root, "rev-parse", "HEAD"),
                     "repo": {"full_name": source.REPO},
                     "label": "upiscium:feat/215-source-collaboration"},
            "base": {"ref": "main", "repo": {"full_name": source.REPO}},
        }
        record.update(changes)
        return record

    def checkpoint_text(self, body: str) -> str:
        current = git(self.root, "rev-parse", "HEAD")
        normalized = body.strip()
        marker = (f"<!-- source-checkpoint:215:{current}:"
                  f"{source.hashlib.sha256(normalized.encode()).hexdigest()} -->")
        return marker + "\n" + normalized + "\n"

    def comment(self, number: int, body: str, *, principal: int = 42,
                issue_url: str | None = None) -> dict:
        return {"id": number, "body": body, "user": {"id": principal, "type": "User"},
                "issue_url": issue_url or "https://api.github.com/repos/upiscium/Templates/issues/7"}

    def checkpoint_api(self, comments: list[dict], calls: list[str], *, actor: int = 42):
        def api(_root: Path, _api: str, endpoint: str):
            calls.append(endpoint)
            if endpoint == "user":
                return {"id": actor, "login": "publisher", "type": "User"}
            if endpoint == f"repos/{source.REPO}/pulls/7":
                return self.pr()
            if endpoint.startswith(f"repos/{source.REPO}/issues/7/comments?"):
                page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(endpoint).query)["page"][0])
                return comments[(page - 1) * 100:page * 100]
            self.fail(f"unexpected GitHub API endpoint: {endpoint}")
        return api

    def test_secret_classifier_allows_envrc(self) -> None:
        self.assertFalse(source.secret_like(".envrc"))
        self.assertFalse(source.secret_like(".env.example"))
        self.assertTrue(source.secret_like(".env"))
        self.assertTrue(source.secret_like(".env.production"))
        self.assertTrue(source.secret_like("config/client-secret.json"))

    def test_context_rejects_default_branch_and_unbound_issue(self) -> None:
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "does not bind"):
                source.context(999, self.root)
        git(self.root, "checkout", "-q", "main")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "default-branch"):
                source.context(215, self.root)

    def test_canonical_and_mixed_case_github_origins(self) -> None:
        for origin in (
            "git@github.com:upiscium/Templates.git",
            "ssh://git@github.com/upiscium/Templates.git",
            "https://github.com/uPiscium/Templates.git",
            "git@GitHub.com:UPISCIUM/templates.git",
            "https://GitHub.com/Upiscium/TEMPLATES/",
        ):
            with self.subTest(origin=origin):
                git(self.root, "remote", "set-url", "origin", origin)
                self.assertEqual(self.root.resolve(), self.ctx()["root"])
                self.assertEqual(origin, git(self.root, "remote", "get-url", "origin"))

    def test_different_or_unsupported_origin_fails_closed(self) -> None:
        for origin in (
            "git@github.com:another/Templates.git",
            "https://github.com/upiscium/Another.git",
            "https://gitlab.com/upiscium/Templates.git",
            "https://github.com.evil.invalid/upiscium/Templates.git",
            "http://github.com/upiscium/Templates.git",
            "https://github.com/upiscium/Templates/other.git",
            "https://github.com/upiscium/Templates.git?redirect=1",
            "https://github.com/upiscium/Templateſ.git",
        ):
            with self.subTest(origin=origin):
                git(self.root, "remote", "set-url", "origin", origin)
                with mock.patch.object(source, "issue_meta") as meta:
                    with self.assertRaises(source.GuardError):
                        source.context(215, self.root)
                    meta.assert_not_called()

    def test_authority_paths_are_review_sensitive_but_publishable(self) -> None:
        self.assertEqual({
            "Justfile", "just/source.just", "tools/source_collaboration.py",
            "tools/source_publication_launcher.sh", "just/template.just",
            "tools/render_templates.py", "flake.nix",
        }, source.SOURCE_AUTHORITY)
        for path in sorted(source.SOURCE_AUTHORITY):
            with self.subTest(path=path):
                target = self.root / path
                target.write_text("future version\n", encoding="utf-8")
                with mock.patch.object(source, "issue_meta", side_effect=self.issue):
                    result = source.publication_check(215, self.root)
                self.assertEqual("READY", result["status"])
                self.assertEqual([path], result["review_sensitive_paths"])
                self.assertEqual([path], [e["path"] for e in result["manifest"]["entries"]])
                self.assertEqual("", git(self.root, "diff", "--cached", "--name-only"))
                target.write_text("trusted\n", encoding="utf-8")

    def test_future_guard_code_can_be_committed_by_installed_executor(self) -> None:
        target = self.root / "tools/source_collaboration.py"
        target.write_text("future policy\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            result = source.publication_check(215, self.root)
            with mock.patch.object(source, "parity") as parity:
                committed = source.commit(215, result["scope_digest"], "test: future source policy", self.root)
        parity.assert_called_once()
        self.assertEqual("COMMITTED", committed["status"])
        self.assertEqual(["tools/source_collaboration.py"], committed["paths"])
        self.assertEqual(committed["head"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual("", git(self.root, "status", "--porcelain"))


    def test_source_deletion_is_scoped_but_symlink_is_rejected(self) -> None:
        target = self.root / "just/source.just"
        target.unlink()
        self.assertEqual(
            [{"path": "just/source.just", "kind": "deleted"}],
            source.manifest(self.ctx())["entries"],
        )
        target.symlink_to("../file.txt")
        with self.assertRaisesRegex(source.GuardError, "regular file"):
            source.manifest(self.ctx())


    def test_committed_source_change_does_not_block_normal_future_task(self) -> None:
        (self.root / "Justfile").write_text("future source entrypoint\n", encoding="utf-8")
        git(self.root, "add", "Justfile")
        git(self.root, "commit", "-qm", "reviewed future guard")
        current = git(self.root, "rev-parse", "HEAD")
        (self.root / "file.txt").write_text("normal change\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            checked = source.publication_check(215, self.root)
        self.assertEqual("READY", checked["status"])
        self.assertEqual([], checked["review_sensitive_paths"])
        self.assertEqual(["file.txt"], [e["path"] for e in checked["manifest"]["entries"]])
        (self.root / "file.txt").write_text("one\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "remote_head", return_value=current
        ):
            self.assertEqual("ALREADY_PUSHED", source.push(215, current, self.root)["status"])


    def test_reverted_source_history_preserves_approved_lineage(self) -> None:
        target = self.root / "tools/source_collaboration.py"
        target.write_text("future\n", encoding="utf-8")
        git(self.root, "add", "tools/source_collaboration.py")
        git(self.root, "commit", "-qm", "future source")
        target.write_text("trusted\n", encoding="utf-8")
        git(self.root, "add", "tools/source_collaboration.py")
        git(self.root, "commit", "-qm", "revert future source")
        (self.root / "file.txt").write_text("normal change\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            self.assertEqual("READY", source.publication_check(215, self.root)["status"])


    def test_merged_source_history_is_accepted(self) -> None:
        git(self.root, "checkout", "-qb", "side")
        target = self.root / "just/template.just"
        target.write_text("new parity\n", encoding="utf-8")
        git(self.root, "add", "just/template.just")
        git(self.root, "commit", "-qm", "new parity")
        target.write_text("trusted\n", encoding="utf-8")
        git(self.root, "add", "just/template.just")
        git(self.root, "commit", "-qm", "revert new parity")
        git(self.root, "checkout", "-q", "feat/215-source-collaboration")
        git(self.root, "merge", "--no-ff", "-qm", "merge side", "side")
        (self.root / "file.txt").write_text("normal change\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            self.assertEqual("READY", source.publication_check(215, self.root)["status"])


    def test_merge_from_unrelated_prebase_side_history_is_accepted(self) -> None:
        # The installed source revision is a Git ancestor through the first
        # parent. The unrelated second parent need not descend from it.
        side_tree = git(self.root, "rev-parse", "HEAD^{tree}")
        old_root = git(self.root, "commit-tree", side_tree, "-m", "prebase side root")
        git(self.root, "branch", "prebase-side", old_root)
        git(self.root, "merge", "--allow-unrelated-histories", "--no-ff",
            "-qm", "merge prebase side", "prebase-side")
        (self.root / "file.txt").write_text("normal change\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            self.assertEqual("READY", source.publication_check(215, self.root)["status"])

    def test_non_descendant_tracked_main_is_rejected(self) -> None:
        side_tree = git(self.root, "rev-parse", "HEAD^{tree}")
        other = git(self.root, "commit-tree", side_tree, "-m", "unrelated main")
        git(self.root, "update-ref", "refs/remotes/origin/main", other)
        (self.root / "file.txt").write_text("normal change\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "approved source authority revision"):
                source.publication_check(215, self.root)

    def test_missing_tracked_default_branch_fails_closed(self) -> None:
        git(self.root, "update-ref", "-d", "refs/remotes/origin/main")
        (self.root / "file.txt").write_text("normal change\n", encoding="utf-8")
        with self.assertRaisesRegex(source.GuardError, "tracked origin/main"):
            source.manifest(self.ctx())

    def test_tracking_ref_advances_after_reviewed_source_update(self) -> None:
        (self.root / "Justfile").write_text("future source\n", encoding="utf-8")
        git(self.root, "add", "Justfile")
        git(self.root, "commit", "-qm", "future reviewed source")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        (self.root / "file.txt").write_text("normal change\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            self.assertEqual("READY", source.publication_check(215, self.root)["status"])


    def test_git_graft_cannot_hide_reverted_authority_history(self) -> None:
        target = self.root / "Justfile"
        target.write_text("unsafe\n", encoding="utf-8")
        git(self.root, "add", "Justfile")
        git(self.root, "commit", "-qm", "unsafe authority")
        target.write_text("trusted\n", encoding="utf-8")
        git(self.root, "add", "Justfile")
        git(self.root, "commit", "-qm", "restore authority")
        base = self.approved_base
        current = git(self.root, "rev-parse", "HEAD")
        (self.root / ".git" / "info" / "grafts").write_text(
            f"{current} {base}\n", encoding="ascii"
        )
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "grafted or shallow"):
                source.push(215, current, self.root)

    def test_graft_added_after_preflight_cannot_hide_raw_authority_history(self) -> None:
        target = self.root / "Justfile"
        target.write_text("unsafe\n", encoding="utf-8")
        git(self.root, "add", "Justfile")
        git(self.root, "commit", "-qm", "unsafe authority")
        target.write_text("trusted\n", encoding="utf-8")
        git(self.root, "add", "Justfile")
        git(self.root, "commit", "-qm", "revert authority")
        current = git(self.root, "rev-parse", "HEAD")
        base = self.approved_base
        original_git = source.git

        def racing_git(root: Path, *args: str, **kwargs):
            result = original_git(root, *args, **kwargs)
            if args[:3] == ("rev-parse", "--verify", "--quiet") and "origin/main" in args[3]:
                (self.root / ".git" / "info" / "grafts").write_text(
                    f"{current} {base}\n", encoding="ascii"
                )
            return result

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "git", side_effect=racing_git
        ):
            with self.assertRaisesRegex(source.GuardError, "grafted or shallow"):
                source.push(215, current, self.root)

    def test_commit_rejects_bytes_changed_during_staging(self) -> None:
        target = self.root / "file.txt"
        target.write_text("reviewed\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        original_git = source.git

        def racing_git(root: Path, *args: str, **kwargs):
            if args and args[0] == "hash-object":
                target.write_text("different staged bytes\n", encoding="utf-8")
            return original_git(root, *args, **kwargs)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "git", side_effect=racing_git):
            with self.assertRaisesRegex(source.GuardError, "source changed while preparing commit"):
                source.commit(215, expected, "test: reject staging race", self.root)
        # Failed private preparation leaves the real index untouched.
        self.assertEqual("", git(self.root, "diff", "--cached", "--name-only"))
        self.assertEqual(self.approved_base, git(self.root, "rev-parse", "HEAD"))

    def test_base_identity_ignores_environment_and_unsubstituted_source_fails(self) -> None:
        with mock.patch.dict(os.environ, {"TEMPLATES_SOURCE_BASE": "f" * 40}):
            self.assertEqual(self.approved_base, source.trusted_base_revision())
        with mock.patch.object(source, "TRUSTED_SOURCE_BASE", "@TRUSTED_SOURCE_BASE@"), mock.patch.dict(
            os.environ, {"TEMPLATES_SOURCE_BASE": self.approved_base}
        ):
            with self.assertRaisesRegex(source.GuardError, "approved source authority revision"):
                source.context(215, self.root)

    def test_user_owned_ssh_agent_socket_is_preserved_without_command_overrides(self) -> None:
        agent_path = Path(self.temp.name) / "agent.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as agent:
            agent.bind(str(agent_path))
            with mock.patch.dict(os.environ, {
                "SSH_AUTH_SOCK": str(agent_path), "GIT_SSH_COMMAND": "./untrusted-ssh",
            }):
                trusted = source.trusted_environment()
            self.assertEqual(str(agent_path), trusted["SSH_AUTH_SOCK"])
            self.assertNotIn("GIT_SSH_COMMAND", trusted)
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": str(Path(self.temp.name) / "missing.sock")}):
            self.assertNotIn("SSH_AUTH_SOCK", source.trusted_environment())

    def test_filter_added_after_preflight_does_not_execute_during_staging(self) -> None:
        sentinel = Path(self.temp.name) / "clean-filter-executed"
        (self.root / ".gitattributes").write_text("file.txt filter=sneaky\n", encoding="utf-8")
        git(self.root, "add", ".gitattributes")
        git(self.root, "commit", "-qm", "fixture attributes")
        (self.root / "file.txt").write_text("reviewed bytes\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        original_git = source.git

        def racing_git(root: Path, *args: str, **kwargs):
            if args and args[0] == "hash-object":
                git(self.root, "config", "filter.sneaky.clean", f"touch {sentinel}")
            return original_git(root, *args, **kwargs)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "git", side_effect=racing_git):
            with self.assertRaisesRegex(source.GuardError, "unsafe local Git configuration"):
                source.commit(215, expected, "test: no candidate clean filter", self.root)
        self.assertFalse(sentinel.exists())
        self.assertEqual("fixture attributes", git(self.root, "log", "-1", "--format=%s"))

    def test_diff_external_added_after_preflight_is_not_executed(self) -> None:
        sentinel = Path(self.temp.name) / "external-diff-executed"
        (self.root / "file.txt").write_text("pending\n", encoding="utf-8")
        original_git = source.git

        def racing_git(root: Path, *args: str, **kwargs):
            if args[:3] == ("diff", "--no-ext-diff", "--check"):
                git(self.root, "config", "diff.external", f"touch {sentinel}")
            return original_git(root, *args, **kwargs)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "git", side_effect=racing_git
        ):
            result = source.publication_check(215, self.root)
        self.assertEqual("READY", result["status"])
        self.assertFalse(sentinel.exists())

    def test_local_git_executable_configuration_is_rejected(self) -> None:
        for name in ("core.fsmonitor", "core.sshCommand", "filter.sneaky.clean"):
            with self.subTest(name=name):
                git(self.root, "config", name, "./untrusted-command")
                with mock.patch.object(source, "issue_meta") as issue:
                    with self.assertRaisesRegex(source.GuardError, "unsafe local Git configuration"):
                        source.context(215, self.root)
                    issue.assert_not_called()
                git(self.root, "config", "--unset", name)

    def test_normal_source_changes_and_existing_path_guards(self) -> None:
        (self.root / "file.txt").write_text("normal change\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            result = source.publication_check(215, self.root)
        self.assertEqual("READY", result["status"])
        self.assertEqual(["file.txt"], [e["path"] for e in result["manifest"]["entries"]])
        (self.root / ".env.production").write_text("password=x\n", encoding="utf-8")
        with self.assertRaisesRegex(source.GuardError, "secret-like"):
            source.manifest(self.ctx())
        (self.root / ".env.production").unlink()
        (self.root / "link").symlink_to("file.txt")
        with self.assertRaisesRegex(source.GuardError, "regular file"):
            source.manifest(self.ctx())

    def test_scope_digest_detects_race(self) -> None:
        (self.root / "file.txt").write_text("two\n", encoding="utf-8")
        value = source.manifest(self.ctx())
        expected = source.digest(value)
        (self.root / "file.txt").write_text("three\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ):
            with self.assertRaisesRegex(source.GuardError, "source scope changed"):
                source.commit(215, expected, "test: source guard", self.root)
        self.assertEqual("", git(self.root, "diff", "--cached", "--name-only"))

    def test_commit_is_exact_and_accepts_envrc(self) -> None:
        (self.root / "file.txt").write_text("two\n", encoding="utf-8")
        (self.root / ".envrc").write_text("use flake\n", encoding="utf-8")
        value = source.manifest(self.ctx())
        expected = source.digest(value)
        old = git(self.root, "rev-parse", "HEAD")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ):
            result = source.commit(215, expected, "test: source guard", self.root)
        self.assertEqual("COMMITTED", result["status"])
        self.assertNotEqual(old, result["head"])
        self.assertEqual(old, git(self.root, "rev-parse", "HEAD^"))
        self.assertEqual(result["head"], git(self.root, "rev-parse", "refs/heads/feat/215-source-collaboration"))
        self.assertEqual("", git(self.root, "status", "--porcelain"))

    def test_commit_cas_works_in_real_linked_task_worktree(self) -> None:
        linked = Path(self.temp.name) / "linked-task"
        git(self.root, "worktree", "add", "-qb", "feat/215-linked", str(linked))
        (linked / "file.txt").write_text("linked reviewed\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            expected = source.digest(source.manifest(source.context(215, linked)))
            with mock.patch.object(source, "parity"):
                result = source.commit(215, expected, "test: linked Task CAS", linked)
        self.assertEqual("COMMITTED", result["status"])
        self.assertEqual(result["head"], git(linked, "rev-parse", "refs/heads/feat/215-linked"))
        self.assertEqual(self.approved_base, git(self.root, "rev-parse", "feat/215-source-collaboration"))
        self.assertEqual("", git(linked, "status", "--porcelain"))

    def test_retargeted_git_pointer_cannot_select_another_registered_task(self) -> None:
        linked = Path(self.temp.name) / "linked-task"
        victim = Path(self.temp.name) / "victim-task"
        git(self.root, "worktree", "add", "-qb", "feat/215-linked", str(linked))
        git(self.root, "worktree", "add", "-qb", "feat/215-victim", str(victim))
        pointer = linked / ".git"
        original = pointer.read_text(encoding="utf-8")
        victim_gitdir = git(victim, "rev-parse", "--absolute-git-dir")
        pointer.write_text(f"gitdir: {victim_gitdir}\n", encoding="utf-8")
        try:
            with mock.patch.object(source, "issue_meta", side_effect=self.issue):
                with self.assertRaisesRegex(source.GuardError, "own Git root|not registered"):
                    source.context(215, linked)
        finally:
            pointer.write_text(original, encoding="utf-8")
        self.assertEqual(self.approved_base, git(victim, "rev-parse", "HEAD"))

    def test_commit_refuses_branch_change_before_staging_even_at_same_head(self) -> None:
        (self.root / "file.txt").write_text("reviewed\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        git(self.root, "branch", "feat/215-other")

        def switch_branch(_: Path) -> None:
            git(self.root, "symbolic-ref", "HEAD", "refs/heads/feat/215-other")

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity", side_effect=switch_branch
        ):
            with self.assertRaisesRegex(source.GuardError, "not an exact registered Git worktree"):
                source.commit(215, expected, "test: wrong branch", self.root)
        self.assertEqual("", git(self.root, "diff", "--cached", "--name-only"))
        for name in ("feat/215-other", "feat/215-source-collaboration"):
            self.assertEqual(self.approved_base, git(self.root, "rev-parse", name))
        self.assertEqual("reviewed\n", (self.root / "file.txt").read_text(encoding="utf-8"))

    def test_commit_refuses_branch_switch_during_staging_and_retains_index(self) -> None:
        (self.root / "file.txt").write_text("reviewed\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        git(self.root, "branch", "feat/215-other")
        original_stage = source.stage_source_scope

        def racing_stage(root: Path, value: dict):
            result = original_stage(root, value)
            git(self.root, "symbolic-ref", "HEAD", "refs/heads/feat/215-other")
            return result

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "stage_source_scope", side_effect=racing_stage):
            with self.assertRaisesRegex(source.GuardError, "branch/ref moved before commit"):
                source.commit(215, expected, "test: staging branch switch", self.root)
        self.assertEqual("", git(self.root, "diff", "--cached", "--name-only"))
        for name in ("feat/215-other", "feat/215-source-collaboration"):
            self.assertEqual(self.approved_base, git(self.root, "rev-parse", name))

    def test_commit_refuses_branch_switch_after_last_check_at_same_head(self) -> None:
        (self.root / "file.txt").write_text("reviewed\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        git(self.root, "branch", "feat/215-other")
        original_commit_ref = source.commit_ref

        def racing_ref(ctx: dict, ref: str, old: str, new: str,
                       value: dict, tree: str, prepared_index: bytes) -> None:
            git(self.root, "symbolic-ref", "HEAD", "refs/heads/feat/215-other")
            original_commit_ref(ctx, ref, old, new, value, tree, prepared_index)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "commit_ref", side_effect=racing_ref):
            with self.assertRaisesRegex(source.GuardError, "not an exact registered Git worktree"):
                source.commit(215, expected, "test: CAS branch switch", self.root)
        for name in ("feat/215-other", "feat/215-source-collaboration"):
            self.assertEqual(self.approved_base, git(self.root, "rev-parse", name))
        self.assertEqual("", git(self.root, "diff", "--cached", "--name-only"))

    def test_commit_head_lock_blocks_branch_switch_during_ref_cas(self) -> None:
        (self.root / "file.txt").write_text("reviewed\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        git(self.root, "branch", "feat/215-other")
        original_update = source.update_task_ref

        def racing_update(root: Path, common: Path, ref: str, old: str, new: str):
            switch = subprocess.run(
                ["git", "symbolic-ref", "HEAD", "refs/heads/feat/215-other"],
                cwd=root, capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(0, switch.returncode, switch.stderr)
            return original_update(root, common, ref, old, new)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "update_task_ref", side_effect=racing_update):
            result = source.commit(215, expected, "test: exact Task ref", self.root)
        self.assertEqual(result["head"], git(self.root, "rev-parse", "feat/215-source-collaboration"))
        self.assertEqual(self.approved_base, git(self.root, "rev-parse", "feat/215-other"))
        self.assertEqual("", git(self.root, "status", "--porcelain"))

    def test_real_checkout_can_mutate_worktree_before_failing_on_head_lock(self) -> None:
        """Characterize real Git checkout, not just symbolic-ref's lock behavior."""
        target = self.root / "switch-only.txt"
        target.write_text("branch A\n", encoding="utf-8")
        git(self.root, "add", "switch-only.txt")
        git(self.root, "commit", "-qm", "fixture branch A")
        git(self.root, "checkout", "-qb", "feat/215-other")
        target.write_text("branch B\n", encoding="utf-8")
        git(self.root, "add", "switch-only.txt")
        git(self.root, "commit", "-qm", "fixture branch B")
        git(self.root, "checkout", "-q", "feat/215-source-collaboration")
        before = git(self.root, "write-tree")
        old = git(self.root, "rev-parse", "HEAD")
        lock = self.root / ".git" / "HEAD.lock"
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            result = subprocess.run(
                ["git", "checkout", "feat/215-other"], cwd=self.root, text=True,
                capture_output=True, check=False, timeout=10,
            )
            after = git(self.root, "write-tree")
            live = target.read_text(encoding="utf-8")
        finally:
            os.close(fd)
            lock.unlink()
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertEqual(old, git(self.root, "rev-parse", "HEAD"))
        self.assertEqual("branch B\n", live, result.stderr)
        self.assertNotEqual(before, after, result.stderr)

    def test_real_checkout_during_task_cas_does_not_silently_mutate_branch_b(self) -> None:
        tracked = self.root / "switch-only.txt"
        tracked.write_text("branch A\n", encoding="utf-8")
        git(self.root, "add", "switch-only.txt")
        git(self.root, "commit", "-qm", "fixture branch A")
        git(self.root, "checkout", "-qb", "feat/215-other")
        tracked.write_text("branch B\n", encoding="utf-8")
        git(self.root, "add", "switch-only.txt")
        git(self.root, "commit", "-qm", "fixture branch B")
        branch_b = git(self.root, "rev-parse", "HEAD")
        git(self.root, "checkout", "-q", "feat/215-source-collaboration")
        (self.root / "file.txt").write_text("reviewed R\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        original_update = source.update_task_ref
        checkout_results = []

        def racing_update(root: Path, common: Path, ref: str, old: str, new: str):
            checkout = subprocess.run(
                ["git", "checkout", "feat/215-other"], cwd=root, text=True,
                capture_output=True, check=False, timeout=10,
            )
            checkout_results.append(checkout)
            self.assertNotEqual(0, checkout.returncode, checkout.stderr)
            return original_update(root, common, ref, old, new)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "update_task_ref", side_effect=racing_update):
            result = source.commit(215, expected, "test: fenced real checkout", self.root)
        self.assertEqual(1, len(checkout_results))
        self.assertEqual("COMMITTED", result["status"])
        self.assertEqual(result["head"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(branch_b, git(self.root, "rev-parse", "feat/215-other"))
        self.assertEqual("branch A\n", tracked.read_text(encoding="utf-8"))
        self.assertEqual("branch A", git(self.root, "show", ":switch-only.txt"))
        self.assertEqual("", git(self.root, "status", "--porcelain"))

    def test_real_switch_during_linked_task_cas_is_fenced(self) -> None:
        linked = Path(self.temp.name) / "linked-task"
        git(self.root, "worktree", "add", "-qb", "feat/215-linked", str(linked))
        tracked = linked / "switch-only.txt"
        tracked.write_text("branch A\n", encoding="utf-8")
        git(linked, "add", "switch-only.txt")
        git(linked, "commit", "-qm", "fixture A")
        git(linked, "switch", "-qc", "feat/215-other")
        tracked.write_text("branch B\n", encoding="utf-8")
        git(linked, "add", "switch-only.txt")
        git(linked, "commit", "-qm", "fixture B")
        branch_b = git(linked, "rev-parse", "HEAD")
        git(linked, "switch", "-q", "feat/215-linked")
        (linked / "file.txt").write_text("reviewed R\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            expected = source.digest(source.manifest(source.context(215, linked)))
        original_update = source.update_task_ref

        def racing_update(root: Path, common: Path, ref: str, old: str, new: str):
            self.assertEqual(linked, root)
            attempt = subprocess.run(
                ["git", "switch", "feat/215-other"], cwd=linked, text=True,
                capture_output=True, check=False, timeout=10,
            )
            self.assertNotEqual(0, attempt.returncode, attempt.stderr)
            return original_update(root, common, ref, old, new)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "update_task_ref", side_effect=racing_update):
            result = source.commit(215, expected, "test: linked switch fence", linked)
        self.assertEqual("COMMITTED", result["status"])
        self.assertEqual(result["head"], git(linked, "rev-parse", "HEAD"))
        self.assertEqual(branch_b, git(linked, "rev-parse", "feat/215-other"))
        self.assertEqual("branch A\n", tracked.read_text(encoding="utf-8"))
        self.assertEqual("", git(linked, "status", "--porcelain"))

    def test_exact_commit_with_concurrent_worktree_mutation_requires_reconciliation(self) -> None:
        tracked = self.root / "switch-only.txt"
        tracked.write_text("branch A\n", encoding="utf-8")
        git(self.root, "add", "switch-only.txt")
        git(self.root, "commit", "-qm", "fixture branch A")
        old = git(self.root, "rev-parse", "HEAD")
        (self.root / "file.txt").write_text("reviewed R\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        original_update = source.update_task_ref
        applied = []

        def racing_update(root: Path, common: Path, ref: str, parent: str, new: str):
            result = original_update(root, common, ref, parent, new)
            applied.append(new)
            tracked.write_text("concurrent writer\n", encoding="utf-8")
            return result

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "update_task_ref", side_effect=racing_update):
            with self.assertRaisesRegex(source.GuardError, "exact commit.*index/worktree convergence needs reconciliation"):
                source.commit(215, expected, "test: retain concurrent work", self.root)
        self.assertEqual(applied, [git(self.root, "rev-parse", "HEAD")])
        self.assertEqual(old, git(self.root, "rev-parse", "HEAD^"))
        self.assertEqual("concurrent writer\n", tracked.read_text(encoding="utf-8"))
        self.assertEqual("branch A", git(self.root, "show", ":switch-only.txt"))
        self.assertEqual("one", git(self.root, "show", ":file.txt"))

    def test_commit_refuses_concurrent_task_ref_move_without_overwriting_it(self) -> None:
        (self.root / "file.txt").write_text("reviewed\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        original_update = source.update_task_ref
        concurrent = git(self.root, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "concurrent")

        def racing_update(root: Path, common: Path, ref: str, old: str, new: str):
            self.assertEqual(0, original_update(root, common, ref, old, concurrent).returncode)
            return original_update(root, common, ref, old, new)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "update_task_ref", side_effect=racing_update):
            with self.assertRaisesRegex(source.GuardError, "commit ref conflict"):
                source.commit(215, expected, "test: CAS moved ref", self.root)
        self.assertEqual(concurrent, git(self.root, "rev-parse", "HEAD"))
        self.assertEqual("reviewed\n", (self.root / "file.txt").read_text(encoding="utf-8"))

    def test_commit_ref_cas_recovers_lost_ack_only_for_exact_effect(self) -> None:
        (self.root / "file.txt").write_text("reviewed\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        original_update = source.update_task_ref

        def lost_ack(root: Path, common: Path, ref: str, old: str, new: str):
            applied = original_update(root, common, ref, old, new)
            self.assertEqual(0, applied.returncode, applied.stderr)
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="lost ack")

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "update_task_ref", side_effect=lost_ack):
            result = source.commit(215, expected, "test: lost CAS ack", self.root)
        self.assertEqual("COMMITTED", result["status"])
        self.assertEqual(result["head"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual("", git(self.root, "status", "--porcelain"))

    def test_post_cas_ref_probe_exception_reports_applied_commit(self) -> None:
        (self.root / "file.txt").write_text("reviewed R\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        original_probe = source.pinned_ref
        calls = 0

        def flaky_probe(ctx: dict, ref: str):
            nonlocal calls
            calls += 1
            if calls == 3:  # after the exact ref CAS has already applied
                raise OSError("lost ref-read acknowledgement")
            return original_probe(ctx, ref)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "pinned_ref", side_effect=flaky_probe):
            with self.assertRaisesRegex(source.GuardError, "exact commit.*index/worktree convergence needs reconciliation"):
                source.commit(215, expected, "test: probe interruption", self.root)
        self.assertGreaterEqual(calls, 4)
        self.assertNotEqual(self.approved_base, git(self.root, "rev-parse", "HEAD"))
        self.assertEqual("one", git(self.root, "show", ":file.txt"))

    def test_interrupt_after_cas_reports_applied_commit_and_keeps_old_index(self) -> None:
        (self.root / "file.txt").write_text("reviewed R\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        original_update = source.update_task_ref

        def interrupted_update(root: Path, common: Path, ref: str, old: str, new: str):
            applied = original_update(root, common, ref, old, new)
            self.assertEqual(0, applied.returncode, applied.stderr)
            raise KeyboardInterrupt()

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "update_task_ref", side_effect=interrupted_update):
            with self.assertRaisesRegex(source.GuardError, "exact commit.*index/worktree convergence needs reconciliation"):
                source.commit(215, expected, "test: interrupted CAS", self.root)
        self.assertNotEqual(self.approved_base, git(self.root, "rev-parse", "HEAD"))
        self.assertEqual("one", git(self.root, "show", ":file.txt"))

    def test_interrupt_before_cas_preserves_unpublished_work(self) -> None:
        (self.root / "file.txt").write_text("reviewed R\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "update_task_ref", side_effect=KeyboardInterrupt):
            with self.assertRaisesRegex(source.GuardError, "commit interrupted.*still at"):
                source.commit(215, expected, "test: interrupted before CAS", self.root)
        self.assertEqual(self.approved_base, git(self.root, "rev-parse", "HEAD"))
        self.assertEqual("one", git(self.root, "show", ":file.txt"))
        self.assertEqual("reviewed R\n", (self.root / "file.txt").read_text(encoding="utf-8"))

    def test_existing_index_lock_is_not_removed_or_bypassed(self) -> None:
        (self.root / "file.txt").write_text("reviewed R\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        lock = self.root / ".git" / "index.lock"
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
                source, "parity"
            ):
                with self.assertRaisesRegex(source.GuardError, "cannot lock Task index"):
                    source.commit(215, expected, "test: respect existing lock", self.root)
            self.assertTrue(lock.exists())
            self.assertEqual(self.approved_base, git(self.root, "rev-parse", "HEAD"))
        finally:
            os.close(fd)
            lock.unlink()

    def test_post_cas_git_verification_exception_is_reconciliation_not_traceback(self) -> None:
        (self.root / "file.txt").write_text("reviewed R\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        original_paths = source.paths

        def flaky_paths(root: Path, *, cached: bool = False, unstaged: bool = False):
            if cached and git(root, "rev-parse", "HEAD") != self.approved_base:
                raise subprocess.CalledProcessError(128, ["git", "diff"])
            return original_paths(root, cached=cached, unstaged=unstaged)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "paths", side_effect=flaky_paths):
            with self.assertRaisesRegex(source.GuardError, "exact commit.*index/worktree convergence needs reconciliation"):
                source.commit(215, expected, "test: post-CAS verification interruption", self.root)
        self.assertNotEqual(self.approved_base, git(self.root, "rev-parse", "HEAD"))
        self.assertEqual("reviewed R", git(self.root, "show", ":file.txt"))

    def test_commit_rejects_concurrent_unreviewed_index_entry(self) -> None:
        (self.root / "file.txt").write_text("reviewed\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        original_stage = source.stage_source_scope

        def racing_stage(root: Path, value: dict):
            result = original_stage(root, value)
            (self.root / "extra.txt").write_text("another writer\n", encoding="utf-8")
            git(self.root, "add", "extra.txt")
            return result

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "stage_source_scope", side_effect=racing_stage):
            with self.assertRaisesRegex(source.GuardError, "shared index changed"):
                source.commit(215, expected, "test: extra index entry", self.root)
        self.assertEqual(self.approved_base, git(self.root, "rev-parse", "HEAD"))
        self.assertEqual("extra.txt", git(self.root, "diff", "--cached", "--name-only"))

    def test_same_path_concurrent_staged_blob_is_never_overwritten(self) -> None:
        target = self.root / "file.txt"
        target.write_text("reviewed R\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        unique = Path(self.temp.name) / "writer-unique-X.txt"
        unique.write_text("writer unique staged X\n", encoding="utf-8")
        oid_x = git(self.root, "hash-object", "-w", str(unique))
        original_stage = source.stage_source_scope

        def concurrent_stage(root: Path, value: dict):
            git(root, "update-index", "--add", "--cacheinfo", "100644", oid_x, "file.txt")
            return original_stage(root, value)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "stage_source_scope", side_effect=concurrent_stage):
            with self.assertRaisesRegex(source.GuardError, "index.*changed|staged.*scope"):
                source.commit(215, expected, "test: keep X", self.root)
        self.assertEqual(self.approved_base, git(self.root, "rev-parse", "HEAD"))
        self.assertIn(oid_x, git(self.root, "ls-files", "--stage", "--", "file.txt"))
        self.assertEqual("reviewed R\n", target.read_text(encoding="utf-8"))

    def test_same_path_git_writer_is_blocked_while_cas_owns_index_lock(self) -> None:
        (self.root / "file.txt").write_text("reviewed R\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        unique = Path(self.temp.name) / "writer-X.txt"
        unique.write_text("unique X\n", encoding="utf-8")
        oid_x = git(self.root, "hash-object", "-w", str(unique))
        original_update = source.update_task_ref

        def racing_update(root: Path, common: Path, ref: str, old: str, new: str):
            writer = subprocess.run(
                ["git", "update-index", "--add", "--cacheinfo", "100644", oid_x, "file.txt"],
                cwd=root, text=True, capture_output=True, check=False, timeout=10,
            )
            self.assertNotEqual(0, writer.returncode, writer.stderr)
            return original_update(root, common, ref, old, new)

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "update_task_ref", side_effect=racing_update):
            result = source.commit(215, expected, "test: index locked before CAS", self.root)
        self.assertEqual("COMMITTED", result["status"])
        self.assertNotIn(oid_x, git(self.root, "ls-files", "--stage", "--", "file.txt"))
        self.assertEqual("", git(self.root, "status", "--porcelain"))

    def test_post_cas_same_path_noncooperating_writer_is_retained(self) -> None:
        (self.root / "file.txt").write_text("reviewed R\n", encoding="utf-8")
        expected = source.digest(source.manifest(self.ctx()))
        unique = Path(self.temp.name) / "writer-X.txt"
        unique.write_text("unique X\n", encoding="utf-8")
        oid_x = git(self.root, "hash-object", "-w", str(unique))
        index = self.root / ".git" / "index"
        writer_index = Path(self.temp.name) / "writer-index"
        writer_index.write_bytes(index.read_bytes())
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(writer_index)
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", "100644", oid_x, "file.txt"],
            cwd=self.root, env=env, capture_output=True, check=True,
        )
        original_update = source.update_task_ref

        def racing_update(root: Path, common: Path, ref: str, old: str, new: str):
            applied = original_update(root, common, ref, old, new)
            # Model a writer ignoring Git's index.lock; guarded convergence
            # must detect this byte change and never overwrite X.
            index.write_bytes(writer_index.read_bytes())
            return applied

        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "parity"
        ), mock.patch.object(source, "update_task_ref", side_effect=racing_update):
            with self.assertRaisesRegex(source.GuardError, "exact commit.*index/worktree convergence needs reconciliation"):
                source.commit(215, expected, "test: retain late X", self.root)
        self.assertNotEqual(self.approved_base, git(self.root, "rev-parse", "HEAD"))
        self.assertIn(oid_x, git(self.root, "ls-files", "--stage", "--", "file.txt"))
        self.assertEqual("reviewed R\n", (self.root / "file.txt").read_text(encoding="utf-8"))

    def test_push_refuses_dirty_worktree(self) -> None:
        (self.root / "file.txt").write_text("dirty\n", encoding="utf-8")
        current = git(self.root, "rev-parse", "HEAD")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "must be clean"):
                source.push(215, current, self.root)

    def test_isolated_transport_ignores_candidate_git_url_rewrites(self) -> None:
        remote = Path(self.temp.name) / "canonical.git"
        attacker = Path(self.temp.name) / "attacker.git"
        git(self.root, "init", "--bare", "-q", str(remote))
        git(self.root, "init", "--bare", "-q", str(attacker))
        git(self.root, "config", f"url.{attacker}.insteadOf", str(remote))
        current = git(self.root, "rev-parse", "HEAD")
        branch = "feat/215-source-collaboration"
        result = source.transport_push(self.root, str(remote), str(remote), branch, current, None)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(current, source.remote_head(self.root, branch, str(remote)))
        self.assertIsNone(source.remote_head(self.root, branch, str(attacker)))

    def test_push_destination_rejects_other_repository_and_multiple_urls(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        for url in (
            "https://github.com/other/Templates.git",
            "https://gitlab.com/upiscium/Templates.git",
        ):
            with self.subTest(url=url):
                git(self.root, "config", "remote.origin.pushurl", url)
                with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
                    source, "remote_head"
                ) as remote:
                    with self.assertRaises(source.GuardError):
                        source.push(215, current, self.root)
                    remote.assert_not_called()
        git(self.root, "config", "remote.origin.pushurl", "https://github.com/uPiscium/Templates.git")
        self.assertEqual(
            "https://github.com/uPiscium/Templates.git", source.push_destination(self.root)
        )
        git(self.root, "config", "--add", "remote.origin.pushurl", "git@github.com:upiscium/Templates.git")
        with self.assertRaisesRegex(source.GuardError, "exactly one"):
            source.push_destination(self.root)

    def test_push_destination_rejects_second_git_url_rewrite(self) -> None:
        git(self.root, "config", "remote.origin.pushurl", "source-alias:repo")
        git(self.root, "config", "url.https://github.com/upiscium/Templates.git.insteadOf", "source-alias:repo")
        git(self.root, "config", "url.https://gitlab.com/attacker/Templates.git.insteadOf",
            "https://github.com/upiscium/Templates.git")
        self.assertEqual(
            "https://github.com/upiscium/Templates.git",
            git(self.root, "remote", "get-url", "--push", "--all", "origin"),
        )
        with self.assertRaisesRegex(source.GuardError, "subject to Git URL rewriting"):
            source.push_destination(self.root)

    def test_pr_adoption_requires_exact_draft(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        pr = {
            "id": 700, "number": 7, "html_url": "https://example/pr/7", "state": "open", "draft": True,
            "body": "Closes #215",
            "head": {"ref": "feat/215-source-collaboration", "sha": current,
                     "repo": {"full_name": source.REPO}},
            "base": {"ref": "main", "repo": {"full_name": source.REPO}},
        }
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "pulls", return_value=[pr]):
            result = source.pr_create(215, self.root)
        self.assertEqual("ADOPTED", result["status"])
        self.assertEqual(7, result["pr"])

    def test_pr_adoption_rejects_wrong_issue_binding(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        pr = {
            "id": 700, "number": 7, "html_url": "https://example/pr/7", "state": "open", "draft": True,
            "body": "Closes #999",
            "head": {"ref": "feat/215-source-collaboration", "sha": current,
                     "repo": {"full_name": source.REPO}},
            "base": {"ref": "main", "repo": {"full_name": source.REPO}},
        }
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "pulls", return_value=[pr]):
            with self.assertRaisesRegex(source.GuardError, "not bound to Issue #215"):
                source.pr_create(215, self.root)

    def test_pr_creation_refuses_human_closed_pr_identity(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        pr = {
            "id": 700, "number": 7, "html_url": "https://example/pr/7", "state": "closed", "draft": True,
            "body": "Closes #215",
            "head": {"ref": "feat/215-source-collaboration", "sha": current,
                     "repo": {"full_name": source.REPO}},
            "base": {"ref": "main", "repo": {"full_name": source.REPO}},
        }
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "pulls", return_value=[pr]):
            with self.assertRaisesRegex(source.GuardError, "PR is not open"):
                source.pr_create(215, self.root)

    def test_pr_lookup_paginates_all_states_and_ignores_unrelated_fork(self) -> None:
        target = self.pr(state="closed")
        unrelated = [{"id": n, "head": {"ref": "other", "repo": {"full_name": source.REPO}}}
                     for n in range(1, 100)]
        fork = {"id": 100, "head": {"ref": "feat/215-source-collaboration",
                                    "repo": {"full_name": "other/Templates"},
                                    "label": "other:feat/215-source-collaboration"}}
        pages = iter([unrelated + [fork], [target]])
        endpoints = []

        def gh_page(_root: Path, *_args: str):
            endpoints.append(_args[-1])
            return next(pages)

        with mock.patch.object(source, "gh_json", side_effect=gh_page):
            self.assertEqual([target], source.pulls(self.root, "feat/215-source-collaboration"))
        self.assertIn("state=all", endpoints[0])
        self.assertIn("page=2", endpoints[1])

    def test_pr_create_does_not_bypass_late_closed_or_incompatible_identity(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        incompatible = (
            self.pr(state="closed"),
            self.pr(merged_at="2026-01-01T00:00:00Z", state="closed"),
            self.pr(head={**self.pr()["head"], "sha": "f" * 40}),
            self.pr(base={"ref": "other", "repo": {"full_name": source.REPO}}),
            self.pr(head={**self.pr()["head"], "repo": {"full_name": "other/Templates"}}),
            self.pr(head={**self.pr()["head"], "ref": "renamed-branch"}),
        )
        for prior in incompatible:
            with self.subTest(prior=prior):
                with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
                    source, "require_clean"
                ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
                    source, "remote_head", return_value=current
                ), mock.patch.object(source, "pulls", side_effect=[[], [prior]]), mock.patch.object(
                    source, "run"
                ) as create:
                    with self.assertRaises(source.GuardError):
                        source.pr_create(215, self.root)
                    create.assert_not_called()

    def test_pr_create_recovers_lost_ack_after_full_scan(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="lost ack")
        with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "pulls", side_effect=[[], [], [self.pr()]]), mock.patch.object(
            source, "require_task_head"
        ), mock.patch.object(source, "run", return_value=failed
        ) as create:
            result = source.pr_create(215, self.root)
        self.assertEqual("CREATED", result["status"])
        create.assert_called_once()

    def test_pr_create_detects_concurrent_duplicate_or_changed_ack(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        created = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/upiscium/Templates/pull/8\n", stderr=""
        )
        for found, message in (
            ([self.pr(), self.pr(id=701, number=8)], "multiple PR identities"),
            ([self.pr()], "acknowledgement conflicts"),
        ):
            with self.subTest(message=message), mock.patch.object(
                source, "context", return_value=self.ctx()
            ), mock.patch.object(source, "require_clean"), mock.patch.object(
                source, "require_approved_lineage"
            ), mock.patch.object(source, "remote_head", return_value=current), mock.patch.object(
                source, "pulls", side_effect=[[], [], found]
            ), mock.patch.object(source, "require_task_head"), mock.patch.object(
                source, "run", return_value=created
            ):
                with self.assertRaisesRegex(source.GuardError, message):
                    source.pr_create(215, self.root)

    def test_pr_create_refuses_disappearing_or_duplicated_scan(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "pulls", side_effect=[[self.pr()], []]), mock.patch.object(
            source, "run"
        ) as create:
            with self.assertRaisesRegex(source.GuardError, "identity changed"):
                source.pr_create(215, self.root)
            create.assert_not_called()
        pages = iter([[self.pr()] * 100, [self.pr()]])
        with mock.patch.object(source, "gh_json", side_effect=lambda *_: next(pages)):
            with self.assertRaisesRegex(source.GuardError, "duplicate GitHub record"):
                source.pulls(self.root, "feat/215-source-collaboration")

    def test_pr_create_refuses_local_switch_before_or_after_create(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        git(self.root, "branch", "feat/215-other")
        original_run = source.run
        for switch_during_lookup in (True, False):
            with self.subTest(switch_during_lookup=switch_during_lookup):
                observed = 0

                def pulls(_root: Path, _branch: str):
                    nonlocal observed
                    observed += 1
                    if switch_during_lookup and observed == 2:
                        git(self.root, "symbolic-ref", "HEAD", "refs/heads/feat/215-other")
                    return [] if observed <= 2 else [self.pr()]

                def create(root: Path, *args: str, **kwargs):
                    if args[0] == "gh":
                        git(self.root, "symbolic-ref", "HEAD", "refs/heads/feat/215-other")
                        return subprocess.CompletedProcess(
                            args=[], returncode=0,
                            stdout="https://github.com/upiscium/Templates/pull/7\n", stderr="",
                        )
                    return original_run(root, *args, **kwargs)

                with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
                    source, "require_clean"
                ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
                    source, "remote_head", return_value=current
                ), mock.patch.object(source, "pulls", side_effect=pulls), mock.patch.object(
                    source, "run", side_effect=create
                ) as gh:
                    with self.assertRaisesRegex(source.GuardError, "registered Git worktree"):
                        source.pr_create(215, self.root)
                self.assertEqual(0 if switch_during_lookup else 1,
                                 sum(call.args[1] == "gh" for call in gh.call_args_list))
                git(self.root, "symbolic-ref", "HEAD", "refs/heads/feat/215-source-collaboration")
                self.assertEqual(current, git(self.root, "rev-parse", "HEAD"))

    def test_pr_adoption_rejects_non_boolean_draft(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "pulls", return_value=[self.pr(draft="false")]):
            with self.assertRaisesRegex(source.GuardError, "not Draft"):
                source.pr_create(215, self.root)

    def test_push_recovers_lost_acknowledgement(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        ctx = {
            "root": self.root, "issue": 215, "branch": "feat/215-source-collaboration",
            "head": current, "title": "Source collaboration",
            "origin": "git@github.com:upiscium/Templates.git",
        }
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="lost ack")
        with mock.patch.object(source, "context", return_value=ctx), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "require_approved_lineage"
        ), mock.patch.object(source, "push_destination", return_value="git@github.com:upiscium/Templates.git"
        ), mock.patch.object(source, "remote_head", side_effect=[None, current]), mock.patch.object(
            source, "transport_push", return_value=failed
        ) as transport:
            result = source.push(215, current, self.root)
        self.assertEqual("PUSHED", result["status"])
        transport.assert_called_once_with(
            self.root, "git@github.com:upiscium/Templates.git",
            "git@github.com:upiscium/Templates.git", "feat/215-source-collaboration", current, None,
        )

    def test_checkpoint_recovers_lost_acknowledgement(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        comments: list[dict] = []
        calls: list[str] = []
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="lost ack")
        def post(*_args: str, **_kwargs):
            comments.append(self.comment(88, self.checkpoint_text("checkpoint")))
            return failed

        with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "require_approved_lineage"
        ), mock.patch.object(source, "remote_head", return_value=current), mock.patch.object(
            source, "gh_json", side_effect=self.checkpoint_api(comments, calls)
        ), mock.patch.object(source, "require_task_head"), mock.patch.object(
            source, "run", side_effect=post
        ) as write:
            result = source.checkpoint(215, 7, current, " checkpoint \n", self.root)
        self.assertEqual("POSTED", result["status"])
        self.assertEqual(88, result["comment_id"])
        write.assert_called_once()
        self.assertEqual(3, calls.count("user"))

    def test_checkpoint_adopts_trusted_later_page_not_spoofed_marker(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        text = self.checkpoint_text("checkpoint")
        comments = [self.comment(n, "unrelated") for n in range(1, 100)]
        comments += [self.comment(100, text, principal=99), self.comment(200, text)]
        calls: list[str] = []
        with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "gh_json", side_effect=self.checkpoint_api(comments, calls)), mock.patch.object(
            source, "require_task_head"
        ), mock.patch.object(source, "run"
        ) as post:
            result = source.checkpoint(215, 7, current, "checkpoint", self.root)
        self.assertEqual("ALREADY_POSTED", result["status"])
        self.assertEqual(200, result["comment_id"])
        self.assertEqual(2, sum("page=2" in call for call in calls))
        post.assert_not_called()

    def test_checkpoint_spoofed_only_is_not_adopted_and_identical_race_is_canonical(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        text = self.checkpoint_text("checkpoint")
        comments = [self.comment(50, text, principal=99)]
        calls: list[str] = []

        def post(*_args: str, **_kwargs):
            comments.extend([self.comment(89, text), self.comment(88, text)])
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "gh_json", side_effect=self.checkpoint_api(comments, calls)), mock.patch.object(
            source, "require_task_head"
        ), mock.patch.object(source, "run", side_effect=post
        ) as write:
            result = source.checkpoint(215, 7, current, "checkpoint", self.root)
        self.assertEqual("POSTED", result["status"])
        self.assertEqual(88, result["comment_id"])
        write.assert_called_once()

    def test_checkpoint_adopts_concurrent_identical_post_before_own_mutation(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        text = self.checkpoint_text("checkpoint")
        comments: list[dict] = []
        calls: list[str] = []
        api = self.checkpoint_api(comments, calls)

        def racing_api(root: Path, command: str, endpoint: str):
            if endpoint.endswith("comments?per_page=100&page=1") and any(
                call.endswith("comments?per_page=100&page=1") for call in calls
            ) and not comments:
                comments.append(self.comment(88, text))
            return api(root, command, endpoint)

        with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "gh_json", side_effect=racing_api), mock.patch.object(
            source, "require_task_head"
        ), mock.patch.object(source, "run"
        ) as post:
            result = source.checkpoint(215, 7, current, "checkpoint", self.root)
        self.assertEqual("ALREADY_POSTED", result["status"])
        self.assertEqual(88, result["comment_id"])
        post.assert_not_called()

    def test_checkpoint_conflicting_trusted_body_or_provenance_fails_closed(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        text = self.checkpoint_text("checkpoint")
        for comment, diagnostic in (
            (self.comment(88, text + "altered"), "conflicting trusted checkpoint body"),
            (self.comment(88, text, issue_url="https://api.github.com/repos/other/Templates/issues/7"),
             "repository/PR provenance conflicts"),
        ):
            with self.subTest(diagnostic=diagnostic):
                comments = [comment]
                with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
                    source, "require_clean"
                ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
                    source, "remote_head", return_value=current
                ), mock.patch.object(source, "gh_json", side_effect=self.checkpoint_api(comments, [])), mock.patch.object(
                    source, "run"
                ) as post:
                    with self.assertRaisesRegex(source.GuardError, diagnostic):
                        source.checkpoint(215, 7, current, "checkpoint", self.root)
                    post.assert_not_called()

    def test_checkpoint_conflict_after_post_is_not_mistaken_for_success(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        text = self.checkpoint_text("checkpoint")
        comments: list[dict] = []
        def post(*_args: str, **_kwargs):
            comments.extend([self.comment(88, text), self.comment(89, text + "conflict")])
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="lost ack")

        with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "gh_json", side_effect=self.checkpoint_api(comments, [])), mock.patch.object(
            source, "require_task_head"
        ), mock.patch.object(source, "run", side_effect=post
        ):
            with self.assertRaisesRegex(source.GuardError, "conflicting trusted checkpoint body"):
                source.checkpoint(215, 7, current, "checkpoint", self.root)

    def test_checkpoint_cannot_adopt_foreign_authenticated_principal(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        comments = [self.comment(88, self.checkpoint_text("checkpoint"), principal=42)]
        with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
            source, "remote_head", return_value=current
        ), mock.patch.object(source, "gh_json", side_effect=self.checkpoint_api(comments, [], actor=43)), mock.patch.object(
            source, "require_task_head"
        ), mock.patch.object(source, "run", return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="post denied",
            )
        ):
            with self.assertRaisesRegex(source.GuardError, "effect absent"):
                source.checkpoint(215, 7, current, "checkpoint", self.root)

    def test_checkpoint_refuses_local_switch_before_or_after_post(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        git(self.root, "branch", "feat/215-other")
        text = self.checkpoint_text("checkpoint")
        original_run = source.run
        for switch_during_lookup in (True, False):
            with self.subTest(switch_during_lookup=switch_during_lookup):
                comments: list[dict] = []
                calls: list[str] = []
                api = self.checkpoint_api(comments, calls)

                def racing_api(root: Path, command: str, endpoint: str):
                    if (switch_during_lookup and endpoint.endswith("comments?per_page=100&page=1")
                            and any(call.endswith("comments?per_page=100&page=1") for call in calls)):
                        git(self.root, "symbolic-ref", "HEAD", "refs/heads/feat/215-other")
                    return api(root, command, endpoint)

                def post(root: Path, *args: str, **kwargs):
                    if args[0] == "gh":
                        comments.append(self.comment(88, text))
                        git(self.root, "symbolic-ref", "HEAD", "refs/heads/feat/215-other")
                        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
                    return original_run(root, *args, **kwargs)

                with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
                    source, "require_clean"
                ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
                    source, "remote_head", return_value=current
                ), mock.patch.object(source, "gh_json", side_effect=racing_api), mock.patch.object(
                    source, "run", side_effect=post
                ) as gh:
                    with self.assertRaisesRegex(source.GuardError, "registered Git worktree"):
                        source.checkpoint(215, 7, current, "checkpoint", self.root)
                self.assertEqual(0 if switch_during_lookup else 1,
                                 sum(call.args[1] == "gh" for call in gh.call_args_list))
                git(self.root, "symbolic-ref", "HEAD", "refs/heads/feat/215-source-collaboration")
                self.assertEqual(current, git(self.root, "rev-parse", "HEAD"))

    def test_checkpoint_rechecks_subject_after_later_final_comment_page(self) -> None:
        current = git(self.root, "rev-parse", "HEAD")
        text = self.checkpoint_text("checkpoint")
        git(self.root, "branch", "feat/215-other")
        comments = [self.comment(n, "unrelated") for n in range(1, 101)]
        comments.append(self.comment(101, text))
        for drift, diagnostic in (
            ("local", "local Task branch/HEAD changed"),
            ("remote", "remote Task branch moved during checkpoint scan"),
            ("pr", "PR base identity mismatch"),
        ):
            with self.subTest(drift=drift):
                calls: list[str] = []
                api = self.checkpoint_api(comments, calls)
                changed = False

                def racing_api(root: Path, command: str, endpoint: str):
                    nonlocal changed
                    if endpoint.endswith("comments?per_page=100&page=2"):
                        if sum(call.endswith("comments?per_page=100&page=2") for call in calls) == 1:
                            changed = True
                            if drift == "local":
                                git(self.root, "symbolic-ref", "HEAD", "refs/heads/feat/215-other")
                    if drift == "pr" and changed and endpoint.endswith("pulls/7"):
                        return self.pr(base={"ref": "other", "repo": {"full_name": source.REPO}})
                    return api(root, command, endpoint)

                def remote(*_args):
                    return "f" * 40 if drift == "remote" and changed else current

                with mock.patch.object(source, "context", return_value=self.ctx()), mock.patch.object(
                    source, "require_clean"
                ), mock.patch.object(source, "require_approved_lineage"), mock.patch.object(
                    source, "remote_head", side_effect=remote
                ), mock.patch.object(source, "gh_json", side_effect=racing_api), mock.patch.object(
                    source, "run", wraps=source.run
                ) as execution:
                    with self.assertRaisesRegex(source.GuardError, diagnostic):
                        source.checkpoint(215, 7, current, "checkpoint", self.root)
                self.assertFalse(any(call.args[1] == "gh" for call in execution.call_args_list))
                if drift == "local":
                    git(self.root, "symbolic-ref", "HEAD", "refs/heads/feat/215-source-collaboration")


if __name__ == "__main__":
    unittest.main()
