from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "components" / "agent-core" / ".automation" / "bin" / "task_lifecycle.py"
spec = importlib.util.spec_from_file_location("task_lifecycle", MODULE_PATH)
assert spec and spec.loader
lifecycle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lifecycle
spec.loader.exec_module(lifecycle)


class TaskLifecycleTest(unittest.TestCase):
    def test_generic_state_set_cannot_cross_publication_boundaries(self) -> None:
        record = lifecycle.WorktreeRecord(Path("/task"), "task/101-metadata", "a" * 40)
        for status in ("draft-pr-created", "integration-pending"):
            with (
                mock.patch.object(lifecycle, "require_local_task", return_value=record),
                mock.patch.object(lifecycle, "require_resolved_contract"),
                self.assertRaisesRegex(lifecycle.LifecycleError, "guarded pull request publication"),
            ):
                lifecycle.task_state_set(Path("/task"), "101", status)

    def test_task_branch_matching_is_not_substring_based(self) -> None:
        self.assertTrue(lifecycle.branch_matches_task("task/TASK-1-example", "TASK-1"))
        self.assertFalse(lifecycle.branch_matches_task("task/TASK-10-example", "TASK-1"))

    def test_state_transition_rejects_invalid_jump(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_text("## Current state\n\n- Status: initialized\n", encoding="utf-8")
            with self.assertRaisesRegex(lifecycle.LifecycleError, "invalid Task State transition"):
                lifecycle.set_state_status(path, "merged")
            lifecycle.set_state_status(path, "planning")
            self.assertEqual(lifecycle.state_status(path), "planning")

    def test_generic_state_set_cannot_recover_blocked_to_publication_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state/task.md"
            state.parent.mkdir()
            state.write_text("## Current state\n\n- Status: blocked\n", encoding="utf-8")
            record = lifecycle.WorktreeRecord(root, "task/148-recovery", "a" * 40)
            with mock.patch.object(lifecycle, "require_local_task", return_value=record), \
                 mock.patch.object(lifecycle, "require_resolved_contract"), \
                 mock.patch.object(lifecycle, "assert_task_identity"), \
                 mock.patch.object(lifecycle, "work_units_lock", return_value=mock.MagicMock(
                     __enter__=mock.Mock(return_value=None), __exit__=mock.Mock(return_value=False)
                 )), \
                 self.assertRaisesRegex(lifecycle.LifecycleError, "invalid Task State transition"):
                lifecycle.task_state_set(root, "148", "publication-ready")
            self.assertEqual(lifecycle.state_status(state), "blocked")

    def test_work_units_lock_rejects_a_symlinked_lock_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state"
            state.mkdir()
            target = root / "outside.lock"
            target.write_bytes(b"")
            (state / "work-units.lock").symlink_to(target)
            record = lifecycle.WorktreeRecord(root, "task/148-lock", "a" * 40)
            with self.assertRaisesRegex(lifecycle.LifecycleError, "lock"):
                with lifecycle.work_units_lock(record):
                    pass

    def test_read_work_units_rejects_non_object_and_non_object_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state"
            state.mkdir()
            record = lifecycle.WorktreeRecord(root, "task/148-schema", "a" * 40)
            path = state / "work-units.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(lifecycle.LifecycleError, "state schema"):
                lifecycle.read_work_units(record, "148")
            path.write_text(
                '{"schema_version":1,"task_id":"148","worktree":"%s",'
                '"branch":"task/148-schema","units":{"WU-148-01":[]}}\n' % root,
                encoding="utf-8",
            )
            with self.assertRaisesRegex(lifecycle.LifecycleError, "invalid Work Unit record"):
                lifecycle.read_work_units(record, "148")

    def test_read_work_units_rejects_nonregular_or_symlinked_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state"
            state.mkdir()
            record = lifecycle.WorktreeRecord(root, "task/148-files", "a" * 40)
            (state / "work-units.json").mkdir()
            with self.assertRaisesRegex(lifecycle.LifecycleError, "state file is not regular"):
                lifecycle.read_work_units(record, "148")
            (state / "work-units.json").rmdir()
            target = root / "outside-work-units.json"
            target.write_text("{}", encoding="utf-8")
            (state / "work-units.json").symlink_to(target)
            with self.assertRaisesRegex(lifecycle.LifecycleError, "safely readable"):
                lifecycle.read_work_units(record, "148")

    def test_guarded_blocked_publication_recovery_is_exact_locked_status_cas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state/task.md"
            state.parent.mkdir()
            original = b"prefix\n## Current state\n\n- Status: blocked\n- Blockers: publication only\nsuffix\n"
            state.write_bytes(original)
            evidence = {
                "work-units.json": b"units",
                "verification.json": b"verification",
                "contract.json": b"contract",
                "issue.json": None,
            }
            for name, content in evidence.items():
                if content is not None:
                    (state.parent / name).write_bytes(content)
            record = lifecycle.WorktreeRecord(root, "task/148-recovery", "a" * 40)
            with mock.patch.object(lifecycle, "current_worktree", return_value=record), \
                 mock.patch.object(lifecycle, "worktree_for_task", return_value=record), \
                 mock.patch.object(lifecycle, "require_resolved_contract"), \
                 mock.patch.object(lifecycle, "assert_task_identity"), \
                 mock.patch.object(lifecycle.private_state, "prepare"), \
                 mock.patch.object(lifecycle.private_state, "publication_recovery_receipt", return_value=root / "receipt"), \
                 mock.patch.object(lifecycle.private_state, "post_merge_publication_recovery_receipt", return_value=root / "post-receipt"), \
                 mock.patch.object(lifecycle.private_state, "_validate_legacy_content"), \
                 mock.patch.object(lifecycle.private_state, "_validate_publication_recovery_topology"), \
                 mock.patch.object(lifecycle.private_state, "topology"), \
                 mock.patch.object(lifecycle.private_state, "mutation_lock", return_value=nullcontext()), \
                 mock.patch.object(lifecycle.private_state, "exclusive_write_bytes", side_effect=lambda path, content, **_: path.write_bytes(content)):
                result = lifecycle.recover_blocked_publication_ready(
                    record, "148", original, evidence, b"{}"
                )
            self.assertEqual(result, "transitioned")
            self.assertEqual(
                state.read_bytes(),
                original.replace(b"- Status: blocked", b"- Status: publication-ready"),
            )

    def test_guarded_blocked_publication_recovery_rejects_stale_state_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state/task.md"
            state.parent.mkdir()
            state.write_text("## Current state\n\n- Status: blocked\n", encoding="utf-8")
            record = lifecycle.WorktreeRecord(root, "task/148-recovery", "a" * 40)
            evidence = {
                "work-units.json": None,
                "verification.json": None,
                "contract.json": None,
                "issue.json": None,
            }
            with mock.patch.object(lifecycle, "current_worktree", return_value=record), \
                 mock.patch.object(lifecycle, "worktree_for_task", return_value=record), \
                 mock.patch.object(lifecycle, "require_resolved_contract"), \
                 mock.patch.object(lifecycle, "assert_task_identity"), \
                 mock.patch.object(lifecycle.private_state, "prepare"), \
                 mock.patch.object(lifecycle.private_state, "publication_recovery_receipt", return_value=root / "receipt"), \
                 mock.patch.object(lifecycle.private_state, "post_merge_publication_recovery_receipt", return_value=root / "post-receipt"), \
                 mock.patch.object(lifecycle.private_state, "_validate_legacy_content"), \
                 mock.patch.object(lifecycle.private_state, "_validate_publication_recovery_topology"), \
                 mock.patch.object(lifecycle.private_state, "topology"), \
                 mock.patch.object(lifecycle.private_state, "mutation_lock", return_value=nullcontext()), \
                 mock.patch.object(lifecycle.private_state, "exclusive_write_bytes", side_effect=lambda path, content, **_: path.write_bytes(content)), \
                 self.assertRaisesRegex(lifecycle.LifecycleError, "Task State changed"):
                lifecycle.recover_blocked_publication_ready(
                    record, "148", b"stale", evidence, b"{}"
                )
            self.assertEqual(lifecycle.state_status(state), "blocked")
            self.assertEqual((root / "receipt").read_bytes(), b"{}")

    def test_guarded_blocked_publication_recovery_rejects_any_evidence_change(self) -> None:
        for case in (
            "work-units.json",
            "verification.json",
            "contract.json",
            "issue.json",
            "issue-value",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / ".task-state/task.md"
                state.parent.mkdir()
                original = b"## Current state\n\n- Status: blocked\n"
                state.write_bytes(original)
                evidence = {
                    "work-units.json": b"units",
                    "verification.json": b"verification",
                    "contract.json": b"contract",
                    "issue.json": b"issue" if case == "issue-value" else None,
                }
                for name, content in evidence.items():
                    if content is not None:
                        (state.parent / name).write_bytes(content)
                changed_name = "issue.json" if case == "issue-value" else case
                (state.parent / changed_name).write_bytes(b"changed")
                record = lifecycle.WorktreeRecord(root, "task/148-recovery", "a" * 40)
                with mock.patch.object(lifecycle, "current_worktree", return_value=record), \
                     mock.patch.object(lifecycle, "worktree_for_task", return_value=record), \
                     mock.patch.object(lifecycle, "require_resolved_contract"), \
                     mock.patch.object(lifecycle, "assert_task_identity"), \
                     mock.patch.object(lifecycle.private_state, "prepare"), \
                     mock.patch.object(lifecycle.private_state, "publication_recovery_receipt", return_value=root / "receipt"), \
                     mock.patch.object(lifecycle.private_state, "post_merge_publication_recovery_receipt", return_value=root / "post-receipt"), \
                     mock.patch.object(lifecycle.private_state, "_validate_legacy_content"), \
                     mock.patch.object(lifecycle.private_state, "_validate_publication_recovery_topology"), \
                     mock.patch.object(lifecycle.private_state, "topology"), \
                     mock.patch.object(lifecycle.private_state, "mutation_lock", return_value=nullcontext()), \
                     mock.patch.object(lifecycle.private_state, "exclusive_write_bytes", side_effect=lambda path, content, **_: path.write_bytes(content)), \
                     self.assertRaisesRegex(lifecycle.LifecycleError, "publication evidence changed"):
                    lifecycle.recover_blocked_publication_ready(
                        record, "148", original, evidence, b"{}"
                    )
                self.assertEqual(state.read_bytes(), original)

    def test_completed_blocked_recovery_consumes_exact_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state/task.md"
            state.parent.mkdir()
            final = b"## Current state\n\n- Status: draft-pr-created\n"
            state.write_bytes(final)
            evidence = {
                "work-units.json": b"units",
                "verification.json": b"verification",
                "contract.json": b"contract",
                "issue.json": None,
            }
            for name, content in evidence.items():
                if content is not None:
                    (state.parent / name).write_bytes(content)
            receipt = root / "receipt"
            receipt.write_bytes(b"receipt")
            record = lifecycle.WorktreeRecord(root, "task/148-recovery", "a" * 40)

            def consume(path: Path, **_: object) -> None:
                path.unlink()

            with mock.patch.object(lifecycle, "current_worktree", return_value=record), \
                 mock.patch.object(lifecycle, "worktree_for_task", return_value=record), \
                 mock.patch.object(lifecycle, "require_resolved_contract"), \
                 mock.patch.object(lifecycle, "assert_task_identity"), \
                 mock.patch.object(lifecycle.private_state, "prepare"), \
                 mock.patch.object(lifecycle.private_state, "publication_recovery_receipt", return_value=receipt), \
                 mock.patch.object(lifecycle.private_state, "mutation_lock", return_value=nullcontext()), \
                 mock.patch.object(lifecycle.private_state, "read_bytes_identity", return_value=(b"receipt", (1, 2))), \
                 mock.patch.object(lifecycle.private_state, "unlink", side_effect=consume) as unlink:
                result = lifecycle.complete_blocked_publication_recovery(
                    record, "148", final, evidence, b"receipt"
                )
            self.assertEqual(result, "consumed")
            self.assertFalse(receipt.exists())
            unlink.assert_called_once()

    def test_guarded_post_merge_recovery_is_exact_state_and_evidence_cas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state/task.md"
            state.parent.mkdir()
            original = b"## Current state\n\n- Status: draft-pr-created\n"
            state.write_bytes(original)
            evidence = {
                "work-units.json": b"units",
                "verification.json": b"verification",
                "contract.json": b"contract",
                "issue.json": None,
            }
            for name, content in evidence.items():
                if content is not None:
                    (state.parent / name).write_bytes(content)
            record = lifecycle.WorktreeRecord(root, "task/225-recovery", "a" * 40)
            with mock.patch.object(lifecycle, "current_worktree", return_value=record), \
                 mock.patch.object(lifecycle, "worktree_for_task", return_value=record), \
                 mock.patch.object(lifecycle, "require_resolved_contract"), \
                 mock.patch.object(lifecycle, "assert_task_identity"), \
                 mock.patch.object(lifecycle.private_state, "prepare"), \
                 mock.patch.object(lifecycle.private_state, "post_merge_publication_recovery_receipt", return_value=root / "receipt"), \
                 mock.patch.object(lifecycle.private_state, "publication_recovery_receipt", return_value=root / "blocked-receipt"), \
                 mock.patch.object(lifecycle.private_state, "_validate_legacy_content"), \
                 mock.patch.object(lifecycle.private_state, "_validate_publication_recovery_topology"), \
                 mock.patch.object(lifecycle.private_state, "topology"), \
                 mock.patch.object(lifecycle.private_state, "mutation_lock", return_value=nullcontext()), \
                 mock.patch.object(lifecycle.private_state, "exclusive_write_bytes", side_effect=lambda path, content, **_: path.write_bytes(content)):
                result = lifecycle.recover_post_merge_publication_pending(
                    record, "225", original, evidence, b"{}"
                )
            self.assertEqual(result, "transitioned")
            self.assertEqual(
                state.read_bytes(),
                original.replace(b"draft-pr-created", b"integration-pending"),
            )

    def test_guarded_post_merge_recovery_rejects_duplicate_status_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state/task.md"
            state.parent.mkdir()
            original = (
                b"## Current state\n\n"
                b"- Status: draft-pr-created\n"
                b"- Status: draft-pr-created\n"
            )
            state.write_bytes(original)
            evidence = {
                "work-units.json": b"units",
                "verification.json": b"verification",
                "contract.json": b"contract",
                "issue.json": None,
            }
            for name, content in evidence.items():
                if content is not None:
                    (state.parent / name).write_bytes(content)
            record = lifecycle.WorktreeRecord(root, "task/225-recovery", "a" * 40)
            with mock.patch.object(lifecycle, "current_worktree", return_value=record), \
                 mock.patch.object(lifecycle, "worktree_for_task", return_value=record), \
                 mock.patch.object(lifecycle, "require_resolved_contract"), \
                 mock.patch.object(lifecycle, "assert_task_identity"), \
                 mock.patch.object(lifecycle.private_state, "prepare"), \
                 mock.patch.object(lifecycle.private_state, "post_merge_publication_recovery_receipt", return_value=root / "receipt"), \
                 mock.patch.object(lifecycle.private_state, "publication_recovery_receipt", return_value=root / "blocked-receipt"), \
                 mock.patch.object(lifecycle.private_state, "_validate_legacy_content"), \
                 mock.patch.object(lifecycle.private_state, "_validate_publication_recovery_topology"), \
                 mock.patch.object(lifecycle.private_state, "topology"), \
                 mock.patch.object(lifecycle.private_state, "mutation_lock", return_value=nullcontext()), \
                 mock.patch.object(lifecycle.private_state, "exclusive_write_bytes", side_effect=lambda path, content, **_: path.write_bytes(content)), \
                 self.assertRaisesRegex(lifecycle.LifecycleError, "cannot update post-merge Task State status"):
                lifecycle.recover_post_merge_publication_pending(
                    record, "225", original, evidence, b"{}"
                )
            self.assertEqual(state.read_bytes(), original)

    def test_direct_finalize_still_rejects_draft_pr_created(self) -> None:
        record = lifecycle.WorktreeRecord(Path("/task"), "task/225-recovery", "a" * 40)
        with mock.patch.object(lifecycle, "require_resolved_contract"), \
             mock.patch.object(lifecycle, "work_units_lock", return_value=nullcontext()), \
             mock.patch.object(lifecycle, "assert_task_identity"), \
             mock.patch.object(lifecycle, "state_path", return_value=Path("/state")), \
             mock.patch.object(lifecycle, "state_status", return_value="draft-pr-created"), \
             self.assertRaisesRegex(lifecycle.LifecycleError, "requires Task status integration-pending"):
            lifecycle.mark_task_merged_from_integration(record, "225")

    def test_batch_conflict_detects_dependency_and_shared_resources(self) -> None:
        summaries = [
            {
                "task": "TASK-1",
                "dependencies": [],
                "scope": ["src/a.cpp"],
                "coordinationSurfaces": ["flake.lock"],
                "externalResources": ["test-db"],
            },
            {
                "task": "TASK-2",
                "dependencies": ["TASK-1"],
                "scope": ["src/b.cpp"],
                "coordinationSurfaces": ["flake.lock"],
                "externalResources": ["test-db"],
            },
        ]
        conflicts = lifecycle.batch_conflicts(summaries)
        self.assertEqual(len(conflicts), 1)
        reasons = conflicts[0]["reasons"]
        self.assertIn("declared dependency", reasons)
        self.assertTrue(any("coordination surface" in reason for reason in reasons))
        self.assertTrue(any("external resource" in reason for reason in reasons))

    def test_next_work_unit_uses_max_canonical_suffix_without_filling_gaps(self) -> None:
        value = {
            "units": {
                "WU-TASK-1-01": {},
                "WU-TASK-1-03": {},
                "WU-TASK-1-003": {},
                "WU-TASK-10-99": {},
                "legacy-unit": {},
            }
        }
        self.assertEqual("WU-TASK-1-04", lifecycle.next_work_unit_id(value, "TASK-1"))
        self.assertEqual(3, lifecycle.canonical_work_unit_sequence("TASK-1", "WU-TASK-1-03"))
        self.assertIsNone(
            lifecycle.canonical_work_unit_sequence("TASK-1", "WU-TASK-1-003")
        )
        self.assertIsNone(
            lifecycle.canonical_work_unit_sequence("TASK-1", "WU-TASK-10-99")
        )

    def test_next_work_unit_rejects_oversized_generated_id(self) -> None:
        task = "T" * 124
        self.assertTrue(lifecycle.TASK_RE.fullmatch(task))
        with self.assertRaisesRegex(
            lifecycle.LifecycleError, "generated Work Unit ID is invalid"
        ):
            lifecycle.next_work_unit_id({"units": {}}, task)

    def test_issue_start_duplicate_never_removes_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            lifecycle, "require_main_worktree"
        ), mock.patch.dict(
            "sys.modules",
            {
                "task_contract": mock.Mock(
                    ContractError=lifecycle.LifecycleError,
                    fetch_issue=mock.Mock(return_value=("acme/widgets", {})),
                    hydrate_task_contract=mock.Mock(),
                )
            },
        ), mock.patch.object(
            lifecycle, "task_start", side_effect=lifecycle.LifecycleError("already exists")
        ), mock.patch.object(lifecycle, "run") as run:
            with self.assertRaisesRegex(lifecycle.LifecycleError, "already exists"):
                lifecycle.task_start_from_issue(Path(directory), "19", "existing")
            run.assert_not_called()

    def test_unresolved_contract_blocks_task_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state/task.md"
            state.parent.mkdir()
            state.write_text("## Purpose\n\nTBD\n", encoding="utf-8")
            record = lifecycle.WorktreeRecord(root, "task/19-example", "a" * 40)
            with self.assertRaisesRegex(lifecycle.LifecycleError, "unresolved"):
                lifecycle.require_resolved_contract(record, "19")

    def test_generated_lifecycle_files_match_sources(self) -> None:
        pairs = []
        for template in ("agent-base", "agent-cpp-cmake", "agent-nix", "agent-python", "agent-rust", "agent-typescript-node"):
            pairs.extend([
                (
                    ROOT / "components" / "agent-core" / ".automation" / "bin" / "task_lifecycle.py",
                    ROOT / "templates" / template / ".automation" / "bin" / "task_lifecycle.py",
                ),
                (
                    ROOT / "components" / "agent-core" / ".automation" / "bin" / "task_contract.py",
                    ROOT / "templates" / template / ".automation" / "bin" / "task_contract.py",
                ),
            ])
        pairs.extend([
            (
                ROOT / "components" / "agent-core" / ".automation" / "just" / "agent.just",
                ROOT / "templates" / "agent-base" / ".automation" / "just" / "agent.just",
            ),
            (
                ROOT / "components" / "agent-core" / ".automation" / "templates" / "task-state.md",
                ROOT / "templates" / "agent-base" / ".automation" / "templates" / "task-state.md",
            ),
            (
                ROOT / "components" / "agent-core" / "opencode.json",
                ROOT / "templates" / "agent-base" / "opencode.json",
            ),
        ])
        for source, generated in pairs:
            self.assertEqual(source.read_bytes(), generated.read_bytes(), source.as_posix())


if __name__ == "__main__":
    unittest.main()
