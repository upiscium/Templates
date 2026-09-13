import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("recovery_bridge", ROOT / "tools/automation_recovery_bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)


class PublicationReadyRecoveryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.target = Path("/tmp/akv-task-13")
        self.main = Path("/tmp/akv-main")
        self.task = "13"
        self.head = "8313bcfcfdee4a3985f8b5dd48861b2b2bcb69a4"
        self.branch = "task/13-hybrid-level1-reranking"
        self.repository = "upiscium/AgentKnowledgeVault"
        self.record = mock.Mock(path=self.target, branch=self.branch, head=self.head)
        self.lifecycle = mock.Mock()
        self.lifecycle.repo_root.return_value = self.target
        self.lifecycle.current_worktree.return_value = mock.Mock(path=self.target)
        self.lifecycle.main_worktree.return_value = mock.Mock(path=self.main)
        self.lifecycle.worktree_for_task.return_value = self.record
        self.lifecycle.state_path.return_value = self.target / ".task-state/task.md"
        self.lifecycle.state_status.return_value = "draft-pr-created"
        self.core = mock.Mock()
        self.core.ensure_task_branch.return_value = self.branch
        self.core.canonical_repository.return_value = self.repository
        self.core.default_branch.return_value = "main"
        self.core.verify = mock.Mock(name="original_verify")
        self.publication = mock.Mock()
        self.publication.verification_evidence.return_value = {"head": self.head}
        self.publication.completed_reviews.return_value = [
            "- `WU-13-52` — `reviewer` — completed",
            "- `WU-13-53` — `security-reviewer` — completed",
        ]
        self.contract = mock.MagicMock()
        self.modules = {"task_lifecycle": self.lifecycle, "agent_core": self.core,
                        "publication_metadata": self.publication, "task_contract": self.contract}

    def test_recovery_subprocess_environment_rejects_transport_and_config_injection(self):
        environment = bridge.sanitized_environment(
            {
                "HOME": "/home/operator",
                "XDG_CONFIG_HOME": "/home/operator/.config",
                "GH_TOKEN": "secret",
                "HTTPS_PROXY": "https://attacker.invalid",
                "NO_PROXY": "github.com",
                "SSL_CERT_FILE": "/tmp/attacker-ca.pem",
                "GH_CONFIG_DIR": "/tmp/attacker-gh",
                "GH_REPO": "attacker/repository",
                "PATH": "/tmp/attacker-bin",
            }
        )
        self.assertEqual(environment["LC_ALL"], "C")
        for key in (
            "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "GH_CONFIG_DIR",
            "GH_REPO", "PATH", "HOME", "XDG_CONFIG_HOME", "GH_TOKEN",
        ):
            self.assertNotIn(key, environment)

    def test_maintenance_uses_private_gh_config_with_explicit_token_only(self):
        with mock.patch.dict(
            bridge.os.environ,
            {"GH_TOKEN": "operator-token", "GH_CONFIG_DIR": "/tmp/attacker-gh"},
            clear=True,
        ):
            with bridge.maintenance_environment():
                environment = bridge.trusted_gh_environment()
                config = Path(environment["GH_CONFIG_DIR"])
                self.assertTrue(config.is_dir())
                self.assertEqual(config.stat().st_mode & 0o777, 0o700)
                self.assertEqual(environment["GH_HOST"], "github.com")
                self.assertEqual(environment["GH_TOKEN"], "operator-token")
                self.assertNotEqual(str(config), "/tmp/attacker-gh")

    def _snapshot_git(self, *args, **kwargs):
        if args[:2] == ("rev-parse", "--verify"):
            return self.head
        if args[:2] == ("rev-parse", "HEAD^{tree}"):
            return "t" * 40
        return ""

    def _snapshot(self):
        state = (b"# 13\n- Base branch: main\n- Base revision: " + b"b" * 40 +
                 b"\n- Status: draft-pr-created\n")
        values = {
            "task.md": state,
            "verification.json": b'{"head":"' + self.head.encode() + b'"}',
            "work-units.json": b'old history WU-13-28; WU-13-52; WU-13-53',
            "contract.json": b'contract', "issue.json": b'issue',
        }
        with mock.patch.object(bridge, "_target_git", side_effect=self._snapshot_git), \
             mock.patch.object(bridge, "_remote_branch_head", return_value=self.head), \
             mock.patch.object(bridge, "_state_bytes", side_effect=lambda _root, n, *_rest: values[n]), \
             mock.patch.object(bridge, "_optional_state_bytes", return_value=values["issue.json"]):
            self.publication.canonical_metadata.return_value = (
                "13: hybrid level1 reranking", "current reviews at " + self.head)
            self.publication.read_and_validate_metadata.return_value = (
                "13: hybrid level1 reranking", "current reviews at " + self.head)
            self.publication.canonical_pr_body_matches.return_value = True
            self.core._target_diff = None
            return bridge._source_publication_snapshot(self.modules, self.target, self.task)

    def test_preferred_parser_surface_and_exact_revision(self):
        args = bridge.parser().parse_args(
            ["publication-ready-recover", "/consumer", "29", "a" * 40]
        )
        self.assertEqual(args.command, "publication-ready-recover")
        self.assertEqual(args.task, "29")
        with self.assertRaises(bridge.BridgeError):
            bridge.parser().parse_args(
                ["publication-ready-recover", "/consumer", "29", "not-a-revision"]
            )

    def test_pr_number_rejects_bool_zero_and_negative(self):
        class Core:
            def pr_for_branch(self, *args):
                return {"number": self.number}

            def _validate_live_pr(self, *args, **kwargs):
                return None

        snapshot = {"branch": "task/29-repair", "repository": "org/repo",
                    "base": "main", "head": "a" * 40, "title": "29: repair",
                    "body": "body"}
        for value in (True, 0, -1):
            core = Core()
            core.number = value
            with mock.patch.object(bridge, "_source_pr_list", return_value=[{"number": value}]), self.assertRaises(bridge.BridgeError):
                 bridge._source_pr(core, Path("/consumer"), snapshot, ready=False)

    def test_akv_snapshot_keeps_historical_work_units_and_effective_reviews(self):
        snapshot = self._snapshot()
        self.assertEqual(snapshot["repository"], self.repository)
        self.assertIn(b"WU-13-28", snapshot["work_units"])
        self.publication.completed_reviews.assert_called_once_with(self.target, self.task)
        self.assertIn("WU-13-52", " ".join(self.publication.completed_reviews.return_value))

    def test_source_pr_requires_unique_pr_and_validates_exact_pr_29(self):
        snapshot = {"repository": self.repository, "branch": self.branch, "base": "main",
                    "head": self.head, "title": "canonical", "body": "body"}
        pr = {"number": 29, "headRefName": self.branch, "baseRefName": "main",
              "headRefOid": self.head, "title": "canonical", "body": "body",
              "isDraft": True, "isCrossRepository": False, "state": "OPEN"}
        self.core.pr_for_branch.return_value = pr
        with mock.patch.object(bridge, "_source_pr_list", return_value=[pr]):
            found, number = bridge._source_pr(self.core, self.target, snapshot, ready=False)
        self.assertIs(found, pr)
        self.assertEqual(number, 29)
        self.core._validate_live_pr.assert_called_once_with(
            pr, branch=self.branch, base="main", head=self.head,
            title="canonical", body="body", draft=True)

    def test_source_pr_accepts_exact_already_ready_pr_for_interrupted_draft_state(self):
        snapshot = {"repository": self.repository, "branch": self.branch, "base": "main",
                    "head": self.head, "title": "canonical", "body": "body"}
        pr = {"number": 29, "headRefName": self.branch, "baseRefName": "main",
              "headRefOid": self.head, "title": "canonical", "body": "body",
              "isDraft": False, "isCrossRepository": False, "state": "OPEN"}
        self.core.pr_for_branch.return_value = pr
        with mock.patch.object(bridge, "_source_pr_list", return_value=[pr]):
            found, number = bridge._source_pr(self.core, self.target, snapshot, ready=None)
        self.assertIs(found, pr)
        self.assertEqual(number, 29)
        self.core._validate_live_pr.assert_called_once_with(
            pr, branch=self.branch, base="main", head=self.head,
            title="canonical", body="body", draft=False)

    def test_source_pr_rejects_wrong_branch_base_head_closed_cross_repo_and_stale_metadata(self):
        snapshot = {"repository": self.repository, "branch": self.branch, "base": "main",
                    "head": self.head, "title": "canonical", "body": "body"}
        exact = {"number": 29, "headRefName": self.branch, "baseRefName": "main",
                 "headRefOid": self.head, "title": "canonical", "body": "body",
                 "isDraft": True, "isCrossRepository": False, "state": "OPEN"}
        cases = [
            {**exact, "headRefName": "task/other"}, {**exact, "baseRefName": "develop"},
            {**exact, "headRefOid": "9" * 40}, {**exact, "state": "CLOSED"},
            {**exact, "isCrossRepository": True}, {**exact, "title": "stale"},
            {**exact, "body": "stale"}, {**exact, "isDraft": False},
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.core.reset_mock()
                self.core.pr_for_branch.return_value = candidate
                self.core._validate_live_pr.side_effect = bridge.BridgeError("live PR is stale or invalid")
                with mock.patch.object(bridge, "_source_pr_list", return_value=[candidate]), \
                     self.assertRaises(bridge.BridgeError):
                    bridge._source_pr(self.core, self.target, snapshot, ready=False)

    def test_duplicate_pr_and_invalid_number_fail_closed(self):
        with mock.patch.object(bridge, "_pinned_run", return_value=mock.Mock(
                stdout=json.dumps([{"number": 29}, {"number": 30}]), returncode=0)), \
             self.assertRaisesRegex(bridge.BridgeError, "exactly one"):
            bridge._source_pr_list(self.target, self.repository, self.branch)
        snapshot = {"repository": self.repository, "branch": self.branch, "base": "main",
                    "head": self.head, "title": "canonical", "body": "body"}
        for number in (True, 0, -1, "29"):
            candidate = {"number": number}
            self.core.reset_mock()
            self.core.pr_for_branch.return_value = candidate
            with self.subTest(number=number), mock.patch.object(bridge, "_source_pr_list", return_value=[candidate]), \
                 self.assertRaises(bridge.BridgeError):
                bridge._source_pr(self.core, self.target, snapshot, ready=False)

    def test_ready_recovery_draft_to_integration_pending_is_state_only_and_restores_verify(self):
        before = self._snapshot()
        after = dict(before)
        after["state"] = before["state"].replace(b"draft-pr-created", b"integration-pending")
        after["status"] = "integration-pending"
        pr = {"number": 29, "headRefName": self.branch, "baseRefName": "main",
              "headRefOid": self.head, "title": before["title"], "body": before["body"],
              "isDraft": True, "isCrossRepository": False, "state": "OPEN"}
        ready_pr = {**pr, "isDraft": False}
        self.core.pr_for_branch.return_value = pr
        original = self.core.verify

        def canonical_ready(root, task, *, expected_pr_number):
            self.assertEqual((root, task, expected_pr_number), (self.target, self.task, 29))
            self.assertIsNot(self.core.verify, original)
            self.core.verify(root, task)

        self.core.pr_ready.side_effect = canonical_ready
        with mock.patch.object(bridge, "_source_publication_snapshot", side_effect=[before, before, before, after]), \
             mock.patch.object(bridge, "_source_pr_list", return_value=[pr]), \
             mock.patch.object(bridge, "_read_publication_recovery_receipt", return_value=None), \
             mock.patch.object(bridge, "_source_pr", side_effect=[(pr, 29), (pr, 29), (ready_pr, 29)]), \
             mock.patch.object(bridge, "_validate_target_git_configuration"), \
             mock.patch.object(bridge, "_target_git", side_effect=self._snapshot_git), \
             mock.patch.object(bridge, "_state_bytes", return_value=before["verification"]), \
             mock.patch.object(bridge, "_pinned_run") as pinned:
            result = bridge._publication_ready_recover(self.modules, self.target, self.task)
        self.assertEqual(result["pullRequest"], ready_pr)
        self.core.pr_ready.assert_called_once_with(self.target, self.task, expected_pr_number=29)
        self.assertIs(self.core.verify, original)
        self.assertEqual(after["work_units"], before["work_units"])
        self.assertEqual(after["tree"], before["tree"])
        self.assertFalse(any(c.args and c.args[0][:3] == ["gh", "pr", "ready"]
                             for c in pinned.call_args_list))

    def test_ready_recovery_retries_after_github_ready_before_local_transition(self):
        before = self._snapshot()
        after = dict(before)
        after["state"] = before["state"].replace(b"draft-pr-created", b"integration-pending")
        after["status"] = "integration-pending"
        ready_pr = {"number": 29, "headRefName": self.branch, "baseRefName": "main",
                    "headRefOid": self.head, "title": before["title"], "body": before["body"],
                    "isDraft": False, "isCrossRepository": False, "state": "OPEN"}
        with mock.patch.object(bridge, "_source_publication_snapshot", side_effect=[before, before, after]), \
             mock.patch.object(bridge, "_read_publication_recovery_receipt", return_value=None), \
             mock.patch.object(bridge, "_source_pr", side_effect=[(ready_pr, 29), (ready_pr, 29), (ready_pr, 29)]) as source_pr, \
             mock.patch.object(bridge, "_target_git", return_value=""):
            result = bridge._publication_ready_recover(self.modules, self.target, self.task)
        self.assertEqual(result["pullRequest"], ready_pr)
        self.assertEqual(source_pr.call_args_list[0].kwargs["ready"], None)
        self.core.pr_ready.assert_called_once_with(
            self.target, self.task, expected_pr_number=29
        )

    def test_ready_recovery_receipt_and_all_preconditions_fail_before_pr_ready(self):
        before = self._snapshot()
        with mock.patch.object(bridge, "_source_publication_snapshot", return_value=before), \
             mock.patch.object(bridge, "_read_publication_recovery_receipt", return_value={"pr_number": 29}), \
             self.assertRaisesRegex(bridge.BridgeError, "receipt exists"):
            bridge._publication_ready_recover(self.modules, self.target, self.task)
        for error in ("target worktree must be clean", "Task HEAD, local branch, and remote branch differ",
                      "contract mismatch", "missing persisted verification evidence",
                      "persisted publication metadata is not canonical"):
            with self.subTest(error=error):
                self.core.pr_ready.reset_mock()
                with mock.patch.object(bridge, "_source_publication_snapshot", side_effect=bridge.BridgeError(error)), \
                     mock.patch.object(bridge, "_read_publication_recovery_receipt", return_value=None):
                    with self.assertRaises(bridge.BridgeError):
                        bridge._publication_ready_recover(self.modules, self.target, self.task)
                self.core.pr_ready.assert_not_called()

    def test_snapshot_blocks_dirty_mismatch_and_unresolved_contract_before_reviews(self):
        self.lifecycle.require_resolved_contract.side_effect = RuntimeError("contract mismatch")
        with mock.patch.object(bridge, "_target_git", side_effect=self._snapshot_git), \
             self.assertRaisesRegex(bridge.BridgeError, "contract mismatch"):
            self._snapshot()
        self.lifecycle.require_resolved_contract.side_effect = None
        for remote in ("9" * 40,):
            with self.subTest(remote=remote), \
                 mock.patch.object(bridge, "_target_git", side_effect=self._snapshot_git), \
                 mock.patch.object(bridge, "_remote_branch_head", return_value=remote), \
                  mock.patch.object(bridge, "_state_bytes", side_effect=lambda _root, n, *_rest: {
                     "task.md": b"- Base branch: main\n- Base revision: " + b"b" * 40,
                     "verification.json": b"{}", "work-units.json": b"u", "contract.json": b"c"}[n]), \
                 mock.patch.object(bridge, "_optional_state_bytes", return_value=None), \
                 self.assertRaisesRegex(bridge.BridgeError, "HEAD.*differ"):
                bridge._source_publication_snapshot(self.modules, self.target, self.task)
        with mock.patch.object(bridge, "_target_git", side_effect=lambda *a, **k: "dirty\n" if a[:1] == ("status",) else self._snapshot_git(*a, **k)), \
             mock.patch.object(bridge, "_remote_branch_head", return_value=self.head), \
             mock.patch.object(bridge, "_state_bytes", side_effect=lambda _root, n, *_rest: {
                 "task.md": b"- Base branch: main\n- Base revision: " + b"b" * 40,
                 "verification.json": b"{}", "work-units.json": b"u", "contract.json": b"c"}[n]), \
             mock.patch.object(bridge, "_optional_state_bytes", return_value=None), \
             self.assertRaisesRegex(bridge.BridgeError, "clean"):
            bridge._source_publication_snapshot(self.modules, self.target, self.task)

    def test_snapshot_blocks_stale_verification_or_effective_reviewer_security_review(self):
        for method, message in (
            ("verification_evidence", "verification evidence is stale"),
            ("completed_reviews", "required reviewer Work Unit is not completed"),
            ("completed_reviews", "required security-reviewer Work Unit is not completed"),
        ):
            with self.subTest(message=message):
                setattr(getattr(self.publication, method), "side_effect", RuntimeError(message))
                with mock.patch.object(bridge, "_target_git", side_effect=self._snapshot_git), \
                     mock.patch.object(bridge, "_remote_branch_head", return_value=self.head), \
                     mock.patch.object(bridge, "_state_bytes", side_effect=lambda _root, n, *_rest: {
                         "task.md": b"- Base branch: main\n- Base revision: " + b"b" * 40,
                         "verification.json": b"{}", "work-units.json": b"u", "contract.json": b"c"}[n]), \
                     mock.patch.object(bridge, "_optional_state_bytes", return_value=None), \
                     self.assertRaisesRegex(bridge.BridgeError, message):
                    bridge._source_publication_snapshot(self.modules, self.target, self.task)
                getattr(self.publication, method).side_effect = None

    def test_integration_pending_is_terminal_validation_only_and_idempotent(self):
        before = self._snapshot()
        before["status"] = "integration-pending"
        before["state"] = before["state"].replace(b"draft-pr-created", b"integration-pending")
        pr = {"number": 29, "isDraft": False}
        with mock.patch.object(bridge, "_source_publication_snapshot", side_effect=[before, before]), \
             mock.patch.object(bridge, "_read_publication_recovery_receipt", return_value=None), \
             mock.patch.object(bridge, "_source_pr", return_value=(pr, 29)):
            result = bridge._publication_ready_recover(self.modules, self.target, self.task)
        self.assertEqual(result["status"], "INTEGRATION_PENDING")
        self.core.pr_ready.assert_not_called()

    def test_replacement_race_before_pr_ready_and_post_state_mismatches_fail_closed(self):
        before = self._snapshot()
        pr = {"number": 29}
        with mock.patch.object(bridge, "_source_publication_snapshot", side_effect=[before, before]), \
             mock.patch.object(bridge, "_read_publication_recovery_receipt", return_value=None), \
             mock.patch.object(bridge, "_source_pr", side_effect=[(pr, 29), (dict(pr, number=30), 30)]):
            with self.assertRaisesRegex(bridge.BridgeError, "identity changed"):
                bridge._publication_ready_recover(self.modules, self.target, self.task)
        self.core.pr_ready.assert_not_called()
        for key in ("status", "verification", "tree"):
            after = dict(before)
            if key == "status":
                after["status"] = "draft-pr-created"
            else:
                after[key] = b"different" if isinstance(after[key], bytes) else "different"
            self.core.reset_mock()
            with self.subTest(key=key), \
                 mock.patch.object(bridge, "_source_publication_snapshot", side_effect=[before, before, after]), \
                 mock.patch.object(bridge, "_read_publication_recovery_receipt", return_value=None), \
                 mock.patch.object(bridge, "_source_pr", side_effect=[(pr, 29), (pr, 29)]), \
                 mock.patch.object(bridge, "_target_git", return_value=""):
                with self.assertRaises(bridge.BridgeError):
                    bridge._publication_ready_recover(self.modules, self.target, self.task)
            self.core.pr_ready.assert_called_once()

    def test_parser_rejects_zero_negative_bool_like_and_wrong_revision(self):
        for task in ("0", "-1", "01", "True"):
            with self.subTest(task=task), self.assertRaises(bridge.BridgeError):
                bridge.parser().parse_args(["publication-ready-recover", "/tmp", task, "a" * 40])

    def test_source_listing_rejects_duplicate_branch_pull_requests(self):
        result = mock.Mock(stdout="[{\"number\": 1}, {\"number\": 2}]", returncode=0)
        with mock.patch.object(bridge, "_pinned_run", return_value=result):
            with self.assertRaisesRegex(bridge.BridgeError, "exactly one"):
                bridge._source_pr_list(Path("/consumer"), "org/repo", "task/29-repair")


if __name__ == "__main__":
    unittest.main()
