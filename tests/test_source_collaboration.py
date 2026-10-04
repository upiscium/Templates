from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
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

    def test_authority_paths_cannot_be_published(self) -> None:
        self.assertTrue({
            "Justfile", "just/source.just", "tools/source_collaboration.py",
            "tools/source_publication_launcher.sh", "just/template.just",
            "tools/render_templates.py", "flake.nix",
        }.issubset(source.SOURCE_AUTHORITY))
        for path in sorted(source.SOURCE_AUTHORITY):
            with self.subTest(path=path):
                target = self.root / path
                target.write_text("redirected\n", encoding="utf-8")
                with mock.patch.object(source, "issue_meta", side_effect=self.issue):
                    with self.assertRaisesRegex(source.GuardError, "maintainer bootstrap"):
                        source.publication_check(215, self.root)
                    with mock.patch.object(source, "parity") as parity:
                        with self.assertRaisesRegex(source.GuardError, "maintainer bootstrap"):
                            source.commit(215, "ignored", "test: authority", self.root)
                        parity.assert_not_called()
                self.assertEqual("", git(self.root, "diff", "--cached", "--name-only"))
                target.write_text("trusted\n", encoding="utf-8")

    def test_authority_deletion_and_symlink_are_rejected(self) -> None:
        target = self.root / "just/source.just"
        target.unlink()
        with self.assertRaisesRegex(source.GuardError, "maintainer bootstrap"):
            source.manifest(self.ctx())
        target.symlink_to("../file.txt")
        with self.assertRaisesRegex(source.GuardError, "maintainer bootstrap"):
            source.manifest(self.ctx())

    def test_committed_authority_change_blocks_all_publication_commands(self) -> None:
        (self.root / "Justfile").write_text("redirected\n", encoding="utf-8")
        git(self.root, "add", "Justfile")
        git(self.root, "commit", "-qm", "unauthorized authority update")
        current = git(self.root, "rev-parse", "HEAD")
        (self.root / "file.txt").write_text("normal change\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "maintainer bootstrap"):
                source.publication_check(215, self.root)
        (self.root / "file.txt").write_text("one\n", encoding="utf-8")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue), mock.patch.object(
            source, "remote_head"
        ) as remote, mock.patch.object(source, "pulls") as pulls:
            for action in (
                lambda: source.push(215, current, self.root),
                lambda: source.pr_create(215, self.root),
                lambda: source.checkpoint(215, 7, current, "body", self.root),
            ):
                with self.assertRaisesRegex(source.GuardError, "maintainer bootstrap"):
                    action()
            remote.assert_not_called()
            pulls.assert_not_called()

    def test_reverted_authority_commit_is_not_publishable(self) -> None:
        target = self.root / "tools/source_collaboration.py"
        target.write_text("unsafe\n", encoding="utf-8")
        git(self.root, "add", "tools/source_collaboration.py")
        git(self.root, "commit", "-qm", "unsafe authority")
        target.write_text("trusted\n", encoding="utf-8")
        git(self.root, "add", "tools/source_collaboration.py")
        git(self.root, "commit", "-qm", "revert authority")
        first = git(self.root, "rev-parse", "HEAD~1")
        raw = source.git(
            self.root, "diff-tree", "-r", "--name-only", "-z", "--no-renames",
            "--diff-filter=ACDMRTUXB", f"{first}^", first,
            "--", *sorted(source.SOURCE_AUTHORITY), binary=True,
        ).stdout
        observed = source.nul_paths(raw)
        self.assertIn("tools/source_collaboration.py", observed, repr(raw))
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "maintainer bootstrap"):
                source.push(215, git(self.root, "rev-parse", "HEAD"), self.root)

    def test_merged_side_branch_authority_history_is_rejected(self) -> None:
        git(self.root, "checkout", "-qb", "side")
        target = self.root / "just/template.just"
        target.write_text("skip checks\n", encoding="utf-8")
        git(self.root, "add", "just/template.just")
        git(self.root, "commit", "-qm", "skip parity")
        target.write_text("trusted\n", encoding="utf-8")
        git(self.root, "add", "just/template.just")
        git(self.root, "commit", "-qm", "revert parity")
        git(self.root, "checkout", "-q", "feat/215-source-collaboration")
        git(self.root, "merge", "--no-ff", "-qm", "merge side", "side")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "maintainer bootstrap"):
                source.pr_create(215, self.root)

    def test_missing_tracked_default_branch_fails_closed(self) -> None:
        git(self.root, "update-ref", "-d", "refs/remotes/origin/main")
        (self.root / "file.txt").write_text("normal change\n", encoding="utf-8")
        with self.assertRaisesRegex(source.GuardError, "tracked origin/main"):
            source.manifest(self.ctx())

    def test_moved_tracking_ref_cannot_hide_committed_authority_change(self) -> None:
        (self.root / "Justfile").write_text("unsafe\n", encoding="utf-8")
        git(self.root, "add", "Justfile")
        git(self.root, "commit", "-qm", "unsafe authority")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        with mock.patch.object(source, "issue_meta", side_effect=self.issue):
            with self.assertRaisesRegex(source.GuardError, "maintainer bootstrap"):
                source.push(215, git(self.root, "rev-parse", "HEAD"), self.root)

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
            with self.assertRaisesRegex(source.GuardError, "maintainer bootstrap"):
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
            with self.assertRaisesRegex(source.GuardError, "source changed while staging"):
                source.commit(215, expected, "test: reject staging race", self.root)
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
        self.assertEqual("", git(self.root, "status", "--porcelain"))

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
            "number": 7, "html_url": "https://example/pr/7", "state": "open", "draft": True,
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
            "number": 7, "html_url": "https://example/pr/7", "state": "open", "draft": True,
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
            "number": 7, "html_url": "https://example/pr/7", "state": "closed", "draft": True,
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
        ), mock.patch.object(source, "require_authority_unchanged"
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
        ctx = {
            "root": self.root, "issue": 215, "branch": "feat/215-source-collaboration",
            "head": current, "title": "Source collaboration",
            "origin": "git@github.com:upiscium/Templates.git",
        }
        pr = {
            "number": 7, "state": "open", "draft": True, "body": "Closes #215",
            "head": {"ref": ctx["branch"], "sha": current, "repo": {"full_name": source.REPO}},
            "base": {"ref": "main", "repo": {"full_name": source.REPO}},
        }
        body = "checkpoint"
        marker = (
            f"<!-- source-checkpoint:215:{current}:"
            f"{source.hashlib.sha256(body.encode()).hexdigest()} -->"
        )
        api_values = iter([pr, [], [{"id": 88, "body": marker + "\ncheckpoint"}]])
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="lost ack")
        with mock.patch.object(source, "context", return_value=ctx), mock.patch.object(
            source, "require_clean"
        ), mock.patch.object(source, "require_authority_unchanged"
        ), mock.patch.object(source, "remote_head", return_value=current), mock.patch.object(
            source, "gh_json", side_effect=lambda *args: next(api_values)
        ), mock.patch.object(source, "run", return_value=failed):
            result = source.checkpoint(215, 7, current, body, self.root)
        self.assertEqual("POSTED", result["status"])
        self.assertEqual(88, result["comment_id"])


if __name__ == "__main__":
    unittest.main()
