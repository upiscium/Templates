"""Human-approved exact-PR integration and post-merge default reconciliation.

This is a deliberately small staged facade, not a general transaction manager
or a live GitHub adapter.  The host installs authenticated, bounded readers,
typed readiness and Human-approval validators, exact operation prerequisite
engines, and durable intent storage.  The only GitHub mutator exposed here is
``merge_exact``.  Its host transport must enforce repository, PR, head, base,
method, and tree preconditions atomically; ordinary GitHub merge endpoints that
only enforce the expected head are not sufficient and are refused.

The Task Record and Contract remain #191 authority, and #192 diagnoses remain
technical prerequisites only.  Neither a positive diagnosis, a CI result, a
non-draft PR, nor a merge acknowledgement is integration authority.  The exact
Human approval is a separate, authenticated reference bound to a digest of the
complete immutable integration subject.  Confirmed merges are verified from
authenticated PR, commit-parent/tree, and default-branch graph facts.  Later
default-branch synchronization is an independent local clean fast-forward; it
cannot create another PR, push a protected branch, rebase, clean up, or change
Task state.

Activation gate: this module has no production GitHub client.  A trusted host
must issue an opaque permit bound by object identity to the exact authenticated
transport.  Its host-only qualifier must attest atomic exact-head AND exact-base
checks, exact repository/PR, the full subject/method/tree, and current ready plus
Human-approval bindings at the server commit.  Adapter self-reported booleans
are only negative probes and cannot mint that permit.  Client-side rereads
detect races but cannot close approval revocation after the last read or undo a
merge to the wrong base.  Ordinary merge endpoints without the full atomic host
gate are refused; this Python boundary is not a sandbox against arbitrary code
that can replace the trusted host factory.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import metadata_codec as codec
import operation_prerequisites as prerequisites
import task_record
import task_view
from metadata_ref import MetadataStore


SCHEMA_VERSION = 1
MAX_REFERENCE_LENGTH = 256
MAX_PLAN_REFERENCE_LENGTH = 1024
MAX_INTENT_REFERENCE_LENGTH = 256
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_TASK = re.compile(r"[1-9][0-9]{0,127}\Z", re.ASCII)
_SUBJECT_DOMAIN = b"agentcore-integration-subject/v1\n"
_INTENT_DOMAIN = b"agentcore-integration-merge-intent/v1\n"
_SYNC_INTENT_DOMAIN = b"agentcore-integration-reconcile-intent/v1\n"
_REQUEST_DOMAIN = b"agentcore-operation-request/v1\n"
_VIEW_DOMAIN = b"agentcore-live-task-view-fingerprint/v1\n"

_MERGE_METHODS = frozenset({"merge", "squash"})
_CAPABILITY_FIELDS = frozenset({"exact_head", "exact_base", "exact_repository", "exact_pr"})
_PERMIT_SEAL = object()


class TaskIntegrationError(ValueError):
    """Safe bounded operation failure; callback and transport detail is hidden."""

    def __init__(
        self,
        code: str,
        *,
        operation: str = "capture_subject",
        diagnosis_bytes: bytes | None = None,
        merge_receipt: "IntegrationReceipt | None" = None,
    ) -> None:
        if type(code) is not str or not _SAFE_CODE.fullmatch(code):
            code = "task_integration_error"
        if operation not in {
            "capture_subject", "merge", "recover", "reconcile",
        }:
            operation = "capture_subject"
        self.code = code
        self.operation = operation
        self.diagnosis_bytes = diagnosis_bytes
        # A confirmed merge remains useful evidence when a later, independent
        # local reconciliation is blocked or uncertain.
        self.merge_receipt = merge_receipt
        super().__init__(f"{operation}:{code}")


@dataclass(frozen=True)
class RepositoryFacts:
    repository: str
    repository_id: int
    default_branch_ref: str
    allowed_merge_methods: tuple[str, ...]


@dataclass(frozen=True)
class PullFacts:
    number: int
    base_repository: str
    head_repository: str
    base_ref: str
    head_ref: str
    head_oid: str
    base_oid: str
    state: str
    draft: bool
    merge_oid: str | None = None
    merge_method: str | None = None


@dataclass(frozen=True)
class CheckpointObservation:
    """Explicit host observation; ``absent`` is not an omitted read."""

    state: str
    previous_checkpoint: task_view.PreviousCheckpoint | None = None


@dataclass(frozen=True)
class MergePlanFacts:
    repository: str
    task: str
    number: int
    head_oid: str
    base_oid: str
    base_ref: str
    method: str
    expected_tree_oid: str
    mergeable: bool
    plan_ref: str


@dataclass(frozen=True)
class GitHubCommitFacts:
    oid: str
    tree: str
    parents: tuple[str, ...]


@dataclass(frozen=True)
class MergeTransportCapabilities:
    """Optional negative probe; these booleans never qualify a transport."""

    exact_head: bool
    exact_base: bool
    exact_repository: bool
    exact_pr: bool


@dataclass(frozen=True)
class MergeTransportQualificationRequest:
    """Trusted-host request to attest the exact installed transport object."""

    transport: object
    repository: str
    repository_id: int
    default_branch_ref: str
    allowed_merge_methods: tuple[str, ...]
    required_guarantees: tuple[str, ...]


@dataclass(frozen=True)
class MergeTransportQualification:
    """Typed proof returned by a trusted host transport verifier.

    The verifier must bind these guarantees to ``transport`` itself and enforce
    them atomically in its only merge mutator.  A literal boolean, a transport
    method's self-reported capability flags, or agent-provided JSON is not a
    qualification.
    """

    transport: object
    repository: str
    repository_id: int
    default_branch_ref: str
    allowed_merge_methods: tuple[str, ...]
    qualification_ref: str
    atomic: bool
    exact_head: bool
    exact_base: bool
    exact_repository: bool
    exact_pr: bool
    exact_full_subject: bool
    exact_method_tree: bool
    exact_ready_binding: bool
    exact_human_approval: bool


@dataclass(frozen=True)
class MergeTransportPermit:
    """Opaque host-qualified permit bound by object identity to one adapter."""

    _transport: object
    repository: str
    repository_id: int
    default_branch_ref: str
    allowed_merge_methods: tuple[str, ...]
    qualification_ref: str
    _seal: object


@dataclass(frozen=True)
class IntegrationSubject:
    """Canonical exact PR merge proposal, bound to #191 and a ready reference."""

    schema_version: int
    repository: str
    repository_id: int
    task: str
    pull_request: int
    head_ref: str
    head_oid: str
    base_ref: str
    base_oid: str
    default_branch_ref: str
    record_id: str
    contract_id: str
    merge_method: str
    expected_merge_tree_oid: str
    plan_ref: str
    ready_reference_id: str

    @property
    def subject_id(self) -> str:
        return hashlib.sha256(_SUBJECT_DOMAIN + _canonical(_subject_wire(self))).hexdigest()


@dataclass(frozen=True)
class ReadyReference:
    subject_id: str
    reference_id: str


@dataclass(frozen=True)
class ReadyBinding:
    subject_id: str
    reference_id: str
    provenance_id: str


@dataclass(frozen=True)
class HumanApprovalReference:
    subject_id: str
    reference_id: str


@dataclass(frozen=True)
class HumanApprovalBinding:
    subject_id: str
    reference_id: str
    human_id: int
    purpose: str
    provenance_id: str


@dataclass(frozen=True)
class MergeIntent:
    """Frozen durable proposal; its digest covers every authority binding."""

    subject: IntegrationSubject
    subject_id: str
    ready_binding: ReadyBinding
    human_binding: HumanApprovalBinding
    principal: int
    request_id: str
    policy_id: str
    view_id: str
    intent_id: str


@dataclass(frozen=True)
class RecordedIntent:
    intent_ref: str
    intent_id: str


@dataclass(frozen=True)
class IntegrationReceipt:
    """Descriptive proof of one exact merge; it does not mutate Task phase."""

    subject_id: str
    repository: str
    task: str
    pull_request: int
    head_oid: str
    base_oid: str
    merge_oid: str
    merge_method: str
    tree_oid: str
    intent_id: str
    intent_ref: str
    approval_ref: str
    ready_ref: str


@dataclass(frozen=True)
class ReconciliationIntent:
    """Narrow, non-human local synchronization intent for one confirmed merge."""

    repository: str
    task: str
    branch_ref: str
    merge_intent_ref: str
    merge_oid: str
    expected_remote_oid: str
    old_head: str
    target_head: str
    target_tree: str
    changed_paths: tuple[bytes, ...]
    intent_id: str


@dataclass(frozen=True)
class RecordedReconciliationIntent:
    intent_ref: str
    intent_id: str


@dataclass(frozen=True)
class IntegrationReconciliationReceipt:
    merge_receipt: IntegrationReceipt
    default_receipt: object


class GitHubTransport(Protocol):
    """Narrow authenticated reader and exact PR merge host capability."""

    def repository(self) -> RepositoryFacts: ...
    def principal(self) -> int: ...
    def pull(self, number: int) -> PullFacts: ...
    def default_head(self) -> str: ...
    def merge_plan(self, number: int, method: str) -> MergePlanFacts: ...
    def commit(self, oid: str) -> GitHubCommitFacts: ...
    def is_default_ancestor(self, old: str, new: str) -> bool: ...
    def merge_capabilities(self) -> MergeTransportCapabilities: ...
    def merge_exact(self, intent: MergeIntent) -> object: ...


class ReadyValidator(Protocol):
    def __call__(self, reference: ReadyReference, subject: IntegrationSubject, mode: str) -> object: ...


class HumanValidator(Protocol):
    def __call__(
        self, reference: HumanApprovalReference, subject: IntegrationSubject, mode: str,
    ) -> object: ...


class CheckpointReader(Protocol):
    """Host-authenticated #194 observation; absence must be stated explicitly."""

    def __call__(
        self, request: task_view.GitHubPullRequestRequest,
    ) -> CheckpointObservation: ...


@dataclass(frozen=True)
class _LocalTaskFacts:
    repository: str
    task: str
    branch_ref: str
    head: str
    tree: str
    worktree: str
    clean: bool
    index_fingerprint: str
    status_fingerprint: str


@dataclass(frozen=True)
class _Capture:
    subject: IntegrationSubject
    repository: RepositoryFacts
    pull: PullFacts
    plan: MergePlanFacts
    principal: int
    task_local: _LocalTaskFacts
    task_remote_head: str
    default_local: object
    default_remote_head: str
    default_head: str
    task_view: dict[str, Any]
    task_view_bytes: bytes
    capabilities: MergeTransportCapabilities


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise TaskIntegrationError("invalid_canonical_value") from None


def _stable_view_bytes(value: object) -> bytes:
    """Fingerprint every Task View field except its unrelated #217 tip."""
    try:
        detached = json.loads(_canonical(value).decode("utf-8", errors="strict"))
        if (
            type(detached) is not dict
            or type(detached.get("authority")) is not dict
            or "metadata_commit" not in detached["authority"]
        ):
            raise ValueError
        detached["authority"]["metadata_commit"] = None
        return _canonical(detached)
    except Exception:
        raise TaskIntegrationError("invalid_live_task_view") from None


def _safe_text(value: object, maximum: int, code: str) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise TaskIntegrationError(code)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise TaskIntegrationError(code) from None
    if len(encoded) > maximum or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise TaskIntegrationError(code)
    return value


def _oid(value: object, oid_length: int, code: str = "invalid_oid") -> str:
    if type(value) is not str or len(value) != oid_length or not _OID.fullmatch(value):
        raise TaskIntegrationError(code)
    return value


def _metadata_id(value: object, code: str = "invalid_reference_id") -> str:
    if type(value) is not str or not _HEX_64.fullmatch(value):
        raise TaskIntegrationError(code)
    return value


def _positive_integer(value: object, code: str) -> int:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise TaskIntegrationError(code)
    return value


def _repo(value: object) -> str:
    if type(value) is not str:
        raise TaskIntegrationError("repository_binding_mismatch")
    try:
        codec.encode_object("contract", value, "1", "0" * 40, {})
    except (codec.MetadataCodecError, TypeError, ValueError):
        raise TaskIntegrationError("repository_binding_mismatch") from None
    return value


def _task(value: object) -> str:
    if type(value) is not str or len(value) > codec.MAX_TASK_LENGTH or not _TASK.fullmatch(value):
        raise TaskIntegrationError("invalid_task")
    return value


def _branch(value: object, code: str = "invalid_branch_ref") -> str:
    try:
        if type(value) is not str or len(value) > task_view.MAX_BRANCH_REF_LENGTH:
            raise ValueError
        result = task_record.validate_branch_ref(value)
        if len(result.encode("utf-8", errors="strict")) > task_view.MAX_BRANCH_REF_LENGTH:
            raise ValueError
        return result
    except (task_record.TaskRecordError, TypeError, ValueError, UnicodeEncodeError):
        raise TaskIntegrationError(code) from None


def _branch_name(value: object, code: str = "invalid_branch_ref") -> str:
    if type(value) is not str or not value or len(value) > task_view.MAX_BRANCH_REF_LENGTH:
        raise TaskIntegrationError(code)
    return _branch("refs/heads/" + value, code)[len("refs/heads/"):]


def _subject_wire(subject: IntegrationSubject) -> dict[str, Any]:
    """Validate and return the exact canonical subject fields (without digest)."""
    if type(subject) is not IntegrationSubject:
        raise TaskIntegrationError("invalid_integration_subject")
    if type(subject.schema_version) is not int or subject.schema_version != SCHEMA_VERSION:
        raise TaskIntegrationError("invalid_integration_subject")
    repository = _repo(subject.repository)
    _positive_integer(subject.repository_id, "invalid_integration_subject")
    task = _task(subject.task)
    _positive_integer(subject.pull_request, "invalid_integration_subject")
    head_ref = _branch(subject.head_ref)
    base_ref = _branch(subject.base_ref)
    default_ref = _branch(subject.default_branch_ref)
    if base_ref != default_ref or head_ref == default_ref:
        raise TaskIntegrationError("invalid_integration_subject")
    oid_length = len(subject.head_oid) if type(subject.head_oid) is str else -1
    if oid_length not in {40, 64}:
        raise TaskIntegrationError("invalid_integration_subject")
    head_oid = _oid(subject.head_oid, oid_length)
    base_oid = _oid(subject.base_oid, oid_length)
    tree_oid = _oid(subject.expected_merge_tree_oid, oid_length)
    record_id = _metadata_id(subject.record_id)
    contract_id = _metadata_id(subject.contract_id)
    if type(subject.merge_method) is not str or subject.merge_method not in _MERGE_METHODS:
        raise TaskIntegrationError("unsupported_merge_method")
    plan_ref = _safe_text(subject.plan_ref, MAX_PLAN_REFERENCE_LENGTH, "invalid_plan_reference")
    ready_reference_id = _metadata_id(subject.ready_reference_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "repository_id": subject.repository_id,
        "task": task,
        "pull_request": subject.pull_request,
        "head_ref": head_ref,
        "head_oid": head_oid,
        "base_ref": base_ref,
        "base_oid": base_oid,
        "default_branch_ref": default_ref,
        "record_id": record_id,
        "contract_id": contract_id,
        "merge_method": subject.merge_method,
        "expected_merge_tree_oid": tree_oid,
        "plan_ref": plan_ref,
        "ready_reference_id": ready_reference_id,
    }


def _validate_subject(subject: object) -> IntegrationSubject:
    wire = _subject_wire(subject)  # type: ignore[arg-type]
    try:
        _canonical(wire)
    except TaskIntegrationError:
        raise TaskIntegrationError("invalid_integration_subject") from None
    return subject  # type: ignore[return-value]


def _validate_capabilities(value: object) -> MergeTransportCapabilities:
    if type(value) is not MergeTransportCapabilities or any(
        type(getattr(value, name)) is not bool for name in _CAPABILITY_FIELDS
    ):
        raise TaskIntegrationError("merge_transport_unqualified")
    if not all(getattr(value, name) for name in _CAPABILITY_FIELDS):
        raise TaskIntegrationError("merge_transport_unqualified")
    return value


_REQUIRED_MERGE_GUARANTEES = (
    "atomic_exact_head",
    "atomic_exact_base",
    "atomic_exact_repository_and_pr",
    "atomic_full_subject_method_and_tree",
    "atomic_current_ready_binding",
    "atomic_current_human_approval",
)


def qualify_merge_transport(
    transport: object,
    *,
    repository: str,
    repository_id: int,
    default_branch_ref: str,
    allowed_merge_methods: tuple[str, ...],
    host_qualifier: Callable[[MergeTransportQualificationRequest], object],
) -> MergeTransportPermit:
    """Ask a trusted host verifier to qualify one concrete transport adapter.

    This factory intentionally does not authenticate a GitHub client itself.
    The installed ``host_qualifier`` must attest the exact adapter object and
    all listed commit-time guarantees.  It must be supplied by the trusted
    host, not parsed from model output.  Python module-private seals are a
    capability boundary against accidental/caller DTO substitution, not a
    sandbox against arbitrary code that can replace the host factory.
    """
    _repo(repository)
    _branch(default_branch_ref)
    if (
        type(repository_id) is not int or not 1 <= repository_id <= 2**63 - 1
        or type(allowed_merge_methods) is not tuple
        or not allowed_merge_methods or len(allowed_merge_methods) > 8
        or any(type(item) is not str or item not in {"merge", "squash", "rebase"}
               for item in allowed_merge_methods)
        or len(set(allowed_merge_methods)) != len(allowed_merge_methods)
        or not callable(host_qualifier)
    ):
        raise TaskIntegrationError("merge_transport_unqualified")
    request = MergeTransportQualificationRequest(
        transport,
        repository,
        repository_id,
        default_branch_ref,
        allowed_merge_methods,
        _REQUIRED_MERGE_GUARANTEES,
    )
    try:
        proof = host_qualifier(request)
    except Exception:
        raise TaskIntegrationError("merge_transport_unqualified") from None
    proof_flags = (
        "atomic", "exact_head", "exact_base", "exact_repository", "exact_pr",
        "exact_full_subject", "exact_method_tree", "exact_ready_binding",
        "exact_human_approval",
    )
    if type(proof) is not MergeTransportQualification or any(
        type(getattr(proof, name)) is not bool or getattr(proof, name) is not True
        for name in proof_flags
    ):
        raise TaskIntegrationError("merge_transport_unqualified")
    if (
        proof.transport is not transport
        or type(proof.repository) is not str
        or proof.repository != repository
        or type(proof.repository_id) is not int
        or not 1 <= proof.repository_id <= 2**63 - 1
        or proof.repository_id != repository_id
        or type(proof.default_branch_ref) is not str
        or proof.default_branch_ref != default_branch_ref
        or type(proof.allowed_merge_methods) is not tuple
        or proof.allowed_merge_methods != allowed_merge_methods
    ):
        raise TaskIntegrationError("merge_transport_binding_mismatch")
    qualification_ref = _metadata_id(proof.qualification_ref, "merge_transport_unqualified")
    return MergeTransportPermit(
        transport,
        repository,
        repository_id,
        default_branch_ref,
        allowed_merge_methods,
        qualification_ref,
        _PERMIT_SEAL,
    )


def _validate_transport_permit(
    value: object,
    *,
    transport: object,
    repository: str,
    repository_id: int,
    default_branch_ref: str,
    allowed_merge_methods: tuple[str, ...],
) -> MergeTransportPermit:
    if (
        type(value) is not MergeTransportPermit
        or value._seal is not _PERMIT_SEAL
        or value._transport is not transport
        or value.repository != repository
        or type(value.repository_id) is not int
        or value.repository_id != repository_id
        or value.default_branch_ref != default_branch_ref
        or type(value.allowed_merge_methods) is not tuple
        or value.allowed_merge_methods != allowed_merge_methods
    ):
        raise TaskIntegrationError("merge_transport_permit_mismatch", operation="merge")
    _metadata_id(value.qualification_ref, "merge_transport_permit_mismatch")
    return value


def _validate_repository_facts(value: object, *, repository: str, default_ref: str) -> RepositoryFacts:
    if type(value) is not RepositoryFacts:
        raise TaskIntegrationError("invalid_repository_facts")
    _repo(value.repository)
    _branch(value.default_branch_ref)
    if (
        value.repository != repository
        or type(value.repository_id) is not int
        or not 1 <= value.repository_id <= 2**63 - 1
        or value.default_branch_ref != default_ref
        or type(value.allowed_merge_methods) is not tuple
        or not value.allowed_merge_methods
        or len(value.allowed_merge_methods) > 8
        or any(type(method) is not str or method not in {"merge", "squash", "rebase"}
               for method in value.allowed_merge_methods)
        or len(set(value.allowed_merge_methods)) != len(value.allowed_merge_methods)
    ):
        raise TaskIntegrationError("repository_binding_mismatch")
    return value


def _validate_pull(value: object, *, number: int, repository: str, oid_length: int) -> PullFacts:
    if type(value) is not PullFacts:
        raise TaskIntegrationError("invalid_pull_facts")
    _repo(value.base_repository)
    _repo(value.head_repository)
    _branch_name(value.base_ref)
    _branch_name(value.head_ref)
    _oid(value.head_oid, oid_length)
    _oid(value.base_oid, oid_length)
    if value.merge_oid is not None:
        _oid(value.merge_oid, oid_length)
    if value.merge_method is not None and (
        type(value.merge_method) is not str or value.merge_method not in {"merge", "squash", "rebase"}
    ):
        raise TaskIntegrationError("invalid_pull_facts")
    if (
        type(value.number) is not int or not 1 <= value.number <= 2**63 - 1
        or value.number != number
        or value.base_repository != repository or value.head_repository != repository
        or type(value.state) is not str or value.state not in {"open", "closed", "merged"}
        or type(value.draft) is not bool
    ):
        raise TaskIntegrationError("pull_request_binding_mismatch")
    return value


def _validate_plan(
    value: object,
    *,
    subject_repository: str,
    task: str,
    pull: PullFacts,
    method: str,
    oid_length: int,
) -> MergePlanFacts:
    if type(value) is not MergePlanFacts:
        raise TaskIntegrationError("invalid_merge_plan")
    _repo(value.repository)
    _task(value.task)
    _oid(value.head_oid, oid_length)
    _oid(value.base_oid, oid_length)
    _branch(value.base_ref)
    _oid(value.expected_tree_oid, oid_length)
    _safe_text(value.plan_ref, MAX_PLAN_REFERENCE_LENGTH, "invalid_plan_reference")
    if (
        type(value.number) is not int or not 1 <= value.number <= 2**63 - 1
        or type(value.mergeable) is not bool
        or type(value.method) is not str
        or value.repository != subject_repository or value.task != task
        or value.number != pull.number or value.head_oid != pull.head_oid
        or value.base_oid != pull.base_oid or value.base_ref != "refs/heads/" + pull.base_ref
        or value.method != method or value.mergeable is not True
    ):
        raise TaskIntegrationError("merge_plan_binding_mismatch")
    return value


def _validate_commit(value: object, *, oid: str, oid_length: int) -> GitHubCommitFacts:
    if type(value) is not GitHubCommitFacts:
        raise TaskIntegrationError("invalid_commit_facts")
    _oid(value.oid, oid_length)
    _oid(value.tree, oid_length)
    if type(value.parents) is not tuple or len(value.parents) > 100:
        raise TaskIntegrationError("invalid_commit_facts")
    for parent in value.parents:
        _oid(parent, oid_length)
    if value.oid != oid:
        raise TaskIntegrationError("commit_binding_mismatch")
    return value


def _reference_text(value: object, code: str) -> str:
    return _safe_text(value, MAX_REFERENCE_LENGTH, code)


def _checkpoint_previous(value: object, operation: str) -> task_view.PreviousCheckpoint | None:
    if type(value) is not CheckpointObservation or type(value.state) is not str:
        raise TaskIntegrationError("checkpoint_observation_unavailable", operation=operation)
    if value.state == "absent" and value.previous_checkpoint is None:
        return None
    if value.state == "observed" and type(value.previous_checkpoint) is task_view.PreviousCheckpoint:
        return value.previous_checkpoint
    raise TaskIntegrationError("checkpoint_observation_unavailable", operation=operation)


def _ready_reference(value: object, subject: IntegrationSubject) -> ReadyReference:
    if type(value) is not ReadyReference:
        raise TaskIntegrationError("invalid_ready_reference", operation="merge")
    _metadata_id(value.subject_id, "invalid_ready_reference")
    _metadata_id(value.reference_id, "invalid_ready_reference")
    if value.subject_id != subject.subject_id or value.reference_id != subject.ready_reference_id:
        raise TaskIntegrationError("ready_reference_binding_mismatch", operation="merge")
    return value


def _approval_reference(value: object, subject: IntegrationSubject) -> HumanApprovalReference:
    if type(value) is not HumanApprovalReference:
        raise TaskIntegrationError("invalid_human_approval_reference", operation="merge")
    _metadata_id(value.subject_id, "invalid_human_approval_reference")
    _metadata_id(value.reference_id, "invalid_human_approval_reference")
    if value.subject_id != subject.subject_id:
        raise TaskIntegrationError("human_approval_binding_mismatch", operation="merge")
    return value


def _ready_binding(value: object, reference: ReadyReference, subject: IntegrationSubject) -> ReadyBinding:
    if type(value) is not ReadyBinding:
        raise TaskIntegrationError("ready_validator_unavailable", operation="merge")
    _metadata_id(value.subject_id, "invalid_ready_binding")
    _metadata_id(value.reference_id, "invalid_ready_binding")
    _metadata_id(value.provenance_id, "invalid_ready_binding")
    if value.subject_id != subject.subject_id or value.reference_id != reference.reference_id:
        raise TaskIntegrationError("ready_binding_mismatch", operation="merge")
    return value


def _human_binding(
    value: object, reference: HumanApprovalReference, subject: IntegrationSubject,
) -> HumanApprovalBinding:
    if type(value) is not HumanApprovalBinding:
        raise TaskIntegrationError("human_approval_unavailable", operation="merge")
    _metadata_id(value.subject_id, "invalid_human_approval_binding")
    _metadata_id(value.reference_id, "invalid_human_approval_binding")
    _metadata_id(value.provenance_id, "invalid_human_approval_binding")
    if (
        type(value.human_id) is not int or not 1 <= value.human_id <= 2**63 - 1
        or type(value.purpose) is not str or value.purpose != "approve_integration"
        or value.subject_id != subject.subject_id or value.reference_id != reference.reference_id
    ):
        raise TaskIntegrationError("human_approval_binding_mismatch", operation="merge")
    return value


def _merge_intent_wire(intent: object) -> dict[str, Any]:
    if type(intent) is not MergeIntent:
        raise TaskIntegrationError("invalid_merge_intent", operation="recover")
    subject = _validate_subject(intent.subject)
    if intent.subject_id != subject.subject_id:
        raise TaskIntegrationError("invalid_merge_intent", operation="recover")
    _metadata_id(intent.request_id, "invalid_merge_intent")
    _metadata_id(intent.policy_id, "invalid_merge_intent")
    _metadata_id(intent.view_id, "invalid_merge_intent")
    _positive_integer(intent.principal, "invalid_merge_intent")
    if type(intent.ready_binding) is not ReadyBinding or type(intent.human_binding) is not HumanApprovalBinding:
        raise TaskIntegrationError("invalid_merge_intent", operation="recover")
    ready = intent.ready_binding
    human = intent.human_binding
    _metadata_id(ready.subject_id, "invalid_merge_intent")
    _metadata_id(ready.reference_id, "invalid_merge_intent")
    _metadata_id(ready.provenance_id, "invalid_merge_intent")
    _metadata_id(human.subject_id, "invalid_merge_intent")
    _metadata_id(human.reference_id, "invalid_merge_intent")
    _metadata_id(human.provenance_id, "invalid_merge_intent")
    if (
        type(human.human_id) is not int or not 1 <= human.human_id <= 2**63 - 1
        or type(human.purpose) is not str or human.purpose != "approve_integration"
        or ready.subject_id != subject.subject_id
        or ready.reference_id != subject.ready_reference_id
        or human.subject_id != subject.subject_id
    ):
        raise TaskIntegrationError("invalid_merge_intent", operation="recover")
    return {
        "schema_version": SCHEMA_VERSION,
        "subject": _subject_wire(subject),
        "subject_id": subject.subject_id,
        "ready_binding": {
            "subject_id": ready.subject_id,
            "reference_id": ready.reference_id,
            "provenance_id": ready.provenance_id,
        },
        "human_binding": {
            "subject_id": human.subject_id,
            "reference_id": human.reference_id,
            "human_id": human.human_id,
            "purpose": human.purpose,
            "provenance_id": human.provenance_id,
        },
        "principal": intent.principal,
        "request_id": intent.request_id,
        "policy_id": intent.policy_id,
        "view_id": intent.view_id,
    }


def _intent_id(wire: dict[str, Any]) -> str:
    return hashlib.sha256(_INTENT_DOMAIN + _canonical(wire)).hexdigest()


def _validate_merge_intent(value: object) -> MergeIntent:
    try:
        wire = _merge_intent_wire(value)
        if value.intent_id != _intent_id(wire):  # type: ignore[union-attr]
            raise TaskIntegrationError("merge_intent_digest_mismatch", operation="recover")
        return value  # type: ignore[return-value]
    except TaskIntegrationError as error:
        raise TaskIntegrationError(error.code, operation="recover") from None


def _make_merge_intent(
    subject: IntegrationSubject,
    ready: ReadyBinding,
    human: HumanApprovalBinding,
    principal: int,
    diagnosis: dict[str, Any],
) -> MergeIntent:
    provisional = MergeIntent(
        subject, subject.subject_id, ready, human, principal,
        diagnosis["request_id"], diagnosis["policy_id"], diagnosis["view_id"], "0" * 64,
    )
    wire = _merge_intent_wire(provisional)
    return MergeIntent(
        subject, subject.subject_id, ready, human, principal,
        diagnosis["request_id"], diagnosis["policy_id"], diagnosis["view_id"], _intent_id(wire),
    )


def _recorded_intent(value: object, intent: MergeIntent) -> RecordedIntent:
    if type(value) is not RecordedIntent:
        raise TaskIntegrationError("merge_intent_not_durably_recorded", operation="merge")
    reference = _reference_text(value.intent_ref, "invalid_merge_intent_reference")
    _metadata_id(value.intent_id, "invalid_merge_intent_reference")
    if value.intent_id != intent.intent_id:
        raise TaskIntegrationError("merge_intent_ack_mismatch", operation="merge")
    return RecordedIntent(reference, value.intent_id)


def _receipt_for(subject: IntegrationSubject, intent: MergeIntent, intent_ref: str, merge_oid: str) -> IntegrationReceipt:
    return IntegrationReceipt(
        subject.subject_id,
        subject.repository,
        subject.task,
        subject.pull_request,
        subject.head_oid,
        subject.base_oid,
        merge_oid,
        subject.merge_method,
        subject.expected_merge_tree_oid,
        intent.intent_id,
        intent_ref,
        intent.human_binding.reference_id,
        intent.ready_binding.reference_id,
    )


class TaskIntegration:
    """Exact-subject human-owned PR merge and scoped default reconciliation.

    ``store`` and ``task_git`` describe the current Task worktree. ``default_git``
    must be an installed ``integration_git.DefaultGit`` whose MetadataStore
    resolves to the same filesystem Git common-directory identity. Repository
    slug equality alone is deliberately insufficient.
    """

    def __init__(
        self,
        store: MetadataStore,
        task_git: object,
        default_git: object,
        github: GitHubTransport,
        *,
        merge_transport_permit: MergeTransportPermit,
        prerequisites_by_operation: dict[str, object],
        ready_validator: ReadyValidator,
        human_validator: HumanValidator,
        record_intent: Callable[[MergeIntent], object],
        load_intent: Callable[[str], object],
        record_sync_intent: Callable[[ReconciliationIntent], object] | None = None,
        checkpoint_reader: CheckpointReader,
    ) -> None:
        if not isinstance(store, MetadataStore):
            raise TaskIntegrationError("invalid_metadata_store")
        if not all(callable(item) for item in (ready_validator, human_validator, record_intent, load_intent)):
            raise TaskIntegrationError("host_capability_required")
        if record_sync_intent is not None and not callable(record_sync_intent):
            raise TaskIntegrationError("invalid_sync_intent_capability")
        if not callable(checkpoint_reader):
            raise TaskIntegrationError("invalid_checkpoint_reader")
        if type(prerequisites_by_operation) is not dict:
            raise TaskIntegrationError("operation_prerequisites_required")

        try:
            integration_git = importlib.import_module("integration_git")
        except Exception:
            raise TaskIntegrationError("default_git_unavailable") from None
        if not isinstance(default_git, integration_git.DefaultGit):
            raise TaskIntegrationError("invalid_default_git")
        task_store = getattr(task_git, "store", None)
        if task_store is not store:
            raise TaskIntegrationError("task_git_binding_mismatch")
        default_store = getattr(default_git, "store", None)
        if not isinstance(default_store, MetadataStore):
            raise TaskIntegrationError("invalid_default_git")
        try:
            repository = _repo(store.repository)
            if _repo(getattr(task_git, "repository", None)) != repository:
                raise TaskIntegrationError("task_git_binding_mismatch")
            task = _task(getattr(task_git, "task", None))
            branch_ref = _branch(getattr(task_git, "branch_ref", None))
            base_revision = _oid(getattr(task_git, "base_revision", None), store._oid_length)
            default_branch_ref = _branch(getattr(task_git, "default_branch_ref", None))
            if _repo(default_store.repository) != repository:
                raise TaskIntegrationError("default_git_binding_mismatch")
            if _branch(getattr(default_git, "default_branch_ref", None)) != default_branch_ref:
                raise TaskIntegrationError("default_git_binding_mismatch")
            if branch_ref == default_branch_ref:
                raise TaskIntegrationError("task_branch_is_default")
            if type(merge_transport_permit) is not MergeTransportPermit:
                raise TaskIntegrationError("merge_transport_permit_required", operation="merge")
            # Validate the private seal and adapter identity before making any
            # authenticated reads or accepting its self-reported flags.
            _validate_transport_permit(
                merge_transport_permit,
                transport=github,
                repository=repository,
                repository_id=merge_transport_permit.repository_id,
                default_branch_ref=default_branch_ref,
                allowed_merge_methods=merge_transport_permit.allowed_merge_methods,
            )
            common_identity = self._common_directory_identity(store)
            default_common_identity = self._common_directory_identity(default_store)
        except TaskIntegrationError:
            raise
        except Exception:
            raise TaskIntegrationError("task_git_binding_mismatch") from None
        if common_identity != default_common_identity:
            raise TaskIntegrationError("distinct_git_object_store")
        if not all(callable(getattr(github, name, None)) for name in (
            "repository", "principal", "pull", "default_head", "merge_plan",
            "commit", "is_default_ancestor", "merge_capabilities", "merge_exact",
        )):
            raise TaskIntegrationError("github_host_capability_required")

        installed: dict[str, prerequisites.OperationPrerequisites] = {}
        for operation, engine in prerequisites_by_operation.items():
            if (
                type(operation) is not str or operation not in {"integration.merge", "integration.reconcile"}
                or type(engine) is not prerequisites.OperationPrerequisites
            ):
                raise TaskIntegrationError("invalid_installed_prerequisites")
            installed[operation] = engine
        self._store = store
        self._task_git = task_git
        self._default_git = default_git
        self._default_store = default_store
        self._github = github
        self._integration_git = integration_git
        self._repository = repository
        self._task = task
        self._branch_ref = branch_ref
        self._base_revision = base_revision
        self._default_branch_ref = default_branch_ref
        self._common_identity = common_identity
        self._engines = installed
        self._ready_validator = ready_validator
        self._human_validator = human_validator
        self._record_intent = record_intent
        self._load_intent = load_intent
        self._record_sync_intent = record_sync_intent
        self._checkpoint_reader = checkpoint_reader
        self._repository_facts = self._read_repository()
        self._merge_transport_permit = _validate_transport_permit(
            merge_transport_permit,
            transport=github,
            repository=self._repository,
            repository_id=self._repository_facts.repository_id,
            default_branch_ref=self._default_branch_ref,
            allowed_merge_methods=self._repository_facts.allowed_merge_methods,
        )
        self._qualified_capabilities()

    @staticmethod
    def _common_directory_identity(store: MetadataStore) -> tuple[int, int]:
        """Read and stat the real common Git directory, without mutating it."""
        try:
            output = store._git(
                ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                max_stdout=16 * 1024,
            )
            if output is None:
                raise ValueError
            raw = output.decode("utf-8", errors="strict").strip()
            path = Path(raw)
            info = path.lstat()
            if (
                not path.is_absolute() or stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode) or path.resolve(strict=True) != path
            ):
                raise ValueError
            return info.st_dev, info.st_ino
        except (OSError, UnicodeError, ValueError, RuntimeError):
            raise TaskIntegrationError("git_common_directory_unavailable") from None

    def _assert_common_identity(self, operation: str) -> None:
        try:
            task_identity = self._common_directory_identity(self._store)
            default_identity = self._common_directory_identity(self._default_store)
        except TaskIntegrationError:
            raise TaskIntegrationError("git_common_directory_unavailable", operation=operation) from None
        if task_identity != default_identity or task_identity != self._common_identity:
            raise TaskIntegrationError("git_common_directory_changed", operation=operation)

    @property
    def repository(self) -> str:
        return self._repository

    @property
    def task(self) -> str:
        return self._task

    @property
    def branch_ref(self) -> str:
        return self._branch_ref

    def _call(self, callback: Callable[..., object], operation: str, code: str, *args: object) -> object:
        try:
            return callback(*args)
        except Exception:
            raise TaskIntegrationError(code, operation=operation) from None

    def _read_repository(self, operation: str = "capture_subject") -> RepositoryFacts:
        value = self._call(self._github.repository, operation, "github_repository_unavailable")
        facts = _validate_repository_facts(
            value, repository=self._repository, default_ref=self._default_branch_ref,
        )
        prior = getattr(self, "_repository_facts", None)
        if prior is not None and facts != prior:
            raise TaskIntegrationError("github_repository_identity_changed", operation=operation)
        return facts

    def _principal(self, operation: str) -> int:
        value = self._call(self._github.principal, operation, "github_principal_unavailable")
        if type(value) is not int or not 1 <= value <= 2**63 - 1:
            raise TaskIntegrationError("github_principal_unavailable", operation=operation)
        return value

    def _pull(self, number: int, operation: str) -> PullFacts:
        value = self._call(self._github.pull, operation, "github_pull_unavailable", number)
        return _validate_pull(
            value, number=number, repository=self._repository, oid_length=self._store._oid_length,
        )

    def _default_head(self, operation: str) -> str:
        value = self._call(self._github.default_head, operation, "github_default_head_unavailable")
        return _oid(value, self._store._oid_length, "invalid_github_default_head")

    def _qualified_capabilities(self, operation: str = "merge") -> MergeTransportCapabilities:
        facts = self._read_repository(operation)
        permit = _validate_transport_permit(
            self._merge_transport_permit,
            transport=self._github,
            repository=self._repository,
            repository_id=facts.repository_id,
            default_branch_ref=self._default_branch_ref,
            allowed_merge_methods=facts.allowed_merge_methods,
        )
        if permit.qualification_ref != self._merge_transport_permit.qualification_ref:
            raise TaskIntegrationError("merge_transport_permit_mismatch", operation=operation)
        # Legacy-style adapter flags are only a negative probe.  A typed all-true
        # response cannot substitute for the host-issued opaque permit.
        value = self._call(
            self._github.merge_capabilities, operation, "merge_transport_unqualified",
        )
        return _validate_capabilities(value)

    def _plan(self, number: int, method: str, pull: PullFacts, operation: str) -> MergePlanFacts:
        value = self._call(
            self._github.merge_plan, operation, "merge_plan_unavailable", number, method,
        )
        return _validate_plan(
            value,
            subject_repository=self._repository,
            task=self._task,
            pull=pull,
            method=method,
            oid_length=self._store._oid_length,
        )

    def _github_commit(self, oid: str, operation: str) -> GitHubCommitFacts:
        value = self._call(self._github.commit, operation, "github_commit_unavailable", oid)
        return _validate_commit(value, oid=oid, oid_length=self._store._oid_length)

    def _github_ancestor(self, old: str, new: str, operation: str) -> bool:
        value = self._call(
            self._github.is_default_ancestor, operation, "github_graph_unavailable", old, new,
        )
        if type(value) is not bool:
            raise TaskIntegrationError("invalid_github_graph_facts", operation=operation)
        return value

    def _task_local(self, operation: str, *, require_clean: bool) -> _LocalTaskFacts:
        try:
            value = self._task_git.observe()
            facts = _LocalTaskFacts(**{
                name: getattr(value, name)
                for name in (
                    "repository", "task", "branch_ref", "head", "tree", "worktree",
                    "clean", "index_fingerprint", "status_fingerprint",
                )
            })
        except Exception:
            raise TaskIntegrationError("task_local_facts_unavailable", operation=operation) from None
        oid_length = self._store._oid_length
        _repo(facts.repository)
        _task(facts.task)
        _branch(facts.branch_ref)
        _oid(facts.head, oid_length)
        _oid(facts.tree, oid_length)
        for fingerprint in (facts.index_fingerprint, facts.status_fingerprint):
            _metadata_id(fingerprint, "invalid_task_local_facts")
        if (
            facts.repository != self._repository or facts.task != self._task
            or facts.branch_ref != self._branch_ref or type(facts.clean) is not bool
            or type(facts.worktree) is not str
        ):
            raise TaskIntegrationError("task_local_binding_mismatch", operation=operation)
        _safe_text(facts.worktree, 4096, "task_worktree_binding_mismatch")
        try:
            if Path(facts.worktree).resolve(strict=True) != self._store.root.resolve(strict=True):
                raise ValueError
        except (OSError, RuntimeError, ValueError):
            raise TaskIntegrationError("task_worktree_binding_mismatch", operation=operation) from None
        if require_clean and facts.clean is not True:
            raise TaskIntegrationError("task_worktree_not_clean", operation=operation)
        return facts

    def _task_remote_head(self, operation: str) -> str | None:
        try:
            value = self._task_git.remote_head()
        except Exception:
            raise TaskIntegrationError("task_remote_head_unavailable", operation=operation) from None
        if value is not None:
            _oid(value, self._store._oid_length, "invalid_task_remote_head")
        return value

    def _default_facts(self, operation: str) -> object:
        try:
            facts = self._default_git.observe()
        except Exception:
            raise TaskIntegrationError("default_local_facts_unavailable", operation=operation) from None
        if type(facts) is not self._integration_git.DefaultFacts:
            raise TaskIntegrationError("invalid_default_local_facts", operation=operation)
        for field in ("repository", "branch_ref", "worktree"):
            if type(getattr(facts, field)) is not str:
                raise TaskIntegrationError("invalid_default_local_facts", operation=operation)
        _repo(facts.repository)
        _branch(facts.branch_ref)
        _oid(facts.head, self._store._oid_length)
        _oid(facts.tree, self._store._oid_length)
        _metadata_id(facts.index_fingerprint, "invalid_default_local_facts")
        _metadata_id(facts.status_fingerprint, "invalid_default_local_facts")
        try:
            worktree = Path(facts.worktree).resolve(strict=True)
            if worktree != self._default_store.root.resolve(strict=True):
                raise ValueError
        except (OSError, RuntimeError, ValueError):
            raise TaskIntegrationError("default_worktree_binding_mismatch", operation=operation) from None
        if (
            facts.repository != self._repository or facts.branch_ref != self._default_branch_ref
            or type(facts.clean) is not bool
        ):
            raise TaskIntegrationError("default_local_binding_mismatch", operation=operation)
        _safe_text(facts.worktree, 4096, "default_worktree_binding_mismatch")
        return facts

    def _default_remote_head(self, operation: str) -> str | None:
        try:
            value = self._default_git.remote_head()
        except Exception:
            raise TaskIntegrationError("default_remote_head_unavailable", operation=operation) from None
        if value is not None:
            _oid(value, self._store._oid_length, "invalid_default_remote_head")
        return value

    def _observe_task_view(
        self,
        *,
        pr_number: int | None,
        operation: str,
        require_task_head: str | None = None,
        expected_pull: PullFacts | None = None,
        require_no_disposition: bool = True,
    ) -> tuple[dict[str, Any], bytes]:
        def github_reader(request: task_view.GitHubPullRequestRequest) -> object:
            if type(request) is not task_view.GitHubPullRequestRequest or request.number != pr_number:
                raise TaskIntegrationError("github_reader_binding_mismatch", operation=operation)
            pull = self._pull(request.number, operation)
            if expected_pull is not None and pull != expected_pull:
                raise TaskIntegrationError("pull_request_observation_changed", operation=operation)
            try:
                checkpoint = self._checkpoint_reader(request)
            except Exception:
                raise TaskIntegrationError("checkpoint_observation_unavailable", operation=operation) from None
            previous = _checkpoint_previous(checkpoint, operation)
            return task_view.GitHubObservation(
                task_view.GitHubPullRequestFacts(
                    pull.base_repository,
                    pull.head_repository,
                    pull.number,
                    pull.state,
                    pull.draft,
                    pull.base_ref,
                    pull.head_ref,
                    pull.head_oid,
                ),
                previous,
            )

        try:
            view = task_view.observe_live_task(
                self._store,
                task=self._task,
                branch_ref=self._branch_ref,
                base_revision=self._base_revision,
                default_branch_ref=self._default_branch_ref,
                observe_remote_head=True,
                pr_number=pr_number,
                github_reader=None if pr_number is None else github_reader,
            )
            raw = _canonical(view)
            detached = json.loads(raw.decode("utf-8", errors="strict"))
            task_view.encode_snapshot(
                self._repository, self._task, detached["subject"], "turn-end", detached,
            )
        except TaskIntegrationError:
            raise
        except Exception:
            raise TaskIntegrationError("live_task_observation_failed", operation=operation) from None
        if type(detached) is not dict:
            raise TaskIntegrationError("invalid_live_task_view", operation=operation)
        try:
            authority = detached["authority"]
            git = detached["git"]
            expected_subject = detached["subject"]
            if (
                detached["repository"] != self._repository or detached["task"] != self._task
                or detached["branch_ref"] != self._branch_ref
                or git["head"] != expected_subject or git["branch_ref"] != self._branch_ref
                or git["branch_matches_task"] is not True
                or authority["state"] != "observed"
                or authority["base_revision"] != self._base_revision
                or authority["branch_ref"] != self._branch_ref
                or (require_no_disposition and authority["disposition"] is not None)
                or type(authority["record_id"]) is not str
                or type(authority["contract_id"]) is not str
            ):
                raise TaskIntegrationError("task_authority_binding_mismatch", operation=operation)
            _metadata_id(authority["record_id"])
            _metadata_id(authority["contract_id"])
            if require_task_head is not None and expected_subject != require_task_head:
                raise TaskIntegrationError("task_head_mismatch", operation=operation)
            if pr_number is not None:
                pull_projection = detached["github"]["pull_request"]
                if (
                    detached["github"]["state"] != "observed"
                    or pull_projection["number"] != pr_number
                    or pull_projection["head_oid"] != expected_subject
                ):
                    raise TaskIntegrationError("pull_request_observation_unavailable", operation=operation)
            remote = git["remote_head"]
            if remote["state"] != "observed" or remote["head_oid"] is None:
                raise TaskIntegrationError("task_remote_head_unavailable", operation=operation)
        except TaskIntegrationError:
            raise
        except (KeyError, TypeError, ValueError):
            raise TaskIntegrationError("invalid_live_task_view", operation=operation) from None
        return detached, raw

    def _subject_from(
        self,
        repository: RepositoryFacts,
        pull: PullFacts,
        plan: MergePlanFacts,
        view: dict[str, Any],
        method: str,
        ready_reference_id: str,
    ) -> IntegrationSubject:
        authority = view["authority"]
        subject = IntegrationSubject(
            SCHEMA_VERSION,
            self._repository,
            repository.repository_id,
            self._task,
            pull.number,
            self._branch_ref,
            pull.head_oid,
            self._default_branch_ref,
            pull.base_oid,
            self._default_branch_ref,
            authority["record_id"],
            authority["contract_id"],
            method,
            plan.expected_tree_oid,
            plan.plan_ref,
            ready_reference_id,
        )
        _validate_subject(subject)
        return subject

    def _capture_open_state(
        self,
        number: int,
        method: str,
        ready_reference_id: str,
        *,
        operation: str,
    ) -> _Capture:
        self._assert_common_identity(operation)
        repository = self._read_repository(operation)
        if method not in _MERGE_METHODS or method not in repository.allowed_merge_methods:
            raise TaskIntegrationError("unsupported_merge_method", operation=operation)
        ready_id = _metadata_id(ready_reference_id, "invalid_ready_reference_id")
        principal = self._principal(operation)
        task_local = self._task_local(operation, require_clean=True)
        task_remote = self._task_remote_head(operation)
        if task_remote != task_local.head:
            raise TaskIntegrationError("task_remote_head_mismatch", operation=operation)
        pull = self._pull(number, operation)
        if pull.state == "merged":
            raise TaskIntegrationError("already_merged_requires_recovery", operation=operation)
        if pull.state != "open" or pull.draft:
            raise TaskIntegrationError("pull_request_not_mergeable", operation=operation)
        if (
            pull.head_ref != self._branch_ref[len("refs/heads/"):]
            or pull.base_ref != self._default_branch_ref[len("refs/heads/"):]
            or pull.head_oid != task_local.head
        ):
            raise TaskIntegrationError("pull_request_identity_conflict", operation=operation)
        default_head = self._default_head(operation)
        if pull.base_oid != default_head:
            raise TaskIntegrationError("pull_request_base_stale", operation=operation)
        if self._github_ancestor(pull.base_oid, pull.head_oid, operation) is not True:
            raise TaskIntegrationError("task_head_not_based_on_pr_base", operation=operation)
        plan = self._plan(number, method, pull, operation)
        default_local = self._default_facts(operation)
        default_remote = self._default_remote_head(operation)
        if (
            default_remote != default_head
            or getattr(default_local, "clean") is not True
        ):
            raise TaskIntegrationError("default_branch_facts_conflict", operation=operation)
        view, view_bytes = self._observe_task_view(
            pr_number=number, operation=operation, require_task_head=task_local.head,
            expected_pull=pull,
        )
        if view["authority"]["record_id"] == "" or view["subject"] != task_local.head:
            raise TaskIntegrationError("task_authority_binding_mismatch", operation=operation)
        if view["git"]["remote_head"].get("head_oid") != task_local.head:
            raise TaskIntegrationError("task_remote_head_mismatch", operation=operation)
        projection = view["github"]["pull_request"]
        if (
            projection["base_repository"] != pull.base_repository
            or projection["head_repository"] != pull.head_repository
            or projection["base_ref"] != pull.base_ref
            or projection["head_ref"] != pull.head_ref
            or projection["head_oid"] != pull.head_oid
        ):
            raise TaskIntegrationError("pull_request_observation_changed", operation=operation)
        subject = self._subject_from(repository, pull, plan, view, method, ready_id)
        capabilities = self._qualified_capabilities(operation)
        return _Capture(
            subject, repository, pull, plan, principal, task_local, task_remote,
            default_local, default_remote, default_head, view, view_bytes, capabilities,
        )

    def capture_subject(
        self,
        pr_number: int,
        method: str,
        ready_reference_id: str,
    ) -> IntegrationSubject:
        """Read and freeze an exact proposed subject; this grants no approval."""
        if type(pr_number) is not int or not 1 <= pr_number <= 2**63 - 1:
            raise TaskIntegrationError("invalid_pr_number")
        if type(method) is not str or method not in _MERGE_METHODS:
            raise TaskIntegrationError("unsupported_merge_method")
        try:
            initial = self._capture_open_state(
                pr_number, method, ready_reference_id, operation="capture_subject",
            )
            # The proposal is only returned after an independent full
            # re-observation.  An ephemeral merge-plan reference or any
            # material PR/Task movement invalidates this capture; it is never
            # renewed behind an existing ready or Human reference.
            self._same_capture(initial, "capture_subject")
            return initial.subject
        except TaskIntegrationError:
            raise
        except Exception:
            raise TaskIntegrationError("capture_subject_failed") from None

    def _same_capture(self, expected: _Capture, operation: str) -> _Capture:
        try:
            current = self._capture_open_state(
                expected.subject.pull_request,
                expected.subject.merge_method,
                expected.subject.ready_reference_id,
                operation=operation,
            )
        except TaskIntegrationError:
            raise
        stable_fields = (
            "subject", "repository", "pull", "plan", "principal", "task_local",
            "task_remote_head", "default_local", "default_remote_head", "default_head",
            "capabilities",
        )
        try:
            # #217 may append unrelated immutable objects between reads.  Bind
            # the current #191 Record and Contract IDs and every other view fact,
            # while deliberately not treating its metadata tip as authority.
            view_matches = _stable_view_bytes(expected.task_view) == _stable_view_bytes(current.task_view)
        except Exception:
            view_matches = False
        if not view_matches or any(
            getattr(current, field) != getattr(expected, field) for field in stable_fields
        ):
            raise TaskIntegrationError("integration_subject_changed", operation=operation)
        return current

    def _validate_ready(
        self,
        reference: ReadyReference,
        subject: IntegrationSubject,
        mode: str,
        operation: str,
    ) -> ReadyBinding:
        if mode not in {"current", "historical"}:
            raise TaskIntegrationError("invalid_validation_mode", operation=operation)
        value = self._call(
            self._ready_validator, operation, "ready_validation_unavailable", reference, subject, mode,
        )
        try:
            return _ready_binding(value, reference, subject)
        except TaskIntegrationError as error:
            raise TaskIntegrationError(error.code, operation=operation) from None

    def _validate_human(
        self,
        reference: HumanApprovalReference,
        subject: IntegrationSubject,
        mode: str,
        operation: str,
    ) -> HumanApprovalBinding:
        if mode not in {"current", "historical"}:
            raise TaskIntegrationError("invalid_validation_mode", operation=operation)
        value = self._call(
            self._human_validator, operation, "human_approval_validation_unavailable",
            reference, subject, mode,
        )
        try:
            return _human_binding(value, reference, subject)
        except TaskIntegrationError as error:
            raise TaskIntegrationError(error.code, operation=operation) from None

    def _diagnose(
        self,
        operation: str,
        view: dict[str, Any],
        parameters: dict[str, Any],
        *,
        require_human_owned: bool,
    ) -> tuple[dict[str, Any], bytes]:
        engine = self._engines.get(operation)
        if engine is None:
            raise TaskIntegrationError("operation_prerequisites_unavailable", operation="merge" if operation == "integration.merge" else "reconcile")
        try:
            detached = json.loads(_canonical(view).decode("utf-8", errors="strict"))
            params = json.loads(_canonical(parameters).decode("utf-8", errors="strict"))
            request = prerequisites.OperationRequest(
                self._repository,
                self._task,
                self._branch_ref,
                detached["subject"],
                operation,
                params,
            )
            request_bytes = _canonical({
                "schema_version": prerequisites.SCHEMA_VERSION,
                "repository": self._repository,
                "task": self._task,
                "branch_ref": self._branch_ref,
                "subject": detached["subject"],
                "operation": operation,
                "parameters": params,
            })
            result = engine.diagnose(request, detached)
            encoded = prerequisites.encode_diagnosis(result)
        except Exception:
            raise TaskIntegrationError("operation_prerequisite_check_failed", operation="merge" if operation == "integration.merge" else "reconcile") from None
        expected_request_id = hashlib.sha256(_REQUEST_DOMAIN + request_bytes).hexdigest()
        view_bytes = _canonical(detached)
        expected_view_id = hashlib.sha256(_VIEW_DOMAIN + view_bytes).hexdigest()
        if (
            type(result) is not dict
            or result.get("repository") != self._repository
            or result.get("task") != self._task
            or result.get("branch_ref") != self._branch_ref
            or result.get("subject") != detached["subject"]
            or result.get("operation") != operation
            or result.get("policy_operation") != operation
            or result.get("request_id") != expected_request_id
            or result.get("view_id") != expected_view_id
            or type(result.get("policy_id")) is not str
            or not _HEX_64.fullmatch(result["policy_id"])
            or result.get("result") != "PREREQUISITES_SATISFIED"
        ):
            raise TaskIntegrationError(
                "operation_prerequisites_not_satisfied",
                operation="merge" if operation == "integration.merge" else "reconcile",
                diagnosis_bytes=encoded,
            )
        if require_human_owned and (
            result.get("authority_mode") != "human_owned"
            or result.get("human_authority_required") is not True
        ):
            raise TaskIntegrationError(
                "human_owned_prerequisite_policy_required",
                operation="merge",
                diagnosis_bytes=encoded,
            )
        if not require_human_owned and (
            result.get("authority_mode") == "human_owned"
            or result.get("human_authority_required") is not False
        ):
            raise TaskIntegrationError(
                "unexpected_human_reconciliation_gate",
                operation="reconcile",
                diagnosis_bytes=encoded,
            )
        return result, encoded

    @staticmethod
    def _merge_parameters(subject: IntegrationSubject, approval: HumanApprovalReference) -> dict[str, Any]:
        return {
            "subject_id": subject.subject_id,
            "ready_reference_id": subject.ready_reference_id,
            "approval_reference_id": approval.reference_id,
            "merge_method": subject.merge_method,
            "expected_tree_oid": subject.expected_merge_tree_oid,
            "plan_ref": subject.plan_ref,
            "record_id": subject.record_id,
            "contract_id": subject.contract_id,
            "pull_request": subject.pull_request,
            "head_oid": subject.head_oid,
            "base_oid": subject.base_oid,
        }

    def _durably_record(self, intent: MergeIntent, operation: str) -> RecordedIntent:
        try:
            result = self._record_intent(intent)
        except Exception:
            raise TaskIntegrationError("merge_intent_record_failed", operation=operation) from None
        try:
            return _recorded_intent(result, intent)
        except TaskIntegrationError as error:
            raise TaskIntegrationError(error.code, operation=operation) from None

    def merge(
        self,
        subject: IntegrationSubject,
        ready_reference: ReadyReference,
        approval_reference: HumanApprovalReference,
    ) -> IntegrationReceipt:
        """Perform at most one server-qualified merge and verify its real result."""
        operation = "merge"
        subject = _validate_subject(subject)
        ready = _ready_reference(ready_reference, subject)
        approval = _approval_reference(approval_reference, subject)
        try:
            _validate_capabilities(self._qualified_capabilities(operation))
            initial = self._capture_open_state(
                subject.pull_request, subject.merge_method, subject.ready_reference_id,
                operation=operation,
            )
            if initial.subject != subject:
                raise TaskIntegrationError("integration_subject_stale", operation=operation)

            ready_binding = self._validate_ready(ready, subject, "current", operation)
            self._same_capture(initial, operation)
            human_binding = self._validate_human(approval, subject, "current", operation)
            self._same_capture(initial, operation)

            parameters = self._merge_parameters(subject, approval)
            diagnosis, _diagnosis_bytes = self._diagnose(
                "integration.merge", initial.task_view, parameters, require_human_owned=True,
            )
            self._same_capture(initial, operation)
            intent = _make_merge_intent(
                subject, ready_binding, human_binding, initial.principal, diagnosis,
            )
            recorded = self._durably_record(intent, operation)

            # Intent storage and every validator are callback boundaries.  Do
            # not let callback acknowledgements substitute for re-observation.
            self._same_capture(initial, operation)
            current_ready = self._validate_ready(ready, subject, "current", operation)
            self._same_capture(initial, operation)
            current_human = self._validate_human(approval, subject, "current", operation)
            self._same_capture(initial, operation)
            if current_ready != ready_binding or current_human != human_binding:
                raise TaskIntegrationError("approval_binding_changed", operation=operation)
            final_diagnosis, _final_diagnosis_bytes = self._diagnose(
                "integration.merge", initial.task_view, parameters, require_human_owned=True,
            )
            if (
                final_diagnosis["request_id"] != diagnosis["request_id"]
                or final_diagnosis["policy_id"] != diagnosis["policy_id"]
                or final_diagnosis["view_id"] != diagnosis["view_id"]
            ):
                raise TaskIntegrationError("operation_context_changed", operation=operation)
            final_ready = self._validate_ready(ready, subject, "current", operation)
            final_human = self._validate_human(approval, subject, "current", operation)
            if final_ready != ready_binding or final_human != human_binding:
                raise TaskIntegrationError("approval_binding_changed", operation=operation)
            self._same_capture(initial, operation)
            if recorded.intent_id != intent.intent_id:
                raise TaskIntegrationError("merge_intent_ack_mismatch", operation=operation)
        except TaskIntegrationError:
            raise
        except Exception:
            raise TaskIntegrationError("merge_precondition_failed", operation=operation) from None

        # A return value or exception from this one mutator is never a receipt.
        # Always read back the exact PR, merge commit, and default graph.
        mutation_error = False
        try:
            self._github.merge_exact(intent)
        except Exception:
            mutation_error = True
        try:
            merge_oid = self._verify_merged(subject, operation)
        except TaskIntegrationError as error:
            if error.code == "merge_not_confirmed" and mutation_error:
                raise TaskIntegrationError("merge_not_confirmed", operation=operation) from None
            raise TaskIntegrationError(
                "merge_outcome_uncertain", operation=operation,
            ) from None
        return _receipt_for(subject, intent, recorded.intent_ref, merge_oid)

    def _verify_merged(self, subject: IntegrationSubject, operation: str) -> str:
        self._assert_common_identity(operation)
        repository = self._read_repository(operation)
        if repository.repository_id != subject.repository_id:
            raise TaskIntegrationError("github_repository_identity_changed", operation=operation)
        pull = self._pull(subject.pull_request, operation)
        if pull.state in {"open", "closed"} or pull.merge_oid is None:
            raise TaskIntegrationError("merge_not_confirmed", operation=operation)
        if (
            pull.state != "merged"
            or pull.base_repository != subject.repository
            or pull.head_repository != subject.repository
            or pull.base_ref != subject.base_ref[len("refs/heads/"):]
            or pull.head_ref != subject.head_ref[len("refs/heads/"):]
            or pull.head_oid != subject.head_oid
            or pull.merge_method != subject.merge_method
        ):
            raise TaskIntegrationError("merged_pull_binding_mismatch", operation=operation)
        merge_oid = _oid(pull.merge_oid, self._store._oid_length)
        commit = self._github_commit(merge_oid, operation)
        expected_parents = (
            (subject.base_oid, subject.head_oid)
            if subject.merge_method == "merge" else (subject.base_oid,)
        )
        if commit.parents != expected_parents or commit.tree != subject.expected_merge_tree_oid:
            raise TaskIntegrationError("merge_commit_binding_mismatch", operation=operation)
        current_default = self._default_head(operation)
        remote_default = self._default_remote_head(operation)
        if remote_default != current_default:
            raise TaskIntegrationError("default_remote_head_mismatch", operation=operation)
        if current_default != merge_oid and self._github_ancestor(merge_oid, current_default, operation) is not True:
            raise TaskIntegrationError("merge_not_reachable_from_default", operation=operation)
        return merge_oid

    def _load_merge_intent(self, intent_ref: str) -> tuple[MergeIntent, RecordedIntent]:
        reference = _reference_text(intent_ref, "invalid_merge_intent_reference")
        try:
            value = self._load_intent(reference)
        except Exception:
            raise TaskIntegrationError("merge_intent_unavailable", operation="recover") from None
        intent = _validate_merge_intent(value)
        subject = _validate_subject(intent.subject)
        if (
            subject.repository != self._repository or subject.task != self._task
            or subject.head_ref != self._branch_ref
            or subject.default_branch_ref != self._default_branch_ref
        ):
            raise TaskIntegrationError("merge_intent_scope_mismatch", operation="recover")
        return intent, RecordedIntent(reference, intent.intent_id)

    def _recover(
        self, intent_ref: str,
    ) -> tuple[IntegrationReceipt, MergeIntent]:
        operation = "recover"
        self._assert_common_identity(operation)
        intent, recorded = self._load_merge_intent(intent_ref)
        subject = intent.subject
        ready_reference = ReadyReference(
            intent.ready_binding.subject_id, intent.ready_binding.reference_id,
        )
        approval_reference = HumanApprovalReference(
            intent.human_binding.subject_id, intent.human_binding.reference_id,
        )
        # Do not even consult historical revocation semantics for an open,
        # closed-unmerged, or wrong-scope PR.  Historical proof mode is only
        # for recovering a merge which this exact recorded intent already owns.
        merge_oid = self._verify_merged(subject, operation)
        # Historical validation authenticates the exact prior proof, including
        # its provenance, but never authorizes another mutation.
        ready = self._validate_ready(ready_reference, subject, "historical", operation)
        human = self._validate_human(approval_reference, subject, "historical", operation)
        if ready != intent.ready_binding or human != intent.human_binding:
            raise TaskIntegrationError("historical_approval_binding_mismatch", operation=operation)
        if self._verify_merged(subject, operation) != merge_oid:
            raise TaskIntegrationError("merge_facts_changed", operation=operation)
        return _receipt_for(subject, intent, recorded.intent_ref, merge_oid), intent

    def recover(self, intent_ref: str) -> IntegrationReceipt:
        """Confirm only a prior exact durable intent; never invoke GitHub merge."""
        try:
            receipt, _intent = self._recover(intent_ref)
            return receipt
        except TaskIntegrationError:
            raise
        except Exception:
            raise TaskIntegrationError("merge_recovery_failed", operation="recover") from None

    def _reconcile_task_context(self, operation: str) -> tuple[_LocalTaskFacts, dict[str, Any], bytes]:
        local = self._task_local(operation, require_clean=False)
        view, data = self._observe_task_view(
            pr_number=None,
            operation=operation,
            require_no_disposition=False,
        )
        if view["subject"] != local.head:
            raise TaskIntegrationError("task_head_mismatch", operation=operation)
        return local, view, data

    def _reconciliation_diagnosis(
        self,
        intent_ref: str,
        merge_receipt: IntegrationReceipt,
        target_head: str,
        task_local: _LocalTaskFacts,
        default_local: object,
        view: dict[str, Any],
    ) -> dict[str, Any]:
        parameters = {
            "merge_intent_ref": intent_ref,
            "merge_intent_id": _metadata_id(merge_receipt.intent_id),
            "merge_oid": merge_receipt.merge_oid,
            "expected_remote_oid": target_head,
            "local_default_head": getattr(default_local, "head"),
            "task_head": task_local.head,
        }
        diagnosis, _encoded = self._diagnose(
            "integration.reconcile", view, parameters, require_human_owned=False,
        )
        return diagnosis

    def reconcile(self, intent_ref: str) -> IntegrationReconciliationReceipt:
        """Fetch a confirmed upstream and fast-forward a clean shared default worktree.

        The caller supplies an intent *reference*, never a self-asserted merge
        receipt.  Recovery authenticates its immutable #195 intent and proves
        that exact merge first.  This operation does not re-consume Human merge
        authority and cannot force, rebase, push, or alter Task metadata.
        """
        operation = "reconcile"
        merge_receipt: IntegrationReceipt | None = None
        merge_intent: MergeIntent | None = None
        try:
            merge_receipt, merge_intent = self._recover(intent_ref)
            if self._record_sync_intent is None:
                raise TaskIntegrationError("sync_intent_capability_unavailable", operation=operation)
            self._assert_common_identity(operation)
            repository = self._read_repository(operation)
            assert merge_intent is not None
            if repository.repository_id != merge_intent.subject.repository_id:
                raise TaskIntegrationError("github_repository_identity_changed", operation=operation)
            current_default = self._default_head(operation)
            remote_head = self._default_remote_head(operation)
            if remote_head != current_default:
                raise TaskIntegrationError("default_remote_head_mismatch", operation=operation)
            if (
                current_default != merge_receipt.merge_oid
                and self._github_ancestor(merge_receipt.merge_oid, current_default, operation) is not True
            ):
                raise TaskIntegrationError("merge_not_reachable_from_default", operation=operation)
            local_default = self._default_facts(operation)
            if getattr(local_default, "clean") is not True:
                raise TaskIntegrationError("default_worktree_not_clean", operation=operation)
            # Do not infer ancestry from the pre-fetch local object database.
            # DefaultGit.reconcile fetches the exact authenticated remote tip,
            # then proves both merge reachability and local fast-forwardability.

            task_local, task_view_value, _task_view_bytes = self._reconcile_task_context(operation)
            diagnosis = self._reconciliation_diagnosis(
                intent_ref, merge_receipt, current_default, task_local, local_default,
                task_view_value,
            )
            # Prerequisite readers are callbacks. Reobserve the Task, upstream,
            # and shared Git identity before the worker is allowed to fetch or
            # install a local fast-forward.
            current_task_local, current_task_view, _current_task_view_bytes = self._reconcile_task_context(operation)
            if (
                current_task_local != task_local
                or _stable_view_bytes(current_task_view) != _stable_view_bytes(task_view_value)
            ):
                raise TaskIntegrationError("task_facts_changed", operation=operation)
            if (
                self._default_head(operation) != current_default
                or self._default_remote_head(operation) != current_default
                or self._default_facts(operation) != local_default
            ):
                raise TaskIntegrationError("default_branch_facts_changed", operation=operation)
            if self._verify_merged(merge_intent.subject, operation) != merge_receipt.merge_oid:
                raise TaskIntegrationError("merge_facts_changed", operation=operation)
            self._assert_common_identity(operation)

            sync_ack: list[RecordedReconciliationIntent] = []
            sync_failure: list[str] = []

            def on_intent(request: object) -> bool:
                """Journal only the exact default fast-forward request."""
                try:
                    if type(request) is self._integration_git.FetchRequest:
                        if (
                            request.repository != self._repository
                            or request.branch_ref != self._default_branch_ref
                            or request.expected_remote != current_default
                            or request.merged_oid != merge_receipt.merge_oid
                        ):
                            sync_failure.append("sync_fetch_request_mismatch")
                            return False
                        return True
                    if type(request) is not self._integration_git.FastForwardRequest:
                        sync_failure.append("invalid_sync_intent_request")
                        return False
                    for field in (
                        "repository", "branch_ref", "worktree", "expected_head", "target_head",
                        "merged_oid", "target_tree", "expected_index_fingerprint",
                        "expected_status_fingerprint", "target_index_fingerprint", "changed_paths",
                    ):
                        if not hasattr(request, field):
                            sync_failure.append("invalid_sync_intent_request")
                            return False
                    if (
                        request.repository != self._repository
                        or request.branch_ref != self._default_branch_ref
                        or request.worktree != local_default.worktree
                        or request.expected_head != local_default.head
                        or request.target_head != current_default
                        or request.merged_oid != merge_receipt.merge_oid
                        or type(request.changed_paths) is not tuple
                        or len(request.changed_paths) > 100_000
                        or any(type(path) is not bytes or not path or len(path) > 16 * 1024
                               for path in request.changed_paths)
                    ):
                        sync_failure.append("sync_fast_forward_binding_mismatch")
                        return False
                    if sum(len(path) for path in request.changed_paths) > 8 * 1024 * 1024 or any(
                        path.startswith(b"/") or b"\0" in path or b"//" in path
                        or any(part in {b"", b".", b"..", b".git"} for part in path.split(b"/"))
                        for path in request.changed_paths
                    ):
                        sync_failure.append("sync_changed_path_scope_invalid")
                        return False
                    _oid(request.target_tree, self._store._oid_length)
                    _metadata_id(request.expected_index_fingerprint)
                    _metadata_id(request.expected_status_fingerprint)
                    _metadata_id(request.target_index_fingerprint)
                    sync_wire = {
                        "schema_version": SCHEMA_VERSION,
                        "repository": self._repository,
                        "task": self._task,
                        "branch_ref": self._default_branch_ref,
                        "merge_intent_ref": _reference_text(intent_ref, "invalid_merge_intent_reference"),
                        "merge_oid": merge_receipt.merge_oid,
                        "expected_remote_oid": current_default,
                        "old_head": local_default.head,
                        "target_head": request.target_head,
                        "target_tree": request.target_tree,
                        "changed_paths": [path.hex() for path in request.changed_paths],
                    }
                    sync_intent = ReconciliationIntent(
                        self._repository,
                        self._task,
                        self._default_branch_ref,
                        sync_wire["merge_intent_ref"],
                        merge_receipt.merge_oid,
                        current_default,
                        local_default.head,
                        request.target_head,
                        request.target_tree,
                        request.changed_paths,
                        hashlib.sha256(_SYNC_INTENT_DOMAIN + _canonical(sync_wire)).hexdigest(),
                    )
                    try:
                        acknowledgement = self._record_sync_intent(sync_intent)  # type: ignore[misc]
                    except Exception:
                        sync_failure.append("sync_intent_record_failed")
                        return False
                    if (
                        type(acknowledgement) is not RecordedReconciliationIntent
                        or type(acknowledgement.intent_ref) is not str
                        or not acknowledgement.intent_ref
                        or len(acknowledgement.intent_ref) > MAX_INTENT_REFERENCE_LENGTH
                        or acknowledgement.intent_id != sync_intent.intent_id
                    ):
                        sync_failure.append("sync_intent_ack_mismatch")
                        return False
                    _reference_text(acknowledgement.intent_ref, "invalid_sync_intent_reference")
                    _metadata_id(acknowledgement.intent_id, "invalid_sync_intent_reference")
                    sync_ack.append(acknowledgement)
                    if (
                        self._verify_merged(merge_intent.subject, operation) != merge_receipt.merge_oid
                        or self._default_head(operation) != current_default
                        or self._default_remote_head(operation) != current_default
                        or self._default_facts(operation) != local_default
                    ):
                        sync_failure.append("sync_facts_changed_before_write")
                        return False
                    task_after_intent, view_after_intent, _view_bytes_after_intent = self._reconcile_task_context(operation)
                    if (
                        task_after_intent != task_local
                        or _stable_view_bytes(view_after_intent) != _stable_view_bytes(task_view_value)
                    ):
                        sync_failure.append("task_facts_changed_before_write")
                        return False
                    self._assert_common_identity(operation)
                    # A literal True is only the local worker protocol ack; the
                    # typed, digest-matching record above is the durable proof.
                    return True
                except TaskIntegrationError as error:
                    sync_failure.append(error.code)
                    return False
                except Exception:
                    sync_failure.append("sync_intent_record_failed")
                    return False

            result: object | None = None
            worker_error = False
            try:
                result = self._default_git.reconcile(
                    merged_oid=merge_receipt.merge_oid,
                    expected_remote_oid=current_default,
                    on_intent=on_intent,
                )
            except Exception:
                worker_error = True
            if sync_failure:
                raise TaskIntegrationError(sync_failure[0], operation=operation)
            if worker_error:
                raise TaskIntegrationError("default_reconciliation_unconfirmed", operation=operation)
            if type(result) is not self._integration_git.DefaultReceipt:
                raise TaskIntegrationError("invalid_default_receipt", operation=operation)
            if current_default != local_default.head and not sync_ack:
                raise TaskIntegrationError("sync_intent_missing", operation=operation)
            if (
                result.repository != self._repository
                or result.branch_ref != self._default_branch_ref
                or result.worktree != local_default.worktree
                or result.old_head != local_default.head
                or result.new_head != current_default
                or result.merged_oid != merge_receipt.merge_oid
            ):
                raise TaskIntegrationError("default_receipt_binding_mismatch", operation=operation)
            result_tree = _oid(result.tree, self._store._oid_length)
            target_facts = self._default_git.commit_facts(current_default)
            if (
                type(target_facts) is not self._integration_git.CommitFacts
                or target_facts.oid != current_default
                or target_facts.tree != result_tree
                or type(target_facts.parents) is not tuple
            ):
                raise TaskIntegrationError("default_receipt_tree_mismatch", operation=operation)
            _oid(target_facts.tree, self._store._oid_length)
            if len(target_facts.parents) > 100:
                raise TaskIntegrationError("invalid_default_commit_facts", operation=operation)
            for parent in target_facts.parents:
                _oid(parent, self._store._oid_length)
            post = self._default_facts(operation)
            post_remote = self._default_remote_head(operation)
            latest_default = self._default_head(operation)
            if (
                post.head != current_default or post.tree != result_tree or post.clean is not True
                or post_remote != latest_default
                or (latest_default != current_default
                    and self._github_ancestor(current_default, latest_default, operation) is not True)
            ):
                raise TaskIntegrationError("default_reconciliation_postcondition_mismatch", operation=operation)
            if self._default_git.is_ancestor(merge_receipt.merge_oid, post.head) is not True:
                raise TaskIntegrationError("merge_not_in_default_history", operation=operation)
            if self._verify_merged(merge_intent.subject, operation) != merge_receipt.merge_oid:
                raise TaskIntegrationError("merge_facts_changed", operation=operation)
            return IntegrationReconciliationReceipt(merge_receipt, result)
        except TaskIntegrationError as error:
            raise TaskIntegrationError(
                error.code, operation=operation, diagnosis_bytes=error.diagnosis_bytes,
                merge_receipt=merge_receipt or error.merge_receipt,
            ) from None
        except Exception:
            raise TaskIntegrationError("reconciliation_unavailable", operation=operation,
                                      merge_receipt=merge_receipt) from None


__all__ = [
    "GitHubCommitFacts",
    "GitHubTransport",
    "CheckpointObservation",
    "CheckpointReader",
    "HumanApprovalBinding",
    "HumanApprovalReference",
    "IntegrationReceipt",
    "IntegrationReconciliationReceipt",
    "IntegrationSubject",
    "MergeIntent",
    "MergePlanFacts",
    "MergeTransportCapabilities",
    "MergeTransportPermit",
    "MergeTransportQualification",
    "MergeTransportQualificationRequest",
    "PullFacts",
    "ReadyBinding",
    "ReadyReference",
    "RecordedIntent",
    "RecordedReconciliationIntent",
    "ReconciliationIntent",
    "RepositoryFacts",
    "TaskIntegration",
    "TaskIntegrationError",
    "qualify_merge_transport",
]
