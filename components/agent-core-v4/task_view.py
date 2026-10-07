"""Canonical read-only live Task facts and immutable exact-HEAD snapshots.

The live projection observes Git, the existing #191 Task Record/Contract and
explicitly selected #145 Evidence objects.  It does not infer readiness,
prerequisites, next actions, evaluation results, or an implicit disposition.
Snapshot writes are limited to one content-addressed ``task-view-snapshot``
object on the #217 metadata plane; the trusted host authorization callback is
required and no Task pointer is changed.

GitHub and checkpoint facts come together from one trusted host reader; that
reader owns authentication, freshness, time/output bounds, and #194 checkpoint
selection/provenance. This module validates closed fields and metadata bindings.
Dirty status contains counts only. A snapshot is bound to HEAD, not to dirty
file content; different edits with the same counts are not distinguished.
"""

from __future__ import annotations

import base64
import os
import re
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import evidence as evidence_module
import metadata_codec as codec
import task_record
from metadata_ref import MetadataRefError, MetadataStore


SCHEMA_VERSION = 1
SNAPSHOT_KIND = "task-view-snapshot"
SNAPSHOT_BOUNDARIES = frozenset({"turn-end", "explicit-handoff"})
GIT_COMMAND_TIMEOUT_SECONDS = 8.0
MAX_CONFIG_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_STATUS_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_WORKTREE_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_GIT_OUTPUT_BYTES = 64 * 1024
MAX_SELECTED_EVIDENCE = 4096
MAX_WORKTREES = 10_000
MAX_PREVIOUS_CHECKPOINTS = 64
MAX_BRANCH_REF_LENGTH = 1024

_HEX_64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ERROR_CODES = frozenset({
    "callback_failed", "invalid_facts", "binding_mismatch", "ref_absent",
    "observation_failed", "subject_absent", "subject_not_commit", "not_ancestor",
})
_PREVIOUS_ERROR_CODES = frozenset({
    "callback_failed", "invalid_facts", "binding_mismatch",
    "invalid_checkpoint_binding", "checkpoint_unavailable",
})


class TaskViewError(ValueError):
    """A safe, bounded Task View error code; never includes Git stderr."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "task_view_error"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EvidenceRef:
    """An explicitly selected Evidence object and its exact bound subject."""

    evidence_id: str
    subject: str


@dataclass(frozen=True)
class GitHubPullRequestRequest:
    """Exact PR request; the positive PR number is independent of Task ID."""

    repository: str
    task: str
    number: int
    branch_ref: str


@dataclass(frozen=True)
class GitHubPullRequestFacts:
    """Closed externally observed PR fields returned by a trusted host reader.

    ``base_ref`` and ``head_ref`` are GitHub's exact branch-name strings (not
    full refs). Both repository strings must exactly match this staged Task's
    repository. The reader must authenticate and check current GitHub facts.
    """

    base_repository: str
    head_repository: str
    number: int
    state: str
    draft: bool
    base_ref: str
    head_ref: str
    head_oid: str


@dataclass(frozen=True)
class PreviousCheckpoint:
    """Opaque #194 PREVIOUS checkpoint binding supplied by a trusted host.

    This value is descriptive only.  ``checkpoint_id`` is bounded and retained
    as-is; this module does not parse or invent a checkpoint protocol.
    """

    checkpoint_id: str
    subject: str
    metadata_commit: str
    snapshot_id: str


@dataclass(frozen=True)
class GitHubObservation:
    """One trusted reader result, tying optional PREVIOUS data to this PR read."""

    pull_request: GitHubPullRequestFacts
    previous_checkpoint: PreviousCheckpoint | None = None


@dataclass(frozen=True)
class SnapshotAuthorizationRequest:
    """Frozen exact snapshot publication request for trusted host approval."""

    task: str
    repository: str
    subject: str
    snapshot_id: str
    record_id: str
    contract_id: str
    boundary: str


@dataclass(frozen=True)
class SnapshotPublication:
    """Receipt for the exact remotely confirmed metadata object and commit."""

    metadata_commit: str
    snapshot_id: str
    subject: str
    boundary: str


def _require_oid(value: object, oid_length: int, code: str = "invalid_input") -> str:
    if type(value) is not str or len(value) != oid_length or not re.fullmatch(r"[0-9a-f]+", value, re.ASCII):
        raise TaskViewError(code)
    return value


def _require_metadata_id(value: object, code: str = "invalid_input") -> str:
    if type(value) is not str or not _HEX_64.fullmatch(value):
        raise TaskViewError(code)
    return value


def _require_branch_ref(value: object, code: str = "invalid_branch_ref") -> str:
    try:
        branch_ref = task_record.validate_branch_ref(value)
        if len(branch_ref) > MAX_BRANCH_REF_LENGTH:
            raise TaskViewError(code)
        branch_ref.encode("utf-8", errors="strict")
        return branch_ref
    except TaskViewError:
        raise
    except (task_record.TaskRecordError, UnicodeEncodeError):
        raise TaskViewError(code) from None


def _git_output(
    root: Path,
    arguments: Sequence[str],
    *,
    max_stdout: int = MAX_GIT_OUTPUT_BYTES,
    timeout: float = GIT_COMMAND_TIMEOUT_SECONDS,
    check: bool = True,
) -> bytes | None:
    """Run one local Git read with bounded time/output and safe cleanup."""
    if timeout <= 0 or max_stdout < 0:
        raise TaskViewError("invalid_git_limit")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GCM_INTERACTIVE": "never",
    })
    command = [
        "git", "--no-replace-objects",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", "core.pager=cat",
        "-c", "core.attributesFile=/dev/null",
        "-C", str(root), *arguments,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            bufsize=0,
            start_new_session=True,
        )
    except OSError:
        raise TaskViewError("git_unavailable") from None

    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    output = bytearray()
    timed_out = False
    over_limit = False
    stdout_open = True
    deadline = time.monotonic() + timeout

    def terminate_group() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass

    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        while stdout_open or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                terminate_group()
                break
            events = selector.select(min(remaining, 0.1))
            if not events and process.poll() is not None and stdout_open:
                # A final readable event normally carries EOF; keep polling
                # briefly rather than treating process exit as output EOF.
                continue
            for _key, _mask in events:
                try:
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(64 * 1024, max_stdout - len(output) + 1),
                    )
                except OSError:
                    raise TaskViewError("git_output_failed") from None
                if not chunk:
                    selector.unregister(process.stdout)
                    stdout_open = False
                    continue
                if len(output) + len(chunk) > max_stdout:
                    over_limit = True
                    terminate_group()
                    break
                output.extend(chunk)
            if over_limit:
                break
        if timed_out or over_limit:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                terminate_group()
                process.wait(timeout=5)
        else:
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_group()
                process.wait(timeout=5)
    except TaskViewError:
        raise
    except Exception:
        raise TaskViewError("git_execution_failed") from None
    finally:
        if process.poll() is None:
            terminate_group()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if stdout_open:
            try:
                selector.unregister(process.stdout)
            except (KeyError, ValueError):
                pass
        try:
            process.stdout.close()
        except OSError:
            pass
        selector.close()

    if timed_out:
        raise TaskViewError("git_timeout")
    if over_limit:
        raise TaskViewError("git_output_limit")
    if process.returncode != 0:
        if check or process.returncode != 1:
            raise TaskViewError("git_command_failed")
        return None
    return bytes(output)


def _check_git_configuration(root: Path) -> None:
    """Reject repository-local command hooks, filters, and transport overrides.

    Global/system config and includes are disabled for the probe.  The status
    reader also disables fsmonitor and optional index locks on every command.
    """
    def keys(scope: str) -> set[str]:
        raw = _git_output(
            root,
            ("config", scope, "--no-includes", "--name-only", "--null", "--list"),
            max_stdout=MAX_CONFIG_OUTPUT_BYTES,
        )
        assert raw is not None
        found: set[str] = set()
        for item in raw.split(b"\0"):
            if not item:
                continue
            try:
                found.add(item.decode("ascii", errors="strict").lower())
            except UnicodeDecodeError:
                raise TaskViewError("unsafe_git_config") from None
        return found

    local_keys = keys("--local")
    configured_keys = set(local_keys)
    # Repositories using per-worktree configuration can put command-bearing
    # settings in .git/config.worktree rather than the common config file.
    if "extensions.worktreeconfig" in local_keys:
        configured_keys.update(keys("--worktree"))
    for key in configured_keys:
        unsafe = (
            key.startswith(("include.", "includeif."))
        ) or (
            key.startswith("url.") and key.endswith((".insteadof", ".pushinsteadof"))
        ) or key in {"core.gitproxy", "core.sshcommand"} or (
            key.startswith("protocol.") and key.endswith(".allow")
        ) or (
            key.startswith("remote.") and key.endswith((".vcs", ".uploadpack", ".receivepack"))
        ) or (
            key.startswith("filter.") and key.endswith((".clean", ".smudge", ".process"))
        ) or (
            key.startswith("diff.") and key.endswith((".command", ".external"))
        ) or key == "core.attributesfile"
        if unsafe:
            raise TaskViewError("unsafe_git_config")


def _reject_incomplete_git_history(root: Path) -> None:
    """Do not report ancestry counts from shallow or grafted history."""
    shallow = _git_output(root, ("rev-parse", "--is-shallow-repository"), max_stdout=32)
    assert shallow is not None
    if shallow.strip() != b"false":
        raise TaskViewError("incomplete_git_history")
    path_output = _git_output(root, ("rev-parse", "--git-path", "info/grafts"), max_stdout=16 * 1024)
    assert path_output is not None
    try:
        path_text = path_output.decode("utf-8", errors="strict").strip()
        grafts_path = Path(path_text)
        if not grafts_path.is_absolute():
            grafts_path = root / grafts_path
        grafts_path.lstat()
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError, ValueError):
        raise TaskViewError("incomplete_git_history") from None
    raise TaskViewError("incomplete_git_history")


def _oid_output(output: bytes, oid_length: int) -> str:
    try:
        value = output.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError:
        raise TaskViewError("invalid_git_output") from None
    return _require_oid(value, oid_length, "invalid_git_output")


def _parse_status(output: bytes) -> dict[str, Any]:
    records = output.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    index_changes = 0
    worktree_changes = 0
    untracked = 0
    conflicts = 0
    submodule_changes = 0
    position = 0
    while position < len(records):
        record = records[position]
        position += 1
        if record.startswith(b"1 "):
            fields = record.split(b" ", 8)
            if len(fields) != 9 or len(fields[1]) != 2:
                raise TaskViewError("invalid_git_output")
            xy = fields[1]
            submodule = fields[2]
        elif record.startswith(b"2 "):
            fields = record.split(b" ", 9)
            if len(fields) != 10 or len(fields[1]) != 2 or position >= len(records):
                raise TaskViewError("invalid_git_output")
            xy = fields[1]
            submodule = fields[2]
            # In -z mode a rename/copy has the original path as the next NUL
            # record; it is deliberately not copied into the view.
            position += 1
        elif record.startswith(b"u "):
            fields = record.split(b" ", 10)
            if len(fields) != 11 or len(fields[1]) != 2:
                raise TaskViewError("invalid_git_output")
            xy = fields[1]
            submodule = fields[2]
            conflicts += 1
        elif record.startswith(b"? "):
            if len(record) < 3:
                raise TaskViewError("invalid_git_output")
            untracked += 1
            continue
        else:
            raise TaskViewError("invalid_git_output")
        if any(character not in b". MADRCUT?!" for character in xy):
            raise TaskViewError("invalid_git_output")
        if submodule == b"N...":
            pass
        elif len(submodule) == 4 and submodule.startswith(b"S"):
            if any(character != ord(".") for character in submodule[1:]):
                submodule_changes += 1
        else:
            raise TaskViewError("invalid_git_output")
        if xy[0:1] != b".":
            index_changes += 1
        if xy[1:2] != b".":
            worktree_changes += 1
    return {
        "index_changes": index_changes,
        "worktree_changes": worktree_changes,
        "untracked": untracked,
        "conflicts": conflicts,
        "submodule_changes": submodule_changes,
    }


def _parse_worktrees(output: bytes, oid_length: int) -> list[dict[str, Any]]:
    fields = output.split(b"\0")
    result: list[dict[str, Any]] = []
    current: dict[str, Any] = {}

    def finish() -> None:
        nonlocal current
        if not current:
            return
        if "path_bytes" not in current:
            raise TaskViewError("invalid_git_output")
        if "head" not in current:
            if current.get("bare", False):
                current["head"] = None
            else:
                raise TaskViewError("invalid_git_output")
        if current.get("detached", False) and current.get("branch_ref") is not None:
            raise TaskViewError("invalid_git_output")
        if current.get("bare", False) and (
            current.get("branch_ref") is not None or current.get("detached", False)
        ):
            raise TaskViewError("invalid_git_output")
        if not current.get("bare", False) and current.get("branch_ref") is None and not current.get("detached", False):
            raise TaskViewError("invalid_git_output")
        path_bytes = current.pop("path_bytes")
        current["path_b64"] = base64.b64encode(path_bytes).decode("ascii")
        result.append(current)
        current = {}

    for field in fields:
        if not field:
            finish()
            continue
        if field.startswith(b"worktree "):
            finish()
            path_bytes = field[len(b"worktree "):]
            if not path_bytes:
                raise TaskViewError("invalid_git_output")
            current = {"path_bytes": path_bytes}
            continue
        if not current:
            raise TaskViewError("invalid_git_output")
        if field.startswith(b"HEAD "):
            if "head" in current:
                raise TaskViewError("invalid_git_output")
            current["head"] = _oid_output(field[len(b"HEAD "):], oid_length)
        elif field.startswith(b"branch "):
            if "branch_ref" in current:
                raise TaskViewError("invalid_git_output")
            try:
                branch_ref = field[len(b"branch "):].decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise TaskViewError("invalid_git_output") from None
            current["branch_ref"] = _require_branch_ref(branch_ref, "invalid_git_output")
        elif field == b"detached":
            current["detached"] = True
        elif field == b"bare":
            current["bare"] = True
        elif field == b"locked" or field.startswith(b"locked "):
            current["locked"] = True
        elif field == b"prunable" or field.startswith(b"prunable "):
            current["prunable"] = True
        else:
            raise TaskViewError("invalid_git_output")
    finish()
    result.sort(key=lambda entry: (entry["path_b64"], entry["head"] or "", entry.get("branch_ref", "")))
    if len({entry["path_b64"] for entry in result}) != len(result):
        raise TaskViewError("invalid_git_output")
    return result


def _staged_gitlinks(root: Path, oid_length: int) -> int:
    """Count index-vs-HEAD gitlinks without visiting any child worktree."""
    output = _git_output(
        root,
        ("diff", "--cached", "--raw", "--no-abbrev", "-z", "--no-renames",
         "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", "HEAD", "--"),
        max_stdout=MAX_STATUS_OUTPUT_BYTES,
    )
    assert output is not None
    records = output.split(b"\0")
    if records[-1] != b"" or (len(records) - 1) % 2:
        raise TaskViewError("invalid_git_output")
    count = 0
    for position in range(0, len(records) - 1, 2):
        header = records[position].split()
        if len(header) != 5 or not header[0].startswith(b":") or not records[position + 1]:
            raise TaskViewError("invalid_git_output")
        for oid in header[2:4]:
            _oid_output(oid, oid_length)
        if header[0][1:] == b"160000" or header[1] == b"160000":
            count += 1
    return count


def _default_branch(root: Path, ref: str | None, head: str, oid_length: int) -> dict[str, Any]:
    if ref is None:
        return {"state": "not_requested"}
    try:
        output = _git_output(
            root, ("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"), check=False,
        )
        if output is None:
            return {"state": "unavailable", "error_code": "ref_absent"}
        default_oid = _oid_output(output, oid_length)
        counts = _git_output(root, ("rev-list", "--left-right", "--count", f"{default_oid}...{head}"))
        assert counts is not None
        values = counts.decode("ascii", errors="strict").split()
        if len(values) != 2 or any(not value.isdecimal() for value in values):
            raise TaskViewError("invalid_git_output")
        default_only, head_only = (int(value) for value in values)
        if default_only == 0 and head_only == 0:
            relation = "equal"
        elif default_only == 0:
            relation = "ahead"
        elif head_only == 0:
            relation = "behind"
        else:
            relation = "diverged"
        return {
            "state": "observed",
            "ref": ref,
            "head_oid": default_oid,
            "relation": relation,
            "default_only_commits": default_only,
            "task_only_commits": head_only,
        }
    except TaskViewError:
        return {"state": "unavailable", "error_code": "observation_failed"}
    except (UnicodeDecodeError, ValueError):
        return {"state": "unavailable", "error_code": "observation_failed"}


def _remote_head(store: MetadataStore, branch_ref: str, oid_length: int, requested: bool) -> dict[str, Any]:
    if not requested:
        return {"state": "not_requested"}
    try:
        # MetadataStore has no public remote-branch observer. Reuse its bounded,
        # scrubbed Git runner only for an exact read-only ls-remote query; never
        # consult local remote-tracking refs, which may be stale.
        store._check_transport_configuration()
        output = store._git(
            ["ls-remote", "--refs", store.remote, branch_ref],
            check=True,
            max_stdout=4096,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
        assert output is not None
    except Exception:
        return {"state": "unavailable", "error_code": "observation_failed"}
    if not output:
        return {"state": "unavailable", "error_code": "ref_absent"}
    lines = output.splitlines()
    if len(lines) != 1:
        return {"state": "unavailable", "error_code": "observation_failed"}
    fields = lines[0].split(b"\t")
    if len(fields) != 2 or fields[1] != branch_ref.encode("utf-8"):
        return {"state": "unavailable", "error_code": "observation_failed"}
    try:
        oid = _oid_output(fields[0], oid_length)
    except TaskViewError:
        return {"state": "unavailable", "error_code": "observation_failed"}
    return {"state": "observed", "head_oid": oid}


def _commits_after(root: Path, previous_subject: str, current_subject: str) -> dict[str, Any]:
    try:
        _reject_incomplete_git_history(root)
        # cat-file errors are not proof of absence. An unavailable object can
        # reflect corrupt storage or I/O failure; report only what was observed.
        object_type = _git_output(root, ("cat-file", "-t", previous_subject))
        if object_type is None:
            return {"state": "unknown", "reason": "observation_failed"}
        if object_type.strip() != b"commit":
            return {"state": "unknown", "reason": "subject_not_commit"}
        ancestor = _git_output(
            root,
            ("merge-base", "--is-ancestor", previous_subject, current_subject),
            check=False,
        )
        if ancestor is None:
            return {"state": "unknown", "reason": "not_ancestor"}
        output = _git_output(root, ("rev-list", "--count", f"{previous_subject}..{current_subject}"))
        assert output is not None
        count = output.decode("ascii", errors="strict").strip()
        if not count.isdecimal():
            raise TaskViewError("invalid_git_output")
        return {"state": "observed", "count": int(count)}
    except (TaskViewError, UnicodeDecodeError):
        return {"state": "unknown", "reason": "observation_failed"}


def _observe_git(
    store: MetadataStore,
    *,
    branch_ref: str,
    default_branch_ref: str | None,
    observe_remote_head: bool,
) -> dict[str, Any]:
    root = store.root
    _check_git_configuration(root)
    _reject_incomplete_git_history(root)
    format_output = _git_output(root, ("rev-parse", "--show-object-format"), max_stdout=128)
    assert format_output is not None
    format_name = format_output.decode("ascii", errors="strict").strip()
    oid_length = {"sha1": 40, "sha256": 64}.get(format_name)
    if oid_length is None:
        raise TaskViewError("unsupported_git_object_format")
    head_output = _git_output(root, ("rev-parse", "--verify", "HEAD^{commit}"))
    tree_output = _git_output(root, ("rev-parse", "--verify", "HEAD^{tree}"))
    assert head_output is not None and tree_output is not None
    head = _oid_output(head_output, oid_length)
    tree = _oid_output(tree_output, oid_length)
    branch_output = _git_output(root, ("symbolic-ref", "--quiet", "HEAD"), check=False)
    if branch_output is None:
        current_branch_ref = None
    else:
        try:
            branch_text = branch_output.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            raise TaskViewError("invalid_git_output") from None
        current_branch_ref = _require_branch_ref(branch_text, "invalid_git_output")
    status_output = _git_output(
        root,
        # Child repositories have separate command-bearing filter config. Do
        # not recurse into their worktrees under a superproject-only preflight.
        ("--no-optional-locks", "status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=all"),
        max_stdout=MAX_STATUS_OUTPUT_BYTES,
    )
    worktree_output = _git_output(
        root, ("worktree", "list", "--porcelain", "-z"), max_stdout=MAX_WORKTREE_OUTPUT_BYTES,
    )
    assert status_output is not None and worktree_output is not None
    git_facts = {
        "head": head,
        "tree": tree,
        "branch_ref": current_branch_ref,
        "branch_matches_task": current_branch_ref == branch_ref,
        "status": _parse_status(status_output),
        "submodule_worktrees": "not_inspected",
        "staged_gitlinks": _staged_gitlinks(root, oid_length),
        "worktrees": _parse_worktrees(worktree_output, oid_length),
        "default_branch": _default_branch(root, default_branch_ref, head, oid_length),
        "remote_head": _remote_head(store, branch_ref, oid_length, observe_remote_head),
    }
    return git_facts


def _normalize_evidence_refs(values: Sequence[EvidenceRef], oid_length: int) -> tuple[EvidenceRef, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise TaskViewError("invalid_evidence_selection")
    if len(values) > MAX_SELECTED_EVIDENCE:
        raise TaskViewError("evidence_selection_limit")
    captured: list[EvidenceRef] = []
    seen: set[str] = set()
    for value in values:
        if type(value) is not EvidenceRef:
            raise TaskViewError("invalid_evidence_selection")
        evidence_id = _require_metadata_id(value.evidence_id, "invalid_evidence_selection")
        subject = _require_oid(value.subject, oid_length, "invalid_evidence_selection")
        if evidence_id in seen:
            raise TaskViewError("duplicate_evidence_id")
        seen.add(evidence_id)
        captured.append(EvidenceRef(evidence_id, subject))
    return tuple(sorted(captured, key=lambda item: (item.evidence_id, item.subject)))


def _validate_previous_input(value: PreviousCheckpoint | None, oid_length: int) -> PreviousCheckpoint | None:
    if value is None:
        return None
    if type(value) is not PreviousCheckpoint:
        raise TaskViewError("invalid_previous_checkpoint")
    checkpoint_id = value.checkpoint_id
    if (
        type(checkpoint_id) is not str or not checkpoint_id or len(checkpoint_id) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in checkpoint_id)
    ):
        raise TaskViewError("invalid_previous_checkpoint")
    return PreviousCheckpoint(
        checkpoint_id,
        _require_oid(value.subject, oid_length, "invalid_previous_checkpoint"),
        _require_oid(value.metadata_commit, oid_length, "invalid_previous_checkpoint"),
        _require_metadata_id(value.snapshot_id, "invalid_previous_checkpoint"),
    )


def _validate_github_facts(
    facts: object,
    *,
    request: GitHubPullRequestRequest,
    repository: str,
    branch_ref: str,
    oid_length: int,
) -> dict[str, Any]:
    if type(facts) is not GitHubPullRequestFacts:
        raise TaskViewError("invalid_facts")
    if (
        type(facts.base_repository) is not str
        or facts.base_repository != repository
        or type(facts.head_repository) is not str
        or facts.head_repository != repository
        or type(facts.number) is not int
        or facts.number != request.number
        or type(facts.state) is not str
        or facts.state not in {"open", "closed", "merged"}
        or type(facts.draft) is not bool
        or type(facts.base_ref) is not str
        or type(facts.head_ref) is not str
        or facts.head_ref != branch_ref[len("refs/heads/"):]
    ):
        raise TaskViewError("binding_mismatch")
    for branch_name in (facts.base_ref, facts.head_ref):
        if not branch_name:
            raise TaskViewError("invalid_facts")
        _require_branch_ref("refs/heads/" + branch_name, "invalid_facts")
    head_oid = _require_oid(facts.head_oid, oid_length, "invalid_facts")
    return {
        "state": "observed",
        "pull_request": {
            "base_repository": facts.base_repository,
            "head_repository": facts.head_repository,
            "number": facts.number,
            "state": facts.state,
            "draft": facts.draft,
            "base_ref": facts.base_ref,
            "head_ref": facts.head_ref,
            "head_oid": head_oid,
        },
    }


def _read_authority(
    store: MetadataStore,
    *,
    metadata_commit: str,
    task: str,
    base_revision: str,
    branch_ref: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    records = task_record.TaskRecords(store, authorize=lambda _request: False)
    resolved = records.read(
        metadata_commit, task=task, base_revision=base_revision, branch_ref=branch_ref,
    )
    if resolved is None:
        raise TaskViewError("authority_absent")
    return resolved


def _authority_projection(
    store: MetadataStore,
    metadata_commit: str,
    record_id: str,
    record: dict[str, Any],
    contract: dict[str, Any],
    *,
    task: str,
    base_revision: str,
    branch_ref: str,
) -> dict[str, Any]:
    contract_id = record["payload"]["contract_id"]
    if (
        contract["kind"] != "contract"
        or contract["task"] != task
        or contract["repository"] != store.repository
        or contract["subject"] != base_revision
        or contract_id != codec.encode_object(
            "contract", store.repository, task, base_revision, contract["payload"]
        )[0]
    ):
        raise TaskViewError("invalid_authority")
    disposition = record["payload"].get("disposition")
    return {
        "state": "observed",
        "metadata_commit": metadata_commit,
        "record_id": record_id,
        "contract_id": contract_id,
        "base_revision": base_revision,
        "branch_ref": branch_ref,
        "disposition": disposition,
    }


def _validate_snapshot_graph(
    store: MetadataStore,
    view: dict[str, Any],
    *,
    root_snapshot_id: str | None = None,
) -> None:
    """Revalidate each immutable pinned graph with one shared bounded deadline."""
    pending: list[dict[str, Any]] = [view]
    visited_ids = {root_snapshot_id} if root_snapshot_id is not None else set()
    traversed = 0
    try:
        with store.validation_scope():
            while pending:
                current = pending.pop()
                authority = current["authority"]
                commit = authority["metadata_commit"]
                task = current["task"]
                resolved = _read_authority(
                    store,
                    metadata_commit=commit,
                    task=task,
                    base_revision=authority["base_revision"],
                    branch_ref=authority["branch_ref"],
                )
                record_id, record, contract = resolved
                if (
                    record_id != authority["record_id"]
                    or record["payload"]["contract_id"] != authority["contract_id"]
                    or record["payload"].get("disposition") != authority["disposition"]
                    or contract["kind"] != "contract"
                ):
                    raise TaskViewError("authority_binding_mismatch")
                reader = evidence_module.Evidence(store)
                for selected in current["evidence"]:
                    reader.read(
                        commit,
                        selected["evidence_id"],
                        task=task,
                        subject=selected["subject"],
                    )
                previous = current["previous_checkpoint"]
                if previous["state"] != "observed":
                    continue
                if previous["snapshot_id"] in visited_ids:
                    raise TaskViewError("previous_checkpoint_cycle")
                if traversed >= MAX_PREVIOUS_CHECKPOINTS:
                    raise TaskViewError("previous_checkpoint_limit")
                visited_ids.add(previous["snapshot_id"])
                traversed += 1
                prior = _read_previous_snapshot_object(
                    store,
                    PreviousCheckpoint(
                        previous["checkpoint_id"],
                        previous["subject"],
                        previous["metadata_commit"],
                        previous["snapshot_id"],
                    ),
                    task,
                )
                pending.append(prior["view"])
    except TaskViewError:
        raise
    except Exception:
        # Store/owner exceptions can include transport or repository details.
        raise TaskViewError("snapshot_graph_invalid") from None


def _validate_projection(view: object, *, repository: str, task: str, subject: str) -> dict[str, Any]:
    if type(view) is not dict or view.keys() != {
        "schema_version", "repository", "task", "subject", "branch_ref", "git",
        "authority", "evidence", "github", "previous_checkpoint",
    }:
        raise TaskViewError("invalid_snapshot_schema")
    if (
        type(view["schema_version"]) is not int or view["schema_version"] != SCHEMA_VERSION
        or view["repository"] != repository or view["task"] != task or view["subject"] != subject
    ):
        raise TaskViewError("snapshot_binding_mismatch")
    _require_branch_ref(view["branch_ref"], "invalid_snapshot_schema")
    git = view["git"]
    if type(git) is not dict or git.keys() != {
        "head", "tree", "branch_ref", "branch_matches_task", "status", "worktrees",
        "default_branch", "remote_head", "submodule_worktrees", "staged_gitlinks",
    }:
        raise TaskViewError("invalid_snapshot_schema")
    oid_length = len(subject)
    _require_oid(git["head"], oid_length, "invalid_snapshot_schema")
    _require_oid(git["tree"], oid_length, "invalid_snapshot_schema")
    if git["head"] != subject:
        raise TaskViewError("snapshot_binding_mismatch")
    if git["submodule_worktrees"] != "not_inspected":
        raise TaskViewError("invalid_snapshot_schema")
    if type(git["staged_gitlinks"]) is not int or git["staged_gitlinks"] < 0:
        raise TaskViewError("invalid_snapshot_schema")
    current_branch = git["branch_ref"]
    if current_branch is not None:
        _require_branch_ref(current_branch, "invalid_snapshot_schema")
    if type(git["branch_matches_task"]) is not bool or git["branch_matches_task"] != (current_branch == view["branch_ref"]):
        raise TaskViewError("invalid_snapshot_schema")
    status = git["status"]
    if type(status) is not dict or status.keys() != {
        "index_changes", "worktree_changes", "untracked", "conflicts", "submodule_changes",
    }:
        raise TaskViewError("invalid_snapshot_schema")
    for field in ("index_changes", "worktree_changes", "untracked", "conflicts", "submodule_changes"):
        if type(status[field]) is not int or status[field] < 0:
            raise TaskViewError("invalid_snapshot_schema")
    if type(git["worktrees"]) is not list or len(git["worktrees"]) > MAX_WORKTREES:
        raise TaskViewError("invalid_snapshot_schema")
    seen_worktrees: set[str] = set()
    for entry in git["worktrees"]:
        if type(entry) is not dict or entry.keys() - {
            "path_b64", "head", "branch_ref", "detached", "bare", "locked", "prunable",
        } or not {"path_b64", "head"} <= entry.keys():
            raise TaskViewError("invalid_snapshot_schema")
        try:
            path_bytes = base64.b64decode(entry["path_b64"], validate=True)
        except (TypeError, ValueError):
            raise TaskViewError("invalid_snapshot_schema") from None
        if not path_bytes or base64.b64encode(path_bytes).decode("ascii") != entry["path_b64"]:
            raise TaskViewError("invalid_snapshot_schema")
        if entry["path_b64"] in seen_worktrees:
            raise TaskViewError("invalid_snapshot_schema")
        seen_worktrees.add(entry["path_b64"])
        if entry["head"] is None:
            if entry.get("bare") is not True:
                raise TaskViewError("invalid_snapshot_schema")
        else:
            _require_oid(entry["head"], oid_length, "invalid_snapshot_schema")
        branch = entry.get("branch_ref")
        if branch is not None:
            _require_branch_ref(branch, "invalid_snapshot_schema")
        for flag in ("detached", "bare", "locked", "prunable"):
            if flag in entry and type(entry[flag]) is not bool:
                raise TaskViewError("invalid_snapshot_schema")
        if entry.get("detached", False) and branch is not None:
            raise TaskViewError("invalid_snapshot_schema")
        if entry.get("bare", False) and (branch is not None or entry.get("detached", False)):
            raise TaskViewError("invalid_snapshot_schema")
        if not entry.get("bare", False) and branch is None and not entry.get("detached", False):
            raise TaskViewError("invalid_snapshot_schema")
    worktree_order = [
        (entry["path_b64"], entry["head"] or "", entry.get("branch_ref", ""))
        for entry in git["worktrees"]
    ]
    if worktree_order != sorted(worktree_order):
        raise TaskViewError("invalid_snapshot_schema")
    _validate_observation(git["default_branch"], "default_branch", oid_length)
    _validate_observation(git["remote_head"], "remote_head", oid_length)

    authority = view["authority"]
    if type(authority) is not dict or authority.keys() != {
        "state", "metadata_commit", "record_id", "contract_id", "base_revision", "branch_ref", "disposition",
    } or authority["state"] != "observed":
        raise TaskViewError("invalid_snapshot_schema")
    _require_oid(authority["metadata_commit"], oid_length, "invalid_snapshot_schema")
    _require_metadata_id(authority["record_id"], "invalid_snapshot_schema")
    _require_metadata_id(authority["contract_id"], "invalid_snapshot_schema")
    _require_oid(authority["base_revision"], oid_length, "invalid_snapshot_schema")
    if authority["branch_ref"] != view["branch_ref"]:
        raise TaskViewError("snapshot_binding_mismatch")
    record_payload: dict[str, Any] = {
        "schema_version": 1,
        "branch_ref": authority["branch_ref"],
        "contract_id": authority["contract_id"],
    }
    if authority["disposition"] is not None:
        record_payload["disposition"] = authority["disposition"]
    try:
        computed_record_id, _data = task_record.encode_record(
            repository, task, authority["base_revision"], record_payload,
        )
    except (TypeError, ValueError):
        raise TaskViewError("invalid_snapshot_schema") from None
    if computed_record_id != authority["record_id"]:
        raise TaskViewError("snapshot_binding_mismatch")

    evidence_values = view["evidence"]
    if type(evidence_values) is not list or len(evidence_values) > MAX_SELECTED_EVIDENCE:
        raise TaskViewError("invalid_snapshot_schema")
    previous_key: tuple[str, str] | None = None
    seen_evidence: set[str] = set()
    for selected in evidence_values:
        if type(selected) is not dict or selected.keys() != {"evidence_id", "subject", "matches_current_head"}:
            raise TaskViewError("invalid_snapshot_schema")
        evidence_id = _require_metadata_id(selected["evidence_id"], "invalid_snapshot_schema")
        evidence_subject = _require_oid(selected["subject"], oid_length, "invalid_snapshot_schema")
        if type(selected["matches_current_head"]) is not bool or selected["matches_current_head"] != (evidence_subject == subject):
            raise TaskViewError("invalid_snapshot_schema")
        key = (evidence_id, evidence_subject)
        if previous_key is not None and key <= previous_key:
            raise TaskViewError("invalid_snapshot_schema")
        if evidence_id in seen_evidence:
            raise TaskViewError("invalid_snapshot_schema")
        previous_key = key
        seen_evidence.add(evidence_id)

    _validate_github_projection(
        view["github"], oid_length, repository=repository, branch_ref=view["branch_ref"],
    )
    _validate_previous_projection(view["previous_checkpoint"], oid_length)
    return view


def _validate_observation(value: object, field: str, oid_length: int) -> None:
    if type(value) is not dict or type(value.get("state")) is not str:
        raise TaskViewError("invalid_snapshot_schema")
    state = value["state"]
    if state == "not_requested":
        if value.keys() != {"state"}:
            raise TaskViewError("invalid_snapshot_schema")
        return
    if state == "unavailable":
        if (
            value.keys() != {"state", "error_code"}
            or type(value["error_code"]) is not str
            or value["error_code"] not in _ERROR_CODES
        ):
            raise TaskViewError("invalid_snapshot_schema")
        return
    if state != "observed":
        raise TaskViewError("invalid_snapshot_schema")
    if field == "remote_head":
        if value.keys() != {"state", "head_oid"}:
            raise TaskViewError("invalid_snapshot_schema")
        _require_oid(value["head_oid"], oid_length, "invalid_snapshot_schema")
        return
    if value.keys() != {
        "state", "ref", "head_oid", "relation", "default_only_commits", "task_only_commits",
    }:
        raise TaskViewError("invalid_snapshot_schema")
    _require_branch_ref(value["ref"], "invalid_snapshot_schema")
    _require_oid(value["head_oid"], oid_length, "invalid_snapshot_schema")
    if type(value["relation"]) is not str or value["relation"] not in {"equal", "ahead", "behind", "diverged"}:
        raise TaskViewError("invalid_snapshot_schema")
    for count in (value["default_only_commits"], value["task_only_commits"]):
        if type(count) is not int or count < 0:
            raise TaskViewError("invalid_snapshot_schema")
    expected_relation = (
        "equal" if value["default_only_commits"] == value["task_only_commits"] == 0
        else "ahead" if value["default_only_commits"] == 0
        else "behind" if value["task_only_commits"] == 0
        else "diverged"
    )
    if value["relation"] != expected_relation:
        raise TaskViewError("invalid_snapshot_schema")


def _validate_github_projection(
    value: object,
    oid_length: int,
    *,
    repository: str,
    branch_ref: str,
) -> None:
    if type(value) is not dict or type(value.get("state")) is not str:
        raise TaskViewError("invalid_snapshot_schema")
    if value["state"] == "not_requested":
        if value.keys() != {"state"}:
            raise TaskViewError("invalid_snapshot_schema")
        return
    if value["state"] == "unavailable":
        if (
            value.keys() != {"state", "error_code"}
            or type(value["error_code"]) is not str
            or value["error_code"] not in _PREVIOUS_ERROR_CODES - {"invalid_checkpoint_binding", "checkpoint_unavailable"}
        ):
            raise TaskViewError("invalid_snapshot_schema")
        return
    if value["state"] != "observed" or value.keys() != {"state", "pull_request"}:
        raise TaskViewError("invalid_snapshot_schema")
    pr = value["pull_request"]
    if type(pr) is not dict or pr.keys() != {
        "base_repository", "head_repository", "number", "state", "draft",
        "base_ref", "head_ref", "head_oid",
    }:
        raise TaskViewError("invalid_snapshot_schema")
    if (
        type(pr["base_repository"]) is not str or pr["base_repository"] != repository
        or type(pr["head_repository"]) is not str or pr["head_repository"] != repository
        or type(pr["number"]) is not int or pr["number"] < 1
        or type(pr["state"]) is not str or pr["state"] not in {"open", "closed", "merged"}
        or type(pr["draft"]) is not bool
        or pr["head_ref"] != branch_ref[len("refs/heads/"):]
    ):
        raise TaskViewError("invalid_snapshot_schema")
    for name in (pr["base_ref"], pr["head_ref"]):
        if type(name) is not str or not name:
            raise TaskViewError("invalid_snapshot_schema")
        _require_branch_ref("refs/heads/" + name, "invalid_snapshot_schema")
    _require_oid(pr["head_oid"], oid_length, "invalid_snapshot_schema")


def _validate_previous_projection(value: object, oid_length: int) -> None:
    if type(value) is not dict or type(value.get("state")) is not str:
        raise TaskViewError("invalid_snapshot_schema")
    if value["state"] == "not_requested":
        if value.keys() != {"state"}:
            raise TaskViewError("invalid_snapshot_schema")
        return
    if value["state"] == "absent":
        if value.keys() != {"state"}:
            raise TaskViewError("invalid_snapshot_schema")
        return
    if value["state"] == "unavailable":
        if (
            value.keys() != {"state", "error_code"}
            or type(value["error_code"]) is not str
            or value["error_code"] not in _PREVIOUS_ERROR_CODES
        ):
            raise TaskViewError("invalid_snapshot_schema")
        return
    if value["state"] != "observed" or value.keys() != {
        "state", "checkpoint_id", "subject", "metadata_commit", "snapshot_id", "commits_after",
    }:
        raise TaskViewError("invalid_snapshot_schema")
    checkpoint_id = value["checkpoint_id"]
    if (
        type(checkpoint_id) is not str or not checkpoint_id or len(checkpoint_id) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in checkpoint_id)
    ):
        raise TaskViewError("invalid_snapshot_schema")
    _require_oid(value["subject"], oid_length, "invalid_snapshot_schema")
    _require_oid(value["metadata_commit"], oid_length, "invalid_snapshot_schema")
    _require_metadata_id(value["snapshot_id"], "invalid_snapshot_schema")
    after = value["commits_after"]
    if type(after) is not dict or type(after.get("state")) is not str:
        raise TaskViewError("invalid_snapshot_schema")
    if after["state"] == "observed":
        if after.keys() != {"state", "count"} or type(after["count"]) is not int or after["count"] < 0:
            raise TaskViewError("invalid_snapshot_schema")
    elif after["state"] == "unknown":
        if (
            after.keys() != {"state", "reason"}
            or type(after["reason"]) is not str
            or after["reason"] not in {
                "subject_absent", "subject_not_commit", "not_ancestor", "observation_failed",
            }
        ):
            raise TaskViewError("invalid_snapshot_schema")
    else:
        raise TaskViewError("invalid_snapshot_schema")


def encode_snapshot(
    repository: str,
    task: str,
    subject: str,
    boundary: str,
    view: dict[str, Any],
) -> tuple[str, bytes]:
    """Encode a closed snapshot whose metadata object ID binds exact subject."""
    if type(boundary) is not str or boundary not in SNAPSHOT_BOUNDARIES:
        raise TaskViewError("invalid_snapshot_boundary")
    _validate_projection(view, repository=repository, task=task, subject=subject)
    payload = {"schema_version": SCHEMA_VERSION, "boundary": boundary, "view": view}
    try:
        return codec.encode_object(SNAPSHOT_KIND, repository, task, subject, payload)
    except codec.MetadataCodecError:
        raise TaskViewError("invalid_snapshot_schema") from None


def decode_snapshot(
    data: bytes,
    *,
    snapshot_id: str,
    repository: str,
    task: str,
    subject: str,
) -> dict[str, Any]:
    """Decode a content-addressed snapshot and reject every extra schema field."""
    try:
        envelope = codec.decode_object(
            data,
            expected_id=snapshot_id,
            expected_repository=repository,
            expected_task=task,
            expected_subject=subject,
            expected_kind=SNAPSHOT_KIND,
        )
    except codec.MetadataCodecError:
        raise TaskViewError("invalid_snapshot_identity") from None
    payload = envelope["payload"]
    if type(payload) is not dict or payload.keys() != {"schema_version", "boundary", "view"}:
        raise TaskViewError("invalid_snapshot_schema")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise TaskViewError("unsupported_snapshot_schema")
    if type(payload["boundary"]) is not str or payload["boundary"] not in SNAPSHOT_BOUNDARIES:
        raise TaskViewError("invalid_snapshot_boundary")
    view = _validate_projection(payload["view"], repository=repository, task=task, subject=subject)
    return {"schema_version": SCHEMA_VERSION, "boundary": payload["boundary"], "view": view}


def _read_previous_snapshot_object(
    store: MetadataStore,
    previous: PreviousCheckpoint,
    task: str,
) -> dict[str, Any]:
    try:
        envelope = store.read_object(
            previous.metadata_commit,
            SNAPSHOT_KIND,
            previous.snapshot_id,
            task=task,
            subject=previous.subject,
        )
        data = codec.encode_object(
            envelope["kind"], envelope["repository"], envelope["task"],
            envelope["subject"], envelope["payload"],
        )[1]
        snapshot = decode_snapshot(
            data,
            snapshot_id=previous.snapshot_id,
            repository=store.repository,
            task=task,
            subject=previous.subject,
        )
        return snapshot
    except TaskViewError:
        raise
    except Exception:
        raise TaskViewError("previous_checkpoint_invalid") from None


def _validate_previous_snapshot(store: MetadataStore, previous: PreviousCheckpoint, task: str) -> dict[str, Any]:
    with store.validation_scope():
        snapshot = _read_previous_snapshot_object(store, previous, task)
        _validate_snapshot_graph(store, snapshot["view"], root_snapshot_id=previous.snapshot_id)
        return snapshot


def _call_github_reader(
    reader: Callable[[GitHubPullRequestRequest], object] | None,
    request: GitHubPullRequestRequest | None,
    *,
    repository: str,
    branch_ref: str,
    oid_length: int,
) -> tuple[dict[str, Any], PreviousCheckpoint | None, dict[str, Any] | None]:
    """Read one trusted external observation; host bounds time and output."""
    if reader is None:
        return {"state": "not_requested"}, None, {"state": "not_requested"}
    assert request is not None
    try:
        observation = reader(request)
    except Exception:
        return (
            {"state": "unavailable", "error_code": "callback_failed"},
            None,
            {"state": "unavailable", "error_code": "callback_failed"},
        )
    if type(observation) is not GitHubObservation:
        return (
            {"state": "unavailable", "error_code": "invalid_facts"},
            None,
            {"state": "unavailable", "error_code": "invalid_facts"},
        )
    try:
        github = _validate_github_facts(
            observation.pull_request,
            request=request,
            repository=repository,
            branch_ref=branch_ref,
            oid_length=oid_length,
        )
    except TaskViewError as error:
        return (
            {"state": "unavailable", "error_code": error.code},
            None,
            {"state": "unavailable", "error_code": error.code},
        )
    if observation.previous_checkpoint is None:
        return github, None, {"state": "absent"}
    try:
        previous = _validate_previous_input(observation.previous_checkpoint, oid_length)
    except TaskViewError:
        return github, None, {"state": "unavailable", "error_code": "invalid_checkpoint_binding"}
    assert previous is not None
    return github, previous, None


def _previous_checkpoint_projection(
    store: MetadataStore,
    previous: PreviousCheckpoint | None,
    fallback: dict[str, Any] | None,
    *,
    task: str,
    current_head: str,
) -> dict[str, Any]:
    if previous is None:
        assert fallback is not None
        return fallback
    try:
        _validate_previous_snapshot(store, previous, task)
    except Exception:
        return {"state": "unavailable", "error_code": "checkpoint_unavailable"}
    return {
        "state": "observed",
        "checkpoint_id": previous.checkpoint_id,
        "subject": previous.subject,
        "metadata_commit": previous.metadata_commit,
        "snapshot_id": previous.snapshot_id,
        "commits_after": _commits_after(store.root, previous.subject, current_head),
    }


def _assert_authority_unchanged(
    store: MetadataStore,
    *,
    task: str,
    base_revision: str,
    branch_ref: str,
    record_id: str,
    contract_id: str,
) -> None:
    """Read the current pointer without rebinding the captured metadata pin."""
    try:
        with store.validation_scope():
            current_tip = store.fetch_tip()
            if current_tip is None:
                raise TaskViewError("authority_changed")
            current_record_id, current_record, _contract = _read_authority(
                store,
                metadata_commit=current_tip,
                task=task,
                base_revision=base_revision,
                branch_ref=branch_ref,
            )
            if (
                current_record_id != record_id
                or current_record["payload"]["contract_id"] != contract_id
            ):
                raise TaskViewError("authority_changed")
    except TaskViewError:
        raise
    except Exception:
        raise TaskViewError("authority_recheck_failed") from None


def observe_live_task(
    store: MetadataStore,
    *,
    task: str,
    branch_ref: str,
    base_revision: str,
    selected_evidence: Sequence[EvidenceRef] = (),
    default_branch_ref: str | None = None,
    observe_remote_head: bool = False,
    pr_number: int | None = None,
    github_reader: Callable[[GitHubPullRequestRequest], object] | None = None,
) -> dict[str, Any]:
    """Return the deterministic canonical factual projection for what is true now.

    Evidence is selected only by explicit exact IDs/subjects supplied by the
    caller.  This function does not enumerate, rank, or select Evidence records.
    The Task Record and Contract are resolved by #191 at one pinned #217
    metadata commit; a missing or invalid authority is a fail-closed error.
    A GitHub callback and explicit positive PR number must be supplied together.
    The trusted reader owns callback time/output bounds and PREVIOUS selection.
    Git, GitHub, and metadata are sequential observations, not an atomic
    cross-system snapshot. Dirty status is counts-only, so same-count edits are
    indistinguishable; ``subject`` remains exact Git HEAD, not dirty content.
    """
    try:
        _require_branch_ref(branch_ref)
        if type(task) is not str or not re.fullmatch(r"[1-9][0-9]{0,127}", task, re.ASCII):
            raise TaskViewError("invalid_task")
        if default_branch_ref is not None:
            _require_branch_ref(default_branch_ref)
        if type(observe_remote_head) is not bool:
            raise TaskViewError("invalid_remote_head_request")
        if github_reader is not None and not callable(github_reader):
            raise TaskViewError("invalid_github_reader")
        if (github_reader is None) != (pr_number is None):
            raise TaskViewError("github_reader_requires_pr_number")
        if pr_number is not None and (type(pr_number) is not int or pr_number <= 0):
            raise TaskViewError("invalid_pr_number")
        git = _observe_git(
            store,
            branch_ref=branch_ref,
            default_branch_ref=default_branch_ref,
            observe_remote_head=observe_remote_head,
        )
        oid_length = len(git["head"])
        base_revision = _require_oid(base_revision, oid_length, "invalid_base_revision")
        refs = _normalize_evidence_refs(selected_evidence, oid_length)
    except TaskViewError:
        raise
    except (MetadataRefError, task_record.TaskRecordError, TypeError, ValueError):
        raise TaskViewError("invalid_live_task_request") from None

    request = (
        None if pr_number is None
        else GitHubPullRequestRequest(store.repository, task, pr_number, branch_ref)
    )
    github, previous, previous_fallback = _call_github_reader(
        github_reader,
        request,
        repository=store.repository,
        branch_ref=branch_ref,
        oid_length=oid_length,
    )
    evidence_facts: list[dict[str, Any]] = []
    try:
        with store.validation_scope():
            metadata_commit = store.fetch_tip()
            if metadata_commit is None:
                raise TaskViewError("metadata_ref_absent")
            resolved = _read_authority(
                store,
                metadata_commit=metadata_commit,
                task=task,
                base_revision=base_revision,
                branch_ref=branch_ref,
            )
            record_id, record, contract = resolved
            authority = _authority_projection(
                store,
                metadata_commit,
                record_id,
                record,
                contract,
                task=task,
                base_revision=base_revision,
                branch_ref=branch_ref,
            )
            evidence_reader = evidence_module.Evidence(store)
            for selected in refs:
                evidence_reader.read(
                    metadata_commit,
                    selected.evidence_id,
                    task=task,
                    subject=selected.subject,
                )
                evidence_facts.append({
                    "evidence_id": selected.evidence_id,
                    "subject": selected.subject,
                    "matches_current_head": selected.subject == git["head"],
                })
    except TaskViewError:
        raise
    except Exception:
        raise TaskViewError("authority_or_evidence_unavailable") from None
    previous_facts = _previous_checkpoint_projection(
        store,
        previous,
        previous_fallback,
        task=task,
        current_head=git["head"],
    )

    view = {
        "schema_version": SCHEMA_VERSION,
        "repository": store.repository,
        "task": task,
        "subject": git["head"],
        "branch_ref": branch_ref,
        "git": git,
        "authority": authority,
        "evidence": evidence_facts,
        "github": github,
        "previous_checkpoint": previous_facts,
    }
    final_git = _observe_git(
        store,
        branch_ref=branch_ref,
        default_branch_ref=default_branch_ref,
        observe_remote_head=observe_remote_head,
    )
    if final_git != git:
        raise TaskViewError("live_git_state_changed")
    _assert_authority_unchanged(
        store,
        task=task,
        base_revision=authority["base_revision"],
        branch_ref=branch_ref,
        record_id=authority["record_id"],
        contract_id=authority["contract_id"],
    )
    return _validate_projection(view, repository=store.repository, task=task, subject=git["head"])


class TaskViewSnapshots:
    """Trusted-host-gated facade for exact immutable Task View snapshots.

    The only durable write is one ``task-view-snapshot`` object.  The generic
    MetadataStore writer is deliberately not exposed by this facade, and no
    pointer, disposition, checkpoint, or product ref is changed here.
    """

    def __init__(self, store: MetadataStore, *, authorize: Callable[[SnapshotAuthorizationRequest], bool]) -> None:
        if not callable(authorize):
            raise TaskViewError("snapshot_authorization_required")
        self._store = store
        self._authorize = authorize

    def capture(
        self,
        *,
        task: str,
        branch_ref: str,
        base_revision: str,
        boundary: str,
        selected_evidence: Sequence[EvidenceRef] = (),
        default_branch_ref: str | None = None,
        observe_remote_head: bool = False,
        pr_number: int | None = None,
        github_reader: Callable[[GitHubPullRequestRequest], object] | None = None,
        on_candidate: Callable[[str], object] | None = None,
    ) -> SnapshotPublication:
        """Persist one exact-HEAD snapshot after repeated factual observations.

        Status checks compare counts only. A mutation that preserves those
        counts is not detected, and the snapshot never claims dirty-file identity.
        Trusted GitHub readers must bound their own callback time and output.
        Integrations needing uncertain-publication recovery should provide
        ``on_candidate`` to journal the exact candidate; #140 owns that protocol.
        """
        if type(boundary) is not str or boundary not in SNAPSHOT_BOUNDARIES:
            raise TaskViewError("invalid_snapshot_boundary")
        if on_candidate is not None and not callable(on_candidate):
            raise TaskViewError("invalid_candidate_callback")
        # Detach caller-owned sequences before invoking any trusted callback.
        if not isinstance(selected_evidence, Sequence) or isinstance(selected_evidence, (str, bytes, bytearray)):
            raise TaskViewError("invalid_evidence_selection")
        if len(selected_evidence) > MAX_SELECTED_EVIDENCE:
            raise TaskViewError("evidence_selection_limit")
        selection = tuple(selected_evidence)
        view = observe_live_task(
            self._store,
            task=task,
            branch_ref=branch_ref,
            base_revision=base_revision,
            selected_evidence=selection,
            default_branch_ref=default_branch_ref,
            observe_remote_head=observe_remote_head,
            pr_number=pr_number,
            github_reader=github_reader,
        )
        subject = view["subject"]
        snapshot_id, snapshot_data = encode_snapshot(
            self._store.repository, task, subject, boundary, view,
        )
        authority = view["authority"]
        request = SnapshotAuthorizationRequest(
            task,
            self._store.repository,
            subject,
            snapshot_id,
            authority["record_id"],
            authority["contract_id"],
            boundary,
        )
        try:
            allowed = self._authorize(request) is True
        except Exception:
            raise TaskViewError("snapshot_authorization_failed") from None
        if not allowed:
            raise TaskViewError("snapshot_authorization_denied")

        request_external = (
            None if pr_number is None
            else GitHubPullRequestRequest(self._store.repository, task, pr_number, branch_ref)
        )

        def guard() -> None:
            current_git = _observe_git(
                self._store,
                branch_ref=branch_ref,
                default_branch_ref=default_branch_ref,
                observe_remote_head=observe_remote_head,
            )
            if current_git != view["git"]:
                raise TaskViewError("live_git_state_changed")
            current_github, current_previous, previous_fallback = _call_github_reader(
                github_reader,
                request_external,
                repository=self._store.repository,
                branch_ref=branch_ref,
                oid_length=len(subject),
            )
            current_previous_facts = _previous_checkpoint_projection(
                self._store,
                current_previous,
                previous_fallback,
                task=task,
                current_head=subject,
            )
            if current_github != view["github"] or current_previous_facts != view["previous_checkpoint"]:
                raise TaskViewError("external_observation_changed")
            _assert_authority_unchanged(
                self._store,
                task=task,
                base_revision=authority["base_revision"],
                branch_ref=branch_ref,
                record_id=authority["record_id"],
                contract_id=authority["contract_id"],
            )

        def candidate_guard(candidate: str) -> None:
            guard()
            # The host can journal the exact #217 candidate before it becomes
            # remotely reachable. Production checkpoint recovery belongs to
            # #140; this callback is only an intent seam, not GitHub authority.
            if on_candidate is not None:
                try:
                    on_candidate(candidate)
                except Exception:
                    # Abort before publication but keep host exception details
                    # (which may contain credentials) out of Task View errors.
                    raise TaskViewError("candidate_intent_failed") from None
            guard()

        # Counts are factual status only: an edit that preserves all counts is
        # not detected. The subject remains exact Git HEAD, never dirty content.
        guard()
        try:
            with self._store.validation_scope():
                candidate = self._store.publish([snapshot_data], on_candidate=candidate_guard)
                confirmed = self._store.confirm(
                    candidate,
                    required_objects=((SNAPSHOT_KIND, snapshot_id, task, subject),),
                )
                if confirmed != candidate:
                    raise TaskViewError("snapshot_publication_unconfirmed")
                # Required even for MetadataStore's no-op path, which does not call
                # on_candidate. It also detects external changes after confirmation.
                guard()
                self.read(candidate, snapshot_id, task=task, subject=subject)
        except TaskViewError:
            raise
        except Exception:
            raise TaskViewError("snapshot_publication_unconfirmed") from None
        return SnapshotPublication(candidate, snapshot_id, subject, boundary)

    def read(self, metadata_commit: str, snapshot_id: str, *, task: str, subject: str) -> dict[str, Any]:
        """Read one exact object and validate its full bounded graph in one scope."""
        try:
            with self._store.validation_scope():
                envelope = self._store.read_object(
                    metadata_commit,
                    SNAPSHOT_KIND,
                    snapshot_id,
                    task=task,
                    subject=subject,
                )
                data = codec.encode_object(
                    envelope["kind"], envelope["repository"], envelope["task"],
                    envelope["subject"], envelope["payload"],
                )[1]
                snapshot = decode_snapshot(
                    data,
                    snapshot_id=snapshot_id,
                    repository=self._store.repository,
                    task=task,
                    subject=subject,
                )
                _validate_snapshot_graph(
                    self._store, snapshot["view"], root_snapshot_id=snapshot_id,
                )
            return snapshot
        except TaskViewError:
            raise
        except Exception:
            raise TaskViewError("snapshot_read_failed") from None


__all__ = [
    "EvidenceRef",
    "GitHubObservation",
    "GitHubPullRequestFacts",
    "GitHubPullRequestRequest",
    "PreviousCheckpoint",
    "SNAPSHOT_BOUNDARIES",
    "SNAPSHOT_KIND",
    "SCHEMA_VERSION",
    "SnapshotAuthorizationRequest",
    "SnapshotPublication",
    "TaskViewError",
    "TaskViewSnapshots",
    "decode_snapshot",
    "encode_snapshot",
    "observe_live_task",
]
