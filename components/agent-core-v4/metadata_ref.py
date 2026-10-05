"""Git transport for the Agent Core v4 metadata reference.

This module owns only ``refs/agentcore/metadata``.  It never stages or checks
out product files, and it never updates a product ref.  Metadata trees are
built with a private index and published only through a trusted direct-ref CAS
capability for that one fixed metadata ref.
"""

from __future__ import annotations

import hashlib
import functools
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import metadata_codec as codec


METADATA_REF = "refs/agentcore/metadata"
MAX_OBJECT_BYTES = codec.MAX_PAYLOAD_BYTES + 4096
MAX_TREE_ENTRIES = 50_000
MAX_TREE_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_TREE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_COMMIT_BYTES = 1024 * 1024
MAX_HISTORY_COMMITS = 100_000
MAX_HISTORY_TOTAL_BYTES = 512 * 1024 * 1024
MAX_HISTORY_VALIDATION_SECONDS = 60.0
MAX_PUBLISH_ATTEMPTS = 4
MAX_OBSERVE_ATTEMPTS = 4
MAX_TASK_ID_LENGTH = 128
GIT_COMMAND_TIMEOUT_SECONDS = 60.0

_OBJECT_DOMAIN = b"agentcore-metadata-object/v1\n"
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TASK = re.compile(r"[1-9][0-9]*\Z", re.ASCII)
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
_OBJECT_TREE_PATH = re.compile(
    r"objects/(task-record|contract|evidence|task-view-snapshot)/([0-9a-f]{2})/([0-9a-f]{64})\.json\Z",
    re.ASCII,
)
_RECORD_PATH = re.compile(r"tasks/([1-9][0-9]{0,127})/record\Z", re.ASCII)
_OBJECT_ID_IN_POINTER = re.compile(rb"([0-9a-f]{64})\n\Z", re.ASCII)


class MetadataRefError(RuntimeError):
    """The metadata ref or one of its objects is invalid or unavailable."""


class MetadataConflictError(MetadataRefError):
    """A compare/exchange or compatible-rebase precondition did not hold."""


@dataclass(frozen=True)
class _TreeEntry:
    path: str
    mode: str
    object_type: str
    oid: str


@dataclass(frozen=True)
class _StoredObject:
    path: str
    kind: str
    object_id: str
    blob_oid: str
    data: bytes
    envelope: dict[str, Any]


@dataclass(frozen=True)
class _TreeSnapshot:
    entries: dict[str, _TreeEntry]
    objects: dict[str, _StoredObject]
    pointers: dict[str, str]


@dataclass(frozen=True)
class _Commit:
    tree: str
    parent: str | None
    raw_size: int


def _bounded_metadata_operation(operation: Callable[..., Any]) -> Callable[..., Any]:
    """Share one validation deadline across nested Git/history checks per call."""
    @functools.wraps(operation)
    def bounded(self: MetadataStore, *args: Any, **kwargs: Any) -> Any:
        previous = self._validation_deadline.get()
        deadline = previous if previous is not None else time.monotonic() + MAX_HISTORY_VALIDATION_SECONDS
        token = self._validation_deadline.set(deadline)
        try:
            return operation(self, *args, **kwargs)
        finally:
            self._validation_deadline.reset(token)

    return bounded


class MetadataStore:
    """Read and publish canonical v4 metadata through one dedicated Git ref.

    ``root`` is a product Git worktree and ``remote`` is a configured remote
    name or a Git transport URL/path.  The remote is used only for the fixed
    metadata ref; callers cannot supply refspecs.
    """

    def __init__(
        self,
        root: Path,
        repository: str,
        remote: str = "origin",
        *,
        direct_ref_cas: Callable[[str, str | None], bool] | None = None,
    ) -> None:
        """Create a reader and, optionally, a narrowly authorized metadata writer.

        A mutating publish requires a trusted host-provided ``direct_ref_cas``
        capability. It must atomically update only the exact direct ref
        ``refs/agentcore/metadata`` (never dereferencing a symbolic ref), compare
        against the supplied expected old OID, and accept the candidate only
        when its parent is that expected OID. This is a trusted server-side
        mutation primitive, not a general Git push abstraction or a guarantee
        that this client can provide itself. Without it, reads remain available
        and mutating publishes fail closed.
        """
        if not isinstance(root, Path):
            raise TypeError("root must be a pathlib.Path")
        if direct_ref_cas is not None and not callable(direct_ref_cas):
            raise MetadataRefError("direct_ref_cas must be callable")
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as error:
            raise MetadataRefError("repository root is not accessible") from error
        if not resolved_root.is_dir():
            raise MetadataRefError("repository root must be a directory")
        if (
            type(repository) is not str
            or len(repository) > 512
            or not _REPOSITORY.fullmatch(repository)
        ):
            raise MetadataRefError("repository must be a canonical owner/name identity")
        if (
            type(remote) is not str
            or not remote
            or len(remote) > 8192
            or remote.startswith("-")
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in remote)
        ):
            raise MetadataRefError("remote must be a non-empty remote name, URL, or path")

        self.root = resolved_root
        self.repository = repository
        self.remote = remote
        self._direct_ref_cas = direct_ref_cas
        self._validation_deadline: ContextVar[float | None] = ContextVar(
            "agentcore_metadata_validation_deadline", default=None
        )

        try:
            object_format = self._git(["rev-parse", "--show-object-format"], max_stdout=128)
        except MetadataRefError as error:
            raise MetadataRefError("root is not an accessible Git worktree") from error
        format_name = object_format.decode("ascii", errors="strict").strip()
        if format_name == "sha1":
            self._oid_length = 40
        elif format_name == "sha256":
            self._oid_length = 64
        else:
            raise MetadataRefError("unsupported Git object format")

        # Resolve the worktree once to reject a bare repository supplied as a
        # product root.  No refs, index, or worktree files are changed here.
        self._git(["rev-parse", "--show-toplevel"], max_stdout=16 * 1024)
        self._check_transport_configuration()

    def _check_transport_configuration(self) -> None:
        """Reject local settings that can silently redirect or execute transport commands."""
        risky_settings = (
            ("^url\\.", "URL rewrite"),
            ("^remote\\..*\\.vcs$", "custom remote helper"),
            ("^core\\.gitproxy$", "custom Git proxy command"),
            ("^protocol\\..*\\.allow$", "custom transport protocol"),
        )
        for expression, label in risky_settings:
            configured = self._git(
                ["config", "--local", "--get-regexp", expression],
                check=False,
                max_stdout=64 * 1024,
            )
            if configured:
                raise MetadataRefError(
                    f"repository Git config contains a {label}; refusing transport integration risk"
                )

        remote_name = self._configured_remote_name()
        if remote_name is not None:
            for key in ("receivepack", "uploadpack"):
                command = self._git(
                    ["config", "--local", "--get-all", f"remote.{remote_name}.{key}"],
                    check=False,
                    max_stdout=64 * 1024,
                )
                if command:
                    raise MetadataRefError(
                        f"repository Git config contains a custom {key} command; refusing transport integration risk"
                    )
            push_urls = self._git(
                ["config", "--local", "--get-all", f"remote.{remote_name}.pushurl"],
                check=False,
                max_stdout=64 * 1024,
            )
            if push_urls:
                raise MetadataRefError(
                    "repository Git config contains remote pushurl destinations; refusing transport integration risk"
                )
            push_options = self._git(
                ["config", "--local", "--get-all", f"remote.{remote_name}.pushoption"],
                check=False,
                max_stdout=64 * 1024,
            )
            if push_options:
                raise MetadataRefError(
                    "repository Git config contains remote push options; refusing transport integration risk"
                )

    def _configured_remote_name(self) -> str | None:
        remote = self.remote
        if (
            remote.startswith(("/", "./", "../", "file://"))
            or re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", remote)
            or (":" in remote and not Path(remote).exists())
        ):
            return None
        return remote

    @_bounded_metadata_operation
    def publish(
        self,
        objects: Sequence[bytes],
        record_updates: Mapping[str, tuple[str | None, str]] = {},
        *,
        on_candidate: Callable[[str], object] | None = None,
    ) -> str:
        """Publish immutable objects and Task Record pointer CAS updates.

        The returned OID is the exact metadata commit proven reachable from the
        remote ref after publication.  An empty call returns the already-valid
        remote tip; it does not create an empty commit or initialize a missing
        ref. When provided, ``on_candidate`` is called with each exact candidate
        OID after local validation and immediately before its direct-ref CAS
        attempt. A callback failure prevents that mutation attempt.
        """
        if on_candidate is not None and not callable(on_candidate):
            raise MetadataRefError("on_candidate must be callable")
        incoming = self._prepare_objects(objects)
        updates = self._prepare_updates(record_updates)
        if len(incoming) + len(updates) > MAX_TREE_ENTRIES:
            raise MetadataRefError("metadata publish request exceeds the combined entry limit")
        if not incoming and not updates:
            tip = self._observe_remote()
            if tip is None:
                raise MetadataRefError("empty publish requires an existing valid remote metadata tip")
            self._validate_full_history(tip)
            return tip

        for attempt in range(MAX_PUBLISH_ATTEMPTS):
            base = self._observe_remote()
            base_snapshot = self._validate_full_history(base) if base is not None else self._empty_snapshot()
            self._validate_request_against_tree(incoming, updates, base_snapshot)

            has_changes = any(path not in base_snapshot.objects for path in incoming)
            for task, (_expected, proposed) in updates.items():
                if base_snapshot.pointers.get(task) != proposed:
                    has_changes = True
            if not has_changes:
                # All requested bytes and pointers are already in this exact,
                # observed remote tree; returning its tip is a confirmed no-op.
                if base is None:
                    raise MetadataRefError("metadata objects cannot be reachable without a remote tip")
                self._validate_requested_tree(base_snapshot, incoming, updates)
                return base

            if self._direct_ref_cas is None:
                raise MetadataRefError(
                    "metadata publication requires a trusted direct-ref CAS capability"
                )

            candidate = self._create_commit(base, base_snapshot, incoming, updates)
            candidate_commit = self._read_commit(candidate)
            if candidate_commit.parent != base:
                raise MetadataRefError("constructed metadata commit has an unexpected parent")
            candidate_snapshot = self._read_tree_snapshot(candidate)
            self._validate_requested_tree(candidate_snapshot, incoming, updates)
            if base is not None:
                self._validate_tree_transition(base_snapshot, candidate_snapshot, protected_tasks={})

            # A caller can durably record the exact intent before this process
            # crosses the remote side-effect boundary. If the callback fails,
            # no ref mutation is attempted.
            if on_candidate is not None:
                on_candidate(candidate)

            mutation_succeeded = self._push_commit(candidate, base)
            try:
                current = self._observe_remote()
            except MetadataRefError as error:
                # A successful CAS response is deliberately not sufficient.
                # Without an observable remote ref there is no durable result
                # to return to a checkpointing caller.
                raise MetadataRefError("published metadata commit could not be confirmed remotely") from error

            current_snapshot = self._validate_full_history(current) if current is not None else None
            if current is not None and current_snapshot is not None and self._validate_reachable(candidate, current):
                self._validate_requested_tree(candidate_snapshot, incoming, updates)
                return candidate

            if current == base:
                if mutation_succeeded:
                    # The server may have acknowledged the CAS before its ref
                    # became visible to ls-remote. Re-observe and retry, but
                    # never report success until reachability is proven.
                    continue
                raise MetadataRefError("metadata CAS failed and the remote tip did not advance")

            if current is None:
                raise MetadataConflictError("remote metadata ref was removed or rewound during publication")

            assert current_snapshot is not None
            if base is None:
                if self._read_commit(current).parent is not None:
                    raise MetadataConflictError(
                        "metadata ref was concurrently initialized from non-genesis history"
                    )
                # Two writers may observe the absent ref and race to create its
                # parentless genesis.  Accept only a fully valid append-only
                # genesis tree; disjoint updates can then be rebuilt on it.
                self._validate_tree_transition(
                    base_snapshot,
                    current_snapshot,
                    protected_tasks=updates,
                )
            else:
                self._validate_compatible_advance(
                    base,
                    current,
                    base_snapshot,
                    current_snapshot,
                    protected_tasks=updates,
                )
            self._check_expected_pointers(updates, current_snapshot)
            if attempt + 1 == MAX_PUBLISH_ATTEMPTS:
                break

        raise MetadataConflictError("metadata publication exceeded the bounded compatible-retry limit")

    @_bounded_metadata_operation
    def fetch_tip(self) -> str | None:
        """Fetch and validate the exact remote metadata ref, without tracking refs."""
        tip = self._observe_remote()
        if tip is not None:
            self._validate_full_history(tip)
        return tip

    @_bounded_metadata_operation
    def confirm(
        self,
        commit: str,
        *,
        required_objects: Sequence[tuple[str, str, str, str]] = (),
    ) -> str:
        """Confirm a previously recorded exact candidate without substituting the tip.

        ``required_objects`` contains ``(kind, object_id, task, subject)`` tuples.
        The candidate must be reachable from the current remote metadata ref and
        its own tree must contain each exact, identity-validated object. The
        exact candidate OID (not a potentially newer ref tip) is returned. A
        restart should pass its durable candidate OID here; ``fetch_tip`` is not
        a substitute because it returns the current remote tip, not that intent.
        """
        self._validate_git_oid(commit, "candidate commit")
        required = self._prepare_required_objects(required_objects)
        tip = self._observe_remote()
        if tip is None:
            raise MetadataConflictError("metadata candidate is not reachable from an existing remote ref")

        tip_snapshot = self._validate_full_history(tip)
        candidate_snapshot = self._read_tree_snapshot(commit)
        if commit != tip:
            try:
                self._validate_compatible_advance(
                    commit,
                    tip,
                    candidate_snapshot,
                    tip_snapshot,
                    protected_tasks={},
                )
            except MetadataConflictError as error:
                raise MetadataConflictError("metadata candidate is not reachable from the remote ref") from error

        for kind, object_id, task, subject in required:
            path = codec.object_path(kind, object_id)
            stored = candidate_snapshot.objects.get(path)
            if (
                stored is None
                or stored.kind != kind
                or stored.object_id != object_id
                or stored.envelope["task"] != task
                or stored.envelope["subject"] != subject
            ):
                raise MetadataRefError("metadata candidate does not contain a required exact object")
            try:
                codec.decode_object(
                    stored.data,
                    expected_id=object_id,
                    expected_repository=self.repository,
                    expected_task=task,
                    expected_subject=subject,
                    expected_kind=kind,
                )
            except codec.MetadataCodecError as error:
                raise MetadataRefError("metadata candidate required object has an invalid identity") from error
        return commit

    def _prepare_required_objects(
        self, required_objects: Sequence[tuple[str, str, str, str]]
    ) -> list[tuple[str, str, str, str]]:
        if (
            not isinstance(required_objects, Sequence)
            or isinstance(required_objects, (bytes, bytearray, str))
            or len(required_objects) > MAX_TREE_ENTRIES
        ):
            raise MetadataRefError("required_objects must be a bounded sequence of exact object identities")
        result: list[tuple[str, str, str, str]] = []
        for item in required_objects:
            if type(item) is not tuple or len(item) != 4:
                raise MetadataRefError("each required object must be a (kind, id, task, subject) tuple")
            kind, object_id, task, subject = item
            if type(kind) is not str or type(object_id) is not str:
                raise MetadataRefError("required object kind and ID must be strings")
            try:
                codec.object_path(kind, object_id)
            except (codec.MetadataCodecError, TypeError) as error:
                raise MetadataRefError("required object kind or ID is invalid") from error
            self._validate_metadata_id(object_id, "required object ID")
            self._validate_task(task)
            self._validate_git_oid(subject, "required object subject")
            result.append((kind, object_id, task, subject))
        return result

    @_bounded_metadata_operation
    def read_object(
        self,
        commit: str,
        kind: str,
        object_id: str,
        *,
        task: str,
        subject: str,
    ) -> dict[str, Any]:
        """Read one object from an exact, remotely reachable metadata commit."""
        self._validate_git_oid(commit, "commit")
        try:
            path = codec.object_path(kind, object_id)
        except (codec.MetadataCodecError, TypeError) as error:
            raise MetadataRefError("invalid metadata object identity") from error
        self._validate_task(task)
        self._validate_git_oid(subject, "subject")

        tip = self._observe_remote()
        if tip is None:
            raise MetadataRefError("metadata ref does not exist")
        tip_snapshot = self._validate_full_history(tip)
        self._validate_compatible_advance(
            commit,
            tip,
            self._read_tree_snapshot(commit),
            tip_snapshot,
            protected_tasks={},
        )

        snapshot = self._read_tree_snapshot(commit)
        stored = snapshot.objects.get(path)
        if stored is None:
            raise MetadataRefError("metadata object is absent from the requested commit")
        try:
            return codec.decode_object(
                stored.data,
                expected_id=object_id,
                expected_repository=self.repository,
                expected_task=task,
                expected_subject=subject,
                expected_kind=kind,
            )
        except codec.MetadataCodecError as error:
            raise MetadataRefError("metadata object does not match the requested identity") from error

    def _prepare_objects(self, objects: Sequence[bytes]) -> dict[str, _StoredObject]:
        if not isinstance(objects, Sequence) or isinstance(objects, (bytes, bytearray, str)):
            raise MetadataRefError("objects must be a finite sequence of canonical envelope bytes")
        if len(objects) > MAX_TREE_ENTRIES:
            raise MetadataRefError("too many metadata objects in one publish request")

        result: dict[str, _StoredObject] = {}
        total_bytes = 0
        for data in objects:
            if type(data) is not bytes or len(data) > MAX_OBJECT_BYTES:
                raise MetadataRefError("metadata object is not bounded canonical bytes")
            total_bytes += len(data)
            if total_bytes > MAX_TREE_TOTAL_BYTES:
                raise MetadataRefError("metadata publish request exceeds the byte limit")
            try:
                envelope = codec.decode_object(data, expected_repository=self.repository)
            except codec.MetadataCodecError as error:
                raise MetadataRefError("publish request contains an invalid metadata envelope") from error
            self._validate_task(envelope["task"])
            self._validate_git_oid(envelope["subject"], "subject")
            object_id = hashlib.sha256(_OBJECT_DOMAIN + data).hexdigest()
            path = codec.object_path(envelope["kind"], object_id)
            stored = _StoredObject(
                path=path,
                kind=envelope["kind"],
                object_id=object_id,
                blob_oid="",
                data=data,
                envelope=envelope,
            )
            previous = result.get(path)
            if previous is not None and previous.data != data:
                raise MetadataRefError("duplicate immutable object path has different bytes")
            result[path] = stored
        return result

    def _prepare_updates(
        self, record_updates: Mapping[str, tuple[str | None, str]]
    ) -> dict[str, tuple[str | None, str]]:
        if not isinstance(record_updates, Mapping):
            raise MetadataRefError("record_updates must be a mapping")
        if len(record_updates) > MAX_TREE_ENTRIES:
            raise MetadataRefError("too many Task Record updates in one publish request")
        result: dict[str, tuple[str | None, str]] = {}
        for task, pair in record_updates.items():
            self._validate_task(task)
            if type(pair) is not tuple or len(pair) != 2:
                raise MetadataRefError("each record update must be an (expected_id, proposed_id) tuple")
            expected, proposed = pair
            if expected is not None:
                self._validate_metadata_id(expected, "expected record ID")
            self._validate_metadata_id(proposed, "proposed record ID")
            result[task] = (expected, proposed)
        return result

    def _validate_request_against_tree(
        self,
        incoming: dict[str, _StoredObject],
        updates: dict[str, tuple[str | None, str]],
        snapshot: _TreeSnapshot,
    ) -> None:
        for path, proposed in incoming.items():
            existing = snapshot.objects.get(path)
            if existing is not None and existing.data != proposed.data:
                raise MetadataRefError("immutable metadata object path already contains different bytes")

        self._check_expected_pointers(updates, snapshot)
        records_by_id = self._record_object_index(incoming, snapshot)
        for task, (_expected, proposed_id) in updates.items():
            proposed = records_by_id.get(proposed_id)
            if proposed is None:
                raise MetadataRefError("proposed Task Record object is absent from the proposed tree")
            if proposed.envelope["repository"] != self.repository or proposed.envelope["task"] != task:
                raise MetadataRefError("proposed Task Record does not match its repository and Task")

    def _check_expected_pointers(
        self,
        updates: dict[str, tuple[str | None, str]],
        snapshot: _TreeSnapshot,
    ) -> None:
        for task, (expected, proposed) in updates.items():
            observed = snapshot.pointers.get(task)
            if observed != expected:
                expected_text = "absent" if expected is None else expected
                observed_text = "absent" if observed is None else observed
                raise MetadataConflictError(
                    f"Task {task} record CAS conflict: expected {expected_text}, "
                    f"observed {observed_text}, proposed {proposed}"
                )

    def _validate_requested_tree(
        self,
        snapshot: _TreeSnapshot,
        incoming: dict[str, _StoredObject],
        updates: dict[str, tuple[str | None, str]],
    ) -> None:
        records_by_id = self._record_object_index({}, snapshot)
        for path, requested in incoming.items():
            stored = snapshot.objects.get(path)
            if stored is None or stored.data != requested.data:
                raise MetadataRefError("requested immutable object is absent from the candidate tree")
        for task, (_expected, proposed) in updates.items():
            if snapshot.pointers.get(task) != proposed:
                raise MetadataRefError("requested Task Record pointer is absent from the candidate tree")
            stored = records_by_id.get(proposed)
            if stored is None or stored.envelope["task"] != task:
                raise MetadataRefError("requested Task Record pointer target is invalid")

    def _record_object_index(
        self,
        incoming: dict[str, _StoredObject],
        snapshot: _TreeSnapshot,
    ) -> dict[str, _StoredObject]:
        records = {
            stored.object_id: stored
            for stored in snapshot.objects.values()
            if stored.kind == "task-record"
        }
        for stored in incoming.values():
            if stored.kind == "task-record":
                records[stored.object_id] = stored
        return records

    def _create_commit(
        self,
        base: str | None,
        base_snapshot: _TreeSnapshot,
        incoming: dict[str, _StoredObject],
        updates: dict[str, tuple[str | None, str]],
    ) -> str:
        with tempfile.TemporaryDirectory(prefix="agentcore-metadata-index-") as temporary:
            index_file = Path(temporary) / "index"
            if base is None:
                self._git(["read-tree", "--empty"], index_file=index_file)
            else:
                base_tree = self._read_commit(base).tree
                self._git(["read-tree", base_tree], index_file=index_file)

            for path, stored in incoming.items():
                if path in base_snapshot.objects:
                    continue
                blob_oid = self._hash_blob(stored.data)
                self._git(
                    ["update-index", "--add", "--cacheinfo", f"100644,{blob_oid},{path}"],
                    index_file=index_file,
                )
            for task, (_expected, proposed) in updates.items():
                if base_snapshot.pointers.get(task) == proposed:
                    continue
                pointer = f"{proposed}\n".encode("ascii")
                blob_oid = self._hash_blob(pointer)
                self._git(
                    [
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"100644,{blob_oid},tasks/{task}/record",
                    ],
                    index_file=index_file,
                )

            tree = self._decode_oid_output(
                self._git(["write-tree"], index_file=index_file, max_stdout=256), "tree"
            )
            environment = {
                "GIT_AUTHOR_NAME": "Agent Core Metadata",
                "GIT_AUTHOR_EMAIL": "metadata@invalid",
                "GIT_AUTHOR_DATE": "946684800 +0000",
                "GIT_COMMITTER_NAME": "Agent Core Metadata",
                "GIT_COMMITTER_EMAIL": "metadata@invalid",
                "GIT_COMMITTER_DATE": "946684800 +0000",
            }
            args = ["commit-tree", tree]
            if base is not None:
                args.extend(["-p", base])
            # A distinct candidate identity prevents two independent writers
            # proposing identical Task Record bytes against the same parent
            # from mistaking the other writer's commit for their own durable
            # operation. Content-addressed object bytes remain deterministic;
            # commit identity is an exact publication receipt, not an object ID.
            args.extend(["-m", f"Agent Core v4 metadata update\n\nCandidate: {secrets.token_hex(16)}"])
            return self._decode_oid_output(self._git(args, extra_env=environment, max_stdout=256), "commit")

    def _hash_blob(self, data: bytes) -> str:
        return self._decode_oid_output(
            self._git(["hash-object", "-w", "--stdin"], input_data=data, max_stdout=256), "blob"
        )

    def _push_commit(self, commit: str, observed: str | None) -> bool:
        """CAS the fixed metadata ref through its trusted host capability only.

        The capability is required to provide a same-transaction direct
        (never-dereferenced) update of exactly ``refs/agentcore/metadata``, an
        expected-old-OID comparison, and a fast-forward check against the
        candidate's parent. A client-side Git push cannot establish those
        guarantees; there is deliberately no Git push fallback here.
        """
        if self._read_commit(commit).parent != observed:
            raise MetadataRefError("metadata CAS candidate is not a direct child of the observed remote tip")
        capability = self._direct_ref_cas
        if capability is None:
            raise MetadataRefError(
                "metadata publication requires a trusted direct-ref CAS capability"
            )
        try:
            # Only literal True is a successful capability result. Exceptions
            # and malformed results are treated as an uncertain mutation; the
            # caller still performs the exact remote postcondition observation.
            return capability(commit, observed) is True
        except Exception:
            # The capability may have applied the CAS before reporting an
            # error. Let publish observe the exact remote postcondition rather
            # than assuming either success or failure.
            return False

    def _observe_remote(self) -> str | None:
        """Return a ref value proven equal to both ls-remote and FETCH_HEAD."""
        self._check_transport_configuration()
        for _attempt in range(MAX_OBSERVE_ATTEMPTS):
            before = self._ls_remote()
            if before is None:
                after = self._ls_remote()
                if after is None:
                    return None
                continue
            try:
                self._git(
                    ["fetch", "--no-tags", "--no-recurse-submodules", "--refmap=", self.remote, METADATA_REF],
                    max_stdout=1024 * 1024,
                )
            except MetadataRefError:
                after = self._ls_remote()
                if after != before:
                    continue
                raise MetadataRefError("could not fetch the remote metadata ref")
            fetched = self._read_fetch_head()
            after = self._ls_remote()
            if before == fetched == after:
                return fetched
        raise MetadataRefError("remote metadata ref changed during bounded observation")

    def _ls_remote(self) -> str | None:
        output = self._git(
            ["ls-remote", "--symref", "--refs", self.remote, METADATA_REF],
            max_stdout=4096,
        )
        if not output:
            return None
        lines = output.splitlines()
        # A custom remote symref could cause a client-side ref mutation to
        # affect refs/heads/main. Never treat an advertised symbolic metadata
        # ref as an ordinary ref.
        if any(line.startswith(b"ref: ") for line in lines):
            raise MetadataRefError("remote metadata ref must not be symbolic")
        if len(lines) != 1:
            raise MetadataRefError("remote advertised an ambiguous metadata ref")
        fields = lines[0].split(b"\t")
        if len(fields) != 2 or fields[1] != METADATA_REF.encode("ascii"):
            raise MetadataRefError("remote advertised an unexpected metadata ref")
        try:
            oid = fields[0].decode("ascii")
        except UnicodeDecodeError as error:
            raise MetadataRefError("remote advertised a malformed metadata OID") from error
        self._validate_git_oid(oid, "remote metadata ref")
        return oid

    def _read_fetch_head(self) -> str:
        path_data = self._git(["rev-parse", "--git-path", "FETCH_HEAD"], max_stdout=16 * 1024)
        try:
            path_text = path_data.decode("utf-8", errors="strict").strip()
            fetch_head = Path(path_text)
            if not fetch_head.is_absolute():
                fetch_head = self.root / fetch_head
            # Validate and read the same file descriptor: a path-based lstat()
            # followed by read_bytes() can follow a replacement symlink and
            # allocate an unbounded attacker-chosen file after validation.
            if not hasattr(os, "O_NOFOLLOW"):
                raise MetadataRefError("no-follow FETCH_HEAD reads are unavailable")
            # O_NONBLOCK prevents a replaced FIFO from hanging before fstat
            # can reject its non-regular file type.
            fd = os.open(fetch_head, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                fetch_stat = os.fstat(handle.fileno())
                if not stat.S_ISREG(fetch_stat.st_mode) or fetch_stat.st_size > 64 * 1024:
                    raise MetadataRefError("FETCH_HEAD is not a bounded regular file")
                data = handle.read(64 * 1024 + 1)
            if len(data) > 64 * 1024:
                raise MetadataRefError("FETCH_HEAD exceeds its byte limit")
            lines = data.splitlines()
        except MetadataRefError:
            raise
        except (OSError, UnicodeDecodeError) as error:
            raise MetadataRefError("FETCH_HEAD is unavailable after metadata fetch") from error
        if len(lines) != 1:
            raise MetadataRefError("metadata fetch did not produce exactly one FETCH_HEAD entry")
        try:
            oid = lines[0].split(b"\t", 1)[0].decode("ascii")
        except UnicodeDecodeError as error:
            raise MetadataRefError("FETCH_HEAD contains a malformed metadata OID") from error
        self._validate_git_oid(oid, "FETCH_HEAD")
        return oid

    def _read_tree_snapshot(self, commit: str) -> _TreeSnapshot:
        commit_info = self._read_commit(commit)
        entries: dict[str, _TreeEntry] = {}
        directories: set[str] = set()
        raw = self._git(
            ["ls-tree", "-r", "-t", "-z", "--full-tree", commit_info.tree],
            max_stdout=MAX_TREE_OUTPUT_BYTES,
        )
        records = raw.split(b"\0") if raw else []
        if records and records[-1] == b"":
            records.pop()
        if len(records) > MAX_TREE_ENTRIES:
            raise MetadataRefError("metadata tree exceeds the entry limit")
        for record in records:
            try:
                header, raw_path = record.split(b"\t", 1)
                mode_bytes, type_bytes, oid_bytes = header.split(b" ")
                path = raw_path.decode("ascii", errors="strict")
                mode = mode_bytes.decode("ascii")
                object_type = type_bytes.decode("ascii")
                oid = oid_bytes.decode("ascii")
            except (ValueError, UnicodeDecodeError) as error:
                raise MetadataRefError("metadata tree contains a malformed entry") from error
            if not path or path.startswith("/") or "//" in path:
                raise MetadataRefError("metadata tree contains an invalid path")
            self._validate_git_oid(oid, "tree entry")

            if mode == "040000" and object_type == "tree":
                self._validate_directory_path(path)
                if path in directories:
                    raise MetadataRefError("metadata tree contains a duplicate directory")
                directories.add(path)
                continue
            if mode != "100644" or object_type != "blob":
                raise MetadataRefError("metadata tree contains a non-regular blob, executable, or gitlink")
            self._validate_file_path(path)
            if path in entries:
                raise MetadataRefError("metadata tree contains a duplicate path")
            entries[path] = _TreeEntry(path, mode, object_type, oid)

        child_counts = dict.fromkeys(directories, 0)
        for path in (*directories, *entries):
            parent = path.rpartition("/")[0]
            if parent in child_counts:
                child_counts[parent] += 1
        if any(count == 0 for count in child_counts.values()):
            raise MetadataRefError("metadata tree contains an empty directory")
        if len(entries) > MAX_TREE_ENTRIES:
            raise MetadataRefError("metadata tree exceeds the file limit")
        blob_data = self._read_blobs(entries)
        objects: dict[str, _StoredObject] = {}
        pointers: dict[str, str] = {}
        for path, entry in entries.items():
            data = blob_data[path]
            object_match = _OBJECT_TREE_PATH.fullmatch(path)
            if object_match:
                kind, prefix, object_id = object_match.groups()
                if prefix != object_id[:2]:
                    raise MetadataRefError("metadata object path prefix does not match its ID")
                try:
                    envelope = codec.decode_object(
                        data,
                        expected_id=object_id,
                        expected_repository=self.repository,
                        expected_kind=kind,
                    )
                except codec.MetadataCodecError as error:
                    raise MetadataRefError("metadata tree contains an invalid content-addressed object") from error
                self._validate_task(envelope["task"])
                self._validate_git_oid(envelope["subject"], "subject")
                objects[path] = _StoredObject(
                    path=path,
                    kind=kind,
                    object_id=object_id,
                    blob_oid=entry.oid,
                    data=data,
                    envelope=envelope,
                )
                continue
            record_match = _RECORD_PATH.fullmatch(path)
            if record_match:
                task = record_match.group(1)
                pointer_match = _OBJECT_ID_IN_POINTER.fullmatch(data)
                if pointer_match is None:
                    raise MetadataRefError("Task Record pointer has malformed bytes")
                pointers[task] = pointer_match.group(1).decode("ascii")
                continue
            raise MetadataRefError("metadata tree contains an unauthorized path")

        by_object_id = {stored.object_id: stored for stored in objects.values()}
        for task, object_id in pointers.items():
            target = by_object_id.get(object_id)
            if target is None or target.kind != "task-record":
                raise MetadataRefError("Task Record pointer target is absent or has the wrong kind")
            if target.envelope["repository"] != self.repository or target.envelope["task"] != task:
                raise MetadataRefError("Task Record pointer target has the wrong repository or Task")
        return _TreeSnapshot(entries=entries, objects=objects, pointers=pointers)

    def _read_blobs(self, entries: dict[str, _TreeEntry]) -> dict[str, bytes]:
        if not entries:
            return {}
        ordered = list(entries.values())
        query = b"".join(entry.oid.encode("ascii") + b"\n" for entry in ordered)
        check_output = self._git(
            ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            input_data=query,
            max_stdout=MAX_TREE_ENTRIES * 160,
        )
        check_lines = check_output.splitlines()
        if len(check_lines) != len(ordered):
            raise MetadataRefError("metadata blob size listing is incomplete")
        sizes: list[int] = []
        total_size = 0
        for entry, line in zip(ordered, check_lines, strict=True):
            fields = line.split(b" ")
            if len(fields) != 3 or fields[0] != entry.oid.encode("ascii") or fields[1] != b"blob":
                raise MetadataRefError("metadata tree references a missing or non-blob object")
            try:
                size = int(fields[2])
            except ValueError as error:
                raise MetadataRefError("metadata blob has a malformed size") from error
            object_match = _OBJECT_TREE_PATH.fullmatch(entry.path)
            if size < 0 or (object_match and size > MAX_OBJECT_BYTES) or (not object_match and size > 65):
                raise MetadataRefError("metadata blob exceeds its per-file byte limit")
            total_size += size
            if total_size > MAX_TREE_TOTAL_BYTES:
                raise MetadataRefError("metadata tree exceeds the total byte limit")
            sizes.append(size)

        batch_output = self._git(
            ["cat-file", "--batch"],
            input_data=query,
            max_stdout=MAX_TREE_TOTAL_BYTES + len(ordered) * 128,
        )
        result: dict[str, bytes] = {}
        offset = 0
        for entry, expected_size in zip(ordered, sizes, strict=True):
            header_end = batch_output.find(b"\n", offset)
            if header_end < 0:
                raise MetadataRefError("metadata blob batch is truncated")
            header = batch_output[offset:header_end].split(b" ")
            if (
                len(header) != 3
                or header[0] != entry.oid.encode("ascii")
                or header[1] != b"blob"
            ):
                raise MetadataRefError("metadata blob batch returned an unexpected object")
            try:
                returned_size = int(header[2])
            except ValueError as error:
                raise MetadataRefError("metadata blob batch has a malformed size") from error
            if returned_size != expected_size:
                raise MetadataRefError("metadata blob size changed while reading")
            start = header_end + 1
            end = start + returned_size
            if end >= len(batch_output) or batch_output[end : end + 1] != b"\n":
                raise MetadataRefError("metadata blob batch has invalid framing")
            result[entry.path] = batch_output[start:end]
            offset = end + 1
        if offset != len(batch_output):
            raise MetadataRefError("metadata blob batch contains trailing bytes")
        return result

    def _read_commit(self, oid: str) -> _Commit:
        self._validate_git_oid(oid, "commit")
        raw = self._git(["cat-file", "commit", oid], max_stdout=MAX_COMMIT_BYTES)
        if b"\0" in raw or b"\n\n" not in raw:
            raise MetadataRefError("metadata ref points to a malformed commit")
        header, _message = raw.split(b"\n\n", 1)
        tree_values: list[str] = []
        parent_values: list[str] = []
        for line in header.split(b"\n"):
            if line.startswith(b"tree "):
                try:
                    tree_values.append(line[5:].decode("ascii"))
                except UnicodeDecodeError as error:
                    raise MetadataRefError("metadata commit has a malformed tree OID") from error
            elif line.startswith(b"parent "):
                try:
                    parent_values.append(line[7:].decode("ascii"))
                except UnicodeDecodeError as error:
                    raise MetadataRefError("metadata commit has a malformed parent OID") from error
        if len(tree_values) != 1 or len(parent_values) > 1:
            raise MetadataRefError("metadata commits must have one tree and at most one parent")
        self._validate_git_oid(tree_values[0], "commit tree")
        parent = parent_values[0] if parent_values else None
        if parent is not None:
            self._validate_git_oid(parent, "commit parent")
            parent_type = self._git(["cat-file", "-t", parent], max_stdout=32)
            if parent_type.strip() != b"commit":
                raise MetadataRefError("metadata commit parent is not a commit object")
        return _Commit(tree=tree_values[0], parent=parent, raw_size=len(raw))

    def _validate_full_history(self, tip: str) -> _TreeSnapshot:
        """Validate every metadata tree and append-only edge through parentless genesis."""
        deadline = time.monotonic() + MAX_HISTORY_VALIDATION_SECONDS
        operation_deadline = self._validation_deadline.get()
        if operation_deadline is not None:
            deadline = min(deadline, operation_deadline)
        current = self._validate_git_oid(tip, "metadata tip")
        current_snapshot = self._read_tree_snapshot(current)
        tip_snapshot = current_snapshot
        visited = 0
        validated_bytes = 0

        while True:
            if time.monotonic() >= deadline:
                raise MetadataRefError("metadata history exceeds the validation time limit")
            visited += 1
            if visited > MAX_HISTORY_COMMITS:
                raise MetadataRefError("metadata history exceeds the total commit limit")
            validated_bytes += self._history_snapshot_size(current_snapshot)
            commit = self._read_commit(current)
            validated_bytes += commit.raw_size
            if validated_bytes > MAX_HISTORY_TOTAL_BYTES:
                raise MetadataRefError("metadata history exceeds the total validated byte limit")
            if commit.parent is None:
                # A valid metadata history terminates at its own genesis; a
                # metadata candidate is never allowed to inherit a product
                # commit as its parent.
                return tip_snapshot
            if visited == MAX_HISTORY_COMMITS:
                raise MetadataRefError("metadata history exceeds the total commit limit")

            if time.monotonic() >= deadline:
                raise MetadataRefError("metadata history exceeds the validation time limit")
            parent_snapshot = self._read_tree_snapshot(commit.parent)
            self._validate_tree_transition(parent_snapshot, current_snapshot, protected_tasks={})
            current = commit.parent
            current_snapshot = parent_snapshot

    @staticmethod
    def _history_snapshot_size(snapshot: _TreeSnapshot) -> int:
        # Count decoded contents and a bounded per-entry allowance for tree
        # records/paths so a long chain of tiny trees is bounded as well.
        object_bytes = sum(len(stored.data) for stored in snapshot.objects.values())
        return object_bytes + len(snapshot.pointers) * 65 + len(snapshot.entries) * 256

    def _validate_reachable(self, ancestor: str, tip: str) -> bool:
        try:
            self._validate_compatible_advance(
                ancestor,
                tip,
                self._read_tree_snapshot(ancestor),
                self._read_tree_snapshot(tip),
                protected_tasks={},
            )
        except MetadataConflictError:
            return False
        return True

    def _validate_compatible_advance(
        self,
        ancestor: str,
        tip: str,
        ancestor_snapshot: _TreeSnapshot,
        tip_snapshot: _TreeSnapshot,
        *,
        protected_tasks: Mapping[str, tuple[str | None, str]],
    ) -> None:
        """Prove a first-parent append-only path from ancestor through tip."""
        deadline = self._validation_deadline.get()
        if deadline is None:
            deadline = time.monotonic() + MAX_HISTORY_VALIDATION_SECONDS
        self._validate_git_oid(ancestor, "ancestor commit")
        self._validate_git_oid(tip, "tip commit")
        if ancestor == tip:
            if ancestor_snapshot.entries != tip_snapshot.entries:
                raise MetadataRefError("identical metadata commits have inconsistent tree snapshots")
            return

        current = tip
        current_snapshot = tip_snapshot
        for _ in range(MAX_HISTORY_COMMITS):
            if time.monotonic() >= deadline:
                raise MetadataRefError("metadata history exceeds the validation time limit")
            if current == ancestor:
                break
            commit = self._read_commit(current)
            if commit.parent is None:
                raise MetadataConflictError("remote metadata history is unrelated or was rewound")
            parent_snapshot = (
                ancestor_snapshot
                if commit.parent == ancestor
                else self._read_tree_snapshot(commit.parent)
            )
            self._validate_tree_transition(
                parent_snapshot,
                current_snapshot,
                protected_tasks=protected_tasks,
            )
            current = commit.parent
            current_snapshot = parent_snapshot
        else:
            raise MetadataRefError("metadata history exceeds the traversal limit")

        if current != ancestor:
            raise MetadataRefError("metadata ancestry validation did not reach the requested ancestor")

    def _validate_tree_transition(
        self,
        previous: _TreeSnapshot,
        current: _TreeSnapshot,
        *,
        protected_tasks: Mapping[str, tuple[str | None, str]],
    ) -> None:
        paths = set(previous.entries) | set(current.entries)
        for path in paths:
            before = previous.entries.get(path)
            after = current.entries.get(path)
            if _OBJECT_TREE_PATH.fullmatch(path):
                if before is not None and before != after:
                    raise MetadataRefError("metadata history modified or deleted an immutable object")
                continue
            match = _RECORD_PATH.fullmatch(path)
            if match is None:
                raise MetadataRefError("metadata history contains an unauthorized path")
            task = match.group(1)
            if before != after and task in protected_tasks:
                expected, proposed = protected_tasks[task]
                observed = current.pointers.get(task)
                expected_text = "absent" if expected is None else expected
                observed_text = "absent" if observed is None else observed
                raise MetadataConflictError(
                    f"Task {task} record CAS conflict: expected {expected_text}, "
                    f"observed {observed_text}, proposed {proposed}"
                )
            if before is not None and after is None:
                raise MetadataRefError("metadata history deleted a Task Record pointer")

    def _validate_directory_path(self, path: str) -> None:
        if path in {"objects", "tasks"}:
            return
        parts = path.split("/")
        if len(parts) == 2 and parts[0] == "objects" and parts[1] in codec.KINDS:
            return
        if (
            len(parts) == 3
            and parts[0] == "objects"
            and parts[1] in codec.KINDS
            and re.fullmatch(r"[0-9a-f]{2}", parts[2], re.ASCII)
        ):
            return
        if (
            len(parts) == 2
            and parts[0] == "tasks"
            and len(parts[1]) <= MAX_TASK_ID_LENGTH
            and _TASK.fullmatch(parts[1])
        ):
            return
        raise MetadataRefError("metadata tree contains an unauthorized directory")

    def _validate_file_path(self, path: str) -> None:
        if _OBJECT_TREE_PATH.fullmatch(path) or _RECORD_PATH.fullmatch(path):
            return
        raise MetadataRefError("metadata tree contains an unauthorized file path")

    def _empty_snapshot(self) -> _TreeSnapshot:
        return _TreeSnapshot(entries={}, objects={}, pointers={})

    def _validate_task(self, task: object) -> str:
        if (
            type(task) is not str
            or len(task) > MAX_TASK_ID_LENGTH
            or not _TASK.fullmatch(task)
        ):
            raise MetadataRefError("Task ID must be a canonical positive decimal string")
        return task

    def _validate_metadata_id(self, object_id: object, label: str) -> str:
        if type(object_id) is not str or not _HEX_64.fullmatch(object_id):
            raise MetadataRefError(f"{label} must be a lowercase 64-hex metadata ID")
        return object_id

    def _validate_git_oid(self, oid: object, label: str) -> str:
        if (
            type(oid) is not str
            or len(oid) != self._oid_length
            or not re.fullmatch(r"[0-9a-f]+", oid, re.ASCII)
        ):
            raise MetadataRefError(f"{label} must be a full lowercase Git OID for this repository")
        return oid

    def _decode_oid_output(self, output: bytes, label: str) -> str:
        try:
            oid = output.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError as error:
            raise MetadataRefError(f"Git returned a malformed {label} OID") from error
        return self._validate_git_oid(oid, label)

    def _git(
        self,
        arguments: list[str],
        *,
        input_data: bytes | None = None,
        index_file: Path | None = None,
        extra_env: dict[str, str] | None = None,
        check: bool = True,
        max_stdout: int = 128 * 1024 * 1024,
        timeout: float = GIT_COMMAND_TIMEOUT_SECONDS,
    ) -> bytes | None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        operation_deadline = self._validation_deadline.get()
        if operation_deadline is not None:
            remaining = operation_deadline - time.monotonic()
            if remaining <= 0:
                raise MetadataRefError("metadata history exceeds the validation time limit")
            timeout = min(timeout, remaining)
        # Remove ambient Git overrides and global/system Git configuration.
        # Local transport command settings are either overridden below or
        # rejected by _check_transport_configuration.
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["GCM_INTERACTIVE"] = "never"
        if index_file is not None:
            environment["GIT_INDEX_FILE"] = str(index_file)
        if extra_env:
            environment.update(extra_env)

        config_arguments = [
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "push.followTags=false",
            "-c",
            "push.gpgSign=false",
            "-c",
            "push.recurseSubmodules=no",
            "-c",
            "push.pushOption=",
            "-c",
            "core.sshCommand=ssh -F /dev/null -oBatchMode=yes -oProxyCommand=none -oProxyJump=none -oPermitLocalCommand=no",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=always",
            "-c",
            "protocol.http.allow=always",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "protocol.ssh.allow=always",
            "-c",
            "protocol.git.allow=always",
        ]
        remote_name = self._configured_remote_name()
        if remote_name is not None:
            config_arguments.extend(
                [
                    "-c",
                    f"remote.{remote_name}.uploadpack=git-upload-pack",
                    "-c",
                    f"remote.{remote_name}.receivepack=git-receive-pack",
                    "-c",
                    f"remote.{remote_name}.mirror=false",
                ]
            )
        command = [
            "git",
            "--no-replace-objects",
            *config_arguments,
            "-C",
            str(self.root),
            *arguments,
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as error:
            if check:
                raise MetadataRefError("could not execute Git metadata operation") from error
            return None

        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        output = bytearray()
        over_limit = False
        timed_out = False
        stdout_open = True
        stdin_open = input_data is not None
        input_offset = 0
        deadline = time.monotonic() + timeout

        def close_stdin() -> None:
            nonlocal stdin_open
            if not stdin_open:
                return
            assert process.stdin is not None
            try:
                selector.unregister(process.stdin)
            except (KeyError, ValueError):
                pass
            try:
                process.stdin.close()
            except OSError:
                pass
            stdin_open = False

        def terminate_process_group() -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass

        try:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            if stdin_open:
                assert process.stdin is not None
                if not input_data:
                    close_stdin()
                else:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")

            while stdout_open or process.poll() is None or stdin_open:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    terminate_process_group()
                    break
                events = selector.select(min(remaining, 0.1))
                if not events and not selector.get_map():
                    time.sleep(min(remaining, 0.05))
                for key, _mask in events:
                    if key.data == "stdin":
                        assert process.stdin is not None and input_data is not None
                        try:
                            written = os.write(
                                process.stdin.fileno(),
                                input_data[input_offset : input_offset + 64 * 1024],
                            )
                        except OSError:
                            close_stdin()
                            continue
                        input_offset += written
                        if input_offset >= len(input_data):
                            close_stdin()
                    else:
                        chunk = os.read(
                            process.stdout.fileno(),
                            min(64 * 1024, max_stdout - len(output) + 1),
                        )
                        if not chunk:
                            try:
                                selector.unregister(process.stdout)
                            except (KeyError, ValueError):
                                pass
                            stdout_open = False
                            continue
                        if len(output) + len(chunk) > max_stdout:
                            over_limit = True
                            terminate_process_group()
                            break
                        output.extend(chunk)
                if over_limit:
                    break
            if timed_out or over_limit:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    terminate_process_group()
                    process.wait(timeout=5)
            else:
                returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_group()
            process.wait(timeout=5)
            returncode = process.returncode
        finally:
            if process.poll() is None:
                terminate_process_group()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            close_stdin()
            if stdout_open:
                try:
                    selector.unregister(process.stdout)
                except (KeyError, ValueError):
                    pass
            try:
                process.stdout.close()
            except OSError:
                pass
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            selector.close()

        if timed_out:
            raise MetadataRefError("Git metadata operation exceeded its runtime limit")
        if over_limit:
            raise MetadataRefError("Git metadata operation exceeded its output limit")
        returncode = process.returncode
        if returncode != 0:
            if check:
                operation = arguments[0] if arguments else "operation"
                raise MetadataRefError(f"Git {operation} operation failed")
            return None
        return bytes(output)


__all__ = ["METADATA_REF", "MetadataConflictError", "MetadataRefError", "MetadataStore"]
