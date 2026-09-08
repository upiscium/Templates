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
        self.modules = {
            "task_lifecycle": self.lifecycle,
            "agent_core": self.agent,
            "publication_metadata": self.publication,
            "task_contract": mock.Mock(),
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
        self.lifecycle.require_resolved_contract.assert_called_once_with(self.record, "131")

    def test_recover_runs_prepare_create_and_only_guarded_state_transition(self) -> None:
        before = {
            "head": self.head, "branch": self.branch, "repository": self.repository,
            "record": self.record, "work_units": b"units", "verification": b"verification",
            "contract": b"contract", "state": b"- Status: publication-ready\n",
        }
        after = {**before, "state": b"- Status: draft-pr-created\n"}
        pr = {"number": 17, "headRefName": self.branch}
        original_verify = self.agent.verify
        self.agent.pr_for_branch.return_value = pr
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
        self.assertIs(self.agent.verify, original_verify)

    def test_stale_verification_and_effective_reviewer_fail_before_create(self) -> None:
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
                mutate()
                git = (lambda *args, **kwargs: "tracked\n") if name == "dirty target" else self.snapshot_git
                with mock.patch.object(bridge, "_target_git", side_effect=git), \
                     mock.patch.object(bridge, "_remote_branch_head", return_value=self.head), \
                      mock.patch.object(bridge, "_state_bytes", side_effect=lambda _root, n, *_: n.encode()), \
                     self.assertRaises(bridge.BridgeError):
                    bridge._publication_snapshot(self.modules, self.target, "131")

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

    def test_historical_blocked_review_is_preserved_and_superseded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".task-state"
            state.mkdir()
            evidence = {
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
            reviews = canonical_agent_core.publication.completed_reviews(root, "13")
            self.assertEqual(len(reviews), 2)
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

    def test_exact_existing_draft_reconciles_without_duplicate_create(self) -> None:
        existing = {"number": 23, "headRefName": self.branch}
        self.agent.pr_for_branch.return_value = existing
        self.agent.pr_create.return_value = existing
        self.agent._validated_local_metadata.return_value = ("title", Path("/tmp/body"), "body")
        self.agent.default_branch.return_value = "main"
        before = {"head": self.head, "branch": self.branch, "repository": self.repository,
                  "record": self.record, "work_units": b"u", "verification": b"v", "contract": b"c",
                  "state": b"- Status: publication-ready\n"}
        after = {**before, "state": b"- Status: draft-pr-created\n"}
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_publication_snapshot_for_post", return_value=after), \
             mock.patch.object(bridge, "_target_git", return_value=""):
            result = bridge._publication_recover(self.modules, self.target, "131")
        self.assertEqual(result["pullRequest"], existing)
        self.agent.pr_create.assert_called_once_with(self.target, "131")

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

    def test_invalid_live_pr_identity_is_rejected(self) -> None:
        good = {"headRefName": self.branch, "baseRefName": "main", "headRefOid": self.head,
                "title": "title", "body": "body", "isDraft": True,
                "isCrossRepository": False, "state": "OPEN"}
        for key, value in (("headRefName", "other"), ("baseRefName", "other"),
                            ("headRefOid", "wrong"), ("state", "CLOSED"),
                            ("title", "wrong"), ("body", "wrong"), ("isDraft", False),
                           ("isCrossRepository", True)):
            with self.subTest(key=key):
                candidate = {**good, key: value}
                with self.assertRaises(canonical_agent_core.AutomationError):
                    canonical_agent_core._validate_live_pr(
                        candidate, branch=self.branch, base="main", head=self.head,
                        title="title", body="body", draft=True
                    )

    def test_interruption_then_retry_creates_exactly_once(self) -> None:
        before = {"head": self.head, "branch": self.branch, "repository": self.repository,
                  "record": self.record, "work_units": b"u", "verification": b"v", "contract": b"c",
                  "state": b"- Status: publication-ready\n"}
        after = {**before, "state": b"- Status: draft-pr-created\n"}
        creates = 0
        def create(_target: Path, _task: str) -> dict:
            nonlocal creates
            if creates == 0:
                creates = 1
                raise RuntimeError("interrupted after GitHub create")
            return {"number": 31}
        self.agent.pr_create.side_effect = create
        self.agent.pr_for_branch.return_value = {"number": 31}
        self.agent._validated_local_metadata.return_value = ("title", Path("/tmp/body"), "body")
        self.agent.default_branch.return_value = "main"
        self.agent._validate_live_pr.return_value = None
        with mock.patch.object(bridge, "_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_publication_snapshot_for_post", return_value=after), \
             mock.patch.object(bridge, "_target_git", return_value=""):
            with self.assertRaises(RuntimeError):
                bridge._publication_recover(self.modules, self.target, "131")
            bridge._publication_recover(self.modules, self.target, "131")
        self.assertEqual(creates, 1)
        self.assertEqual(self.agent.pr_create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
