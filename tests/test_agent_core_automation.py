from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "components"
    / "agent-core"
    / ".automation"
    / "bin"
    / "agent_core.py"
)
spec = importlib.util.spec_from_file_location("agent_core", MODULE_PATH)
assert spec and spec.loader
agent_core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_core)


class AgentCoreSafetyTest(unittest.TestCase):
    def test_integration_checkpoint_preserves_opencode_project_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True,
                           capture_output=True)
            foreign = root / ".git/opencode"
            foreign.write_bytes(b"0123456789abcdef0123456789abcdef01234567")
            before = foreign.lstat()
            details = {"number": 12, "headRefOid": "a" * 40}
            with mock.patch.object(agent_core, "validate_integration", return_value=details):
                agent_core.integrate_check(root, "12")
            after = foreign.lstat()
            self.assertEqual(foreign.read_bytes(), b"0123456789abcdef0123456789abcdef01234567")
            self.assertEqual(
                (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns),
                (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns),
            )
            self.assertEqual(
                (root / ".git/agent-core/integration/pr-12.head").read_bytes(),
                b"a" * 40 + b"\n",
            )

    def test_automation_core_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".automation").mkdir()
            (root / ".automation" / "policy.toml").write_text(
                '[paths]\nautomation_core = ["Justfile", ".automation/**"]\nsecret_patterns = []\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(agent_core.AutomationError, "Automation Core"):
                agent_core.reject_unsafe_paths(root, ["Justfile"])

    def test_task_state_is_never_committable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".automation").mkdir()
            (root / ".automation" / "policy.toml").write_text(
                '[paths]\nautomation_core = []\nsecret_patterns = []\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(agent_core.AutomationError, "task-state"):
                agent_core.reject_unsafe_paths(root, [".task-state/task.md"])

    @mock.patch.object(agent_core, "default_branch", return_value="main")
    @mock.patch.object(agent_core, "current_branch", return_value="main")
    def test_default_branch_cannot_be_used_as_task_branch(self, _current, _default) -> None:
        with self.assertRaisesRegex(agent_core.AutomationError, "not the Task branch"):
            agent_core.ensure_task_branch(Path("."), "TASK-1")

    @mock.patch.object(agent_core, "default_branch", return_value="main")
    @mock.patch.object(agent_core, "current_branch", return_value="task/TASK-1-example")
    def test_task_branch_is_accepted(self, _current, _default) -> None:
        branch = agent_core.ensure_task_branch(Path("."), "TASK-1")
        self.assertEqual(branch, "task/TASK-1-example")

    def test_integration_merge_rejects_head_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "agent-core" / "integration" / "checkpoint"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.parent.parent.chmod(0o700)
            checkpoint.parent.chmod(0o700)
            checkpoint.write_text("old-head\n", encoding="utf-8")
            checkpoint.chmod(0o600)
            with (
                mock.patch.object(agent_core, "integration_checkpoint", return_value=checkpoint),
                mock.patch.object(
                    agent_core,
                    "validate_integration",
                    return_value={"headRefOid": "new-head", "number": 10},
                ),
                self.assertRaisesRegex(agent_core.AutomationError, "head moved"),
            ):
                agent_core.integrate_merge(root, "10")

    def test_integration_merge_accepts_valid_unchanged_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "agent-core/integration/checkpoint"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.parent.parent.chmod(0o700)
            checkpoint.parent.chmod(0o700)
            checkpoint.write_text("current-head\n", encoding="utf-8")
            checkpoint.chmod(0o600)
            with (
                mock.patch.object(
                    agent_core, "integration_checkpoint", return_value=checkpoint
                ),
                mock.patch.object(
                    agent_core,
                    "validate_integration",
                    return_value={"headRefOid": "current-head", "number": 10},
                ),
                mock.patch.object(agent_core, "gh") as merge,
            ):
                agent_core.integrate_merge(root, "10")
            merge.assert_called_once_with(
                "pr", "merge", "10", "--squash", "--match-head-commit",
                "current-head", cwd=root
            )

    def test_integration_merge_rejects_unsafe_canonical_checkpoint_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            details = {"number": 12, "headRefOid": "a" * 40}
            with mock.patch.object(agent_core, "validate_integration", return_value=details):
                agent_core.integrate_check(root, "12")
            checkpoint = root / ".git/agent-core/integration/pr-12.head"
            checkpoint.chmod(0o644)

            with (
                mock.patch.object(agent_core, "validate_integration") as validate,
                mock.patch.object(agent_core, "gh") as merge,
                self.assertRaisesRegex(agent_core.AutomationError, "unsafe integration checkpoint"),
            ):
                agent_core.integrate_merge(root, "12")
            validate.assert_not_called()
            merge.assert_not_called()


class PublicationMetadataTest(unittest.TestCase):
    HEAD = "a" * 40

    def setUp(self) -> None:
        self.evidence_snapshot_patch = mock.patch.object(
            agent_core.publication,
            "publication_evidence_snapshot",
            return_value={"verification.json": b"verification", "work-units.json": b"work-units"},
        )
        self.evidence_snapshot_patch.start()
        self.addCleanup(self.evidence_snapshot_patch.stop)

    @staticmethod
    def unit(role: str, state: str, digest: str = "c") -> dict:
        objective = f"review publication metadata fixture {digest}"
        evidence = f"review evidence fixture {digest}"
        return {
            "id": "",
            "requested_role": role,
            "objective": objective,
            "semantic_sha256": agent_core.lifecycle.semantic_digest(objective),
            "state": state,
            "transitions": [] if state == "in-flight" else [{
                "from": "in-flight",
                "to": state,
                "evidence": evidence,
                "evidence_sha256": agent_core.lifecycle.semantic_digest(evidence),
                "recorded_at": "2026-08-30T00:00:00+00:00",
            }],
            "created_at": "2026-08-30T00:00:00+00:00",
            "updated_at": "2026-08-30T00:00:00+00:00",
        }

    def write_work_units(self, root: Path, units: list[tuple[str, dict]]) -> None:
        state = (root / ".task-state" / "task.md").read_text(encoding="utf-8")
        branch = next(line.split(": ", 1)[1] for line in state.splitlines() if line.startswith("- Branch: "))
        worktree = next(line.split(": ", 1)[1] for line in state.splitlines() if line.startswith("- Worktree: "))
        values = {}
        for identifier, unit in units:
            unit = dict(unit)
            unit["id"] = identifier
            values[identifier] = unit
        (root / ".task-state" / "work-units.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "19",
                    "worktree": worktree,
                    "branch": branch,
                    "units": values,
                }
            ),
            encoding="utf-8",
        )

    def test_canonical_pr_body_matches_allows_only_one_terminal_lf(self) -> None:
        matches = agent_core.publication.canonical_pr_body_matches
        self.assertTrue(matches("canonical body", "canonical body"))
        self.assertTrue(matches("canonical body", "canonical body\n"))
        self.assertTrue(matches("canonical body\n", "canonical body"))
        for actual in (
            "canonical body\n\n",
            "canonical body ",
            "changed body\n",
            "canonical body\r\n",
        ):
            self.assertFalse(matches("canonical body", actual))
        self.assertFalse(matches("canonical body\r\n", "canonical body\r\n"))
        self.assertFalse(matches("canonical body\n\n", "canonical body\n"))

    def test_live_validation_uses_terminal_lf_helper(self) -> None:
        pr = {
            "headRefName": "task/19", "baseRefName": "main", "headRefOid": self.HEAD,
            "title": "19: title", "body": "canonical body", "isDraft": True,
            "isCrossRepository": False, "state": "OPEN",
        }
        with mock.patch.object(
            agent_core.publication,
            "canonical_pr_body_matches",
            wraps=agent_core.publication.canonical_pr_body_matches,
        ) as matches:
            agent_core._validate_live_pr(
                pr, branch="task/19", base="main", head=self.HEAD,
                title="19: title", body="canonical body\n", draft=True,
            )
        matches.assert_called_once_with("canonical body\n", "canonical body")

    def fixture(self, root: Path, *, reviews: bool = True) -> None:
        state = root / ".task-state"
        state.mkdir()
        (state / "task.md").write_text(
            """# 19

## Identity

- Task ID: 19
- Branch: task/19-agent-core-v3-1-1
- Worktree: /fixture
- Base branch: main
- Base revision: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

## Purpose

Repair AgentKnowledgeVault publication metadata without replacing PR #20.

## Acceptance criteria

- [x] Guard publication metadata.

## Current state

- Status: publication-ready
- Blockers: none
- Unverified: none

## Follow-up Task candidates

None yet.
""",
            encoding="utf-8",
        )
        (state / "verification.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "19",
                    "head": self.HEAD,
                    "clean_tracked_worktree": True,
                    "worktree_stable": True,
                    "project_check": {
                        "command": ["just", "project::check"],
                        "returncode": 0,
                        "executed_at": "2026-08-30T00:00:00+00:00",
                    },
                }
            ),
            encoding="utf-8",
        )
        if reviews:
            self.write_work_units(
                root,
                [("WU-19-04", self.unit("reviewer", "completed"))],
            )

    def issue_fixture(self, root: Path) -> None:
        self.fixture(root)
        state = root / ".task-state"
        task = state / "task.md"
        task.write_text(
            task.read_text(encoding="utf-8")
            .replace("- Task ID: 19", "- Task ID: 2")
            .replace("- Branch: task/19-agent-core-v3-1-1", "- Branch: task/2-switchboard")
            .replace(
                "Repair AgentKnowledgeVault publication metadata without replacing PR #20.",
                "Authoritative source: .task-state/issue.json#title (Issue #2); body: .task-state/issue.json#body",
            )
            .replace("- [x] Guard publication metadata.", "- Satisfy the authoritative Issue #2 requirements."),
            encoding="utf-8",
        )
        verification = json.loads((state / "verification.json").read_text(encoding="utf-8"))
        verification["task_id"] = "2"
        (state / "verification.json").write_text(json.dumps(verification), encoding="utf-8")
        units = json.loads((state / "work-units.json").read_text(encoding="utf-8"))
        units["task_id"] = "2"
        units["worktree"] = "/fixture"
        units["branch"] = "task/2-switchboard"
        units["units"]["WU-2-04"] = units["units"].pop("WU-19-04")
        units["units"]["WU-2-04"]["id"] = "WU-2-04"
        (state / "work-units.json").write_text(json.dumps(units), encoding="utf-8")
        payload = {
            "number": 2,
            "url": "https://github.com/upiscium/SwitchBoard/issues/2",
            "title": "Spike OpenCode headless control and event semantics",
            "body": (
                "## 目的\n"
                "OpenCodeのheadless controlとevent semanticsを検証する。\n\n"
                "背景説明はacceptance criterionではない。\n\n"
                "## 検証対象\n"
                "headless controlとevent semanticsの境界を確認する。\n\n"
                "```text\n"
                "event context example\n"
                "```\n\n"
                "## Acceptance criteria\n"
                "- [ ] イベント意味論を確認する。\n"
                "```text\n"
                "```literal\n"
                "- fake requirement\n"
                "```\n"
                "- [ ] headless event boundaryを確認する。\n\n"
                "## Non-goals\n"
                "- PWA実装\n"
                "- SQLite schema確定\n"
                "- Permission Broker本実装\n\n"
                "## Stop condition\n"
                "検証対象の境界が確認できたら停止する。"
            ),
            "state": "open",
            "repository": "upiscium/SwitchBoard",
            "labels": ["spike"],
            "assignees": [],
            "milestone": None,
        }
        digest = agent_core.task_contract._digest(payload)
        (state / "issue.json").write_text(
            json.dumps({
                "schema_version": 1,
                "issue": 2,
                "repository": "upiscium/SwitchBoard",
                "sha256": digest,
                "payload": payload,
            }),
            encoding="utf-8",
        )
        (state / "contract.json").write_text(
            json.dumps({
                "schema_version": 1,
                "issue": 2,
                "repository": "upiscium/SwitchBoard",
                "snapshot": ".task-state/issue.json",
                "sha256": digest,
            }),
            encoding="utf-8",
        )

    def test_untouched_default_template_is_rejected(self) -> None:
        body = (MODULE_PATH.parents[1] / "templates" / "pull-request.md").read_text(encoding="utf-8")
        with self.assertRaisesRegex(agent_core.publication.PublicationMetadataError, "placeholder"):
            agent_core.publication.validate_metadata("19: repair", body)

    def test_unresolved_title_placeholder_is_rejected(self) -> None:
        with self.assertRaisesRegex(agent_core.publication.PublicationMetadataError, "placeholder"):
            agent_core.publication.validate_metadata("@@TITLE@@", "## Summary\n\nDone\n\n## Acceptance criteria\n\n- done\n\n## Validation\n\n- PASS\n\n## Risks and unverified areas\n\n- none\n\n## Follow-up Tasks\n\n- none")

    def test_pass_evidence_contradicting_not_run_is_rejected(self) -> None:
        receipt = {"project_check": {"returncode": 0}}
        body = "## Summary\n\nDone\n\n## Acceptance criteria\n\n- done\n\n## Validation\n\n- `just project::check`: NOT RUN\n\n## Risks and unverified areas\n\n- none\n\n## Follow-up Tasks\n\n- none"
        with self.assertRaisesRegex(agent_core.publication.PublicationMetadataError, "contradicts"):
            agent_core.publication.validate_metadata("19: repair", body, receipt=receipt)

    def test_verification_receipt_for_another_task_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            receipt_path = root / ".task-state" / "verification.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["task_id"] = "20"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(agent_core.publication.PublicationMetadataError, "another Task"):
                agent_core.publication.verification_evidence(root, "19", self.HEAD)

    def test_symlinked_persisted_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            for name in ("verification.json", "work-units.json"):
                with self.subTest(name=name):
                    source = root / ".task-state" / name
                    target = root / f"valid-{name}"
                    target.write_bytes(source.read_bytes())
                    source.unlink()
                    source.symlink_to(target)
                    with self.assertRaisesRegex(
                        agent_core.publication.PublicationMetadataError,
                        "safely readable",
                    ):
                        agent_core.publication.canonical_metadata(
                            root, "19", head=self.HEAD, changed_paths=["one"]
                        )
                    source.unlink()
                    source.write_bytes(target.read_bytes())

    def test_verification_receipt_rejects_boolean_returncode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            receipt_path = root / ".task-state" / "verification.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["project_check"]["returncode"] = False
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(
                agent_core.publication.PublicationMetadataError,
                "PASS evidence",
            ):
                agent_core.publication.verification_evidence(root, "19", self.HEAD)

    def test_issue_closing_directives_are_neutralized_except_bound_relation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.issue_fixture(root)
            snapshot_path = root / ".task-state" / "issue.json"
            contract_path = root / ".task-state" / "contract.json"
            state_path = root / ".task-state" / "task.md"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            old_digest = snapshot["sha256"]
            snapshot["payload"]["title"] = "Fixes #99 in the event bridge"
            snapshot["payload"]["body"] = (
                "Closes #99\n\n"
                "See fixes acme/other#77 and fixes https://github.com/acme/other/issues/8."
            )
            digest = agent_core.task_contract._digest(snapshot["payload"])
            snapshot["sha256"] = digest
            metadata = json.loads(contract_path.read_text(encoding="utf-8"))
            metadata["sha256"] = digest
            state_path.write_text(
                state_path.read_text(encoding="utf-8").replace(old_digest, digest),
                encoding="utf-8",
            )
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            contract_path.write_text(json.dumps(metadata), encoding="utf-8")
            title, body = agent_core.publication.canonical_metadata(
                root, "2", head=self.HEAD, changed_paths=["one"]
            )
            directives = [
                match.group(0).casefold()
                for match in agent_core.publication.CLOSING_DIRECTIVE_RE.finditer(title + "\n" + body)
            ]
            self.assertEqual(["closes #2"], directives)
            self.assertIn("References #99", title)
            self.assertIn("References #99", body)
            self.assertIn("References acme/other#77", body)
            self.assertIn("References https://github.com/acme/other/issues/8", body)

    def test_verification_rejects_head_drift_during_project_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".task-state").mkdir()
            heads = iter((self.HEAD, "b" * 40))

            def fake_git(*args, **_kwargs):
                if args == ("rev-parse", "HEAD"):
                    return next(heads)
                if args == ("status", "--porcelain", "--untracked-files=all"):
                    return ""
                raise AssertionError(args)

            with (
                mock.patch.object(agent_core, "ensure_task_branch"),
                mock.patch.object(agent_core, "git", side_effect=fake_git),
                mock.patch.object(
                    agent_core,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        ["just", "project::check"], 0, "", ""
                    ),
                ),
                self.assertRaisesRegex(agent_core.AutomationError, "changed HEAD"),
            ):
                agent_core.verify(root, "19")
            self.assertFalse((root / ".task-state" / "verification.json").exists())

    def test_dirty_worktree_verification_cannot_authorize_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            receipt_path = root / ".task-state" / "verification.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["clean_tracked_worktree"] = False
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(agent_core.publication.PublicationMetadataError, "clean stable"):
                agent_core.publication.verification_evidence(root, "19", self.HEAD)

    def test_dogfood_fixture_prepares_complete_metadata_without_product_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, reviews=True)
            product = root / "product.txt"
            product.write_text("unchanged\n", encoding="utf-8")
            title, body = agent_core.publication.canonical_metadata(
                root, "19", head=self.HEAD, changed_paths=[".automation/bin/agent_core.py"]
            )
            agent_core.publication.write_metadata(root, title, body)
            self.assertEqual(product.read_text(encoding="utf-8"), "unchanged\n")
            self.assertIn("`just project::check`: PASS", body)
            self.assertIn("authoritative Task requirements", body)
            self.assertIn("- Requirement: Guard publication metadata.", body)
            self.assertNotIn("- [x]", body)
            self.assertIn("`WU-19-04` — `reviewer`", body)
            self.assertNotIn("security-reviewer", body)
            self.assertNotIn("NOT RUN", body)
            self.assertNotIn("Describe the implemented", body)

    def test_issue_backed_metadata_resolves_snapshot_and_preserves_source_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.issue_fixture(root)
            title, body = agent_core.publication.canonical_metadata(
                root,
                "2",
                head=self.HEAD,
                changed_paths=["components/control.py", "docs/fixes #99"],
            )
            self.assertEqual("2: Spike OpenCode headless control and event semantics", title)
            self.assertIn("Issue #2: Spike OpenCode headless control and event semantics", body)
            self.assertIn("Bound Issue source content (preserved language):", body)
            self.assertIn("> ## 目的", body)
            self.assertIn("> OpenCodeのheadless controlとevent semanticsを検証する。", body)
            self.assertIn("イベント意味論を確認する。", body)
            self.assertIn("> - [ ] イベント意味論を確認する。", body)
            self.assertIn("- Requirement: イベント意味論を確認する。", body)
            self.assertIn("- Requirement: headless event boundaryを確認する。", body)
            self.assertIn("> ```text", body)
            self.assertIn("> event context example", body)
            self.assertIn("> ```literal", body)
            self.assertIn("> ## Non-goals", body)
            self.assertIn("> - PWA実装", body)
            self.assertIn("> - SQLite schema確定", body)
            self.assertIn("> ## Stop condition", body)
            self.assertNotIn("- Requirement: PWA実装", body)
            self.assertNotIn("- Requirement: SQLite schema確定", body)
            self.assertNotIn("- Requirement: Permission Broker本実装", body)
            self.assertNotIn("- Requirement: ## Non-goals", body)
            self.assertNotIn("- Requirement: ```text", body)
            self.assertNotIn("- Requirement: event context example", body)
            self.assertNotIn("- Requirement: fake requirement", body)
            self.assertNotIn("- Requirement: 検証対象の境界が確認できたら停止する。", body)
            self.assertIn("Closes #2", body)
            directives = agent_core.publication._publication_directives(title, body)
            self.assertEqual(["closes #2"], directives)
            self.assertIn("https://github.com/upiscium/SwitchBoard/issues/2", body)
            self.assertNotIn(".task-state/issue.json#title", title + "\n" + body)
            self.assertNotIn(".task-state/issue.json#body", title + "\n" + body)
            self.assertNotIn("Authoritative source:", title + "\n" + body)

    def test_issue_backed_metadata_without_acceptance_section_uses_bounded_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.issue_fixture(root)
            snapshot_path = root / ".task-state/issue.json"
            contract_path = root / ".task-state/contract.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["payload"]["body"] = (
                "Context prose about the requested spike.\n\n"
                "## Non-goals\n"
                "- PWA実装\n"
                "- SQLite schema確定\n\n"
                "## Stop condition\n"
                "Stop after the boundary is understood."
            )
            snapshot["sha256"] = agent_core.task_contract._digest(snapshot["payload"])
            metadata = json.loads(contract_path.read_text(encoding="utf-8"))
            metadata["sha256"] = snapshot["sha256"]
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            contract_path.write_text(json.dumps(metadata), encoding="utf-8")

            _, body = agent_core.publication.canonical_metadata(
                root, "2", head=self.HEAD, changed_paths=["one"]
            )

            self.assertIn(
                "- Requirement: Satisfy the authoritative requirements in Issue #2.",
                body,
            )
            self.assertNotIn("- Requirement: Context prose about the requested spike.", body)
            self.assertNotIn("- Requirement: ## Non-goals", body)
            self.assertNotIn("- Requirement: PWA実装", body)
            self.assertNotIn("- Requirement: SQLite schema確定", body)
            self.assertNotIn("- Requirement: Stop after the boundary is understood.", body)

    def test_generated_publication_metadata_matches_source(self) -> None:
        for template in (
            "agent-base",
            "agent-cpp-cmake",
            "agent-nix",
            "agent-python",
            "agent-rust",
            "agent-typescript-node",
        ):
            generated = (
                MODULE_PATH.parents[4]
                / "templates"
                / template
                / ".automation"
                / "bin"
                / "publication_metadata.py"
            )
            self.assertEqual(
                MODULE_PATH.with_name("publication_metadata.py").read_bytes(),
                generated.read_bytes(),
                generated.as_posix(),
            )

    def test_issue_backed_metadata_rejects_missing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.issue_fixture(root)
            (root / ".task-state/issue.json").unlink()
            with self.assertRaisesRegex(
                agent_core.publication.PublicationMetadataError,
                "canonical Issue snapshot is missing",
            ):
                agent_core.publication.canonical_metadata(
                    root, "2", head=self.HEAD, changed_paths=["one"]
                )

    def test_issue_backed_metadata_rejects_snapshot_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.issue_fixture(root)
            snapshot_path = root / ".task-state/issue.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["repository"] = "other/repository"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            with self.assertRaisesRegex(
                agent_core.publication.PublicationMetadataError,
                "canonical Issue snapshot repository is malformed|canonical Issue payload identity or content|integrity check failed|identity mismatch",
            ):
                agent_core.publication.canonical_metadata(
                    root, "2", head=self.HEAD, changed_paths=["one"]
                )

    def test_issue_backed_metadata_rejects_unresolved_pointers_in_snapshot_content(self) -> None:
        for field in ("title", "body"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.issue_fixture(root)
                snapshot_path = root / ".task-state/issue.json"
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                snapshot["payload"][field] = ".task-state/issue.json#title"
                snapshot["sha256"] = agent_core.task_contract._digest(snapshot["payload"])
                metadata_path = root / ".task-state/contract.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["sha256"] = snapshot["sha256"]
                snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                with self.assertRaisesRegex(
                    agent_core.publication.PublicationMetadataError,
                    "placeholder",
                ):
                    agent_core.publication.canonical_metadata(
                        root, "2", head=self.HEAD, changed_paths=["one"]
                    )

    def test_pointer_based_task_without_snapshot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            task = root / ".task-state/task.md"
            task.write_text(
                task.read_text(encoding="utf-8").replace(
                    "Repair AgentKnowledgeVault publication metadata without replacing PR #20.",
                    "Authoritative source: .task-state/issue.json#title",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                agent_core.publication.PublicationMetadataError,
                "canonical Issue snapshot is missing",
            ):
                agent_core.publication.canonical_metadata(
                    root, "19", head=self.HEAD, changed_paths=["one"]
                )

    def test_known_blockers_and_unverified_state_are_rendered_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            task = root / ".task-state" / "task.md"
            text = task.read_text(encoding="utf-8")
            text = text.replace("- Blockers: none", "- Blockers: release approval pending")
            text = text.replace("- Unverified: none", "- Unverified: generated C++ smoke")
            task.write_text(text, encoding="utf-8")
            _, body = agent_core.publication.canonical_metadata(
                root, "19", head=self.HEAD, changed_paths=["one"]
            )
            risks = body.split("## Risks and unverified areas", 1)[1].split("## Follow-up Tasks", 1)[0]
            self.assertIn("- Blockers: release approval pending", risks)
            self.assertIn("- Unverified: generated C++ smoke", risks)
            self.assertNotIn("None recorded", risks)

    def test_missing_current_state_evidence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            task = root / ".task-state" / "task.md"
            task.write_text(
                task.read_text(encoding="utf-8").replace("- Unverified: none\n", ""),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(agent_core.publication.PublicationMetadataError, "Current state Unverified"):
                agent_core.publication.canonical_metadata(
                    root, "19", head=self.HEAD, changed_paths=["one"]
                )

    def test_missing_reviewer_evidence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, reviews=False)
            with self.assertRaisesRegex(agent_core.publication.PublicationMetadataError, "completed reviewer"):
                agent_core.publication.canonical_metadata(
                    root, "19", head=self.HEAD, changed_paths=["one"]
                )

    def test_blocked_reviewer_is_superseded_by_later_completed_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.write_work_units(
                root,
                [
                    ("WU-19-04", self.unit("reviewer", "blocked", "b")),
                    ("WU-19-05", self.unit("reviewer", "completed", "c")),
                ],
            )
            _, body = agent_core.publication.canonical_metadata(
                root, "19", head=self.HEAD, changed_paths=["one"]
            )
            self.assertIn("`WU-19-05` — `reviewer` — completed", body)
            self.assertNotIn("WU-19-04", body)

    def test_later_blocked_reviewer_supersedes_completed_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.write_work_units(
                root,
                [
                    ("WU-19-04", self.unit("reviewer", "completed")),
                    ("WU-19-05", self.unit("reviewer", "blocked")),
                ],
            )
            with self.assertRaisesRegex(
                agent_core.publication.PublicationMetadataError,
                "reviewer Work Unit is not completed: WU-19-05",
            ):
                agent_core.publication.canonical_metadata(
                    root, "19", head=self.HEAD, changed_paths=["one"]
                )

    def test_blocked_security_review_is_superseded_by_later_completed_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.write_work_units(
                root,
                [
                    ("WU-19-04", self.unit("reviewer", "completed")),
                    ("WU-19-05", self.unit("security-reviewer", "blocked", "b")),
                    ("WU-19-06", self.unit("security-reviewer", "completed", "d")),
                ],
            )
            _, body = agent_core.publication.canonical_metadata(
                root, "19", head=self.HEAD, changed_paths=["one"]
            )
            self.assertIn("`WU-19-06` — `security-reviewer` — completed", body)
            self.assertNotIn("WU-19-05", body)

    def test_later_blocked_security_review_supersedes_completed_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.write_work_units(
                root,
                [
                    ("WU-19-04", self.unit("reviewer", "completed")),
                    ("WU-19-05", self.unit("security-reviewer", "completed")),
                    ("WU-19-06", self.unit("security-reviewer", "blocked")),
                ],
            )
            with self.assertRaisesRegex(
                agent_core.publication.PublicationMetadataError,
                "security-reviewer Work Unit is not completed: WU-19-06",
            ):
                agent_core.publication.canonical_metadata(
                    root, "19", head=self.HEAD, changed_paths=["one"]
                )

    def test_provider_failure_cannot_be_attached_to_completed_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            unit = self.unit("reviewer", "completed")
            unit["transitions"][0]["provider_failure"] = {
                "provider": "openai",
                "model": "gpt-5.6-luna",
                "error": "reported after completion",
            }
            self.write_work_units(root, [("WU-19-04", unit)])
            with self.assertRaisesRegex(
                agent_core.publication.PublicationMetadataError,
                "provider failure",
            ):
                agent_core.publication.canonical_metadata(
                    root, "19", head=self.HEAD, changed_paths=["one"]
                )

    def test_completed_legacy_review_requires_evidence_transition(self) -> None:
        value = {
            "schema_version": 1,
            "task_id": "19",
            "units": {
                "WU-19-04": {
                    "requested_role": "reviewer",
                    "state": "completed",
                    "transitions": [],
                }
            },
        }
        with self.assertRaisesRegex(
            agent_core.publication.PublicationMetadataError,
            "completed reviewer Work Unit has no evidence",
        ):
            agent_core.publication._completed_reviews_from_value(value, "19", "state")

    def test_failed_non_review_work_units_do_not_gate_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.write_work_units(
                root,
                [
                    ("WU-19-04", self.unit("reviewer", "completed")),
                    ("WU-19-05", self.unit("general", "blocked")),
                    ("WU-19-06", self.unit("verifier", "failed")),
                ],
            )
            _, body = agent_core.publication.canonical_metadata(
                root, "19", head=self.HEAD, changed_paths=["one"]
            )
            self.assertIn("`WU-19-04` — `reviewer` — completed", body)

    def test_only_highest_reviewer_sequence_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.write_work_units(
                root,
                [
                    ("WU-19-05", self.unit("reviewer", "failed")),
                    ("WU-19-04", self.unit("reviewer", "blocked")),
                    ("WU-19-06", self.unit("reviewer", "completed", "d")),
                ],
            )
            _, body = agent_core.publication.canonical_metadata(
                root, "19", head=self.HEAD, changed_paths=["one"]
            )
            self.assertIn("`WU-19-06` — `reviewer` — completed", body)
            self.assertNotIn("WU-19-04", body)
            self.assertNotIn("WU-19-05", body)

    def test_only_highest_security_review_sequence_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.write_work_units(
                root,
                [
                    ("WU-19-04", self.unit("reviewer", "completed")),
                    ("WU-19-06", self.unit("security-reviewer", "completed", "d")),
                    ("WU-19-05", self.unit("security-reviewer", "failed")),
                ],
            )
            _, body = agent_core.publication.canonical_metadata(
                root, "19", head=self.HEAD, changed_paths=["one"]
            )
            self.assertIn("`WU-19-06` — `security-reviewer` — completed", body)
            self.assertNotIn("WU-19-05", body)

    def test_canonical_sequence_wins_over_json_insertion_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.write_work_units(
                root,
                [
                    ("WU-19-05", self.unit("reviewer", "blocked")),
                    ("WU-19-04", self.unit("reviewer", "completed")),
                ],
            )
            with self.assertRaisesRegex(
                agent_core.publication.PublicationMetadataError,
                "reviewer Work Unit is not completed: WU-19-05",
            ):
                agent_core.publication.canonical_metadata(
                    root, "19", head=self.HEAD, changed_paths=["one"]
                )

    def test_noncanonical_review_work_unit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.write_work_units(
                root,
                [
                    ("WU-19-04", self.unit("reviewer", "completed")),
                    ("WU-19-5", self.unit("reviewer", "completed")),
                ],
            )
            with self.assertRaisesRegex(
                agent_core.publication.PublicationMetadataError,
                "no canonical sequence: WU-19-5",
            ):
                agent_core.publication.canonical_metadata(
                    root, "19", head=self.HEAD, changed_paths=["one"]
                )

    def test_complete_metadata_allows_draft_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            title, body_text = agent_core.publication.canonical_metadata(root, "19", head=self.HEAD, changed_paths=["one"])
            agent_core.publication.write_metadata(root, title, body_text)
            context = {"record": mock.sentinel.record, "status": "publication-ready", "repository": "example/repo"}
            live = {"number": 20, "title": title, "body": body_text.rstrip(), "headRefName": "task/19-agent-core-v3-1-1", "baseRefName": "main", "isDraft": True, "isCrossRepository": False, "state": "OPEN", "headRefOid": self.HEAD}
            with (
                mock.patch.object(agent_core, "verify") as verify_mock,
                mock.patch.object(agent_core, "_publication_context", return_value=(live["headRefName"], context, self.HEAD)),
                mock.patch.object(agent_core, "_validated_local_metadata", return_value=(title, root / ".task-state/pr-body.md", body_text.rstrip())),
                 mock.patch.object(agent_core, "default_branch", return_value="main"),
                 mock.patch.object(agent_core, "canonical_repository", return_value="example/repo"),
                 mock.patch.object(agent_core, "remote_branch_head", return_value=self.HEAD),
                 mock.patch.object(agent_core, "git", return_value=self.HEAD),
                 mock.patch.object(agent_core, "pr_for_branch", side_effect=[None, live]),
                mock.patch.object(agent_core, "gh") as gh,
                mock.patch.object(agent_core.lifecycle, "mark_task_publication_state") as transition,
            ):
                agent_core.pr_create(root, "19")
            self.assertIn("--draft", gh.call_args.args)
            self.assertEqual("-", gh.call_args.args[gh.call_args.args.index("--body-file") + 1])
            self.assertEqual(body_text.rstrip(), gh.call_args.kwargs["input_text"])
            verify_mock.assert_called_once_with(root, "19")
            transition.assert_called_once()

    def test_symlinked_publication_body_is_rejected_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            title, body = agent_core.publication.canonical_metadata(
                root, "19", head=self.HEAD, changed_paths=["one"]
            )
            agent_core.publication.write_metadata(root, title, body)
            body_path = root / ".task-state/pr-body.md"
            body_path.unlink()
            body_path.symlink_to(root / "product.txt")
            with self.assertRaisesRegex(
                agent_core.publication.PublicationMetadataError,
                "run agent::pr-prepare",
            ):
                agent_core.publication.read_and_validate_metadata(root)

    def test_create_reconciles_exact_existing_draft_without_write(self) -> None:
        live = {
            "number": 20, "title": "title", "body": "canonical\n",
            "headRefName": "task/19-fix", "baseRefName": "main",
            "isDraft": True, "isCrossRepository": False, "state": "OPEN",
            "headRefOid": self.HEAD,
        }
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", return_value=(
                "task/19-fix", {"record": mock.sentinel.record, "status": "publication-ready", "repository": "example/repo"}, self.HEAD
            )),
             mock.patch.object(agent_core, "default_branch", return_value="main"),
             mock.patch.object(agent_core, "canonical_repository", return_value="example/repo"),
             mock.patch.object(agent_core, "remote_branch_head", return_value=self.HEAD),
             mock.patch.object(agent_core, "git", return_value=self.HEAD),
             mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
            mock.patch.object(agent_core, "pr_for_branch", side_effect=[live, live]),
            mock.patch.object(agent_core, "gh") as gh,
            mock.patch.object(agent_core.lifecycle, "mark_task_publication_state") as transition,
        ):
            with mock.patch("builtins.print") as output:
                agent_core.pr_create(Path("."), "19")
        gh.assert_not_called()
        transition.assert_called_once()
        self.assertEqual(json.loads(output.call_args.args[0]), live)

    def test_create_rejects_noncanonical_existing_pr_without_write(self) -> None:
        for field, value in (
            ("headRefOid", "b" * 40),
            ("title", "wrong title"),
            ("body", "wrong body"),
            ("isDraft", False),
            ("isCrossRepository", True),
        ):
            live = {
                "number": 20, "title": "title", "body": "canonical",
                "headRefName": "task/19-fix", "baseRefName": "main",
                "isDraft": True, "isCrossRepository": False, "state": "OPEN",
                "headRefOid": self.HEAD,
            }
            live[field] = value
            with (
                mock.patch.object(agent_core, "verify"),
                mock.patch.object(agent_core, "_publication_context", return_value=(
                    "task/19-fix", {"record": mock.sentinel.record, "status": "publication-ready", "repository": "example/repo"}, self.HEAD
                )),
                mock.patch.object(agent_core, "default_branch", return_value="main"),
                mock.patch.object(agent_core, "canonical_repository", return_value="example/repo"),
                mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
                mock.patch.object(agent_core, "pr_for_branch", return_value=live),
                mock.patch.object(agent_core, "gh") as gh,
                self.assertRaises(agent_core.AutomationError),
            ):
                agent_core.pr_create(Path("."), "19")
            gh.assert_not_called()

    def test_create_retry_reconciles_after_interrupted_transition(self) -> None:
        live = {
            "number": 20, "title": "title", "body": "canonical",
            "headRefName": "task/19-fix", "baseRefName": "main",
            "isDraft": True, "isCrossRepository": False, "state": "OPEN",
            "headRefOid": self.HEAD,
        }
        context = {"record": mock.sentinel.record, "status": "publication-ready", "repository": "example/repo"}
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", context, self.HEAD)),
            mock.patch.object(agent_core, "default_branch", return_value="main"),
             mock.patch.object(agent_core, "canonical_repository", return_value="example/repo"),
             mock.patch.object(agent_core, "remote_branch_head", return_value=self.HEAD),
             mock.patch.object(agent_core, "git", return_value=self.HEAD),
             mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
            mock.patch.object(agent_core, "pr_for_branch", side_effect=[None, live, live, live]),
            mock.patch.object(agent_core, "gh"),
            mock.patch.object(
                agent_core.lifecycle,
                "mark_task_publication_state",
                side_effect=[agent_core.lifecycle.LifecycleError("interrupted"), None],
            ) as transition,
        ):
            with self.assertRaisesRegex(agent_core.AutomationError, "interrupted"):
                agent_core.pr_create(Path("."), "19")
            agent_core.pr_create(Path("."), "19")
        self.assertEqual(transition.call_count, 2)

    def test_create_rejects_remote_head_drift_before_github_write(self) -> None:
        context = {"record": mock.sentinel.record, "status": "publication-ready", "repository": "example/repo"}
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", context, self.HEAD)),
            mock.patch.object(agent_core, "default_branch", return_value="main"),
            mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
            mock.patch.object(agent_core, "pr_for_branch", return_value=None),
            mock.patch.object(agent_core, "remote_branch_head", return_value="b" * 40),
            mock.patch.object(agent_core, "gh") as gh,
            self.assertRaisesRegex(agent_core.AutomationError, "remote Task branch"),
        ):
            agent_core.pr_create(Path("."), "19")
        gh.assert_not_called()

    def test_existing_stale_draft_is_repaired_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body_file = root / ".task-state/pr-body.md"
            body_file.parent.mkdir()
            body_file.write_text("canonical", encoding="utf-8")
            stale = {"number": 20, "headRefName": "task/19-fix", "baseRefName": "main", "headRefOid": self.HEAD, "isDraft": True, "isCrossRepository": False, "state": "OPEN"}
            updated = {"number": 20, "title": "title", "body": "canonical", "headRefName": "task/19-fix", "baseRefName": "main", "isDraft": True, "isCrossRepository": False, "state": "OPEN", "headRefOid": self.HEAD}
            with (
                mock.patch.object(agent_core, "verify") as verify_mock,
                mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", {"record": mock.sentinel.record, "status": "draft-pr-created", "repository": "example/repo"}, self.HEAD)),
                 mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", body_file, "canonical")),
                 mock.patch.object(agent_core, "default_branch", return_value="main"),
                 mock.patch.object(agent_core, "canonical_repository", return_value="example/repo"),
                 mock.patch.object(agent_core, "git", return_value=self.HEAD),
                 mock.patch.object(agent_core, "pr_for_branch", side_effect=[stale, stale, updated]),
                mock.patch.object(agent_core, "gh") as gh,
            ):
                agent_core.pr_edit(root, "19")
            self.assertEqual(gh.call_args.args[:3], ("pr", "edit", "20"))
            verify_mock.assert_called_once_with(root, "19")

    def test_pr_edit_expected_number_exact_match_allows_mutation(self) -> None:
        stale = {"number": 20, "headRefName": "task/19-fix", "baseRefName": "main", "headRefOid": self.HEAD, "isDraft": True, "isCrossRepository": False, "state": "OPEN"}
        updated = {**stale, "title": "title", "body": "canonical"}
        context = {"record": mock.sentinel.record, "status": "publication-ready", "repository": "example/repo"}
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", context, self.HEAD)),
             mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
             mock.patch.object(agent_core, "default_branch", return_value="main"),
             mock.patch.object(agent_core, "canonical_repository", return_value="example/repo"),
             mock.patch.object(agent_core, "git", return_value=self.HEAD),
              mock.patch.object(agent_core, "pr_for_branch", side_effect=[stale, stale, updated]),
            mock.patch.object(agent_core, "gh") as gh,
            mock.patch.object(agent_core.lifecycle, "mark_task_publication_state") as transition,
        ):
            agent_core.pr_edit(Path("."), "19", expected_pr_number=20)
        self.assertEqual(gh.call_args.args[:3], ("pr", "edit", "20"))
        transition.assert_called_once_with(
            mock.sentinel.record,
            "19",
            "publication-ready",
            "draft-pr-created",
            expected_evidence={"verification.json": b"verification", "work-units.json": b"work-units"},
        )

    def test_pr_edit_rejects_invalid_expected_number_before_mutation(self) -> None:
        context = {"record": mock.sentinel.record, "status": "publication-ready", "repository": "example/repo"}
        for expected in (True, False, 0, -1, "29"):
            with self.subTest(expected=expected):
                with (
                    mock.patch.object(agent_core, "verify"),
                    mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", context, self.HEAD)),
                    mock.patch.object(agent_core, "pr_for_branch") as resolve,
                    mock.patch.object(agent_core, "gh") as gh,
                ):
                    with self.assertRaisesRegex(agent_core.AutomationError, "expected pull request number is invalid"):
                        agent_core.pr_edit(Path("."), "19", expected_pr_number=expected)
                    resolve.assert_not_called()
                    gh.assert_not_called()

    def test_create_and_edit_do_not_write_when_verification_fails(self) -> None:
        for action in (agent_core.pr_create, agent_core.pr_edit):
            with (
                mock.patch.object(
                    agent_core,
                    "verify",
                    side_effect=agent_core.AutomationError("verification failed"),
                ),
                mock.patch.object(agent_core, "gh") as gh,
                self.assertRaisesRegex(agent_core.AutomationError, "verification failed"),
            ):
                action(Path("."), "19")
            gh.assert_not_called()

    def test_pr_edit_rejects_wrong_identity_before_write(self) -> None:
        wrong = {"number": 20, "headRefName": "task/19-fix", "baseRefName": "other", "headRefOid": self.HEAD, "isDraft": True, "isCrossRepository": False, "state": "OPEN"}
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", {"record": mock.sentinel.record, "status": "draft-pr-created", "repository": "example/repo"}, self.HEAD)),
            mock.patch.object(agent_core, "default_branch", return_value="main"),
            mock.patch.object(agent_core, "pr_for_branch", return_value=wrong),
            mock.patch.object(agent_core, "gh") as gh,
            self.assertRaisesRegex(agent_core.AutomationError, "repair target identity"),
        ):
            agent_core.pr_edit(Path("."), "19")
        gh.assert_not_called()

    def test_pr_ready_reconciles_already_ready_pr(self) -> None:
        ready = {"number": 20, "title": "title", "body": "canonical", "headRefName": "task/19-fix", "baseRefName": "main", "isDraft": False, "isCrossRepository": False, "state": "OPEN", "headRefOid": self.HEAD}
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", {"record": mock.sentinel.record, "status": "draft-pr-created", "repository": "example/repo"}, self.HEAD)),
            mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
            mock.patch.object(agent_core, "default_branch", return_value="main"),
            mock.patch.object(agent_core, "pr_for_branch", side_effect=[ready, ready]),
            mock.patch.object(agent_core, "gh") as gh,
            mock.patch.object(agent_core.lifecycle, "mark_task_publication_state") as transition,
        ):
            agent_core.pr_ready(Path("."), "19")
        gh.assert_not_called()
        transition.assert_called_once()

    def test_pr_ready_expected_number_validation_precedes_lookup(self) -> None:
        context = {"record": mock.sentinel.record, "status": "draft-pr-created", "repository": "example/repo"}
        for expected in (True, False, 0, -1, "20"):
            with self.subTest(expected=expected):
                with (
                    mock.patch.object(agent_core, "verify"),
                    mock.patch.object(agent_core, "pr_for_branch") as resolve,
                    mock.patch.object(agent_core, "gh") as gh,
                ):
                    with self.assertRaisesRegex(agent_core.AutomationError, "expected pull request number is invalid"):
                        agent_core.pr_ready(Path("."), "19", expected_pr_number=expected)
                    resolve.assert_not_called()
                    gh.assert_not_called()

    def test_pr_ready_expected_number_must_match_before_mutation(self) -> None:
        pr = {"number": 20}
        context = {"record": mock.sentinel.record, "status": "draft-pr-created", "repository": "example/repo"}
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", context, self.HEAD)),
            mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
            mock.patch.object(agent_core, "default_branch", return_value="main"),
            mock.patch.object(agent_core, "pr_for_branch", return_value=pr),
            mock.patch.object(agent_core, "gh") as gh,
        ):
            with self.assertRaisesRegex(agent_core.AutomationError, "identity changed before mutation"):
                agent_core.pr_ready(Path("."), "19", expected_pr_number=21)
        gh.assert_not_called()

    def test_pr_ready_exact_match_rechecks_before_ready_write(self) -> None:
        draft = {"number": 20, "title": "title", "body": "canonical", "headRefName": "task/19-fix", "baseRefName": "main", "isDraft": True, "isCrossRepository": False, "state": "OPEN", "headRefOid": self.HEAD}
        replacement = {**draft, "number": 21}
        context = {"record": mock.sentinel.record, "status": "draft-pr-created", "repository": "example/repo"}
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", context, self.HEAD)),
            mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
            mock.patch.object(agent_core, "default_branch", return_value="main"),
            mock.patch.object(agent_core, "pr_for_branch", side_effect=[draft, replacement]),
            mock.patch.object(agent_core, "gh") as gh,
        ):
            with self.assertRaisesRegex(agent_core.AutomationError, "identity changed before mutation"):
                agent_core.pr_ready(Path("."), "19", expected_pr_number=20)
        gh.assert_not_called()

    def test_pr_ready_rejects_replacement_before_lifecycle_transition(self) -> None:
        draft = {"number": 20, "title": "title", "body": "canonical", "headRefName": "task/19-fix", "baseRefName": "main", "isDraft": True, "isCrossRepository": False, "state": "OPEN", "headRefOid": self.HEAD}
        ready = {**draft, "isDraft": False}
        replacement = {**ready, "number": 21}
        context = {"record": mock.sentinel.record, "status": "draft-pr-created", "repository": "example/repo"}
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", context, self.HEAD)),
            mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
            mock.patch.object(agent_core, "default_branch", return_value="main"),
            mock.patch.object(agent_core, "canonical_repository", return_value="example/repo"),
            mock.patch.object(agent_core, "pr_for_branch", side_effect=[draft, draft, ready, replacement]),
            mock.patch.object(agent_core, "gh"),
            mock.patch.object(agent_core.lifecycle, "mark_task_publication_state") as transition,
        ):
            with self.assertRaisesRegex(agent_core.AutomationError, "before lifecycle transition"):
                agent_core.pr_ready(Path("."), "19", expected_pr_number=20)
        transition.assert_not_called()

    def test_pr_ready_explicit_guard_rejects_context_drift(self) -> None:
        draft = {"number": 20, "title": "title", "body": "canonical", "headRefName": "task/19-fix", "baseRefName": "main", "isDraft": True, "isCrossRepository": False, "state": "OPEN", "headRefOid": self.HEAD}
        ready = {**draft, "isDraft": False}
        initial = {"record": mock.sentinel.record, "status": "draft-pr-created", "repository": "example/repo"}
        changed = {"record": mock.sentinel.other_record, "status": "draft-pr-created", "repository": "example/repo"}
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", side_effect=[("task/19-fix", initial, self.HEAD), ("task/19-fix", changed, self.HEAD)]),
            mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
            mock.patch.object(agent_core, "default_branch", return_value="main"),
            mock.patch.object(agent_core, "canonical_repository", return_value="example/repo"),
            mock.patch.object(agent_core, "pr_for_branch", side_effect=[draft, draft, ready]),
            mock.patch.object(agent_core, "gh"),
            mock.patch.object(agent_core.lifecycle, "mark_task_publication_state") as transition,
        ):
            with self.assertRaisesRegex(agent_core.AutomationError, "publication context changed"):
                agent_core.pr_ready(Path("."), "19", expected_pr_number=20)
        transition.assert_not_called()

    def test_pr_ready_explicit_guard_rejects_post_ready_metadata_drift(self) -> None:
        draft = {"number": 20, "title": "title", "body": "canonical", "headRefName": "task/19-fix", "baseRefName": "main", "isDraft": True, "isCrossRepository": False, "state": "OPEN", "headRefOid": self.HEAD}
        ready = {**draft, "isDraft": False}
        context = {"record": mock.sentinel.record, "status": "draft-pr-created", "repository": "example/repo"}
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", context, self.HEAD)),
            mock.patch.object(agent_core, "_validated_local_metadata", side_effect=[("title", Path("body"), "canonical"), ("changed", Path("body"), "canonical")]),
            mock.patch.object(agent_core, "default_branch", return_value="main"),
            mock.patch.object(agent_core, "canonical_repository", return_value="example/repo"),
            mock.patch.object(agent_core, "pr_for_branch", side_effect=[draft, draft, ready]),
            mock.patch.object(agent_core, "gh"),
            mock.patch.object(agent_core.lifecycle, "mark_task_publication_state") as transition,
        ):
            with self.assertRaisesRegex(agent_core.AutomationError, "local publication metadata changed"):
                agent_core.pr_ready(Path("."), "19", expected_pr_number=20)
        transition.assert_not_called()

    def test_pr_ready_none_preserves_normal_ready_flow(self) -> None:
        draft = {"number": 20, "title": "title", "body": "canonical", "headRefName": "task/19-fix", "baseRefName": "main", "isDraft": True, "isCrossRepository": False, "state": "OPEN", "headRefOid": self.HEAD}
        ready = {**draft, "isDraft": False}
        context = {"record": mock.sentinel.record, "status": "draft-pr-created", "repository": "example/repo"}
        with (
            mock.patch.object(agent_core, "verify") as verify_mock,
            mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", context, self.HEAD)),
             mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
             mock.patch.object(agent_core, "default_branch", return_value="main"),
             mock.patch.object(agent_core, "canonical_repository", return_value="example/repo"),
             mock.patch.object(agent_core, "git", return_value=self.HEAD),
             mock.patch.object(agent_core, "pr_for_branch", side_effect=[draft, draft, ready, ready]),
            mock.patch.object(agent_core, "gh") as gh,
            mock.patch.object(agent_core.lifecycle, "mark_task_publication_state") as transition,
        ):
            agent_core.pr_ready(Path("."), "19")
        gh.assert_called_once_with("pr", "ready", "20", "--repo", "example/repo", cwd=Path("."))
        transition.assert_called_once_with(
            mock.sentinel.record,
            "19",
            "draft-pr-created",
            "integration-pending",
            expected_evidence={"verification.json": b"verification", "work-units.json": b"work-units"},
        )
        self.assertEqual(2, verify_mock.call_count)
        verify_mock.assert_called_with(Path("."), "19")

    def test_pr_ready_rejects_stale_live_body_before_write(self) -> None:
        live = {"number": 20, "title": "title", "body": "stale", "headRefName": "task/19-fix", "baseRefName": "main", "isDraft": True, "isCrossRepository": False, "state": "OPEN", "headRefOid": self.HEAD}
        with (
            mock.patch.object(agent_core, "verify"),
            mock.patch.object(agent_core, "_publication_context", return_value=("task/19-fix", {"record": mock.sentinel.record, "status": "draft-pr-created", "repository": "example/repo"}, self.HEAD)),
            mock.patch.object(agent_core, "_validated_local_metadata", return_value=("title", Path("body"), "canonical")),
            mock.patch.object(agent_core, "default_branch", return_value="main"),
            mock.patch.object(agent_core, "pr_for_branch", return_value=live),
            mock.patch.object(agent_core, "gh") as gh,
            self.assertRaisesRegex(agent_core.AutomationError, "stale or inconsistent"),
        ):
            agent_core.pr_ready(Path("."), "19")
        gh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
