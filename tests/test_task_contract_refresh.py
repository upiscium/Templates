from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "components" / "agent-core" / ".automation" / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import maintenance_lifecycle as maintenance
import task_contract as contract


BRIDGE_SPEC = importlib.util.spec_from_file_location(
    "task_contract_refresh_bridge_test", ROOT / "tools" / "automation_recovery_bridge.py"
)
assert BRIDGE_SPEC and BRIDGE_SPEC.loader
bridge = importlib.util.module_from_spec(BRIDGE_SPEC)
sys.modules[BRIDGE_SPEC.name] = bridge
BRIDGE_SPEC.loader.exec_module(bridge)


TASK = "19"
ISSUE = 19
REPOSITORY = "acme/widgets"
BRANCH = "task/19-contract-refresh"
HEAD = "a" * 40


def response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


class TaskContractRefreshTest(unittest.TestCase):
    def _git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=root, check=True, text=True, capture_output=True
        ).stdout.strip()

    def _issue_payload(
        self,
        *,
        title: str = "Contract refresh source",
        body: str = "The authoritative contract body.",
        state: str = "open",
        repository: str = REPOSITORY,
    ) -> dict:
        return {
            "number": ISSUE,
            "repository_url": f"https://api.github.com/repos/{repository}",
            "html_url": f"https://github.com/{repository}/issues/{ISSUE}",
            "title": title,
            "body": body,
            "state": state,
            "labels": [{"name": "maintenance"}],
            "assignees": [],
            "milestone": None,
            "repository": {"full_name": repository},
        }

    def _runner(self, payload: dict):
        return lambda _command, **_kwargs: response(payload)

    def _new_payload(self) -> dict:
        return self._issue_payload(
            title="Contract refresh source (updated)",
            body="The authoritative contract body after the intentional refresh.",
        )

    def _write_pristine_fixture(self, root: Path) -> tuple[contract.lifecycle.WorktreeRecord, str, dict]:
        root.mkdir(parents=True, exist_ok=True)
        template = (
            ROOT / "components" / "agent-core" / ".automation" / "templates" / "task-state.md"
        ).read_text(encoding="utf-8")
        (root / ".automation" / "templates").mkdir(parents=True)
        (root / ".automation" / "templates" / "task-state.md").write_text(
            template, encoding="utf-8"
        )
        state_dir = root / ".task-state"
        state_dir.mkdir()
        state = template
        for marker, value in {
            "@@TASK_ID@@": TASK,
            "@@BRANCH@@": BRANCH,
            "@@WORKTREE@@": str(root.resolve()),
            "@@BASE_BRANCH@@": "main",
            "@@BASE_REVISION@@": HEAD,
        }.items():
            state = state.replace(marker, value)
        (state_dir / "task.md").write_text(state, encoding="utf-8")
        record = contract.lifecycle.WorktreeRecord(root.resolve(), BRANCH, HEAD)
        payload = self._issue_payload()

        def git(*args: str, **_kwargs: object) -> str:
            if args[0] == "status":
                return ""
            if args[0] == "rev-parse":
                return HEAD
            raise AssertionError(args)

        with (
            mock.patch.object(contract.lifecycle, "require_local_task", return_value=record),
            mock.patch.object(contract.lifecycle, "state_status", return_value="initialized"),
            mock.patch.object(contract.lifecycle, "read_work_units", return_value={"units": {}}),
            mock.patch.object(contract.lifecycle, "git", side_effect=git),
        ):
            contract.hydrate_task_contract(root, TASK, TASK, payload, REPOSITORY)
        return record, contract._digest(contract.authoritative_payload(payload, ISSUE, REPOSITORY)), payload

    @contextmanager
    def _local_environment(
        self,
        record: contract.lifecycle.WorktreeRecord,
        *,
        status: str = "initialized",
        units: dict | None = None,
    ):
        lifecycle = contract.lifecycle
        with (
            mock.patch.object(lifecycle, "require_local_task", return_value=record),
            mock.patch.object(lifecycle, "current_worktree", return_value=record),
            mock.patch.object(
                maintenance.lifecycle, "current_worktree", return_value=record
            ),
            mock.patch.object(
                maintenance.lifecycle, "worktree_for_task", return_value=record
            ),
            mock.patch.object(
                maintenance.upgrade.private_state,
                "mutation_lock",
                return_value=nullcontext(),
            ),
            mock.patch.object(lifecycle, "main_worktree", return_value=record),
            mock.patch.object(lifecycle, "worktree_for_task", return_value=record),
            mock.patch.object(lifecycle, "state_status", return_value=status),
            mock.patch.object(
                lifecycle,
                "read_work_units",
                return_value={"units": {} if units is None else units},
            ),
        ):
            yield

    def _local_contract(self, root: Path, record: contract.lifecycle.WorktreeRecord) -> dict:
        with self._local_environment(record):
            return contract.validate_contract(root, TASK)

    def _state_bytes(self, root: Path) -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in (root / ".task-state").iterdir()
            if path.is_file()
        }

    def test_pristine_refresh_passes_and_local_contract_is_updated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, old_digest, _ = self._write_pristine_fixture(root)
            new_payload = self._new_payload()
            new_digest = contract._digest(
                contract.authoritative_payload(new_payload, ISSUE, REPOSITORY)
            )
            local = self._local_contract(root, record)
            protected = {"head": HEAD, "status": "initialized", "evidence": b"none"}
            active = root / ".task-state" / "active-receipt.json"
            consumed = root / ".task-state" / "consumed-receipt.json"
            with (
                self._local_environment(record),
                mock.patch.object(contract, "repository_identity", return_value=REPOSITORY),
                mock.patch.object(maintenance, "_stored_contract", return_value=(record, local)),
                mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                mock.patch.object(
                    maintenance.upgrade, "consumed_receipt_path", return_value=consumed
                ),
                mock.patch.object(
                    maintenance, "_refresh_protected_snapshot", return_value=protected
                ),
            ):
                result = maintenance.maintenance_contract_refresh(
                    root,
                    TASK,
                    old_digest,
                    new_digest,
                    runner=self._runner(new_payload),
                )
            self.assertEqual(result["status"], "REFRESHED")
            self.assertEqual(result["mode"], "maintenance")
            self.assertEqual(result["stage"], "pristine")
            with self._local_environment(record):
                validated = contract.validate_contract(root, TASK)
            self.assertEqual(validated["sha256"], new_digest)

    def test_applied_refresh_passes_with_a_stable_eligibility_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, old_digest, _ = self._write_pristine_fixture(root)
            new_payload = self._new_payload()
            new_digest = contract._digest(
                contract.authoritative_payload(new_payload, ISSUE, REPOSITORY)
            )
            local = self._local_contract(root, record)
            receipt = {"status": "active", "task_id": TASK}
            protected = {"head": HEAD, "receipt": b"active", "authority": b"authority"}
            with (
                self._local_environment(record),
                mock.patch.object(contract, "repository_identity", return_value=REPOSITORY),
                mock.patch.object(
                    maintenance,
                    "_refresh_eligibility",
                    return_value=(record, local, "applied", receipt, protected),
                ),
                mock.patch.object(
                    maintenance, "_refresh_protected_snapshot", return_value=protected
                ),
            ):
                result = maintenance.maintenance_contract_refresh(
                    root,
                    TASK,
                    old_digest,
                    new_digest,
                    runner=self._runner(new_payload),
                )
            self.assertEqual(result["status"], "REFRESHED")
            self.assertEqual(result["stage"], "applied")
            with self._local_environment(record):
                self.assertEqual(contract.validate_contract(root, TASK)["sha256"], new_digest)

    def test_applied_refresh_is_fenced_by_the_maintenance_mutation_lock(self) -> None:
        root = Path("/tmp/task-contract-refresh-lock").resolve()
        record = contract.lifecycle.WorktreeRecord(root, BRANCH, HEAD)
        held = False

        @contextmanager
        def mutation_lock(*_args, **_kwargs):
            nonlocal held
            held = True
            try:
                yield
            finally:
                held = False

        def eligibility(*_args):
            self.assertTrue(held)
            return record, {"sha256": "a" * 64}, "applied", {"status": "active"}, {"head": HEAD}

        def refreshed(*_args, **_kwargs):
            self.assertTrue(held)
            return {"status": "REFRESHED"}

        with (
            mock.patch.object(maintenance.lifecycle, "worktree_for_task", return_value=record),
            mock.patch.object(maintenance.lifecycle, "current_worktree", return_value=record),
            mock.patch.object(
                maintenance.upgrade.private_state,
                "mutation_lock",
                side_effect=mutation_lock,
            ),
            mock.patch.object(maintenance, "_refresh_eligibility", side_effect=eligibility),
            mock.patch.object(
                maintenance.task_contract,
                "refresh_task_contract",
                side_effect=refreshed,
            ),
        ):
            result = maintenance.maintenance_contract_refresh(
                root, TASK, "a" * 64, "b" * 64
            )
        self.assertFalse(held)
        self.assertEqual(result["stage"], "applied")

    def test_applied_snapshot_preserves_a_valid_legacy_authority_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, _, _ = self._write_pristine_fixture(root)
            active = root / ".task-state" / "automation-maintenance.json"
            active.write_bytes(b"active\n")
            consumed = root / ".task-state" / "automation-maintenance.consumed.json"
            canonical = root / "git-private" / "canonical" / "authority.json"
            legacy = root / "git-private" / "legacy" / "authority.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"legacy authority\n")
            receipt = {"authority_head": HEAD}

            def git_value(_record, *arguments):
                if arguments[0] == "show-ref":
                    return f"{HEAD} refs/heads/{BRANCH}\n"
                if arguments[0] == "diff" or arguments[0] == "status":
                    return ""
                return HEAD + "\n"

            with (
                mock.patch.object(maintenance.lifecycle, "worktree_for_task", return_value=record),
                mock.patch.object(maintenance.lifecycle, "state_status", return_value="initialized"),
                mock.patch.object(maintenance.lifecycle, "read_work_units", return_value={"units": {}}),
                mock.patch.object(maintenance.lifecycle, "default_branch", return_value="main"),
                mock.patch.object(maintenance.upgrade, "receipt_path", return_value=active),
                mock.patch.object(maintenance.upgrade, "consumed_receipt_path", return_value=consumed),
                mock.patch.object(
                    maintenance.upgrade,
                    "_authority_locations",
                    return_value=(canonical, legacy),
                ),
                mock.patch.object(
                    maintenance.upgrade,
                    "source_recovery_proof_path",
                    return_value=canonical.parent / "source-recovery-proof.json",
                ),
                mock.patch.object(maintenance, "_validate_active_receipt", return_value=receipt),
                mock.patch.object(maintenance.upgrade, "receipt_paths", return_value=[]),
                mock.patch.object(maintenance.upgrade, "pending_paths", return_value=[]),
                mock.patch.object(maintenance.upgrade, "validate_authority", return_value=legacy),
                mock.patch.object(maintenance, "_git_refresh_value", side_effect=git_value),
                mock.patch.object(maintenance, "_refresh_index_bytes", return_value=b"index"),
            ):
                snapshot = maintenance._refresh_protected_snapshot(
                    record, TASK, "applied", receipt
                )
            self.assertEqual(snapshot["authorities"][str(legacy)], b"legacy authority\n")
            self.assertIsNone(snapshot["authorities"][str(canonical)])

    def test_refresh_inspection_is_byte_for_byte_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, old_digest, _ = self._write_pristine_fixture(root)
            new_payload = self._new_payload()
            new_digest = contract._digest(
                contract.authoritative_payload(new_payload, ISSUE, REPOSITORY)
            )
            local = self._local_contract(root, record)
            protected = {"head": HEAD, "evidence": b"none"}
            before = self._state_bytes(root)
            with (
                self._local_environment(record),
                mock.patch.object(contract, "repository_identity", return_value=REPOSITORY),
                mock.patch.object(
                    maintenance,
                    "_refresh_eligibility",
                    return_value=(record, local, "pristine", None, protected),
                ),
                mock.patch.object(
                    maintenance, "_refresh_protected_snapshot", return_value=protected
                ),
            ):
                result = maintenance.maintenance_contract_refresh_inspect(
                    root,
                    TASK,
                    old_digest,
                    new_digest,
                    runner=self._runner(new_payload),
                )
            self.assertEqual(result["status"], "REFRESH_AVAILABLE")
            self.assertEqual(result["stage"], "pristine")
            self.assertEqual(before, self._state_bytes(root))

    def test_refresh_rejects_wrong_old_and_new_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, old_digest, _ = self._write_pristine_fixture(root)
            new_payload = self._new_payload()
            new_digest = contract._digest(
                contract.authoritative_payload(new_payload, ISSUE, REPOSITORY)
            )
            cases = (
                ("e" * 64, new_digest, "expected old digest"),
                (old_digest, "f" * 64, "expected new digest"),
            )
            for old, new, message in cases:
                with self.subTest(message=message):
                    with (
                        self._local_environment(record),
                        mock.patch.object(
                            contract, "repository_identity", return_value=REPOSITORY
                        ),
                        self.assertRaisesRegex(contract.ContractError, message),
                    ):
                        contract.inspect_task_contract_refresh(
                            root,
                            TASK,
                            old,
                            new,
                            runner=self._runner(new_payload),
                        )

    def test_same_digest_refresh_is_a_byte_identical_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, digest, payload = self._write_pristine_fixture(root)
            before = self._state_bytes(root)
            with (
                self._local_environment(record),
                mock.patch.object(contract, "repository_identity", return_value=REPOSITORY),
            ):
                result = contract.refresh_task_contract(
                    root,
                    TASK,
                    digest,
                    digest,
                    runner=self._runner(payload),
                )
            self.assertEqual(result["status"], "NO_CHANGE")
            self.assertEqual(result["oldSha256"], digest)
            self.assertEqual(result["newSha256"], digest)
            self.assertEqual(before, self._state_bytes(root))

    def test_same_digest_no_op_preserves_accepted_section_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, digest, payload = self._write_pristine_fixture(root)
            state = root / ".task-state" / "task.md"
            state.write_text(
                state.read_text(encoding="utf-8").replace(
                    "#body\n\n## Scope", "#body   \n\n## Scope"
                ),
                encoding="utf-8",
            )
            before = self._state_bytes(root)
            with (
                self._local_environment(record),
                mock.patch.object(contract, "repository_identity", return_value=REPOSITORY),
            ):
                result = contract.refresh_task_contract(
                    root, TASK, digest, digest, runner=self._runner(payload)
                )
            self.assertEqual(result["status"], "NO_CHANGE")
            self.assertEqual(before, self._state_bytes(root))

    def test_live_issue_race_fails_and_restores_original_contract_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, old_digest, _ = self._write_pristine_fixture(root)
            first = self._new_payload()
            raced = self._issue_payload(
                title="Contract refresh source (raced)",
                body="The authoritative Issue changed during the refresh.",
            )
            new_digest = contract._digest(
                contract.authoritative_payload(first, ISSUE, REPOSITORY)
            )
            payloads = iter((first, raced))
            before = self._state_bytes(root)
            with (
                self._local_environment(record),
                mock.patch.object(contract, "repository_identity", return_value=REPOSITORY),
                self.assertRaisesRegex(
                    contract.ContractError, "authoritative Issue changed before refresh mutation"
                ),
            ):
                contract.refresh_task_contract(
                    root,
                    TASK,
                    old_digest,
                    new_digest,
                    runner=lambda _command, **_kwargs: response(next(payloads)),
                )
            self.assertEqual(before, self._state_bytes(root))

    def test_closed_and_wrong_repository_issues_fail_without_network_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, old_digest, _ = self._write_pristine_fixture(root)
            before = self._state_bytes(root)
            cases = (
                (self._issue_payload(state="closed"), "Issue must be open"),
                (
                    self._issue_payload(repository="other/widgets"),
                    "Issue repository identity mismatch",
                ),
            )
            for payload, message in cases:
                with self.subTest(message=message):
                    with (
                        self._local_environment(record),
                        mock.patch.object(
                            contract, "repository_identity", return_value=REPOSITORY
                        ),
                        self.assertRaisesRegex(contract.ContractError, message),
                    ):
                        contract.inspect_task_contract_refresh(
                            root,
                            TASK,
                            old_digest,
                            "b" * 64,
                            runner=self._runner(payload),
                        )
                    self.assertEqual(before, self._state_bytes(root))

    def test_refresh_eligibility_rejects_work_units_evidence_and_consumed_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, _, _ = self._write_pristine_fixture(root)
            local = {
                "status": "READY",
                "task": TASK,
                "worktree": str(root),
                "issue": ISSUE,
                "repository": REPOSITORY,
                "sha256": "a" * 64,
            }
            state_dir = root / ".task-state"
            active = state_dir / "active.json"
            consumed = state_dir / "consumed.json"
            cases = (
                (
                    "work unit",
                    {"WU-19-01": {"state": "completed"}},
                    None,
                    "Task Contract refresh requires no Work Units",
                ),
                (
                    "evidence",
                    {},
                    "verification.json",
                    "Task Contract refresh requires no verification or publication evidence",
                ),
                (
                    "consumed receipt",
                    {},
                    "consumed-receipt",
                    "Task Contract refresh rejects committed-or-later maintenance",
                ),
            )
            for name, units, artifact, message in cases:
                with self.subTest(name=name):
                    for path in (active, consumed, state_dir / "verification.json"):
                        path.unlink(missing_ok=True)
                    if artifact == "verification.json":
                        (state_dir / artifact).write_bytes(b"evidence")
                    elif artifact == "consumed-receipt":
                        consumed.write_bytes(b"consumed")
                    with (
                        mock.patch.object(
                            maintenance, "_stored_contract", return_value=(record, local)
                        ),
                        mock.patch.object(
                            maintenance.lifecycle, "worktree_for_task", return_value=record
                        ),
                        mock.patch.object(
                            maintenance.lifecycle, "state_status", return_value="initialized"
                        ),
                        mock.patch.object(
                            maintenance.lifecycle,
                            "read_work_units",
                            return_value={"units": units},
                        ),
                        mock.patch.object(
                            maintenance.upgrade, "receipt_path", return_value=active
                        ),
                        mock.patch.object(
                            maintenance.upgrade,
                            "consumed_receipt_path",
                            return_value=consumed,
                        ),
                        self.assertRaisesRegex(maintenance.MaintenanceError, message),
                    ):
                        maintenance._refresh_eligibility(root, TASK)

    def test_real_pristine_snapshot_rejects_unrelated_tracked_dirty_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, _, _ = self._write_pristine_fixture(root)
            (root / ".gitignore").write_text(".task-state/\n", encoding="utf-8")
            (root / "product.txt").write_text("clean\n", encoding="utf-8")
            self._git(root, "init", "-b", "main")
            self._git(root, "config", "user.name", "Refresh Test")
            self._git(root, "config", "user.email", "refresh@example.invalid")
            self._git(root, "remote", "add", "origin", "https://github.com/acme/widgets.git")
            self._git(root, "add", ".")
            self._git(root, "commit", "-m", "base")
            base = self._git(root, "rev-parse", "HEAD")
            self._git(root, "update-ref", "refs/remotes/origin/main", base)
            self._git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
            self._git(root, "checkout", "-b", BRANCH)
            state = root / ".task-state" / "task.md"
            state.write_text(
                state.read_text(encoding="utf-8").replace(
                    f"- Base revision: {HEAD}", f"- Base revision: {base}"
                ),
                encoding="utf-8",
            )
            live_record = contract.lifecycle.WorktreeRecord(root, BRANCH, base)
            first = maintenance._refresh_protected_snapshot(
                live_record, TASK, "pristine", None
            )
            second = maintenance._refresh_protected_snapshot(
                live_record, TASK, "pristine", None
            )
            self.assertEqual(first, second)
            self.assertEqual(first["head"], base)
            self.assertEqual(first["branch_head"], base)
            self.assertEqual(first["status_porcelain"], "")
            (root / "product.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(
                maintenance.MaintenanceError, "clean Task Base content"
            ):
                maintenance._refresh_protected_snapshot(
                    live_record, TASK, "pristine", None
                )

    def test_protected_snapshot_drift_rolls_back_the_contract_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, old_digest, _ = self._write_pristine_fixture(root)
            new_payload = self._new_payload()
            new_digest = contract._digest(
                contract.authoritative_payload(new_payload, ISSUE, REPOSITORY)
            )
            local = self._local_contract(root, record)
            before_bytes = self._state_bytes(root)
            protected = {"head": HEAD, "receipt": b"same"}
            drifted = {"head": HEAD, "receipt": b"changed"}
            with (
                self._local_environment(record),
                mock.patch.object(contract, "repository_identity", return_value=REPOSITORY),
                mock.patch.object(
                    maintenance,
                    "_refresh_eligibility",
                    return_value=(record, local, "pristine", None, protected),
                ),
                mock.patch.object(
                    maintenance,
                    "_refresh_protected_snapshot",
                    side_effect=(protected, drifted),
                ),
                self.assertRaisesRegex(
                    maintenance.MaintenanceError,
                    "protected maintenance state changed",
                ),
            ):
                maintenance.maintenance_contract_refresh(
                    root,
                    TASK,
                    old_digest,
                    new_digest,
                    runner=self._runner(new_payload),
                )
            self.assertEqual(before_bytes, self._state_bytes(root))

    def test_parser_and_source_dispatch_require_the_expected_implementation_revision(self) -> None:
        expected = "c" * 40
        args = bridge.parser().parse_args(
            [
                "maintenance-contract-refresh-inspect",
                "/tmp/task",
                TASK,
                "a" * 64,
                "b" * 64,
                expected,
            ]
        )
        self.assertEqual(args.expected_implementation_revision, expected)
        with self.assertRaises(bridge.BridgeError):
            bridge.parser().parse_args(
                [
                    "maintenance-contract-refresh-inspect",
                    "/tmp/task",
                    TASK,
                    "a" * 64,
                    "b" * 64,
                    "not-a-revision",
                ]
            )

        with (
            mock.patch.object(
                bridge,
                "_clean_root",
                side_effect=bridge.BridgeError("Templates HEAD changed during bootstrap"),
            ),
            mock.patch.object(bridge, "_verify_bootstrap") as verify,
            mock.patch.object(bridge, "_verified_modules") as modules,
            mock.patch.object(
                sys,
                "argv",
                [
                    "bridge",
                    "maintenance-contract-refresh-inspect",
                    "/tmp/task",
                    TASK,
                    "a" * 64,
                    "b" * 64,
                    expected,
                ],
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            self.assertEqual(bridge.main(), 2)
        verify.assert_not_called()
        modules.assert_not_called()

    def test_source_dispatch_calls_the_verified_maintenance_module_and_reports_revision(self) -> None:
        revision = "d" * 40
        target = Path("/tmp/task-contract-refresh").resolve()
        verified_maintenance = mock.Mock()
        verified_maintenance.maintenance_contract_refresh_inspect.return_value = {
            "status": "REFRESH_AVAILABLE",
            "stage": "pristine",
        }
        with (
            mock.patch.object(bridge, "_clean_root", return_value=revision),
            mock.patch.object(bridge, "_verify_bootstrap"),
            mock.patch.object(bridge, "_validate_target_git_configuration"),
            mock.patch.object(bridge, "_verified_modules") as modules,
            mock.patch.object(bridge, "maintenance_environment", return_value=nullcontext()),
            mock.patch.object(
                sys,
                "argv",
                [
                    "bridge",
                    "maintenance-contract-refresh-inspect",
                    str(target),
                    TASK,
                    "a" * 64,
                    "b" * 64,
                    revision,
                ],
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            modules.return_value.__enter__.return_value = {
                "maintenance_lifecycle": verified_maintenance
            }
            self.assertEqual(bridge.main(), 0)
        modules.assert_called_once_with(bridge.ROOT, revision)
        verified_maintenance.maintenance_contract_refresh_inspect.assert_called_once_with(
            target,
            TASK,
            "a" * 64,
            "b" * 64,
            runner=bridge.trusted_gh_run,
        )
        self.assertEqual(json.loads(output.getvalue())["implementationRevision"], revision)

    def test_source_mutation_dispatch_revalidates_the_source_inside_refresh(self) -> None:
        revision = "d" * 40
        target = Path("/tmp/task-contract-refresh").resolve()
        verified_maintenance = mock.Mock()

        def refresh(*_args, validate_external=None, **_kwargs):
            self.assertIsNotNone(validate_external)
            validate_external()
            return {"status": "REFRESHED"}

        verified_maintenance.maintenance_contract_refresh.side_effect = refresh
        with (
            mock.patch.object(bridge, "_clean_root", return_value=revision) as clean,
            mock.patch.object(bridge, "_verify_bootstrap"),
            mock.patch.object(bridge, "_validate_target_git_configuration"),
            mock.patch.object(bridge, "_verified_modules") as modules,
            mock.patch.object(bridge, "maintenance_environment", return_value=nullcontext()),
            mock.patch.object(
                sys,
                "argv",
                [
                    "bridge", "maintenance-contract-refresh", str(target), TASK,
                    "a" * 64, "b" * 64, revision,
                ],
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            modules.return_value.__enter__.return_value = {
                "maintenance_lifecycle": verified_maintenance
            }
            self.assertEqual(bridge.main(), 0)
        self.assertEqual(clean.call_count, 4)

    def test_refresh_rejects_a_main_or_sibling_worktree_target(self) -> None:
        record = contract.lifecycle.WorktreeRecord(Path("/tmp/task").resolve(), BRANCH, HEAD)
        other = contract.lifecycle.WorktreeRecord(Path("/tmp/main").resolve(), "main", HEAD)
        local = {"sha256": "a" * 64}
        with (
            mock.patch.object(
                maintenance,
                "_refresh_eligibility",
                return_value=(record, local, "pristine", None, {"head": HEAD}),
            ),
            mock.patch.object(
                maintenance.lifecycle, "worktree_for_task", return_value=record
            ),
            mock.patch.object(maintenance.lifecycle, "current_worktree", return_value=other),
            self.assertRaisesRegex(maintenance.MaintenanceError, "exact registered Task worktree"),
        ):
            maintenance.maintenance_contract_refresh(
                other.path, TASK, "a" * 64, "b" * 64
            )

    def test_public_just_recipe_exposes_the_contract_refresh_surface(self) -> None:
        text = (ROOT / "just" / "agent-core.just").read_text(encoding="utf-8")
        self.assertIn(
            "task-contract-refresh-inspect target task expected_old_digest expected_new_digest expected_implementation_revision:",
            text,
        )
        self.assertIn(
            "task-contract-refresh target task expected_old_digest expected_new_digest expected_implementation_revision:",
            text,
        )
        self.assertIn("maintenance-contract-refresh-inspect", text)
        self.assertIn("maintenance-contract-refresh", text)
        self.assertIn("{{quote(expected_implementation_revision)}}", text)

    def test_stale_maintenance_check_fails_before_refresh_and_local_validation_passes_after(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record, old_digest, _ = self._write_pristine_fixture(root)
            new_payload = self._new_payload()
            new_digest = contract._digest(
                contract.authoritative_payload(new_payload, ISSUE, REPOSITORY)
            )
            local = self._local_contract(root, record)
            protected = {"head": HEAD, "evidence": b"none"}
            with (
                self._local_environment(record),
                mock.patch.object(contract, "repository_identity", return_value=REPOSITORY),
                mock.patch.object(
                    maintenance, "_stored_contract", return_value=(record, local)
                ),
                mock.patch.object(
                    maintenance.task_contract,
                    "_validate_authoritative_issue",
                    side_effect=contract.ContractError(
                        "canonical Issue snapshot no longer matches its authoritative Issue"
                    ),
                ),
                self.assertRaisesRegex(
                    maintenance.MaintenanceError,
                    "canonical Issue snapshot no longer matches",
                ),
            ):
                maintenance.maintenance_check(root, TASK)

            with (
                self._local_environment(record),
                mock.patch.object(contract, "repository_identity", return_value=REPOSITORY),
                mock.patch.object(
                    maintenance,
                    "_refresh_eligibility",
                    return_value=(record, local, "pristine", None, protected),
                ),
                mock.patch.object(
                    maintenance, "_refresh_protected_snapshot", return_value=protected
                ),
            ):
                maintenance.maintenance_contract_refresh(
                    root,
                    TASK,
                    old_digest,
                    new_digest,
                    runner=self._runner(new_payload),
                )
            with self._local_environment(record):
                validated = contract.validate_contract(root, TASK)
            self.assertEqual(validated["status"], "READY")
            self.assertEqual(validated["sha256"], new_digest)


if __name__ == "__main__":
    unittest.main()
