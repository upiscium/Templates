from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "tools" / "automation_recovery_bridge.py"
spec = importlib.util.spec_from_file_location("recovery_bridge_test", BRIDGE_PATH)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class TaskContractRecoveryTest(unittest.TestCase):
    def test_parser_keeps_maintenance_commands_and_accepts_numeric_argument(self) -> None:
        self.assertEqual(
            bridge.parser().parse_args(["recover-maintenance-authority", "/tmp/task"]).command,
            "recover-maintenance-authority",
        )
        args = bridge.parser().parse_args(["commit-recovered-maintenance", "/tmp/task", "T-1"])
        self.assertEqual((args.command, args.task, args.message), ("commit-recovered-maintenance", "T-1", ""))
        args = bridge.parser().parse_args(["recover-task-contract-from-issue", "/tmp/task", "19"])
        self.assertEqual((args.command, args.issue), ("recover-task-contract-from-issue", "19"))
        args = bridge.parser().parse_args(["rebind-maintenance-provenance", "/tmp/task", "a" * 40])
        self.assertEqual((args.command, args.expected_source_revision), ("rebind-maintenance-provenance", "a" * 40))
        args = bridge.parser().parse_args(["rebind-maintenance-provenance", "/tmp/task", "a" * 64])
        self.assertEqual(args.expected_source_revision, "a" * 64)
        args = bridge.parser().parse_args(["resume-contract-check", "/tmp/task", "99"])
        self.assertEqual((args.command, args.task), ("resume-contract-check", "99"))
        for value in ("a" * 39, "a" * 41, "a" * 63, "A" * 40, "main", "v1.2.3"):
            with self.subTest(value=value):
                with self.assertRaises(bridge.BridgeError):
                    bridge.parser().parse_args(["rebind-maintenance-provenance", "/tmp/task", value])
        for value in ("", "TASK-99", "019", "bad/task", "-bad", "bad task"):
            with self.subTest(value=value):
                with self.assertRaises(bridge.BridgeError):
                    bridge.parser().parse_args(["resume-contract-check", "/tmp/task", value])
        args = bridge.parser().parse_args(
            ["maintenance-finalize", "/tmp/task", "112", "7", "b" * 40]
        )
        self.assertEqual(
            (args.command, args.task, args.pr, args.expected_implementation_revision),
            ("maintenance-finalize", "112", "7", "b" * 40),
        )
        for value in ("0", "07", "-1", "A" * 40, "a" * 39):
            with self.subTest(value=value):
                with self.assertRaises(bridge.BridgeError):
                    bridge.parser().parse_args(
                        ["maintenance-finalize", "/tmp/task", "112", value, "a" * 40]
                    )

    def test_resume_check_requires_exact_registered_non_main_target(self) -> None:
        contract = mock.Mock()
        contract.lifecycle.repo_root.return_value = Path("/tmp/task").resolve()
        contract.lifecycle.current_worktree.return_value = mock.Mock(path=Path("/tmp/task").resolve())
        contract.lifecycle.main_worktree.return_value = mock.Mock(path=Path("/tmp/main").resolve())
        contract.lifecycle.worktree_for_task.return_value = mock.Mock(
            path=Path("/tmp/task").resolve()
        )
        contract.check_resume_contract.return_value = {
            "status": "READY", "mode": "resume", "worktree": str(Path("/tmp/task").resolve())
        }
        self.assertEqual(
            bridge._check_resume_contract(contract, Path("/tmp/task").resolve(), "99")["mode"],
            "resume",
        )
        contract.lifecycle.repo_root.return_value = Path("/tmp/other").resolve()
        with self.assertRaises(bridge.BridgeError):
            bridge._check_resume_contract(contract, Path("/tmp/other").resolve(), "99")

    def test_resume_dispatch_uses_verified_contract_and_reports_revision(self) -> None:
        contract = mock.Mock()
        target = Path("/tmp/task").resolve()
        contract.lifecycle.repo_root.return_value = target
        contract.lifecycle.current_worktree.return_value = mock.Mock(path=target)
        contract.lifecycle.main_worktree.return_value = mock.Mock(path=Path("/tmp/main").resolve())
        contract.lifecycle.worktree_for_task.return_value = mock.Mock(path=target)
        contract.check_resume_contract.return_value = {
            "status": "READY", "mode": "resume", "task": "99",
            "worktree": str(target), "issue": 99, "repository": "o/r",
            "sha256": "d" * 64, "taskStatus": "implementing",
        }
        with mock.patch.object(bridge, "_clean_root", return_value="b" * 40), \
             mock.patch.object(bridge, "_verify_bootstrap"), \
             mock.patch.object(bridge, "trusted_git", return_value=Path("/usr/bin/git")), \
             mock.patch.object(bridge, "_verified_task_contract") as verified, \
             mock.patch.object(bridge, "maintenance_environment"), \
             mock.patch.object(bridge, "_verified_engine") as engine, \
             mock.patch.object(sys, "argv", ["bridge", "resume-contract-check", str(target), "99"]), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            verified.return_value.__enter__.return_value = contract
            self.assertEqual(bridge.main(), 0)
        engine.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["implementationRevision"], "b" * 40)
        contract.check_resume_contract.assert_called_once_with(
            target, "99", runner=bridge.trusted_gh_run
        )

    def test_rebind_passes_raw_source_revision_to_verified_engine(self) -> None:
        engine = mock.Mock()
        engine.git_executable.return_value = bridge.trusted_git()
        engine.rebind_maintenance_provenance_from_source.return_value = {"status": "REBIND_COMPLETE"}
        with mock.patch.object(bridge, "_clean_root", return_value="b" * 40), \
             mock.patch.object(bridge, "_verify_bootstrap"), \
             mock.patch.object(bridge, "_verified_engine") as verified, \
             mock.patch.object(bridge, "maintenance_environment"), \
             mock.patch.object(sys, "argv", ["bridge", "rebind-maintenance-provenance", "/tmp/task", "a" * 40]), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            verified.return_value.__enter__.return_value = engine
            self.assertEqual(bridge.main(), 0)
        engine.rebind_maintenance_provenance_from_source.assert_called_once_with(
            Path("/tmp/task").resolve(), bridge.ROOT, "a" * 40,
            expected_implementation_revision="b" * 40,
        )

    def test_dirty_source_is_rejected_before_target_engine_call(self) -> None:
        with mock.patch.object(bridge, "_clean_root", side_effect=bridge.BridgeError("dirty")), \
             mock.patch.object(bridge, "_verify_bootstrap"), \
             mock.patch.object(bridge, "_verified_engine") as verified, \
             mock.patch.object(sys, "argv", ["bridge", "recover-task-contract-from-issue", "/tmp/task", "19"]), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(bridge.main(), 2)
        verified.assert_not_called()

    def test_tampered_bootstrap_is_rejected_before_target_changes(self) -> None:
        with mock.patch.object(bridge, "_clean_root", return_value="a" * 40), \
             mock.patch.object(bridge, "_verify_bootstrap", side_effect=bridge.BridgeError("tampered")), \
             mock.patch.object(bridge, "_verified_engine") as verified, \
             mock.patch.object(sys, "argv", ["bridge", "recover-task-contract-from-issue", "/tmp/task", "19"]), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(bridge.main(), 2)
        verified.assert_not_called()

    def test_verified_task_contract_is_loaded_from_head_blob(self) -> None:
        contract_bytes = bridge.CONTRACT_PATH.read_bytes()
        lifecycle_bytes = bridge.LIFECYCLE_PATH.read_bytes()

        def tree_blob(_root: Path, _revision: str, path: str):
            return ("contract", 0o100755) if path.endswith("task_contract.py") else ("lifecycle", 0o100644)

        def blob(_root: Path, oid: str) -> bytes:
            return contract_bytes if oid == "contract" else lifecycle_bytes

        with mock.patch.object(bridge, "_tree_blob", side_effect=tree_blob), \
             mock.patch.object(bridge, "_blob", side_effect=blob):
            with bridge._verified_task_contract(ROOT, "0" * 40) as contract:
                self.assertNotEqual(Path(contract.__file__).resolve(), bridge.CONTRACT_PATH.resolve())
                self.assertTrue(hasattr(contract, "recover_task_from_issue"))

    def test_maintenance_dispatch_uses_verified_modules_and_reports_revision(self) -> None:
        maintenance = mock.Mock()
        maintenance.maintenance_finalize.return_value = {"status": "FINALIZED"}
        target = Path("/tmp/task").resolve()
        with mock.patch.object(bridge, "_clean_root", return_value="b" * 40), \
             mock.patch.object(bridge, "_verify_bootstrap"), \
             mock.patch.object(bridge, "_validate_target_git_configuration"), \
             mock.patch.object(bridge, "_verified_modules") as verified, \
             mock.patch.object(bridge, "maintenance_environment"), \
             mock.patch.object(sys, "argv", ["bridge", "maintenance-finalize", str(target), "112", "7", "b" * 40]), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            verified.return_value.__enter__.return_value = {"maintenance_lifecycle": maintenance}
            self.assertEqual(bridge.main(), 0)
        maintenance.maintenance_finalize.assert_called_once_with(target, "112", 7)
        self.assertEqual(json.loads(output.getvalue())["implementationRevision"], "b" * 40)

    def test_verified_maintenance_modules_are_private_and_restore_process_state(self) -> None:
        blobs = {
            relative: (ROOT / relative).read_bytes()
            for _, relative in bridge.CANONICAL_MODULES
        }
        old_path = list(sys.path)
        sentinel = mock.Mock(name="stale-maintenance-module")
        previous = sys.modules.get("maintenance_lifecycle")
        sys.modules["maintenance_lifecycle"] = sentinel

        def tree_blob(_root: Path, _revision: str, relative: str):
            return relative, 0o100755

        try:
            with mock.patch.object(bridge, "_tree_blob", side_effect=tree_blob), \
                 mock.patch.object(bridge, "_blob", side_effect=lambda _root, oid: blobs[oid]), \
                 mock.patch.object(bridge, "trusted_git", return_value=Path("/usr/bin/git")), \
                 mock.patch.object(bridge, "trusted_gh", return_value=Path("/usr/bin/gh")):
                with bridge._verified_modules(ROOT, "0" * 40) as modules:
                    loaded = modules["maintenance_lifecycle"]
                    self.assertIsNot(loaded, sentinel)
                    self.assertNotEqual(
                        Path(loaded.__file__).resolve(), bridge.MAINTENANCE_PATH.resolve()
                    )
                    self.assertIs(sys.modules["maintenance_lifecycle"], loaded)
            self.assertEqual(sys.path, old_path)
            self.assertIs(sys.modules["maintenance_lifecycle"], sentinel)
        finally:
            if previous is None:
                sys.modules.pop("maintenance_lifecycle", None)
            else:
                sys.modules["maintenance_lifecycle"] = previous

    def test_pinned_git_runner_disables_repository_hooks(self) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(bridge, "trusted_git", return_value=Path("/usr/bin/git")), \
             mock.patch.object(bridge.subprocess, "run", return_value=completed) as run:
            self.assertIs(
                bridge._pinned_run(["git", "status"], cwd=Path("/tmp/task")),
                completed,
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/git")
        self.assertIn("core.hooksPath=/dev/null", argv)
        self.assertIn("core.fsmonitor=false", argv)

    def test_target_git_configuration_rejects_execution_and_transport_overrides(self) -> None:
        for key in (
            "include.path",
            "url.ssh://attacker.invalid/.insteadOf",
            "core.sshCommand",
            "credential.helper",
            "filter.attack.smudge",
            "remote.origin.uploadpack",
        ):
            with self.subTest(key=key), mock.patch.object(
                bridge,
                "_pinned_run",
                return_value=mock.Mock(stdout=key + "\0"),
            ), self.assertRaisesRegex(bridge.BridgeError, "unsafe local Git configuration"):
                bridge._validate_target_git_configuration(Path("/tmp/task"))

    def test_target_git_configuration_allows_normal_repository_identity(self) -> None:
        ordinary = "\0".join(
            (
                "core.repositoryformatversion",
                "core.filemode",
                "remote.origin.url",
                "remote.origin.fetch",
                "branch.main.remote",
                "branch.main.merge",
                "",
            )
        )
        with mock.patch.object(
            bridge,
            "_pinned_run",
            side_effect=[
                mock.Mock(stdout=ordinary, returncode=0),
                mock.Mock(stdout="", returncode=1),
            ],
        ):
            bridge._validate_target_git_configuration(Path("/tmp/task"))


if __name__ == "__main__":
    unittest.main()
