from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "components/agent-core/.automation/bin"
sys.path.insert(0, str(BIN))
import task_lifecycle as lifecycle
import task_contract

spec = importlib.util.spec_from_file_location("post_merge_agent_core", BIN / "agent_core.py")
assert spec and spec.loader
agent_core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_core)


def command(*args: str, cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class RepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        top = Path(self.temporary.name)
        self.remote = top / "origin.git"
        self.repo = top / "repo"
        self.publisher = top / "publisher"
        command("git", "init", "--bare", "--initial-branch=main", str(self.remote), cwd=top)
        command("git", "init", "--initial-branch=main", str(self.repo), cwd=top)
        self.configure(self.repo)
        (self.repo / ".automation/templates").mkdir(parents=True)
        (self.repo / ".automation/templates/task-state.md").write_text(
            (ROOT / "components/agent-core/.automation/templates/task-state.md").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        (self.repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
        command("git", "add", ".", cwd=self.repo)
        command("git", "commit", "-m", "initial", cwd=self.repo)
        command("git", "remote", "add", "origin", str(self.remote), cwd=self.repo)
        command("git", "push", "-u", "origin", "main", cwd=self.repo)
        command("git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=self.repo)
        exclude = Path(command("git", "rev-parse", "--git-common-dir", cwd=self.repo)) / "info/exclude"
        if not exclude.is_absolute():
            exclude = self.repo / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("/.worktrees/\n/.task-state/\n", encoding="utf-8")
        command("git", "clone", str(self.remote), str(self.publisher), cwd=top)
        self.configure(self.publisher)

    @staticmethod
    def configure(repo: Path) -> None:
        command("git", "config", "user.name", "Test User", cwd=repo)
        command("git", "config", "user.email", "test@example.invalid", cwd=repo)

    def publish(self, text: str) -> str:
        path = self.publisher / "tracked.txt"
        path.write_text(path.read_text(encoding="utf-8") + text + "\n", encoding="utf-8")
        command("git", "add", "tracked.txt", cwd=self.publisher)
        command("git", "commit", "-m", text, cwd=self.publisher)
        command("git", "push", "origin", "main", cwd=self.publisher)
        return command("git", "rev-parse", "HEAD", cwd=self.publisher)

    def start_task(self, task: str = "TASK-1", slug: str = "demo") -> Path:
        lifecycle.task_start(self.repo, task, slug)
        worktree = self.repo / ".worktrees" / f"{task}-{slug}"
        state = worktree / ".task-state/task.md"
        resolved = state.read_text(encoding="utf-8")
        resolved = resolved.replace("## Purpose\n\nTBD", "## Purpose\n\nFinalization fixture")
        resolved = resolved.replace("## Scope\n\n- TBD", "## Scope\n\n- Guarded integration coverage")
        resolved = resolved.replace(
            "- [ ] Define Task-specific acceptance criteria",
            "- [ ] Finalize only verified merged evidence",
        )
        resolved = resolved.replace("- Unverified: Task contract", "- Unverified: finalization checks")
        state.write_text(resolved, encoding="utf-8")
        return worktree


class DefaultBranchSynchronizationTest(RepositoryFixture):
    def test_fetches_only_origin_default_and_fast_forwards(self) -> None:
        expected = self.publish("remote advance")
        with mock.patch.object(lifecycle, "run", wraps=lifecycle.run) as observed:
            result = lifecycle.synchronize_default_branch(self.repo)
        fetches = [call.args[0] for call in observed.call_args_list if call.args[0][:2] == ["git", "fetch"]]
        self.assertEqual(
            fetches,
            [["git", "fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main"]],
        )
        self.assertEqual(result["revision"], expected)
        self.assertEqual(command("git", "rev-parse", "main", cwd=self.repo), expected)
        self.assertEqual(command("git", "rev-parse", "origin/main", cwd=self.repo), expected)

    def test_already_synchronized_is_idempotent(self) -> None:
        first = lifecycle.synchronize_default_branch(self.repo)
        second = lifecycle.synchronize_default_branch(self.repo)
        self.assertFalse(first["updated"])
        self.assertFalse(second["updated"])
        self.assertEqual(first["revision"], second["revision"])

    def test_dirty_tracked_and_untracked_main_fail_closed(self) -> None:
        for name in ("tracked.txt", "untracked.txt"):
            with self.subTest(name=name):
                path = self.repo / name
                original = path.read_text(encoding="utf-8") if path.exists() else None
                path.write_text("dirty\n", encoding="utf-8")
                with self.assertRaisesRegex(lifecycle.LifecycleError, "must be clean"):
                    lifecycle.synchronize_default_branch(self.repo)
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original, encoding="utf-8")

    def test_local_only_commit_is_preserved_and_rejected(self) -> None:
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        command("git", "add", "local.txt", cwd=self.repo)
        command("git", "commit", "-m", "local only", cwd=self.repo)
        local = command("git", "rev-parse", "HEAD", cwd=self.repo)
        with self.assertRaisesRegex(lifecycle.LifecycleError, "local-only commits or divergence"):
            lifecycle.synchronize_default_branch(self.repo)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=self.repo), local)

    def test_non_fast_forward_remote_tracking_movement_fails_closed(self) -> None:
        old = command("git", "rev-parse", "origin/main", cwd=self.repo)
        command("git", "checkout", "--orphan", "replacement", cwd=self.publisher)
        command("git", "rm", "-rf", ".", cwd=self.publisher)
        (self.publisher / "replacement.txt").write_text("replacement\n", encoding="utf-8")
        command("git", "add", ".", cwd=self.publisher)
        command("git", "commit", "-m", "replacement", cwd=self.publisher)
        command("git", "push", "--force", "origin", "HEAD:main", cwd=self.publisher)
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.synchronize_default_branch(self.repo)
        self.assertEqual(command("git", "rev-parse", "origin/main", cwd=self.repo), old)

    def test_task_start_refreshes_a_later_remote_revision(self) -> None:
        expected = self.publish("later merge")
        worktree = self.start_task("TASK-2", "fresh")
        state = (worktree / ".task-state/task.md").read_text(encoding="utf-8")
        self.assertIn(f"- Base revision: {expected}", state)
        self.assertEqual(command("git", "rev-parse", "HEAD", cwd=worktree), expected)

    def test_ambient_git_config_and_ssh_overrides_are_scrubbed(self) -> None:
        expected = self.publish("safe remote")
        marker = self.repo.parent / "ssh-command-ran"
        injected = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "remote.origin.url",
            "GIT_CONFIG_VALUE_0": "ssh://attacker.invalid/repository",
            "GIT_SSH_COMMAND": f"touch {marker}",
        }
        with mock.patch.dict(os.environ, injected):
            result = lifecycle.synchronize_default_branch(self.repo)
        self.assertEqual(result["revision"], expected)
        self.assertFalse(marker.exists())

    def test_github_https_network_git_uses_only_command_scoped_gh_helper(self) -> None:
        original = command("git", "remote", "get-url", "origin", cwd=self.repo)
        command(
            "git",
            "remote",
            "set-url",
            "origin",
            "https://github.com/upiscium/private-fixture.git",
            cwd=self.repo,
        )
        try:
            trusted_gh = Path("/nix/store/example-gh/bin/gh")
            with mock.patch.object(
                lifecycle, "_github_cli_executable", return_value=trusted_gh
            ):
                argv, github_https, helper = lifecycle._network_git_command(
                    self.repo,
                    [
                        "fetch",
                        "--no-tags",
                        "origin",
                        "refs/heads/main:refs/remotes/origin/main",
                    ],
                )
        finally:
            command("git", "remote", "set-url", "origin", original, cwd=self.repo)

        self.assertTrue(github_https)
        self.assertEqual(helper, trusted_gh)
        self.assertEqual(argv[0], "git")
        self.assertIn("credential.helper=", argv)
        self.assertIn("credential.https://github.com.helper=", argv)
        self.assertIn(
            "credential.https://github.com.helper="
            "!/nix/store/example-gh/bin/gh auth git-credential",
            argv,
        )
        self.assertIn("credential.interactive=false", argv)
        self.assertEqual(
            argv[-4:],
            [
                "fetch",
                "--no-tags",
                "origin",
                "refs/heads/main:refs/remotes/origin/main",
            ],
        )

    def test_github_https_auth_rejects_unsafe_repository_local_network_config(self) -> None:
        original = command("git", "remote", "get-url", "origin", cwd=self.repo)
        command(
            "git",
            "remote",
            "set-url",
            "origin",
            "https://github.com/upiscium/private-fixture.git",
            cwd=self.repo,
        )
        command(
            "git",
            "config",
            "url.https://attacker.invalid/.insteadOf",
            "https://github.com/",
            cwd=self.repo,
        )
        try:
            with self.assertRaisesRegex(
                lifecycle.LifecycleError,
                "unsafe local Git network configuration",
            ):
                lifecycle._network_git_command(self.repo, ["fetch", "origin"])
        finally:
            command(
                "git",
                "config",
                "--unset-all",
                "url.https://attacker.invalid/.insteadOf",
                cwd=self.repo,
            )
            command("git", "remote", "set-url", "origin", original, cwd=self.repo)

    def test_github_https_auth_rejects_unsafe_worktree_network_config(self) -> None:
        original = command("git", "remote", "get-url", "origin", cwd=self.repo)
        command(
            "git",
            "remote",
            "set-url",
            "origin",
            "https://github.com/upiscium/private-fixture.git",
            cwd=self.repo,
        )
        command("git", "config", "extensions.worktreeConfig", "true", cwd=self.repo)
        command(
            "git",
            "config",
            "--worktree",
            "url.https://attacker.invalid/.insteadOf",
            "https://github.com/",
            cwd=self.repo,
        )
        try:
            with self.assertRaisesRegex(
                lifecycle.LifecycleError,
                "unsafe worktree-local Git network configuration",
            ):
                lifecycle._network_git_command(self.repo, ["fetch", "origin"])
        finally:
            command(
                "git",
                "config",
                "--worktree",
                "--unset-all",
                "url.https://attacker.invalid/.insteadOf",
                cwd=self.repo,
            )
            command(
                "git",
                "config",
                "--unset-all",
                "extensions.worktreeConfig",
                cwd=self.repo,
            )
            command("git", "remote", "set-url", "origin", original, cwd=self.repo)

    def test_github_https_auth_rejects_custom_origin_vcs_transport(self) -> None:
        original = command("git", "remote", "get-url", "origin", cwd=self.repo)
        command(
            "git",
            "remote",
            "set-url",
            "origin",
            "https://github.com/upiscium/private-fixture.git",
            cwd=self.repo,
        )
        command("git", "config", "remote.origin.vcs", "attacker", cwd=self.repo)
        try:
            with self.assertRaisesRegex(
                lifecycle.LifecycleError,
                "unsafe local Git network configuration",
            ):
                lifecycle._network_git_command(self.repo, ["fetch", "origin"])
        finally:
            command(
                "git",
                "config",
                "--unset-all",
                "remote.origin.vcs",
                cwd=self.repo,
            )
            command("git", "remote", "set-url", "origin", original, cwd=self.repo)

    def test_github_https_auth_rejects_worktree_origin_override(self) -> None:
        original = command("git", "remote", "get-url", "origin", cwd=self.repo)
        command(
            "git",
            "remote",
            "set-url",
            "origin",
            "https://github.com/upiscium/private-fixture.git",
            cwd=self.repo,
        )
        command("git", "config", "extensions.worktreeConfig", "true", cwd=self.repo)
        command(
            "git",
            "config",
            "--worktree",
            "remote.origin.url",
            "https://attacker.invalid/repository.git",
            cwd=self.repo,
        )
        try:
            with self.assertRaisesRegex(
                lifecycle.LifecycleError,
                "unsafe worktree-local Git network configuration",
            ):
                lifecycle._network_git_command(self.repo, ["fetch", "origin"])
        finally:
            command(
                "git",
                "config",
                "--worktree",
                "--unset-all",
                "remote.origin.url",
                cwd=self.repo,
            )
            command(
                "git",
                "config",
                "--unset-all",
                "extensions.worktreeConfig",
                cwd=self.repo,
            )
            command("git", "remote", "set-url", "origin", original, cwd=self.repo)

    def test_ssh_operation_rejects_worktree_origin_override(self) -> None:
        original = command("git", "remote", "get-url", "origin", cwd=self.repo)
        command(
            "git",
            "remote",
            "set-url",
            "origin",
            "git@github.com:upiscium/private-fixture.git",
            cwd=self.repo,
        )
        command("git", "config", "extensions.worktreeConfig", "true", cwd=self.repo)
        command(
            "git",
            "config",
            "--worktree",
            "remote.origin.url",
            "ssh://attacker.invalid/repository.git",
            cwd=self.repo,
        )
        try:
            with self.assertRaisesRegex(
                lifecycle.LifecycleError,
                "unsafe worktree-local Git network configuration",
            ):
                lifecycle._network_git_command(self.repo, ["fetch", "origin"])
        finally:
            command(
                "git",
                "config",
                "--worktree",
                "--unset-all",
                "remote.origin.url",
                cwd=self.repo,
            )
            command(
                "git",
                "config",
                "--unset-all",
                "extensions.worktreeConfig",
                cwd=self.repo,
            )
            command("git", "remote", "set-url", "origin", original, cwd=self.repo)

    def test_public_git_operation_can_succeed_without_github_cli_helper(self) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            lifecycle,
            "_network_git_command",
            return_value=(["git", "fetch", "origin"], True, None),
        ), mock.patch.object(lifecycle, "run", return_value=completed) as run:
            self.assertIs(
                lifecycle.network_git("fetch", "origin", cwd=self.repo),
                completed,
            )
        run.assert_called_once_with(
            ["git", "fetch", "origin"],
            cwd=self.repo,
            check=False,
        )

    def test_private_git_failure_without_github_cli_helper_is_explicit(self) -> None:
        failed = mock.Mock(
            returncode=128,
            stdout="",
            stderr="fatal: could not read Username for 'https://github.com'",
        )
        with mock.patch.object(
            lifecycle,
            "_network_git_command",
            return_value=(["git", "fetch", "origin"], True, None),
        ), mock.patch.object(lifecycle, "run", return_value=failed), self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "GitHub CLI credential helper is unavailable",
        ):
            lifecycle.network_git("fetch", "origin", cwd=self.repo)

    def test_invalid_github_cli_auth_is_reported_without_token_exposure(self) -> None:
        failed = mock.Mock(returncode=128, stdout="", stderr="authentication failed")
        auth_failed = mock.Mock(returncode=1, stdout="", stderr="not logged in")
        helper = Path("/nix/store/example-gh/bin/gh")
        with mock.patch.object(
            lifecycle,
            "_network_git_command",
            return_value=(["git", "fetch", "origin"], True, helper),
        ), mock.patch.object(
            lifecycle,
            "run",
            side_effect=[failed, auth_failed],
        ) as run, self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "GitHub HTTPS authentication is unavailable",
        ):
            lifecycle.network_git("fetch", "origin", cwd=self.repo)

        self.assertEqual(
            run.call_args_list[1].args[0],
            ["gh", "auth", "status", "--hostname", "github.com"],
        )
        self.assertEqual(
            run.call_args_list[1].kwargs["remove_env"],
            ("GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN"),
        )

    def test_github_default_branch_fallback_scrubs_repository_override(self) -> None:
        responses = [
            mock.Mock(returncode=1, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout='{"defaultBranchRef":{"name":"main"}}', stderr=""),
        ]
        with mock.patch.object(lifecycle, "run", side_effect=responses) as observed:
            self.assertEqual(lifecycle.default_branch(self.repo), "main")
        self.assertEqual(
            observed.call_args_list[1].kwargs["remove_env"],
            ("GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN"),
        )


class PostMergeFinalizationTest(RepositoryFixture):
    def test_cleanup_preserves_opencode_project_file(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        foreign = Path(command("git", "rev-parse", "--git-common-dir", cwd=self.repo)) / "opencode"
        if not foreign.is_absolute():
            foreign = self.repo / foreign
        foreign.write_bytes(b"0123456789abcdef0123456789abcdef01234567")
        before = foreign.lstat()
        with self.cleanup_run(evidence):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        after = foreign.lstat()
        self.assertFalse(task_worktree.exists())
        self.assertEqual(foreign.read_bytes(), b"0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(
            (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns),
            (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns),
        )

    def merged_evidence(self, task_worktree: Path, merge_oid: str, **changes: object) -> dict:
        value = {
            "number": 93,
                "state": "MERGED",
                "headRefName": command("git", "branch", "--show-current", cwd=task_worktree),
                "headRefOid": command("git", "rev-parse", "HEAD", cwd=task_worktree),
                "baseRefName": "main",
                "baseRefOid": command("git", "rev-parse", "main", cwd=task_worktree),
                "isCrossRepository": False,
                "mergeCommit": {"oid": merge_oid},
        }
        value.update(changes)
        return value

    def prepare(self) -> tuple[Path, str, dict]:
        task_worktree = self.start_task()
        (task_worktree / "task.txt").write_text("task change\n", encoding="utf-8")
        command("git", "add", "task.txt", cwd=task_worktree)
        command("git", "commit", "-m", "task", cwd=task_worktree)
        task_head = command("git", "rev-parse", "HEAD", cwd=task_worktree)
        merge_oid = self.publish("squash result")
        state = task_worktree / ".task-state/task.md"
        state.write_text(state.read_text(encoding="utf-8").replace("initialized", "integration-pending"), encoding="utf-8")
        return task_worktree, task_head, self.merged_evidence(task_worktree, merge_oid)

    def finalize(self, evidence: dict, task: str = "TASK-1") -> None:
        with (
            mock.patch.object(agent_core, "pr_details", side_effect=[evidence, evidence]),
            mock.patch.object(
                agent_core,
                "prs_for_branch",
                return_value=[{"number": 93, "headRefName": evidence["headRefName"], "baseRefName": "main"}],
            ),
        ):
            agent_core.integrate_finalize(self.repo, task, "93")

    def cleanup_run(
        self,
        evidence: dict,
        *,
        fail_update_ref_once: bool = False,
        fail_worktree_remove_once: bool = False,
        pr_pages: list[list[dict]] | None = None,
    ):
        real_run = lifecycle.run
        failed = False

        def fake(command_args: list[str], **kwargs):
            nonlocal failed
            if command_args[:3] == ["gh", "repo", "view"]:
                return subprocess.CompletedProcess(
                    command_args, 0, json.dumps({"nameWithOwner": "acme/widgets"}), ""
                )
            if command_args[:2] == ["gh", "api"]:
                if pr_pages is not None:
                    return subprocess.CompletedProcess(command_args, 0, json.dumps(pr_pages), "")
                raw = {
                    "number": evidence["number"],
                    "state": "closed" if evidence["state"] == "MERGED" else evidence["state"].lower(),
                    "merged_at": "2026-08-30T00:00:00Z" if evidence["state"] == "MERGED" else None,
                    "draft": bool(evidence.get("draft", False)),
                    "merge_commit_sha": (evidence.get("mergeCommit") or {}).get("oid"),
                    "head": {
                        "ref": evidence["headRefName"],
                        "sha": evidence["headRefOid"],
                        "repo": {
                            "full_name": "other/widgets"
                            if evidence.get("isCrossRepository")
                            else "acme/widgets"
                        },
                    },
                    "base": {
                        "ref": evidence["baseRefName"],
                        "sha": evidence.get("baseRefOid", "c" * 40),
                        "repo": {"full_name": "acme/widgets"},
                    },
                }
                return subprocess.CompletedProcess(command_args, 0, json.dumps([[raw]]), "")
            if (
                fail_update_ref_once
                and not failed
                and command_args[:4] == ["git", "update-ref", "-d", f"refs/heads/{evidence['headRefName']}"]
            ):
                failed = True
                raise lifecycle.LifecycleError("injected ref deletion failure")
            if (
                fail_worktree_remove_once
                and not failed
                and command_args[:3] == ["git", "worktree", "remove"]
            ):
                failed = True
                raise lifecycle.LifecycleError("injected worktree removal failure")
            return real_run(command_args, **kwargs)

        return mock.patch.object(lifecycle, "run", side_effect=fake)

    def merged_cleanup_fixture(
        self, *, delete_remote: bool, task: str = "TASK-1", slug: str = "demo"
    ) -> tuple[Path, str, dict]:
        if task == "TASK-1" and slug == "demo":
            task_worktree, task_head, evidence = self.prepare()
        else:
            task_worktree = self.start_task(task, slug)
            (task_worktree / "task.txt").write_text("task change\n", encoding="utf-8")
            command("git", "add", "task.txt", cwd=task_worktree)
            command("git", "commit", "-m", "task", cwd=task_worktree)
            task_head = command("git", "rev-parse", "HEAD", cwd=task_worktree)
            merge_oid = self.publish("squash result")
            state = task_worktree / ".task-state/task.md"
            state.write_text(
                state.read_text(encoding="utf-8").replace("initialized", "integration-pending"),
                encoding="utf-8",
            )
            evidence = self.merged_evidence(task_worktree, merge_oid)
        branch = evidence["headRefName"]
        command("git", "push", "-u", "origin", branch, cwd=task_worktree)
        self.finalize(evidence, task=task)
        if delete_remote:
            command("git", "push", "origin", "--delete", branch, cwd=task_worktree)
        return task_worktree, task_head, evidence

    def cancelled_cleanup_fixture(self, *, publish_commit: bool) -> tuple[Path, str, dict]:
        task_worktree = self.start_task()
        branch = command("git", "branch", "--show-current", cwd=task_worktree)
        if publish_commit:
            (task_worktree / "cancelled.txt").write_text("cancelled\n", encoding="utf-8")
            command("git", "add", "cancelled.txt", cwd=task_worktree)
            command("git", "commit", "-m", "cancelled task", cwd=task_worktree)
            command("git", "push", "-u", "origin", branch, cwd=task_worktree)
        task_head = command("git", "rev-parse", "HEAD", cwd=task_worktree)
        state = task_worktree / ".task-state/task.md"
        state.write_text(
            state.read_text(encoding="utf-8").replace("Status: initialized", "Status: cancelled"),
            encoding="utf-8",
        )
        evidence = {
            "number": 94,
            "state": "CLOSED",
            "headRefName": branch,
            "headRefOid": task_head,
            "baseRefName": "main",
            "isCrossRepository": False,
            "mergeCommit": None,
        }
        return task_worktree, task_head, evidence

    def test_standard_squash_flow_and_idempotent_retry(self) -> None:
        task_worktree, task_head, evidence = self.prepare()
        self.finalize(evidence)
        self.assertEqual(lifecycle.state_status(task_worktree / ".task-state/task.md"), "merged")
        self.assertFalse(agent_core.merge_commit_is_ancestor(self.repo, task_head, evidence["mergeCommit"]["oid"]))
        self.finalize(evidence)
        self.assertEqual(lifecycle.state_status(task_worktree / ".task-state/task.md"), "merged")

    def test_unresolved_contract_cannot_finalize(self) -> None:
        task_worktree, _, evidence = self.prepare()
        state = task_worktree / ".task-state/task.md"
        state.write_text(
            state.read_text(encoding="utf-8").replace(
                "## Purpose\n\nFinalization fixture", "## Purpose\n\nTBD"
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(agent_core.AutomationError, "unresolved"):
            self.finalize(evidence)
        self.assertEqual(lifecycle.state_status(state), "integration-pending")

    def test_canonical_issue_contract_survives_guarded_finalization(self) -> None:
        lifecycle.task_start(self.repo, "19", "issue-contract")
        task_worktree = self.repo / ".worktrees/19-issue-contract"
        payload = {
            "number": 19,
            "repository_url": "https://api.github.com/repos/acme/widgets",
            "html_url": "https://github.com/acme/widgets/issues/19",
            "title": "Guarded finalization integration",
            "body": "Preserve authoritative Task Contract validation through finalization.",
            "state": "open",
            "labels": [],
            "assignees": [],
            "milestone": None,
        }
        task_contract.hydrate_task_contract(
            task_worktree, "19", "19", payload, "acme/widgets"
        )
        (task_worktree / "task.txt").write_text("task change\n", encoding="utf-8")
        command("git", "add", "task.txt", cwd=task_worktree)
        command("git", "commit", "-m", "task", cwd=task_worktree)
        merge_oid = self.publish("canonical contract squash result")
        state = task_worktree / ".task-state/task.md"
        state.write_text(
            state.read_text(encoding="utf-8").replace(
                "Status: initialized", "Status: integration-pending"
            ),
            encoding="utf-8",
        )
        evidence = self.merged_evidence(task_worktree, merge_oid)
        self.finalize(evidence, task="19")
        self.assertEqual(lifecycle.state_status(state), "merged")
        self.assertEqual("READY", task_contract.validate_contract(task_worktree, "19")["status"])

    def test_finalized_task_is_eligible_for_existing_cleanup(self) -> None:
        task_worktree, _, evidence = self.prepare()
        branch = evidence["headRefName"]
        command("git", "push", "-u", "origin", branch, cwd=task_worktree)
        self.finalize(evidence)
        with self.cleanup_run(evidence):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(task_worktree.exists())
        branch_check = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.repo,
        )
        self.assertNotEqual(branch_check.returncode, 0)

    def test_deleted_upstream_merged_pr_head_match_allows_cleanup(self) -> None:
        task_worktree, task_head, evidence = self.merged_cleanup_fixture(
            delete_remote=True, task="12", slug="semantic-candidate-expansion"
        )
        evidence = dict(evidence, number=18)
        with self.cleanup_run(evidence):
            lifecycle.task_cleanup(self.repo, "12")
        self.assertFalse(task_worktree.exists())
        branch_check = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{evidence['headRefName']}"],
            cwd=self.repo,
        )
        self.assertNotEqual(branch_check.returncode, 0)
        self.assertEqual(evidence["headRefOid"], task_head)

    def test_deleted_upstream_with_local_only_commit_is_rejected(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        (task_worktree / "local-only.txt").write_text("local\n", encoding="utf-8")
        command("git", "add", "local-only.txt", cwd=task_worktree)
        command("git", "commit", "-m", "local only", cwd=task_worktree)
        with self.cleanup_run(evidence), self.assertRaisesRegex(
            lifecycle.LifecycleError, "does not match published PR head"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertTrue(task_worktree.exists())

    def test_deleted_upstream_rejects_unmerged_pr(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        with self.cleanup_run(dict(evidence, state="OPEN")), self.assertRaises(lifecycle.LifecycleError):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertTrue(task_worktree.exists())

    def test_deleted_upstream_rejects_pr_branch_mismatch(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        with self.cleanup_run(dict(evidence, headRefName="task/TASK-OTHER-demo")), self.assertRaises(lifecycle.LifecycleError):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertTrue(task_worktree.exists())

    def test_deleted_upstream_rejects_pr_sha_mismatch(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        with self.cleanup_run(dict(evidence, headRefOid="a" * 40)), self.assertRaises(lifecycle.LifecycleError):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertTrue(task_worktree.exists())

    def test_cleanup_pr_query_paginates_all_historical_branch_prs(self) -> None:
        branch = "task/TASK-1-demo"

        def raw(number: int) -> dict:
            return {
                "number": number,
                "state": "closed",
                "merged_at": "2026-08-30T00:00:00Z",
                "draft": False,
                "merge_commit_sha": "b" * 40,
                "head": {
                    "ref": branch,
                    "sha": "a" * 40,
                    "repo": {"full_name": "acme/widgets"},
                },
                "base": {
                    "ref": "main",
                    "sha": "c" * 40,
                    "repo": {"full_name": "acme/widgets"},
                },
            }

        pages = [[raw(number) for number in range(1, 101)], [raw(101)]]
        with mock.patch.object(
            lifecycle,
            "gh",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(pages), ""),
        ) as query:
            matches = lifecycle.cleanup_prs(self.repo, branch, "acme/widgets")
        self.assertEqual(101, len(matches))
        self.assertIn("--paginate", query.call_args.args)
        self.assertIn("--slurp", query.call_args.args)

    def test_cleanup_rejects_malformed_pr_base_sha(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        raw = {
            "number": evidence["number"],
            "state": "closed",
            "merged_at": "2026-08-30T00:00:00Z",
            "draft": False,
            "merge_commit_sha": evidence["mergeCommit"]["oid"],
            "head": {
                "ref": evidence["headRefName"],
                "sha": evidence["headRefOid"],
                "repo": {"full_name": "acme/widgets"},
            },
            "base": {
                "ref": evidence["baseRefName"],
                "sha": "not-a-sha",
                "repo": {"full_name": "acme/widgets"},
            },
        }
        with self.cleanup_run(evidence, pr_pages=[[raw]]), self.assertRaisesRegex(
            lifecycle.LifecycleError, "does not match the Task"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertTrue(task_worktree.exists())

    def test_cleanup_rejects_pr_base_sha_outside_default_branch_history(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        raw = {
            "number": evidence["number"],
            "state": "closed",
            "merged_at": "2026-08-30T00:00:00Z",
            "draft": False,
            "merge_commit_sha": evidence["mergeCommit"]["oid"],
            "head": {
                "ref": evidence["headRefName"],
                "sha": evidence["headRefOid"],
                "repo": {"full_name": "acme/widgets"},
            },
            "base": {
                "ref": evidence["baseRefName"],
                "sha": "d" * 40,
                "repo": {"full_name": "acme/widgets"},
            },
        }
        with self.cleanup_run(evidence, pr_pages=[[raw]]), self.assertRaisesRegex(
            lifecycle.LifecycleError, "not trusted default-branch history"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertTrue(task_worktree.exists())

    def test_legacy_merged_cleanup_receipt_is_upgraded_on_retry(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        with self.cleanup_run(evidence, fail_update_ref_once=True), self.assertRaisesRegex(
            lifecycle.LifecycleError, "injected ref deletion failure"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(task_worktree.exists())
        receipt = lifecycle.cleanup_receipt_path(self.repo, "TASK-1")
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["evidence"].pop("base_revision")
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        with self.cleanup_run(evidence):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(receipt.exists())

    def test_cleanup_rejects_ambiguity_beyond_first_pr_page(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)

        def raw(number: int) -> dict:
            return {
                "number": number,
                "state": "closed",
                "merged_at": "2026-08-30T00:00:00Z",
                "draft": False,
                "merge_commit_sha": evidence["mergeCommit"]["oid"],
                "head": {
                    "ref": evidence["headRefName"],
                    "sha": evidence["headRefOid"],
                    "repo": {"full_name": "acme/widgets"},
                },
                "base": {
                    "ref": evidence["baseRefName"],
                    "sha": "c" * 40,
                    "repo": {"full_name": "acme/widgets"},
                },
            }

        pages = [[raw(number) for number in range(1, 101)], [raw(101)]]
        with self.cleanup_run(evidence, pr_pages=pages), self.assertRaisesRegex(
            lifecycle.LifecycleError, "missing or ambiguous"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertTrue(task_worktree.exists())

    def test_dirty_merged_worktree_is_rejected(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        (task_worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.cleanup_run(evidence), self.assertRaisesRegex(
            lifecycle.LifecycleError, "uncommitted changes"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertTrue(task_worktree.exists())

    def test_cancelled_missing_upstream_with_unpublished_commit_is_rejected(self) -> None:
        task_worktree = self.start_task()
        (task_worktree / "unpublished.txt").write_text("unpublished\n", encoding="utf-8")
        command("git", "add", "unpublished.txt", cwd=task_worktree)
        command("git", "commit", "-m", "unpublished", cwd=task_worktree)
        state = task_worktree / ".task-state/task.md"
        state.write_text(
            state.read_text(encoding="utf-8").replace("Status: initialized", "Status: cancelled"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(lifecycle.LifecycleError, "unpushed commit"):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertTrue(task_worktree.exists())

    def test_cancelled_tampered_task_base_cannot_hide_unpublished_commit(self) -> None:
        task_worktree = self.start_task()
        (task_worktree / "unpublished.txt").write_text("unpublished\n", encoding="utf-8")
        command("git", "add", "unpublished.txt", cwd=task_worktree)
        command("git", "commit", "-m", "unpublished", cwd=task_worktree)
        task_head = command("git", "rev-parse", "HEAD", cwd=task_worktree)
        state = task_worktree / ".task-state/task.md"
        original_base = lifecycle.extract_identity_value(state, "Base revision")
        self.assertIsNotNone(original_base)
        state.write_text(
            state.read_text(encoding="utf-8")
            .replace("Status: initialized", "Status: cancelled")
            .replace(f"Base revision: {original_base}", f"Base revision: {task_head}"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(lifecycle.LifecycleError, "not trusted"):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertTrue(task_worktree.exists())

    def test_cancelled_receipt_retry_rejects_deleted_published_upstream(self) -> None:
        task_worktree, task_head, evidence = self.cancelled_cleanup_fixture(publish_commit=True)
        branch = evidence["headRefName"]
        with self.cleanup_run(evidence, fail_update_ref_once=True), self.assertRaisesRegex(
            lifecycle.LifecycleError, "injected ref deletion failure"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(task_worktree.exists())
        command("git", "push", "origin", "--delete", branch, cwd=self.repo)
        with self.cleanup_run(evidence), self.assertRaisesRegex(
            lifecycle.LifecycleError, "unpublished commit"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertEqual(task_head, command("git", "rev-parse", branch, cwd=self.repo))
        self.assertTrue(lifecycle.cleanup_receipt_path(self.repo, "TASK-1").exists())

    def test_cancelled_receipt_retry_rejects_rewound_published_upstream(self) -> None:
        task_worktree, task_head, evidence = self.cancelled_cleanup_fixture(publish_commit=True)
        branch = evidence["headRefName"]
        with self.cleanup_run(evidence, fail_update_ref_once=True), self.assertRaisesRegex(
            lifecycle.LifecycleError, "injected ref deletion failure"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(task_worktree.exists())
        command("git", "push", "--force", "origin", f"main:refs/heads/{branch}", cwd=self.repo)
        with self.cleanup_run(evidence), self.assertRaisesRegex(
            lifecycle.LifecycleError, "unpublished commit"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertEqual(task_head, command("git", "rev-parse", branch, cwd=self.repo))
        self.assertTrue(lifecycle.cleanup_receipt_path(self.repo, "TASK-1").exists())

    def test_cancelled_tampered_receipt_base_cannot_hide_unpublished_commit(self) -> None:
        task_worktree, task_head, evidence = self.cancelled_cleanup_fixture(publish_commit=True)
        branch = evidence["headRefName"]
        receipt = lifecycle.cleanup_receipt_path(self.repo, "TASK-1")
        with self.cleanup_run(evidence, fail_update_ref_once=True), self.assertRaisesRegex(
            lifecycle.LifecycleError, "injected ref deletion failure"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(task_worktree.exists())
        command("git", "push", "origin", "--delete", branch, cwd=self.repo)
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["evidence"]["base_revision"] = task_head
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        with self.cleanup_run(evidence), self.assertRaisesRegex(
            lifecycle.LifecycleError, "not trusted"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertEqual(task_head, command("git", "rev-parse", branch, cwd=self.repo))
        self.assertTrue(receipt.exists())

    def test_cancelled_pristine_receipt_retry_uses_base_revision_fallback(self) -> None:
        task_worktree, _, evidence = self.cancelled_cleanup_fixture(publish_commit=False)
        branch = evidence["headRefName"]
        with self.cleanup_run(evidence, fail_update_ref_once=True), self.assertRaisesRegex(
            lifecycle.LifecycleError, "injected ref deletion failure"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(task_worktree.exists())
        with self.cleanup_run(evidence):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(lifecycle.cleanup_receipt_path(self.repo, "TASK-1").exists())
        branch_check = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=self.repo
        )
        self.assertNotEqual(branch_check.returncode, 0)

    def test_cleanup_removes_only_expected_registration_and_branch(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        command("git", "branch", "keep-me", "main", cwd=self.repo)
        with self.cleanup_run(evidence):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(task_worktree.exists())
        self.assertEqual(command("git", "rev-parse", "keep-me", cwd=self.repo), command("git", "rev-parse", "main", cwd=self.repo))

    def test_partial_branch_deletion_failure_is_retryable(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        with self.cleanup_run(evidence, fail_update_ref_once=True), self.assertRaisesRegex(
            lifecycle.LifecycleError, "injected ref deletion failure"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(task_worktree.exists())
        self.assertTrue(lifecycle.cleanup_receipt_path(self.repo, "TASK-1").exists())
        self.assertTrue(command("git", "rev-parse", evidence["headRefName"], cwd=self.repo))
        with self.cleanup_run(evidence):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(lifecycle.cleanup_receipt_path(self.repo, "TASK-1").exists())

    def test_worktree_removal_failure_preserves_registration_and_is_retryable(self) -> None:
        task_worktree, _, evidence = self.merged_cleanup_fixture(delete_remote=True)
        with self.cleanup_run(evidence, fail_worktree_remove_once=True), self.assertRaisesRegex(
            lifecycle.LifecycleError, "injected worktree removal failure"
        ):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertTrue(task_worktree.exists())
        self.assertEqual(lifecycle.worktree_for_task(self.repo, "TASK-1").path, task_worktree)
        self.assertTrue(lifecycle.cleanup_receipt_path(self.repo, "TASK-1").exists())
        with self.cleanup_run(evidence):
            lifecycle.task_cleanup(self.repo, "TASK-1")
        self.assertFalse(task_worktree.exists())

    def test_merge_commit_identity_is_accepted(self) -> None:
        task_worktree = self.start_task()
        (task_worktree / "task.txt").write_text("merge style\n", encoding="utf-8")
        command("git", "add", "task.txt", cwd=task_worktree)
        command("git", "commit", "-m", "task", cwd=task_worktree)
        branch = command("git", "branch", "--show-current", cwd=task_worktree)
        command("git", "push", "-u", "origin", branch, cwd=task_worktree)
        command("git", "fetch", "origin", branch, cwd=self.publisher)
        command("git", "merge", "--no-ff", "FETCH_HEAD", "-m", "merge result", cwd=self.publisher)
        merge_oid = command("git", "rev-parse", "HEAD", cwd=self.publisher)
        self.assertEqual(command("git", "rev-list", "--parents", "-n", "1", "HEAD", cwd=self.publisher).count(" "), 2)
        command("git", "push", "origin", "main", cwd=self.publisher)
        state = task_worktree / ".task-state/task.md"
        state.write_text(state.read_text(encoding="utf-8").replace("initialized", "integration-pending"), encoding="utf-8")
        self.finalize(self.merged_evidence(task_worktree, merge_oid))
        self.assertEqual(lifecycle.state_status(state), "merged")

    def test_rebase_style_merge_identity_is_accepted_without_pr_head_ancestry(self) -> None:
        task_worktree = self.start_task()
        (task_worktree / "task.txt").write_text("rebase style\n", encoding="utf-8")
        command("git", "add", "task.txt", cwd=task_worktree)
        command("git", "commit", "-m", "task", cwd=task_worktree)
        branch = command("git", "branch", "--show-current", cwd=task_worktree)
        task_head = command("git", "rev-parse", "HEAD", cwd=task_worktree)
        command("git", "push", "-u", "origin", branch, cwd=task_worktree)
        self.publish("concurrent main advance")
        command("git", "fetch", "origin", branch, cwd=self.publisher)
        command("git", "cherry-pick", "FETCH_HEAD", cwd=self.publisher)
        merge_oid = command("git", "rev-parse", "HEAD", cwd=self.publisher)
        command("git", "push", "origin", "main", cwd=self.publisher)
        state = task_worktree / ".task-state/task.md"
        state.write_text(state.read_text(encoding="utf-8").replace("initialized", "integration-pending"), encoding="utf-8")
        self.finalize(self.merged_evidence(task_worktree, merge_oid))
        self.assertFalse(agent_core.merge_commit_is_ancestor(self.repo, task_head, merge_oid))
        self.assertEqual(lifecycle.state_status(state), "merged")

    def test_wrong_pr_evidence_and_ambiguity_are_rejected(self) -> None:
        task_worktree, _, evidence = self.prepare()
        invalid_values = (
            dict(evidence, state="OPEN"),
            dict(evidence, state="CLOSED"),
            dict(evidence, headRefName="task/OTHER-demo"),
            dict(evidence, baseRefName="release"),
            dict(evidence, number=94),
            dict(evidence, isCrossRepository=True),
            dict(evidence, mergeCommit=None),
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with mock.patch.object(agent_core, "pr_details", return_value=invalid):
                    with self.assertRaises(agent_core.AutomationError):
                        agent_core.merged_pr_evidence(self.repo, "TASK-1", "93")
        with (
            mock.patch.object(agent_core, "pr_details", return_value=evidence),
            mock.patch.object(agent_core, "prs_for_branch", return_value=[]),
            self.assertRaisesRegex(agent_core.AutomationError, "missing or ambiguous"),
        ):
            agent_core.merged_pr_evidence(self.repo, "TASK-1", "93")

    def test_wrong_task_states_cannot_jump_to_merged(self) -> None:
        task_worktree = self.start_task()
        record = lifecycle.worktree_for_task(self.repo, "TASK-1")
        state = task_worktree / ".task-state/task.md"
        for status in ("implementing", "publication-ready", "draft-pr-created", "blocked"):
            with self.subTest(status=status):
                text = state.read_text(encoding="utf-8")
                text = text[: text.index("- Status:")] + f"- Status: {status}\n"
                state.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(lifecycle.LifecycleError, "requires Task status"):
                    lifecycle.mark_task_merged_from_integration(record, "TASK-1")

    def test_merge_result_must_be_in_synchronized_default_branch(self) -> None:
        _, _, evidence = self.prepare()
        evidence = dict(evidence, mergeCommit={"oid": "a" * 40})
        with (
            mock.patch.object(agent_core, "pr_details", return_value=evidence),
            mock.patch.object(agent_core, "prs_for_branch", return_value=[{"number": 93, "headRefName": evidence["headRefName"]}]),
            self.assertRaisesRegex(agent_core.AutomationError, "not present"),
        ):
            agent_core.integrate_finalize(self.repo, "TASK-1", "93")

    def test_ref_movement_after_evidence_revalidation_does_not_mark_merged(self) -> None:
        task_worktree, _, evidence = self.prepare()
        with (
            mock.patch.object(agent_core, "pr_details", side_effect=[evidence, evidence]),
            mock.patch.object(agent_core, "prs_for_branch", return_value=[{"number": 93, "headRefName": evidence["headRefName"]}]),
            mock.patch.object(
                lifecycle,
                "require_synchronized_default_branch_revision",
                side_effect=lifecycle.LifecycleError("refs moved"),
            ),
            self.assertRaisesRegex(agent_core.AutomationError, "refs moved"),
        ):
            agent_core.integrate_finalize(self.repo, "TASK-1", "93")
        self.assertEqual(
            lifecycle.state_status(task_worktree / ".task-state/task.md"),
            "integration-pending",
        )

    def test_just_exposes_finalize_without_raw_git(self) -> None:
        recipe = (ROOT / "components/agent-core/.automation/just/integrate.just").read_text(encoding="utf-8")
        self.assertIn("finalize task pr:", recipe)
        self.assertIn("integrate finalize", recipe)
        self.assertNotIn("git fetch", recipe)


if __name__ == "__main__":
    unittest.main()
