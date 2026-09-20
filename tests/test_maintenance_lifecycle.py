from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "components" / "agent-core" / ".automation" / "bin"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
SCRIPT = SCRIPT_DIR / "maintenance_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("maintenance_lifecycle_for_tests", SCRIPT)
assert SPEC and SPEC.loader
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)


class MaintenanceLifecycleTest(unittest.TestCase):
    HEAD = "a" * 40
    BASE = "b" * 40
    OLD_REMOTE = "9" * 40
    MERGE = "c" * 40

    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    def _init_repo(self, root: Path) -> None:
        self._git(root, "init", "-b", "main")
        self._git(root, "config", "user.name", "Maintenance Test")
        self._git(root, "config", "user.email", "maintenance@example.invalid")

    def _commit(self, root: Path, message: str) -> str:
        self._git(root, "add", ".")
        self._git(root, "commit", "-m", message)
        return self._git(root, "rev-parse", "HEAD")

    def _write(self, root: Path, relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _record(self, root: Path):
        return maintenance.lifecycle.WorktreeRecord(
            root,
            "task/21-agent-core-v3-1-5",
            self.HEAD,
        )

    def _contract(self, root: Path) -> dict:
        return {
            "status": "READY",
            "task": "21",
            "worktree": str(root),
            "issue": 21,
            "repository": "example/repo",
            "sha256": "d" * 64,
        }

    def _receipt(self, **values) -> dict:
        return {
            "authority_head": self.BASE,
            "source": "/trusted/Templates",
            "source_revision": self.HEAD,
            "changed_paths": [".automation/bin/maintenance.py"],
            "path_fingerprints": {
                ".automation/bin/maintenance.py": {
                    "state": "file",
                    "mode": 0o644,
                    "content_sha256": "e" * 64,
                }
            },
            **values,
        }

    def _state(self, root: Path, status: str = "initialized") -> None:
        state = root / ".task-state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "task.md").write_text(
            f"""# Task 21

## Identity

- Task ID: 21
- Branch: task/21-agent-core-v3-1-5
- Worktree: {root}

## Current state

- Status: {status}
- Blockers: none
- Unverified: none
""",
            encoding="utf-8",
        )

    def test_applied_stage_uses_active_receipt_without_normal_resume_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            active = root / "active.json"
            active.write_text("{}\n", encoding="utf-8")
            consumed = root / "consumed.json"
            with (
                mock.patch.object(maintenance.lifecycle, "state_status", return_value="initialized"),
                mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                mock.patch.object(maintenance.upgrade, "consumed_receipt_path", return_value=consumed),
                mock.patch.object(
                    maintenance,
                    "_validate_active_receipt",
                    return_value=self._receipt(),
                ),
            ):
                result = maintenance._maintenance_stage(record, "21", self._contract(root))
            self.assertEqual(result["status"], "READY")
            self.assertEqual(result["mode"], "maintenance")
            self.assertEqual(result["stage"], "applied")
            self.assertEqual(result["taskStatus"], "initialized")

    def test_maintenance_stage_rejects_dangling_receipt_symlinks(self) -> None:
        for kind in ("active", "consumed"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                record = self._record(root)
                active = root / "active.json"
                consumed = root / "consumed.json"
                selected = active if kind == "active" else consumed
                selected.symlink_to(root / "missing-receipt.json")
                with (
                    mock.patch.object(
                        maintenance.lifecycle, "state_status", return_value="initialized"
                    ),
                    mock.patch.object(
                        maintenance.upgrade, "receipt_path", return_value=active
                    ),
                    mock.patch.object(
                        maintenance.upgrade,
                        "consumed_receipt_path",
                        return_value=consumed,
                    ),
                    self.assertRaises(maintenance.MaintenanceError),
                ):
                    maintenance._maintenance_stage(
                        record, "21", self._contract(root)
                    )

    def test_consumed_receipt_resumes_when_remote_is_old_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            active = root / "active.json"
            consumed = root / "consumed.json"
            consumed.write_text("{}\n", encoding="utf-8")
            receipt = self._receipt(commit_sha=self.HEAD)

            class Result:
                returncode = 0

            with (
                mock.patch.object(maintenance.lifecycle, "state_status", return_value="initialized"),
                mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                mock.patch.object(maintenance.upgrade, "consumed_receipt_path", return_value=consumed),
                mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                mock.patch.object(maintenance, "_remote_head", return_value=self.OLD_REMOTE),
                mock.patch.object(maintenance.lifecycle, "run", return_value=Result()),
                mock.patch.object(maintenance, "_pr_evidence", return_value=None),
            ):
                result = maintenance._maintenance_stage(record, "21", self._contract(root))
            self.assertEqual(result["stage"], "committed")
            self.assertEqual(result["remoteHead"], self.OLD_REMOTE)
            self.assertEqual(result["remoteRelation"], "ancestor")

    def test_consumed_receipt_uses_real_ancestry_and_rejects_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            self._write(root, "tracked.txt", "base\n")
            base = self._commit(root, "base")
            self._write(root, "tracked.txt", "older maintenance\n")
            older = self._commit(root, "older maintenance")
            self._write(root, "latest.txt", "latest maintenance\n")
            head = self._commit(root, "latest maintenance")
            self._git(root, "checkout", "-b", "diverged", base)
            self._write(root, "diverged.txt", "diverged\n")
            diverged = self._commit(root, "diverged")
            self._git(root, "checkout", "main")

            record = maintenance.lifecycle.WorktreeRecord(
                root, "task/21-agent-core-v3-1-5", head
            )
            active = root / "active.json"
            consumed = root / "consumed.json"
            consumed.write_text("{}\n", encoding="utf-8")
            receipt = self._receipt(
                commit_sha=head, authority_head=base, source_revision=head
            )
            with (
                mock.patch.object(maintenance.lifecycle, "state_status", return_value="initialized"),
                mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                mock.patch.object(maintenance.upgrade, "consumed_receipt_path", return_value=consumed),
                mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                mock.patch.object(maintenance, "_remote_head", return_value=older),
                mock.patch.object(maintenance, "_pr_evidence", return_value=None),
            ):
                result = maintenance._maintenance_stage(record, "21", self._contract(root))
            self.assertEqual(result["stage"], "committed")
            self.assertEqual(result["remoteRelation"], "ancestor")

            with (
                mock.patch.object(maintenance.lifecycle, "state_status", return_value="initialized"),
                mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                mock.patch.object(maintenance.upgrade, "consumed_receipt_path", return_value=consumed),
                mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                mock.patch.object(maintenance, "_remote_head", return_value=diverged),
                mock.patch.object(maintenance, "_pr_evidence", return_value=None),
            ):
                with self.assertRaisesRegex(
                    maintenance.MaintenanceError, "not the maintenance commit or its ancestor"
                ):
                    maintenance._maintenance_stage(record, "21", self._contract(root))

    def test_consumed_receipt_stages_resume_from_commit_push_and_draft_pr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            active = root / "active.json"
            consumed = root / "consumed.json"
            consumed.write_text("{}\n", encoding="utf-8")
            receipt = self._receipt(commit_sha=self.HEAD)
            cases = (
                (None, None, "committed"),
                (self.HEAD, None, "pushed"),
                (
                    self.HEAD,
                    {"number": 22, "state": "OPEN", "isDraft": True},
                    "draft-pr-created",
                ),
                (
                    None,
                    {"number": 22, "state": "MERGED", "isDraft": False},
                    "merged-remote",
                ),
            )
            for remote, pr, expected in cases:
                with self.subTest(stage=expected):
                    with (
                        mock.patch.object(maintenance.lifecycle, "state_status", return_value="initialized"),
                        mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                        mock.patch.object(maintenance.upgrade, "consumed_receipt_path", return_value=consumed),
                        mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                        mock.patch.object(maintenance, "_remote_head", return_value=remote),
                        mock.patch.object(maintenance, "_pr_evidence", return_value=pr),
                    ):
                        result = maintenance._maintenance_stage(record, "21", self._contract(root))
                    self.assertEqual(result["stage"], expected)
                    self.assertEqual(result["taskStatus"], "initialized")

    def test_review_evidence_requires_reviewer_and_security_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            receipt = self._receipt(commit_sha=self.HEAD)
            subject = maintenance._review_subject(receipt)
            objective = maintenance._review_objective("21", "reviewer", subject)
            with mock.patch.object(
                maintenance.lifecycle,
                "read_work_units",
                return_value={
                    "units": {
                        "WU-21-01": {
                            "requested_role": "reviewer",
                            "objective": objective,
                            "semantic_sha256": maintenance.lifecycle.semantic_digest(objective),
                            "state": "completed",
                            "transitions": [
                                {"to": "completed", "evidence_sha256": "f" * 64}
                            ],
                        }
                    }
                },
            ):
                with self.assertRaisesRegex(maintenance.MaintenanceError, "security-reviewer"):
                    maintenance._require_review_evidence(record, "21", receipt)

    def test_review_evidence_is_bound_to_exact_maintenance_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            old_receipt = self._receipt(commit_sha=self.HEAD)
            new_receipt = self._receipt(
                commit_sha=self.HEAD,
                source_revision=self.MERGE,
                changed_paths=[".automation/bin/newer.py"],
                path_fingerprints={
                    ".automation/bin/newer.py": {
                        "state": "file",
                        "mode": 0o644,
                        "content_sha256": "1" * 64,
                    }
                },
            )
            old_subject = maintenance._review_subject(old_receipt)
            units = {}
            for index, role in enumerate(("reviewer", "security-reviewer"), start=1):
                objective = maintenance._review_objective("21", role, old_subject)
                units[f"WU-21-0{index}"] = {
                    "requested_role": role,
                    "objective": objective,
                    "semantic_sha256": maintenance.lifecycle.semantic_digest(objective),
                    "state": "completed",
                    "transitions": [
                        {"to": "completed", "evidence_sha256": str(index) * 64}
                    ],
                }
            with mock.patch.object(
                maintenance.lifecycle,
                "read_work_units",
                return_value={"units": units},
            ):
                with self.assertRaisesRegex(
                    maintenance.MaintenanceError, "reviewer, security-reviewer"
                ):
                    maintenance._require_review_evidence(record, "21", new_receipt)

    def test_review_recorder_persists_exact_subject_without_physical_main_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root / "task")
            receipt = self._receipt(commit_sha=self.HEAD)
            units = {"schema_version": 1, "task_id": "21", "units": {}}
            evidence = "status: COMPLETED; no findings for the exact maintenance subject"
            with (
                mock.patch.object(
                    maintenance.lifecycle,
                    "require_main_worktree",
                    side_effect=AssertionError("review recording must not inspect physical main"),
                ) as main_only,
                mock.patch.object(
                    maintenance, "_validated_contract", return_value=(record, self._contract(record.path))
                ),
                mock.patch.object(
                    maintenance, "_validate_consumed_receipt", return_value=receipt
                ),
                mock.patch.object(
                    maintenance.lifecycle, "work_units_lock", return_value=nullcontext()
                ),
                mock.patch.object(maintenance.lifecycle, "assert_task_identity"),
                mock.patch.object(
                    maintenance.lifecycle, "read_work_units", return_value=units
                ),
                mock.patch.object(maintenance.lifecycle, "persist_work_units") as persist,
            ):
                result = maintenance.maintenance_review_record(
                    root, "21", "security-reviewer", evidence
                )
            main_only.assert_not_called()
            persisted = persist.call_args.args[1]
            unit = persisted["units"][result["workUnit"]]
            self.assertEqual(unit["state"], "completed")
            self.assertEqual(
                unit["objective"],
                maintenance._review_objective(
                    "21", "security-reviewer", maintenance._review_subject(receipt)
                ),
            )
            self.assertEqual(unit["transitions"][-1]["evidence"], evidence)

            with self.assertRaisesRegex(
                    maintenance.MaintenanceError, "must start with status: COMPLETED;"
            ):
                maintenance.maintenance_review_record(
                    root, "21", "security-reviewer", "status: BLOCKED; no result"
                )

    def test_task_worktree_dogfood_review_gate_is_exact_and_fails_closed(self) -> None:
        """Exercise Issue #110 from a linked worktree, not a synthetic record."""
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = temporary / "repository"
            remote = temporary / "remote.git"
            task = temporary / "task-21"
            repository.mkdir()
            self._init_repo(repository)
            self._git(temporary, "init", "--bare", str(remote))
            self._git(repository, "remote", "add", "origin", "https://github.com/example/repo.git")
            self._git(repository, "remote", "add", "fixture-remote", str(remote))

            template = (
                ROOT / "components" / "agent-core" / ".automation" / "templates" / "task-state.md"
            ).read_text(encoding="utf-8")
            self._write(repository, ".automation/templates/task-state.md", template)
            self._write(repository, ".automation/VERSION", "3\n")
            self._write(repository, ".automation/bin/maintenance.py", "recipe: pre-v3.1.6\n")
            base = self._commit(repository, "pre-v3.1.6 Agent Core")
            self._git(repository, "push", "fixture-remote", "main")
            self._git(repository, "update-ref", "refs/remotes/origin/main", base)
            self._git(
                repository,
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/main",
            )
            self._git(repository, "worktree", "add", "-b", "task/21-agent-core-v3-1-5", str(task), base)
            self._git(repository, "push", "fixture-remote", "task/21-agent-core-v3-1-5")
            common_git = maintenance.upgrade.common_git_dir(task)
            (common_git / "info" / "exclude").write_text("/.task-state/\n", encoding="utf-8")

            self._write(
                task,
                ".task-state/task.md",
                template.replace("@@TASK_ID@@", "21")
                .replace("@@BRANCH@@", "task/21-agent-core-v3-1-5")
                .replace("@@WORKTREE@@", str(task))
                .replace("@@BASE_BRANCH@@", "main")
                .replace("@@BASE_REVISION@@", base),
            )
            payload = {
                "number": 21,
                "url": "https://github.com/example/repo/issues/21",
                "title": "Agent Core maintenance v3.1.6",
                "body": "Upgrade the Agent Core marker and maintenance recipe.",
                "state": "open",
                "repository": "example/repo",
                "labels": ["maintenance"],
                "assignees": [],
                "milestone": None,
            }
            digest = maintenance.task_contract._digest(payload)
            self._write(task, ".task-state/issue.json", json.dumps({
                "schema_version": 1, "issue": 21, "repository": "example/repo",
                "sha256": digest, "payload": payload,
            }) + "\n")
            self._write(task, ".task-state/contract.json", json.dumps({
                "schema_version": 1, "issue": 21, "repository": "example/repo",
                "snapshot": ".task-state/issue.json", "sha256": digest,
            }) + "\n")
            state = task / ".task-state/task.md"
            state.write_text(
                maintenance.task_contract._canonical_state(state.read_text(encoding="utf-8"), 21, digest),
                encoding="utf-8",
            )

            self._write(task, ".automation/VERSION", "3\n")
            self._write(task, ".automation/bin/maintenance.py", "recipe: v3.1.6\n")
            commit = self._commit(task, "upgrade Agent Core to v3.1.6")
            # Keep the fixture remote at the previous commit while the local Task advances.
            self._git(repository, "push", "--force", "fixture-remote", f"{base}:refs/heads/task/21-agent-core-v3-1-5")

            changed_paths = [".automation/bin/maintenance.py"]
            receipt = {
                "schema_version": 1,
                "status": "consumed",
                "task_id": "21",
                "branch": "task/21-agent-core-v3-1-5",
                "worktree": str(task),
                "source": str(repository),
                "source_revision": base,
                "current_version": "3",
                "upstream_version": "3",
                "changed_paths": changed_paths,
                "authority_head": base,
                "authority_nonce": "a" * 64,
                "path_fingerprints": {
                    path: maintenance.upgrade.file_fingerprint(task, path) for path in changed_paths
                },
                "commit_sha": commit,
            }
            self._write(task, ".task-state/automation-maintenance.consumed.json", json.dumps(receipt) + "\n")

            external = mock.patch.object(
                maintenance.task_contract, "_validate_authoritative_issue"
            )
            with (
                external,
                mock.patch.object(maintenance, "_remote_head", return_value=base),
                mock.patch.object(maintenance, "_pr_evidence", return_value=None),
            ):
                ready = maintenance.maintenance_check(task, "21")
                validated_receipt = maintenance._validate_consumed_receipt(
                    maintenance.lifecycle.WorktreeRecord(
                        task, "task/21-agent-core-v3-1-5", commit
                    ),
                    "21",
                )
                self.assertEqual(validated_receipt["commit_sha"], commit)
                self.assertEqual(
                    validated_receipt["path_fingerprints"], receipt["path_fingerprints"]
                )
                self.assertEqual(ready["stage"], "committed")
                self.assertEqual(ready["taskStatus"], "initialized")
                self.assertEqual(ready["remoteHead"], base)
                self.assertEqual(ready["remoteRelation"], "ancestor")
                self.assertIsNone(ready["pr"])
                self.assertEqual(ready["reviewEvidence"], {"reviewer": False, "security-reviewer": False})

                evidence = "status: COMPLETED; exact Task #21 upgrade evidence"
                for role in ("reviewer", "security-reviewer"):
                    recorded = maintenance.maintenance_review_record(task, "21", role, evidence)
                    self.assertEqual(recorded["status"], "RECORDED")
                    units = maintenance.lifecycle.read_work_units(
                        maintenance.lifecycle.WorktreeRecord(
                            task, "task/21-agent-core-v3-1-5", commit
                        ),
                        "21",
                    )
                    self.assertEqual(
                        units["units"][recorded["workUnit"]]["objective"],
                        ready["reviewObjectives"][role],
                    )
                    duplicate = maintenance.maintenance_review_record(task, "21", role, evidence)
                    self.assertEqual(duplicate["status"], "ALREADY_RECORDED")
                    with self.assertRaisesRegex(
                        maintenance.MaintenanceError, "different or invalid completed evidence"
                    ):
                        maintenance.maintenance_review_record(
                            task,
                            "21",
                            role,
                            "status: COMPLETED; different evidence for the same subject",
                        )

                checked = maintenance.maintenance_check(task, "21")
                self.assertEqual(checked["reviewEvidence"], {"reviewer": True, "security-reviewer": True})
                maintenance._require_review_evidence(
                    maintenance.lifecycle.WorktreeRecord(task, "task/21-agent-core-v3-1-5", commit),
                    "21",
                    validated_receipt,
                )

                receipt["source_revision"] = "b" * 40
                self._write(task, ".task-state/automation-maintenance.consumed.json", json.dumps(receipt) + "\n")
                stale = maintenance.maintenance_check(task, "21")
                self.assertEqual(stale["taskStatus"], "initialized")
                self.assertEqual(stale["reviewEvidence"], {"reviewer": False, "security-reviewer": False})
                with self.assertRaisesRegex(maintenance.MaintenanceError, "reviewer, security-reviewer"):
                    maintenance._require_review_evidence(
                        maintenance.lifecycle.WorktreeRecord(task, "task/21-agent-core-v3-1-5", commit),
                        "21",
                        maintenance._validate_consumed_receipt(
                            maintenance.lifecycle.WorktreeRecord(task, "task/21-agent-core-v3-1-5", commit), "21"
                        ),
                    )
                self.assertNotIn("merged", maintenance.lifecycle.LINEAR_TRANSITIONS["initialized"])

    def test_pr_create_uses_full_direct_upgrade_diff_not_latest_receipt_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            body_path = root / ".task-state" / "pr-body.md"
            body_path.parent.mkdir(parents=True)
            body_path.write_text("body\n", encoding="utf-8")
            receipt = self._receipt(
                commit_sha=self.HEAD,
                changed_paths=[".automation/bin/second-upgrade-only.py"],
            )
            full_paths = [
                ".automation/bin/first-upgrade.py",
                ".automation/bin/second-upgrade-only.py",
            ]
            live = {
                "number": 22,
                "title": "21: maintenance",
                "body": "body",
                "headRefName": record.branch,
                "baseRefName": "main",
                "headRefOid": self.HEAD,
                "isDraft": True,
                "isCrossRepository": False,
                "state": "OPEN",
            }
            with (
                mock.patch.object(maintenance.lifecycle, "require_local_task", return_value=record),
                mock.patch.object(
                    maintenance,
                    "maintenance_check",
                    return_value={**self._contract(root), "stage": "pushed", "mode": "maintenance"},
                ),
                mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                mock.patch.object(maintenance, "_remote_head", return_value=self.HEAD),
                mock.patch.object(maintenance, "_require_review_evidence"),
                mock.patch.object(maintenance.agent_core, "verify"),
                mock.patch.object(maintenance.publication, "verification_evidence"),
                mock.patch.object(maintenance, "_direct_upgrade_publication", return_value=full_paths),
                mock.patch.object(
                    maintenance.publication,
                    "canonical_metadata",
                    return_value=("21: maintenance", "body\n"),
                ) as metadata,
                mock.patch.object(maintenance.publication, "write_metadata"),
                mock.patch.object(
                    maintenance.agent_core,
                    "_validated_local_metadata",
                    return_value=("21: maintenance", body_path, "body"),
                ),
                mock.patch.object(maintenance.agent_core, "default_branch", return_value="main"),
                mock.patch.object(maintenance.agent_core, "pr_for_branch", side_effect=[live, live]),
                mock.patch.object(maintenance.agent_core, "canonical_repository", return_value="example/repo"),
                mock.patch.object(maintenance.agent_core, "_validate_live_pr"),
                mock.patch.object(maintenance.agent_core, "gh") as gh_mock,
            ):
                result = maintenance.maintenance_pr_create(root, "21")
            gh_mock.assert_not_called()
            metadata.assert_called_once_with(
                root, "21", head=self.HEAD, changed_paths=full_paths
            )
            self.assertEqual(result["stage"], "draft-pr-created")
            self.assertEqual(result["pr"], 22)

    def test_direct_upgrade_proof_covers_all_maintenance_commits_from_task_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            task = temporary / "task"
            source = temporary / "templates"
            task.mkdir()
            source.mkdir()
            self._init_repo(task)
            self._init_repo(source)

            self._write(task, ".automation/VERSION", "3\n")
            self._write(task, ".automation/bin/first-upgrade.py", "base\n")
            base = self._commit(task, "Task base")
            self._write(task, ".automation/bin/first-upgrade.py", "first upgrade\n")
            self._commit(task, "first maintenance upgrade")
            self._write(task, ".automation/bin/second-upgrade.py", "second upgrade\n")
            head = self._commit(task, "second maintenance upgrade")

            self._write(source, "components/agent-core/.automation/VERSION", "3\n")
            self._write(
                source,
                "components/agent-core/.automation/bin/first-upgrade.py",
                "first upgrade\n",
            )
            self._write(
                source,
                "components/agent-core/.automation/bin/second-upgrade.py",
                "second upgrade\n",
            )
            source_revision = self._commit(source, "final Templates source")

            self._state(task)
            state = task / ".task-state/task.md"
            state.write_text(
                state.read_text(encoding="utf-8")
                + f"\n## Provenance\n\n- Base revision: {base}\n",
                encoding="utf-8",
            )
            receipt = {
                "commit_sha": head,
                "source": str(source),
                "source_revision": source_revision,
                "changed_paths": [".automation/bin/second-upgrade.py"],
            }
            record = maintenance.lifecycle.WorktreeRecord(
                task, "task/21-agent-core-v3-1-5", head
            )

            paths = maintenance._direct_upgrade_publication(record, receipt)
            self.assertEqual(
                paths,
                [
                    ".automation/bin/first-upgrade.py",
                    ".automation/bin/second-upgrade.py",
                ],
            )

            (task / ".automation/bin/first-upgrade.py").write_text(
                "stale first upgrade\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                maintenance.MaintenanceError,
                "does not match the reconstructed direct upgrade",
            ):
                maintenance._direct_upgrade_publication(record, receipt)

    def test_finalize_revalidates_direct_upgrade_and_uses_dedicated_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_root = root / "task"
            task_root.mkdir()
            record = self._record(task_root)
            contract = self._contract(task_root)
            receipt = {"commit_sha": self.HEAD}
            merged = {"mergeCommitOid": self.MERGE}
            sync = {"branch": "main", "revision": "e" * 40, "updated": True}
            publication_evidence = ([".automation/bin/a.py"], "21: maintenance", "body")

            class Result:
                returncode = 0

            with (
                mock.patch.object(maintenance.lifecycle, "require_main_worktree"),
                mock.patch.object(maintenance, "_stored_contract", return_value=(record, contract)),
                mock.patch.object(maintenance, "_validate_consumed_receipt", return_value=receipt),
                mock.patch.object(
                    maintenance,
                    "_publication_evidence",
                    return_value=publication_evidence,
                ) as reconstruct,
                mock.patch.object(maintenance, "_merged_pr", side_effect=[merged, merged]),
                mock.patch.object(maintenance.lifecycle, "synchronize_default_branch", return_value=sync),
                mock.patch.object(maintenance.lifecycle, "run", return_value=Result()),
                mock.patch.object(maintenance.lifecycle, "require_synchronized_default_branch_revision"),
                mock.patch.object(maintenance, "_mark_maintenance_merged", return_value="finalized") as mark,
                mock.patch.object(maintenance.lifecycle, "append_task_evidence"),
            ):
                result = maintenance.maintenance_finalize(root, "21", 22)
            self.assertEqual(reconstruct.call_count, 2)
            mark.assert_called_once_with(
                record,
                "21",
                publication_line=(
                    f"PR #22 merged from {self.HEAD}; merge commit {self.MERGE}; "
                    "finalization finalized"
                ),
                validate_before_write=mock.ANY,
                default_branch="main",
                default_revision="e" * 40,
            )
            self.assertEqual(result["status"], "FINALIZED")
            self.assertEqual(result["stage"], "merged")

    def test_publication_evidence_rejects_stale_verification_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            receipt = self._receipt(commit_sha=self.HEAD)
            with (
                mock.patch.object(maintenance, "_require_review_evidence"),
                mock.patch.object(
                    maintenance.publication,
                    "verification_evidence",
                    side_effect=maintenance.publication.PublicationMetadataError(
                        "project verification evidence is stale"
                    ),
                ),
                self.assertRaisesRegex(maintenance.MaintenanceError, "verification evidence is stale"),
            ):
                maintenance._publication_evidence(record, "21", receipt)

    def test_merged_pr_requires_canonical_title_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            details = {
                "number": 22,
                "title": "stale title",
                "body": "canonical body",
                "headRefName": record.branch,
                "baseRefName": "main",
                "headRefOid": self.HEAD,
                "isCrossRepository": False,
                "state": "MERGED",
                "mergeCommit": {"oid": self.MERGE},
            }
            with (
                mock.patch.object(maintenance.agent_core, "pr_details", return_value=details),
                mock.patch.object(maintenance.lifecycle, "default_branch", return_value="main"),
                mock.patch.object(
                    maintenance.agent_core,
                    "canonical_repository",
                    return_value="example/repo",
                ),
                self.assertRaisesRegex(maintenance.MaintenanceError, "title"),
            ):
                maintenance._merged_pr(
                    root,
                    record,
                    "example/repo",
                    22,
                    self.HEAD,
                    "21: canonical title",
                    "canonical body",
                )

    def test_merged_pr_uses_terminal_lf_helper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._record(root)
            details = {
                "number": 22, "title": "21: canonical title", "body": "canonical body",
                "headRefName": record.branch, "baseRefName": "main", "headRefOid": self.HEAD,
                "isCrossRepository": False, "state": "MERGED", "mergeCommit": {"oid": self.MERGE},
            }
            with (
                mock.patch.object(maintenance.agent_core, "pr_details", return_value=details),
                mock.patch.object(maintenance.lifecycle, "default_branch", return_value="main"),
                mock.patch.object(maintenance.agent_core, "canonical_repository", return_value="example/repo"),
                mock.patch.object(
                    maintenance.publication,
                    "canonical_pr_body_matches",
                    wraps=maintenance.publication.canonical_pr_body_matches,
                ) as matches,
            ):
                maintenance._merged_pr(
                    root, record, "example/repo", 22, self.HEAD,
                    "21: canonical title", "canonical body\n",
                )
            matches.assert_called_once_with("canonical body\n", "canonical body")

    def test_dedicated_terminal_transition_does_not_change_normal_transition_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._state(root, "initialized")
            record = self._record(root)
            with (
                mock.patch.object(maintenance.lifecycle, "require_resolved_contract"),
                mock.patch.object(maintenance.lifecycle, "assert_task_identity"),
                mock.patch.object(maintenance.lifecycle, "work_units_lock", return_value=nullcontext()),
                mock.patch.object(maintenance, "_terminal_ref_locks", return_value=nullcontext()),
            ):
                result = maintenance._mark_maintenance_merged(record, "21")
            self.assertEqual(result, "finalized")
            self.assertEqual(
                maintenance.lifecycle.state_status(root / ".task-state" / "task.md"),
                "merged",
            )
            self.assertNotIn("merged", maintenance.lifecycle.LINEAR_TRANSITIONS["initialized"])

    def test_finalize_publication_is_byte_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._state(root)
            record = self._record(root)
            line = (
                f"PR #22 merged from {self.HEAD}; merge commit {self.MERGE}; "
                "finalization finalized"
            )
            with (
                mock.patch.object(maintenance.lifecycle, "require_resolved_contract"),
                mock.patch.object(maintenance.lifecycle, "assert_task_identity"),
                mock.patch.object(maintenance.lifecycle, "work_units_lock", return_value=nullcontext()),
                mock.patch.object(maintenance, "_terminal_ref_locks", return_value=nullcontext()),
            ):
                self.assertEqual(
                    maintenance._mark_maintenance_merged(
                        record, "21", publication_line=line
                    ),
                    "finalized",
                )
                path = root / ".task-state" / "task.md"
                first = path.read_bytes()
                self.assertEqual(
                    maintenance._mark_maintenance_merged(
                        record, "21", publication_line=line
                    ),
                    "already-finalized",
                )
                self.assertEqual(path.read_bytes(), first)
            self.assertEqual(first.count(b"### Maintenance publication"), 1)
            self.assertEqual(first.count(("- " + line).encode("utf-8")), 1)

    def test_finalize_publication_coexists_with_canonical_evidence_subsections(self) -> None:
        template = (
            ROOT / "components" / "agent-core" / ".automation" / "templates" / "task-state.md"
        ).read_text(encoding="utf-8")
        line = (
            f"PR #22 merged from {self.HEAD}; merge commit {self.MERGE}; "
            "finalization finalized"
        )
        updated = maintenance._add_finalized_publication(template, line)
        maintenance._validate_finalized_publication(updated, line)
        self.assertIn("### Changed files\n\nNone yet.", updated)
        self.assertEqual(updated.count("### Maintenance publication"), 1)

    def test_finalize_rejects_duplicate_or_conflicting_publication_evidence(self) -> None:
        line = f"PR #22 merged from {self.HEAD}; merge commit {self.MERGE}"
        for evidence in (
            f"### Maintenance publication\n\n- {line}\n- {line}\n",
            "### Maintenance publication\n\n- a different publication\n",
        ):
            with self.subTest(evidence=evidence):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self._state(root)
                    path = root / ".task-state" / "task.md"
                    path.write_text(path.read_text(encoding="utf-8") + evidence, encoding="utf-8")
                    record = self._record(root)
                    with (
                        mock.patch.object(maintenance.lifecycle, "require_resolved_contract"),
                        mock.patch.object(maintenance.lifecycle, "assert_task_identity"),
                        mock.patch.object(maintenance.lifecycle, "work_units_lock", return_value=nullcontext()),
                        mock.patch.object(maintenance, "_terminal_ref_locks", return_value=nullcontext()),
                    ):
                        with self.assertRaisesRegex(maintenance.MaintenanceError, "publication evidence"):
                            maintenance._mark_maintenance_merged(
                                record, "21", publication_line=line
                            )
                    self.assertEqual(maintenance.lifecycle.state_status(path), "initialized")

    def test_terminal_transition_revalidates_before_writing_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._state(root)
            record = self._record(root)

            def moved() -> None:
                raise maintenance.MaintenanceError(
                    "maintenance receipt changed before terminal transition"
                )

            with (
                mock.patch.object(maintenance.lifecycle, "require_resolved_contract"),
                mock.patch.object(maintenance.lifecycle, "assert_task_identity"),
                mock.patch.object(
                    maintenance.lifecycle, "work_units_lock", return_value=nullcontext()
                ),
                mock.patch.object(maintenance, "_terminal_ref_locks", return_value=nullcontext()),
                self.assertRaisesRegex(
                    maintenance.MaintenanceError, "receipt changed"
                ),
            ):
                maintenance._mark_maintenance_merged(
                    record,
                    "21",
                    publication_line="exact publication",
                    validate_before_write=moved,
                )
            self.assertEqual(
                maintenance.lifecycle.state_status(
                    root / ".task-state" / "task.md"
                ),
                "initialized",
            )

    def test_terminal_ref_lock_blocks_concurrent_task_branch_movement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            self._write(root, "tracked.txt", "base\n")
            base = self._commit(root, "base")
            self._git(root, "checkout", "-b", "task/21-maintenance")
            self._write(root, "tracked.txt", "maintenance\n")
            head = self._commit(root, "maintenance")
            record = maintenance.lifecycle.WorktreeRecord(
                root, "task/21-maintenance", head
            )
            with maintenance._terminal_ref_locks(
                record, default_branch="main", default_revision=base
            ):
                for branch, old, new in (
                    ("task/21-maintenance", head, base),
                    ("main", base, head),
                ):
                    attempted = subprocess.run(
                        ("git", "update-ref", f"refs/heads/{branch}", new, old),
                        cwd=root,
                        text=True,
                        capture_output=True,
                    )
                    self.assertNotEqual(attempted.returncode, 0)
                self.assertEqual(self._git(root, "rev-parse", "HEAD"), head)
            self._git(
                root,
                "update-ref",
                "refs/heads/task/21-maintenance",
                base,
                head,
            )
            self.assertEqual(self._git(root, "rev-parse", "HEAD"), base)

    def test_terminal_ref_lock_supports_packed_nested_task_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            self._write(root, "tracked.txt", "base\n")
            base = self._commit(root, "base")
            self._git(root, "checkout", "-b", "task/21-maintenance")
            head = self._git(root, "rev-parse", "HEAD")
            self._git(root, "pack-refs", "--all", "--prune")
            nested = Path(self._git(root, "rev-parse", "--git-common-dir")) / "refs/heads/task"
            if not nested.is_absolute():
                nested = root / nested
            self.assertFalse(nested.exists())
            record = maintenance.lifecycle.WorktreeRecord(
                root, "task/21-maintenance", head
            )
            with maintenance._terminal_ref_locks(
                record, default_branch="main", default_revision=base
            ):
                self.assertTrue(nested.is_dir())

    def test_terminal_ref_lock_rejects_symlinked_ref_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory)
            common = top / "git"
            outside = top / "outside"
            (common / "refs").mkdir(parents=True)
            outside.mkdir()
            (common / "refs" / "heads").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(
                maintenance.MaintenanceError, "ref directory is unsafe"
            ):
                maintenance._safe_ref_parent(common, "task/21-maintenance")
            self.assertEqual(list(outside.iterdir()), [])

    def test_permission_and_command_surfaces_are_explicit(self) -> None:
        config = json.loads(
            (ROOT / "components" / "agent-core" / "opencode.json").read_text(encoding="utf-8")
        )
        bash = config["permission"]["bash"]
        self.assertEqual(bash["just automation::maintenance-check *"], "allow")
        self.assertEqual(
            bash["just automation::maintenance-contract-refresh-inspect *"], "allow"
        )
        self.assertEqual(
            bash["just automation::maintenance-contract-refresh *"], "deny"
        )
        self.assertEqual(bash["just automation::maintenance-review-record *"], "deny")
        self.assertEqual(bash["just automation::maintenance-pr-create *"], "deny")
        self.assertEqual(bash["just automation::maintenance-finalize *"], "deny")
        self.assertEqual(bash["just automation::upgrade *"], "ask")
        self.assertEqual(bash["just agent::push *"], "ask")
        self.assertEqual(bash["git push *"], "deny")
        build = (
            ROOT / "components" / "agent-core" / ".opencode" / "agents" / "build.md"
        ).read_text(encoding="utf-8")
        self.assertIn('"just automation::maintenance-review-record *": allow', build)
        self.assertIn('"just automation::maintenance-contract-refresh *": allow', build)
        self.assertIn('"just automation::maintenance-pr-create *": deny', build)
        self.assertIn('"just automation::maintenance-finalize *": allow', build)
        command = (
            ROOT
            / "components"
            / "agent-core"
            / ".opencode"
            / "commands"
            / "maintenance-run.md"
        ).read_text(encoding="utf-8")
        self.assertIn("maintenance-orchestration", command)
        self.assertIn("maintenance-check", command)
        agent = (
            ROOT
            / "components"
            / "agent-core"
            / ".opencode"
            / "agents"
            / "maintenance-orchestrator.md"
        ).read_text(encoding="utf-8")
        self.assertIn("model: openai/gpt-5.6-sol", agent)
        self.assertIn('"just automation::maintenance-review-record *": deny', agent)
        self.assertIn('"just automation::maintenance-contract-refresh *": deny', agent)
        self.assertIn('"just automation::maintenance-pr-create *": allow', agent)
        self.assertIn('"just automation::maintenance-finalize *": deny', agent)
        self.assertNotIn("reviewer: allow", agent)
        self.assertNotIn("security-reviewer: allow", agent)
        self.assertNotIn('"just agent::work-unit-state-set *": allow', agent)

    def test_generated_maintenance_surface_matches_canonical(self) -> None:
        canonical = ROOT / "components" / "agent-core"
        for template in (
            "agent-base",
            "agent-python",
            "agent-rust",
            "agent-nix",
            "agent-cpp-cmake",
        ):
            generated = ROOT / "templates" / template
            for relative in (
                ".automation/bin/maintenance_lifecycle.py",
                ".automation/just/automation.just",
                ".opencode/agents/build.md",
                ".opencode/agents/maintenance-orchestrator.md",
                ".opencode/commands/maintenance-run.md",
                ".opencode/skills/maintenance-orchestration/SKILL.md",
                "opencode.json",
            ):
                self.assertEqual(
                    (generated / relative).read_bytes(),
                    (canonical / relative).read_bytes(),
                    f"{template}: {relative}",
                )


if __name__ == "__main__":
    unittest.main()
