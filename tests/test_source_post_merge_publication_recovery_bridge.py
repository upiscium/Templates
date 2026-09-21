from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "source_post_merge_publication_recovery_bridge_test",
    ROOT / "tools" / "automation_recovery_bridge.py",
)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class PostMergePublicationRecoveryBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.target = Path("/tmp/terreate-task-225")
        self.main = Path("/tmp/terreate-main")
        self.task = "225"
        self.branch = "task/225-vulkan-development-environment"
        self.head = "deced18f680e4432764a973cf5ac7cf99f069b12"
        self.merge = "22cde771ab9ee4fd1eb69f6a2b4ff57ac49eb245"
        self.default_revision = "3" * 40
        self.repository = "upiscium/Terreate"
        self.record = mock.Mock(path=self.target, branch=self.branch, head=self.head)
        self.lifecycle = mock.Mock()
        self.lifecycle.repo_root.return_value = self.target
        self.lifecycle.current_worktree.return_value = self.record
        self.lifecycle.worktree_for_task.return_value = self.record
        self.lifecycle.main_worktree.return_value = mock.Mock(path=self.main)
        self.lifecycle.state_path.return_value = self.target / ".task-state/task.md"
        self.lifecycle.synchronize_default_branch.return_value = {
            "branch": "main",
            "revision": self.default_revision,
        }
        self.core = mock.Mock()
        self.core.ensure_task_branch.return_value = self.branch
        self.core.canonical_repository.return_value = self.repository
        self.core.default_branch.return_value = "main"
        self.core.merge_commit_is_ancestor.return_value = True
        self.publication = mock.Mock()
        self.publication.canonical_pr_body_matches.return_value = True
        self.publication.verification_evidence.return_value = {"head": self.head}
        self.publication.completed_reviews.return_value = [
            "reviewer completed",
            "security-reviewer completed",
        ]
        self.private = mock.Mock()
        self.contract = mock.MagicMock()
        self.modules = {
            "task_lifecycle": self.lifecycle,
            "agent_core": self.core,
            "publication_metadata": self.publication,
            "git_private_state": self.private,
            "task_contract": self.contract,
        }
        self.before = {
            "record": self.record,
            "head": self.head,
            "branch": self.branch,
            "repository": self.repository,
            "base": "main",
            "base_revision": "b" * 40,
            "state": (
                b"# 225\n- Base branch: main\n- Base revision: "
                + b"b" * 40
                + b"\n- Status: draft-pr-created\n"
            ),
            "status": "draft-pr-created",
            "verification": b"verification",
            "work_units": b"reviewer and security-reviewer completed",
            "contract": b"contract",
            "issue": b"issue 225",
            "tree": "c" * 40,
            "title": "225: add Vulkan development environment",
            "body": "canonical body at " + self.head,
        }
        self.after = {
            **self.before,
            "state": self.before["state"].replace(
                b"draft-pr-created", b"integration-pending"
            ),
            "status": "integration-pending",
        }
        self.pr = {
            "number": 367,
            "headRefName": self.branch,
            "baseRefName": "main",
            "headRefOid": self.head,
            "isCrossRepository": False,
            "state": "MERGED",
            "title": self.before["title"],
            "body": self.before["body"],
            "mergeCommit": {"oid": self.merge},
        }
        self.core.pr_details.return_value = self.pr
        self.publication.canonical_metadata.return_value = (
            self.before["title"],
            self.before["body"],
        )
        self.publication.read_and_validate_metadata.return_value = (
            self.before["title"],
            self.before["body"],
        )
        self.snapshot_calls = 0
        self.lifecycle.state_status.side_effect = (
            lambda _path: "integration-pending"
            if self.snapshot_calls >= 3
            else "draft-pr-created"
        )

    def snapshot_git(self, *args: str, **_: object) -> str:
        if args[:2] == ("rev-parse", "--verify"):
            return self.head
        if args[:2] == ("rev-parse", "HEAD^{tree}"):
            return self.before["tree"]
        return ""

    def snapshot_state(self, _root: Path, name: str, *_: object) -> bytes:
        if name == "task.md":
            self.snapshot_calls += 1
            return self.after["state"] if self.snapshot_calls >= 3 else self.before["state"]
        return {
            "verification.json": b"{}",
            "work-units.json": self.before["work_units"],
            "contract.json": self.before["contract"],
        }[name]

    def test_parser_accepts_exact_task_pr_and_revision(self) -> None:
        args = bridge.parser().parse_args(
            [
                "post-merge-publication-recover",
                str(self.target),
                self.task,
                "367",
                "a" * 40,
            ]
        )
        self.assertEqual((args.task, args.pr), (self.task, "367"))

    def test_terreate_shaped_merged_pr_recovers_without_github_mutation(self) -> None:
        with (
            mock.patch.object(bridge, "_target_git", side_effect=self.snapshot_git),
            mock.patch.object(bridge, "_state_bytes", side_effect=self.snapshot_state),
            mock.patch.object(
                bridge, "_optional_state_bytes", return_value=self.before["issue"]
            ),
            mock.patch.object(
                bridge,
                "_remote_branch_head",
                side_effect=bridge.BridgeError(
                    "remote Task branch is missing (GitHub 404)"
                ),
            ) as remote_branch,
            mock.patch.object(bridge, "_validate_target_git_configuration"),
            mock.patch.object(
                bridge, "_read_publication_recovery_receipt", return_value=None
            ),
            mock.patch.object(
                bridge, "_read_post_merge_publication_receipt", return_value=None
            ),
            mock.patch.object(
                bridge, "_source_pr_list", return_value=[{"number": 367}]
            ),
        ):
            result = bridge._post_merge_publication_recover(
                self.modules, self.target, self.task, 367, "a" * 40
            )
        self.assertEqual(result["status"], "INTEGRATION_PENDING")
        self.assertEqual(result["mergeCommit"], self.merge)
        remote_branch.assert_not_called()
        self.lifecycle.recover_post_merge_publication_pending.assert_called_once()
        self.lifecycle.complete_post_merge_publication_recovery.assert_called_once()
        self.core.gh.assert_not_called()
        self.core.pr_ready.assert_not_called()
        self.core.pr_edit.assert_not_called()
        self.core.pr_create.assert_not_called()

    def test_ordinary_snapshot_still_requires_live_remote_task_branch(self) -> None:
        with (
            mock.patch.object(bridge, "_target_git", side_effect=self.snapshot_git),
            mock.patch.object(bridge, "_state_bytes", side_effect=self.snapshot_state),
            mock.patch.object(
                bridge, "_optional_state_bytes", return_value=self.before["issue"]
            ),
            mock.patch.object(
                bridge,
                "_remote_branch_head",
                side_effect=bridge.BridgeError(
                    "remote Task branch is missing (GitHub 404)"
                ),
            ) as remote_branch,
            self.assertRaisesRegex(bridge.BridgeError, "remote Task branch is missing"),
        ):
            bridge._source_publication_snapshot(self.modules, self.target, self.task)
        remote_branch.assert_called_once_with(
            self.target, self.repository, self.branch
        )

    def test_open_draft_open_ready_and_closed_unmerged_all_reject(self) -> None:
        for state, draft in (("OPEN", True), ("OPEN", False), ("CLOSED", False)):
            with self.subTest(state=state, draft=draft):
                self.core.pr_details.return_value = {
                    **self.pr,
                    "state": state,
                    "isDraft": draft,
                    "mergeCommit": None,
                }
                with mock.patch.object(
                    bridge, "_source_pr_list", return_value=[{"number": 367}]
                ), self.assertRaises(bridge.BridgeError):
                    bridge._merged_publication_pr(
                        self.modules, self.target, self.before, 367
                    )

    def test_wrong_identity_metadata_and_cross_repository_reject(self) -> None:
        cases = {
            "number": 368,
            "headRefName": "task/other",
            "baseRefName": "develop",
            "headRefOid": "9" * 40,
            "isCrossRepository": True,
            "title": "stale",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                self.core.pr_details.return_value = {**self.pr, field: value}
                with mock.patch.object(
                    bridge, "_source_pr_list", return_value=[{"number": 367}]
                ), self.assertRaises(bridge.BridgeError):
                    bridge._merged_publication_pr(
                        self.modules, self.target, self.before, 367
                    )

    def test_conflicting_receipt_rejects_before_lifecycle_transition(self) -> None:
        conflict = bridge._post_merge_receipt(
            self.target,
            self.task,
            self.before,
            368,
            self.merge,
            self.default_revision,
            "a" * 40,
        )
        with mock.patch.object(
            bridge, "_source_publication_snapshot", side_effect=[self.before, self.before]
        ), mock.patch.object(
            bridge, "_read_publication_recovery_receipt", return_value=None
        ), mock.patch.object(
            bridge, "_read_post_merge_publication_receipt", return_value=conflict
        ), mock.patch.object(
            bridge, "_source_pr_list", return_value=[{"number": 367}]
        ), self.assertRaisesRegex(bridge.BridgeError, "conflicting"):
            bridge._post_merge_publication_recover(
                self.modules, self.target, self.task, 367, "a" * 40
            )
        self.lifecycle.recover_post_merge_publication_pending.assert_not_called()

    def test_unreachable_merge_commit_rejects_before_state_transition(self) -> None:
        self.core.merge_commit_is_ancestor.return_value = False
        with mock.patch.object(
            bridge, "_source_publication_snapshot", return_value=self.before
        ), mock.patch.object(
            bridge, "_read_publication_recovery_receipt", return_value=None
        ), mock.patch.object(
            bridge, "_read_post_merge_publication_receipt", return_value=None
        ), mock.patch.object(
            bridge, "_source_pr_list", return_value=[{"number": 367}]
        ), self.assertRaisesRegex(bridge.BridgeError, "not present"):
            bridge._post_merge_publication_recover(
                self.modules, self.target, self.task, 367, "a" * 40
            )
        self.lifecycle.recover_post_merge_publication_pending.assert_not_called()

    def test_interrupted_retry_allows_default_branch_to_advance(self) -> None:
        pending = dict(self.after)
        old_default = "4" * 40
        receipt = bridge._post_merge_receipt(
            self.target,
            self.task,
            self.before,
            367,
            self.merge,
            old_default,
            "a" * 40,
        )
        with mock.patch.object(
            bridge, "_source_publication_snapshot", side_effect=[pending, pending, pending]
        ), mock.patch.object(
            bridge, "_read_publication_recovery_receipt", return_value=None
        ), mock.patch.object(
            bridge, "_read_post_merge_publication_receipt", return_value=receipt
        ), mock.patch.object(
            bridge, "_source_pr_list", return_value=[{"number": 367}]
        ):
            result = bridge._post_merge_publication_recover(
                self.modules, self.target, self.task, 367, "a" * 40
            )
        self.assertEqual(result["defaultBranchRevision"], self.default_revision)
        self.lifecycle.recover_post_merge_publication_pending.assert_not_called()
        self.lifecycle.complete_post_merge_publication_recovery.assert_called_once_with(
            self.record,
            self.task,
            pending["state"],
            {
                "work-units.json": pending["work_units"],
                "verification.json": pending["verification"],
                "contract.json": pending["contract"],
                "issue.json": pending["issue"],
            },
            receipt,
        )


if __name__ == "__main__":
    unittest.main()
