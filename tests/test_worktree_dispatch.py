from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "components" / "agent-core" / ".automation" / "bin" / "worktree_dispatch.py"
SPEC = importlib.util.spec_from_file_location("worktree_dispatch_for_tests", SCRIPT)
assert SPEC and SPEC.loader
dispatch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dispatch
SPEC.loader.exec_module(dispatch)


class _RecordStore:
    def __init__(self) -> None:
        self.value: bytes | None = None
        self.writes: list[bytes] = []

    def read(self, _path, _what=""):
        if self.value is None:
            raise FileNotFoundError("missing")
        return self.value

    def exclusive(self, _path, content):
        if self.value is not None:
            raise RuntimeError("already exists")
        self.value = content
        self.writes.append(content)

    def write(self, _path, content):
        self.value = content
        self.writes.append(content)


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class WorktreeDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.main = self.root / "main"
        self.target = self.root / "task"
        self.common = self.root / "common.git"
        self.main.mkdir()
        self.target.mkdir()
        self.common.mkdir()
        self.store = _RecordStore()
        self.registration = dispatch.Registration(
            self.root, self.main, self.target, "task/7-dispatch", self.common
        )
        self.patches = mock.patch.multiple(
            dispatch.private_state,
            prepare=mock.Mock(),
            dispatch_record=mock.Mock(return_value=self.root / "dispatch.json"),
            dispatch_lock=mock.Mock(return_value=_Lock()),
            read_bytes=self.store.read,
            exclusive_write_bytes=self.store.exclusive,
            write_bytes=self.store.write,
            create=True,
        )
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.listener_patch = mock.patch.object(dispatch, "_require_listener_owner")
        self.listener_patch.start()
        self.addCleanup(self.listener_patch.stop)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _record(self, **overrides):
        value = {
            "schema_version": 1,
            "kind": "task",
            "task_id": "7",
            "repository": "example/repo",
            "branch": "task/7-dispatch",
            "worktree": str(self.target),
            "common_git_dir": str(self.common),
            "orchestrator": "task-orchestrator",
            "pid": 731,
            "boot_id": "01234567-89ab-cdef-0123-456789abcdef",
            "start_ticks": 12,
            "port": 4097,
            "session_id": "session-7",
            "credential": "credential-not-for-output",
            "implementation_version": "3.2.0",
            "implementation_revision": "a" * 40,
            "state": "running",
            "failure": None,
        }
        value.update(overrides)
        if "pending_permission" in overrides:
            value["state"] = (
                "permission-pending" if overrides["pending_permission"] is not None else "idle"
            )
        return value

    def _resolve(self):
        patch = mock.patch.object(dispatch, "resolve_target", return_value=self.registration)
        patch.start()
        self.addCleanup(patch.stop)

    def test_target_resolution_rejects_different_common_git_directory(self) -> None:
        main_record = types.SimpleNamespace(path=self.main)
        task_record = types.SimpleNamespace(path=self.target, branch="task/7-dispatch")
        with (
            mock.patch.object(dispatch.lifecycle, "repo_root", return_value=self.main),
            mock.patch.object(dispatch.lifecycle, "main_worktree", return_value=main_record),
            mock.patch.object(dispatch.lifecycle, "worktree_for_task", return_value=task_record),
            mock.patch.object(dispatch.lifecycle, "parse_worktrees", return_value=[task_record]),
            mock.patch.object(
                dispatch.lifecycle,
                "common_git_dir",
                side_effect=[self.common, self.root / "other.git"],
            ),
        ):
            with self.assertRaisesRegex(dispatch.DispatchError, "common directory"):
                dispatch.resolve_target(self.main, "task", "7")

    def test_start_uses_main_path_for_readiness_and_keeps_password_out_of_output(self) -> None:
        self._resolve()
        process = types.SimpleNamespace(pid=731, poll=mock.Mock(return_value=None))
        client = mock.create_autospec(
            dispatch.opencode_http.OpenCodeHTTPAdapter, instance=True
        )
        client.global_health.return_value = {"ok": True}
        client.create_session.return_value = {"id": "session-7"}
        client.prompt_async.return_value = {"accepted": True}
        fake_http = types.SimpleNamespace(OpenCodeHTTPAdapter=mock.Mock(return_value=client))
        readiness = {
            "status": "READY",
            "mode": "initial",
            "task": "7",
            "repository": "example/repo",
            "worktree": str(self.target),
        }
        with (
            mock.patch.object(dispatch, "opencode_http", fake_http),
            mock.patch.object(dispatch, "_allocate_port", return_value=4097),
            mock.patch.object(
                dispatch, "_opencode_executable", return_value=Path("/trusted/opencode")
            ),
            mock.patch.object(
                dispatch,
                "_target_implementation",
                return_value=("3.2.0", "a" * 40),
            ),
            mock.patch.object(
                dispatch,
                "_boot_id",
                return_value="01234567-89ab-cdef-0123-456789abcdef",
            ),
            mock.patch.object(dispatch, "_proc_start_ticks", return_value=12),
            mock.patch.object(dispatch, "_wait_for_listener_owner"),
            mock.patch.object(dispatch.subprocess, "Popen", return_value=process) as popen,
            mock.patch.object(dispatch, "_readiness", return_value=readiness) as ready,
            mock.patch.dict(dispatch.os.environ, {"GIT_DIR": "/attacker/repository"}),
            mock.patch.object(dispatch.os, "pipe", return_value=(40, 41)),
            mock.patch.object(dispatch.os, "write", return_value=1),
            mock.patch.object(dispatch.os, "close"),
        ):
            result = dispatch.start(self.root, "task", "7")
        ready.assert_called_once_with(self.registration, "task", "7")
        args, kwargs = popen.call_args
        self.assertEqual(args[0][1:4], ["-B", str(dispatch.Path(dispatch.__file__).resolve()), "_serve-gated"])
        self.assertEqual(args[0][4:], ["40", "4097", "/trusted/opencode"])
        self.assertNotIn("--auto", args[0])
        self.assertEqual(kwargs["cwd"], self.target)
        self.assertEqual(kwargs["pass_fds"], (40,))
        self.assertNotIn(kwargs["env"][dispatch.PASSWORD_ENV], args[0])
        self.assertNotIn("GIT_DIR", kwargs["env"])
        self.assertEqual(kwargs["env"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertNotIn(kwargs["env"][dispatch.PASSWORD_ENV], json.dumps(result))
        self.assertEqual(result["action"], "started")
        self.assertNotIn("credential", result)
        self.assertEqual(
            [json.loads(value.decode())["state"] for value in self.store.writes],
            ["starting", "starting", "running"],
        )
        self.assertEqual(json.loads(self.store.writes[1].decode())["pid"], 731)
        client.prompt_async.assert_called_once()
        self.assertIsInstance(client.prompt_async.call_args.args[1], str)
        handoff = json.loads(client.prompt_async.call_args.args[1])
        self.assertEqual(handoff["orchestrator"], "task-orchestrator")
        self.assertEqual(handoff["repository"], "example/repo")
        self.assertEqual(handoff["readiness"]["repository"], "example/repo")
        self.assertEqual(client.prompt_async.call_args.kwargs["agent"], "task-orchestrator")

    def test_launch_gate_execs_exact_loopback_server_command(self) -> None:
        with (
            mock.patch.object(dispatch.os, "read", return_value=b"1"),
            mock.patch.object(dispatch.os, "close"),
            mock.patch.object(dispatch.os, "execve", side_effect=OSError("captured")) as execute,
        ):
            self.assertEqual(
                dispatch._serve_gated(40, 4097, Path("/trusted/opencode")), 126
            )
        command = [
            "/trusted/opencode",
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            "4097",
        ]
        execute.assert_called_once_with("/trusted/opencode", command, dispatch.os.environ)
        self.assertNotIn("--auto", command)

    def test_maintenance_child_gets_only_dedicated_opt_in(self) -> None:
        with mock.patch.dict(
            dispatch.os.environ,
            {"GIT_DIR": "/attacker", "AUTOMATION_MAINTENANCE": "unexpected"},
        ):
            task = dispatch._child_environment("task", "task-secret")
            maintenance = dispatch._child_environment(
                "maintenance", "maintenance-secret"
            )
        self.assertNotIn("GIT_DIR", task)
        self.assertNotIn("AUTOMATION_MAINTENANCE", task)
        self.assertEqual(maintenance["AUTOMATION_MAINTENANCE"], "1")
        self.assertEqual(maintenance[dispatch.PASSWORD_ENV], "maintenance-secret")

    def test_listener_ownership_requires_exact_process_socket_inode(self) -> None:
        proc = self.root / "proc"
        fd = proc / "731" / "fd"
        net = proc / "731" / "net"
        fd.mkdir(parents=True)
        net.mkdir()
        (fd / "8").symlink_to("socket:[12345]")
        (net / "tcp").write_text(
            "  sl  local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
            "   0: 0100007F:1001 00000000:0000 0A 0:0 00:0 0 1000 0 12345\n",
            encoding="ascii",
        )
        with mock.patch.object(dispatch, "PROC_PATH", proc):
            self.assertTrue(dispatch._process_owns_loopback_listener(731, 4097))
            self.assertFalse(dispatch._process_owns_loopback_listener(731, 4098))

    def test_zombie_process_identity_is_dead(self) -> None:
        proc = self.root / "proc"
        process = proc / "731"
        process.mkdir(parents=True)
        process.joinpath("stat").write_text(
            "731 (opencode) " + " ".join(["Z", *("0" for _ in range(18)), "12"]) + "\n",
            encoding="ascii",
        )
        boot = self.root / "boot_id"
        boot.write_text("01234567-89ab-cdef-0123-456789abcdef\n", encoding="ascii")
        with (
            mock.patch.object(dispatch, "PROC_PATH", proc),
            mock.patch.object(dispatch, "BOOT_ID_PATH", boot),
        ):
            self.assertEqual(dispatch._identity_state(self._record()), "dead")

    def test_start_reconciles_a_live_duplicate_without_spawning(self) -> None:
        self._resolve()
        self.store.value = dispatch._encode_record(self._record())
        client = mock.create_autospec(
            dispatch.opencode_http.OpenCodeHTTPAdapter, instance=True
        )
        client.global_health.return_value = {"ok": True}
        client.session_status.return_value = {"id": "session-7"}
        with (
            mock.patch.object(dispatch, "_identity_state", return_value="live"),
            mock.patch.object(
                dispatch,
                "opencode_http",
                types.SimpleNamespace(OpenCodeHTTPAdapter=mock.Mock(return_value=client)),
            ),
            mock.patch.object(dispatch.subprocess, "Popen") as popen,
        ):
            result = dispatch.start(self.root, "task", "7")
        popen.assert_not_called()
        self.assertEqual(result["action"], "existing")
        self.assertNotIn("credential", result)

    def test_start_publishes_starting_then_failed_without_leaking_credential(self) -> None:
        self._resolve()
        readiness = {
            "status": "READY",
            "mode": "initial",
            "task": "7",
            "repository": "example/repo",
            "worktree": str(self.target),
        }
        with (
            mock.patch.object(dispatch, "_readiness", return_value=readiness),
            mock.patch.object(
                dispatch, "_target_implementation", return_value=("3.2.0", "a" * 40)
            ),
            mock.patch.object(dispatch, "_allocate_port", return_value=4097),
            mock.patch.object(
                dispatch.subprocess,
                "Popen",
                side_effect=RuntimeError("spawn failed"),
            ),
            self.assertRaisesRegex(dispatch.DispatchError, "spawn failed"),
        ):
            dispatch.start(self.root, "task", "7")
        states = [json.loads(value.decode())["state"] for value in self.store.writes]
        self.assertEqual(states, ["starting", "failed"])
        failed = json.loads(self.store.value.decode())
        self.assertIsNotNone(failed["failure"])
        self.assertNotIn(failed["credential"], str(failed["failure"]))

    def test_maintenance_readiness_uses_main_worktree_and_fixed_orchestrator(self) -> None:
        readiness = {
            "status": "READY",
            "mode": "maintenance",
            "task": "7",
            "repository": "example/repo",
            "worktree": str(self.target),
        }
        with mock.patch.object(
            dispatch.maintenance_lifecycle,
            "maintenance_check",
            return_value=readiness,
        ) as check:
            result = dispatch._readiness(self.registration, "maintenance", "7")
        check.assert_called_once_with(self.main, "7")
        self.assertEqual(dispatch.ORCHESTRATORS["maintenance"], "maintenance-orchestrator")
        self.assertEqual(result["mode"], "maintenance")

    def test_maintenance_handoff_derives_source_only_from_validated_receipt(self) -> None:
        readiness = {
            "status": "READY",
            "mode": "maintenance",
            "task": "7",
            "repository": "example/repo",
            "worktree": str(self.target),
            "stage": "applied",
            "sourceRevision": "a" * 40,
        }
        receipt = {"source": str(self.root / "source"), "source_revision": "a" * 40}
        with (
            mock.patch.object(
                dispatch.lifecycle,
                "worktree_for_task",
                return_value=dispatch.lifecycle.WorktreeRecord(
                    self.target, "task/7-dispatch", "b" * 40
                ),
            ),
            mock.patch.object(
                dispatch.maintenance_lifecycle,
                "_validate_active_receipt",
                return_value=receipt,
            ),
            mock.patch.object(
                dispatch.maintenance_lifecycle.upgrade,
                "resolve_pinned_source",
                return_value=(self.root / "source", "a" * 40),
            ),
        ):
            handoff = json.loads(
                dispatch._handoff("maintenance", "7", self.registration, readiness)
            )
        self.assertEqual(
            handoff["maintenanceSource"],
            {"path": str(self.root / "source"), "revision": "a" * 40},
        )

    def test_pristine_maintenance_dispatch_fails_without_source_receipt(self) -> None:
        readiness = {
            "status": "READY",
            "mode": "maintenance",
            "task": "7",
            "repository": "example/repo",
            "worktree": str(self.target),
            "stage": "pristine",
        }
        with self.assertRaisesRegex(dispatch.DispatchError, "source receipt"):
            dispatch._handoff("maintenance", "7", self.registration, readiness)

    def test_status_persists_exact_permission_without_broadening_patterns(self) -> None:
        self._resolve()
        self.store.value = dispatch._encode_record(self._record())
        client = mock.create_autospec(
            dispatch.opencode_http.OpenCodeHTTPAdapter, instance=True
        )
        client.global_health.return_value = {"ok": True}
        client.session_status.return_value = {"id": "session-7"}
        client.list_permissions.return_value = [
            {
                "id": "permission-1",
                "sessionID": "session-7",
                "permission": "bash",
                "patterns": ["git status --short"],
            }
        ]
        with (
            mock.patch.object(dispatch, "_identity_state", return_value="live"),
            mock.patch.object(
                dispatch,
                "opencode_http",
                types.SimpleNamespace(OpenCodeHTTPAdapter=mock.Mock(return_value=client)),
            ),
        ):
            result = dispatch.status(self.root, "task", "7")
        pending = result["pending_permission"]
        self.assertEqual(pending["patterns"], ["git status --short"])
        persisted = json.loads(self.store.value.decode())
        self.assertEqual(persisted["pending_permission"], pending)

    def test_status_ignores_permissions_for_other_sessions(self) -> None:
        self._resolve()
        self.store.value = dispatch._encode_record(self._record())
        client = mock.create_autospec(
            dispatch.opencode_http.OpenCodeHTTPAdapter, instance=True
        )
        client.global_health.return_value = {"ok": True}
        client.session_status.return_value = {"id": "session-7"}
        client.list_permissions.return_value = [
            {
                "id": "permission-foreign",
                "sessionID": "session-other",
                "permission": "bash",
                "patterns": ["git push"],
            }
        ]
        with (
            mock.patch.object(dispatch, "_identity_state", return_value="live"),
            mock.patch.object(
                dispatch,
                "opencode_http",
                types.SimpleNamespace(OpenCodeHTTPAdapter=mock.Mock(return_value=client)),
            ),
        ):
            result = dispatch.status(self.root, "task", "7")
        self.assertIsNone(result["pending_permission"])

    def test_respond_does_not_clear_pending_when_reply_is_not_acknowledged(self) -> None:
        self._resolve()
        pending = {
            "id": "permission-1",
            "session_id": "session-7",
            "permission": "bash",
            "patterns": ["git status --short"],
        }
        self.store.value = dispatch._encode_record(self._record(pending_permission=pending))
        client = mock.create_autospec(
            dispatch.opencode_http.OpenCodeHTTPAdapter, instance=True
        )
        client.global_health.return_value = {"ok": True}
        client.session_status.return_value = {"id": "session-7"}
        client.list_permissions.return_value = [
            {
                "id": "permission-1",
                "sessionID": "session-7",
                "permission": "bash",
                "patterns": ["git status --short"],
            }
        ]
        client.reply.return_value = {"acknowledged": False}
        with (
            mock.patch.object(dispatch, "_identity_state", return_value="live"),
            mock.patch.object(
                dispatch,
                "opencode_http",
                types.SimpleNamespace(OpenCodeHTTPAdapter=mock.Mock(return_value=client)),
            ),
        ):
            with self.assertRaisesRegex(dispatch.DispatchError, "acknowledge"):
                dispatch.respond(self.root, "task", "7", "permission-1", "once")
        self.assertEqual(json.loads(self.store.value.decode())["pending_permission"], pending)

    def test_respond_maps_session_to_always_and_clears_only_after_ack(self) -> None:
        self._resolve()
        pending = {
            "id": "permission-1",
            "session_id": "session-7",
            "permission": "bash",
            "patterns": ["git status --short"],
        }
        self.store.value = dispatch._encode_record(self._record(pending_permission=pending))
        client = mock.create_autospec(
            dispatch.opencode_http.OpenCodeHTTPAdapter, instance=True
        )
        client.global_health.return_value = {"ok": True}
        client.session_status.return_value = {"id": "session-7"}
        client.list_permissions.return_value = [
            {
                "id": "permission-1",
                "sessionID": "session-7",
                "permission": "bash",
                "patterns": ["git status --short"],
            }
        ]
        client.reply.return_value = {"acknowledged": True}
        with (
            mock.patch.object(dispatch, "_identity_state", return_value="live"),
            mock.patch.object(
                dispatch,
                "opencode_http",
                types.SimpleNamespace(OpenCodeHTTPAdapter=mock.Mock(return_value=client)),
            ),
        ):
            result = dispatch.respond(self.root, "task", "7", "permission-1", "session")
        self.assertEqual(result["response"], "always")
        client.reply.assert_called_once_with("permission-1", "always")
        self.assertNotIn("pending_permission", json.loads(self.store.value.decode()))

    def test_stop_never_kills_after_pid_identity_changes(self) -> None:
        self._resolve()
        self.store.value = dispatch._encode_record(self._record())
        with (
            mock.patch.object(dispatch, "_identity_state", side_effect=["live", "mismatch"]),
            mock.patch.object(dispatch.os, "pidfd_open", return_value=44, create=True),
            mock.patch.object(
                dispatch.signal, "pidfd_send_signal", create=True
            ) as pidfd_send,
            mock.patch.object(dispatch.os, "close"),
        ):
            with self.assertRaisesRegex(dispatch.DispatchError, "before signalling"):
                dispatch.stop(self.root, "task", "7")
        pidfd_send.assert_not_called()

    def test_stop_recovers_live_incomplete_starting_dispatch(self) -> None:
        self._resolve()
        self.store.value = dispatch._encode_record(
            self._record(session_id=None, state="starting")
        )
        with (
            mock.patch.object(
                dispatch, "_identity_state", side_effect=["live", "live", "dead"]
            ),
            mock.patch.object(dispatch.os, "pidfd_open", return_value=44, create=True),
            mock.patch.object(
                dispatch.signal, "pidfd_send_signal", create=True
            ) as pidfd_send,
            mock.patch.object(dispatch.os, "close"),
        ):
            result = dispatch.stop(self.root, "task", "7")
        self.assertEqual(result["state"], "stopped")
        pidfd_send.assert_called_once_with(44, dispatch.signal.SIGTERM, None, 0)
        persisted = json.loads(self.store.value.decode())
        self.assertIsNone(persisted["pid"])

    def test_strict_record_rejects_extra_fields(self) -> None:
        self._resolve()
        record = self._record(extra="must-not-be-accepted")
        self.store.value = dispatch._encode_record(record)
        with self.assertRaisesRegex(dispatch.DispatchError, "schema is not exact"):
            dispatch.status(self.root, "task", "7")

    def test_status_preserves_dead_startup_failure_evidence(self) -> None:
        self._resolve()
        self.store.value = dispatch._encode_record(
            self._record(
                pid=None,
                boot_id=None,
                start_ticks=None,
                session_id=None,
                state="failed",
                failure="startup failed safely",
            )
        )
        result = dispatch.status(self.root, "task", "7")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["failure"], "startup failed safely")
        self.assertEqual(len(self.store.writes), 0)


class WorktreeDispatchSurfaceTest(unittest.TestCase):
    def test_public_just_apis_bind_fixed_kinds(self) -> None:
        core = ROOT / "components" / "agent-core" / ".automation" / "just"
        agent = (core / "agent.just").read_text(encoding="utf-8")
        automation = (core / "automation.just").read_text(encoding="utf-8")
        for operation in ("start", "status", "stop"):
            self.assertIn(f"dispatch-{operation} task:", agent)
            self.assertIn(f"dispatch-{operation} task:", automation)
        self.assertIn("dispatch-respond task permission_id response:", agent)
        self.assertIn("dispatch-respond task permission_id response:", automation)
        self.assertIn(" start task {{quote(task)}}", agent)
        self.assertIn(" start maintenance {{quote(task)}}", automation)
        self.assertNotIn("--auto", agent)
        self.assertNotIn("--auto", automation)


if __name__ == "__main__":
    unittest.main()
