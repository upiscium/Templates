from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "components" / "agent-core" / ".automation" / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))
import agent_core as canonical_agent_core

SPEC = importlib.util.spec_from_file_location(
    "source_publication_recovery_bridge_test", ROOT / "tools" / "automation_recovery_bridge.py"
)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class PublicationRecoveryBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.target = Path("/tmp/publication-task").resolve()
        self.main = Path("/tmp/publication-main").resolve()
        self.branch = "task/131-source-publication-recovery"
        self.head = "a" * 40
        self.repository = "upiscium/Templates"
        self.record = mock.Mock(path=self.target, branch=self.branch, head=self.head)
        self.lifecycle = mock.Mock()
        self.lifecycle.repo_root.return_value = self.target
        self.lifecycle.current_worktree.return_value = mock.Mock(path=self.target)
        self.lifecycle.main_worktree.return_value = mock.Mock(path=self.main)
        self.lifecycle.worktree_for_task.return_value = self.record
        self.lifecycle.state_path.return_value = self.target / ".task-state" / "task.md"
        self.lifecycle.state_status.return_value = "publication-ready"
        self.agent = mock.Mock()
        self.agent.ensure_task_branch.return_value = self.branch
        self.agent.canonical_repository.return_value = self.repository
        self.publication = mock.Mock()
        self.publication.verification_evidence.return_value = {"head": self.head}
        self.publication.completed_reviews.return_value = ["- `review` — `reviewer` — completed"]
        self.contract = mock.MagicMock()
        self.contract._read_state_file.return_value = None
        self.modules = {
            "task_lifecycle": self.lifecycle,
            "agent_core": self.agent,
            "publication_metadata": self.publication,
            "task_contract": self.contract,
        }

    def snapshot_git(self, *args: str, **_: object) -> str:
        if args[0] == "rev-parse" and args[1] == "--verify":
            return self.head
        return ""

    def snapshot(self) -> dict:
        with mock.patch.object(bridge, "_target_git", side_effect=self.snapshot_git), \
             mock.patch.object(bridge, "_remote_branch_head", return_value=self.head), \
             mock.patch.object(bridge, "_state_bytes", side_effect=lambda _root, name, *_: name.encode()):
            return bridge._publication_snapshot(self.modules, self.target, "131")

    def test_parser_accepts_full_publication_revision_and_task(self) -> None:
        args = bridge.parser().parse_args(
            ["publication-recover", "/tmp/task", "131", "b" * 40]
        )
        self.assertEqual(
            (args.command, args.task, args.expected_implementation_revision),
            ("publication-recover", "131", "b" * 40),
        )
        with self.assertRaises(bridge.BridgeError):
            bridge.parser().parse_args(["publication-recover", "/tmp/task", "013", "b" * 40])

    def test_snapshot_preserves_identity_and_evidence_for_normal_recovery(self) -> None:
        before = self.snapshot()
        self.assertEqual(before["head"], self.head)
        self.assertEqual(before["branch"], self.branch)
        self.assertEqual(before["record"], self.record)
        self.assertEqual(before["work_units"], b"work-units.json")
        self.assertEqual(before["verification"], b"verification.json")
        self.assertEqual(before["status"], "publication-ready")
        self.lifecycle.require_resolved_contract.assert_called_once_with(self.record, "131")

    def test_snapshot_accepts_draft_pr_created_and_preserves_initial_status(self) -> None:
        self.lifecycle.state_status.return_value = "draft-pr-created"
        before = self.snapshot()
        self.assertEqual(before["status"], "draft-pr-created")

    def test_snapshot_accepts_blocked_only_as_a_recovery_candidate(self) -> None:
        self.lifecycle.state_status.return_value = "blocked"
        before = self.snapshot()
        self.assertEqual(before["status"], "blocked")

    def test_snapshot_rejects_other_lifecycle_states(self) -> None:
        self.lifecycle.state_status.return_value = "implementing"
        with self.assertRaisesRegex(
            bridge.BridgeError,
            "requires blocked, publication-ready, or draft-pr-created",
        ):
            self.snapshot()

    def test_no_existing_pr_runs_prepare_create_and_only_guarded_state_transition(self) -> None:
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"units", "verification": b"verification",
            "contract": b"contract", "state": b"- Status: publication-ready\n",
            "status": "publication-ready",
        }
        after = {**before, "state": b"- Status: draft-pr-created\n", "status": "draft-pr-created"}
        pr = {"number": 17, "headRefName": self.branch}
        original_verify = self.agent.verify
        self.agent.pr_for_branch.side_effect = [None, pr]
        self.agent._validated_local_metadata.return_value = ("131: summary", Path("/tmp/body"), "body")
        self.agent.default_branch.return_value = "main"
        self.agent._validate_live_pr.return_value = None
        self.agent.pr_prepare.return_value = None
        self.agent.pr_create.return_value = pr
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_publication_snapshot_for_post", return_value=after), \
             mock.patch.object(bridge, "_target_git", return_value=""):
            result = bridge._publication_recover(self.modules, self.target, "131")
        self.assertEqual(result["status"], "DRAFT_PR_CREATED")
        self.agent.pr_prepare.assert_called_once_with(self.target, "131")
        self.agent.pr_create.assert_called_once_with(self.target, "131")
        self.agent.pr_edit.assert_not_called()
        self.assertIs(self.agent.verify, original_verify)

    def test_draft_pr_created_without_existing_pr_fails_closed_before_mutation(self) -> None:
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"units", "verification": b"verification",
            "contract": b"contract", "state": b"- Status: draft-pr-created\n",
            "status": "draft-pr-created",
        }
        self.agent.pr_for_branch.return_value = None
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_target_git", return_value=""), \
             self.assertRaisesRegex(
                 bridge.BridgeError,
                 "draft-pr-created recovery requires the existing Draft PR",
             ):
            bridge._publication_recover(self.modules, self.target, "131")
        self.agent.pr_prepare.assert_not_called()
        self.agent.pr_create.assert_not_called()
        self.agent.pr_edit.assert_not_called()

    def test_draft_pr_created_replacement_during_prepare_fails_before_edit(self) -> None:
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"units", "verification": b"verification",
            "contract": b"contract", "state": b"- Status: draft-pr-created\n",
            "status": "draft-pr-created",
        }
        captured = {"number": 29}
        replacement = {"number": 30}
        self.agent.pr_for_branch.side_effect = [captured, replacement]
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_target_git", return_value=""), \
             self.assertRaisesRegex(
                 bridge.BridgeError,
                 "identity changed before canonical repair",
             ):
            bridge._publication_recover(self.modules, self.target, "131")
        self.agent.pr_prepare.assert_called_once_with(self.target, "131")
        self.agent.pr_create.assert_not_called()
        self.agent.pr_edit.assert_not_called()

    def test_blocked_without_existing_pr_fails_before_state_or_github_mutation(self) -> None:
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"units", "verification": b"verification",
            "contract": b"contract", "issue": b"issue", "state": b"- Status: blocked\n",
            "status": "blocked",
        }
        self.agent.pr_for_branch.return_value = None
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             self.assertRaisesRegex(bridge.BridgeError, "blocked publication recovery requires the existing Draft PR"):
            bridge._publication_recover(self.modules, self.target, "131")
        self.lifecycle.recover_blocked_publication_ready.assert_not_called()
        self.agent.pr_prepare.assert_not_called()
        self.agent.pr_create.assert_not_called()
        self.agent.pr_edit.assert_not_called()

    def test_blocked_akv_shaped_stale_draft_recovers_same_pr_and_preserves_subject(self) -> None:
        current_head = "8313bcfcfdee4a3985f8b5dd48861b2b2bcb69a4"
        branch = "task/13-hybrid-level1-reranking"
        repository = "upiscium/AgentKnowledgeVault"
        original_state = (
            b"prefix\n- Base revision: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
            b"- Status: blocked\n- Blockers: publication-only failure\nsuffix\n"
        )
        ready_state = original_state.replace(b"- Status: blocked", b"- Status: publication-ready")
        final_state = original_state.replace(b"- Status: blocked", b"- Status: draft-pr-created")
        before = {
            "head": current_head, "branch": branch, "repository": repository,
            "record": self.record, "work_units": b'history including WU-13-28',
            "verification": b"current verification", "contract": b"contract",
            "issue": b"issue", "state": original_state, "status": "blocked",
        }
        ready = {**before, "state": ready_state, "status": "publication-ready"}
        after = {**before, "state": final_state, "status": "draft-pr-created"}
        stale = {
            "number": 29, "headRefName": branch, "baseRefName": "main",
            "headRefOid": current_head, "isDraft": True,
            "isCrossRepository": False, "state": "OPEN",
            "body": "Validation at 97dbf03607d8eaabfc76104be2722d89aa6ddf2d",
        }
        repaired = {**stale, "body": f"Validation at {current_head}"}
        self.agent.pr_for_branch.side_effect = [stale, stale, stale, repaired, repaired]
        self.agent.default_branch.return_value = "main"
        self.publication.canonical_metadata.return_value = ("13: canonical", repaired["body"])
        self.agent._validated_local_metadata.return_value = (
            "13: canonical", Path("/tmp/body"), repaired["body"]
        )
        with mock.patch.object(bridge, "_publication_snapshot", side_effect=[before, before, ready, ready]), \
             mock.patch.object(bridge, "_publication_snapshot_for_post", return_value=after), \
             mock.patch.object(bridge, "_target_git", return_value=""):
            result = bridge._publication_recover(self.modules, self.target, "13")
        self.assertEqual(result["pullRequest"]["number"], 29)
        self.lifecycle.recover_blocked_publication_ready.assert_called_once_with(
            self.record,
            "13",
            original_state,
            {
                "work-units.json": before["work_units"],
                "verification.json": before["verification"],
                "contract.json": before["contract"],
                "issue.json": before["issue"],
            },
        )
        self.agent.pr_edit.assert_called_once_with(self.target, "13", expected_pr_number=29)
        self.agent.pr_create.assert_not_called()
        self.assertEqual(after["work_units"], before["work_units"])
        self.assertIn(b"WU-13-28", after["work_units"])

    def test_blocked_invalid_or_moved_pr_fails_before_state_mutation(self) -> None:
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"units", "verification": b"verification",
            "contract": b"contract", "issue": b"issue",
            "state": b"- Base revision: " + b"b" * 40 + b"\n- Status: blocked\n",
            "status": "blocked",
        }
        exact = {
            "number": 29, "headRefName": self.branch, "baseRefName": "main",
            "headRefOid": self.head, "isDraft": True,
            "isCrossRepository": False, "state": "OPEN",
        }
        cases = [
            ("number", {**exact, "number": 0}),
            ("bool number", {**exact, "number": True}),
            ("branch", {**exact, "headRefName": "task/other"}),
            ("base", {**exact, "baseRefName": "other"}),
            ("head", {**exact, "headRefOid": "c" * 40}),
            ("ready", {**exact, "isDraft": False}),
            ("closed", {**exact, "state": "CLOSED"}),
            ("cross repository", {**exact, "isCrossRepository": True}),
        ]
        self.agent.default_branch.return_value = "main"
        for name, candidate in cases:
            with self.subTest(name=name):
                self.agent.reset_mock()
                self.agent.default_branch.return_value = "main"
                self.agent.pr_for_branch.return_value = candidate
                with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
                     self.assertRaises(bridge.BridgeError):
                    bridge._publication_recover(self.modules, self.target, "131")
                self.lifecycle.recover_blocked_publication_ready.assert_not_called()
                self.agent.pr_edit.assert_not_called()

    def test_blocked_pr_replacement_before_state_mutation_fails_closed(self) -> None:
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"units", "verification": b"verification",
            "contract": b"contract", "issue": b"issue",
            "state": b"- Base revision: " + b"b" * 40 + b"\n- Status: blocked\n",
            "status": "blocked",
        }
        exact = {
            "number": 29, "headRefName": self.branch, "baseRefName": "main",
            "headRefOid": self.head, "isDraft": True,
            "isCrossRepository": False, "state": "OPEN",
        }
        self.publication.canonical_metadata.return_value = ("title", "body")
        for field, changed in (
            ("number", 30), ("headRefName", "task/other"),
            ("baseRefName", "other"), ("headRefOid", "c" * 40),
            ("isDraft", False), ("state", "CLOSED"),
        ):
            with self.subTest(field=field):
                self.agent.reset_mock()
                self.agent.default_branch.return_value = "main"
                self.agent.pr_for_branch.side_effect = [exact, {**exact, field: changed}]
                with mock.patch.object(bridge, "_publication_snapshot", side_effect=[before, before]), \
                     mock.patch.object(bridge, "_target_git", return_value=""), \
                     self.assertRaises(bridge.BridgeError):
                    bridge._publication_recover(self.modules, self.target, "131")
                self.lifecycle.recover_blocked_publication_ready.assert_not_called()
                self.agent.pr_edit.assert_not_called()

    def test_blocked_default_branch_move_before_state_mutation_fails_closed(self) -> None:
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"units", "verification": b"verification",
            "contract": b"contract", "issue": b"issue",
            "state": b"- Base revision: " + b"b" * 40 + b"\n- Status: blocked\n",
            "status": "blocked",
        }
        exact = {
            "number": 29, "headRefName": self.branch, "baseRefName": "main",
            "headRefOid": self.head, "isDraft": True,
            "isCrossRepository": False, "state": "OPEN",
        }
        self.agent.default_branch.side_effect = ["main", "moved"]
        self.agent.pr_for_branch.return_value = exact
        self.publication.canonical_metadata.return_value = ("title", "body")
        with mock.patch.object(bridge, "_publication_snapshot", side_effect=[before, before]), \
             mock.patch.object(bridge, "_target_git", return_value=""), \
             self.assertRaisesRegex(bridge.BridgeError, "default branch changed"):
            bridge._publication_recover(self.modules, self.target, "131")
        self.lifecycle.recover_blocked_publication_ready.assert_not_called()

    def test_blocked_authority_change_before_state_mutation_fails_closed(self) -> None:
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"units", "verification": b"verification",
            "contract": b"contract", "issue": b"issue",
            "state": b"- Base revision: " + b"b" * 40 + b"\n- Status: blocked\n",
            "status": "blocked",
        }
        exact = {
            "number": 29, "headRefName": self.branch, "baseRefName": "main",
            "headRefOid": self.head, "isDraft": True,
            "isCrossRepository": False, "state": "OPEN",
        }
        self.agent.default_branch.return_value = "main"
        self.agent.pr_for_branch.return_value = exact
        self.publication.canonical_metadata.return_value = ("title", "body")
        for field, changed in (
            ("status", "planning"), ("head", "c" * 40),
            ("branch", "task/other"), ("repository", "other/repository"),
            ("work_units", b"changed"), ("verification", b"changed"),
            ("contract", b"changed"), ("issue", b"changed"),
        ):
            with self.subTest(field=field):
                self.agent.reset_mock()
                self.agent.default_branch.return_value = "main"
                self.agent.pr_for_branch.return_value = exact
                moved = {**before, field: changed}
                with mock.patch.object(bridge, "_publication_snapshot", side_effect=[before, moved]), \
                     mock.patch.object(bridge, "_target_git", return_value=""), \
                     self.assertRaisesRegex(bridge.BridgeError, "authority changed"):
                    bridge._publication_recover(self.modules, self.target, "131")
                self.lifecycle.recover_blocked_publication_ready.assert_not_called()

    def test_stale_verification_and_effective_reviewer_fail_before_create(self) -> None:
        self.lifecycle.state_status.return_value = "blocked"
        for failure in (
            "project verification evidence is stale",
            "required reviewer Work Unit is not completed",
            "required security-reviewer Work Unit is not completed",
        ):
            with self.subTest(failure=failure):
                self.publication.verification_evidence.side_effect = (
                    bridge.BridgeError(failure) if "verification" in failure else None
                )
                self.publication.completed_reviews.side_effect = (
                    RuntimeError(failure) if "Work Unit" in failure else None
                )
                with mock.patch.object(bridge, "_target_git", side_effect=self.snapshot_git), \
                     mock.patch.object(bridge, "_remote_branch_head", return_value=self.head), \
                     mock.patch.object(bridge, "_state_bytes", side_effect=lambda _root, name, *_: name.encode()), \
                     self.assertRaises(bridge.BridgeError):
                    bridge._publication_snapshot(self.modules, self.target, "131")
                self.agent.pr_create.assert_not_called()
                self.publication.verification_evidence.reset_mock(side_effect=True)
                self.publication.completed_reviews.reset_mock(side_effect=True)

    def test_wrong_task_worktree_default_and_dirty_target_are_rejected(self) -> None:
        cases = {
            "wrong task": lambda: self.lifecycle.worktree_for_task.configure_mock(
                return_value=mock.Mock(path=Path("/tmp/other"))
            ),
            "wrong worktree": lambda: self.lifecycle.current_worktree.configure_mock(
                return_value=mock.Mock(path=Path("/tmp/other"))
            ),
            "default": lambda: self.lifecycle.main_worktree.configure_mock(
                return_value=mock.Mock(path=self.target)
            ),
            "dirty target": lambda: None,
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                self.setUp()
                self.lifecycle.state_status.return_value = "blocked"
                mutate()
                git = (lambda *args, **kwargs: "tracked\n") if name == "dirty target" else self.snapshot_git
                with mock.patch.object(bridge, "_target_git", side_effect=git), \
                     mock.patch.object(bridge, "_remote_branch_head", return_value=self.head), \
                      mock.patch.object(bridge, "_state_bytes", side_effect=lambda _root, n, *_: n.encode()), \
                     self.assertRaises(bridge.BridgeError):
                    bridge._publication_snapshot(self.modules, self.target, "131")

    def test_blocked_local_or_remote_head_mismatch_is_rejected(self) -> None:
        self.lifecycle.state_status.return_value = "blocked"

        def local_mismatch(*args: str, **_: object) -> str:
            if args[:2] == ("rev-parse", "--verify"):
                return self.head if args[2] == "HEAD^{commit}" else "c" * 40
            return ""

        with mock.patch.object(bridge, "_target_git", side_effect=local_mismatch), \
             mock.patch.object(bridge, "_optional_state_bytes", return_value=None), \
             self.assertRaisesRegex(bridge.BridgeError, "HEAD.*differ"):
            bridge._publication_snapshot(self.modules, self.target, "131")
        with mock.patch.object(bridge, "_target_git", side_effect=self.snapshot_git), \
             mock.patch.object(bridge, "_remote_branch_head", return_value="c" * 40), \
             mock.patch.object(bridge, "_optional_state_bytes", return_value=None), \
             self.assertRaisesRegex(bridge.BridgeError, "remote Task branch"):
            bridge._publication_snapshot(self.modules, self.target, "131")

    def test_blocked_unresolved_contract_is_rejected_before_evidence_or_pr(self) -> None:
        self.lifecycle.state_status.return_value = "blocked"
        self.lifecycle.require_resolved_contract.side_effect = RuntimeError("contract mismatch")
        with mock.patch.object(bridge, "_target_git", side_effect=self.snapshot_git), \
             self.assertRaisesRegex(bridge.BridgeError, "contract mismatch"):
            bridge._publication_snapshot(self.modules, self.target, "131")
        self.publication.verification_evidence.assert_not_called()
        self.agent.pr_for_branch.assert_not_called()

    def test_blocked_missing_current_reviewer_metadata_rejects_before_state_mutation(self) -> None:
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"units", "verification": b"verification",
            "contract": b"contract", "issue": b"issue",
            "state": b"- Base revision: " + b"b" * 40 + b"\n- Status: blocked\n",
            "status": "blocked",
        }
        exact = {
            "number": 29, "headRefName": self.branch, "baseRefName": "main",
            "headRefOid": self.head, "isDraft": True,
            "isCrossRepository": False, "state": "OPEN",
        }
        self.agent.default_branch.return_value = "main"
        self.agent.pr_for_branch.return_value = exact
        self.publication.canonical_metadata.side_effect = RuntimeError(
            "publication requires a completed reviewer Work Unit"
        )
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_target_git", return_value=""), \
             self.assertRaisesRegex(bridge.BridgeError, "completed reviewer"):
            bridge._publication_recover(self.modules, self.target, "131")
        self.lifecycle.recover_blocked_publication_ready.assert_not_called()

    def test_unsafe_config_is_rejected(self) -> None:
        with mock.patch.object(bridge, "_pinned_run", return_value=mock.Mock(stdout="core.sshCommand\0")):
            with self.assertRaisesRegex(bridge.BridgeError, "unsafe local Git configuration"):
                bridge._validate_target_git_configuration(self.target)

    def test_unsafe_worktree_config_is_rejected(self) -> None:
        responses = [
            mock.Mock(stdout="", returncode=0),
            mock.Mock(stdout="true\n", returncode=0),
            mock.Mock(stdout="core.hooksPath\0", returncode=0),
        ]
        with mock.patch.object(bridge, "_pinned_run", side_effect=responses):
            with self.assertRaisesRegex(bridge.BridgeError, "unsafe .*Git configuration"):
                bridge._validate_target_git_configuration(self.target)

    def test_akv_stale_metadata_fixture_renders_current_evidence_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state"
            state.mkdir()
            old_head = "97dbf03607d8eaabfc76104be2722d89aa6ddf2d"
            current_head = "8313bcfcfdee4a3985f8b5dd48861b2b2bcb69a4"
            (state / "task.md").write_text(
                """# 13

## Identity

- Task ID: 13
- Branch: task/13-hybrid-level1-reranking
- Worktree: /fixture
- Base branch: main
- Base revision: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

## Purpose

Repair AgentKnowledgeVault PR #29 publication metadata.

## Acceptance criteria

- [x] Preserve the exact Draft PR identity.

## Current state

- Status: blocked
- Blockers: none
- Unverified: none

## Follow-up Task candidates

None yet.
""",
                encoding="utf-8",
            )
            (state / "verification.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "task_id": "13",
                    "head": current_head,
                    "clean_tracked_worktree": True,
                    "worktree_stable": True,
                    "project_check": {
                        "command": ["just", "project::check"],
                        "returncode": 0,
                        "executed_at": "2026-09-08T00:00:00+00:00",
                    },
                }),
                encoding="utf-8",
            )
            evidence = {
                "schema_version": 1,
                "task_id": "13",
                "units": {
                    "WU-13-28": {"requested_role": "reviewer", "state": "blocked", "transitions": []},
                    "WU-13-29": {"requested_role": "reviewer", "state": "completed", "transitions": [{"evidence_sha256": "a" * 64}]},
                    "WU-13-30": {"requested_role": "security-reviewer", "state": "completed", "transitions": [{"evidence_sha256": "b" * 64}]},
                },
            }
            path = state / "work-units.json"
            path.write_bytes((json.dumps(evidence, separators=(",", ":")) + "\n").encode())
            before = path.read_bytes()
            initial_live_pr = {
                "number": 29,
                "headRefOid": current_head,
                "body": f"## Validation\n\n- `just project::check`: PASS at {old_head}\n",
            }
            title, body = canonical_agent_core.publication.canonical_metadata(
                root, "13", head=current_head, changed_paths=["product.py"]
            )
            self.assertEqual(initial_live_pr["number"], 29)
            self.assertEqual(initial_live_pr["headRefOid"], current_head)
            self.assertIn(old_head, initial_live_pr["body"])
            self.assertIn(current_head, body)
            self.assertNotIn(old_head, body)
            self.assertIn("`WU-13-29` — `reviewer` — completed", body)
            self.assertIn("`WU-13-30` — `security-reviewer` — completed", body)
            self.assertNotIn("WU-13-28", body)
            self.assertTrue(title.startswith("13:"))
            self.assertEqual(path.read_bytes(), before)
            self.assertIn(b'"WU-13-28"', before)

    def test_source_revision_dirty_source_and_bootstrap_mismatch_block_main_before_modules(self) -> None:
        revision = "b" * 40
        for failure in ("wrong source revision", "dirty source", "bootstrap mismatch"):
            with self.subTest(failure=failure):
                clean = mock.Mock(return_value=revision)
                if failure == "wrong source revision":
                    clean.side_effect = bridge.BridgeError("Templates HEAD changed during bootstrap")
                verify = mock.Mock()
                if failure == "bootstrap mismatch":
                    verify.side_effect = bridge.BridgeError("live recovery bootstrap does not match its HEAD blob")
                with mock.patch.object(bridge, "_clean_root", clean), \
                     mock.patch.object(bridge, "_verify_bootstrap", verify), \
                     mock.patch.object(bridge, "_verified_modules") as modules, \
                     mock.patch.object(bridge, "maintenance_environment", return_value=nullcontext()), \
                     mock.patch.object(sys, "argv", ["bridge", "publication-recover", str(self.target), "131", revision]), \
                     mock.patch("sys.stderr", new_callable=io.StringIO):
                    if failure == "dirty source":
                        clean.side_effect = bridge.BridgeError("Templates source worktree must be clean")
                    self.assertEqual(bridge.main(), 2)
                modules.assert_not_called()

    def test_existing_stale_draft_routes_to_canonical_edit_without_duplicate_create(self) -> None:
        branch = "task/13-hybrid-level1-reranking"
        repository = "upiscium/AgentKnowledgeVault"
        old_head = "97dbf03607d8eaabfc76104be2722d89aa6ddf2d"
        current_head = "8313bcfcfdee4a3985f8b5dd48861b2b2bcb69a4"
        stale = {"number": 29, "headRefName": branch, "headRefOid": current_head,
                 "body": f"Validation PASS at {old_head}"}
        repaired = {"number": 29, "headRefName": branch, "headRefOid": current_head,
                    "body": f"Validation PASS at {current_head}"}
        self.agent.pr_for_branch.side_effect = [stale, repaired, repaired]
        self.agent._validated_local_metadata.return_value = (
            "13: repair stale publication metadata", Path("/tmp/body"), repaired["body"]
        )
        self.agent.default_branch.return_value = "main"
        before = {"head": current_head, "branch": branch, "repository": repository,
                  "record": self.record, "work_units": b"u", "verification": b"v", "contract": b"c",
                  "state": b"- Status: publication-ready\n", "status": "publication-ready"}
        after = {**before, "state": b"- Status: draft-pr-created\n", "status": "draft-pr-created"}
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_publication_snapshot_for_post", return_value=after), \
             mock.patch.object(bridge, "_target_git", return_value=""):
            result = bridge._publication_recover(self.modules, self.target, "13")
        self.assertEqual(result["pullRequest"], repaired)
        self.agent.pr_edit.assert_called_once_with(
            self.target, "13", expected_pr_number=29
        )
        self.agent.pr_create.assert_not_called()
        self.assertEqual(
            self.agent.pr_for_branch.call_args_list,
            [mock.call(self.target, branch, repository)] * 3,
        )

    def test_draft_pr_created_stale_exact_draft_edits_with_byte_identical_state(self) -> None:
        stale = {"number": 29, "headRefName": self.branch, "body": "stale"}
        repaired = {"number": 29, "headRefName": self.branch, "body": "canonical"}
        self.agent.pr_for_branch.side_effect = [stale, stale, repaired, repaired]
        self.agent._validated_local_metadata.return_value = (
            "title", Path("/tmp/body"), repaired["body"]
        )
        self.agent.default_branch.return_value = "main"
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"u", "verification": b"v",
            "contract": b"c", "state": b"prefix\n- Status: draft-pr-created\nsuffix\n",
            "status": "draft-pr-created",
        }
        after = dict(before)
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_publication_snapshot_for_post", return_value=after), \
             mock.patch.object(bridge, "_target_git", return_value=""):
            result = bridge._publication_recover(self.modules, self.target, "131")
        self.assertEqual(result["pullRequest"], repaired)
        self.agent.pr_prepare.assert_called_once_with(self.target, "131")
        self.agent.pr_edit.assert_called_once_with(
            self.target, "131", expected_pr_number=29
        )
        self.agent.pr_create.assert_not_called()
        self.assertEqual(after["state"], before["state"])

    def test_draft_pr_created_canonical_draft_converges_with_byte_identical_state(self) -> None:
        canonical = {"number": 23, "headRefName": self.branch, "body": "body"}
        self.agent.pr_for_branch.return_value = canonical
        self.agent._validated_local_metadata.return_value = ("title", Path("/tmp/body"), "body")
        self.agent.default_branch.return_value = "main"
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"u", "verification": b"v",
            "contract": b"c", "state": b"- Status: draft-pr-created\n",
            "status": "draft-pr-created",
        }
        after = dict(before)
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_publication_snapshot_for_post", return_value=after), \
             mock.patch.object(bridge, "_target_git", return_value=""):
            result = bridge._publication_recover(self.modules, self.target, "131")
        self.assertEqual(result["pullRequest"], canonical)
        self.agent.pr_edit.assert_called_once_with(
            self.target, "131", expected_pr_number=23
        )
        self.agent.pr_create.assert_not_called()
        self.assertEqual(after["state"], before["state"])

    def test_already_canonical_existing_draft_converges_through_idempotent_edit(self) -> None:
        canonical = {"number": 23, "headRefName": self.branch, "body": "body"}
        self.agent.pr_for_branch.return_value = canonical
        self.agent._validated_local_metadata.return_value = ("title", Path("/tmp/body"), "body")
        self.agent.default_branch.return_value = "main"
        before = {"head": self.head, "branch": self.branch, "repository": self.repository,
                  "record": self.record, "work_units": b"u", "verification": b"v", "contract": b"c",
                  "state": b"- Status: publication-ready\n", "status": "publication-ready"}
        after = {**before, "state": b"- Status: draft-pr-created\n", "status": "draft-pr-created"}
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_publication_snapshot_for_post", return_value=after), \
             mock.patch.object(bridge, "_target_git", return_value=""):
            result = bridge._publication_recover(self.modules, self.target, "131")
        self.assertEqual(result["pullRequest"]["number"], 23)
        self.agent.pr_edit.assert_called_once_with(
            self.target, "131", expected_pr_number=23
        )
        self.agent.pr_create.assert_not_called()

    def test_canonical_operations_use_captured_current_head_verification(self) -> None:
        before = {"head": self.head, "branch": self.branch, "repository": self.repository,
                  "record": self.record, "work_units": b"u", "verification": b"v", "contract": b"c",
                  "state": b"- Status: publication-ready\n", "status": "publication-ready"}
        after = {**before, "state": b"- Status: draft-pr-created\n", "status": "draft-pr-created"}
        pr = {"number": 29}
        self.agent.pr_prepare.side_effect = lambda root, task: self.agent.verify(root, task)
        self.agent.pr_create.side_effect = lambda root, task: self.agent.verify(root, task) or pr
        self.agent.pr_for_branch.side_effect = [None, pr]
        self.agent._validated_local_metadata.return_value = ("title", Path("/tmp/body"), "body")
        self.agent.default_branch.return_value = "main"

        def git(*args: str, **_: object) -> str:
            return self.head if args[0] == "rev-parse" else ""

        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_publication_snapshot_for_post", return_value=after), \
             mock.patch.object(bridge, "_target_git", side_effect=git), \
             mock.patch.object(bridge, "_state_bytes", return_value=b"v"):
            bridge._publication_recover(self.modules, self.target, "131")
        self.assertEqual(self.publication.verification_evidence.call_args_list, [
            mock.call(self.target, "131", self.head),
            mock.call(self.target, "131", self.head),
        ])

    def test_draft_pr_created_postcondition_rejects_any_task_state_change(self) -> None:
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "work_units": b"u", "verification": b"v", "contract": b"c",
            "state": b"- Status: draft-pr-created\n- Blockers: none\n",
            "status": "draft-pr-created",
        }
        after = {
            **before,
            "state": b"- Status: draft-pr-created\n- Blockers: changed\n",
        }
        with mock.patch.object(bridge, "_publication_snapshot_for_post", return_value=after), \
             mock.patch.object(bridge, "_target_git", return_value=""), \
             self.assertRaisesRegex(
                 bridge.BridgeError,
                 "changed Task State from draft-pr-created",
             ):
            bridge._assert_publication_postconditions(
                self.modules, self.target, "131", before
            )

    def test_existing_draft_with_invalid_or_ambiguous_number_fails_before_edit(self) -> None:
        before = {"head": self.head, "branch": self.branch, "repository": self.repository,
                  "record": self.record, "work_units": b"u", "verification": b"v", "contract": b"c",
                  "state": b"- Status: publication-ready\n", "status": "publication-ready"}
        for existing in ({"number": True}, {"number": False}, {"number": "29"}, {"number": None}, []):
            with self.subTest(existing=existing):
                self.agent.reset_mock()
                self.agent.pr_for_branch.return_value = existing
                with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
                     mock.patch.object(bridge, "_target_git", return_value=""), \
                     self.assertRaisesRegex(bridge.BridgeError, "invalid or ambiguous number"):
                    bridge._publication_recover(self.modules, self.target, "131")
                self.agent.pr_edit.assert_not_called()
                self.agent.pr_create.assert_not_called()

    def test_canonical_pr_create_reconciles_exact_existing_draft_without_gh_create(self) -> None:
        existing = {
            "number": 23, "headRefName": self.branch, "baseRefName": "main",
            "headRefOid": self.head, "title": "title", "body": "body", "isDraft": True,
            "isCrossRepository": False, "state": "OPEN",
        }
        context = {"record": self.record, "status": "publication-ready", "repository": self.repository}
        with mock.patch.object(canonical_agent_core, "verify"), \
             mock.patch.object(canonical_agent_core, "_publication_context", return_value=(self.branch, context, self.head)), \
             mock.patch.object(canonical_agent_core, "default_branch", return_value="main"), \
             mock.patch.object(canonical_agent_core, "_validated_local_metadata", return_value=("title", Path("/tmp/body"), "body")), \
             mock.patch.object(canonical_agent_core, "pr_for_branch", side_effect=[existing, existing]), \
             mock.patch.object(canonical_agent_core, "_validate_live_pr") as validate, \
             mock.patch.object(canonical_agent_core, "canonical_repository", return_value=self.repository), \
             mock.patch.object(canonical_agent_core, "gh") as gh, \
             mock.patch.object(canonical_agent_core.lifecycle, "mark_task_publication_state") as transition:
            canonical_agent_core.pr_create(self.target, "131")
        gh.assert_not_called()
        transition.assert_called_once_with(self.record, "131", "publication-ready", "draft-pr-created")
        self.assertEqual(validate.call_count, 2)

    def test_invalid_edit_target_identity_is_rejected_before_write(self) -> None:
        good = {"number": 29, "headRefName": self.branch, "baseRefName": "main",
                "headRefOid": self.head, "isDraft": True,
                "isCrossRepository": False, "state": "OPEN"}
        for key, value in (("headRefName", "other"), ("baseRefName", "other"),
                           ("headRefOid", "wrong"), ("state", "CLOSED"),
                           ("isDraft", False), ("isCrossRepository", True)):
            with self.subTest(key=key):
                candidate = {**good, key: value}
                with self.assertRaises(canonical_agent_core.AutomationError):
                    canonical_agent_core._validate_edit_target(
                        candidate, branch=self.branch, base="main", head=self.head
                    )

        for number in (True, False):
            with self.subTest(number=number):
                candidate = {**good, "number": number}
                with self.assertRaises(canonical_agent_core.AutomationError):
                    canonical_agent_core._validate_edit_target(
                        candidate, branch=self.branch, base="main", head=self.head
                    )

    def test_pr_replacement_is_rejected_before_canonical_edit_mutation(self) -> None:
        captured = {"number": 29}
        replacement = {
            "number": 30, "headRefName": self.branch, "baseRefName": "main",
            "headRefOid": self.head, "isDraft": True,
            "isCrossRepository": False, "state": "OPEN",
        }
        context = {
            "record": self.record, "status": "publication-ready",
            "repository": self.repository,
        }
        before = {"head": self.head, "branch": self.branch, "repository": self.repository,
                  "record": self.record, "work_units": b"u", "verification": b"v", "contract": b"c",
                  "state": b"- Status: publication-ready\n", "status": "publication-ready"}

        def git(*args: str, **_: object) -> str:
            return self.head if args[0] == "rev-parse" else ""

        replacement_modules = {**self.modules, "agent_core": canonical_agent_core}
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_target_git", side_effect=git), \
             mock.patch.object(bridge, "_state_bytes", return_value=b"v"), \
             mock.patch.object(canonical_agent_core, "pr_prepare"), \
             mock.patch.object(canonical_agent_core, "_publication_context", return_value=(self.branch, context, self.head)), \
             mock.patch.object(canonical_agent_core, "pr_for_branch", side_effect=[captured, replacement]), \
             mock.patch.object(canonical_agent_core, "gh") as gh, \
             mock.patch.object(canonical_agent_core.lifecycle, "mark_task_publication_state") as transition, \
             self.assertRaisesRegex(canonical_agent_core.AutomationError, "identity changed before mutation"):
            bridge._publication_recover(replacement_modules, self.target, "131")
        gh.assert_not_called()
        transition.assert_not_called()

    def test_edit_success_then_lifecycle_interruption_retry_converges_on_same_pr(self) -> None:
        before = {"head": self.head, "branch": self.branch, "repository": self.repository,
                  "record": self.record, "work_units": b"u", "verification": b"v", "contract": b"c",
                  "state": b"- Status: publication-ready\n", "status": "publication-ready"}
        after = {**before, "state": b"- Status: draft-pr-created\n", "status": "draft-pr-created"}
        existing = {"number": 29}
        edits = 0
        def edit(_target: Path, _task: str, *, expected_pr_number: int) -> None:
            self.assertEqual(expected_pr_number, 29)
            nonlocal edits
            edits += 1
            if edits == 1:
                raise RuntimeError("interrupted after GitHub edit")
        self.agent.pr_edit.side_effect = edit
        self.agent.pr_for_branch.return_value = existing
        self.agent._validated_local_metadata.return_value = ("title", Path("/tmp/body"), "body")
        self.agent.default_branch.return_value = "main"
        self.agent._validate_live_pr.return_value = None
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_publication_snapshot_for_post", return_value=after), \
             mock.patch.object(bridge, "_target_git", return_value=""):
            with self.assertRaises(RuntimeError):
                bridge._publication_recover(self.modules, self.target, "131")
            result = bridge._publication_recover(self.modules, self.target, "131")
        self.assertEqual(result["pullRequest"], existing)
        self.assertEqual(edits, 2)
        self.agent.pr_create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
