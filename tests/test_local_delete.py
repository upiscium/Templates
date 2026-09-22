from __future__ import annotations

from contextlib import contextmanager, nullcontext
import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "components" / "agent-core" / ".automation" / "bin"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lifecycle = load_module("task_lifecycle", BIN / "task_lifecycle.py")
task_contract = load_module("task_contract", BIN / "task_contract.py")
local_delete = load_module("local_delete", BIN / "local_delete.py")


class LocalDeleteTest(unittest.TestCase):
    @staticmethod
    def _write_task_state(root: Path, status: str = "implementing") -> Path:
        state_directory = root / ".task-state"
        state_directory.mkdir()
        state = state_directory / "task.md"
        state.write_text(
            "\n".join(
                (
                    "# Task State",
                    "",
                    "- Task ID: 174",
                    "- Branch: task/174-local-delete",
                    f"- Worktree: {root}",
                    "",
                    "## Current state",
                    "",
                    f"- Status: {status}",
                    "",
                    "## Evidence",
                    "",
                    "None yet.",
                    "",
                )
            ),
            encoding="utf-8",
        )
        return state

    @staticmethod
    def _task_record(root: Path) -> lifecycle.WorktreeRecord:
        return lifecycle.WorktreeRecord(root.resolve(), "task/174-local-delete", "a" * 40)

    def test_target_parser_rejects_root_escape_and_shell_syntax(self) -> None:
        for raw in (
            ".",
            "./",
            "..",
            "../outside",
            "/tmp/outside",
            ".build/../outside",
            ".build/*",
            ".build/$(dirname)",
            "foo//bar",
            "foo/",
            ".git",
            ".git/objects",
            ".task-state/task.md",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(local_delete.LocalDeleteError):
                    local_delete.parse_relative_target(raw)

    def test_target_parser_accepts_only_literal_relative_components(self) -> None:
        self.assertEqual(
            local_delete.parse_relative_target(".build/default"),
            (".build", "default"),
        )
        with self.assertRaises(local_delete.LocalDeleteError):
            local_delete.parse_relative_target("cache file")

    def test_nonrecursive_delete_requires_an_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".build" / "default"
            target.mkdir(parents=True)
            (target / "artifact").write_text("artifact", encoding="utf-8")

            with self.assertRaisesRegex(
                local_delete.LocalDeleteError, "requires recursive=true"
            ):
                local_delete.delete_target(root, ".build/default", False)
            self.assertTrue(target.exists())

            (target / "artifact").unlink()
            self.assertEqual(
                local_delete.delete_target(root, ".build/default", False),
                ".build/default",
            )
            self.assertFalse(target.exists())

    def test_recursive_delete_removes_a_nested_literal_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".build" / "default" / "nested"
            target.mkdir(parents=True)
            (target / "artifact").write_text("artifact", encoding="utf-8")

            self.assertEqual(
                local_delete.delete_target(root, ".build", True),
                ".build",
            )
            self.assertFalse((root / ".build").exists())

    def test_recursive_delete_is_descriptor_anchored_and_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".build" / "default"
            target.mkdir(parents=True)
            (target / "artifact").write_text("artifact", encoding="utf-8")
            outside = root.parent / f"{root.name}-outside"
            outside.mkdir()
            (outside / "keep").write_text("keep", encoding="utf-8")
            try:
                (target / "escape").symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(local_delete.LocalDeleteError, "symlink"):
                    local_delete.delete_target(root, ".build", True)
                self.assertTrue((target / "artifact").exists())
                self.assertTrue((outside / "keep").exists())
            finally:
                (target / "escape").unlink(missing_ok=True)
                (outside / "keep").unlink(missing_ok=True)
                outside.rmdir()

    def test_recursive_delete_rejects_a_same_device_top_level_mount_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".build" / "default"
            target.mkdir(parents=True)
            artifact = target / "artifact"
            artifact.write_text("artifact", encoding="utf-8")
            mounted_inode = target.stat().st_ino

            def mount_id(descriptor: int) -> int:
                return 200 if os.fstat(descriptor).st_ino == mounted_inode else 100

            with (
                mock.patch.object(local_delete, "_mount_id", side_effect=mount_id),
                self.assertRaisesRegex(local_delete.LocalDeleteError, "mounted path"),
            ):
                local_delete.delete_target(root, ".build/default", True)

            self.assertTrue(target.exists())
            self.assertTrue(artifact.exists())

    def test_recursive_delete_rejects_a_same_device_nested_mount_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".build"
            nested = target / "default"
            nested.mkdir(parents=True)
            artifact = nested / "artifact"
            artifact.write_text("artifact", encoding="utf-8")
            mounted_inode = nested.stat().st_ino

            def mount_id(descriptor: int) -> int:
                return 200 if os.fstat(descriptor).st_ino == mounted_inode else 100

            with (
                mock.patch.object(local_delete, "_mount_id", side_effect=mount_id),
                self.assertRaisesRegex(local_delete.LocalDeleteError, "mounted path"),
            ):
                local_delete.delete_target(root, ".build", True)

            self.assertTrue(target.exists())
            self.assertTrue(artifact.exists())

    def test_recursive_delete_rejects_a_same_device_file_mount_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".build"
            target.mkdir()
            mounted_file = target / "artifact"
            mounted_file.write_text("artifact", encoding="utf-8")
            mounted_inode = mounted_file.stat().st_ino

            def mount_id(descriptor: int) -> int:
                return 200 if os.fstat(descriptor).st_ino == mounted_inode else 100

            with (
                mock.patch.object(local_delete, "_mount_id", side_effect=mount_id),
                self.assertRaisesRegex(local_delete.LocalDeleteError, "mounted path"),
            ):
                local_delete.delete_target(root, ".build", True)

            self.assertTrue(target.exists())
            self.assertTrue(mounted_file.exists())

    def test_delete_rejects_a_same_device_parent_mount_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / ".build"
            target = parent / "default"
            target.mkdir(parents=True)
            mounted_inode = parent.stat().st_ino

            def mount_id(descriptor: int) -> int:
                return 200 if os.fstat(descriptor).st_ino == mounted_inode else 100

            with (
                mock.patch.object(local_delete, "_mount_id", side_effect=mount_id),
                self.assertRaisesRegex(local_delete.LocalDeleteError, "mounted path"),
            ):
                local_delete.delete_target(root, ".build/default", True)

            self.assertTrue(target.exists())

    def test_mount_identity_failure_is_fail_closed_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".build"
            target.mkdir()
            artifact = target / "artifact"
            artifact.write_text("artifact", encoding="utf-8")

            with (
                mock.patch.object(
                    local_delete,
                    "_mount_id",
                    side_effect=local_delete.LocalDeleteError("mount identity unavailable"),
                ),
                self.assertRaisesRegex(local_delete.LocalDeleteError, "mount identity"),
            ):
                local_delete.delete_target(root, ".build", True)

            self.assertTrue(target.exists())
            self.assertTrue(artifact.exists())

    def test_recursive_delete_rejects_new_entries_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".build"
            target.mkdir()
            (target / "artifact").write_text("artifact", encoding="utf-8")
            protected = root / ".automation"
            protected.mkdir()
            (protected / "policy").write_text("policy", encoding="utf-8")

            original_preflight = local_delete._preflight_directory

            def preflight_then_move(*args, **kwargs):
                original_preflight(*args, **kwargs)
                protected.rename(target / "staging")

            with (
                mock.patch.object(
                    local_delete, "_preflight_directory", side_effect=preflight_then_move
                ),
                self.assertRaisesRegex(
                    local_delete.LocalDeleteError, "contents changed"
                ),
            ):
                local_delete.delete_target(root, ".build", True)

            self.assertTrue((target / "staging" / "policy").exists())
            self.assertTrue((target / "artifact").exists())

    def test_guarded_delete_revalidates_the_current_task_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record = lifecycle.WorktreeRecord(root, "task/174-local-delete", "a" * 40)
            state = root / ".task-state" / "task.md"
            with (
                mock.patch.object(local_delete.lifecycle, "current_worktree", return_value=record),
                mock.patch.object(local_delete.lifecycle, "state_path", return_value=state),
                mock.patch.object(
                    local_delete.lifecycle, "extract_identity_value", return_value="174"
                ),
                mock.patch.object(local_delete.lifecycle, "require_local_task", return_value=record),
                mock.patch.object(local_delete.lifecycle, "require_resolved_contract"),
                mock.patch.object(local_delete.lifecycle, "state_status", return_value="implementing"),
                mock.patch.object(
                    local_delete.lifecycle, "work_units_lock", return_value=nullcontext()
                ),
            ):
                target = root / ".build" / "default"
                target.mkdir(parents=True)
                result = local_delete.guarded_local_delete(root, ".build/default", True)

            self.assertEqual(result["task"], "174")
            self.assertFalse(target.exists())

    def test_guarded_delete_allows_only_the_explicit_implementing_state(self) -> None:
        self.assertEqual(local_delete._MUTABLE_TASK_STATES, frozenset({"implementing"}))
        rejected_states = sorted(
            set(lifecycle.VALID_STATES) - local_delete._MUTABLE_TASK_STATES
        )
        for status in rejected_states:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                record = lifecycle.WorktreeRecord(root, "task/174-local-delete", "a" * 40)
                target = root / ".build" / "default"
                target.mkdir(parents=True)
                with (
                    mock.patch.object(
                        local_delete.lifecycle, "current_worktree", return_value=record
                    ),
                    mock.patch.object(
                        local_delete.lifecycle, "state_path", return_value=root / "task.md"
                    ),
                    mock.patch.object(
                        local_delete.lifecycle, "extract_identity_value", return_value="174"
                    ),
                    mock.patch.object(
                        local_delete.lifecycle, "require_local_task", return_value=record
                    ),
                    mock.patch.object(local_delete.lifecycle, "require_resolved_contract"),
                    mock.patch.object(
                        local_delete.lifecycle, "state_status", return_value=status
                    ),
                    mock.patch.object(
                        local_delete.lifecycle, "work_units_lock", return_value=nullcontext()
                    ),
                ):
                    with self.assertRaisesRegex(
                        local_delete.LocalDeleteError, "explicit mutable state"
                    ):
                        local_delete.guarded_local_delete(root, ".build/default", True)
                self.assertTrue(target.exists())

    def test_locked_contract_validation_reuses_the_canonical_lock_fd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = self._write_task_state(root)
            headings = (
                "Purpose",
                "Scope",
                "Prohibited changes",
                "Dependencies",
                "Acceptance criteria",
                "Test plan",
                "Stop conditions",
                "Coordination surfaces",
                "External resources",
            )
            state.write_text(
                state.read_text(encoding="utf-8")
                + "\n"
                + "\n".join(f"## {heading}\n\ncontent" for heading in headings)
                + "\n\ncanonical-contract sha256="
                + "a" * 64
                + " issue=174\n",
                encoding="utf-8",
            )
            (root / ".task-state" / "contract.json").write_text("{}\n", encoding="utf-8")
            record = self._task_record(root)
            captured_directory_fds: list[int] = []

            def validate_locked(
                locked_root: Path,
                locked_task: str,
                *,
                require_pristine: bool,
                directory_fd: int,
            ) -> dict[str, str]:
                self.assertEqual(locked_root, root)
                self.assertEqual(locked_task, "174")
                self.assertFalse(require_pristine)
                captured_directory_fds.append(directory_fd)
                return {"status": "READY"}

            with (
                mock.patch.object(
                    task_contract,
                    "_validate_contract_locked",
                    side_effect=validate_locked,
                ),
                mock.patch.object(
                    task_contract,
                    "contract_state_lock",
                    side_effect=AssertionError("nested contract lock"),
                ),
            ):
                with lifecycle.work_units_lock(record) as directory_fd:
                    lifecycle.require_resolved_contract(
                        record,
                        "174",
                        directory_fd=directory_fd,
                    )

            self.assertEqual(len(captured_directory_fds), 1)
            self.assertGreaterEqual(captured_directory_fds[0], 0)

    def test_delete_holds_canonical_lock_until_mutation_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = self._write_task_state(root)
            record = self._task_record(root)
            target = root / ".build" / "default"
            target.mkdir(parents=True)
            (target / "artifact").write_text("artifact", encoding="utf-8")
            delete_paused = threading.Event()
            resume_delete = threading.Event()
            transition_started = threading.Event()
            transition_lock_acquired = threading.Event()
            local_errors: list[BaseException] = []
            transition_errors: list[BaseException] = []
            local_result: list[dict[str, object]] = []
            original_delete = local_delete.delete_target
            original_work_units_lock = lifecycle.work_units_lock

            @contextmanager
            def tracked_work_units_lock(locked_record: lifecycle.WorktreeRecord):
                with original_work_units_lock(locked_record) as directory_fd:
                    if transition_started.is_set():
                        transition_lock_acquired.set()
                    yield directory_fd

            def paused_delete(*args, **kwargs):
                delete_paused.set()
                if not resume_delete.wait(5):
                    raise AssertionError("delete did not receive its resume signal")
                return original_delete(*args, **kwargs)

            def run_delete() -> None:
                try:
                    local_result.append(
                        local_delete.guarded_local_delete(root, ".build/default", True)
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    local_errors.append(exc)

            def run_transition() -> None:
                try:
                    transition_started.set()
                    lifecycle.task_state_set(root, "174", "verification-pending")
                except BaseException as exc:  # pragma: no cover - asserted below
                    transition_errors.append(exc)

            with (
                mock.patch.object(local_delete.lifecycle, "current_worktree", return_value=record),
                mock.patch.object(local_delete.lifecycle, "require_local_task", return_value=record),
                mock.patch.object(local_delete.lifecycle, "require_resolved_contract"),
                mock.patch.object(local_delete.lifecycle, "assert_task_identity"),
                mock.patch.object(local_delete.lifecycle, "work_units_lock", tracked_work_units_lock),
                mock.patch.object(local_delete, "delete_target", side_effect=paused_delete),
            ):
                delete_thread = threading.Thread(target=run_delete)
                transition_thread = threading.Thread(target=run_transition)
                delete_thread.start()
                self.assertTrue(delete_paused.wait(5))
                self.assertEqual(lifecycle.state_status(state), "implementing")
                transition_thread.start()
                self.assertTrue(transition_started.is_set())
                self.assertFalse(transition_lock_acquired.wait(0.2))
                resume_delete.set()
                delete_thread.join(5)
                transition_thread.join(5)

            self.assertFalse(delete_thread.is_alive())
            self.assertFalse(transition_thread.is_alive())
            self.assertEqual(local_errors, [])
            self.assertEqual(transition_errors, [])
            self.assertEqual(local_result[0]["status"], "deleted")
            self.assertTrue(transition_lock_acquired.is_set())
            self.assertFalse(target.exists())
            self.assertEqual(lifecycle.state_status(state), "verification-pending")

    def test_transition_wins_lock_and_local_delete_revalidates_fresh_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = self._write_task_state(root)
            record = self._task_record(root)
            target = root / ".build" / "default"
            target.mkdir(parents=True)
            artifact = target / "artifact"
            artifact.write_text("artifact", encoding="utf-8")
            transition_errors: list[BaseException] = []

            def run_transition() -> None:
                try:
                    lifecycle.task_state_set(root, "174", "verification-pending")
                except BaseException as exc:  # pragma: no cover - asserted below
                    transition_errors.append(exc)

            with (
                mock.patch.object(local_delete.lifecycle, "current_worktree", return_value=record),
                mock.patch.object(local_delete.lifecycle, "require_local_task", return_value=record),
                mock.patch.object(local_delete.lifecycle, "require_resolved_contract"),
                mock.patch.object(local_delete.lifecycle, "assert_task_identity"),
            ):
                transition_thread = threading.Thread(target=run_transition)
                transition_thread.start()
                transition_thread.join(5)
                self.assertFalse(transition_thread.is_alive())
                self.assertEqual(transition_errors, [])
                self.assertEqual(lifecycle.state_status(state), "verification-pending")
                with self.assertRaisesRegex(local_delete.LocalDeleteError, "explicit mutable state"):
                    local_delete.guarded_local_delete(root, ".build/default", True)

            self.assertTrue(target.exists())
            self.assertTrue(artifact.exists())


if __name__ == "__main__":
    unittest.main()
