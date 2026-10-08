"""Host-qualified, Task-bound cleanup orchestration for Agent Core v4.

This module is deliberately an orchestrator, not a filesystem or Git deletion
primitive.  Filesystem inspection is delegated to ``cleanup_resources`` and
each destructive step requires a separately installed, host-qualified
capability.  In particular, a cancelled/superseded Task Record is not a data
disposal grant, callbacks' return values are never receipts, and this module
has no ``rm``, ``git clean``, force, remote-ref, or generic-path deletion
fallback.

There is no production deletion backend or atomicity attestation in this
module. ``CleanupQualification`` is configuration supplied at the trusted host
assembly boundary, not a self-authenticating proof: its truth-valued fields and
the Python permit sentinel do not establish race freedom or isolate an Agent
running in the same process. A production host must install and separately
audit a backend which provides the specified atomic custody, expected-OID CAS,
and data-preservation guarantees. With no such installed capability, effects
are refused. Fixture backends qualify only their disposable test repositories.

The host installation boundary is trusted: callbacks and deletion capabilities
must be installed by the host, never constructed from agent-generated JSON.
All public requests are immutable and bind the canonical #191 Record and
Contract, local Git facts, complete filesystem inventory, typed ownership
proofs, and current authorization.  Durable intents are written before the
first destructive step and recovery accepts only an exact intent returned by
the installed journal reader.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import operation_prerequisites
import task_record
import task_view
from metadata_ref import MetadataStore


SCHEMA_VERSION = 1
MAX_GIT_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_HISTORY_COMMITS = 100_000
MAX_WORKTREES = 10_000
MAX_INVENTORY_NODES = 100_000
MAX_TRACKED_FILE_BYTES = 8 * 1024 * 1024
MAX_TRACKED_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PATH_BYTES = 4096
_TASK = re.compile(r"[1-9][0-9]{0,127}\Z", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_INTENT_DOMAIN = b"agentcore-task-cleanup-intent/v1\n"
_PERMIT_SEAL = object()
_DISPOSABLE_KINDS = frozenset({"ephemeral_file", "ephemeral_dir", "recovery_state"})
_PREREQUISITE_OPERATIONS = frozenset({
    "cleanup.worktree", "cleanup.branch", "cleanup.ephemeral",
})


class TaskCleanupError(ValueError):
    """A safe cleanup failure code; callback and Git details are not exposed."""

    def __init__(self, code: str, operation: str = "plan") -> None:
        if type(code) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code, re.ASCII):
            code = "task_cleanup_error"
        if operation not in {"plan", "worktree", "branch", "ephemeral", "recover"}:
            operation = "plan"
        self.code = code
        self.operation = operation
        self.intent_reference: str | None = None
        super().__init__(f"{operation}:{code}")


@dataclass(frozen=True)
class TaskRetentionRequest:
    """Exact binding for an authenticated remote/PR retention observation."""

    repository: str
    task: str
    branch_ref: str
    expected_head: str
    default_branch_ref: str
    default_head: str


@dataclass(frozen=True)
class TaskPullRequestFacts:
    """Closed PR facts from a trusted, authenticated retention reader.

    ``merge_commit_oid`` is factual, not a claim that Git history is retained.
    The facade independently proves the exact ancestry against actual local
    commit objects and the current default ref.
    """

    base_repository: str
    head_repository: str
    number: int
    state: str
    merged: bool
    base_ref: str
    head_ref: str
    head_oid: str
    merge_commit_oid: str | None


@dataclass(frozen=True)
class TaskRetentionFacts:
    """Remote Task ref and optional PR result, bound to one expected Task HEAD."""

    repository: str
    task: str
    branch_ref: str
    expected_head: str
    remote_task_head: str | None
    pull_request: TaskPullRequestFacts | None


@dataclass(frozen=True)
class DisposableResourceProof:
    """Host-classifier proof for exactly one inventory-bound disposable scope.

    The proof is descriptive until returned by the trusted classifier callback.
    It is accepted only when all Task, HEAD, inventory, scope, producer, kind,
    and exact NodeObservation bindings match the current complete inventory.
    """

    repository: str
    task: str
    branch_ref: str
    head: str
    inventory_id: str
    relative_scope: str
    resource_kind: str
    producer: str
    proof_id: str
    node_manifest: tuple[Any, ...]


@dataclass(frozen=True)
class ResourceClassificationRequest:
    repository: str
    task: str
    branch_ref: str
    head: str
    spec: Any
    inventory: Any
    unknown_nodes: tuple[Any, ...]


@dataclass(frozen=True)
class OpaqueResourceReference:
    """Opaque registry handle; it intentionally has no filesystem path field."""

    owner: str
    token: str


@dataclass(frozen=True)
class OwnedResourceBinding:
    """Typed result from the host registrar, never a caller-selected path."""

    repository: str
    task: str
    branch_ref: str
    head: str
    inventory_id: str
    relative_scope: str
    resource_kind: str
    producer: str
    node_manifest: tuple[Any, ...]
    binding_id: str


@dataclass(frozen=True)
class CleanupNodeFact:
    relative_path: str
    kind: str
    device: int
    inode: int
    mount_id: int
    mode: int
    uid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    content_sha256: str | None


@dataclass(frozen=True)
class CleanupProofFact:
    relative_scope: str
    resource_kind: str
    producer: str
    proof_id: str
    node_digest: str


@dataclass(frozen=True)
class CleanupIntent:
    """Immutable, content-addressed description of one cleanup operation."""

    schema_version: int
    operation: str
    repository: str
    task: str
    branch_ref: str
    base_revision: str
    record_id: str
    contract_id: str
    disposition: str | None
    target_head: str
    metadata_tip: str
    main_state_fingerprint: str
    default_branch_ref: str
    default_head: str
    worktree_root: str
    git_admin: str
    root_identity: tuple[int, int, int]
    root_nlink: int
    parent_identity: tuple[int, int, int]
    admin_parent_identity: tuple[int, int, int]
    admin_identity: tuple[int, int, int]
    git_pointer_sha256: str
    inventory_id: str
    manifest_digest: str
    nodes: tuple[CleanupNodeFact, ...]
    resource_proofs: tuple[CleanupProofFact, ...]
    remote_task_head: str | None
    retention_fingerprint: str
    pull_request_head: str | None
    pull_request_merge_commit: str | None
    ephemeral_scope: str | None = None
    ephemeral_binding_id: str | None = None
    resource_reference: OpaqueResourceReference | None = None

    def canonical_bytes(self) -> bytes:
        return _canonical_json(_intent_value(self))

    @property
    def intent_id(self) -> str:
        return hashlib.sha256(_INTENT_DOMAIN + self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class RecordedCleanupIntent:
    """Typed acknowledgement from the host's durable intent journal."""

    reference: str
    intent_id: str
    intent: CleanupIntent


@dataclass(frozen=True)
class CleanupAuthorizationRequest:
    """Exact host authorization request for one immutable intent step."""

    intent_id: str
    intent_bytes: bytes
    operation: str
    step: str
    repository: str
    task: str
    branch_ref: str
    expected_head: str
    record_id: str
    contract_id: str
    default_branch_ref: str
    default_head: str
    root_identity: tuple[int, int, int]
    inventory_id: str
    manifest_digest: str
    ownership_proof_ids: tuple[str, ...]
    resource_scope: str | None


@dataclass(frozen=True)
class CleanupQualification:
    """Host assembly descriptor; fields are claims, not attested guarantees.

    This value does not authorize an operation and cannot authenticate its
    creator. Only trusted host code may install it alongside an audited backend.
    """

    operation: str
    qualification_id: str
    repository: str
    common_directory: str
    common_identity: tuple[int, int, int]
    default_branch_ref: str
    worktree_namespace: str
    descriptor_pinned: bool
    atomic_identity: bool
    atomic_custody: bool
    current_authorization: bool
    expected_oid_compare_exchange: bool
    preserves_product_data: bool
    no_force: bool
    no_remote_refs: bool


@dataclass(frozen=True)
class DeletionPermit:
    """Short-lived request-binding guard; not an in-process security boundary."""

    operation: str
    qualification_id: str
    intent_id: str
    authorization_digest: str
    target_digest: str
    _seal: object


@dataclass(frozen=True)
class WorktreeRemovalRequest:
    spec: Any
    inventory: Any
    intent: CleanupIntent
    proofs: tuple[DisposableResourceProof, ...]
    authorization: CleanupAuthorizationRequest
    permit: DeletionPermit


@dataclass(frozen=True)
class BranchRemovalRequest:
    repository: str
    task: str
    branch_ref: str
    expected_head: str
    retained_refs: tuple[tuple[str, str], ...]
    intent: CleanupIntent
    authorization: CleanupAuthorizationRequest
    permit: DeletionPermit


@dataclass(frozen=True)
class EphemeralRemovalRequest:
    spec: Any
    relative_scope: str
    inventory: Any
    expected_nodes: tuple[Any, ...]
    intent: CleanupIntent
    binding: OwnedResourceBinding
    authorization: CleanupAuthorizationRequest
    permit: DeletionPermit


class QualifiedDeletionCapability:
    """One operation-specific, trusted-host deletion backend.

    The callback receives only its immutable typed request. The descriptor is
    checked against the current repository context, but its claims are not
    independently proved here. The trusted host must bind this facade to a
    separately audited atomic-custody implementation; an ordinary unlink, Git
    --force command, or callback acknowledgement is never treated as equivalent.
    """

    __slots__ = ("operation", "qualification", "_apply")

    def __init__(
        self,
        operation: str,
        qualification: CleanupQualification,
        *,
        apply: Callable[[object], object],
    ) -> None:
        if operation not in {"worktree", "branch", "ephemeral"}:
            raise TaskCleanupError("invalid_deletion_operation")
        if type(qualification) is not CleanupQualification or qualification.operation != operation:
            raise TaskCleanupError("invalid_backend_qualification")
        if not callable(apply):
            raise TaskCleanupError("deletion_capability_required")
        self.operation = operation
        self.qualification = qualification
        self._apply = apply

    def _invoke(self, request: object) -> object:
        if self.operation == "worktree":
            if type(request) is not WorktreeRemovalRequest:
                raise TaskCleanupError("invalid_worktree_removal_request", "worktree")
            intent = request.intent
            if type(intent) is not CleanupIntent:
                raise TaskCleanupError("invalid_worktree_removal_request", "worktree")
            target_digest = intent.manifest_digest
            spec = request.spec
            inventory = request.inventory
            if (
                type(intent) is not CleanupIntent
                or intent.operation != "cleanup.worktree_and_branch"
                or getattr(spec, "repository", None) != intent.repository
                or getattr(spec, "task", None) != intent.task
                or getattr(spec, "branch_ref", None) != intent.branch_ref
                or getattr(spec, "head", None) != intent.target_head
                or getattr(spec, "worktree_root", None) != intent.worktree_root
                or getattr(spec, "git_admin", None) != intent.git_admin
                or getattr(inventory, "spec", None) != spec
                or getattr(inventory, "inventory_id", None) != intent.inventory_id
                or getattr(inventory, "git_pointer_sha256", None) != intent.git_pointer_sha256
                or _identity(inventory.root_identity) != intent.root_identity
                or _identity(inventory.parent_identity) != intent.parent_identity
                or tuple(_node_fact(node) for node in inventory.nodes) != intent.nodes
            ):
                raise TaskCleanupError("worktree_removal_binding_mismatch", "worktree")
            inventory_digest = hashlib.sha256(_canonical_json([
                node.__dict__ for node in intent.nodes
            ])).hexdigest()
            if inventory_digest != intent.manifest_digest:
                raise TaskCleanupError("worktree_inventory_digest_mismatch", "worktree")
            supplied_proofs = _proof_facts(request.proofs)
            if supplied_proofs != intent.resource_proofs:
                raise TaskCleanupError("worktree_resource_proof_mismatch", "worktree")
        elif self.operation == "branch":
            if type(request) is not BranchRemovalRequest:
                raise TaskCleanupError("invalid_branch_removal_request", "branch")
            intent = request.intent
            if type(intent) is not CleanupIntent:
                raise TaskCleanupError("invalid_branch_removal_request", "branch")
            target_digest = hashlib.sha256(_canonical_json([
                request.branch_ref, request.expected_head,
            ])).hexdigest()
            if (
                type(intent) is not CleanupIntent
                or intent.operation != "cleanup.worktree_and_branch"
                or request.repository != intent.repository or request.task != intent.task
                or request.branch_ref != intent.branch_ref or request.expected_head != intent.target_head
                or request.retained_refs != ((intent.default_branch_ref, intent.default_head),)
            ):
                raise TaskCleanupError("branch_removal_binding_mismatch", "branch")
        else:
            if type(request) is not EphemeralRemovalRequest:
                raise TaskCleanupError("invalid_ephemeral_removal_request", "ephemeral")
            intent = request.intent
            if type(intent) is not CleanupIntent or type(request.binding) is not OwnedResourceBinding:
                raise TaskCleanupError("invalid_ephemeral_removal_request", "ephemeral")
            target_digest = hashlib.sha256(_canonical_json([
                request.relative_scope,
                [_node_fact(node).__dict__ for node in request.expected_nodes],
            ])).hexdigest()
            if (
                type(intent) is not CleanupIntent or intent.operation != "cleanup.ephemeral"
                or getattr(request.spec, "repository", None) != intent.repository
                or getattr(request.spec, "task", None) != intent.task
                or getattr(request.spec, "branch_ref", None) != intent.branch_ref
                or getattr(request.spec, "head", None) != intent.target_head
                or getattr(request.spec, "worktree_root", None) != intent.worktree_root
                or getattr(request.spec, "git_admin", None) != intent.git_admin
                or request.relative_scope != intent.ephemeral_scope
                or request.binding.binding_id != intent.ephemeral_binding_id
                or getattr(request.inventory, "inventory_id", None) != intent.inventory_id
                or getattr(request.inventory, "git_pointer_sha256", None) != intent.git_pointer_sha256
                or _identity(request.inventory.root_identity) != intent.root_identity
                or _identity(request.inventory.parent_identity) != intent.parent_identity
                or tuple(_node_fact(node) for node in request.inventory.nodes) != intent.nodes
                or tuple(
                    _node_fact(node) for node in request.expected_nodes
                ) != tuple(
                    node for node in intent.nodes
                    if node.relative_path == intent.ephemeral_scope
                    or node.relative_path.startswith(intent.ephemeral_scope + "/")
                )
            ):
                raise TaskCleanupError("ephemeral_removal_binding_mismatch", "ephemeral")

        authorization = request.authorization
        permit = request.permit
        qualification = self.qualification
        try:
            expected_common = Path(intent.git_admin).parent.parent
            expected_common_identity = _identity_path(expected_common)
            expected_namespace = Path(intent.worktree_root).parent
        except Exception:
            raise TaskCleanupError("backend_scope_revalidation_failed", self.operation) from None
        if (
            type(qualification) is not CleanupQualification
            or qualification.operation != self.operation
            or type(qualification.qualification_id) is not str
            or not _HEX64.fullmatch(qualification.qualification_id)
            or qualification.repository != intent.repository
            or qualification.common_directory != str(expected_common)
            or qualification.common_identity != expected_common_identity
            or qualification.default_branch_ref != intent.default_branch_ref
            or qualification.worktree_namespace != str(expected_namespace)
            or any(flag is not True for flag in (
                qualification.descriptor_pinned,
                qualification.atomic_identity,
                qualification.atomic_custody,
                qualification.current_authorization,
                qualification.expected_oid_compare_exchange,
                qualification.preserves_product_data,
                qualification.no_force,
                qualification.no_remote_refs,
            ))
        ):
            raise TaskCleanupError("backend_scope_revalidation_failed", self.operation)
        if (
            type(authorization) is not CleanupAuthorizationRequest
            or type(permit) is not DeletionPermit
            or permit._seal is not _PERMIT_SEAL
            or permit.operation != self.operation
            or permit.qualification_id != self.qualification.qualification_id
            or permit.intent_id != intent.intent_id
            or permit.target_digest != target_digest
            or authorization.intent_id != intent.intent_id
            or authorization.intent_bytes != intent.canonical_bytes()
            or authorization.operation != f"cleanup.{self.operation}"
            or authorization.step != self.operation
            or authorization.repository != intent.repository
            or authorization.task != intent.task
            or authorization.branch_ref != intent.branch_ref
            or authorization.expected_head != intent.target_head
            or authorization.record_id != intent.record_id
            or authorization.contract_id != intent.contract_id
            or authorization.default_branch_ref != intent.default_branch_ref
            or authorization.default_head != intent.default_head
            or authorization.root_identity != intent.root_identity
            or authorization.inventory_id != intent.inventory_id
            or authorization.manifest_digest != intent.manifest_digest
            or authorization.ownership_proof_ids != tuple(
                proof.proof_id for proof in intent.resource_proofs
            )
            or authorization.resource_scope != intent.ephemeral_scope
        ):
            raise TaskCleanupError("deletion_permit_binding_mismatch", self.operation)
        request_value = dict(authorization.__dict__)
        request_value["intent_bytes"] = hashlib.sha256(authorization.intent_bytes).hexdigest()
        expected_authorization_digest = hashlib.sha256(
            authorization.intent_bytes + _canonical_json(request_value),
        ).hexdigest()
        if permit.authorization_digest != expected_authorization_digest:
            raise TaskCleanupError("deletion_authorization_digest_mismatch", self.operation)
        return self._apply(request)


@dataclass(frozen=True)
class CleanupAcknowledgement:
    """Exact read-back postconditions, not a Task lifecycle state or receipt."""

    intent_id: str
    operation: str
    worktree_root_absent: bool
    git_admin_absent: bool
    task_branch_absent: bool
    retained_head_exists: bool
    canonical_record_preserved: bool
    ephemeral_scope_absent: bool = False
    intent_reference: str | None = None


@dataclass(frozen=True)
class CleanupPlan:
    intent: CleanupIntent
    spec: Any
    inventory: Any
    proofs: tuple[DisposableResourceProof, ...]
    task_view_bytes: bytes


@dataclass(frozen=True)
class _TaskAuthority:
    task: str
    record_id: str
    contract_id: str
    base_revision: str
    branch_ref: str
    disposition: str | None
    metadata_tip: str


@dataclass(frozen=True)
class _MainFacts:
    default_branch_ref: str
    default_head: str
    common_directory: str
    common_identity: tuple[int, int, int]
    root_identity: tuple[int, int, int]
    git_admin_identity: tuple[int, int, int]
    head: str
    tree: str
    branch_ref: str
    index_digest: str
    status_digest: str
    refs: tuple[tuple[str, str], ...]
    worktrees: tuple[tuple[str, str], ...]


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise TaskCleanupError("invalid_cleanup_value") from None


def _identity(value: object) -> tuple[int, int, int]:
    try:
        device = value.device
        inode = value.inode
        mount_id = value.mount_id
    except Exception:
        raise TaskCleanupError("invalid_filesystem_identity") from None
    if any(type(item) is not int or item < 0 for item in (device, inode, mount_id)):
        raise TaskCleanupError("invalid_filesystem_identity")
    return device, inode, mount_id


def _identity_path(path: Path) -> tuple[int, int, int]:
    """Read one no-follow directory identity and Linux mount ID."""
    if sys.platform != "linux" or not Path("/proc/self/fd").is_dir():
        raise TaskCleanupError("stable_filesystem_identity_unsupported")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            named = path.lstat()
            if (
                not stat.S_ISDIR(opened.st_mode) or stat.S_ISLNK(named.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise TaskCleanupError("filesystem_identity_changed")
            with open(f"/proc/self/fdinfo/{descriptor}", "rb") as info_file:
                info = info_file.read(16 * 1024)
            match = re.search(rb"(?m)^mnt_id:\s*([0-9]+)\s*$", info)
            if match is None:
                raise TaskCleanupError("mount_identity_unavailable")
            mount_id = int(match.group(1))
            after = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
                raise TaskCleanupError("filesystem_identity_changed")
            return opened.st_dev, opened.st_ino, mount_id
        finally:
            os.close(descriptor)
    except TaskCleanupError:
        raise
    except (OSError, ValueError):
        raise TaskCleanupError("filesystem_identity_unavailable") from None


def _identity_fd(descriptor: int) -> tuple[int, int, int]:
    """Read a directory descriptor identity including its Linux mount ID."""
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise TaskCleanupError("unsafe_filesystem_directory")
    return info.st_dev, info.st_ino, _mount_id_fd(descriptor)


def _mount_id_fd(descriptor: int) -> int:
    """Read Linux mount identity for a regular file or directory descriptor."""
    if sys.platform != "linux" or not Path("/proc/self/fd").is_dir():
        raise TaskCleanupError("stable_filesystem_identity_unsupported")
    try:
        with open(f"/proc/self/fdinfo/{descriptor}", "rb") as info_file:
            data = info_file.read(16 * 1024)
        match = re.search(rb"(?m)^mnt_id:\s*([0-9]+)\s*$", data)
        if match is None:
            raise TaskCleanupError("mount_identity_unavailable")
        return int(match.group(1))
    except TaskCleanupError:
        raise
    except (OSError, ValueError):
        raise TaskCleanupError("filesystem_identity_unavailable") from None


def _oid(value: object, oid_length: int, code: str = "invalid_git_identity") -> str:
    if type(value) is not str or len(value) != oid_length or not _OID.fullmatch(value):
        raise TaskCleanupError(code)
    return value


def _relative(value: object) -> str:
    if (
        type(value) is not str or not value or len(value.encode("utf-8", errors="strict")) > MAX_PATH_BYTES
        or value.startswith("/") or "\\" in value or "\x00" in value
    ):
        raise TaskCleanupError("invalid_resource_scope")
    parts = value.split("/")
    if any(part in {"", ".", "..", ".git"} for part in parts):
        raise TaskCleanupError("protected_resource_scope")
    return value


def _node_fact(node: object) -> CleanupNodeFact:
    try:
        relative_path = _relative(node.relative_path)
        kind = node.kind
        identity = node.identity
        device, inode, mount_id = _identity(identity)
        mode = identity.mode
        uid = identity.uid
        nlink = identity.nlink
        size = node.size
        mtime_ns = node.mtime_ns
        ctime_ns = node.ctime_ns
        digest = node.content_sha256
    except TaskCleanupError:
        raise
    except Exception:
        raise TaskCleanupError("invalid_inventory_node") from None
    if kind not in {"file", "directory"}:
        raise TaskCleanupError("unsupported_inventory_node")
    if any(type(number) is not int or number < 0 for number in (mode, uid, nlink, size, mtime_ns, ctime_ns)):
        raise TaskCleanupError("invalid_inventory_node")
    if kind == "file":
        if not stat.S_ISREG(mode) or nlink != 1 or type(digest) is not str or not _HEX64.fullmatch(digest):
            raise TaskCleanupError("unsafe_inventory_file")
    elif not stat.S_ISDIR(mode) or digest is not None:
        raise TaskCleanupError("unsafe_inventory_directory")
    return CleanupNodeFact(
        relative_path, kind, device, inode, mount_id, mode, uid, nlink,
        size, mtime_ns, ctime_ns, digest,
    )


def _identity_value(identity: tuple[int, int, int]) -> list[int]:
    return list(identity)


def _intent_value(intent: CleanupIntent) -> dict[str, Any]:
    return {
        "schema_version": intent.schema_version,
        "operation": intent.operation,
        "repository": intent.repository,
        "task": intent.task,
        "branch_ref": intent.branch_ref,
        "base_revision": intent.base_revision,
        "record_id": intent.record_id,
        "contract_id": intent.contract_id,
        "disposition": intent.disposition,
        "target_head": intent.target_head,
        "metadata_tip": intent.metadata_tip,
        "main_state_fingerprint": intent.main_state_fingerprint,
        "default_branch_ref": intent.default_branch_ref,
        "default_head": intent.default_head,
        "worktree_root": intent.worktree_root,
        "git_admin": intent.git_admin,
        "root_identity": _identity_value(intent.root_identity),
        "root_nlink": intent.root_nlink,
        "parent_identity": _identity_value(intent.parent_identity),
        "admin_parent_identity": _identity_value(intent.admin_parent_identity),
        "admin_identity": _identity_value(intent.admin_identity),
        "git_pointer_sha256": intent.git_pointer_sha256,
        "inventory_id": intent.inventory_id,
        "manifest_digest": intent.manifest_digest,
        "nodes": [node.__dict__ for node in intent.nodes],
        "resource_proofs": [proof.__dict__ for proof in intent.resource_proofs],
        "remote_task_head": intent.remote_task_head,
        "retention_fingerprint": intent.retention_fingerprint,
        "pull_request_head": intent.pull_request_head,
        "pull_request_merge_commit": intent.pull_request_merge_commit,
        "ephemeral_scope": intent.ephemeral_scope,
        "ephemeral_binding_id": intent.ephemeral_binding_id,
        "resource_reference": None if intent.resource_reference is None else {
            "owner": intent.resource_reference.owner,
            "token": intent.resource_reference.token,
        },
    }


def _proof_facts(proofs: object) -> tuple[CleanupProofFact, ...]:
    if type(proofs) is not tuple or any(type(proof) is not DisposableResourceProof for proof in proofs):
        raise TaskCleanupError("invalid_resource_proof_set")
    return tuple(
        CleanupProofFact(
            proof.relative_scope, proof.resource_kind, proof.producer, proof.proof_id,
            hashlib.sha256(_canonical_json([
                _node_fact(node).__dict__ for node in proof.node_manifest
            ])).hexdigest(),
        )
        for proof in proofs
    )


def _task_context(task: object) -> str:
    if type(task) is not str or len(task) > 128 or not _TASK.fullmatch(task):
        raise TaskCleanupError("invalid_task")
    return task


class CoreTaskCleanup:
    """Prepare, authorize, remove, verify, and recover one exact Task's resources.

    The constructor is a trusted-host assembly boundary.  Host callbacks are
    copied into this instance, and all destructive effects require the exact
    operation-specific capability and literal-``True`` authorization.  The
    caller supplies only a canonical positive Task ID, an opaque journal
    reference, or a typed opaque resource reference; it never supplies a branch
    name, path, permission boolean, or force flag.
    """

    def __init__(
        self,
        main_store: MetadataStore,
        *,
        host_prerequisites: Mapping[str, operation_prerequisites.OperationPrerequisites],
        host_authorizer: Callable[[CleanupAuthorizationRequest], object],
        resource_classifier: object,
        record_intent: Callable[[CleanupIntent], object],
        load_intent: Callable[[str], object],
        qualified_deletion_capabilities: Mapping[str, QualifiedDeletionCapability],
        retention_reader: Callable[[TaskRetentionRequest], object] | None = None,
    ) -> None:
        if not isinstance(main_store, MetadataStore):
            raise TaskCleanupError("invalid_metadata_store")
        if type(host_prerequisites) is not dict:
            raise TaskCleanupError("invalid_prerequisite_registry")
        if any(key not in _PREREQUISITE_OPERATIONS for key in host_prerequisites):
            raise TaskCleanupError("invalid_prerequisite_registry")
        if any(
            type(engine) is not operation_prerequisites.OperationPrerequisites
            for engine in host_prerequisites.values()
        ):
            raise TaskCleanupError("invalid_prerequisite_engine")
        if not callable(host_authorizer) or not callable(record_intent) or not callable(load_intent):
            raise TaskCleanupError("host_capability_required")
        if not (callable(resource_classifier) or callable(getattr(resource_classifier, "classify", None))):
            raise TaskCleanupError("resource_classifier_required")
        if type(qualified_deletion_capabilities) is not dict:
            raise TaskCleanupError("invalid_deletion_registry")
        if any(
            key not in {"worktree", "branch", "ephemeral"}
            or type(capability) is not QualifiedDeletionCapability
            or capability.operation != key
            for key, capability in qualified_deletion_capabilities.items()
        ):
            raise TaskCleanupError("invalid_deletion_registry")
        if retention_reader is not None and not callable(retention_reader):
            raise TaskCleanupError("invalid_retention_reader")

        self._store = main_store
        self._prerequisites = dict(host_prerequisites)
        self._authorizer = host_authorizer
        self._classifier = resource_classifier
        self._record_intent = record_intent
        self._load_intent = load_intent
        self._deletion = dict(qualified_deletion_capabilities)
        self._retention_reader = retention_reader

    def _git(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        max_stdout: int = MAX_GIT_OUTPUT_BYTES,
        store: MetadataStore | None = None,
        input_data: bytes | None = None,
    ) -> bytes | None:
        runner = self._store if store is None else store
        try:
            return runner._git(
                arguments, input_data=input_data, check=check, max_stdout=max_stdout,
                extra_env={"GIT_OPTIONAL_LOCKS": "0"},
            )
        except Exception:
            raise TaskCleanupError("git_observation_failed") from None

    def _text(self, output: bytes | None, code: str = "invalid_git_output") -> str:
        if output is None:
            raise TaskCleanupError(code)
        try:
            return output.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            raise TaskCleanupError(code) from None

    def _read_authority(self, task: str) -> _TaskAuthority:
        try:
            tip = self._store.fetch_tip()
            if tip is None:
                raise TaskCleanupError("metadata_ref_absent")
            entry = self._store.read_record(tip, task)
            if entry is None:
                raise TaskCleanupError("task_record_absent")
            record_id, envelope = entry
            base = envelope["subject"]
            branch_ref = task_record.validate_branch_ref(envelope["payload"]["branch_ref"])
            records = task_record.TaskRecords(self._store, authorize=lambda _request: False)
            resolved = records.read(tip, task=task, base_revision=base, branch_ref=branch_ref)
            if resolved is None or resolved[0] != record_id:
                raise TaskCleanupError("task_record_binding_mismatch")
            record_id, record, _contract = resolved
            disposition = record["payload"].get("disposition")
            if disposition is None:
                disposition_kind = None
            elif (
                type(disposition) is dict
                and disposition.get("kind") in {"cancelled", "superseded"}
            ):
                disposition_kind = disposition["kind"]
            else:
                raise TaskCleanupError("task_disposition_invalid")
            return _TaskAuthority(
                task, record_id, record["payload"]["contract_id"], base, branch_ref,
                disposition_kind, tip,
            )
        except TaskCleanupError:
            raise
        except Exception:
            raise TaskCleanupError("task_authority_unavailable") from None

    def _main_facts(self) -> _MainFacts:
        try:
            root = self._store.root
            task_view._check_git_configuration(root)
            config_keys = self._git(
                ["config", "--local", "--no-includes", "--name-only", "--null", "--list"],
                max_stdout=1024 * 1024,
            )
            if config_keys is None:
                raise TaskCleanupError("git_configuration_unavailable")
            try:
                names = {
                    item.decode("ascii", errors="strict").lower()
                    for item in config_keys.split(b"\0") if item
                }
            except UnicodeDecodeError:
                raise TaskCleanupError("unsafe_git_configuration") from None
            if names & {
                "core.worktree", "core.sparsecheckout", "core.ignorecase",
                "extensions.worktreeconfig", "index.sparse", "core.attributesfile",
                "core.fsmonitor", "core.untrackedcache",
            }:
                raise TaskCleanupError("unsafe_git_configuration")
            top = Path(self._text(self._git(["rev-parse", "--show-toplevel"], max_stdout=16 * 1024)))
            if top.resolve(strict=True) != root:
                raise TaskCleanupError("main_worktree_binding_mismatch")
            branch_ref = self._text(self._git(["symbolic-ref", "--quiet", "HEAD"], max_stdout=4096))
            task_record.validate_branch_ref(branch_ref)
            head = _oid(self._text(self._git(["rev-parse", "--verify", "HEAD^{commit}"], max_stdout=128)), self._store._oid_length)
            default_head = _oid(
                self._text(self._git(["rev-parse", "--verify", "--end-of-options", f"{branch_ref}^{{commit}}"], max_stdout=128)),
                self._store._oid_length,
            )
            if head != default_head:
                raise TaskCleanupError("default_ref_head_mismatch")
            if self._git(["symbolic-ref", "--quiet", "--no-recurse", branch_ref], check=False, max_stdout=4096) is not None:
                raise TaskCleanupError("symbolic_default_ref")
            tree = _oid(self._text(self._git(["rev-parse", "--verify", "HEAD^{tree}"], max_stdout=128)), self._store._oid_length)
            common = Path(self._text(self._git(["rev-parse", "--path-format=absolute", "--git-common-dir"], max_stdout=16 * 1024)))
            self._check_path_components(common)
            common_identity = _identity_path(common)
            self._check_path_components(root)
            root_identity = _identity_path(root)
            git_entry = root / ".git"
            git_info = git_entry.lstat()
            if stat.S_ISLNK(git_info.st_mode) or not stat.S_ISDIR(git_info.st_mode):
                raise TaskCleanupError("main_git_admin_not_primary")
            git_admin_identity = _identity_path(git_entry)
            if git_admin_identity != common_identity:
                raise TaskCleanupError("main_git_admin_binding_mismatch")
            refs = self._refs()
            index_before = self._main_index_bytes()
            status = self._git(["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=none"], max_stdout=MAX_GIT_OUTPUT_BYTES)
            cached = self._git(["diff", "--cached", "--raw", "--no-abbrev", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", "HEAD", "--"], max_stdout=MAX_GIT_OUTPUT_BYTES)
            index_after = self._main_index_bytes()
            if status is None or cached is None or index_before != index_after:
                raise TaskCleanupError("main_state_unavailable")
            return _MainFacts(
                branch_ref, default_head, str(common), common_identity,
                root_identity, git_admin_identity, head, tree,
                branch_ref, hashlib.sha256(index_after).hexdigest(),
                self._main_mutable_files_digest(status, cached), refs,
                self._worktree_preservation_facts(common),
            )
        except TaskCleanupError:
            raise
        except Exception:
            raise TaskCleanupError("main_state_unavailable") from None

    def _refs(self) -> tuple[tuple[str, str], ...]:
        output = self._git(["for-each-ref", "--format=%(refname)%00%(objectname)"], max_stdout=MAX_GIT_OUTPUT_BYTES)
        if output is None:
            raise TaskCleanupError("refs_unavailable")
        refs: list[tuple[str, str]] = []
        for line in output.splitlines():
            fields = line.split(b"\0")
            if len(fields) != 2:
                raise TaskCleanupError("invalid_ref_output")
            try:
                ref = fields[0].decode("utf-8", errors="strict")
                oid = fields[1].decode("ascii", errors="strict")
            except UnicodeDecodeError:
                raise TaskCleanupError("invalid_ref_output") from None
            if not ref.startswith("refs/") or not _OID.fullmatch(oid):
                raise TaskCleanupError("invalid_ref_output")
            refs.append((ref, oid))
        refs.sort()
        if len(refs) != len({ref for ref, _oid_value in refs}):
            raise TaskCleanupError("duplicate_ref")
        return tuple(refs)

    def _main_index_bytes(self) -> bytes:
        text = self._text(self._git(
            ["rev-parse", "--path-format=absolute", "--git-path", "index"], max_stdout=16 * 1024,
        ))
        path = Path(text)
        if not path.is_absolute():
            path = self._store.root / path
        self._check_path_components(path.parent)
        try:
            return self._read_regular_nofollow(path, 16 * 1024 * 1024)
        except TaskCleanupError as error:
            if error.code == "regular_file_unavailable":
                try:
                    path.lstat()
                except FileNotFoundError:
                    return b"<index-absent>"
            raise

    def _registered_managed_worktree_paths(self) -> tuple[str, ...]:
        """Return exact, physically validated registered roots under .worktrees.

        Only these exact native Git registrations are excluded from the main
        worktree's status/content fingerprint.  A path-prefix match, ignored
        directory name, or unregistered directory is never sufficient.
        """
        repository_root = self._store.root
        namespace = repository_root / ".worktrees"
        try:
            namespace_info = namespace.lstat()
        except FileNotFoundError:
            return ()
        except OSError:
            raise TaskCleanupError("managed_worktree_namespace_unavailable") from None
        if stat.S_ISLNK(namespace_info.st_mode) or not stat.S_ISDIR(namespace_info.st_mode):
            raise TaskCleanupError("unsafe_managed_worktree_namespace")
        self._check_path_components(namespace)
        namespace_identity = _identity_path(namespace)
        common_text = self._text(self._git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], max_stdout=16 * 1024,
        ))
        common = Path(common_text)
        self._check_path_components(common)
        common_identity = _identity_path(common)
        if common_identity[2] != namespace_identity[2]:
            raise TaskCleanupError("managed_worktree_mount_mismatch")

        def linked_target(data: bytes, *, prefix: bytes | None, base: Path) -> Path:
            if prefix is not None:
                if not data.startswith(prefix):
                    raise TaskCleanupError("invalid_managed_git_pointer")
                data = data[len(prefix):]
            if not data.endswith(b"\n") or data.count(b"\n") != 1 or b"\0" in data or b"\r" in data:
                raise TaskCleanupError("invalid_managed_git_pointer")
            try:
                value = data[:-1].decode("utf-8", errors="strict")
                target = Path(value)
            except (UnicodeDecodeError, ValueError):
                raise TaskCleanupError("invalid_managed_git_pointer") from None
            if not target.is_absolute():
                target = base / target
            target = Path(os.path.normpath(str(target)))
            if not target.is_absolute():
                raise TaskCleanupError("invalid_managed_git_pointer")
            self._check_path_components(target)
            if target.resolve(strict=True) != target:
                raise TaskCleanupError("noncanonical_managed_git_pointer")
            return target

        excluded: list[str] = []
        for entry in self._worktrees():
            try:
                registered_root = Path(entry["worktree"])
                branch_ref = entry.get("branch")
                head = entry.get("head")
                if (
                    registered_root.parent != namespace
                    or registered_root == repository_root
                    or type(branch_ref) is not str
                    or type(head) is not str
                ):
                    continue
                task_record.validate_branch_ref(branch_ref)
                _oid(head, self._store._oid_length, "invalid_registered_worktree_head")
                if self._branch_oid(branch_ref) != head:
                    raise TaskCleanupError("registered_worktree_branch_mismatch")
                self._check_path_components(registered_root)
                root_info = registered_root.lstat()
                if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
                    raise TaskCleanupError("registered_worktree_root_changed")
                root_identity = _identity_path(registered_root)
                if root_identity[2] != namespace_identity[2]:
                    raise TaskCleanupError("registered_worktree_mount_mismatch")

                pointer_path = registered_root / ".git"
                pointer_info = pointer_path.lstat()
                if (
                    stat.S_ISLNK(pointer_info.st_mode) or not stat.S_ISREG(pointer_info.st_mode)
                    or pointer_info.st_nlink != 1
                ):
                    raise TaskCleanupError("unsafe_registered_git_pointer")
                pointer = self._read_regular_nofollow(pointer_path, 4096)
                admin = linked_target(pointer, prefix=b"gitdir: ", base=registered_root)
                if admin.parent.name != "worktrees" or admin.parent.parent != common:
                    raise TaskCleanupError("registered_git_admin_mismatch")
                admin_info = admin.lstat()
                if stat.S_ISLNK(admin_info.st_mode) or not stat.S_ISDIR(admin_info.st_mode):
                    raise TaskCleanupError("unsafe_registered_git_admin")
                if _identity_path(admin)[2] != namespace_identity[2]:
                    raise TaskCleanupError("registered_git_admin_mount_mismatch")
                backlink = self._read_regular_nofollow(admin / "gitdir", 4096)
                common_link = self._read_regular_nofollow(admin / "commondir", 4096)
                if (
                    linked_target(backlink, prefix=None, base=admin) != pointer_path
                    or linked_target(common_link, prefix=None, base=admin) != common
                ):
                    raise TaskCleanupError("registered_git_admin_reciprocal_mismatch")
                relative = registered_root.relative_to(repository_root).as_posix()
                excluded.append(_relative(relative))
            except TaskCleanupError:
                raise
            except Exception:
                raise TaskCleanupError("registered_worktree_binding_failed") from None
        return tuple(sorted(set(excluded)))

    def _main_mutable_files_digest(self, status: bytes, cached: bytes) -> str:
        registered_roots = self._registered_managed_worktree_paths()
        paths: set[str] = set()

        def include_path(raw_path: bytes) -> bool:
            try:
                decoded = raw_path.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise TaskCleanupError("non_utf8_main_path") from None
            # Porcelain may report a registered nested worktree directory with
            # a trailing slash. Normalize only that representation before an
            # exact-root or slash-boundary membership test.
            normalized = decoded.rstrip("/")
            if (
                not normalized or normalized.startswith("/") or "\\" in normalized
                or "\x00" in normalized
                or any(part in {"", ".", ".."} for part in normalized.split("/"))
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized)
            ):
                raise TaskCleanupError("invalid_main_status_path")
            if any(normalized == root or normalized.startswith(root + "/") for root in registered_roots):
                return False
            paths.add(_relative(normalized))
            return True

        rows = status.split(b"\0")
        position = 0
        retained_status: list[bytes] = []
        while position < len(rows):
            row = rows[position]
            position += 1
            if not row:
                continue
            original_path = None
            if row.startswith(b"? "):
                raw_path = row[2:]
            elif row.startswith(b"1 "):
                fields = row.split(b" ", 8)
                if len(fields) != 9:
                    raise TaskCleanupError("invalid_main_status")
                raw_path = fields[8]
            elif row.startswith(b"2 "):
                fields = row.split(b" ", 9)
                if len(fields) != 10 or position >= len(rows):
                    raise TaskCleanupError("invalid_main_status")
                raw_path = fields[9]
                original_path = rows[position]
                if not include_path(original_path):
                    # Tracked changes must never be hidden by a nested
                    # registered worktree's path.
                    raise TaskCleanupError("managed_worktree_contains_main_product")
                position += 1
            elif row.startswith(b"u "):
                fields = row.split(b" ", 10)
                if len(fields) != 11:
                    raise TaskCleanupError("invalid_main_status")
                raw_path = fields[10]
            else:
                raise TaskCleanupError("invalid_main_status")
            included = include_path(raw_path)
            if not included and not row.startswith(b"? "):
                raise TaskCleanupError("managed_worktree_contains_main_product")
            if included:
                retained_status.append(row + b"\0")
                if original_path is not None:
                    retained_status.append(original_path + b"\0")

        ignored = self._git(
            ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
            max_stdout=MAX_GIT_OUTPUT_BYTES,
        )
        if ignored is None:
            raise TaskCleanupError("main_ignored_paths_unavailable")
        for raw_path in ignored.split(b"\0"):
            if not raw_path:
                continue
            include_path(raw_path)
        if len(paths) > MAX_INVENTORY_NODES:
            raise TaskCleanupError("main_dirty_path_limit")

        facts: list[dict[str, Any]] = []
        total_bytes = 0
        for relative_path in sorted(paths):
            selected = self._read_regular_under_root(
                self._store.root, relative_path, MAX_TRACKED_FILE_BYTES,
            )
            if selected is None:
                facts.append({"path": relative_path, "absent": True})
                continue
            before, data = selected
            if before.st_size > MAX_TRACKED_FILE_BYTES:
                raise TaskCleanupError("main_dirty_file_size_limit")
            total_bytes += before.st_size
            if total_bytes > MAX_TRACKED_TOTAL_BYTES:
                raise TaskCleanupError("main_dirty_total_size_limit")
            facts.append({
                "path": relative_path,
                "device": before.st_dev,
                "inode": before.st_ino,
                "mode": before.st_mode,
                "uid": before.st_uid,
                "nlink": before.st_nlink,
                "size": before.st_size,
                "mtime_ns": before.st_mtime_ns,
                "ctime_ns": before.st_ctime_ns,
                "content_sha256": hashlib.sha256(data).hexdigest(),
            })
        return hashlib.sha256(b"".join(retained_status) + b"\0" + cached + b"\0" + _canonical_json(facts)).hexdigest()

    def _worktree_preservation_facts(self, common: Path) -> tuple[tuple[str, str], ...]:
        """Pin every registration/root/admin, including protected siblings.

        Filtering nested worktrees out of the main product-status digest must
        not also hide their disappearance or replacement. Only the exact Task
        entry can be omitted by a later operation-specific comparison.
        """
        entries = self._worktrees()
        facts: list[tuple[str, str]] = []
        for entry in entries:
            root = Path(entry["worktree"])
            self._check_path_components(root)
            root_id = _identity_path(root)
            if root == self._store.root:
                admin = common
                pointer_digest = None
                backlink_digest = None
            else:
                pointer = self._read_regular_nofollow(root / ".git", 4096)
                if not pointer.startswith(b"gitdir: ") or pointer.count(b"\n") != 1 or not pointer.endswith(b"\n"):
                    raise TaskCleanupError("invalid_registered_git_pointer")
                try:
                    admin = Path(pointer[8:-1].decode("utf-8", errors="strict"))
                except UnicodeDecodeError:
                    raise TaskCleanupError("invalid_registered_git_pointer") from None
                if not admin.is_absolute():
                    admin = root / admin
                if admin.parent != common / "worktrees":
                    raise TaskCleanupError("registered_git_admin_mismatch")
                self._check_path_components(admin)
                backlink = self._read_regular_nofollow(admin / "gitdir", 4096)
                if backlink != str(root / ".git").encode("utf-8") + b"\n":
                    raise TaskCleanupError("registered_git_admin_reciprocal_mismatch")
                pointer_digest = hashlib.sha256(pointer).hexdigest()
                backlink_digest = hashlib.sha256(backlink).hexdigest()
            admin_id = _identity_path(admin)
            facts.append((str(root), hashlib.sha256(_canonical_json({
                "registration": entry, "root_identity": root_id,
                "admin": str(admin), "admin_identity": admin_id,
                "pointer_digest": pointer_digest, "backlink_digest": backlink_digest,
            })).hexdigest()))
        if self._worktrees() != entries:
            raise TaskCleanupError("worktree_registry_changed")
        return tuple(sorted(facts))

    def _assert_main_stable(
        self, before: _MainFacts, *, removed_ref: str | None = None,
        removed_worktree: str | None = None,
    ) -> _MainFacts:
        after = self._main_facts()
        old_refs = dict(before.refs)
        new_refs = dict(after.refs)
        if removed_ref is not None:
            old_refs.pop(removed_ref, None)
            new_refs.pop(removed_ref, None)
        if (
            after.default_branch_ref != before.default_branch_ref
            or after.default_head != before.default_head
            or after.common_directory != before.common_directory
            or after.common_identity != before.common_identity
            or after.root_identity != before.root_identity
            or after.git_admin_identity != before.git_admin_identity
            or after.head != before.head or after.tree != before.tree
            or after.branch_ref != before.branch_ref
            or after.index_digest != before.index_digest
            or after.status_digest != before.status_digest
            or old_refs != new_refs
            or tuple(item for item in after.worktrees if item[0] != removed_worktree)
            != tuple(item for item in before.worktrees if item[0] != removed_worktree)
        ):
            raise TaskCleanupError("main_repository_state_changed")
        return after

    @staticmethod
    def _main_state_fingerprint(
        main: _MainFacts, *, removed_ref: str | None = None,
        removed_worktree: str | None = None,
    ) -> str:
        refs = tuple(item for item in main.refs if item[0] != removed_ref)
        value = {
            "default_branch_ref": main.default_branch_ref,
            "default_head": main.default_head,
            "common_directory": main.common_directory,
            "common_identity": list(main.common_identity),
            "root_identity": list(main.root_identity),
            "git_admin_identity": list(main.git_admin_identity),
            "head": main.head,
            "tree": main.tree,
            "branch_ref": main.branch_ref,
            "index_digest": main.index_digest,
            "status_digest": main.status_digest,
            "refs": [list(item) for item in refs],
            "worktrees": [list(item) for item in main.worktrees if item[0] != removed_worktree],
        }
        return hashlib.sha256(_canonical_json(value)).hexdigest()

    def _worktrees(self) -> tuple[dict[str, Any], ...]:
        raw = self._git(["worktree", "list", "--porcelain", "-z"], max_stdout=MAX_GIT_OUTPUT_BYTES)
        if raw is None:
            raise TaskCleanupError("worktree_registry_unavailable")
        records: list[dict[str, Any]] = []
        current: dict[str, Any] = {}

        def finish() -> None:
            nonlocal current
            if not current:
                return
            if "worktree" not in current or "head" not in current:
                raise TaskCleanupError("invalid_worktree_registry")
            if current.get("detached") and current.get("branch") is not None:
                raise TaskCleanupError("invalid_worktree_registry")
            if not current.get("detached") and current.get("branch") is None and not current.get("bare"):
                raise TaskCleanupError("invalid_worktree_registry")
            records.append(current)
            current = {}

        for field in raw.split(b"\0"):
            if not field:
                finish()
                continue
            if field.startswith(b"worktree "):
                finish()
                try:
                    path = os.fsdecode(field[len(b"worktree "):])
                    if not Path(path).is_absolute():
                        raise ValueError
                    current = {"worktree": path}
                except (TypeError, ValueError):
                    raise TaskCleanupError("invalid_worktree_registry") from None
                continue
            if not current:
                raise TaskCleanupError("invalid_worktree_registry")
            key, separator, value = field.partition(b" ")
            if key in {b"detached", b"bare", b"locked", b"prunable"}:
                name = key.decode("ascii")
                if name in current:
                    raise TaskCleanupError("invalid_worktree_registry")
                current[name] = True
                continue
            if not separator:
                raise TaskCleanupError("invalid_worktree_registry")
            try:
                name = key.decode("ascii")
                if name not in {"HEAD", "branch"} or name.lower() in current:
                    raise ValueError
                if name == "HEAD":
                    current["head"] = value.decode("ascii", errors="strict")
                else:
                    current["branch"] = value.decode("utf-8", errors="strict")
            except (UnicodeDecodeError, ValueError):
                raise TaskCleanupError("invalid_worktree_registry") from None
        finish()
        if len(records) > MAX_WORKTREES:
            raise TaskCleanupError("worktree_registry_limit")
        if len({item["worktree"] for item in records}) != len(records):
            raise TaskCleanupError("duplicate_worktree_path")
        return tuple(records)

    def _assert_complete_history(self, head: str) -> set[str]:
        shallow = self._text(self._git(["rev-parse", "--is-shallow-repository"], max_stdout=64))
        if shallow != "false":
            raise TaskCleanupError("incomplete_git_history")
        replacements = self._git(["for-each-ref", "--format=%(refname)", "refs/replace"], max_stdout=1024 * 1024)
        if replacements:
            raise TaskCleanupError("replacement_refs_unsupported")
        graft = self._text(self._git(["rev-parse", "--git-path", "info/grafts"], max_stdout=16 * 1024))
        graft_path = Path(graft)
        if not graft_path.is_absolute():
            graft_path = self._store.root / graft_path
        try:
            graft_path.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise TaskCleanupError("incomplete_git_history") from None
        else:
            raise TaskCleanupError("incomplete_git_history")
        raw = self._git(["rev-list", "--parents", f"--max-count={MAX_HISTORY_COMMITS + 1}", head], max_stdout=MAX_GIT_OUTPUT_BYTES)
        if raw is None:
            raise TaskCleanupError("history_unavailable")
        rows = raw.splitlines()
        if len(rows) > MAX_HISTORY_COMMITS:
            raise TaskCleanupError("history_limit")
        found: set[str] = set()
        for row in rows:
            values = row.split()
            if not values:
                raise TaskCleanupError("invalid_history_output")
            if len(values) > 65:
                raise TaskCleanupError("commit_parent_limit")
            for raw_oid in values:
                try:
                    value = raw_oid.decode("ascii", errors="strict")
                except UnicodeDecodeError:
                    raise TaskCleanupError("invalid_history_output") from None
                found.add(_oid(value, self._store._oid_length, "invalid_history_output"))
        return found

    def _retention(
        self,
        authority: _TaskAuthority,
        main: _MainFacts,
        *,
        expected_head: str | None = None,
    ) -> TaskRetentionFacts:
        if self._retention_reader is None:
            raise TaskCleanupError("retention_reader_required")
        exact_head = self._registered_head(authority) if expected_head is None else _oid(
            expected_head, self._store._oid_length, "invalid_task_head",
        )
        request = TaskRetentionRequest(
            self._store.repository, authority.task, authority.branch_ref,
            exact_head, main.default_branch_ref, main.default_head,
        )
        try:
            facts = self._retention_reader(request)
        except Exception:
            raise TaskCleanupError("retention_observation_unavailable") from None
        if type(facts) is not TaskRetentionFacts or (
            type(facts.repository) is not str or type(facts.task) is not str
            or type(facts.branch_ref) is not str or type(facts.expected_head) is not str
            or facts.repository != request.repository or facts.task != request.task
            or facts.branch_ref != request.branch_ref or facts.expected_head != request.expected_head
            or (facts.remote_task_head is not None and type(facts.remote_task_head) is not str)
        ):
            raise TaskCleanupError("retention_binding_mismatch")
        if facts.remote_task_head is not None:
            _oid(facts.remote_task_head, self._store._oid_length, "invalid_remote_task_head")
            if facts.remote_task_head != request.expected_head:
                raise TaskCleanupError("remote_task_head_not_exact")
        pr = facts.pull_request
        if pr is not None:
            if type(pr) is not TaskPullRequestFacts:
                raise TaskCleanupError("invalid_pull_request_facts")
            if (
                type(pr.base_repository) is not str or type(pr.head_repository) is not str
                or type(pr.state) is not str or type(pr.base_ref) is not str
                or type(pr.head_ref) is not str or type(pr.head_oid) is not str
                or (pr.merge_commit_oid is not None and type(pr.merge_commit_oid) is not str)
                or pr.base_repository != request.repository or pr.head_repository != request.repository
                or type(pr.number) is not int or pr.number <= 0
                or pr.state not in {"open", "closed", "merged"}
                or type(pr.merged) is not bool
                or pr.head_ref != request.branch_ref.removeprefix("refs/heads/")
                or pr.base_ref != request.default_branch_ref.removeprefix("refs/heads/")
                or pr.head_oid != request.expected_head
            ):
                raise TaskCleanupError("pull_request_binding_mismatch")
            if pr.merge_commit_oid is not None:
                _oid(pr.merge_commit_oid, self._store._oid_length, "invalid_merge_commit")
        if facts.remote_task_head is None:
            if pr is None or pr.merged is not True or pr.state != "merged" or pr.merge_commit_oid is None:
                raise TaskCleanupError("remote_absence_not_proven_merged")
            merge = pr.merge_commit_oid
            if merge not in self._assert_complete_history(main.default_head):
                raise TaskCleanupError("merge_commit_not_retained")
            if not self._is_ancestor(request.expected_head, merge):
                raise TaskCleanupError("task_head_not_merged")
        return facts

    def _is_ancestor(self, old: str, new: str) -> bool:
        old = _oid(old, self._store._oid_length)
        new = _oid(new, self._store._oid_length)
        # Use a bounded, complete, replacement/graft/shallow-free native graph.
        # A nonzero ``merge-base --is-ancestor`` from MetadataStore._git with
        # check=False is ambiguous (all nonzero statuses map to None), so it is
        # intentionally not treated as proof of non-ancestry.
        return old in self._assert_complete_history(new)

    def _registered_head(self, authority: _TaskAuthority) -> str:
        matches = [item for item in self._worktrees() if item.get("branch") == authority.branch_ref]
        if len(matches) != 1:
            raise TaskCleanupError("task_worktree_registry_mismatch")
        return _oid(matches[0].get("head"), self._store._oid_length, "invalid_task_head")

    def _registered_task(self, authority: _TaskAuthority, main: _MainFacts) -> tuple[Any, Any, Any]:
        try:
            from cleanup_resources import TaskResourceInspector, TaskRootSpec
        except Exception:
            raise TaskCleanupError("filesystem_inspector_unavailable") from None
        records = self._worktrees()
        task_entries = [item for item in records if item.get("branch") == authority.branch_ref]
        default_entries = [item for item in records if item.get("branch") == main.default_branch_ref]
        if len(task_entries) != 1 or len(default_entries) != 1:
            raise TaskCleanupError("worktree_registry_mismatch")
        task_entry = task_entries[0]
        default_entry = default_entries[0]
        if (
            task_entry["worktree"] == str(self._store.root)
            or Path(task_entry["worktree"]).parent != self._store.root / ".worktrees"
            or Path(task_entry["worktree"]).parent.parent != self._store.root
            or task_entry.get("locked") or task_entry.get("prunable") or task_entry.get("detached")
            or default_entry["worktree"] != str(self._store.root)
            or default_entry.get("head") != main.default_head
            or default_entry.get("locked") or default_entry.get("prunable")
        ):
            raise TaskCleanupError("worktree_registry_mismatch")
        head = _oid(task_entry.get("head"), self._store._oid_length, "invalid_task_head")
        if head != self._registered_head(authority):
            raise TaskCleanupError("task_head_changed")
        if self._branch_oid(authority.branch_ref) != head:
            raise TaskCleanupError("task_branch_head_mismatch")
        root = Path(task_entry["worktree"])
        self._check_path_components(root)
        if root.resolve(strict=True) != root:
            raise TaskCleanupError("task_root_not_canonical")
        pointer = root / ".git"
        pointer_info = pointer.lstat()
        if stat.S_ISLNK(pointer_info.st_mode) or not stat.S_ISREG(pointer_info.st_mode) or pointer_info.st_nlink != 1:
            raise TaskCleanupError("unsafe_task_git_pointer")
        pointer_data = self._read_regular_nofollow(pointer, 4096)
        if len(pointer_data) > 4096 or not pointer_data.startswith(b"gitdir: ") or pointer_data.count(b"\n") != 1 or not pointer_data.endswith(b"\n"):
            raise TaskCleanupError("invalid_task_git_pointer")
        admin_raw = os.fsdecode(pointer_data[len(b"gitdir: "):-1])
        admin = Path(admin_raw)
        if not admin.is_absolute():
            admin = root / admin
        if admin.parent.parent != Path(main.common_directory) or admin.parent.name != "worktrees":
            raise TaskCleanupError("task_git_admin_outside_common_directory")
        self._check_path_components(admin)
        if admin.resolve(strict=True) != admin:
            raise TaskCleanupError("task_git_admin_not_canonical")
        spec = TaskRootSpec(
            self._store.repository, authority.task, authority.branch_ref, head,
            str(self._store.root), str(root), str(admin),
        )
        inspector = TaskResourceInspector(spec)
        inventory = inspector.inventory()
        self._validate_inventory(inventory, spec, root, pointer_data)
        return spec, inspector, inventory

    @staticmethod
    def _check_path_components(path: Path) -> None:
        if not path.is_absolute():
            raise TaskCleanupError("unsafe_filesystem_path")
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            try:
                info = current.lstat()
            except OSError:
                raise TaskCleanupError("filesystem_path_unavailable") from None
            if stat.S_ISLNK(info.st_mode) or (current != path and not stat.S_ISDIR(info.st_mode)):
                raise TaskCleanupError("unsafe_filesystem_path")

    @staticmethod
    def _read_regular_nofollow(path: Path, limit: int) -> bytes:
        if limit < 0 or not hasattr(os, "O_NOFOLLOW"):
            raise TaskCleanupError("stable_file_read_unsupported")
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOATIME", 0),
            )
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                    raise TaskCleanupError("unsafe_regular_file")
                data = bytearray()
                while len(data) <= limit:
                    chunk = os.read(descriptor, min(8192, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                after = os.fstat(descriptor)
                named = path.lstat()
                if (
                    len(data) > limit
                    or (before.st_dev, before.st_ino, before.st_mode, before.st_uid,
                        before.st_nlink, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_mode, after.st_uid,
                        after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or (after.st_dev, after.st_ino, after.st_mode, after.st_uid,
                        after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    != (named.st_dev, named.st_ino, named.st_mode, named.st_uid,
                        named.st_nlink, named.st_size, named.st_mtime_ns, named.st_ctime_ns)
                    or not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
                ):
                    raise TaskCleanupError("regular_file_changed")
                return bytes(data)
            finally:
                os.close(descriptor)
        except TaskCleanupError:
            raise
        except OSError:
            raise TaskCleanupError("regular_file_unavailable") from None

    @staticmethod
    def _read_regular_under_root(root: Path, relative_path: str, limit: int) -> tuple[os.stat_result, bytes] | None:
        """Read one relative regular file through a no-follow FD chain."""
        parts = _relative(relative_path).split("/")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOATIME", 0)
        opened: list[int] = []
        try:
            root_fd = os.open(root, directory_flags)
            opened.append(root_fd)
            root_identity = _identity_fd(root_fd)
            if _identity_path(root) != root_identity:
                raise TaskCleanupError("main_root_identity_changed")
            parent_fd = root_fd
            for part in parts[:-1]:
                before = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
                    raise TaskCleanupError("unsafe_main_path_component")
                child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                opened.append(child_fd)
                after = os.fstat(child_fd)
                if (
                    (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_nlink)
                    != (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_nlink)
                    or _identity_fd(child_fd)[2] != root_identity[2]
                    or _identity_fd(child_fd) != (
                        after.st_dev, after.st_ino, root_identity[2],
                    )
                ):
                    raise TaskCleanupError("unsafe_main_path_component")
                parent_fd = child_fd
            try:
                before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if (
                not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or before.st_nlink != 1 or before.st_size < 0 or before.st_size > limit
            ):
                raise TaskCleanupError("unsupported_main_dirty_node")
            file_fd = os.open(parts[-1], file_flags, dir_fd=parent_fd)
            opened.append(file_fd)
            file_before = os.fstat(file_fd)
            if (
                (before.st_dev, before.st_ino, before.st_mode, before.st_uid,
                 before.st_nlink, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (file_before.st_dev, file_before.st_ino, file_before.st_mode, file_before.st_uid,
                    file_before.st_nlink, file_before.st_size, file_before.st_mtime_ns, file_before.st_ctime_ns)
                or _mount_id_fd(file_fd) != root_identity[2]
            ):
                raise TaskCleanupError("main_dirty_file_changed")
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(file_fd, min(64 * 1024, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(file_fd)
            named = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            stable_fields = lambda value: (
                value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
            )
            if (
                len(data) > limit or stable_fields(file_before) != stable_fields(after)
                or stable_fields(after) != stable_fields(named)
                or len(data) != after.st_size
                or _identity_path(root) != root_identity
            ):
                raise TaskCleanupError("main_dirty_file_changed")
            return before, bytes(data)
        except TaskCleanupError:
            raise
        except OSError:
            raise TaskCleanupError("main_dirty_file_unavailable") from None
        finally:
            for descriptor in reversed(opened):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _validate_inventory(self, inventory: Any, spec: Any, root: Path, pointer_data: bytes) -> tuple[CleanupNodeFact, ...]:
        try:
            from cleanup_resources import WorktreeInventory
        except Exception:
            raise TaskCleanupError("filesystem_inspector_unavailable") from None
        if type(inventory) is not WorktreeInventory or inventory.spec != spec:
            raise TaskCleanupError("inventory_binding_mismatch")
        if (
            type(inventory.nodes) is not tuple or len(inventory.nodes) > MAX_INVENTORY_NODES
            or type(inventory.inventory_id) is not str or not _HEX64.fullmatch(inventory.inventory_id)
        ):
            raise TaskCleanupError("invalid_inventory")
        root_identity = _identity(inventory.root_identity)
        repository_identity = _identity(inventory.repository_identity)
        parent_identity = _identity(inventory.parent_identity)
        if (
            root_identity != _identity_path(root)
            or repository_identity != _identity_path(self._store.root)
            or parent_identity != _identity_path(root.parent)
            or root_identity[2] != repository_identity[2]
            or root_identity[2] != parent_identity[2]
            or inventory.git_pointer_sha256 != hashlib.sha256(pointer_data).hexdigest()
        ):
            raise TaskCleanupError("inventory_physical_binding_mismatch")
        facts = tuple(_node_fact(node) for node in inventory.nodes)
        paths = [node.relative_path for node in facts]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise TaskCleanupError("inventory_order_or_duplicate")
        for fact in facts:
            if fact.mount_id != root_identity[2]:
                raise TaskCleanupError("inventory_mount_escape")
            if fact.uid != os.getuid():
                raise TaskCleanupError("foreign_owned_node")
        return facts

    def _tracked_paths_and_clean(self, spec: Any, head: str, inventory: Any) -> tuple[set[str], set[str]]:
        if spec is None:
            spec = getattr(inventory, "spec", None)
        try:
            task_store = MetadataStore(Path(spec.worktree_root), self._store.repository, self._store.remote)
        except Exception:
            raise TaskCleanupError("task_store_unavailable") from None
        git = lambda arguments, **options: self._git(arguments, store=task_store, **options)
        try:
            task_view._check_git_configuration(Path(spec.worktree_root))
            config_keys = git(
                ["config", "--local", "--no-includes", "--name-only", "--null", "--list"],
                max_stdout=1024 * 1024,
            )
            if config_keys is None:
                raise TaskCleanupError("git_configuration_unavailable")
            names = {
                item.decode("ascii", errors="strict").lower()
                for item in config_keys.split(b"\0") if item
            }
        except TaskCleanupError:
            raise
        except Exception:
            raise TaskCleanupError("unsafe_git_configuration") from None
        if names & {
            "core.worktree", "core.sparsecheckout", "core.ignorecase",
            "extensions.worktreeconfig", "index.sparse", "core.attributesfile",
            "core.fsmonitor", "core.untrackedcache",
        }:
            raise TaskCleanupError("unsafe_git_configuration")
        raw_tree = git(["ls-tree", "-r", "-z", "--full-tree", head], max_stdout=MAX_GIT_OUTPUT_BYTES)
        if raw_tree is None:
            raise TaskCleanupError("tracked_tree_unavailable")
        tracked: set[str] = set()
        tracked_digest: dict[str, str] = {}
        tracked_exec: dict[str, bool] = {}
        tree_entries: list[tuple[str, str]] = []
        for entry in raw_tree.split(b"\0"):
            if not entry:
                continue
            try:
                header, raw_path = entry.split(b"\t", 1)
                mode, object_type, raw_oid = header.split(b" ")
                path = raw_path.decode("utf-8", errors="strict")
                oid = raw_oid.decode("ascii", errors="strict")
                mode_text = mode.decode("ascii")
                type_text = object_type.decode("ascii")
            except (ValueError, UnicodeDecodeError):
                raise TaskCleanupError("invalid_tracked_tree") from None
            path = _relative(path)
            _oid(oid, self._store._oid_length, "invalid_tracked_tree")
            if mode_text not in {"100644", "100755"} or type_text != "blob" or path in tracked:
                raise TaskCleanupError("unsupported_tracked_node")
            tracked.add(path)
            tracked_exec[path] = mode_text == "100755"
            tree_entries.append((path, oid))
        if len(tree_entries) > MAX_INVENTORY_NODES:
            raise TaskCleanupError("tracked_file_count_limit")
        if tree_entries:
            query = b"".join(oid.encode("ascii") + b"\n" for _path, oid in tree_entries)
            batch = git(
                ["cat-file", "--batch"], input_data=query,
                max_stdout=MAX_TRACKED_TOTAL_BYTES + len(tree_entries) * 128,
            )
            if batch is None:
                raise TaskCleanupError("tracked_blob_unavailable")
            offset = 0
            total_size = 0
            for path, expected_oid in tree_entries:
                header_end = batch.find(b"\n", offset)
                if header_end < 0:
                    raise TaskCleanupError("invalid_tracked_blob_batch")
                fields = batch[offset:header_end].split(b" ")
                if len(fields) != 3 or fields[0] != expected_oid.encode("ascii") or fields[1] != b"blob":
                    raise TaskCleanupError("invalid_tracked_blob_batch")
                try:
                    size = int(fields[2])
                except ValueError:
                    raise TaskCleanupError("invalid_tracked_blob_batch") from None
                total_size += size
                if size < 0 or size > MAX_TRACKED_FILE_BYTES or total_size > MAX_TRACKED_TOTAL_BYTES:
                    raise TaskCleanupError("tracked_blob_size_limit")
                start = header_end + 1
                end = start + size
                if end >= len(batch) or batch[end:end + 1] != b"\n":
                    raise TaskCleanupError("invalid_tracked_blob_batch")
                tracked_digest[path] = hashlib.sha256(batch[start:end]).hexdigest()
                offset = end + 1
            if offset != len(batch):
                raise TaskCleanupError("invalid_tracked_blob_batch")

        index_flags = git(["ls-files", "-v", "-z"], max_stdout=MAX_GIT_OUTPUT_BYTES)
        if index_flags is None:
            raise TaskCleanupError("index_state_unavailable")
        records = index_flags.split(b"\0") if index_flags else []
        if records and records[-1] == b"":
            records.pop()
        index_paths: set[str] = set()
        for item in records:
            if len(item) < 3 or item[1:2] != b" " or item[:1] != b"H":
                raise TaskCleanupError("hidden_index_entry")
            try:
                index_paths.add(item[2:].decode("utf-8", errors="strict"))
            except UnicodeDecodeError:
                raise TaskCleanupError("non_utf8_repository_path") from None
        if index_paths != tracked:
            raise TaskCleanupError("index_tree_mismatch")

        status = git(["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=none"], max_stdout=MAX_GIT_OUTPUT_BYTES)
        if status is None:
            raise TaskCleanupError("task_status_unavailable")
        for row in status.split(b"\0"):
            if not row:
                continue
            if row.startswith(b"? "):
                continue
            # All tracked/index modifications, conflicts, rename/copy entries,
            # and submodule/gitlink changes are unpublished human decisions.
            raise TaskCleanupError("task_worktree_dirty")

        ordinary = git(["ls-files", "--others", "--exclude-standard", "-z"], max_stdout=MAX_GIT_OUTPUT_BYTES)
        ignored = git(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], max_stdout=MAX_GIT_OUTPUT_BYTES)
        if ordinary is None or ignored is None:
            raise TaskCleanupError("unknown_content_unavailable")
        unknown_files: set[str] = set()
        for output in (ordinary, ignored):
            for value in output.split(b"\0"):
                if not value:
                    continue
                try:
                    unknown_files.add(_relative(value.decode("utf-8", errors="strict")))
                except UnicodeDecodeError:
                    raise TaskCleanupError("non_utf8_repository_path") from None

        node_by_path = {node.relative_path: node for node in (_node_fact(item) for item in inventory.nodes)}
        if not tracked <= node_by_path.keys() or not unknown_files <= node_by_path.keys():
            raise TaskCleanupError("inventory_content_mismatch")
        for path in tracked:
            node = node_by_path[path]
            if node.kind != "file" or node.content_sha256 != tracked_digest[path]:
                raise TaskCleanupError("tracked_file_changed")
            if bool(node.mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)) != tracked_exec[path]:
                raise TaskCleanupError("tracked_mode_changed")
        inventory_files = {path for path, node in node_by_path.items() if node.kind == "file"}
        if inventory_files != tracked | unknown_files:
            raise TaskCleanupError("unclassified_filesystem_content")
        tracked_ancestor_directories: set[str] = set()
        for path in tracked:
            pieces = path.split("/")
            tracked_ancestor_directories.update("/".join(pieces[:index]) for index in range(1, len(pieces)))
        unknown = set(unknown_files)
        # Directories shared with product files are not themselves disposable.
        # Every other directory, including an empty directory invisible to
        # Git's untracked-path listing, is unknown and must be covered by an
        # authenticated producer proof.
        for path, node in node_by_path.items():
            if node.kind == "directory" and path not in tracked_ancestor_directories:
                unknown.add(path)
        return tracked, unknown

    def _classify(self, authority: _TaskAuthority, spec: Any, inventory: Any, head: str, unknown: set[str]) -> tuple[DisposableResourceProof, ...]:
        if not unknown:
            return ()
        unknown_nodes = tuple(
            node for node in inventory.nodes
            if getattr(node, "relative_path", None) in unknown
        )
        request = ResourceClassificationRequest(
            self._store.repository, authority.task, authority.branch_ref, head,
            spec, inventory, unknown_nodes,
        )
        try:
            callback = getattr(self._classifier, "classify", None)
            result = callback(request) if callable(callback) else self._classifier(request)
        except Exception:
            raise TaskCleanupError("resource_classification_unavailable") from None
        if type(result) is not tuple or any(type(item) is not DisposableResourceProof for item in result):
            raise TaskCleanupError("resource_classification_not_typed")
        node_map = {node.relative_path: node for node in inventory.nodes}
        covered: set[str] = set()
        scopes: list[str] = []
        proofs = tuple(sorted(result, key=lambda proof: proof.relative_scope))
        for proof in proofs:
            scope = _relative(proof.relative_scope)
            if (
                proof.repository != self._store.repository or proof.task != authority.task
                or proof.branch_ref != authority.branch_ref or proof.head != head
                or proof.inventory_id != inventory.inventory_id
                or type(proof.producer) is not str or not proof.producer
                or len(proof.producer) > 128
                or type(proof.resource_kind) is not str or proof.resource_kind not in _DISPOSABLE_KINDS
                or type(proof.proof_id) is not str or not _HEX64.fullmatch(proof.proof_id)
                or type(proof.node_manifest) is not tuple
            ):
                raise TaskCleanupError("resource_proof_binding_mismatch")
            expected = tuple(
                node_map[path] for path in sorted(node_map)
                if path == scope or path.startswith(scope + "/")
            )
            if not expected or proof.node_manifest != expected:
                raise TaskCleanupError("resource_proof_manifest_mismatch")
            paths_in_scope = {getattr(node, "relative_path", None) for node in expected}
            if not paths_in_scope <= unknown:
                raise TaskCleanupError("resource_proof_covers_product_data")
            if any(scope == prior or scope.startswith(prior + "/") or prior.startswith(scope + "/") for prior in scopes):
                raise TaskCleanupError("overlapping_resource_proofs")
            scopes.append(scope)
            covered.update(paths_in_scope)
        if covered != unknown:
            raise TaskCleanupError("unknown_content_requires_human_decision")
        return proofs

    def _read_view(self, authority: _TaskAuthority, main: _MainFacts, root_store: MetadataStore | None) -> dict[str, Any]:
        store = root_store if root_store is not None else self._store
        try:
            return task_view.observe_live_task(
                store, task=authority.task, branch_ref=authority.branch_ref,
                base_revision=authority.base_revision, default_branch_ref=main.default_branch_ref,
                observe_remote_head=True,
            )
        except Exception:
            raise TaskCleanupError("task_factual_view_unavailable") from None

    def _diagnose(self, operation: str, authority: _TaskAuthority, view: dict[str, Any], parameters: dict[str, Any]) -> None:
        engine = self._prerequisites.get(operation)
        if engine is None:
            raise TaskCleanupError("prerequisite_engine_missing", "plan")
        request = operation_prerequisites.OperationRequest(
            self._store.repository, authority.task, authority.branch_ref,
            view["subject"], operation, parameters,
        )
        try:
            diagnosis = engine.diagnose(request, view)
            operation_prerequisites.encode_diagnosis(diagnosis)
        except Exception:
            raise TaskCleanupError("operation_prerequisite_unavailable") from None
        if (
            diagnosis.get("repository") != self._store.repository
            or diagnosis.get("task") != authority.task
            or diagnosis.get("branch_ref") != authority.branch_ref
            or diagnosis.get("operation") != operation
            or diagnosis.get("subject") != view["subject"]
            or diagnosis.get("result") != "PREREQUISITES_SATISFIED"
            or diagnosis.get("human_decision_required") is True
        ):
            raise TaskCleanupError("operation_prerequisites_not_satisfied")

    @staticmethod
    def _retention_digest(facts: TaskRetentionFacts) -> str:
        pr = facts.pull_request
        payload = {
            "repository": facts.repository,
            "task": facts.task,
            "branch_ref": facts.branch_ref,
            "expected_head": facts.expected_head,
            "remote_task_head": facts.remote_task_head,
            "pull_request": None if pr is None else {
                "base_repository": pr.base_repository,
                "head_repository": pr.head_repository,
                "number": pr.number,
                "state": pr.state,
                "merged": pr.merged,
                "base_ref": pr.base_ref,
                "head_ref": pr.head_ref,
                "head_oid": pr.head_oid,
                "merge_commit_oid": pr.merge_commit_oid,
            },
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def _make_intent(
        self,
        authority: _TaskAuthority,
        main: _MainFacts,
        spec: Any,
        inventory: Any,
        node_facts: tuple[CleanupNodeFact, ...],
        proofs: tuple[DisposableResourceProof, ...],
        retention: TaskRetentionFacts,
        *,
        operation: str = "cleanup.worktree_and_branch",
        binding: OwnedResourceBinding | None = None,
        resource_reference: OpaqueResourceReference | None = None,
    ) -> CleanupIntent:
        proof_facts = tuple(
            CleanupProofFact(
                proof.relative_scope, proof.resource_kind, proof.producer,
                proof.proof_id,
                hashlib.sha256(_canonical_json([_node_fact(node).__dict__ for node in proof.node_manifest])).hexdigest(),
            )
            for proof in proofs
        )
        pr = retention.pull_request
        root_id = _identity(inventory.root_identity)
        parent_id = _identity(inventory.parent_identity)
        admin_path = Path(spec.git_admin)
        admin_parent_id = _identity_path(admin_path.parent)
        admin_id = _identity_path(admin_path)
        manifest_digest = hashlib.sha256(_canonical_json([node.__dict__ for node in node_facts])).hexdigest()
        intent = CleanupIntent(
            SCHEMA_VERSION, operation, self._store.repository, authority.task,
            authority.branch_ref, authority.base_revision, authority.record_id,
            authority.contract_id, authority.disposition, spec.head,
            authority.metadata_tip, self._main_state_fingerprint(
                main, removed_ref=authority.branch_ref, removed_worktree=spec.worktree_root,
            ),
            main.default_branch_ref, main.default_head,
            str(spec.worktree_root), str(spec.git_admin), root_id,
            inventory.root_identity.nlink, parent_id,
            admin_parent_id, admin_id,
            inventory.git_pointer_sha256, inventory.inventory_id, manifest_digest,
            node_facts, proof_facts, retention.remote_task_head,
            self._retention_digest(retention),
            None if pr is None else pr.head_oid,
            None if pr is None else pr.merge_commit_oid,
            None if binding is None else binding.relative_scope,
            None if binding is None else binding.binding_id,
            resource_reference,
        )
        _validate_intent(intent, self._store._oid_length)
        return intent

    def plan(self, task: str) -> CleanupPlan:
        """Return a read-only plan for one canonical Task ID."""
        task = _task_context(task)
        authority = self._read_authority(task)
        main = self._main_facts()
        spec, inspector, inventory = self._registered_task(authority, main)
        head = _oid(spec.head, self._store._oid_length, "invalid_task_head")
        history = self._assert_complete_history(main.default_head)
        if head not in history:
            raise TaskCleanupError("task_head_not_retained")
        if not self._is_ancestor(authority.base_revision, head):
            raise TaskCleanupError("task_base_not_ancestor")
        view_store = MetadataStore(Path(spec.worktree_root), self._store.repository, self._store.remote)
        task_view_value = self._read_view(authority, main, view_store)
        if (
            task_view_value["subject"] != head
            or task_view_value["git"]["head"] != head
            or task_view_value["git"]["branch_ref"] != authority.branch_ref
            or task_view_value["git"]["branch_matches_task"] is not True
        ):
            raise TaskCleanupError("task_live_identity_mismatch")
        tracked, unknown = self._tracked_paths_and_clean(spec, head, inventory)
        del tracked
        node_facts = self._validate_inventory(
            inventory, spec, Path(spec.worktree_root),
            self._read_regular_nofollow(Path(spec.worktree_root) / ".git", 4096),
        )
        proofs = self._classify(authority, spec, inventory, head, unknown)
        retention = self._retention(authority, main)
        expected_remote = (
            {"state": "observed", "head_oid": retention.remote_task_head}
            if retention.remote_task_head is not None
            else {"state": "unavailable", "error_code": "ref_absent"}
        )
        if task_view_value["git"]["remote_head"] != expected_remote:
            raise TaskCleanupError("remote_task_head_observation_mismatch")
        # No classifier result or Task disposition is permission. The policy
        # diagnosis and explicit host authorization happen again at mutation.
        self._diagnose("cleanup.worktree", authority, task_view_value, {
            "target_head": head, "record_id": authority.record_id,
            "contract_id": authority.contract_id, "inventory_id": inventory.inventory_id,
        })
        intent = self._make_intent(authority, main, spec, inventory, node_facts, proofs, retention)
        return CleanupPlan(intent, spec, inventory, proofs, _canonical_json(task_view_value))

    capture = plan

    def _qualification(self, operation: str, main: _MainFacts) -> tuple[QualifiedDeletionCapability, str]:
        capability = self._deletion.get(operation)
        if capability is None or type(capability) is not QualifiedDeletionCapability:
            raise TaskCleanupError("qualified_deletion_backend_required", operation)
        proof = capability.qualification
        required_flags = (
            proof.descriptor_pinned is True,
            proof.atomic_identity is True,
            proof.atomic_custody is True,
            proof.current_authorization is True,
            proof.expected_oid_compare_exchange is True,
            proof.preserves_product_data is True,
            proof.no_force is True,
            proof.no_remote_refs is True,
        )
        if (
            type(proof) is not CleanupQualification or proof.operation != operation
            or type(proof.qualification_id) is not str or not _HEX64.fullmatch(proof.qualification_id)
            or proof.repository != self._store.repository
            or type(proof.common_directory) is not str
            or proof.common_directory != main.common_directory
            or type(proof.common_identity) is not tuple or len(proof.common_identity) != 3
            or proof.common_identity != main.common_identity
            or type(proof.default_branch_ref) is not str
            or proof.default_branch_ref != main.default_branch_ref
            or type(proof.worktree_namespace) is not str
            or proof.worktree_namespace != str(self._store.root / ".worktrees")
            or not all(required_flags)
        ):
            raise TaskCleanupError("backend_qualification_mismatch", operation)
        return capability, proof.qualification_id

    def _authorize(self, intent: CleanupIntent, step: str) -> CleanupAuthorizationRequest:
        request = CleanupAuthorizationRequest(
            intent.intent_id, intent.canonical_bytes(), f"cleanup.{step}", step,
            intent.repository, intent.task, intent.branch_ref, intent.target_head,
            intent.record_id, intent.contract_id, intent.default_branch_ref,
            intent.default_head, intent.root_identity, intent.inventory_id,
            intent.manifest_digest, tuple(proof.proof_id for proof in intent.resource_proofs),
            intent.ephemeral_scope,
        )
        try:
            allowed = self._authorizer(request) is True
        except Exception:
            raise TaskCleanupError("cleanup_authorization_failed", step) from None
        if not allowed:
            raise TaskCleanupError("cleanup_authorization_denied", step)
        return request

    @staticmethod
    def _permit(operation: str, qualification_id: str, intent: CleanupIntent, request: CleanupAuthorizationRequest, target_digest: str) -> DeletionPermit:
        request_value = dict(request.__dict__)
        request_value["intent_bytes"] = hashlib.sha256(request.intent_bytes).hexdigest()
        return DeletionPermit(
            operation, qualification_id, intent.intent_id,
            hashlib.sha256(request.intent_bytes + _canonical_json(request_value)).hexdigest(),
            target_digest, _PERMIT_SEAL,
        )

    def _record(self, intent: CleanupIntent) -> RecordedCleanupIntent:
        try:
            result = self._record_intent(intent)
        except Exception:
            raise TaskCleanupError("cleanup_intent_record_failed") from None
        if (
            type(result) is not RecordedCleanupIntent
            or type(result.reference) is not str or not result.reference or len(result.reference) > 1024
            or result.intent_id != intent.intent_id or result.intent != intent
        ):
            raise TaskCleanupError("cleanup_intent_record_unconfirmed")
        return result

    def _load_recorded(self, reference: str) -> RecordedCleanupIntent:
        if type(reference) is not str or not reference or len(reference) > 1024:
            raise TaskCleanupError("invalid_cleanup_intent_reference", "recover")
        try:
            result = self._load_intent(reference)
        except Exception:
            raise TaskCleanupError("cleanup_intent_unavailable", "recover") from None
        if (
            type(result) is not RecordedCleanupIntent or result.reference != reference
            or type(result.intent_id) is not str
            or type(result.intent) is not CleanupIntent
        ):
            raise TaskCleanupError("cleanup_intent_not_known", "recover")
        try:
            _validate_intent(result.intent, self._store._oid_length)
            if result.intent_id != result.intent.intent_id:
                raise TaskCleanupError("invalid_cleanup_intent", "recover")
        except Exception:
            raise TaskCleanupError("invalid_cleanup_intent", "recover") from None
        return result

    def _verify_intent_authority(
        self,
        intent: CleanupIntent,
        *,
        allow_branch_absent: bool = False,
    ) -> tuple[_TaskAuthority, _MainFacts]:
        authority = self._read_authority(intent.task)
        main = self._main_facts()
        if (
            authority.record_id != intent.record_id or authority.contract_id != intent.contract_id
            or authority.base_revision != intent.base_revision or authority.branch_ref != intent.branch_ref
            or authority.disposition != intent.disposition
            or main.default_branch_ref != intent.default_branch_ref
            or main.default_head != intent.default_head
        ):
            raise TaskCleanupError("cleanup_intent_authority_changed", "recover")
        try:
            metadata_history_preserved = self._store._validate_reachable(
                intent.metadata_tip, authority.metadata_tip,
            )
        except Exception:
            raise TaskCleanupError("metadata_history_revalidation_failed", "recover") from None
        if metadata_history_preserved is not True:
            raise TaskCleanupError("metadata_history_rewound", "recover")
        if allow_branch_absent:
            if self._branch_oid(intent.branch_ref) is not None:
                raise TaskCleanupError("cleanup_intent_branch_not_absent", "recover")
        else:
            if self._branch_oid(intent.branch_ref) != intent.target_head:
                raise TaskCleanupError("task_branch_changed", "recover")
        # The exact target ref has its own expected-OID/absence guard. Its
        # authorized disappearance must not change the preservation fingerprint
        # of every *other* ref and main-worktree state.
        fingerprint = self._main_state_fingerprint(
            main, removed_ref=intent.branch_ref, removed_worktree=intent.worktree_root,
        )
        if fingerprint != intent.main_state_fingerprint:
            raise TaskCleanupError("cleanup_main_state_changed", "recover")
        history = self._assert_complete_history(main.default_head)
        if intent.target_head not in history:
            raise TaskCleanupError("task_head_not_retained", "recover")
        if not self._is_ancestor(intent.base_revision, intent.target_head):
            raise TaskCleanupError("task_base_not_ancestor", "recover")
        return authority, main

    def _fresh_retention(self, authority: _TaskAuthority, main: _MainFacts, intent: CleanupIntent) -> TaskRetentionFacts:
        current = self._retention(authority, main, expected_head=intent.target_head)
        if self._retention_digest(current) != intent.retention_fingerprint:
            raise TaskCleanupError("retention_facts_changed", "recover")
        return current

    def _verify_inventory_same(self, inspector: Any, expected: Any, spec: Any, pointer_data: bytes) -> tuple[Any, tuple[CleanupNodeFact, ...]]:
        try:
            fresh = inspector.revalidate(expected)
        except Exception:
            raise TaskCleanupError("filesystem_revalidation_failed") from None
        facts = self._validate_inventory(fresh, spec, Path(spec.worktree_root), pointer_data)
        if fresh != expected or facts != tuple(_node_fact(node) for node in expected.nodes):
            raise TaskCleanupError("filesystem_inventory_changed")
        return fresh, facts

    def _current_registered_spec(self, intent: CleanupIntent, main: _MainFacts) -> tuple[Any, Any, Any] | None:
        records = self._worktrees()
        task_matches = [item for item in records if item.get("branch") == intent.branch_ref]
        path_matches = [item for item in records if item.get("worktree") == intent.worktree_root]
        root = Path(intent.worktree_root)
        try:
            info = root.lstat()
        except FileNotFoundError:
            if task_matches or path_matches:
                raise TaskCleanupError("worktree_registry_root_mismatch", "recover")
            return None
        except OSError:
            raise TaskCleanupError("task_root_unavailable", "recover") from None
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise TaskCleanupError("task_root_identity_changed", "recover")
        if len(task_matches) != 1 or len(path_matches) != 1:
            raise TaskCleanupError("task_root_replaced_or_unregistered", "recover")
        if any(task_matches[0].get(flag) for flag in ("locked", "prunable", "detached", "bare")):
            raise TaskCleanupError("task_registry_state_changed", "recover")
        if task_matches[0] != path_matches[0] or task_matches[0].get("head") != intent.target_head:
            raise TaskCleanupError("task_root_replaced_or_unregistered", "recover")
        try:
            from cleanup_resources import TaskResourceInspector, TaskRootSpec
        except Exception:
            raise TaskCleanupError("filesystem_inspector_unavailable", "recover") from None
        self._check_path_components(root)
        pointer_path = root / ".git"
        pointer_data = self._read_regular_nofollow(pointer_path, 4096)
        admin = Path(intent.git_admin)
        spec = TaskRootSpec(
            intent.repository, intent.task, intent.branch_ref, intent.target_head,
            str(self._store.root), str(root), str(admin),
        )
        inspector = TaskResourceInspector(spec)
        inventory = inspector.inventory()
        self._validate_inventory(inventory, spec, root, pointer_data)
        if (
            _identity(inventory.root_identity) != intent.root_identity
            or _identity(inventory.parent_identity) != intent.parent_identity
            or _identity_path(admin.parent) != intent.admin_parent_identity
            or _identity_path(admin) != intent.admin_identity
            or inventory.git_pointer_sha256 != intent.git_pointer_sha256
            or (
                intent.operation == "cleanup.worktree_and_branch"
                and inventory.inventory_id != intent.inventory_id
            )
        ):
            raise TaskCleanupError("task_root_identity_changed", "recover")
        return spec, inspector, inventory

    def _verify_intent_inventory(self, intent: CleanupIntent, inventory: Any, spec: Any) -> tuple[CleanupNodeFact, ...]:
        pointer = Path(spec.worktree_root) / ".git"
        try:
            facts = self._validate_inventory(
                inventory, spec, Path(spec.worktree_root), self._read_regular_nofollow(pointer, 4096),
            )
        except Exception:
            raise TaskCleanupError("inventory_unavailable", "recover") from None
        digest = hashlib.sha256(_canonical_json([node.__dict__ for node in facts])).hexdigest()
        if (
            inventory.inventory_id != intent.inventory_id
            or inventory.git_pointer_sha256 != intent.git_pointer_sha256
            or _identity(inventory.root_identity) != intent.root_identity
            or _identity_path(Path(spec.git_admin)) != intent.admin_identity
            or facts != intent.nodes or digest != intent.manifest_digest
        ):
            raise TaskCleanupError("filesystem_inventory_changed", "recover")
        return facts

    def _verify_proofs(self, intent: CleanupIntent, proofs: tuple[DisposableResourceProof, ...], inventory: Any) -> None:
        current = tuple(
            CleanupProofFact(
                proof.relative_scope, proof.resource_kind, proof.producer,
                proof.proof_id,
                hashlib.sha256(_canonical_json([_node_fact(node).__dict__ for node in proof.node_manifest])).hexdigest(),
            ) for proof in proofs
        )
        if current != intent.resource_proofs:
            raise TaskCleanupError("resource_proof_changed", "recover")
        # Re-run classifier against exact inventory; a journaled proof does not
        # become authority after its producer ownership has changed.
        authority = _TaskAuthority(
            intent.task, intent.record_id, intent.contract_id, intent.base_revision,
            intent.branch_ref, intent.disposition, intent.metadata_tip,
        )
        _tracked, unknown = self._tracked_paths_and_clean(None, intent.target_head, inventory)
        verified = self._classify(authority, inventory.spec, inventory, intent.target_head, unknown)
        if verified != proofs:
            raise TaskCleanupError("resource_proof_revalidation_failed", "recover")

    def _remove_worktree_step(self, intent: CleanupIntent, *, recovery: bool = False) -> None:
        operation = "recover" if recovery else "worktree"
        authority, main = self._verify_intent_authority(intent)
        self._fresh_retention(authority, main, intent)
        initial_main = main
        current = self._current_registered_spec(intent, main)
        if current is None:
            if not self._worktree_admin_absent(intent):
                raise TaskCleanupError("worktree_admin_remains", operation)
            return
        spec, inspector, inventory = current
        self._verify_intent_inventory(intent, inventory, spec)
        if intent.operation == "cleanup.worktree_and_branch":
            _tracked, unknown = self._tracked_paths_and_clean(spec, intent.target_head, inventory)
            proofs = self._classify(authority, spec, inventory, intent.target_head, unknown)
            self._verify_proofs(intent, proofs, inventory)
        if intent.operation == "cleanup.ephemeral":
            raise TaskCleanupError("ephemeral_intent_cannot_remove_worktree", operation)
        view_store = MetadataStore(Path(spec.worktree_root), self._store.repository, self._store.remote)
        view = self._read_view(authority, main, view_store)
        self._diagnose("cleanup.worktree", authority, view, {
            "target_head": intent.target_head, "record_id": intent.record_id,
            "contract_id": intent.contract_id, "inventory_id": intent.inventory_id,
        })
        capability, qualification_id = self._qualification("worktree", main)
        if self._assert_main_stable(initial_main) != initial_main:
            raise TaskCleanupError("main_repository_state_changed", operation)
        try:
            pointer_data = self._read_regular_nofollow(Path(spec.worktree_root) / ".git", 4096)
        except OSError:
            raise TaskCleanupError("task_git_pointer_unavailable", operation) from None
        self._verify_inventory_same(inspector, inventory, spec, pointer_data)
        try:
            if inspector.root_absent():
                raise TaskCleanupError("task_root_disappeared_before_removal", operation)
        except TaskCleanupError:
            raise
        except Exception:
            raise TaskCleanupError("task_root_identity_revalidation_failed", operation) from None
        auth = self._authorize(intent, "worktree")
        authority_after, main_after = self._verify_intent_authority(intent)
        if main_after != initial_main:
            raise TaskCleanupError("main_repository_state_changed", operation)
        self._fresh_retention(authority_after, main_after, intent)
        refreshed = self._current_registered_spec(intent, main_after)
        if refreshed is None:
            raise TaskCleanupError("task_worktree_disappeared", operation)
        spec_after, inspector_after, inventory_after = refreshed
        if inventory_after != inventory:
            raise TaskCleanupError("filesystem_inventory_changed", operation)
        self._verify_intent_inventory(intent, inventory_after, spec_after)
        _tracked_after, unknown_after = self._tracked_paths_and_clean(spec_after, intent.target_head, inventory_after)
        proofs_after = self._classify(authority_after, spec_after, inventory_after, intent.target_head, unknown_after)
        self._verify_proofs(intent, proofs_after, inventory_after)
        view_after = self._read_view(
            authority_after, main_after,
            MetadataStore(Path(spec_after.worktree_root), self._store.repository, self._store.remote),
        )
        self._diagnose("cleanup.worktree", authority_after, view_after, {
            "target_head": intent.target_head, "record_id": intent.record_id,
            "contract_id": intent.contract_id, "inventory_id": intent.inventory_id,
        })
        authority_final, main_final = self._verify_intent_authority(intent)
        if main_final != initial_main or self._branch_oid(intent.branch_ref) != intent.target_head:
            raise TaskCleanupError("cleanup_state_changed_after_authorization", operation)
        self._fresh_retention(authority_final, main_final, intent)
        final_registered = self._current_registered_spec(intent, main_final)
        if final_registered is None or final_registered[2] != inventory_after:
            raise TaskCleanupError("filesystem_inventory_changed", operation)
        self._assert_main_stable(initial_main)
        permit = self._permit("worktree", qualification_id, intent, auth, intent.manifest_digest)
        request = WorktreeRemovalRequest(spec_after, inventory_after, intent, proofs_after, auth, permit)
        callback_error = False
        try:
            capability._invoke(request)
        except Exception:
            callback_error = True
        # Never trust the callback result. Confirm both the native registry and
        # physical root/admin postconditions before the branch step can run.
        self._assert_main_stable(initial_main, removed_worktree=intent.worktree_root)
        if not self._root_and_admin_absent(intent):
            if callback_error:
                raise TaskCleanupError("worktree_removal_unconfirmed", operation)
            raise TaskCleanupError("worktree_removal_postcondition_failed", operation)
        self._verify_intent_authority(intent)
        self._fresh_retention(authority, self._main_facts(), intent)

    def _worktree_admin_absent(self, intent: CleanupIntent) -> bool:
        admin = Path(intent.git_admin)
        common = self._store.root / ".git"
        if admin.parent != common / "worktrees":
            raise TaskCleanupError("invalid_task_admin_scope", "recover")
        try:
            admin.parent.lstat()
        except FileNotFoundError:
            # Native removal of the last linked worktree also removes the
            # empty private-registry container. Confirm that exact container
            # name is absent beneath the preserved common Git directory; do
            # not interpret a missing/redirected common directory as success.
            main = self._main_facts()
            if main.common_directory != str(common):
                raise TaskCleanupError("common_directory_changed", "recover")
            return self._pinned_name_absent(admin.parent, main.common_identity)
        except OSError:
            raise TaskCleanupError("cleanup_absence_observation_failed", "recover") from None
        return self._pinned_name_absent(admin, intent.admin_parent_identity)

    def _pinned_name_absent(self, path: Path, expected_parent: tuple[int, int, int]) -> bool:
        """Confirm exact name absence under the original no-follow parent inode."""
        parent = path.parent
        self._check_path_components(parent)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(parent, flags)
            try:
                opened_identity = _identity_fd(descriptor)
                if opened_identity != expected_parent or _identity_path(parent) != expected_parent:
                    raise TaskCleanupError("cleanup_parent_identity_changed", "recover")
                for _attempt in range(2):
                    try:
                        os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        raise TaskCleanupError("cleanup_absence_observation_failed", "recover") from None
                    else:
                        return False
                    if (
                        _identity_fd(descriptor) != expected_parent
                        or _identity_path(parent) != expected_parent
                    ):
                        raise TaskCleanupError("cleanup_parent_identity_changed", "recover")
                return True
            finally:
                os.close(descriptor)
        except TaskCleanupError:
            raise
        except OSError:
            raise TaskCleanupError("cleanup_absence_observation_failed", "recover") from None

    def _root_and_admin_absent(self, intent: CleanupIntent) -> bool:
        root = Path(intent.worktree_root)
        admin = Path(intent.git_admin)
        if not self._pinned_name_absent(root, intent.parent_identity):
            return False
        if not self._worktree_admin_absent(intent):
            return False
        if any(item.get("worktree") == intent.worktree_root or item.get("branch") == intent.branch_ref for item in self._worktrees()):
            return False
        return True

    def _branch_oid(self, branch_ref: str) -> str | None:
        task_record.validate_branch_ref(branch_ref)
        output = self._git(
            [
                "for-each-ref",
                "--format=%(refname)%00%(objectname)%00%(objecttype)%00%(symref)",
                branch_ref,
            ],
            max_stdout=1024 * 1024,
        )
        if output is None:
            # Absence is returned only after a successful complete ref query;
            # a command error is never collapsed into "branch absent".
            raise TaskCleanupError("task_ref_observation_unavailable", "branch")
        found: str | None = None
        for line in output.splitlines():
            fields = line.split(b"\0")
            if len(fields) != 4:
                raise TaskCleanupError("invalid_task_ref_output", "branch")
            try:
                ref = fields[0].decode("utf-8", errors="strict")
                oid_text = fields[1].decode("ascii", errors="strict")
                object_type = fields[2].decode("ascii", errors="strict")
                symref = fields[3].decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise TaskCleanupError("invalid_task_ref_output", "branch") from None
            if ref != branch_ref and not ref.startswith(branch_ref + "/"):
                raise TaskCleanupError("unexpected_task_ref_output", "branch")
            if ref != branch_ref:
                _oid(oid_text, self._store._oid_length, "invalid_task_ref_output")
                continue
            if found is not None:
                raise TaskCleanupError("duplicate_task_ref", "branch")
            if symref:
                raise TaskCleanupError("symbolic_task_ref", "branch")
            if object_type != "commit":
                raise TaskCleanupError("task_ref_not_commit", "branch")
            found = _oid(oid_text, self._store._oid_length)
        return found

    def _remove_branch_step(self, intent: CleanupIntent, *, recovery: bool = False) -> None:
        operation = "recover" if recovery else "branch"
        observed_branch = self._branch_oid(intent.branch_ref)
        authority, main = self._verify_intent_authority(
            intent, allow_branch_absent=observed_branch is None,
        )
        if not self._root_and_admin_absent(intent):
            raise TaskCleanupError("worktree_removal_not_confirmed", operation)
        if observed_branch is None:
            # An absent branch is already applied only under this exact durable
            # intent and only while its target commit remains retained.
            if intent.target_head not in self._assert_complete_history(main.default_head):
                raise TaskCleanupError("task_head_not_retained", operation)
            retention = self._fresh_retention(authority, main, intent)
            if self._retention_digest(retention) != intent.retention_fingerprint:
                raise TaskCleanupError("retention_facts_changed", operation)
            self._assert_main_stable(main, removed_ref=intent.branch_ref)
            authority_after, main_after = self._verify_intent_authority(
                intent, allow_branch_absent=True,
            )
            retention_after = self._fresh_retention(authority_after, main_after, intent)
            if self._retention_digest(retention_after) != intent.retention_fingerprint:
                raise TaskCleanupError("retention_facts_changed", operation)
            return
        if self._branch_oid(intent.branch_ref) != intent.target_head:
            raise TaskCleanupError("task_branch_changed", operation)
        if any(item.get("branch") == intent.branch_ref for item in self._worktrees()):
            raise TaskCleanupError("task_branch_still_checked_out", operation)
        retention = self._fresh_retention(authority, main, intent)
        if self._retention_digest(retention) != intent.retention_fingerprint:
            raise TaskCleanupError("retention_facts_changed", operation)
        view = self._read_view(authority, main, None)
        self._diagnose("cleanup.branch", authority, view, {
            "target_head": intent.target_head, "record_id": intent.record_id,
            "contract_id": intent.contract_id, "inventory_id": intent.inventory_id,
        })
        capability, qualification_id = self._qualification("branch", main)
        self._assert_main_stable(main)
        if self._branch_oid(intent.branch_ref) != intent.target_head:
            raise TaskCleanupError("task_branch_changed", operation)
        auth = self._authorize(intent, "branch")
        authority_after, main_after = self._verify_intent_authority(intent)
        if (
            main_after != main or self._branch_oid(intent.branch_ref) != intent.target_head
            or not self._root_and_admin_absent(intent)
            or any(item.get("branch") == intent.branch_ref for item in self._worktrees())
        ):
            raise TaskCleanupError("cleanup_state_changed_after_authorization", operation)
        self._fresh_retention(authority_after, main_after, intent)
        view_after = self._read_view(authority_after, main_after, None)
        self._diagnose("cleanup.branch", authority_after, view_after, {
            "target_head": intent.target_head, "record_id": intent.record_id,
            "contract_id": intent.contract_id, "inventory_id": intent.inventory_id,
        })
        authority_final, main_final = self._verify_intent_authority(intent)
        if (
            main_final != main or self._branch_oid(intent.branch_ref) != intent.target_head
            or not self._root_and_admin_absent(intent)
        ):
            raise TaskCleanupError("cleanup_state_changed_after_authorization", operation)
        self._fresh_retention(authority_final, main_final, intent)
        self._assert_main_stable(main)
        target_digest = hashlib.sha256(_canonical_json([intent.branch_ref, intent.target_head])).hexdigest()
        permit = self._permit("branch", qualification_id, intent, auth, target_digest)
        request = BranchRemovalRequest(
            intent.repository, intent.task, intent.branch_ref, intent.target_head,
            ((intent.default_branch_ref, intent.default_head),), intent, auth, permit,
        )
        callback_error = False
        try:
            capability._invoke(request)
        except Exception:
            callback_error = True
        after_main = self._assert_main_stable(main, removed_ref=intent.branch_ref)
        if self._branch_oid(intent.branch_ref) is not None:
            raise TaskCleanupError("branch_removal_unconfirmed" if callback_error else "branch_removal_postcondition_failed", operation)
        if intent.target_head not in self._assert_complete_history(after_main.default_head):
            raise TaskCleanupError("task_head_not_retained", operation)
        self._verify_intent_authority(intent, allow_branch_absent=True)
        self._fresh_retention(authority, self._main_facts(), intent)

    def cleanup(self, task: str) -> CleanupAcknowledgement:
        """Remove only the inspected Task root, then its exact local branch."""
        plan = self.plan(task)
        recorded = self._record(plan.intent)
        # Record callback side effects are not assumed to be harmless; re-read
        # every authoritative and physical fact before invoking either backend.
        try:
            self._remove_worktree_step(plan.intent)
            self._remove_branch_step(plan.intent)
            return self._acknowledgement(plan.intent, intent_reference=recorded.reference)
        except TaskCleanupError as error:
            error.intent_reference = recorded.reference
            raise
        except Exception:
            error = TaskCleanupError("cleanup_interrupted", "recover")
            error.intent_reference = recorded.reference
            raise error from None

    def recover(self, intent_ref: str) -> CleanupAcknowledgement:
        """Resume only remaining steps of an exact host-journaled intent."""
        recorded = self._load_recorded(intent_ref)
        try:
            return self._recover_recorded_intent(recorded)
        except TaskCleanupError as error:
            error.intent_reference = recorded.reference
            raise
        except Exception:
            error = TaskCleanupError("cleanup_recovery_failed", "recover")
            error.intent_reference = recorded.reference
            raise error from None

    def _recover_recorded_intent(self, recorded: RecordedCleanupIntent) -> CleanupAcknowledgement:
        intent = recorded.intent
        if intent.repository != self._store.repository:
            raise TaskCleanupError("cleanup_intent_repository_mismatch", "recover")
        branch_is_absent = self._branch_oid(intent.branch_ref) is None
        self._verify_intent_authority(intent, allow_branch_absent=branch_is_absent)
        if intent.operation == "cleanup.worktree_and_branch":
            if not self._root_and_admin_absent(intent):
                self._remove_worktree_step(intent, recovery=True)
            self._remove_branch_step(intent, recovery=True)
            return self._acknowledgement(intent, intent_reference=recorded.reference)
        if intent.operation == "cleanup.ephemeral":
            self._recover_ephemeral(intent)
            return self._acknowledgement(
                intent, ephemeral_absent=True, intent_reference=recorded.reference,
            )
        raise TaskCleanupError("unsupported_cleanup_intent", "recover")

    def _acknowledgement(
        self,
        intent: CleanupIntent,
        *,
        ephemeral_absent: bool = False,
        intent_reference: str | None = None,
    ) -> CleanupAcknowledgement:
        root_absent = self._root_and_admin_absent(intent) if intent.operation == "cleanup.worktree_and_branch" else False
        branch_absent = self._branch_oid(intent.branch_ref) is None
        authority, main = self._verify_intent_authority(intent, allow_branch_absent=branch_absent)
        retention = self._fresh_retention(authority, main, intent)
        if self._retention_digest(retention) != intent.retention_fingerprint:
            raise TaskCleanupError("retention_facts_changed", "recover")
        if intent.operation == "cleanup.ephemeral":
            current = self._current_registered_spec(intent, main)
            if current is None:
                raise TaskCleanupError("cleanup_postcondition_failed", "recover")
            spec, inspector, inventory = current
            if (
                _identity(inventory.root_identity) != intent.root_identity
                or inventory.git_pointer_sha256 != intent.git_pointer_sha256
                or self._branch_oid(intent.branch_ref) != intent.target_head
            ):
                raise TaskCleanupError("cleanup_postcondition_failed", "recover")
            try:
                if not inspector.is_absent(intent.ephemeral_scope):
                    raise TaskCleanupError("cleanup_postcondition_failed", "recover")
            except TaskCleanupError:
                raise
            except Exception:
                raise TaskCleanupError("cleanup_postcondition_unavailable", "recover") from None
            facts = self._validate_inventory(
                inventory, spec, Path(spec.worktree_root),
                self._read_regular_nofollow(Path(spec.worktree_root) / ".git", 4096),
            )
            if not self._ephemeral_remainder_matches(intent, facts, inventory):
                raise TaskCleanupError("cleanup_postcondition_failed", "recover")
            ephemeral_absent = True
        retained = intent.target_head in self._assert_complete_history(main.default_head)
        record_preserved = (
            authority.record_id == intent.record_id
            and authority.contract_id == intent.contract_id
            and authority.base_revision == intent.base_revision
        )
        if not retained or not record_preserved:
            raise TaskCleanupError("cleanup_postcondition_failed", "recover")
        if intent.operation == "cleanup.worktree_and_branch" and (not root_absent or not branch_absent):
            raise TaskCleanupError("cleanup_postcondition_failed", "recover")
        return CleanupAcknowledgement(
            intent.intent_id, intent.operation,
            root_absent, self._worktree_admin_absent(intent), branch_absent,
            retained, record_preserved, ephemeral_absent, intent_reference,
        )

    def cleanup_ephemeral(self, task: str, resource_reference: OpaqueResourceReference) -> CleanupAcknowledgement:
        """Remove one registrar-resolved child scope; never accepts a path."""
        task = _task_context(task)
        if type(resource_reference) is not OpaqueResourceReference:
            raise TaskCleanupError("opaque_resource_reference_required", "ephemeral")
        if (
            type(resource_reference.owner) is not str or not resource_reference.owner
            or type(resource_reference.token) is not str or not resource_reference.token
            or len(resource_reference.token) > 1024
        ):
            raise TaskCleanupError("invalid_resource_reference", "ephemeral")
        authority = self._read_authority(task)
        main = self._main_facts()
        spec, inspector, inventory = self._registered_task(authority, main)
        head = _oid(spec.head, self._store._oid_length)
        if head not in self._assert_complete_history(main.default_head):
            raise TaskCleanupError("task_head_not_retained", "ephemeral")
        view_store = MetadataStore(Path(spec.worktree_root), self._store.repository, self._store.remote)
        view = self._read_view(authority, main, view_store)
        self._diagnose("cleanup.ephemeral", authority, view, {
            "target_head": head, "record_id": authority.record_id,
            "contract_id": authority.contract_id, "inventory_id": inventory.inventory_id,
            "resource_owner": resource_reference.owner,
        })
        resolver = getattr(self._classifier, "resolve_resource", None)
        if not callable(resolver):
            raise TaskCleanupError("resource_registrar_unavailable", "ephemeral")
        try:
            binding = resolver(resource_reference, {
                "repository": self._store.repository, "task": task,
                "branch_ref": authority.branch_ref, "head": head,
                "inventory_id": inventory.inventory_id,
            })
        except Exception:
            raise TaskCleanupError("resource_binding_unavailable", "ephemeral") from None
        if type(binding) is not OwnedResourceBinding or (
            binding.repository != self._store.repository or binding.task != task
            or binding.branch_ref != authority.branch_ref or binding.head != head
            or binding.inventory_id != inventory.inventory_id
            or type(binding.relative_scope) is not str
            or type(binding.resource_kind) is not str or binding.resource_kind not in _DISPOSABLE_KINDS
            or type(binding.producer) is not str or binding.producer != resource_reference.owner
            or type(binding.binding_id) is not str or not _HEX64.fullmatch(binding.binding_id)
            or type(binding.node_manifest) is not tuple
        ):
            raise TaskCleanupError("resource_binding_mismatch", "ephemeral")
        scope = _relative(binding.relative_scope)
        try:
            nodes = inspector.relative_nodes(inventory, scope)
        except Exception:
            raise TaskCleanupError("resource_scope_unavailable", "ephemeral") from None
        if type(nodes) is not tuple or not nodes or nodes != binding.node_manifest:
            raise TaskCleanupError("resource_scope_manifest_mismatch", "ephemeral")
        tracked, unknown = self._tracked_paths_and_clean(spec, head, inventory)
        paths = {getattr(node, "relative_path", None) for node in nodes}
        if paths & tracked or not paths <= unknown:
            raise TaskCleanupError("resource_scope_contains_product_data", "ephemeral")
        proofs = self._classify(authority, spec, inventory, head, unknown)
        if not any(
            proof.relative_scope == scope
            and proof.resource_kind == binding.resource_kind
            and proof.producer == binding.producer
            and proof.node_manifest == binding.node_manifest
            for proof in proofs
        ):
            raise TaskCleanupError("resource_scope_not_classified", "ephemeral")
        node_facts = self._validate_inventory(
            inventory, spec, Path(spec.worktree_root),
            self._read_regular_nofollow(Path(spec.worktree_root) / ".git", 4096),
        )
        retention = self._retention(authority, main)
        intent = self._make_intent(
            authority, main, spec, inventory, node_facts, proofs, retention,
            operation="cleanup.ephemeral", binding=binding,
            resource_reference=resource_reference,
        )
        recorded = self._record(intent)
        try:
            self._remove_ephemeral_step(
                intent, binding, nodes, inspector, inventory, resource_reference,
            )
            return self._acknowledgement(
                intent, ephemeral_absent=True, intent_reference=recorded.reference,
            )
        except TaskCleanupError as error:
            error.intent_reference = recorded.reference
            raise
        except Exception:
            error = TaskCleanupError("ephemeral_cleanup_interrupted", "recover")
            error.intent_reference = recorded.reference
            raise error from None

    def _remove_ephemeral_step(
        self,
        intent: CleanupIntent,
        binding: OwnedResourceBinding,
        nodes: tuple[Any, ...],
        inspector: Any,
        inventory: Any,
        resource_reference: OpaqueResourceReference,
    ) -> None:
        authority, main = self._verify_intent_authority(intent)
        initial_main = main
        self._fresh_retention(authority, main, intent)
        current = self._current_registered_spec(intent, main)
        if current is None:
            raise TaskCleanupError("task_root_absent", "ephemeral")
        spec, inspector, fresh_inventory = current
        if fresh_inventory != inventory:
            raise TaskCleanupError("filesystem_inventory_changed", "ephemeral")
        self._verify_intent_inventory(intent, fresh_inventory, spec)
        try:
            fresh_nodes = inspector.relative_nodes(fresh_inventory, intent.ephemeral_scope)
        except Exception:
            raise TaskCleanupError("resource_scope_unavailable", "ephemeral") from None
        if fresh_nodes != nodes or not fresh_nodes:
            raise TaskCleanupError("resource_scope_changed", "ephemeral")
        resolver = getattr(self._classifier, "resolve_resource", None)
        if not callable(resolver):
            raise TaskCleanupError("resource_registrar_unavailable", "ephemeral")
        try:
            refreshed_binding = resolver(resource_reference, {
                "repository": self._store.repository, "task": intent.task,
                "branch_ref": intent.branch_ref, "head": intent.target_head,
                "inventory_id": fresh_inventory.inventory_id,
            })
        except Exception:
            raise TaskCleanupError("resource_binding_revalidation_failed", "ephemeral") from None
        if (
            type(refreshed_binding) is not OwnedResourceBinding
            or refreshed_binding != binding
            or refreshed_binding.binding_id != intent.ephemeral_binding_id
            or refreshed_binding.node_manifest != fresh_nodes
        ):
            raise TaskCleanupError("resource_binding_changed", "ephemeral")
        _tracked, unknown = self._tracked_paths_and_clean(spec, intent.target_head, fresh_inventory)
        fresh_proofs = self._classify(authority, spec, fresh_inventory, intent.target_head, unknown)
        if not any(
            proof.relative_scope == intent.ephemeral_scope
            and proof.resource_kind == binding.resource_kind
            and proof.producer == binding.producer
            and proof.node_manifest == fresh_nodes
            for proof in fresh_proofs
        ):
            raise TaskCleanupError("resource_scope_not_classified", "ephemeral")
        self._verify_proofs(intent, fresh_proofs, fresh_inventory)
        self._diagnose("cleanup.ephemeral", authority, self._read_view(authority, main, MetadataStore(Path(spec.worktree_root), self._store.repository, self._store.remote)), {
            "target_head": intent.target_head, "record_id": intent.record_id,
            "contract_id": intent.contract_id, "inventory_id": intent.inventory_id,
            "resource_owner": binding.producer,
        })
        capability, qualification_id = self._qualification("ephemeral", main)
        self._assert_main_stable(initial_main)
        self._fresh_retention(authority, self._main_facts(), intent)
        auth = self._authorize(intent, "ephemeral")
        authority_after, main_after = self._verify_intent_authority(intent)
        if main_after != initial_main or self._branch_oid(intent.branch_ref) != intent.target_head:
            raise TaskCleanupError("cleanup_state_changed_after_authorization", "ephemeral")
        self._fresh_retention(authority_after, main_after, intent)
        refreshed = self._current_registered_spec(intent, main_after)
        if refreshed is None or refreshed[2] != fresh_inventory:
            raise TaskCleanupError("filesystem_inventory_changed", "ephemeral")
        spec_after, inspector_after, inventory_after = refreshed
        self._verify_intent_inventory(intent, inventory_after, spec_after)
        try:
            nodes_after = inspector_after.relative_nodes(inventory_after, intent.ephemeral_scope)
        except Exception:
            raise TaskCleanupError("resource_scope_changed", "ephemeral") from None
        if nodes_after != nodes:
            raise TaskCleanupError("resource_scope_changed", "ephemeral")
        try:
            binding_after = resolver(resource_reference, {
                "repository": self._store.repository, "task": intent.task,
                "branch_ref": intent.branch_ref, "head": intent.target_head,
                "inventory_id": inventory_after.inventory_id,
            })
        except Exception:
            raise TaskCleanupError("resource_binding_revalidation_failed", "ephemeral") from None
        if type(binding_after) is not OwnedResourceBinding or binding_after != binding:
            raise TaskCleanupError("resource_binding_changed", "ephemeral")
        view_after = self._read_view(
            authority_after, main_after,
            MetadataStore(Path(spec_after.worktree_root), self._store.repository, self._store.remote),
        )
        self._diagnose("cleanup.ephemeral", authority_after, view_after, {
            "target_head": intent.target_head, "record_id": intent.record_id,
            "contract_id": intent.contract_id, "inventory_id": intent.inventory_id,
            "resource_owner": binding.producer,
        })
        authority_final, main_final = self._verify_intent_authority(intent)
        if (
            main_final != initial_main or self._branch_oid(intent.branch_ref) != intent.target_head
        ):
            raise TaskCleanupError("cleanup_state_changed_after_authorization", "ephemeral")
        self._fresh_retention(authority_final, main_final, intent)
        _tracked_final, unknown_final = self._tracked_paths_and_clean(
            spec_after, intent.target_head, inventory_after,
        )
        proofs_final = self._classify(authority_final, spec_after, inventory_after, intent.target_head, unknown_final)
        self._verify_proofs(intent, proofs_final, inventory_after)
        final_registered = self._current_registered_spec(intent, main_final)
        if final_registered is None or final_registered[2] != inventory_after:
            raise TaskCleanupError("filesystem_inventory_changed", "ephemeral")
        spec_final, inspector_final, inventory_final = final_registered
        nodes_final = inspector_final.relative_nodes(inventory_final, intent.ephemeral_scope)
        try:
            binding_final = resolver(resource_reference, {
                "repository": self._store.repository, "task": intent.task,
                "branch_ref": intent.branch_ref, "head": intent.target_head,
                "inventory_id": inventory_final.inventory_id,
            })
        except Exception:
            raise TaskCleanupError("resource_binding_revalidation_failed", "ephemeral") from None
        if (
            type(binding_final) is not OwnedResourceBinding or binding_final != binding
            or nodes_final != nodes
        ):
            raise TaskCleanupError("resource_binding_changed", "ephemeral")
        self._verify_intent_authority(intent)
        self._fresh_retention(authority_final, self._main_facts(), intent)
        try:
            inspector_final.revalidate(inventory_final)
            if inspector_final.relative_nodes(inventory_final, intent.ephemeral_scope) != nodes_final:
                raise TaskCleanupError("resource_scope_changed", "ephemeral")
        except TaskCleanupError:
            raise
        except Exception:
            raise TaskCleanupError("filesystem_revalidation_failed", "ephemeral") from None
        self._assert_main_stable(initial_main)
        target_digest = hashlib.sha256(_canonical_json([intent.ephemeral_scope, [_node_fact(node).__dict__ for node in nodes]])).hexdigest()
        permit = self._permit("ephemeral", qualification_id, intent, auth, target_digest)
        request = EphemeralRemovalRequest(
            spec_final, intent.ephemeral_scope, inventory_final, nodes_final,
            intent, binding_final, auth, permit,
        )
        try:
            capability._invoke(request)
        except Exception:
            # A partial/uncertain child deletion is never recursively resumed.
            raise TaskCleanupError("ephemeral_removal_uncertain", "ephemeral") from None
        try:
            # An authorized directory removal changes its parent's link count.
            # Re-open the same registry/root identity for post-observation,
            # then constrain *all* changes with the exact remainder manifest.
            # Reusing the pre-effect inspector would reject that expected
            # transition before we could verify the postcondition.
            post_registered = self._current_registered_spec(intent, self._main_facts())
            if post_registered is None:
                raise TaskCleanupError("ephemeral_postcondition_unavailable", "ephemeral")
            spec_post, inspector_post, post = post_registered
            if spec_post != spec_final:
                raise TaskCleanupError("ephemeral_postcondition_unavailable", "ephemeral")
            if not inspector_post.is_absent(intent.ephemeral_scope):
                raise TaskCleanupError("ephemeral_removal_postcondition_failed", "ephemeral")
            inspector_post.revalidate(post)
        except TaskCleanupError:
            raise
        except Exception:
            raise TaskCleanupError("ephemeral_postcondition_unavailable", "ephemeral") from None
        post_facts = self._validate_inventory(
            post, spec_final, Path(spec_final.worktree_root),
            self._read_regular_nofollow(Path(spec_final.worktree_root) / ".git", 4096),
        )
        if not self._ephemeral_remainder_matches(intent, post_facts, post):
            raise TaskCleanupError("ephemeral_partial_or_unexpected_change", "ephemeral")
        self._assert_main_stable(initial_main)
        self._verify_intent_authority(intent)

    @staticmethod
    def _ephemeral_remainder_matches(
        intent: CleanupIntent,
        current: tuple[CleanupNodeFact, ...],
        inventory: Any,
    ) -> bool:
        scope = intent.ephemeral_scope
        if scope is None:
            return False
        target = next((node for node in intent.nodes if node.relative_path == scope), None)
        if target is None or inventory is None:
            return False
        direct_parent = scope.rpartition("/")[0]
        removes_directory = target.kind == "directory"
        expected_root_nlink = intent.root_nlink - int(removes_directory and not direct_parent)
        if (
            _identity(inventory.root_identity) != intent.root_identity
            or inventory.root_identity.nlink != expected_root_nlink
            or _identity(inventory.parent_identity) != intent.parent_identity
            or inventory.git_pointer_sha256 != intent.git_pointer_sha256
        ):
            return False
        removed = {node.relative_path for node in intent.nodes if (
            node.relative_path == scope or node.relative_path.startswith(scope + "/")
        )}
        expected = {node.relative_path: node for node in intent.nodes if node.relative_path not in removed}
        observed = {node.relative_path: node for node in current}
        if set(expected) != set(observed):
            return False
        for path, before in expected.items():
            after = observed[path]
            is_ancestor_directory = before.kind == "directory" and scope.startswith(path + "/")
            if is_ancestor_directory:
                if (
                    before.relative_path, before.kind, before.device, before.inode,
                    before.mount_id, before.mode, before.uid,
                ) != (
                    after.relative_path, after.kind, after.device, after.inode,
                    after.mount_id, after.mode, after.uid,
                ):
                    return False
                expected_nlink = before.nlink - int(
                    removes_directory and bool(direct_parent) and path == direct_parent
                )
                if after.nlink != expected_nlink:
                    return False
            elif before != after:
                return False
        return True

    def _recover_ephemeral(self, intent: CleanupIntent) -> None:
        authority, main = self._verify_intent_authority(intent)
        current = self._current_registered_spec(intent, main)
        if current is None:
            raise TaskCleanupError("ephemeral_recovery_root_absent", "recover")
        spec, inspector, inventory = current
        if (
            _identity(inventory.root_identity) != intent.root_identity
            or _identity(inventory.parent_identity) != intent.parent_identity
            or _identity_path(Path(spec.git_admin).parent) != intent.admin_parent_identity
            or _identity_path(Path(spec.git_admin)) != intent.admin_identity
            or inventory.git_pointer_sha256 != intent.git_pointer_sha256
        ):
            raise TaskCleanupError("ephemeral_recovery_identity_mismatch", "recover")
        try:
            absent = inspector.is_absent(intent.ephemeral_scope)
        except Exception:
            raise TaskCleanupError("ephemeral_recovery_observation_failed", "recover") from None
        if absent:
            facts = self._validate_inventory(
                inventory, spec, Path(spec.worktree_root),
                self._read_regular_nofollow(Path(spec.worktree_root) / ".git", 4096),
            )
            if not self._ephemeral_remainder_matches(intent, facts, inventory):
                raise TaskCleanupError("ephemeral_partial_or_unexpected_change", "recover")
            return
        resolver = getattr(self._classifier, "resolve_resource", None)
        if not callable(resolver):
            raise TaskCleanupError("resource_registrar_unavailable", "recover")
        try:
            original_facts = self._validate_inventory(
                inventory, spec, Path(spec.worktree_root),
                self._read_regular_nofollow(Path(spec.worktree_root) / ".git", 4096),
            )
        except Exception:
            raise TaskCleanupError("ephemeral_partial_or_unexpected_change", "recover") from None
        if original_facts != intent.nodes:
            raise TaskCleanupError("ephemeral_partial_or_unexpected_change", "recover")
        try:
            binding = resolver(intent.resource_reference, {
                "repository": intent.repository, "task": intent.task,
                "branch_ref": intent.branch_ref, "head": intent.target_head,
                "inventory_id": inventory.inventory_id,
            })
        except Exception:
            raise TaskCleanupError("resource_binding_revalidation_failed", "recover") from None
        if (
            type(binding) is not OwnedResourceBinding
            or binding.binding_id != intent.ephemeral_binding_id
            or binding.relative_scope != intent.ephemeral_scope
            or binding.repository != intent.repository or binding.task != intent.task
            or binding.branch_ref != intent.branch_ref or binding.head != intent.target_head
            or binding.inventory_id != inventory.inventory_id
            or type(binding.resource_kind) is not str or binding.resource_kind not in _DISPOSABLE_KINDS
            or type(binding.producer) is not str
            or binding.node_manifest != tuple(
                node for node in inventory.nodes
                if node.relative_path == intent.ephemeral_scope
                or node.relative_path.startswith(intent.ephemeral_scope + "/")
            )
        ):
            raise TaskCleanupError("resource_binding_changed", "recover")
        try:
            nodes = inspector.relative_nodes(inventory, intent.ephemeral_scope)
        except Exception:
            raise TaskCleanupError("resource_scope_revalidation_failed", "recover") from None
        self._remove_ephemeral_step(
            intent, binding, nodes, inspector, inventory, intent.resource_reference,
        )


def _validate_intent(intent: CleanupIntent, oid_length: int) -> None:
    if (
        type(intent) is not CleanupIntent or type(intent.schema_version) is not int
        or intent.schema_version != SCHEMA_VERSION
    ):
        raise TaskCleanupError("invalid_cleanup_intent")
    if type(intent.operation) is not str or intent.operation not in {"cleanup.worktree_and_branch", "cleanup.ephemeral"}:
        raise TaskCleanupError("invalid_cleanup_intent")
    _task_context(intent.task)
    task_record.validate_branch_ref(intent.branch_ref)
    task_record.validate_branch_ref(intent.default_branch_ref)
    for value in (intent.base_revision, intent.target_head, intent.default_head, intent.metadata_tip):
        _oid(value, oid_length)
    if (
        type(intent.record_id) is not str or not _HEX64.fullmatch(intent.record_id)
        or type(intent.contract_id) is not str or not _HEX64.fullmatch(intent.contract_id)
    ):
        raise TaskCleanupError("invalid_cleanup_intent")
    if intent.disposition is not None and (
        type(intent.disposition) is not str or intent.disposition not in {"cancelled", "superseded"}
    ):
        raise TaskCleanupError("invalid_cleanup_intent")
    if intent.default_branch_ref == intent.branch_ref:
        raise TaskCleanupError("invalid_cleanup_intent")
    if intent.remote_task_head is not None:
        _oid(intent.remote_task_head, oid_length)
    for value in (intent.pull_request_head, intent.pull_request_merge_commit):
        if value is not None:
            _oid(value, oid_length)
    string_ids = (
        intent.main_state_fingerprint, intent.git_pointer_sha256,
        intent.inventory_id, intent.manifest_digest, intent.retention_fingerprint,
    )
    if (
        type(intent.worktree_root) is not str or type(intent.git_admin) is not str
        or not Path(intent.worktree_root).is_absolute() or not Path(intent.git_admin).is_absolute()
        or any(type(value) is not str or not _HEX64.fullmatch(value) for value in string_ids)
    ):
        raise TaskCleanupError("invalid_cleanup_intent")
    if any(
        type(identity) is not tuple or len(identity) != 3
        for identity in (
            intent.root_identity, intent.parent_identity,
            intent.admin_parent_identity, intent.admin_identity,
        )
    ) or any(
        type(item) is not int or item < 0
        for identity in (
            intent.root_identity, intent.parent_identity,
            intent.admin_parent_identity, intent.admin_identity,
        )
        for item in identity
    ):
        raise TaskCleanupError("invalid_cleanup_intent")
    if type(intent.root_nlink) is not int or intent.root_nlink < 1:
        raise TaskCleanupError("invalid_cleanup_intent")
    if type(intent.nodes) is not tuple or any(type(item) is not CleanupNodeFact for item in intent.nodes):
        raise TaskCleanupError("invalid_cleanup_intent")
    for node in intent.nodes:
        _relative(node.relative_path)
        if type(node.kind) is not str or node.kind not in {"file", "directory"}:
            raise TaskCleanupError("invalid_cleanup_intent")
        if any(
            type(value) is not int or value < 0
            for value in (
                node.device, node.inode, node.mount_id, node.mode, node.uid,
                node.nlink, node.size, node.mtime_ns, node.ctime_ns,
            )
        ):
            raise TaskCleanupError("invalid_cleanup_intent")
        if node.kind == "file":
            if node.nlink != 1 or type(node.content_sha256) is not str or not _HEX64.fullmatch(node.content_sha256):
                raise TaskCleanupError("invalid_cleanup_intent")
        elif node.content_sha256 is not None:
            raise TaskCleanupError("invalid_cleanup_intent")
    digest = hashlib.sha256(_canonical_json([node.__dict__ for node in intent.nodes])).hexdigest()
    if digest != intent.manifest_digest:
        raise TaskCleanupError("cleanup_intent_manifest_mismatch")
    if type(intent.resource_proofs) is not tuple or any(type(item) is not CleanupProofFact for item in intent.resource_proofs):
        raise TaskCleanupError("invalid_cleanup_intent")
    for proof in intent.resource_proofs:
        _relative(proof.relative_scope)
        if (
            type(proof.resource_kind) is not str or proof.resource_kind not in _DISPOSABLE_KINDS
            or type(proof.producer) is not str or not proof.producer
            or type(proof.proof_id) is not str or not _HEX64.fullmatch(proof.proof_id)
            or type(proof.node_digest) is not str or not _HEX64.fullmatch(proof.node_digest)
        ):
            raise TaskCleanupError("invalid_cleanup_intent")
    if intent.operation == "cleanup.ephemeral":
        _relative(intent.ephemeral_scope)
        if type(intent.ephemeral_binding_id) is not str or not _HEX64.fullmatch(intent.ephemeral_binding_id):
            raise TaskCleanupError("invalid_cleanup_intent")
        if (
            type(intent.resource_reference) is not OpaqueResourceReference
            or type(intent.resource_reference.owner) is not str
            or not intent.resource_reference.owner
            or type(intent.resource_reference.token) is not str
            or not intent.resource_reference.token
            or len(intent.resource_reference.token) > 1024
        ):
            raise TaskCleanupError("invalid_cleanup_intent")
    elif (
        intent.ephemeral_scope is not None or intent.ephemeral_binding_id is not None
        or intent.resource_reference is not None
    ):
        raise TaskCleanupError("invalid_cleanup_intent")


__all__ = [
    "BranchRemovalRequest",
    "CleanupAcknowledgement",
    "CleanupAuthorizationRequest",
    "CleanupIntent",
    "CleanupPlan",
    "CleanupQualification",
    "CoreTaskCleanup",
    "DisposableResourceProof",
    "EphemeralRemovalRequest",
    "OwnedResourceBinding",
    "OpaqueResourceReference",
    "QualifiedDeletionCapability",
    "RecordedCleanupIntent",
    "ResourceClassificationRequest",
    "TaskCleanupError",
    "TaskPullRequestFacts",
    "TaskRetentionFacts",
    "TaskRetentionRequest",
    "WorktreeRemovalRequest",
]
