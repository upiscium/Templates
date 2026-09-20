from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "components" / "agent-core" / ".automation" / "bin"

lifecycle_spec = importlib.util.spec_from_file_location(
    "task_lifecycle", BIN / "task_lifecycle.py"
)
assert lifecycle_spec and lifecycle_spec.loader
lifecycle = importlib.util.module_from_spec(lifecycle_spec)
sys.modules[lifecycle_spec.name] = lifecycle
lifecycle_spec.loader.exec_module(lifecycle)

discard_spec = importlib.util.spec_from_file_location(
    "discard_pristine", BIN / "discard_pristine.py"
)
assert discard_spec and discard_spec.loader
discard = importlib.util.module_from_spec(discard_spec)
sys.modules[discard_spec.name] = discard
discard_spec.loader.exec_module(discard)


def command(*args: str, cwd: Path, check: bool = True) -> str:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"{' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


class PristineDiscardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary_root = Path(self.temporary.name)
        self.repo = temporary_root / "repo"
        self.origin = temporary_root / "origin.git"
        self.repo.mkdir()
        command("git", "init", "-b", "main", cwd=self.repo)
        command("git", "config", "user.name", "Agent Core Test", cwd=self.repo)
        command("git", "config", "user.email", "agent-core@example.invalid", cwd=self.repo)

        template = self.repo / ".automation/templates/task-state.md"
        template.parent.mkdir(parents=True)
        template.write_bytes(
            (
                ROOT
                / "components"
                / "agent-core"
                / ".automation"
                / "templates"
                / "task-state.md"
            ).read_bytes()
        )
        (self.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        command("git", "add", ".", cwd=self.repo)
        command("git", "commit", "-m", "seed", cwd=self.repo)

        command("git", "init", "--bare", str(self.origin), cwd=temporary_root)
        command("git", "remote", "add", "origin", str(self.origin), cwd=self.repo)
        command("git", "push", "-u", "origin", "main", cwd=self.repo)
        command(
            "git",
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
            cwd=self.repo,
        )
        self.initial_main = command("git", "rev-parse", "main", cwd=self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def github(self, prs: list[dict] | None = None) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(
                lifecycle,
                "cleanup_repository",
                return_value="acme/widgets",
            )
        )
        stack.enter_context(
            mock.patch.object(
                lifecycle,
                "cleanup_prs",
                return_value=[] if prs is None else prs,
            )
        )
        return stack

    def start(self, task: str = "13", slug: str = "hybrid-level1-reranking") -> Path:
        record = lifecycle.task_start(
            self.repo,
            task,
            slug,
            quiet=True,
        )
        return record.path

    def advance_main(self) -> str:
        (self.repo / "new-main.txt").write_text("new main\n", encoding="utf-8")
        command("git", "add", "new-main.txt", cwd=self.repo)
        command("git", "commit", "-m", "advance main", cwd=self.repo)
        command("git", "push", "origin", "main", cwd=self.repo)
        return command("git", "rev-parse", "main", cwd=self.repo)

    def test_akv_shape_discards_and_same_issue_restarts_from_current_main(self) -> None:
        task_worktree = self.start()
        self.assertEqual(self.initial_main, command("git", "rev-parse", "HEAD", cwd=task_worktree))
        current_main = self.advance_main()

        with self.github():
            discard.task_discard_pristine(self.repo, "13")

        self.assertFalse(task_worktree.exists())
        self.assertNotEqual(
            0,
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/task/13-hybrid-level1-reranking"],
                cwd=self.repo,
            ).returncode,
        )

        def hydrate(
            worktree: Path,
            task: str,
            issue: str,
            payload: dict,
            identity: str,
        ) -> None:
            state = worktree / ".task-state/task.md"
            text = state.read_text(encoding="utf-8")
            text = text.replace("TBD", "Restarted canonical contract")
            text = text.replace(
                "- [ ] Define Task-specific acceptance criteria",
                "- [ ] Implement Issue-backed acceptance criteria",
            )
            text = text.replace(
                "- Unverified: Task contract",
                "- Unverified: none",
            )
            text += "\ncanonical-contract sha256=" + ("a" * 64) + "\n"
            state.write_text(text, encoding="utf-8")
            (worktree / ".task-state/contract.json").write_text(
                json.dumps({"issue": int(issue), "repository": identity}),
                encoding="utf-8",
            )

        fake_contract = mock.Mock(
            ContractError=lifecycle.LifecycleError,
            fetch_issue=mock.Mock(return_value=("acme/widgets", {})),
            hydrate_task_contract=mock.Mock(side_effect=hydrate),
        )
        with mock.patch.dict(sys.modules, {"task_contract": fake_contract}):
            lifecycle.task_start_from_issue(
                self.repo,
                "13",
                "hybrid-level1-reranking",
            )

        restarted = lifecycle.worktree_for_task(self.repo, "13")
        self.assertEqual(current_main, restarted.head)
        state = lifecycle.state_path(restarted.path).read_text(encoding="utf-8")
        self.assertIn(f"- Base revision: {current_main}", state)

    def test_discard_preserves_opencode_project_file(self) -> None:
        task_worktree = self.start()
        foreign = Path(command("git", "rev-parse", "--git-common-dir", cwd=self.repo)) / "opencode"
        if not foreign.is_absolute():
            foreign = self.repo / foreign
        foreign.write_bytes(b"0123456789abcdef0123456789abcdef01234567")
        before = foreign.lstat()
        with self.github():
            discard.task_discard_pristine(self.repo, "13")
        after = foreign.lstat()
        self.assertFalse(task_worktree.exists())
        self.assertEqual(foreign.read_bytes(), b"0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(
            (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns),
            (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns),
        )

    def test_dirty_worktree_is_rejected(self) -> None:
        task_worktree = self.start()
        (task_worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.github(), self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "uncommitted changes",
        ):
            discard.task_discard_pristine(self.repo, "13")
        self.assertTrue(task_worktree.exists())

    def test_head_beyond_base_is_rejected(self) -> None:
        task_worktree = self.start()
        (task_worktree / "implementation.txt").write_text("work\n", encoding="utf-8")
        command("git", "add", "implementation.txt", cwd=task_worktree)
        command("git", "commit", "-m", "implementation", cwd=task_worktree)
        with self.github(), self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "HEAD differs from Base revision",
        ):
            discard.task_discard_pristine(self.repo, "13")
        self.assertTrue(task_worktree.exists())

    def test_work_unit_evidence_is_rejected(self) -> None:
        task_worktree = self.start()
        record = lifecycle.worktree_for_task(self.repo, "13")
        payload = lifecycle.empty_work_units(record, "13")
        payload["units"]["WU-13-01"] = {
            "id": "WU-13-01",
            "state": "completed",
        }
        lifecycle.atomic_json(
            lifecycle.work_units_path(task_worktree),
            payload,
        )
        with self.github(), self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "lifecycle evidence|Work Units",
        ):
            discard.task_discard_pristine(self.repo, "13")
        self.assertTrue(task_worktree.exists())

    def test_contract_metadata_is_rejected(self) -> None:
        task_worktree = self.start()
        (task_worktree / ".task-state/contract.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        with self.github(), self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "lifecycle evidence|Contract metadata",
        ):
            discard.task_discard_pristine(self.repo, "13")
        self.assertTrue(task_worktree.exists())

    def test_non_initialized_task_is_rejected(self) -> None:
        task_worktree = self.start()
        state = lifecycle.state_path(task_worktree)
        state.write_text(
            state.read_text(encoding="utf-8").replace(
                "- Status: initialized",
                "- Status: planning",
            ),
            encoding="utf-8",
        )
        with self.github(), self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "status must be exactly initialized",
        ):
            discard.task_discard_pristine(self.repo, "13")
        self.assertTrue(task_worktree.exists())

    def test_pull_request_evidence_is_rejected(self) -> None:
        task_worktree = self.start()
        with self.github(
            [
                {
                    "number": 99,
                    "state": "CLOSED",
                    "headRefName": "task/13-hybrid-level1-reranking",
                }
            ]
        ), self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "pull request evidence",
        ):
            discard.task_discard_pristine(self.repo, "13")
        self.assertTrue(task_worktree.exists())

    def test_remote_publication_evidence_is_rejected(self) -> None:
        task_worktree = self.start()
        branch = command("git", "branch", "--show-current", cwd=task_worktree)
        command("git", "push", "-u", "origin", branch, cwd=task_worktree)
        command("git", "push", "origin", "--delete", branch, cwd=task_worktree)
        with self.github(), self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "publication configuration",
        ):
            discard.task_discard_pristine(self.repo, "13")
        self.assertTrue(task_worktree.exists())

    def test_operation_requires_main_authority(self) -> None:
        task_worktree = self.start()
        with self.github(), self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "default-branch worktree",
        ):
            discard.task_discard_pristine(task_worktree, "13")

    def test_only_expected_worktree_and_branch_are_removed(self) -> None:
        task_worktree = self.start()
        command("git", "branch", "keep-me", "main", cwd=self.repo)
        with self.github():
            discard.task_discard_pristine(self.repo, "13")
        self.assertFalse(task_worktree.exists())
        self.assertEqual(
            command("git", "rev-parse", "main", cwd=self.repo),
            command("git", "rev-parse", "keep-me", cwd=self.repo),
        )

    def test_partial_delete_retry_fails_closed_if_remote_publication_appears(self) -> None:
        task_worktree = self.start()
        branch = command("git", "branch", "--show-current", cwd=task_worktree)
        real_run = lifecycle.run
        failed = False

        def fail_ref_delete_once(command_args: list[str], **kwargs):
            nonlocal failed
            if (
                not failed
                and command_args[:4]
                == ["git", "update-ref", "-d", f"refs/heads/{branch}"]
            ):
                failed = True
                raise lifecycle.LifecycleError("injected ref deletion failure")
            return real_run(command_args, **kwargs)

        with self.github(), mock.patch.object(
            lifecycle,
            "run",
            side_effect=fail_ref_delete_once,
        ), self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "injected ref deletion failure",
        ):
            discard.task_discard_pristine(self.repo, "13")

        self.assertFalse(task_worktree.exists())
        self.assertTrue(discard.discard_receipt_path(self.repo, "13").exists())
        command(
            "git",
            "push",
            "origin",
            f"{branch}:refs/heads/{branch}",
            cwd=self.repo,
        )

        with self.github(), self.assertRaisesRegex(
            lifecycle.LifecycleError,
            "publication configuration|live remote Task branch exists",
        ):
            discard.task_discard_pristine(self.repo, "13")
        self.assertEqual(
            self.initial_main,
            command("git", "rev-parse", branch, cwd=self.repo),
        )
        self.assertTrue(discard.discard_receipt_path(self.repo, "13").exists())

    def test_permission_surface_is_explicitly_approval_gated(self) -> None:
        config = json.loads(
            (ROOT / "components/agent-core/opencode.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "ask",
            config["permission"]["bash"]["just agent::discard-pristine *"],
        )
        justfile = (
            ROOT
            / "components"
            / "agent-core"
            / ".automation"
            / "just"
            / "agent.just"
        ).read_text(encoding="utf-8")
        self.assertIn("discard-pristine task:", justfile)

    def test_generated_discard_scripts_match_source(self) -> None:
        source = (
            ROOT
            / "components"
            / "agent-core"
            / ".automation"
            / "bin"
            / "discard_pristine.py"
        ).read_bytes()
        for name in (
            "agent-base",
            "agent-cpp-cmake",
            "agent-nix",
            "agent-python",
            "agent-rust",
            "agent-typescript-node",
        ):
            generated = (
                ROOT
                / "templates"
                / name
                / ".automation"
                / "bin"
                / "discard_pristine.py"
            )
            self.assertEqual(source, generated.read_bytes(), generated.as_posix())


if __name__ == "__main__":
    unittest.main()
