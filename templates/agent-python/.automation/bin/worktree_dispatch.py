#!/usr/bin/env python3
"""Fail-closed dispatcher for one OpenCode server per registered worktree.

The dispatcher deliberately owns only the process/session hand-off.  Worktree
creation, Task Contract validation, and private-state layout remain owned by
their respective sibling modules.  In particular, a caller never supplies a
worktree path: the path is resolved from Git's worktree registration from the
default-branch worktree.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from typing import Any, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import git_private_state as private_state
import task_lifecycle as lifecycle

try:
    import maintenance_lifecycle
except ModuleNotFoundError:  # pragma: no cover - an offline unit harness may omit it
    maintenance_lifecycle = None

try:
    import task_contract
except ModuleNotFoundError:  # pragma: no cover - an offline unit harness may omit it
    task_contract = None

try:
    import opencode_http
except ModuleNotFoundError:  # pragma: no cover - the production sibling is injected later
    opencode_http = None


TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PERMISSION_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
BOOT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
KINDS = ("task", "maintenance")
ORCHESTRATORS = {
    "task": "task-orchestrator",
    "maintenance": "maintenance-orchestrator",
}
DISPATCH_SCHEMA_VERSION = 1
RECORD_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "task_id",
        "repository",
        "branch",
        "worktree",
        "common_git_dir",
        "orchestrator",
        "port",
        "pid",
        "boot_id",
        "start_ticks",
        "session_id",
        "credential",
        "implementation_version",
        "implementation_revision",
        "state",
        "failure",
    }
)
OPTIONAL_RECORD_KEYS = frozenset({"pending_permission"})
PENDING_KEYS = frozenset({"id", "session_id", "permission", "patterns"})
DISPATCH_STATES = frozenset(
    {"starting", "running", "permission-pending", "idle", "stopped", "failed"}
)
PERMISSION_ACTIONS = {"once": "once", "session": "always", "deny": "reject"}
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
PROC_PATH = Path("/proc")
STARTUP_TIMEOUT = 15.0
STARTUP_POLL = 0.05
STOP_GRACE_TIMEOUT = 2.0
STOP_KILL_TIMEOUT = 1.0
STOP_POLL = 0.05
MAX_HANDOFF_BYTES = 128 * 1024


class DispatchError(RuntimeError):
    """A handled, fail-closed dispatcher error."""


DispatcherError = DispatchError


@dataclass(frozen=True)
class Registration:
    caller_root: Path
    main_root: Path
    target: Path
    branch: str
    common_git_dir: Path


def _safe_detail(value: object) -> str:
    return "".join(
        character
        if ord(character) >= 0x20
        and ord(character) != 0x7F
        and not 0xD800 <= ord(character) <= 0xDFFF
        else " "
        for character in str(value)
    ).strip()


def _error(message: str, cause: BaseException | None = None) -> DispatchError:
    # Never allow a provider/library exception to accidentally echo the server
    # credential.  The credential is intentionally persisted, but is not a CLI
    # or diagnostic secret.
    text = _safe_detail(message)
    if cause is not None:
        detail = _safe_detail(cause)
        if detail and detail not in text:
            text = f"{text}: {detail}"
    return DispatchError(f"BLOCKED: {text[:1024]}")


def _path(value: Path | str) -> Path:
    try:
        return Path(value).resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _error("path cannot be resolved", exc) from exc


def _validate_kind(kind: str) -> None:
    if kind not in KINDS:
        raise _error(f"kind must be one of {', '.join(KINDS)}")


def _validate_task(task: str) -> None:
    if not isinstance(task, str) or TASK_RE.fullmatch(task) is None:
        raise _error("task identifier is malformed")


def _validate_permission_id(permission_id: str) -> None:
    if (
        not isinstance(permission_id, str)
        or PERMISSION_ID_RE.fullmatch(permission_id) is None
        or any(0xD800 <= ord(character) <= 0xDFFF for character in permission_id)
    ):
        raise _error("permission identifier is malformed")


def _repo_root(cwd: Path | str | None = None) -> Path:
    start = _path(cwd or Path.cwd())
    try:
        value = lifecycle.repo_root(start)
    except Exception as exc:
        raise _error("cannot resolve the caller's Git worktree", exc) from exc
    return _path(value)


def _common_git_dir(root: Path) -> Path:
    function = getattr(lifecycle, "common_git_dir", None)
    if function is None:
        function = getattr(private_state, "common_git_dir", None)
    if function is None:
        raise _error("Git common-directory API is unavailable")
    try:
        return _path(function(root))
    except Exception as exc:
        raise _error("cannot resolve the Git common directory", exc) from exc


def resolve_target(root: Path | str, kind: str, task: str) -> Registration:
    """Resolve *task* only through Git's canonical worktree registration."""
    _validate_kind(kind)
    _validate_task(task)
    caller = _repo_root(root)
    try:
        main_record = lifecycle.main_worktree(caller)
        main = _path(main_record.path)
        target_record = lifecycle.worktree_for_task(main, task)
    except Exception as exc:
        raise _error("canonical worktree registration could not be resolved", exc) from exc

    try:
        target = _path(target_record.path)
        branch = target_record.branch
    except Exception as exc:
        raise _error("canonical worktree registration is malformed", exc) from exc
    if not isinstance(branch, str) or not branch:
        raise _error("registered target worktree is detached")
    if target == main:
        raise _error("the default-branch worktree cannot be dispatched")

    # Re-read the registration from the main worktree and require exactly one
    # matching path.  This prevents a stale or synthetic WorktreeRecord from
    # becoming an authority by itself.
    try:
        registered = [item for item in lifecycle.parse_worktrees(main) if _path(item.path) == target]
    except Exception as exc:
        raise _error("Git worktree registration could not be revalidated", exc) from exc
    if len(registered) != 1 or getattr(registered[0], "branch", None) != branch:
        raise _error("target is not the exact canonical Git worktree registration")

    main_common = _common_git_dir(main)
    target_common = _common_git_dir(target)
    if main_common != target_common:
        raise _error("target and default-branch worktrees do not share one Git common directory")
    return Registration(caller, main, target, branch, main_common)


def _prepare(root: Path) -> None:
    function = getattr(private_state, "prepare", None)
    if function is None:
        raise _error("Git private-state prepare API is unavailable")
    try:
        function(root)
    except Exception as exc:
        raise _error("Git private state is not ready", exc) from exc


def _record_path(root: Path, task: str) -> Path:
    function = getattr(private_state, "dispatch_record", None)
    if function is None:
        raise _error("Git private-state dispatch-record API is unavailable")
    try:
        value = function(root, task)
        path = Path(value)
    except Exception as exc:
        raise _error("cannot resolve the dispatch record", exc) from exc
    if not path.is_absolute():
        path = (root / path).resolve()
    return path


@contextmanager
def _transaction(registration: Registration, task: str) -> Iterator[Path]:
    _prepare(registration.main_root)
    lock_function = getattr(private_state, "dispatch_lock", None)
    if lock_function is None:
        raise _error("Git private-state dispatch-lock API is unavailable")
    record = _record_path(registration.main_root, task)
    try:
        lock = lock_function(registration.main_root)
        with lock:
            yield record
    except DispatchError:
        raise
    except Exception as exc:
        raise _error("dispatch private-state lock failed", exc) from exc


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON number: {value}")


def _read_record(path: Path) -> dict[str, Any] | None:
    reader = getattr(private_state, "read_bytes", None)
    if reader is None:
        raise _error("Git private-state read API is unavailable")
    try:
        raw = reader(path, "worktree dispatch record")
    except FileNotFoundError:
        return None
    except Exception as exc:
        # The real private-state API reports a missing canonical record through
        # its own error type.  Only a genuinely absent path may be treated as
        # an empty record; unsafe, malformed, and replaced paths remain BLOCKED.
        try:
            absent = not os.path.lexists(path)
        except OSError:
            absent = False
        if absent:
            return None
        raise _error("dispatch record cannot be read safely", exc) from exc
    try:
        if not isinstance(raw, (bytes, bytearray)):
            raise ValueError("private-state reader returned non-bytes")
        value = json.loads(
            bytes(raw).decode("utf-8"),
            object_pairs_hook=_no_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error("dispatch record is not valid JSON", exc) from exc
    if not isinstance(value, dict):
        raise _error("dispatch record is not a JSON object")
    return value


def _encode_record(record: dict[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _error("dispatch record contains non-JSON data", exc) from exc


def _absolute_equal(left: str, right: Path) -> bool:
    try:
        candidate = Path(left)
        if not candidate.is_absolute() or ".." in candidate.parts:
            return False
        return _path(candidate) == right
    except Exception:
        return False


def _validate_string(value: Any, label: str, *, limit: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise _error(f"dispatch record {label} is malformed")
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise _error(f"dispatch record {label} contains control characters")
    return value


def _validate_pending(value: Any, session_id: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != PENDING_KEYS:
        raise _error("dispatch record pending permission metadata is malformed")
    permission_id = _validate_string(value["id"], "pending permission id", limit=256)
    pending_session = _validate_string(value["session_id"], "pending permission session", limit=256)
    if pending_session != session_id:
        raise _error("dispatch record pending permission session mismatch")
    permission = _validate_string(value["permission"], "pending permission name", limit=256)
    patterns = value["patterns"]
    if not isinstance(patterns, list) or len(patterns) > 128:
        raise _error("dispatch record pending permission patterns are malformed")
    checked_patterns: list[str] = []
    for pattern in patterns:
        checked_patterns.append(_validate_string(pattern, "pending permission pattern", limit=4096))
    return {
        "id": permission_id,
        "session_id": pending_session,
        "permission": permission,
        "patterns": checked_patterns,
    }


def _validate_record(record: Any, registration: Registration, kind: str, task: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise _error("dispatch record schema is not exact")
    expected = RECORD_KEYS | OPTIONAL_RECORD_KEYS
    if not (set(record) <= expected and RECORD_KEYS <= set(record)):
        raise _error("dispatch record schema is not exact")
    if record["schema_version"] != DISPATCH_SCHEMA_VERSION:
        raise _error("dispatch record schema version is unsupported")
    if record["kind"] != kind or record["task_id"] != task:
        raise _error("dispatch record Task identity mismatch")
    repository = record["repository"]
    if not isinstance(repository, str) or re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ) is None:
        raise _error("dispatch record repository is malformed")
    branch = record["branch"]
    if (
        not isinstance(branch, str)
        or BRANCH_RE.fullmatch(branch) is None
        or branch.startswith("/")
        or branch.endswith("/")
        or "//" in branch
        or ".." in branch
        or (kind == "task" and not (branch.startswith(f"task/{task}-") or branch.startswith(f"fix/{task}-")))
    ):
        raise _error("dispatch record branch is malformed")
    if record["orchestrator"] != ORCHESTRATORS[kind]:
        raise _error("dispatch record orchestrator mismatch")
    if not _absolute_equal(record["worktree"], registration.target):
        raise _error("dispatch record worktree mismatch")
    if branch != registration.branch:
        raise _error("dispatch record branch mismatch")
    if not _absolute_equal(record["common_git_dir"], registration.common_git_dir):
        raise _error("dispatch record common Git directory mismatch")
    port = record["port"]
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise _error("dispatch record port is malformed")
    credential = record["credential"]
    if credential is not None:
        _validate_string(credential, "credential", limit=512)
    pid = record["pid"]
    if pid is not None and (
        isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 2**31 - 1
    ):
        raise _error("dispatch record PID is malformed")
    boot_id = record["boot_id"]
    if boot_id is not None:
        _validate_string(boot_id, "Linux boot_id", limit=128)
        if BOOT_ID_RE.fullmatch(boot_id) is None:
            raise _error("dispatch record Linux boot_id is malformed")
    if (
        record["start_ticks"] is not None
        and (
            isinstance(record["start_ticks"], bool)
            or not isinstance(record["start_ticks"], int)
            or record["start_ticks"] < 0
            or record["start_ticks"] > 2**63 - 1
        )
    ):
        raise _error("dispatch record /proc start_ticks is malformed")
    if (pid is None) != (boot_id is None) or (pid is None) != (record["start_ticks"] is None):
        raise _error("dispatch record process identity is incomplete")
    session_id = record["session_id"]
    if session_id is not None:
        _validate_string(session_id, "session id", limit=256)
    implementation_version = _validate_string(
        record["implementation_version"], "implementation version", limit=128
    )
    implementation_revision = record["implementation_revision"]
    if not isinstance(implementation_revision, str) or re.fullmatch(
        r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", implementation_revision
    ) is None:
        raise _error("dispatch record implementation revision is malformed")
    state = record["state"]
    if not isinstance(state, str) or state not in DISPATCH_STATES:
        raise _error("dispatch record state is malformed")
    failure = record["failure"]
    if failure is not None:
        _validate_string(failure, "failure", limit=4000)
    if state == "failed" and failure is None:
        raise _error("failed dispatch record has no failure detail")
    if state != "failed" and failure is not None:
        raise _error("non-failed dispatch record has failure detail")
    runtime_required = state in {"running", "permission-pending", "idle"}
    if runtime_required and any(
        value is None for value in (pid, boot_id, record["start_ticks"], port, session_id, credential)
    ):
        raise _error("running dispatch record has incomplete runtime identity")
    pending = record.get("pending_permission")
    if state == "permission-pending" and pending is None:
        raise _error("permission-pending dispatch record has no permission metadata")
    if state != "permission-pending" and pending is not None:
        raise _error("non-pending dispatch record has permission metadata")
    _validate_pending(pending, session_id) if pending is not None else None
    return record


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "credential"}


def _redact_secret(exc: DispatchError, secret: str) -> DispatchError:
    text = str(exc)
    if secret and secret in text:
        return DispatchError(text.replace(secret, "<redacted>"))
    return exc


def _write_record(path: Path, record: dict[str, Any], *, exclusive: bool = False) -> None:
    content = _encode_record(record)
    function_name = "exclusive_write_bytes" if exclusive else "write_bytes"
    function = getattr(private_state, function_name, None)
    if function is None:
        raise _error(f"Git private-state {function_name} API is unavailable")
    try:
        function(path, content)
    except Exception as exc:
        raise _error("dispatch record publication failed", exc) from exc


def _boot_id() -> str:
    try:
        value = BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise _error("Linux boot_id is unavailable", exc) from exc
    try:
        if BOOT_ID_RE.fullmatch(value) is None:
            raise ValueError("boot_id does not use the canonical UUID form")
    except ValueError as exc:
        raise _error("Linux boot_id is malformed", exc) from exc
    return value


def _proc_start_ticks(pid: int) -> int | None:
    path = PROC_PATH / str(pid) / "stat"
    try:
        raw = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _error("cannot inspect the exact process identity", exc) from exc
    closing = raw.rfind(")")
    if closing < 0:
        raise _error("/proc process stat is malformed")
    fields = raw[closing + 2 :].split()
    # The suffix starts at field 3 (state), so field 22 (starttime) is index 19.
    if len(fields) <= 19:
        raise _error("/proc process stat has no start_ticks")
    try:
        value = int(fields[19])
    except ValueError as exc:
        raise _error("/proc process start_ticks is malformed", exc) from exc
    if value < 0:
        raise _error("/proc process start_ticks is negative")
    return value


def _proc_state(pid: int) -> str | None:
    try:
        raw = (PROC_PATH / str(pid) / "stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _error("cannot inspect the exact process state", exc) from exc
    closing = raw.rfind(")")
    fields = raw[closing + 2 :].split() if closing >= 0 else []
    if not fields or len(fields[0]) != 1:
        raise _error("/proc process state is malformed")
    return fields[0]


def _identity_state(record: dict[str, Any]) -> str:
    if record.get("pid") is None:
        return "dead"
    if record.get("boot_id") is None or record.get("start_ticks") is None:
        return "mismatch"
    if record["boot_id"] != _boot_id():
        return "mismatch"
    ticks = _proc_start_ticks(record["pid"])
    if ticks is None:
        return "dead"
    if ticks != record["start_ticks"]:
        return "mismatch"
    if _proc_state(record["pid"]) in {None, "Z", "X", "x"}:
        return "dead"
    return "live"


def _require_live(record: dict[str, Any]) -> None:
    if record.get("state") not in {"running", "permission-pending", "idle"}:
        raise _error(f"dispatch record state {record.get('state')} is not live-capable")
    state = _identity_state(record)
    if state == "mismatch":
        raise _error("PID identity mismatch")
    if state != "live":
        raise _error("the recorded OpenCode process is not live")


def _allocate_port() -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            value = listener.getsockname()[1]
    except OSError as exc:
        raise _error("cannot allocate a loopback port", exc) from exc
    if not isinstance(value, int) or not 1 <= value <= 65535:
        raise _error("allocated loopback port is malformed")
    return value


def _http_client(port: int, password: str) -> Any:
    if opencode_http is None:
        raise _error("OpenCode HTTP adapter is unavailable")
    factory = getattr(opencode_http, "OpenCodeHTTPAdapter", None)
    if factory is None:
        factory = getattr(opencode_http, "OpenCodeClient", None)
    if factory is None:
        raise _error("OpenCode HTTP adapter class is unavailable")
    try:
        return factory(port, password)
    except Exception as exc:
        raise _error("OpenCode HTTP client could not be created", exc) from exc


def _call_noarg(client: Any, name: str) -> Any:
    method = getattr(client, name, None)
    if method is None:
        raise _error(f"OpenCode HTTP API lacks {name}")
    try:
        return method()
    except Exception as exc:
        raise _error(f"OpenCode {name} failed", exc) from exc


def _call_noarg_alias(client: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        if getattr(client, name, None) is not None:
            return _call_noarg(client, name)
    raise _error(f"OpenCode HTTP API lacks {' or '.join(names)}")


def _call_session(client: Any, name: str, session_id: str) -> Any:
    method = getattr(client, name, None)
    if method is None:
        raise _error(f"OpenCode HTTP API lacks {name}")
    try:
        return method(session_id)
    except Exception as exc:
        raise _error(f"OpenCode {name} failed", exc) from exc


def _health_ok(value: Any) -> bool:
    if value is True:
        return True
    if value is False or not isinstance(value, dict) or not value:
        return False
    status = value.get("status")
    if isinstance(status, str) and status.casefold() in {"down", "unhealthy", "error"}:
        return False
    indicators = [value[key] for key in ("healthy", "ok") if key in value]
    if indicators:
        return all(indicator is True for indicator in indicators)
    return isinstance(status, str) and status.casefold() in {"ok", "healthy", "ready", "up"}


def _wait_for_health(
    client: Any,
    process: Any | None = None,
    identity: dict[str, Any] | None = None,
) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last_error: DispatchError | None = None
    while True:
        try:
            if identity is not None:
                _require_listener_owner(identity)
            if _health_ok(_call_noarg_alias(client, ("global_health", "health"))):
                return
        except DispatchError as exc:
            last_error = exc
        if process is not None:
            try:
                if process.poll() is not None:
                    raise _error("OpenCode server exited before health became ready")
            except AttributeError:
                pass
        if time.monotonic() >= deadline:
            raise _error("OpenCode health check timed out", last_error)
        time.sleep(STARTUP_POLL)


def _process_owns_loopback_listener(pid: int, port: int) -> bool:
    """Return whether this exact Linux process owns the requested IPv4 listener."""
    socket_inodes: set[str] = set()
    try:
        entries = list((PROC_PATH / str(pid) / "fd").iterdir())
        if len(entries) > 4096:
            raise _error("OpenCode process has an unbounded descriptor table")
        for entry in entries:
            try:
                target = os.readlink(entry)
            except (FileNotFoundError, OSError):
                continue
            match = re.fullmatch(r"socket:\[([0-9]+)\]", target)
            if match is not None:
                socket_inodes.add(match.group(1))
        rows = (PROC_PATH / str(pid) / "net" / "tcp").read_text(
            encoding="ascii"
        ).splitlines()[1:]
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _error("cannot inspect OpenCode listener ownership", exc) from exc
    wanted = f"0100007F:{port:04X}"
    for row in rows:
        fields = row.split()
        if (
            len(fields) > 9
            and fields[1].upper() == wanted
            and fields[3] == "0A"
            and fields[9] in socket_inodes
        ):
            return True
    return False


def _wait_for_listener_owner(
    pid: int, boot_id: str, start_ticks: int, port: int, process: Any
) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    identity = {"pid": pid, "boot_id": boot_id, "start_ticks": start_ticks}
    while True:
        if _identity_state(identity) != "live" or process.poll() is not None:
            raise _error("OpenCode server exited before owning its loopback listener")
        if _process_owns_loopback_listener(pid, port):
            return
        if time.monotonic() >= deadline:
            raise _error("OpenCode did not own the allocated loopback listener")
        time.sleep(STARTUP_POLL)


def _require_listener_owner(record: dict[str, Any]) -> None:
    if _identity_state(record) != "live":
        raise _error("OpenCode process identity is not live")
    port = record.get("port")
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not _process_owns_loopback_listener(record["pid"], port)
    ):
        raise _error("OpenCode process does not own the recorded loopback listener")


def _session_id(value: Any) -> str:
    if isinstance(value, str):
        return _validate_string(value, "session id", limit=256)
    if isinstance(value, dict):
        candidates: list[str] = []
        for key in ("id", "session_id", "sessionID", "sessionId"):
            if key in value:
                candidates.append(_session_id(value[key]))
        for key in ("session", "data"):
            if key in value:
                candidates.append(_session_id(value[key]))
        if len(set(candidates)) == 1:
            return candidates[0]
    raise _error("OpenCode did not return exactly one session id")


def _create_session(client: Any, orchestrator: str) -> str:
    method = getattr(client, "create_session", None)
    if method is None:
        raise _error("OpenCode HTTP API lacks create_session")
    try:
        try:
            parameters = list(inspect.signature(method).parameters.values())
        except (TypeError, ValueError):
            parameters = []
        names = {item.name for item in parameters}
        if "agent" in names:
            response = method(agent=orchestrator)
        elif "orchestrator" in names:
            response = method(orchestrator=orchestrator)
        else:
            response = method()
        return _session_id(response)
    except DispatchError:
        raise
    except Exception as exc:
        raise _error("OpenCode session creation failed", exc) from exc


def _handoff(kind: str, task: str, registration: Registration, readiness: dict[str, Any]) -> str:
    if not isinstance(readiness, dict):
        raise _error("readiness result is not a JSON object")
    repository = readiness.get("repository")
    if not isinstance(repository, str) or re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ) is None:
        raise _error("readiness handoff repository identity is malformed")
    payload = {
        "kind": kind,
        "task": task,
        "repository": repository,
        "orchestrator": ORCHESTRATORS[kind],
        "worktree": str(registration.target),
        "readiness": readiness,
    }
    if kind == "maintenance":
        payload["maintenanceSource"] = _maintenance_source_handoff(
            registration, task, readiness
        )
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise _error("readiness handoff is not JSON serializable", exc) from exc
    if len(encoded.encode("utf-8")) > MAX_HANDOFF_BYTES:
        raise _error("readiness handoff exceeds its bounded size")
    return encoded


def _maintenance_source_handoff(
    registration: Registration, task: str, readiness: dict[str, Any]
) -> dict[str, str]:
    """Derive immutable maintenance source inputs from the validated receipt."""
    stage = readiness.get("stage")
    if stage == "pristine":
        raise _error(
            "maintenance dispatch requires a validated source receipt before launch"
        )
    try:
        record = lifecycle.worktree_for_task(registration.main_root, task)
        if stage == "applied":
            receipt = maintenance_lifecycle._validate_active_receipt(record, task)
        else:
            receipt = maintenance_lifecycle._validate_consumed_receipt(record, task)
        source = Path(receipt["source"])
        revision = receipt["source_revision"]
        resolved_source, live_revision = maintenance_lifecycle.upgrade.resolve_pinned_source(
            source
        )
    except Exception as exc:
        raise _error("maintenance source receipt cannot be revalidated", exc) from exc
    if live_revision != revision or readiness.get("sourceRevision") != revision:
        raise _error("maintenance source revision does not match readiness")
    return {"path": str(resolved_source), "revision": revision}


def _prompt_async(client: Any, session_id: str, prompt: str, orchestrator: str) -> None:
    method = getattr(client, "prompt_async", None)
    if method is None:
        raise _error("OpenCode HTTP API lacks prompt_async")
    try:
        try:
            parameters = list(inspect.signature(method).parameters.values())
        except (TypeError, ValueError):
            parameters = []
        names = {item.name for item in parameters}
        if "agent" in names or any(
            item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters
        ):
            result = method(session_id, prompt, agent=orchestrator)
        else:
            result = method(session_id, prompt)
    except Exception as exc:
        raise _error("OpenCode readiness handoff failed", exc) from exc
    if result is not None and not _acknowledged(result):
        raise _error("OpenCode rejected the readiness handoff")


def _readiness(registration: Registration, kind: str, task: str) -> dict[str, Any]:
    if kind == "maintenance":
        if maintenance_lifecycle is None or not hasattr(maintenance_lifecycle, "maintenance_check"):
            raise _error("maintenance_lifecycle.maintenance_check is unavailable")
        try:
            result = maintenance_lifecycle.maintenance_check(registration.main_root, task)
        except Exception as exc:
            raise _error("maintenance readiness check failed", exc) from exc
    else:
        if task_contract is None:
            raise _error("task_contract is unavailable")
        # Prefer the strict resume path for an already-resumable Task.  If the
        # state cannot be inspected (which is common in a mocked harness), use
        # the canonical initial check and then its canonical resume fallback.
        preferred_resume = False
        states = getattr(task_contract, "RESUMABLE_STATES", set())
        try:
            status = lifecycle.state_status(lifecycle.state_path(registration.target))
            preferred_resume = status in states
        except Exception:
            pass
        methods = []
        if preferred_resume:
            methods.append(("resume", getattr(task_contract, "check_resume_contract", None)))
        else:
            methods.append(("initial", getattr(task_contract, "check_contract", None)))
            methods.append(("resume", getattr(task_contract, "check_resume_contract", None)))
        errors: list[str] = []
        result = None
        for mode, method in methods:
            if method is None:
                errors.append(f"{mode} API unavailable")
                continue
            try:
                candidate = method(registration.main_root, task)
                if not isinstance(candidate, dict):
                    raise ValueError("readiness result is not an object")
                result = candidate
                break
            except Exception as exc:
                errors.append(f"{mode}: {str(exc).replace(chr(10), ' ')[:512]}")
        if result is None:
            raise _error("Task readiness check failed; " + "; ".join(errors))
    if not isinstance(result, dict) or result.get("status") != "READY":
        raise _error("readiness check did not return status READY")
    if result.get("task") != task:
        raise _error("readiness Task identity mismatch")
    if not _absolute_equal(result.get("worktree"), registration.target):
        raise _error("readiness worktree mismatch")
    repository = result.get("repository")
    if not isinstance(repository, str) or re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ) is None:
        raise _error("readiness repository identity is malformed")
    if kind == "maintenance":
        if result.get("mode") != "maintenance":
            raise _error("maintenance readiness mode mismatch")
    elif result.get("mode") not in ("initial", "resume"):
        raise _error("Task readiness mode mismatch")
    return result


class StartupFailure(DispatchError):
    """A startup failure carrying only exact, safe process identity evidence."""

    def __init__(
        self,
        message: str,
        *,
        pid: int | None = None,
        boot_id: str | None = None,
        start_ticks: int | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.pid = pid
        self.boot_id = boot_id
        self.start_ticks = start_ticks
        self.session_id = session_id


def _target_implementation(registration: Registration) -> tuple[str, str]:
    version_path = registration.target / ".automation" / "VERSION"
    try:
        metadata = version_path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("implementation version is not a regular file")
        version = version_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise _error("target implementation version is unavailable", exc) from exc
    _validate_string(version, "implementation version", limit=128)
    try:
        revision = lifecycle.git("rev-parse", "HEAD", cwd=registration.target)
    except Exception as exc:
        raise _error("target implementation revision is unavailable", exc) from exc
    if not isinstance(revision, str) or re.fullmatch(
        r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision
    ) is None:
        raise _error("target implementation revision is malformed")
    return version, revision


def _starting_record(
    registration: Registration,
    kind: str,
    task: str,
    readiness: dict[str, Any],
) -> dict[str, Any]:
    implementation_version, implementation_revision = _target_implementation(registration)
    port = _allocate_port()
    credential = secrets.token_urlsafe(32)
    return {
        "schema_version": DISPATCH_SCHEMA_VERSION,
        "kind": kind,
        "task_id": task,
        "repository": readiness["repository"],
        "branch": registration.branch,
        "worktree": str(registration.target),
        "common_git_dir": str(registration.common_git_dir),
        "orchestrator": ORCHESTRATORS[kind],
        "pid": None,
        "boot_id": None,
        "start_ticks": None,
        "port": port,
        "session_id": None,
        "credential": credential,
        "implementation_version": implementation_version,
        "implementation_revision": implementation_revision,
        "state": "starting",
        "failure": None,
    }


def _child_environment(kind: str, credential: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key != "AUTOMATION_MAINTENANCE"
    }
    environment.update(
        {
            PASSWORD_ENV: credential,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if kind == "maintenance":
        environment["AUTOMATION_MAINTENANCE"] = "1"
    return environment


def _spawn(
    registration: Registration,
    kind: str,
    task: str,
    readiness: dict[str, Any],
    starting: dict[str, Any],
    record_path: Path,
) -> dict[str, Any]:
    port = starting["port"]
    credential = starting["credential"]
    environment = _child_environment(kind, credential)
    executable = _opencode_executable(registration.target)
    command = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        "_serve-gated",
        "{gate_fd}",
        str(port),
        str(executable),
    ]
    read_fd, write_fd = os.pipe()
    command[4] = str(read_fd)
    try:
        try:
            process = subprocess.Popen(
                command,
                cwd=registration.target,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(read_fd,),
                start_new_session=True,
            )
        finally:
            os.close(read_fd)
    except Exception as exc:
        os.close(write_fd)
        raise _error("OpenCode server could not be started", exc) from exc
    pid = getattr(process, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        os.close(write_fd)
        raise _error("OpenCode server returned an invalid PID")
    try:
        boot_id = _boot_id()
        start_ticks = _proc_start_ticks(pid)
        if start_ticks is None:
            raise _error("OpenCode server disappeared before identity capture")
        identified = dict(starting)
        identified.update(
            {"pid": pid, "boot_id": boot_id, "start_ticks": start_ticks}
        )
        _write_record(record_path, identified)
        try:
            if os.write(write_fd, b"1") != 1:
                raise OSError("short launch-gate write")
        finally:
            os.close(write_fd)
            write_fd = -1
        _wait_for_listener_owner(pid, boot_id, start_ticks, port, process)
        client = _http_client(port, credential)
        _wait_for_health(client, process, identified)
        _require_listener_owner(identified)
        session_id = _create_session(client, ORCHESTRATORS[kind])
        handoff = _handoff(kind, task, registration, readiness)
        _require_listener_owner(identified)
        _prompt_async(
            client,
            session_id,
            handoff,
            ORCHESTRATORS[kind],
        )
        _assert_spawn_identity(pid, boot_id, start_ticks)
    except DispatchError as exc:
        if write_fd >= 0:
            os.close(write_fd)
        _terminate_spawned(pid, boot_id if "boot_id" in locals() else None, start_ticks if "start_ticks" in locals() else None)
        safe = str(exc).replace(credential, "<redacted>")
        raise StartupFailure(
            safe,
            pid=pid,
            boot_id=boot_id if "boot_id" in locals() else None,
            start_ticks=start_ticks if "start_ticks" in locals() else None,
            session_id=session_id if "session_id" in locals() else None,
        ) from exc
    except Exception as exc:
        if write_fd >= 0:
            os.close(write_fd)
        _terminate_spawned(pid, boot_id if "boot_id" in locals() else None, start_ticks if "start_ticks" in locals() else None)
        safe = str(exc).replace(credential, "<redacted>")
        raise StartupFailure(
            _error("OpenCode readiness failed", RuntimeError(safe)),
            pid=pid,
            boot_id=boot_id if "boot_id" in locals() else None,
            start_ticks=start_ticks if "start_ticks" in locals() else None,
            session_id=session_id if "session_id" in locals() else None,
        ) from exc
    running = dict(starting)
    running.update(
        {
            "pid": pid,
            "boot_id": boot_id,
            "start_ticks": start_ticks,
            "session_id": session_id,
            "state": "running",
            "failure": None,
        }
    )
    return running


def _opencode_executable(target: Path) -> Path:
    selected = shutil.which("opencode")
    if selected is None:
        raise _error("OpenCode executable is unavailable")
    try:
        executable = Path(selected).resolve(strict=True)
        metadata = executable.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error("OpenCode executable cannot be resolved safely", exc) from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(executable, os.X_OK):
        raise _error("OpenCode executable is unavailable or unsafe")
    try:
        executable.relative_to(target.resolve())
    except ValueError:
        pass
    else:
        raise _error("OpenCode executable must not come from the target worktree")
    return executable


def _serve_gated(gate_fd: int, port: int, executable: Path) -> int:
    """Exec OpenCode only after the parent durably records this exact PID."""
    if gate_fd < 0 or not 1 <= port <= 65535:
        return 125
    try:
        release = os.read(gate_fd, 2)
    except OSError:
        return 125
    finally:
        try:
            os.close(gate_fd)
        except OSError:
            pass
    if release != b"1":
        return 125
    command = [
        str(executable),
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    try:
        os.execve(command[0], command, os.environ)
    except OSError:
        return 126
    return 126


def _terminate_spawned(pid: int, boot_id: str | None, start_ticks: int | None) -> None:
    if boot_id is None or start_ticks is None:
        return
    identity = {
        "pid": pid,
        "boot_id": boot_id,
        "start_ticks": start_ticks,
    }
    try:
        _signal_identity(identity, signal.SIGTERM)
        if _wait_stopped(identity, STOP_GRACE_TIMEOUT) != "live":
            return
        # The identity fence is repeated immediately before SIGKILL so a
        # reused PID can never receive a signal from this cleanup path.
        if _identity_state(identity) != "live":
            return
        _signal_identity(identity, signal.SIGKILL)
        _wait_stopped(identity, STOP_KILL_TIMEOUT)
    except (ProcessLookupError, PermissionError, OSError, DispatchError):
        return


def _assert_spawn_identity(pid: int, boot_id: str, start_ticks: int) -> None:
    current_boot = _boot_id()
    current_ticks = _proc_start_ticks(pid)
    if current_boot != boot_id or current_ticks != start_ticks:
        raise _error("PID identity mismatch during readiness")


def _signal_identity(record: dict[str, Any], selected_signal: int) -> None:
    """Signal only a pidfd pinned to the exact persisted Linux process identity."""
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        raise _error("pidfd process signalling is unavailable")
    try:
        descriptor = pidfd_open(record["pid"], 0)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise _error("cannot open the exact process identity", exc) from exc
    try:
        if _identity_state(record) != "live":
            raise _error("PID identity changed before signalling")
        pidfd_send_signal(descriptor, selected_signal, None, 0)
    except ProcessLookupError:
        return
    except DispatchError:
        raise
    except OSError as exc:
        raise _error("cannot signal the exact process identity", exc) from exc
    finally:
        os.close(descriptor)


def _server_identity_payload(
    value: Any, record: dict[str, Any], *, check_session: bool = True
) -> None:
    """Reject explicit identity fields returned by a server/client wrapper."""
    if not isinstance(value, dict):
        return
    values = [value]
    for key in ("data", "session", "result"):
        nested = value.get(key)
        if isinstance(nested, dict):
            values.append(nested)
    for item in values:
        if check_session:
            for key in ("session_id", "sessionID", "sessionId", "id"):
                if key in item and item[key] != record["session_id"]:
                    raise _error("OpenCode session identity mismatch")
        for key in ("port", "server_port"):
            if key in item and item[key] != record["port"]:
                raise _error("OpenCode port identity mismatch")
        for key in ("worktree", "worktree_path"):
            if key in item and not _absolute_equal(item[key], _path(record["worktree"])):
                raise _error("OpenCode worktree identity mismatch")
        for key in ("common_git_dir", "git_common_dir"):
            if key in item and not _absolute_equal(item[key], _path(record["common_git_dir"])):
                raise _error("OpenCode common Git directory identity mismatch")


def _verify_server(client: Any, record: dict[str, Any]) -> None:
    try:
        _require_listener_owner(record)
        health = _call_noarg_alias(client, ("global_health", "health"))
        if not _health_ok(health):
            raise _error("OpenCode health is not ready")
        _server_identity_payload(health, record, check_session=False)
        _require_listener_owner(record)
        status = _call_session(client, "session_status", record["session_id"])
        if status is False:
            raise _error("OpenCode session is unavailable")
        if _session_id(status) != record["session_id"]:
            raise _error("OpenCode session identity is not exact")
        _server_identity_payload(status, record)
    except DispatchError:
        raise
    except Exception as exc:
        raise _error("OpenCode session reconciliation failed", exc) from exc


def _permission_method_call(method: Any, session_id: str) -> Any:
    try:
        parameters = list(inspect.signature(method).parameters.values())
    except (TypeError, ValueError):
        parameters = []
    names = {item.name for item in parameters}
    for name in ("session_id", "sessionID", "sessionId", "session"):
        if name in names:
            return method(**{name: session_id})
    positional = [
        item
        for item in parameters
        if item.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if positional or any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters):
        return method(session_id)
    return method()


def _list_permissions(client: Any, session_id: str) -> list[dict[str, Any]]:
    method = getattr(client, "list_permissions", None)
    if method is None:
        raise _error("OpenCode HTTP API lacks list_permissions")
    try:
        raw = _permission_method_call(method, session_id)
    except Exception as exc:
        raise _error("OpenCode permission listing failed", exc) from exc
    if raw is None:
        return []
    if isinstance(raw, dict):
        if isinstance(raw.get("permissions"), list):
            raw = raw["permissions"]
        elif any(
            key in raw for key in ("id", "permission_id", "permissionID", "permissionId")
        ):
            raw = [raw]
        else:
            # A keyed response is accepted only when every value is a complete
            # permission object; keys are never treated as permission IDs.
            values = list(raw.values())
            if all(isinstance(item, dict) for item in values):
                raw = values
            else:
                raise _error("OpenCode permission listing has an invalid shape")
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise _error("OpenCode permission listing has an invalid shape")
    return list(raw)


def _permission_id(value: dict[str, Any]) -> str | None:
    candidates: list[str] = []
    for key in ("id", "permission_id", "permissionID", "permissionId"):
        if key in value:
            if not isinstance(value[key], str):
                return None
            candidates.append(value[key])
    return candidates[0] if candidates and len(set(candidates)) == 1 else None


def _permission_session(value: dict[str, Any]) -> str | None:
    candidates: list[str] = []
    for key in ("session_id", "sessionID", "sessionId", "session"):
        if key in value:
            if not isinstance(value[key], str):
                return None
            candidates.append(value[key])
    return candidates[0] if candidates and len(set(candidates)) == 1 else None


def _permission_record(value: dict[str, Any], session_id: str) -> dict[str, Any]:
    current_id = _permission_id(value)
    current_session = _permission_session(value)
    if current_id is None or current_session != session_id:
        raise _error("current permission request has an exact session mismatch")
    permission = value.get("permission")
    if not isinstance(permission, str) or not permission:
        raise _error("current permission request has no exact permission name")
    patterns = value.get("patterns")
    single_pattern = value.get("pattern")
    if patterns is None and isinstance(single_pattern, str):
        patterns = [single_pattern]
    elif patterns is not None and "pattern" in value:
        if not isinstance(single_pattern, str) or patterns != [single_pattern]:
            raise _error("current permission request has conflicting pattern data")
    if not isinstance(patterns, list) or any(not isinstance(item, str) for item in patterns):
        raise _error("current permission request has no exact pattern list")
    return _validate_pending(
        {
            "id": current_id,
            "session_id": current_session,
            "permission": permission,
            "patterns": patterns,
        },
        session_id,
    ) or {}  # the validator can only return None for a literal None input


def _current_permission(client: Any, record: dict[str, Any], permission_id: str | None = None) -> dict[str, Any]:
    _require_listener_owner(record)
    requests = _list_permissions(client, record["session_id"])
    if permission_id is not None and any(
        _permission_id(item) == permission_id and _permission_session(item) != record["session_id"]
        for item in requests
    ):
        raise _error("permission request session mismatch")
    session_requests = [
        item for item in requests if _permission_session(item) == record["session_id"]
    ]
    matching_id = [
        item
        for item in session_requests
        if permission_id is None or _permission_id(item) == permission_id
    ]
    if not matching_id:
        raise _error("no current permission request")
    if len(matching_id) != 1:
        raise _error("current permission request is ambiguous")
    return _permission_record(matching_id[0], record["session_id"])


_UNSET = object()


def _persist_state(
    path: Path,
    record: dict[str, Any],
    state: str,
    *,
    pending: object = _UNSET,
    failure: str | None = None,
    clear_process: bool = False,
) -> dict[str, Any]:
    updated = dict(record)
    updated["state"] = state
    updated["failure"] = failure
    if clear_process:
        updated["pid"] = None
        updated["boot_id"] = None
        updated["start_ticks"] = None
    if pending is not _UNSET:
        if pending is None:
            updated.pop("pending_permission", None)
        else:
            updated["pending_permission"] = pending
    _validate_record(
        updated,
        Registration(
            Path("/"),
            Path("/"),
            _path(record["worktree"]),
            record["branch"],
            _path(record["common_git_dir"]),
        ),
        record["kind"],
        record["task_id"],
    )
    _write_record(path, updated)
    return updated


def _persist_pending(
    path: Path, record: dict[str, Any], pending: dict[str, Any] | None
) -> dict[str, Any]:
    return _persist_state(
        path,
        record,
        "permission-pending" if pending is not None else "idle",
        pending=pending,
    )


def _acknowledged(value: Any) -> bool:
    if value is None or value is True:
        return True
    if value is False:
        return False
    if isinstance(value, dict):
        status = value.get("status")
        if isinstance(status, str) and status.casefold() in {"error", "failed", "rejected"}:
            return False
        indicators = [
            value[key] for key in ("ok", "success", "acknowledged", "accepted") if key in value
        ]
        if indicators:
            return all(indicator is True for indicator in indicators)
        return isinstance(status, str) and status.casefold() in {
            "ok",
            "success",
            "accepted",
            "acknowledged",
            "done",
        }
    if isinstance(value, str):
        return value.casefold() in {"true", "ok", "success", "accepted", "acknowledged", "done"}
    return False


def _reply_permission(client: Any, session_id: str, permission_id: str, response: str) -> Any:
    method = getattr(client, "reply", None)
    if method is None:
        method = getattr(client, "reply_permission", None)
    if method is None:
        raise _error("OpenCode HTTP API lacks reply or reply_permission")
    try:
        parameters = list(inspect.signature(method).parameters.values())
    except (TypeError, ValueError):
        parameters = []
    names = {item.name for item in parameters}
    values = {
        "session_id": session_id,
        "sessionID": session_id,
        "sessionId": session_id,
        "session": session_id,
        "permission_id": permission_id,
        "permissionID": permission_id,
        "permissionId": permission_id,
        "id": permission_id,
        "response": response,
        "reply": response,
    }
    if all(name in names for name in ("session_id", "permission_id", "response")):
        return method(session_id=session_id, permission_id=permission_id, response=response)
    if {"sessionID", "permissionID", "response"}.issubset(names):
        return method(sessionID=session_id, permissionID=permission_id, response=response)
    positional = [
        item
        for item in parameters
        if item.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters) or len(positional) >= 3:
        return method(session_id, permission_id, response)
    if len(positional) >= 2:
        return method(permission_id, response)
    kwargs = {name: values[name] for name in names if name in values}
    if kwargs:
        return method(**kwargs)
    return method(permission_id, response)


def start(root: Path | str, kind: str, task: str) -> dict[str, Any]:
    registration = resolve_target(root, kind, task)
    with _transaction(registration, task) as path:
        current = _read_record(path)
        if current is not None:
            current = _validate_record(current, registration, kind, task)
            state = _identity_state(current)
            if current["state"] in {"stopped", "failed"} and state != "live":
                state = "dead"
            if state == "mismatch":
                raise _error("PID identity mismatch")
            if state == "live":
                if current["state"] not in {
                    "starting",
                    "running",
                    "permission-pending",
                    "idle",
                }:
                    raise _error(
                        f"dispatch record state {current['state']} has a live process"
                    )
                if any(
                    current.get(field) is None
                    for field in ("port", "credential", "session_id")
                ):
                    raise _error("live dispatch record has incomplete session identity")
                try:
                    _verify_server(_http_client(current["port"], current["credential"]), current)
                except DispatchError as exc:
                    raise _redact_secret(exc, current["credential"]) from exc
                if current["state"] == "starting":
                    current = _persist_state(path, current, "running", pending=None)
                return {
                    "status": "READY",
                    "state": current["state"],
                    "action": "existing",
                    **_public_record(current),
                }

        readiness = _readiness(registration, kind, task)
        starting = _starting_record(registration, kind, task, readiness)
        _validate_record(starting, registration, kind, task)
        try:
            if current is None:
                _write_record(path, starting, exclusive=True)
            else:
                _write_record(path, starting)
        except DispatchError:
            raise
        try:
            record = _spawn(registration, kind, task, readiness, starting, path)
            _validate_record(record, registration, kind, task)
            _write_record(path, record)
        except DispatchError as exc:
            if "record" in locals():
                _terminate_spawned(record["pid"], record["boot_id"], record["start_ticks"])
            failed = dict(starting)
            if "record" in locals():
                failed.update(
                    {
                        "pid": record["pid"],
                        "boot_id": record["boot_id"],
                        "start_ticks": record["start_ticks"],
                        "session_id": record["session_id"],
                    }
                )
            if isinstance(exc, StartupFailure):
                failed.update(
                    {
                        "pid": exc.pid,
                        "boot_id": exc.boot_id,
                        "start_ticks": exc.start_ticks,
                        "session_id": exc.session_id,
                    }
                )
                if failed["pid"] is None or failed["boot_id"] is None or failed["start_ticks"] is None:
                    failed["pid"] = None
                    failed["boot_id"] = None
                    failed["start_ticks"] = None
            failed["state"] = "failed"
            failed["failure"] = str(exc).replace(failed["credential"], "<redacted>")[:4000]
            failed.pop("pending_permission", None)
            try:
                _validate_record(failed, registration, kind, task)
                _write_record(path, failed)
            except DispatchError as persist_exc:
                raise _error("startup failed and failed-state publication failed", persist_exc) from exc
            raise
        return {
            "status": "READY",
            "state": record["state"],
            "action": "started",
            **_public_record(record),
        }


def status(root: Path | str, kind: str, task: str) -> dict[str, Any]:
    registration = resolve_target(root, kind, task)
    with _transaction(registration, task) as path:
        current = _read_record(path)
        if current is None:
            return {"status": "STOPPED", "state": "absent", "kind": kind, "task": task}
        current = _validate_record(current, registration, kind, task)
        state = _identity_state(current)
        if current["state"] == "failed" and state != "live":
            return {"status": "FAILED", **_public_record(current)}
        if current["state"] == "stopped" and state != "live":
            return {"status": "STOPPED", **_public_record(current)}
        if state == "mismatch":
            raise _error("PID identity mismatch")
        if state == "dead":
            current = _persist_state(
                path, current, "stopped", pending=None, clear_process=True
            )
            return {"status": "STOPPED", "state": "stopped", **_public_record(current)}
        if current["state"] in {"failed", "stopped"}:
            raise _error(f"dispatch record state {current['state']} has a live process")
        if any(
            current.get(field) is None for field in ("port", "credential", "session_id")
        ):
            raise _error("live dispatch record has incomplete session identity")
        try:
            client = _http_client(current["port"], current["credential"])
            _verify_server(client, current)
        except DispatchError as exc:
            raise _redact_secret(exc, current["credential"]) from exc
        pending = None
        try:
            pending = _current_permission(client, current)
        except DispatchError as exc:
            exc = _redact_secret(exc, current["credential"])
            if "no current permission request" not in str(exc):
                raise
        desired_state = "permission-pending" if pending is not None else "idle"
        if current.get("pending_permission") != pending or current["state"] != desired_state:
            current = _persist_state(path, current, desired_state, pending=pending)
        return {
            "status": "READY",
            "state": current["state"],
            **_public_record(current),
            "pending_permission": pending,
        }


def respond(root: Path | str, kind: str, task: str, permission_id: str, action: str) -> dict[str, Any]:
    _validate_permission_id(permission_id)
    if action not in PERMISSION_ACTIONS:
        raise _error("permission response must be once, session, or deny")
    registration = resolve_target(root, kind, task)
    with _transaction(registration, task) as path:
        current = _read_record(path)
        if current is None:
            raise _error("no dispatch record exists")
        current = _validate_record(current, registration, kind, task)
        _require_live(current)
        try:
            client = _http_client(current["port"], current["credential"])
            _verify_server(client, current)
        except DispatchError as exc:
            raise _redact_secret(exc, current["credential"]) from exc
        try:
            pending = _current_permission(client, current, permission_id)
        except DispatchError as exc:
            raise _redact_secret(exc, current["credential"]) from exc
        stored = current.get("pending_permission")
        if stored is None or stored != pending:
            raise _error("current permission request does not match persisted permission metadata")
        try:
            _require_listener_owner(current)
            reply = _reply_permission(
                client, current["session_id"], permission_id, PERMISSION_ACTIONS[action]
            )
        except Exception as exc:
            if isinstance(exc, DispatchError):
                raise _redact_secret(exc, current["credential"]) from exc
            raise _redact_secret(
                _error("OpenCode permission reply failed", exc), current["credential"]
            ) from exc
        if not _acknowledged(reply):
            raise _error("OpenCode did not acknowledge the permission reply")
        if _identity_state(current) != "live":
            raise _error("PID identity changed before clearing the permission request")
        current = _persist_pending(path, current, None)
        return {
            "status": "RESPONDED",
            "kind": kind,
            "task": task,
            "permission_id": permission_id,
            "response": PERMISSION_ACTIONS[action],
            "session_id": current["session_id"],
        }


def _wait_stopped(record: dict[str, Any], timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while True:
        state = _identity_state(record)
        if state == "dead":
            return state
        if state == "mismatch":
            raise _error("PID identity changed while stopping")
        if time.monotonic() >= deadline:
            return state
        time.sleep(STOP_POLL)


def stop(root: Path | str, kind: str, task: str) -> dict[str, Any]:
    registration = resolve_target(root, kind, task)
    with _transaction(registration, task) as path:
        current = _read_record(path)
        if current is None:
            return {"status": "STOPPED", "state": "absent", "kind": kind, "task": task}
        current = _validate_record(current, registration, kind, task)
        state = _identity_state(current)
        if current["state"] == "stopped" and state != "live":
            return {"status": "STOPPED", **_public_record(current)}
        if current["state"] == "failed" and state != "live":
            current = _persist_state(
                path, current, "stopped", pending=None, clear_process=True
            )
            return {"status": "STOPPED", **_public_record(current)}
        if state == "mismatch":
            raise _error("PID identity mismatch")
        if state == "dead":
            current = _persist_state(
                path, current, "stopped", pending=None, clear_process=True
            )
            return {"status": "STOPPED", "state": "stopped", **_public_record(current)}
        _signal_identity(current, signal.SIGTERM)
        if _wait_stopped(current, STOP_GRACE_TIMEOUT) == "live":
            # Re-checking _wait_stopped immediately before SIGKILL is the
            # essential PID-reuse fence; no process-name scan is ever used.
            before_kill = _identity_state(current)
            if before_kill == "dead":
                current = _persist_state(
                    path, current, "stopped", pending=None, clear_process=True
                )
                return {"status": "STOPPED", "state": "stopped", **_public_record(current)}
            if before_kill != "live":
                raise _error("PID identity changed before SIGKILL")
            _signal_identity(current, signal.SIGKILL)
            final = _wait_stopped(current, STOP_KILL_TIMEOUT)
            if final != "dead":
                raise _error("OpenCode process did not stop within the bounded timeout")
        current = _persist_state(
            path, current, "stopped", pending=None, clear_process=True
        )
        return {"status": "STOPPED", "state": "stopped", **_public_record(current)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    start_parser = commands.add_parser("start")
    start_parser.add_argument("kind", choices=KINDS)
    start_parser.add_argument("task")

    status_parser = commands.add_parser("status")
    status_parser.add_argument("kind", choices=KINDS)
    status_parser.add_argument("task")

    respond_parser = commands.add_parser("respond")
    respond_parser.add_argument("kind", choices=KINDS)
    respond_parser.add_argument("task")
    respond_parser.add_argument("permission_id")
    respond_parser.add_argument("action", choices=tuple(PERMISSION_ACTIONS))

    stop_parser = commands.add_parser("stop")
    stop_parser.add_argument("kind", choices=KINDS)
    stop_parser.add_argument("task")

    gated_parser = commands.add_parser("_serve-gated", help=argparse.SUPPRESS)
    gated_parser.add_argument("gate_fd", type=int)
    gated_parser.add_argument("port", type=int)
    gated_parser.add_argument("executable", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "_serve-gated":
        return _serve_gated(args.gate_fd, args.port, args.executable)
    try:
        root = _repo_root()
        if args.command == "start":
            result = start(root, args.kind, args.task)
        elif args.command == "status":
            result = status(root, args.kind, args.task)
        elif args.command == "respond":
            result = respond(root, args.kind, args.task, args.permission_id, args.action)
        else:
            result = stop(root, args.kind, args.task)
    except DispatchError as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())
