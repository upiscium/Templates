"""Host-gated, local turn orchestration for Agent Core v4.

This facade coordinates the pure ``turn_work`` state machine with facts and
capabilities owned by the existing v4 modules.  It does not create a
TaskRecord, infer a workflow phase, execute a command, choose a model, or make a
Git/metadata/Task write itself.  The only execution seam accepts an opaque
registered-worktree reference and a typed request; production hosts must
install a qualified adapter which enforces the requested role, output and
time budgets, and no-delegation rules.  No such production process adapter is
installed by this module.

The Python type checks here are protocol checks between trusted in-process
components, not a sandbox or a cryptographic boundary.  In particular, path
scopes are descriptive input to the qualified adapter, not filesystem ACLs.
An implementation unit may make product edits, but its context remains the
historical exact H/tree/Task authority.  A later turn must obtain fresh facts;
the old context is never promoted to a new subject.

The control plane has one serialized owner. This facade is not a threaded
executor; callers must not concurrently mutate/discard its coordinator while a
host dispatch is active.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import evidence as evidence_module
import metadata_codec as codec
import operation_prerequisites as prerequisites_module
import task_collaboration
import task_record
import task_view
from metadata_ref import MetadataStore
import turn_work as work


SCHEMA_VERSION = 1
MAX_VIEW_BYTES = codec.MAX_OBJECT_BYTES
MAX_VIEW_NODES = 100_000
MAX_SUMMARY_BYTES = 4 * 1024
MAX_FINDINGS = 256
MAX_FINDING_BYTES = 4 * 1024
MAX_CHECKS = 64
MAX_ARTIFACT_REFERENCE_BYTES = 1024
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z", re.ASCII)
_CLASS_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,127}\Z", re.ASCII)
_UNIT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)
_ROLE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}\Z", re.ASCII)
_ARTIFACT_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,1023}\Z", re.ASCII)
_SAFE_FAILURE = frozenset({
    "turn_context_unavailable", "turn_context_changed", "unit_binding_mismatch",
    "prerequisites_unavailable", "prerequisites_not_satisfied",
    "execution_authorization_unavailable", "execution_authorization_denied",
    "worktree_unavailable", "worktree_binding_mismatch", "dispatch_unavailable",
    "provider_failure", "invalid_dispatch_response", "invalid_leaf_result",
    "invalid_escalation_resolution",
    "invalid_decision_request", "invalid_mechanical_decision", "mechanical_decision_unavailable",
    "resource_limit_exceeded", "changed_verification_subject", "verification_unavailable",
    "verification_subject_mismatch", "verification_policy_mismatch", "verification_failed",
    "evidence_authorization_denied", "evidence_publication_unavailable",
    "evidence_publication_unconfirmed", "snapshot_unavailable",
    "snapshot_binding_mismatch", "checkpoint_unavailable", "checkpoint_binding_mismatch",
    "checkpoint_confirmation_required", "invalid_checkpoint_confirmation",
    "checkpoint_observation_required", "invalid_checkpoint_observer",
    "checkpoint_pr_pull_unavailable",
    "human_direction_required", "operational_approval_denied",
})
_VIEW_DOMAIN = b"agentcore-turn-view/v1\n"
_VERIFICATION_KIND = "agentcore.turn-observation/v1"
_PROJECT_CHECKS = frozenset({
    "project::check", "project::lint", "project::test", "project::typecheck",
    "project::build", "project::format-check",
})


class TurnOrchestrationError(ValueError):
    """Safe bounded error code; adapter detail and credentials are omitted."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or not _IDENTIFIER.fullmatch(code):
            code = "turn_orchestration_error"
        self.code = code
        super().__init__(code)


def _canonical(value: object, *, maximum: int = MAX_VIEW_BYTES) -> bytes:
    """Bound/canonicalize JSON before making a detached view or request."""
    nodes = [MAX_VIEW_NODES]
    remaining = [maximum]

    def charge(size: int) -> None:
        remaining[0] -= size
        if remaining[0] < 0:
            raise TurnOrchestrationError("task_view_size_limit")

    def inspect(item: object, depth: int = 0) -> None:
        nodes[0] -= 1
        if nodes[0] < 0 or depth > codec.MAX_JSON_DEPTH:
            raise TurnOrchestrationError("invalid_task_view")
        item_type = type(item)
        if item is None or item_type in {bool, int}:
            if item_type is int and not -(2**63) <= item <= 2**63 - 1:
                raise TurnOrchestrationError("invalid_task_view")
            charge(4 if item is None or item is True else 5 if item is False else len(str(item)))
            return
        if item_type is str:
            if len(item) > maximum:
                raise TurnOrchestrationError("task_view_size_limit")
            try:
                item.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise TurnOrchestrationError("invalid_task_view") from None
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise TurnOrchestrationError("invalid_task_view")
            try:
                escaped = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, UnicodeEncodeError):
                raise TurnOrchestrationError("invalid_task_view") from None
            charge(len(escaped))
            return
        if item_type is list or item_type is tuple:
            charge(2 + max(0, len(item) - 1))
            for child in item:
                inspect(child, depth + 1)
            return
        if item_type is dict:
            charge(2 + max(0, len(item) - 1) + len(item))
            for key, child in item.items():
                if type(key) is not str:
                    raise TurnOrchestrationError("invalid_task_view")
                inspect(key, depth + 1)
                inspect(child, depth + 1)
            return
        raise TurnOrchestrationError("invalid_task_view")

    inspect(value)
    try:
        data = json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise TurnOrchestrationError("invalid_task_view") from None
    if len(data) > maximum:
        raise TurnOrchestrationError("task_view_size_limit")
    return data


def _detached(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise TurnOrchestrationError("invalid_task_view") from None


def _bounded_text(value: object, maximum: int, code: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value.strip()):
        raise TurnOrchestrationError(code)
    if len(value) > maximum:
        raise TurnOrchestrationError(code)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise TurnOrchestrationError(code) from None
    if len(encoded) > maximum or any(ord(char) < 0x20 and char not in "\n\t" for char in value):
        raise TurnOrchestrationError(code)
    if any(ord(char) == 0x7F for char in value):
        raise TurnOrchestrationError(code)
    return value


def _oid(value: object, code: str) -> str:
    if type(value) is not str or not _OID.fullmatch(value):
        raise TurnOrchestrationError(code)
    return value


def _metadata_id(value: object, code: str) -> str:
    if type(value) is not str or not _HEX_64.fullmatch(value):
        raise TurnOrchestrationError(code)
    return value


def _check_hash(value: object, code: str) -> str:
    return _metadata_id(value, code)


@dataclass(frozen=True)
class ExactSubject:
    """Strong host observation binding a Task's exact clean checkout.

    A production reader must certify ignored files, the index, worktree,
    untracked content and any other relevant local state.  Task View's dirty
    counts alone are deliberately insufficient.  The two SHA-256 fingerprints
    are opaque host-owned digests; this facade checks identity and equality,
    not how a host produced them.
    """

    repository: str
    task: str
    branch_ref: str
    head: str
    tree: str
    record_id: str
    contract_id: str
    clean: bool
    index_fingerprint: str
    status_fingerprint: str


@dataclass(frozen=True)
class RegisteredWorktreeReference:
    """Opaque owner-issued token.  It contains no path, PID, or raw command."""

    repository: str
    task: str
    branch_ref: str
    token: str


@dataclass(frozen=True)
class NativeInspectionRequest:
    """Request native read-only Git inspection of the exact task/base pair.

    A qualified adapter should inspect Git objects and refs directly.  It must
    not substitute an agent-provided rendered diff or arbitrary revision.
    """

    repository: str
    task: str
    branch_ref: str
    base_revision: str
    head: str
    tree: str
    read_scope: tuple[str, ...]


@dataclass(frozen=True)
class DispatchBinding:
    repository: str
    task: str
    branch_ref: str
    head: str
    tree: str
    record_id: str
    contract_id: str
    view_id: str
    unit_id: str
    role: str
    provider: str
    model: str
    executor_ref: str
    worktree_token: str


@dataclass(frozen=True)
class DispatchRequest:
    """Typed bounded request passed only to the installed host adapter."""

    binding: DispatchBinding
    context: Any
    subject: ExactSubject
    objective: str
    read_scope: tuple[str, ...]
    edit_scope: tuple[str, ...]
    constraints: tuple[str, ...]
    dependencies: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    max_seconds: int
    max_output_bytes: int
    max_context_bytes: int
    inspection: NativeInspectionRequest | None


@dataclass(frozen=True)
class WorktreeRegistration:
    """Resolver result bound to the exact registered local worktree/subject."""

    registration_id: str
    repository: str
    task: str
    branch_ref: str
    head: str
    tree: str
    record_id: str
    contract_id: str
    view_id: str
    unit_id: str
    role: str
    provider: str
    model: str
    executor_ref: str
    worktree_token: str


@dataclass(frozen=True)
class ExecutedCheck:
    """One host-executed registered readonly project check."""

    check_id: str
    command: str
    state: str
    exit_code: int | None
    duration_ms: int
    output_sha256: str
    artifact_reference: str | None = None


@dataclass(frozen=True)
class ReviewFinding:
    """Bounded exact source observation, not an authorization or disposition."""

    finding_id: str
    severity: str
    location: str
    observation: str


@dataclass(frozen=True)
class VerificationObservation:
    """Trusted adapter observation kept separate from LeafResult prose."""

    kind: str
    repository: str
    task: str
    branch_ref: str
    head: str
    tree: str
    record_id: str
    contract_id: str
    view_id: str
    checks: tuple[ExecutedCheck, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    review_summary: str = ""


@dataclass(frozen=True)
class EditObservation:
    """Trusted host custody observation for one general-role edit.

    ``changed_paths`` is independently derived by the qualified adapter after
    it has enforced filesystem containment (including aliases and links).
    This value is a structured record, not proof that the adapter is honest.
    """

    binding: DispatchBinding
    before: ExactSubject
    after: ExactSubject
    changed_paths: tuple[str, ...]
    confinement_reference: str


@dataclass(frozen=True)
class DispatchResponse:
    """Exact echo plus result and optional capability-owned observation.

    A production adapter must derive ``observation`` from host-executed,
    closed-registry checks or independently validated source findings and
    ``edit_observation`` from its own before/after custody check. It must never
    deserialize either field from leaf prose/result. This in-process dataclass
    is not itself an attestation or cryptographic seal.
    """

    binding: DispatchBinding
    context: Any
    result: Any
    observation: VerificationObservation | None = None
    edit_observation: EditObservation | None = None


@dataclass(frozen=True)
class ProviderFailure:
    """Sanitized failure bound to the exact attempted provider and ticket."""

    unit_id: str
    role: str
    provider: str
    model: str
    exception_class: str
    context: Any
    failure_code: str = "provider_failure"


@dataclass(frozen=True)
class QualifiedHostDispatcher:
    """Trusted adapter installation contract; not a runtime sandbox claim.

    A production installation must attest as a trusted host contract that it
    confines the registered worktree root despite symlinks/hardlinks/mounts,
    disallows writes to Git/admin state and recursive child worktrees,
    binds the configured role/provider/model, and enforces no delegation,
    readonly mode, wall time, output and context budgets. These booleans are
    installation assertions, not runtime attestation or a Python sandbox.
    """

    resolve: Callable[[RegisteredWorktreeReference, DispatchRequest], object]
    dispatch: Callable[[WorktreeRegistration, DispatchRequest], object]
    qualification_id: str
    no_delegation_enforced: bool
    readonly_enforced: bool
    time_budget_enforced: bool
    output_budget_enforced: bool
    required_check_ids: tuple[str, ...] = ()
    scope_enforced: bool = False
    subject_custody_enforced: bool = False


@dataclass(frozen=True)
class DispatchAuthorizationRequest:
    binding: DispatchBinding
    context: Any
    readonly: bool
    prerequisite_digest: str | None
    request: DispatchRequest
    request_digest: str


@dataclass(frozen=True)
class EvidenceAuthorizationRequest:
    repository: str
    task: str
    subject: str
    evidence_id: str
    kind: str
    view_id: str


@dataclass(frozen=True)
class EvidenceReference:
    metadata_commit: str
    evidence_id: str
    subject: str


@dataclass(frozen=True)
class EvidencePromotionRequest:
    repository: str
    task: str
    subject: str
    evidence_id: str
    canonical_bytes: bytes
    kind: str


@dataclass(frozen=True)
class RecordedEvidence:
    metadata_commit: str
    evidence_id: str
    subject: str


@dataclass(frozen=True)
class OperationalResolutionRequest:
    binding: DispatchBinding
    request: Any


@dataclass(frozen=True)
class OperationalResolution:
    disposition: str
    binding: DispatchBinding
    request: Any


@dataclass(frozen=True)
class MechanicalDecisionResolutionRequest:
    binding: DispatchBinding
    decision: Any


@dataclass(frozen=True)
class MechanicalDecisionResolution:
    binding: DispatchBinding
    decision: Any
    selected_option: str
    authority_reference: str


@dataclass(frozen=True)
class UnitOutcomeDescriptor:
    unit_id: str
    role: str
    state: str
    outcome: str | None
    reason_code: str


@dataclass(frozen=True)
class CheckpointRequest:
    repository: str
    task: str
    branch_ref: str
    subject: str
    record_id: str
    contract_id: str
    summary: str
    unresolved: tuple[UnitOutcomeDescriptor, ...]
    evidence: tuple[EvidenceReference, ...]
    snapshot: task_view.SnapshotPublication
    pull_request: int


@dataclass(frozen=True)
class HandoffReport:
    """Ephemeral concise handoff; durable sources remain owner evidence."""

    repository: str
    task: str
    branch_ref: str
    subject: str
    tree: str
    record_id: str
    contract_id: str
    summary: str
    unresolved: tuple[UnitOutcomeDescriptor, ...]
    evidence: tuple[EvidenceReference, ...]
    snapshot: task_view.SnapshotPublication
    checkpoint: task_collaboration.CollaborationReceipt
    checkpoint_confirmation: ConfirmedCheckpoint


@dataclass(frozen=True)
class ConfirmedCheckpoint:
    """Owner-reader confirmation of an exact #194 receipt.

    Production reader qualification includes authentication of the live PR,
    comment owner and numeric principal, and validation of the exact snapshot
    graph/metadata binding. This typed echo alone is not authenticated evidence.
    """

    receipt: task_collaboration.CollaborationReceipt
    principal_id: int
    observation_reference: str


def _validate_role(role: object) -> None:
    if type(role) is not work.LeafRole:
        raise TurnOrchestrationError("invalid_role_registry")
    _bounded_text(role.provider, 256, "invalid_role_registry")
    _bounded_text(role.model, 256, "invalid_role_registry")
    _bounded_text(role.executor_ref, 256, "invalid_role_registry")
    if (
        type(role.role) is not str or not _IDENTIFIER.fullmatch(role.role)
        or type(role.read_only) is not bool
        or type(role.may_delegate) is not bool or role.may_delegate is not False
    ):
        raise TurnOrchestrationError("invalid_role_registry")


def _validate_reference(reference: object) -> RegisteredWorktreeReference:
    if type(reference) is not RegisteredWorktreeReference:
        raise TurnOrchestrationError("registered_worktree_required")
    try:
        codec.encode_object("evidence", reference.repository, reference.task, "0" * 40, {})
        task_record.validate_branch_ref(reference.branch_ref)
    except (codec.MetadataCodecError, task_record.TaskRecordError, TypeError, ValueError):
        raise TurnOrchestrationError("registered_worktree_required") from None
    _bounded_text(reference.token, 256, "registered_worktree_required")
    return reference


class TurnOrchestrator:
    """Build and execute one host-bound turn over pure ``turn_work`` state.

    ``observe_view`` must return a current owner-validated #144 projection;
    ``observe_subject`` must be a stronger host reader which fingerprints the
    exact checkout (including ignored/untracked/index state).  These callbacks
    are trusted host capabilities, not agent-configurable paths or commands.
    The independent authorization callback receives the full objective,
    requested scopes, constraints and finite budgets. It must qualify those
    exact scopes against the actual registered-root filesystem; the pure
    coordinator's lexical checks are not permission.
    """

    def __init__(
        self,
        repository: str,
        task: str,
        roles: tuple[Any, ...],
        *,
        observe_view: Callable[[], object],
        observe_subject: Callable[[], object],
        dispatch_capability: QualifiedHostDispatcher | None,
        worktree_reference: RegisteredWorktreeReference,
        prerequisites: prerequisites_module.OperationPrerequisites | None = None,
        authorizer: Callable[[object], object],
        evidence_store: MetadataStore | None = None,
        publish_evidence: Callable[[EvidencePromotionRequest], object] | None = None,
        snapshots: task_view.TaskViewSnapshots | None = None,
        checkpoint_writer: Callable[[CheckpointRequest], object] | None = None,
        checkpoint_reader: Callable[[task_collaboration.CollaborationReceipt], object] | None = None,
        checkpoint_observer: Callable[[task_view.GitHubPullRequestRequest], object] | None = None,
        escalation_policy: Callable[[OperationalResolutionRequest], object] | None = None,
        decision_policy: Callable[[MechanicalDecisionResolutionRequest], object] | None = None,
    ) -> None:
        try:
            codec.encode_object("evidence", repository, task, "0" * 40, {})
        except (codec.MetadataCodecError, TypeError, ValueError):
            raise TurnOrchestrationError("invalid_task_identity") from None
        if not callable(observe_view) or not callable(observe_subject) or not callable(authorizer):
            raise TurnOrchestrationError("host_observation_capability_required")
        if type(roles) is not tuple or not roles or len(roles) > len(work.LEAF_ROLES):
            raise TurnOrchestrationError("invalid_role_registry")
        for role in roles:
            _validate_role(role)
        if len({role.role for role in roles}) != len(roles):
            raise TurnOrchestrationError("duplicate_role")
        if any(role.role not in work.LEAF_ROLES for role in roles):
            raise TurnOrchestrationError("invalid_role_registry")
        reference = _validate_reference(worktree_reference)
        if (reference.repository, reference.task) != (repository, task):
            raise TurnOrchestrationError("registered_worktree_binding_mismatch")
        if prerequisites is not None and type(prerequisites) is not prerequisites_module.OperationPrerequisites:
            raise TurnOrchestrationError("invalid_prerequisites")
        if dispatch_capability is not None:
            if (
                type(dispatch_capability) is not QualifiedHostDispatcher
                or not all(type(getattr(dispatch_capability, field)) is bool and getattr(dispatch_capability, field) is True
                           for field in ("no_delegation_enforced", "readonly_enforced", "time_budget_enforced", "output_budget_enforced"))
                or not callable(dispatch_capability.resolve)
                or not callable(dispatch_capability.dispatch)
                or type(dispatch_capability.qualification_id) is not str
                or not _ROLE_TOKEN.fullmatch(dispatch_capability.qualification_id)
                or type(dispatch_capability.required_check_ids) is not tuple
                or dispatch_capability.scope_enforced is not True
                or dispatch_capability.subject_custody_enforced is not True
            ):
                raise TurnOrchestrationError("unqualified_dispatch_capability")
            if len(dispatch_capability.required_check_ids) > MAX_CHECKS:
                raise TurnOrchestrationError("invalid_check_policy")
            if any(type(item) is not str or item not in _PROJECT_CHECKS for item in dispatch_capability.required_check_ids):
                raise TurnOrchestrationError("invalid_check_policy")
            if len(set(dispatch_capability.required_check_ids)) != len(dispatch_capability.required_check_ids):
                raise TurnOrchestrationError("invalid_check_policy")
            if any(role.role == "verifier" for role in roles) and not dispatch_capability.required_check_ids:
                raise TurnOrchestrationError("invalid_check_policy")
        if evidence_store is not None:
            if type(evidence_store) is not MetadataStore or evidence_store.repository != repository:
                raise TurnOrchestrationError("invalid_evidence_store")
        if publish_evidence is not None and not callable(publish_evidence):
            raise TurnOrchestrationError("invalid_evidence_publisher")
        if publish_evidence is not None and evidence_store is None:
            raise TurnOrchestrationError("invalid_evidence_store")
        if snapshots is not None and type(snapshots) is not task_view.TaskViewSnapshots:
            raise TurnOrchestrationError("invalid_snapshot_owner")
        if checkpoint_writer is not None and not callable(checkpoint_writer):
            raise TurnOrchestrationError("invalid_checkpoint_owner")
        if checkpoint_writer is not None and not callable(checkpoint_reader):
            raise TurnOrchestrationError("checkpoint_confirmation_required")
        if checkpoint_reader is not None and not callable(checkpoint_reader):
            raise TurnOrchestrationError("invalid_checkpoint_reader")
        if checkpoint_writer is not None and not callable(checkpoint_observer):
            raise TurnOrchestrationError("checkpoint_observation_required")
        if checkpoint_observer is not None and not callable(checkpoint_observer):
            raise TurnOrchestrationError("invalid_checkpoint_observer")
        if escalation_policy is not None and not callable(escalation_policy):
            raise TurnOrchestrationError("invalid_escalation_policy")
        if decision_policy is not None and not callable(decision_policy):
            raise TurnOrchestrationError("invalid_decision_policy")

        self.repository = repository
        self.task = task
        self.roles = tuple(roles)
        self._role_by_name = {role.role: role for role in roles}
        self._observe_view = observe_view
        self._observe_subject = observe_subject
        self._dispatch_capability = dispatch_capability
        self._worktree_reference = reference
        self._prerequisites = prerequisites
        self._authorizer = authorizer
        self._evidence_store = evidence_store
        self._publish_evidence = publish_evidence
        self._snapshots = snapshots
        self._checkpoint_writer = checkpoint_writer
        self._checkpoint_reader = checkpoint_reader
        self._checkpoint_observer = checkpoint_observer
        self._escalation_policy = escalation_policy
        self._decision_policy = decision_policy
        self._coordinator: Any | None = None
        self._context: Any | None = None
        self._initial_view: dict[str, Any] | None = None
        self._initial_subject: ExactSubject | None = None
        self._invalidated = False
        self._observations: dict[str, VerificationObservation] = {}
        self._dispatch_bindings: dict[str, DispatchBinding] = {}
        self._provider_failures: dict[str, ProviderFailure] = {}
        self._operational_resolutions: dict[str, OperationalResolution] = {}
        self._mechanical_decisions: dict[str, MechanicalDecisionResolution] = {}
        self._running_units: set[str] = set()

    @property
    def context(self) -> Any:
        if self._context is None:
            raise TurnOrchestrationError("turn_not_started")
        return self._context

    @property
    def coordinator(self) -> Any:
        if self._coordinator is None:
            raise TurnOrchestrationError("turn_not_started")
        return self._coordinator

    def _validated_observation(self) -> tuple[dict[str, Any], bytes, str, ExactSubject]:
        try:
            raw_view = self._observe_view()
        except Exception:
            raise TurnOrchestrationError("turn_context_unavailable") from None
        raw = _canonical(raw_view)
        view = _detached(raw)
        if type(view) is not dict:
            raise TurnOrchestrationError("invalid_task_view")
        try:
            task_view._validate_projection(
                view, repository=self.repository, task=self.task, subject=view.get("subject"),
            )
        except Exception:
            raise TurnOrchestrationError("invalid_task_view") from None
        try:
            authority = view["authority"]
            if (
                view["repository"] != self.repository
                or view["task"] != self.task
                or view["branch_ref"] != self._worktree_reference.branch_ref
                or view["git"]["branch_ref"] != view["branch_ref"]
                or view["git"]["branch_matches_task"] is not True
                or authority["branch_ref"] != view["branch_ref"]
                or authority["disposition"] is not None
            ):
                raise ValueError
            _metadata_id(authority["record_id"], "invalid_task_view")
            _metadata_id(authority["contract_id"], "invalid_task_view")
        except (KeyError, TypeError, ValueError):
            raise TurnOrchestrationError("task_authority_binding_mismatch") from None
        try:
            subject = self._observe_subject()
        except Exception:
            raise TurnOrchestrationError("exact_subject_unavailable") from None
        if type(subject) is not ExactSubject:
            raise TurnOrchestrationError("exact_subject_unavailable")
        try:
            _check_hash(subject.index_fingerprint, "invalid_exact_subject")
            _check_hash(subject.status_fingerprint, "invalid_exact_subject")
            if (
                type(subject.clean) is not bool
                or subject.repository != self.repository
                or subject.task != self.task
                or subject.branch_ref != view["branch_ref"]
                or subject.head != view["subject"]
                or subject.head != view["git"]["head"]
                or subject.tree != view["git"]["tree"]
                or subject.record_id != authority["record_id"]
                or subject.contract_id != authority["contract_id"]
                or (subject.clean is True and any(view["git"]["status"].values()))
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise TurnOrchestrationError("exact_subject_binding_mismatch") from None
        view_id = hashlib.sha256(_VIEW_DOMAIN + raw).hexdigest()
        return view, raw, view_id, subject

    def begin_turn(self) -> Any:
        """Discard any prior ephemeral state and capture a new exact turn."""
        if self._running_units:
            raise TurnOrchestrationError("unit_still_running")
        if self._coordinator is not None:
            try:
                self._coordinator.discard()
            except Exception:
                raise TurnOrchestrationError("turn_discard_failed") from None
        self._coordinator = None
        self._context = None
        self._initial_view = None
        self._initial_subject = None
        self._observations.clear()
        self._dispatch_bindings.clear()
        self._provider_failures.clear()
        self._operational_resolutions.clear()
        self._mechanical_decisions.clear()
        self._running_units.clear()
        self._invalidated = True
        view, _view_bytes, view_id, subject = self._validated_observation()
        try:
            context = work.TaskTurnContext(
                repository=self.repository,
                task=self.task,
                branch_ref=view["branch_ref"],
                turn_id=secrets.token_hex(16),
                record_id=view["authority"]["record_id"],
                contract_id=view["authority"]["contract_id"],
                head=view["subject"],
                tree=view["git"]["tree"],
                view_id=view_id,
            )
            coordinator = work.TurnCoordinator(context, self.roles, work.TurnBudget())
        except Exception:
            raise TurnOrchestrationError("turn_context_invalid") from None
        self._context = context
        self._coordinator = coordinator
        self._initial_view = view
        self._initial_subject = subject
        self._invalidated = False
        self._observations = {}
        self._dispatch_bindings = {}
        self._provider_failures = {}
        self._operational_resolutions = {}
        self._mechanical_decisions = {}
        self._running_units = set()
        return coordinator

    def _require_live_turn(self) -> tuple[Any, Any]:
        if self._coordinator is None or self._context is None:
            raise TurnOrchestrationError("turn_not_started")
        if self._invalidated:
            raise TurnOrchestrationError("fresh_turn_required")
        return self._coordinator, self._context

    def add(self, unit: Any) -> Any:
        coordinator, context = self._require_live_turn()
        try:
            if type(unit) is not work.WorkUnit or unit.context != context:
                raise TurnOrchestrationError("unit_binding_mismatch")
            if unit.role.role not in self._role_by_name or unit.role != self._role_by_name[unit.role.role]:
                raise TurnOrchestrationError("unit_binding_mismatch")
            validation = coordinator.validate_unit(unit)
            if validation is False:
                raise TurnOrchestrationError("unit_binding_mismatch")
            return coordinator.add(unit)
        except TurnOrchestrationError:
            raise
        except work.WorkUnitError as error:
            raise TurnOrchestrationError(error.code) from None
        except Exception:
            raise TurnOrchestrationError("unit_binding_mismatch") from None

    def ready(self) -> tuple[Any, ...]:
        coordinator, _context = self._require_live_turn()
        try:
            return coordinator.ready()
        except Exception:
            raise TurnOrchestrationError("coordinator_unavailable") from None

    def result(self, unit_id: str) -> Any | None:
        if self._coordinator is None:
            raise TurnOrchestrationError("turn_not_started")
        coordinator = self._coordinator
        try:
            return coordinator.result(unit_id)
        except Exception:
            raise TurnOrchestrationError("coordinator_unavailable") from None

    def units(self) -> tuple[Any, ...]:
        if self._coordinator is None:
            raise TurnOrchestrationError("turn_not_started")
        coordinator = self._coordinator
        try:
            return coordinator.units()
        except Exception:
            raise TurnOrchestrationError("coordinator_unavailable") from None

    def provider_failure(self, unit_id: str) -> ProviderFailure | None:
        """Return sanitized exact attempted-provider diagnostics, if any."""
        if type(unit_id) is not str or not _UNIT_ID.fullmatch(unit_id):
            raise TurnOrchestrationError("invalid_unit_id")
        return self._provider_failures.get(unit_id)

    def operational_resolution(self, unit_id: str) -> OperationalResolution | None:
        """Read a policy resolution; it never grants execution to this unit."""
        if type(unit_id) is not str or not _UNIT_ID.fullmatch(unit_id):
            raise TurnOrchestrationError("invalid_unit_id")
        return self._operational_resolutions.get(unit_id)

    def mechanical_decision(self, unit_id: str) -> MechanicalDecisionResolution | None:
        """Read a host-selected mechanical option for a separate next unit."""
        if type(unit_id) is not str or not _UNIT_ID.fullmatch(unit_id):
            raise TurnOrchestrationError("invalid_unit_id")
        return self._mechanical_decisions.get(unit_id)

    def _blocked_result(self, unit: Any, code: str, summary: str | None = None) -> Any:
        safe = code if code in _SAFE_FAILURE else "provider_failure"
        return work.LeafResult(
            unit_id=unit.unit_id,
            outcome="BLOCKED",
            summary=_bounded_text(summary or safe, MAX_SUMMARY_BYTES, "invalid_summary"),
            failure_code=safe,
        )

    def _finish(self, coordinator: Any, unit: Any, result: Any) -> Any:
        try:
            validation = coordinator.validate_result(result, unit)
            if validation is False:
                raise ValueError
            coordinator.finish(result)
            return result
        except Exception:
            blocked = self._blocked_result(unit, "invalid_leaf_result")
            try:
                coordinator.validate_result(blocked, unit)
                coordinator.finish(blocked)
                return blocked
            except Exception:
                # The pure coordinator is the owner of lifecycle transitions.
                # If it rejects its own closed failure value, do not claim a
                # result or attempt to rewrite the running unit.
                raise TurnOrchestrationError("coordinator_finish_failed") from None
        finally:
            self._running_units.discard(unit.unit_id)

    def _unit(self, unit_id: str) -> Any:
        if type(unit_id) is not str or not _UNIT_ID.fullmatch(unit_id):
            raise TurnOrchestrationError("invalid_unit_id")
        for unit in self.units():
            if unit.unit_id == unit_id:
                return unit
        raise TurnOrchestrationError("unknown_unit")

    def _registration(self, value: object, binding: DispatchBinding, role: Any) -> WorktreeRegistration:
        if type(value) is not WorktreeRegistration:
            raise TurnOrchestrationError("worktree_binding_mismatch")
        expected = {
            "repository": binding.repository, "task": binding.task,
            "branch_ref": binding.branch_ref, "head": binding.head, "tree": binding.tree,
            "record_id": binding.record_id, "contract_id": binding.contract_id,
            "view_id": binding.view_id, "unit_id": binding.unit_id, "role": role.role,
            "provider": role.provider, "model": role.model,
            "executor_ref": role.executor_ref, "worktree_token": binding.worktree_token,
        }
        if any(getattr(value, field) != exact for field, exact in expected.items()):
            raise TurnOrchestrationError("worktree_binding_mismatch")
        _bounded_text(value.registration_id, 256, "worktree_binding_mismatch")
        return value

    @staticmethod
    def _path_under(prefix: str, path: str) -> bool:
        return prefix == "." or path == prefix or path.startswith(prefix + "/")

    @staticmethod
    def _validate_changed_path(value: object) -> str:
        path = _bounded_text(value, 1024, "verification_subject_mismatch")
        parts = path.split("/")
        forbidden = set("*?[]{}$`'\";|&<>!#~:")
        if (
            path.startswith("/") or path.endswith("/") or "\\" in path
            or any(not part or part in {".", ".."} or part.startswith("-") for part in parts)
            or any(part.lower() in {".git", ".task-state", ".automation"} for part in parts)
            or any(char in forbidden for char in path)
        ):
            raise TurnOrchestrationError("verification_subject_mismatch")
        return path

    @staticmethod
    def _subject_wire(subject: ExactSubject) -> dict[str, Any]:
        return {
            "repository": subject.repository,
            "task": subject.task,
            "branch_ref": subject.branch_ref,
            "head": subject.head,
            "tree": subject.tree,
            "record_id": subject.record_id,
            "contract_id": subject.contract_id,
            "clean": subject.clean,
            "index_fingerprint": subject.index_fingerprint,
            "status_fingerprint": subject.status_fingerprint,
        }

    def _validate_subject_shape(self, subject: object) -> ExactSubject:
        if type(subject) is not ExactSubject:
            raise TurnOrchestrationError("verification_subject_mismatch")
        try:
            if (
                type(subject.clean) is not bool
                or subject.repository != self.repository
                or subject.task != self.task
            ):
                raise ValueError
            _bounded_text(subject.branch_ref, task_view.MAX_BRANCH_REF_LENGTH, "verification_subject_mismatch")
            task_record.validate_branch_ref(subject.branch_ref)
            _oid(subject.head, "verification_subject_mismatch")
            _oid(subject.tree, "verification_subject_mismatch")
            _metadata_id(subject.record_id, "verification_subject_mismatch")
            _metadata_id(subject.contract_id, "verification_subject_mismatch")
            _check_hash(subject.index_fingerprint, "verification_subject_mismatch")
            _check_hash(subject.status_fingerprint, "verification_subject_mismatch")
        except Exception:
            raise TurnOrchestrationError("verification_subject_mismatch") from None
        return subject

    @staticmethod
    def _result_wire(result: Any) -> dict[str, Any]:
        approval = result.approval
        decision = result.decision
        return {
            "unit_id": result.unit_id,
            "outcome": result.outcome,
            "summary": result.summary,
            "outputs": [{"name": item.name, "value": item.value} for item in result.outputs],
            "approval": None if approval is None else {
                "operation_class": approval.operation_class,
                "operation_identity": approval.operation_identity,
                "scope": approval.scope,
                "purpose": approval.purpose,
                "evidence": approval.evidence,
                "least_privilege": approval.least_privilege,
                "safe_alternatives": approval.safe_alternatives,
                "configured_authority": approval.configured_authority,
            },
            "decision": None if decision is None else {
                "category": decision.category,
                "question": decision.question,
                "options": decision.options,
            },
            "failure_code": result.failure_code,
        }

    @staticmethod
    def _observation_wire(observation: VerificationObservation | None) -> object:
        if observation is None:
            return None
        return {
            "kind": observation.kind,
            "repository": observation.repository,
            "task": observation.task,
            "branch_ref": observation.branch_ref,
            "head": observation.head,
            "tree": observation.tree,
            "record_id": observation.record_id,
            "contract_id": observation.contract_id,
            "view_id": observation.view_id,
            "checks": [{
                "check_id": item.check_id,
                "command": item.command,
                "state": item.state,
                "exit_code": item.exit_code,
                "duration_ms": item.duration_ms,
                "output_sha256": item.output_sha256,
                "artifact_reference": item.artifact_reference,
            } for item in observation.checks],
            "findings": [{
                "finding_id": item.finding_id,
                "severity": item.severity,
                "location": item.location,
                "observation": item.observation,
            } for item in observation.findings],
            "review_summary": observation.review_summary,
        }

    def _validate_edit_observation(
        self,
        value: EditObservation | None,
        unit: Any,
        binding: DispatchBinding,
        before: ExactSubject,
        after: ExactSubject,
        role: Any,
    ) -> None:
        if role.read_only:
            if value is not None or before != after:
                raise TurnOrchestrationError("changed_verification_subject")
            return
        if before == after:
            if value is not None:
                raise TurnOrchestrationError("verification_subject_mismatch")
            return
        if (
            type(value) is not EditObservation
            or value.binding != binding
            or value.before != before
            or value.after != after
            or type(value.changed_paths) is not tuple
            or not value.changed_paths
            or len(value.changed_paths) > work.MAX_SCOPE_ENTRIES
            or not unit.edit_scope
        ):
            raise TurnOrchestrationError("verification_subject_mismatch")
        if before.index_fingerprint != after.index_fingerprint:
            raise TurnOrchestrationError("verification_subject_mismatch")
        paths = tuple(self._validate_changed_path(path) for path in value.changed_paths)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise TurnOrchestrationError("verification_subject_mismatch")
        if any(not any(self._path_under(prefix, path) for prefix in unit.edit_scope) for path in paths):
            raise TurnOrchestrationError("verification_subject_mismatch")
        _bounded_text(value.confinement_reference, 256, "verification_subject_mismatch")

    def _response_size(self, response: DispatchResponse, maximum: int) -> int:
        edit = response.edit_observation
        edit_wire = None if edit is None else {
            "binding": edit.binding.__dict__,
            "before": self._subject_wire(edit.before),
            "after": self._subject_wire(edit.after),
            "changed_paths": edit.changed_paths,
            "confinement_reference": edit.confinement_reference,
        }
        wire = {
            "result": self._result_wire(response.result),
            "observation": self._observation_wire(response.observation),
            "edit_observation": edit_wire,
        }
        try:
            return len(_canonical(wire, maximum=maximum))
        except TurnOrchestrationError:
            raise TurnOrchestrationError("resource_limit_exceeded") from None

    def _checkpoint_previous_projection(
        self, receipt: task_collaboration.CollaborationReceipt,
    ) -> dict[str, Any]:
        context = self._context
        if (
            context is None
            or type(receipt) is not task_collaboration.CollaborationReceipt
            or receipt.repository != self.repository
            or receipt.task != self.task
            or receipt.branch_ref != context.branch_ref
            or receipt.subject != context.head
            or receipt.record_id != context.record_id
            or receipt.contract_id != context.contract_id
            or type(receipt.pull_request) is not int or receipt.pull_request <= 0
            or type(receipt.comment_id) is not int or receipt.comment_id <= 0
            or type(receipt.metadata_commit) is not str or not _OID.fullmatch(receipt.metadata_commit)
            or type(receipt.snapshot_id) is not str or not _HEX_64.fullmatch(receipt.snapshot_id)
        ):
            raise TurnOrchestrationError("checkpoint_binding_mismatch")
        return {
            "state": "observed",
            "checkpoint_id": str(receipt.comment_id),
            "subject": receipt.subject,
            "metadata_commit": receipt.metadata_commit,
            "snapshot_id": receipt.snapshot_id,
            "commits_after": {"state": "observed", "count": 0},
        }

    def _preflight_checkpoint_observation(
        self,
        view: dict[str, Any],
        pull: dict[str, Any],
        *,
        branch_ref: str,
        subject: str,
    ) -> task_view.GitHubPullRequestRequest:
        if self._checkpoint_observer is None:
            raise TurnOrchestrationError("checkpoint_observation_required")
        request = task_view.GitHubPullRequestRequest(
            self.repository, self.task, pull["number"], branch_ref,
        )
        try:
            raw = self._checkpoint_observer(request)
            if type(raw) is not task_view.GitHubObservation:
                raise ValueError
            github = task_view._validate_github_facts(
                raw.pull_request,
                request=request,
                repository=self.repository,
                branch_ref=branch_ref,
                oid_length=len(subject),
            )
            previous = task_view._validate_previous_input(raw.previous_checkpoint, len(subject))
        except Exception:
            raise TurnOrchestrationError("checkpoint_observation_required") from None
        if github != view["github"]:
            raise TurnOrchestrationError("checkpoint_binding_mismatch")
        projection = view["previous_checkpoint"]
        if previous is None:
            if projection != {"state": "absent"}:
                raise TurnOrchestrationError("checkpoint_binding_mismatch")
        else:
            if (
                projection.get("state") != "observed"
                or projection.get("checkpoint_id") != previous.checkpoint_id
                or projection.get("subject") != previous.subject
                or projection.get("metadata_commit") != previous.metadata_commit
                or projection.get("snapshot_id") != previous.snapshot_id
            ):
                raise TurnOrchestrationError("checkpoint_binding_mismatch")
            try:
                self._snapshots.read(
                    previous.metadata_commit, previous.snapshot_id,
                    task=self.task, subject=previous.subject,
                )
            except Exception:
                raise TurnOrchestrationError("checkpoint_binding_mismatch") from None
        return request

    def _current(
        self,
        *,
        allowed_checkpoint: task_collaboration.CollaborationReceipt | None = None,
    ) -> tuple[dict[str, Any], bytes, str, ExactSubject]:
        try:
            current = self._validated_observation()
        except Exception:
            self._invalidated = True
            raise
        if self._context is None or self._initial_view is None or self._initial_subject is None:
            raise TurnOrchestrationError("turn_not_started")
        view, raw, view_id, subject = current
        context = self._context
        initial = _detached(_canonical(self._initial_view))
        observed = _detached(_canonical(view))
        # Evidence-only metadata commits may advance the pointer without
        # changing this Task's exact Record/Contract or Git subject. Ignore
        # that pointer alone; every other observed fact remains guarded.
        initial["authority"]["metadata_commit"] = None
        observed["authority"]["metadata_commit"] = None
        # General implementation tickets may change dirty-worktree counts
        # while remaining anchored to the same exact committed H/tree. Those
        # counts are never used as proof of cleanliness; readonly tickets also
        # require the independent ExactSubject fingerprints below.
        initial["git"]["status"] = None
        observed["git"]["status"] = None
        if allowed_checkpoint is not None:
            expected_previous = self._checkpoint_previous_projection(allowed_checkpoint)
            initial_pull = initial["github"].get("pull_request")
            observed_pull = observed["github"].get("pull_request")
            if (
                initial["github"].get("state") != "observed"
                or observed["github"].get("state") != "observed"
                or type(initial_pull) is not dict or type(observed_pull) is not dict
                or initial_pull.get("number") != allowed_checkpoint.pull_request
                or observed_pull.get("number") != allowed_checkpoint.pull_request
                or initial_pull.get("head_oid") != self._context.head
                or observed_pull.get("head_oid") != self._context.head
            ):
                self._invalidated = True
                raise TurnOrchestrationError("checkpoint_binding_mismatch")
            current_previous = observed["previous_checkpoint"]
            if current_previous == expected_previous:
                # Only this exact receipt-derived C1 may be normalized to the
                # captured C0 for the one explicit post-write handoff guard.
                observed["previous_checkpoint"] = initial["previous_checkpoint"]
            elif current_previous != initial["previous_checkpoint"]:
                self._invalidated = True
                raise TurnOrchestrationError("checkpoint_binding_mismatch")
        if (
            view["branch_ref"] != context.branch_ref
            or view["subject"] != context.head
            or view["git"]["tree"] != context.tree
            or view["authority"]["record_id"] != context.record_id
            or view["authority"]["contract_id"] != context.contract_id
            or _canonical(initial) != _canonical(observed)
        ):
            self._invalidated = True
            raise TurnOrchestrationError("turn_context_changed")
        return view, raw, view_id, subject

    def _require_checkpoint_current(
        self, receipt: task_collaboration.CollaborationReceipt,
    ) -> dict[str, Any]:
        view, _raw, _view_id, _subject = self._current(allowed_checkpoint=receipt)
        if view["previous_checkpoint"] != self._checkpoint_previous_projection(receipt):
            raise TurnOrchestrationError("checkpoint_binding_mismatch")
        return view

    @staticmethod
    def _readonly_view_facts(view: dict[str, Any]) -> bytes:
        # Append-only Evidence may advance the metadata tip without moving
        # this exact Record/Contract/product subject. Do not compare that
        # permitted pointer as if it were a dirty or rewritten checkout.
        value = _detached(_canonical(view))
        value["authority"]["metadata_commit"] = None
        return _canonical(value)

    def _recheck_subject(
        self,
        expected: ExactSubject,
        *,
        allowed_checkpoint: task_collaboration.CollaborationReceipt | None = None,
    ) -> dict[str, Any]:
        view, _raw, _view_id, current = self._current(allowed_checkpoint=allowed_checkpoint)
        if current != expected:
            self._invalidated = True
            raise TurnOrchestrationError("turn_context_changed")
        return view

    def _prerequisite_digest(self, unit: Any, view: dict[str, Any]) -> str | None:
        if self._prerequisites is None:
            return None
        try:
            # The installed evaluator's operation is policy-owned. A diagnosis
            # is a precondition only; it cannot grant execution permission.
            operation = self._prerequisites._policy_value["operation"]
            request = prerequisites_module.OperationRequest(
                self.repository, self.task, self._context.branch_ref, self._context.head,
                operation, {"unit_id": unit.unit_id, "role": unit.role.role},
            )
            diagnosis = self._prerequisites.diagnose(request, view)
            encoded = prerequisites_module.encode_diagnosis(diagnosis)
        except Exception:
            raise TurnOrchestrationError("prerequisites_unavailable") from None
        digest = hashlib.sha256(encoded).hexdigest()
        if diagnosis.get("result") != "PREREQUISITES_SATISFIED":
            raise TurnOrchestrationError("prerequisites_not_satisfied")
        return digest

    @staticmethod
    def _inspection(unit: Any, view: dict[str, Any], binding: DispatchBinding) -> NativeInspectionRequest | None:
        if unit.role.read_only is not True:
            return None
        return NativeInspectionRequest(
            binding.repository, binding.task, binding.branch_ref,
            view["authority"]["base_revision"], binding.head, binding.tree,
            tuple(unit.read_scope),
        )

    def dispatch(self, unit_id: str) -> Any:
        """Start and dispatch one ready unit; every failure is terminal BLOCKED."""
        coordinator, context = self._require_live_turn()
        unit = self._unit(unit_id)
        try:
            started = coordinator.start(unit_id)
        except Exception:
            raise TurnOrchestrationError("unit_not_ready") from None
        if started != unit:
            unit = started
        self._running_units.add(unit.unit_id)
        try:
            if type(unit) is not work.WorkUnit or unit.context != context:
                raise TurnOrchestrationError("unit_binding_mismatch")
            role = self._role_by_name.get(unit.role.role)
            if role is None or role != unit.role:
                raise TurnOrchestrationError("unit_binding_mismatch")
            if self._dispatch_capability is None:
                raise TurnOrchestrationError("dispatch_unavailable")
            view, _view_bytes, _view_id, before = self._current()
            initial_subject = self._initial_subject
            if role.read_only and (
                before.clean is not True or initial_subject is None or before != initial_subject
            ):
                raise TurnOrchestrationError("changed_verification_subject")
            if any(type(value) is not str for value in unit.read_scope + unit.edit_scope):
                raise TurnOrchestrationError("unit_binding_mismatch")
            prerequisite_digest = self._prerequisite_digest(unit, view)
            binding = DispatchBinding(
                self.repository, self.task, context.branch_ref, context.head, context.tree,
                context.record_id, context.contract_id, context.view_id, unit.unit_id,
                role.role, role.provider, role.model, role.executor_ref,
                self._worktree_reference.token,
            )
            request = DispatchRequest(
                binding=binding,
                context=context,
                subject=before,
                objective=_bounded_text(unit.objective, MAX_SUMMARY_BYTES, "unit_binding_mismatch"),
                read_scope=tuple(unit.read_scope),
                edit_scope=tuple(unit.edit_scope),
                constraints=tuple(unit.constraints),
                dependencies=tuple(unit.dependencies),
                expected_outputs=tuple(unit.expected_outputs),
                stop_conditions=tuple(unit.stop_conditions),
                max_seconds=unit.budget.max_seconds,
                max_output_bytes=unit.budget.max_output_bytes,
                max_context_bytes=unit.budget.max_context_bytes,
                inspection=self._inspection(unit, view, binding),
            )
            request_wire = {
                "binding": binding.__dict__,
                "context": context.__dict__,
                "subject": self._subject_wire(request.subject),
                "objective": request.objective,
                "read_scope": request.read_scope,
                "edit_scope": request.edit_scope,
                "constraints": request.constraints,
                "dependencies": request.dependencies,
                "expected_outputs": request.expected_outputs,
                "stop_conditions": request.stop_conditions,
                "max_seconds": request.max_seconds,
                "max_output_bytes": request.max_output_bytes,
                "max_context_bytes": request.max_context_bytes,
                "inspection": None if request.inspection is None else request.inspection.__dict__,
            }
            try:
                request_bytes = _canonical(request_wire, maximum=unit.budget.max_context_bytes)
            except TurnOrchestrationError:
                raise TurnOrchestrationError("resource_limit_exceeded") from None
            request_digest = hashlib.sha256(
                b"agentcore-turn-dispatch-authorization/v1\n" + request_bytes
            ).hexdigest()
            auth_request = DispatchAuthorizationRequest(
                binding, context, role.read_only is True, prerequisite_digest,
                request, request_digest,
            )
            before_authorization = before
            try:
                authorized = self._authorizer(auth_request)
            except Exception:
                self._recheck_subject(before_authorization)
                raise TurnOrchestrationError("execution_authorization_unavailable") from None
            view = self._recheck_subject(before_authorization)
            if authorized is not True:
                raise TurnOrchestrationError("execution_authorization_denied")
            # Recheck the exact turn after policy/authorization callbacks and
            # before asking the resolver for an opaque registered checkout.
            view, _raw, _view_id, before = self._current()
            if before != before_authorization:
                raise TurnOrchestrationError("turn_context_changed")
            if role.read_only and (
                before.clean is not True or initial_subject is None or before != initial_subject
            ):
                raise TurnOrchestrationError("changed_verification_subject")
            try:
                registration_raw = self._dispatch_capability.resolve(self._worktree_reference, request)
            except Exception:
                self._recheck_subject(before)
                raise TurnOrchestrationError("worktree_unavailable") from None
            registration = self._registration(registration_raw, binding, role)
            _resolved_view, _resolved_raw, _resolved_id, after_resolve = self._current()
            if after_resolve != before:
                raise TurnOrchestrationError("turn_context_changed")
            started_at = time.monotonic()
            self._dispatch_bindings[unit.unit_id] = binding
            try:
                response_raw = self._dispatch_capability.dispatch(registration, request)
            except Exception as error:
                exception_class = type(error).__name__
                if type(exception_class) is not str or not _CLASS_NAME.fullmatch(exception_class):
                    exception_class = "ProviderError"
                self._provider_failures[unit.unit_id] = ProviderFailure(
                    unit.unit_id, role.role, role.provider, role.model,
                    exception_class, context,
                )
                try:
                    _failed_view, _failed_raw, _failed_id, after_failure = self._current()
                except TurnOrchestrationError:
                    self._invalidated = True
                    raise
                except Exception:
                    self._invalidated = True
                    raise TurnOrchestrationError("turn_context_changed") from None
                if after_failure != before:
                    self._invalidated = True
                    raise TurnOrchestrationError("changed_verification_subject") from None
                raise TurnOrchestrationError("provider_failure") from None
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            after_view, _after_raw, _after_view_id, after = self._validated_observation()
            identity_stable = (
                after.head == before.head and after.tree == before.tree
                and after.record_id == before.record_id and after.contract_id == before.contract_id
                and after.branch_ref == before.branch_ref
            )
            if not identity_stable:
                self._invalidated = True
                raise TurnOrchestrationError("changed_verification_subject")
            if role.read_only and (
                before.clean is not True or after.clean is not True
                or initial_subject is None or before != initial_subject
                or after != before
                or before.index_fingerprint != after.index_fingerprint
                or before.status_fingerprint != after.status_fingerprint
                or self._readonly_view_facts(after_view) != self._readonly_view_facts(view)
            ):
                self._invalidated = True
                raise TurnOrchestrationError("changed_verification_subject")
            if type(response_raw) is not DispatchResponse:
                if after != before:
                    self._invalidated = True
                    raise TurnOrchestrationError("verification_subject_mismatch")
                raise TurnOrchestrationError("invalid_dispatch_response")
            try:
                self._validate_edit_observation(
                    response_raw.edit_observation, unit, binding, before, after, role,
                )
            except TurnOrchestrationError:
                if after != before:
                    self._invalidated = True
                raise
            response = self._validate_response(response_raw, unit, binding, role, request)
            if elapsed_ms > unit.budget.max_seconds * 1000:
                raise TurnOrchestrationError("resource_limit_exceeded")
            result = response.result
            if result.outcome == "NEEDS_DECISION":
                # Product/requirement/architecture choices are returned to a
                # human verbatim; this layer never selects a model-proposed
                # option or synthesizes a human-decision schema.
                _bounded_text(result.decision.question, 2_048, "invalid_leaf_result")
                if result.decision.category == "mechanical":
                    self._resolve_mechanical(binding, result.decision, unit.unit_id, before)
            if result.outcome == "NEEDS_APPROVAL":
                self._escalate(binding, result.approval, unit.unit_id, before)
            if role.role == "verifier" and result.outcome == "COMPLETED":
                observation = response.observation
                if observation is None:
                    result = self._blocked_result(unit, "verification_unavailable")
                elif any(
                    check.state == "FAILED"
                    or (check.state == "EXECUTED" and check.exit_code != 0)
                    for check in observation.checks
                ):
                    result = self._blocked_result(unit, "verification_failed")
                elif any(check.state in {"UNAVAILABLE", "NOT_RUN"} for check in observation.checks):
                    result = self._blocked_result(unit, "verification_unavailable")
            finished = self._finish(coordinator, unit, result)
            if finished.outcome in {"COMPLETED", "BLOCKED"}:
                self._dispatch_bindings[unit.unit_id] = binding
                if response.observation is not None:
                    self._observations[unit.unit_id] = response.observation
            return finished
        except TurnOrchestrationError as error:
            if error.code in {
                "turn_context_changed", "changed_verification_subject",
                "turn_context_unavailable", "invalid_task_view",
                "task_authority_binding_mismatch", "exact_subject_unavailable",
                "exact_subject_binding_mismatch",
            }:
                self._invalidated = True
            result = self._blocked_result(unit, error.code)
            return self._finish(coordinator, unit, result)
        except Exception:
            result = self._blocked_result(unit, "provider_failure")
            return self._finish(coordinator, unit, result)

    def _escalate(
        self, binding: DispatchBinding, request: Any, unit_id: str, before: ExactSubject,
    ) -> OperationalResolution | None:
        if self._escalation_policy is None:
            return None
        typed = OperationalResolutionRequest(binding, request)
        try:
            response = self._escalation_policy(typed)
        except Exception:
            self._recheck_subject(before)
            raise TurnOrchestrationError("operational_approval_denied") from None
        self._recheck_subject(before)
        if (
            type(response) is not OperationalResolution
            or response.binding != binding or response.request != request
            or type(response.disposition) is not str
        ):
            raise TurnOrchestrationError("invalid_escalation_resolution")
        if response.disposition not in {"allow", "approved", "deny", "ask"}:
            raise TurnOrchestrationError("invalid_escalation_resolution")
        _view, _raw, _view_id, after = self._current()
        if after != before:
            self._invalidated = True
            raise TurnOrchestrationError("turn_context_changed")
        self._operational_resolutions[unit_id] = response
        if response.disposition == "deny":
            raise TurnOrchestrationError("operational_approval_denied")
        # allow/approved is not permission to continue the current unit.  The
        # owner must create a separate bounded unit with a fresh authorization.
        return response

    def _resolve_mechanical(
        self, binding: DispatchBinding, decision: Any, unit_id: str, before: ExactSubject,
    ) -> MechanicalDecisionResolution | None:
        if self._decision_policy is None:
            return None
        if type(decision) is not work.DecisionRequest or decision.category != "mechanical":
            raise TurnOrchestrationError("invalid_decision_request")
        request = MechanicalDecisionResolutionRequest(binding, decision)
        try:
            response = self._decision_policy(request)
        except Exception:
            self._recheck_subject(before)
            raise TurnOrchestrationError("mechanical_decision_unavailable") from None
        self._recheck_subject(before)
        if (
            type(response) is not MechanicalDecisionResolution
            or response.binding != binding
            or response.decision != decision
            or type(response.selected_option) is not str
            or response.selected_option not in decision.options
        ):
            raise TurnOrchestrationError("invalid_mechanical_decision")
        _bounded_text(response.authority_reference, 256, "invalid_mechanical_decision")
        _view, _raw, _view_id, after = self._current()
        if after != before:
            self._invalidated = True
            raise TurnOrchestrationError("turn_context_changed")
        self._mechanical_decisions[unit_id] = response
        return response

    def _validate_response(
        self,
        value: object,
        unit: Any,
        binding: DispatchBinding,
        role: Any,
        request: DispatchRequest,
    ) -> DispatchResponse:
        if type(value) is not DispatchResponse or value.binding != binding or value.context != unit.context:
            raise TurnOrchestrationError("invalid_dispatch_response")
        try:
            coordinator = self._coordinator
            if coordinator is None:
                raise ValueError
            coordinator.validate_result(value.result, unit)
        except Exception:
            raise TurnOrchestrationError("invalid_leaf_result") from None
        if value.observation is not None:
            self._validate_observation(value.observation, binding, role)
            if value.observation.kind in {"review", "security_review"} and request.inspection is None:
                raise TurnOrchestrationError("verification_subject_mismatch")
        if value.edit_observation is not None:
            edit = value.edit_observation
            if (
                type(edit) is not EditObservation or edit.binding != binding
                or type(edit.changed_paths) is not tuple
                or len(edit.changed_paths) > work.MAX_SCOPE_ENTRIES
            ):
                raise TurnOrchestrationError("verification_subject_mismatch")
            self._validate_subject_shape(edit.before)
            self._validate_subject_shape(edit.after)
            for path in edit.changed_paths:
                self._validate_changed_path(path)
            _bounded_text(edit.confinement_reference, 256, "verification_subject_mismatch")
        self._response_size(value, unit.budget.max_output_bytes)
        _bounded_text(value.result.summary, MAX_SUMMARY_BYTES, "invalid_leaf_result", empty=True)
        return value

    def _validate_observation(self, value: object, binding: DispatchBinding, role: Any) -> None:
        if type(value) is not VerificationObservation or role.read_only is not True:
            raise TurnOrchestrationError("verification_unavailable")
        expected_kind = {
            "verifier": "verification",
            "reviewer": "review",
            "security-reviewer": "security_review",
        }.get(role.role)
        if (
            expected_kind is None or type(value.kind) is not str or value.kind != expected_kind
            or value.repository != binding.repository or value.task != binding.task
            or value.branch_ref != binding.branch_ref or value.head != binding.head
            or value.tree != binding.tree or value.record_id != binding.record_id
            or value.contract_id != binding.contract_id or value.view_id != binding.view_id
            or type(value.checks) is not tuple or type(value.findings) is not tuple
        ):
            raise TurnOrchestrationError("verification_subject_mismatch")
        if len(value.checks) > MAX_CHECKS or len(value.findings) > MAX_FINDINGS:
            raise TurnOrchestrationError("verification_policy_mismatch")
        check_ids: list[str] = []
        for check in value.checks:
            if type(check) is not ExecutedCheck:
                raise TurnOrchestrationError("verification_policy_mismatch")
            if (
                type(check.check_id) is not str or check.check_id not in _PROJECT_CHECKS
                or type(check.command) is not str or check.command != check.check_id
                or type(check.state) is not str
            ):
                raise TurnOrchestrationError("verification_policy_mismatch")
            if check.state not in {"EXECUTED", "UNAVAILABLE", "FAILED", "NOT_RUN"}:
                raise TurnOrchestrationError("verification_policy_mismatch")
            if check.exit_code is not None and type(check.exit_code) is not int:
                raise TurnOrchestrationError("verification_policy_mismatch")
            if type(check.duration_ms) is not int or not 0 <= check.duration_ms <= 3_600_000:
                raise TurnOrchestrationError("verification_policy_mismatch")
            _check_hash(check.output_sha256, "verification_policy_mismatch")
            if check.artifact_reference is not None:
                _bounded_text(
                    check.artifact_reference, MAX_ARTIFACT_REFERENCE_BYTES,
                    "verification_policy_mismatch",
                )
                if not _ARTIFACT_REF.fullmatch(check.artifact_reference):
                    raise TurnOrchestrationError("verification_policy_mismatch")
            if check.state == "EXECUTED" and type(check.exit_code) is not int:
                raise TurnOrchestrationError("verification_policy_mismatch")
            if check.state in {"UNAVAILABLE", "NOT_RUN"} and check.exit_code is not None:
                raise TurnOrchestrationError("verification_policy_mismatch")
            if check.exit_code is not None and check.exit_code < 0:
                raise TurnOrchestrationError("verification_policy_mismatch")
            check_ids.append(check.check_id)
        if len(check_ids) != len(set(check_ids)):
            raise TurnOrchestrationError("verification_policy_mismatch")
        if value.kind == "verification":
            if value.findings or value.review_summary != "":
                raise TurnOrchestrationError("verification_policy_mismatch")
            expected = self._dispatch_capability.required_check_ids if self._dispatch_capability else ()
            if not expected or tuple(check_ids) != expected:
                raise TurnOrchestrationError("verification_policy_mismatch")
        elif value.checks:
            raise TurnOrchestrationError("verification_policy_mismatch")
        else:
            _bounded_text(value.review_summary, MAX_SUMMARY_BYTES, "verification_policy_mismatch")
        finding_ids: set[str] = set()
        for finding in value.findings:
            if type(finding) is not ReviewFinding:
                raise TurnOrchestrationError("verification_policy_mismatch")
            if type(finding.severity) is not str or finding.severity not in {
                "critical", "high", "medium", "low", "info",
            }:
                raise TurnOrchestrationError("verification_policy_mismatch")
            _bounded_text(finding.finding_id, 128, "verification_policy_mismatch")
            if finding.finding_id in finding_ids:
                raise TurnOrchestrationError("verification_policy_mismatch")
            finding_ids.add(finding.finding_id)
            _bounded_text(finding.location, 512, "verification_policy_mismatch")
            _bounded_text(finding.observation, MAX_FINDING_BYTES, "verification_policy_mismatch")
        try:
            # Reserve the worst-case permitted read scope and producer/subject
            # envelope before accepting an observation. An accepted response
            # must not become unpublishable merely when its scope is attached.
            reserve = work.MAX_SCOPE_ENTRIES * (work.MAX_SCOPE_PATH_BYTES + 4) + 16384
            _canonical(self._observation_wire(value), maximum=codec.MAX_PAYLOAD_BYTES - reserve)
        except TurnOrchestrationError:
            raise TurnOrchestrationError("resource_limit_exceeded") from None

    def promote_verification(self, unit_id: str) -> EvidenceReference:
        """Publish only typed adapter observations, then verify exact owner readback."""
        _coordinator, context = self._require_live_turn()
        if self._evidence_store is None or self._publish_evidence is None:
            raise TurnOrchestrationError("evidence_publication_unavailable")
        result = self.result(unit_id)
        observation = self._observations.get(unit_id)
        binding = self._dispatch_bindings.get(unit_id)
        if (
            result is None or result.outcome not in {"COMPLETED", "BLOCKED"}
            or observation is None or binding is None
        ):
            raise TurnOrchestrationError("verification_unavailable")
        role = self._role_by_name[binding.role]
        self._validate_observation(observation, binding, role)
        try:
            current_view, _raw, _current_id, subject = self._current()
        except TurnOrchestrationError as error:
            if error.code == "turn_context_changed":
                raise TurnOrchestrationError("changed_verification_subject") from None
            raise
        if (
            role.read_only is not True or subject.clean is not True
            or subject != self._initial_subject
            or observation.view_id != context.view_id
            or (observation.head, observation.tree) != (context.head, context.tree)
        ):
            raise TurnOrchestrationError("changed_verification_subject")
        if observation.kind != "verification":
            verdict = "OBSERVED"
        elif any(check.state == "FAILED" or (check.state == "EXECUTED" and check.exit_code != 0)
                 for check in observation.checks):
            verdict = "FAIL"
        elif any(check.state in {"UNAVAILABLE", "NOT_RUN"} for check in observation.checks):
            verdict = "UNAVAILABLE"
        else:
            verdict = "PASS"
        unit = next(unit for unit in self.units() if unit.unit_id == unit_id)
        scope = list(unit.read_scope)
        scope_digest = hashlib.sha256(
            b"agentcore-turn-read-scope/v1\n" + _canonical({"read_scope": scope})
        ).hexdigest()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "observation_kind": observation.kind,
            "repository": observation.repository,
            "task": observation.task,
            "branch_ref": observation.branch_ref,
            "head": observation.head,
            "tree": observation.tree,
            "record_id": observation.record_id,
            "contract_id": observation.contract_id,
            "view_id": observation.view_id,
            "base_revision": self._initial_view["authority"]["base_revision"],
            "read_scope": scope,
            "scope_sha256": scope_digest,
            "producer_binding": {
                "role": binding.role,
                "provider": binding.provider,
                "model": binding.model,
                "executor_ref": binding.executor_ref,
                "dispatch_qualification": self._dispatch_capability.qualification_id,
            },
            "subject_fingerprints": {
                "index_sha256": subject.index_fingerprint,
                "status_sha256": subject.status_fingerprint,
            },
            "required_check_ids": list(self._dispatch_capability.required_check_ids if observation.kind == "verification" else ()),
            "checks": [
                {
                    "check_id": item.check_id,
                    "command": item.command,
                    "state": item.state,
                    "exit_code": item.exit_code,
                    "duration_ms": item.duration_ms,
                    "output_sha256": item.output_sha256,
                    "artifact_reference": item.artifact_reference,
                }
                for item in observation.checks
            ],
            "findings": [
                {
                    "finding_id": item.finding_id,
                    "severity": item.severity,
                    "location": item.location,
                    "observation": item.observation,
                }
                for item in observation.findings
            ],
            "review_summary": observation.review_summary,
            "result": verdict,
            "clean_subject": True,
        }
        try:
            evidence_id, canonical_bytes = evidence_module.encode_evidence(
                self.repository, self.task, context.head,
                {
                    "schema_version": 1,
                    "kind": _VERIFICATION_KIND,
                    "producer": {"name": "agent-core-v4-turn-orchestration", "version": "1"},
                    "created_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "payload": payload,
                },
            )
        except Exception:
            raise TurnOrchestrationError("verification_policy_mismatch") from None
        auth = EvidenceAuthorizationRequest(
            self.repository, self.task, context.head, evidence_id, _VERIFICATION_KIND, context.view_id,
        )
        try:
            authorized = self._authorizer(auth)
        except Exception:
            self._recheck_subject(subject)
            raise TurnOrchestrationError("evidence_authorization_denied") from None
        self._recheck_subject(subject)
        if authorized is not True:
            raise TurnOrchestrationError("evidence_authorization_denied")
        request = EvidencePromotionRequest(
            self.repository, self.task, context.head, evidence_id, canonical_bytes, _VERIFICATION_KIND,
        )
        try:
            recorded = self._publish_evidence(request)
        except Exception:
            self._recheck_subject(subject)
            raise TurnOrchestrationError("evidence_publication_unavailable") from None
        if type(recorded) is not RecordedEvidence or (
            recorded.evidence_id != evidence_id or recorded.subject != context.head
            or type(recorded.metadata_commit) is not str or not _OID.fullmatch(recorded.metadata_commit)
        ):
            raise TurnOrchestrationError("evidence_publication_unconfirmed")
        try:
            confirmed_commit = self._evidence_store.confirm(
                recorded.metadata_commit,
                required_objects=(("evidence", evidence_id, self.task, context.head),),
            )
            if confirmed_commit != recorded.metadata_commit:
                raise TurnOrchestrationError("evidence_publication_unconfirmed")
            resolved = evidence_module.Evidence(self._evidence_store).read(
                recorded.metadata_commit, evidence_id, task=self.task, subject=context.head,
            )
            actual_bytes = codec.encode_object(
                resolved["kind"], resolved["repository"], resolved["task"],
                resolved["subject"], resolved["payload"],
            )[1]
        except Exception:
            raise TurnOrchestrationError("evidence_publication_unconfirmed") from None
        if actual_bytes != canonical_bytes:
            raise TurnOrchestrationError("evidence_publication_unconfirmed")
        try:
            current_view, _current_raw, _current_id, current_subject = self._current()
            if (
                current_subject != self._initial_subject
                or current_subject.clean is not True
                or current_view["subject"] != context.head
                or current_view["git"]["tree"] != context.tree
            ):
                raise ValueError
        except Exception:
            raise TurnOrchestrationError("evidence_publication_unconfirmed") from None
        return EvidenceReference(recorded.metadata_commit, evidence_id, context.head)

    def _unresolved(self) -> tuple[UnitOutcomeDescriptor, ...]:
        unresolved: list[UnitOutcomeDescriptor] = []
        for unit in self.units():
            result = self.result(unit.unit_id)
            if result is None:
                unresolved.append(UnitOutcomeDescriptor(
                    unit.unit_id, unit.role.role, "pending", None, "not_dispatched",
                ))
            elif result.outcome != "COMPLETED":
                failure = result.failure_code or (
                    "mechanical_resolution_available"
                    if self._mechanical_decisions.get(unit.unit_id) is not None
                    else "approval_resolved_new_unit_required"
                    if self._operational_resolutions.get(unit.unit_id) is not None
                    and self._operational_resolutions[unit.unit_id].disposition in {"allow", "approved"}
                    else "human_direction_required" if result.outcome == "NEEDS_DECISION"
                    else "needs_approval"
                )
                unresolved.append(UnitOutcomeDescriptor(
                    unit.unit_id, unit.role.role, "terminal", result.outcome, failure,
                ))
        return tuple(unresolved[:32])

    def handoff(self, summary: str, *, evidence: tuple[EvidenceReference, ...] = ()) -> HandoffReport:
        """Create an owner-confirmed snapshot/checkpoint report for explicit handoff.

        TaskViewSnapshots owns the #144 snapshot publication. The checkpoint
        observer authenticates current PR/principal/base/head and prior graph;
        its reader confirms the live comment and exact snapshot metadata graph.
        PR number comes from fresh facts, never a caller-selected alternate PR.
        """
        coordinator, context = self._require_live_turn()
        del coordinator
        clean_summary = _bounded_text(summary, MAX_SUMMARY_BYTES, "invalid_handoff_summary")
        if self._running_units:
            raise TurnOrchestrationError("unit_still_running")
        if self._snapshots is None or self._checkpoint_writer is None:
            raise TurnOrchestrationError("checkpoint_unavailable")
        if type(evidence) is not tuple or len(evidence) > 256:
            raise TurnOrchestrationError("invalid_evidence_reference")
        if evidence and self._evidence_store is None:
            raise TurnOrchestrationError("invalid_evidence_reference")
        normalized: list[EvidenceReference] = []
        for ref in evidence:
            if type(ref) is not EvidenceReference:
                raise TurnOrchestrationError("invalid_evidence_reference")
            if ref.subject != context.head:
                raise TurnOrchestrationError("snapshot_binding_mismatch")
            _oid(ref.subject, "invalid_evidence_reference")
            _metadata_id(ref.evidence_id, "invalid_evidence_reference")
            _oid(ref.metadata_commit, "invalid_evidence_reference")
            try:
                confirmed = self._evidence_store.confirm(
                    ref.metadata_commit,
                    required_objects=(("evidence", ref.evidence_id, self.task, ref.subject),),
                )
                if confirmed != ref.metadata_commit:
                    raise TurnOrchestrationError("invalid_evidence_reference")
                evidence_module.Evidence(self._evidence_store).read(
                    ref.metadata_commit, ref.evidence_id, task=self.task, subject=ref.subject,
                )
            except Exception:
                raise TurnOrchestrationError("invalid_evidence_reference") from None
            normalized.append(ref)
        normalized.sort(key=lambda item: item.evidence_id)
        if len({item.evidence_id for item in normalized}) != len(normalized):
            raise TurnOrchestrationError("invalid_evidence_reference")
        view, _raw, _view_id, subject = self._current()
        if subject.head != context.head or subject.tree != context.tree:
            raise TurnOrchestrationError("snapshot_binding_mismatch")
        github = view["github"]
        pull = github.get("pull_request") if type(github) is dict else None
        remote_head = view["git"]["remote_head"]
        if (
            self._checkpoint_observer is None
            or github.get("state") != "observed"
            or type(pull) is not dict
            or type(pull.get("number")) is not int or pull["number"] <= 0
            or pull.get("base_repository") != self.repository
            or pull.get("head_repository") != self.repository
            or pull.get("head_oid") != context.head
            or pull.get("head_ref") != context.branch_ref.removeprefix("refs/heads/")
            or pull.get("state") != "open"
            or remote_head.get("state") != "observed"
            or remote_head.get("head_oid") != context.head
        ):
            raise TurnOrchestrationError("checkpoint_observation_required")
        default_branch = view["git"]["default_branch"]
        default_branch_ref = (
            default_branch["ref"] if default_branch.get("state") == "observed" else None
        )
        self._preflight_checkpoint_observation(
            view, pull, branch_ref=context.branch_ref, subject=context.head,
        )
        try:
            selection = tuple(task_view.EvidenceRef(item.evidence_id, item.subject) for item in normalized)
            publication = self._snapshots.capture(
                task=self.task,
                branch_ref=context.branch_ref,
                base_revision=view["authority"]["base_revision"],
                boundary="explicit-handoff",
                selected_evidence=selection,
                default_branch_ref=default_branch_ref,
                observe_remote_head=True,
                pr_number=pull["number"],
                github_reader=self._checkpoint_observer,
            )
            saved = self._snapshots.read(
                publication.metadata_commit, publication.snapshot_id,
                task=self.task, subject=context.head,
            )
        except Exception:
            self._recheck_subject(subject)
            raise TurnOrchestrationError("snapshot_unavailable") from None
        saved_view = saved.get("view") if type(saved) is dict else None
        try:
            authority = saved_view["authority"]
            if (
                publication.subject != context.head
                or saved["boundary"] != "explicit-handoff"
                or saved_view["repository"] != self.repository
                or saved_view["task"] != self.task
                or saved_view["branch_ref"] != context.branch_ref
                or saved_view["subject"] != context.head
                or saved_view["git"]["tree"] != context.tree
                or authority["record_id"] != context.record_id
                or authority["contract_id"] != context.contract_id
                or saved_view["github"] != view["github"]
                or saved_view["previous_checkpoint"] != view["previous_checkpoint"]
            ):
                raise ValueError
            recorded_ids = {(item["evidence_id"], item["subject"]) for item in saved_view["evidence"]}
            if any((item.evidence_id, item.subject) not in recorded_ids for item in normalized):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise TurnOrchestrationError("snapshot_binding_mismatch") from None
        self._recheck_subject(subject)
        unresolved = self._unresolved()
        checkpoint_request = CheckpointRequest(
            self.repository, self.task, context.branch_ref, context.head,
            context.record_id, context.contract_id, clean_summary,
            unresolved, tuple(normalized), publication, pull["number"],
        )
        try:
            checkpoint = self._checkpoint_writer(checkpoint_request)
        except Exception:
            self._recheck_subject(subject)
            raise TurnOrchestrationError("checkpoint_unavailable") from None
        if type(checkpoint) is not task_collaboration.CollaborationReceipt:
            self._recheck_subject(subject)
            raise TurnOrchestrationError("checkpoint_binding_mismatch")
        if (
            checkpoint.repository != self.repository or checkpoint.task != self.task
            or checkpoint.branch_ref != context.branch_ref or checkpoint.subject != context.head
            or checkpoint.metadata_commit != publication.metadata_commit
            or checkpoint.record_id != context.record_id or checkpoint.contract_id != context.contract_id
            or checkpoint.snapshot_id != publication.snapshot_id
            or type(checkpoint.pull_request) is not int or checkpoint.pull_request <= 0
            or checkpoint.pull_request != checkpoint_request.pull_request
            or type(checkpoint.comment_id) is not int or checkpoint.comment_id <= 0
        ):
            self._recheck_subject(subject)
            raise TurnOrchestrationError("checkpoint_binding_mismatch")
        first_confirmation = self._confirm_checkpoint(checkpoint, subject)
        self._require_checkpoint_current(checkpoint)
        self._recheck_subject(subject, allowed_checkpoint=checkpoint)
        second_confirmation = self._confirm_checkpoint(checkpoint, subject)
        if second_confirmation != first_confirmation:
            raise TurnOrchestrationError("invalid_checkpoint_confirmation")
        self._require_checkpoint_current(checkpoint)
        self._recheck_subject(subject, allowed_checkpoint=checkpoint)
        # Freshly re-read the immutable snapshot after the owner callback.  The
        # callback itself is required to confirm the #194 receipt with its owner.
        try:
            final = self._snapshots.read(
                publication.metadata_commit, publication.snapshot_id,
                task=self.task, subject=context.head,
            )
        except Exception:
            raise TurnOrchestrationError("snapshot_binding_mismatch") from None
        if final != saved:
            raise TurnOrchestrationError("snapshot_binding_mismatch")
        self._require_checkpoint_current(checkpoint)
        self._recheck_subject(subject, allowed_checkpoint=checkpoint)
        # This Turn's durable handoff is complete. Further execution requires
        # a newly observed context, not reuse of the pre-checkpoint facts.
        self._invalidated = True
        return HandoffReport(
            self.repository, self.task, context.branch_ref, context.head, context.tree,
            context.record_id, context.contract_id, clean_summary, unresolved,
            tuple(normalized), publication, checkpoint, second_confirmation,
        )

    def _confirm_checkpoint(
        self, receipt: task_collaboration.CollaborationReceipt, expected_subject: ExactSubject,
    ) -> ConfirmedCheckpoint:
        if self._checkpoint_reader is None:
            raise TurnOrchestrationError("checkpoint_confirmation_required")
        try:
            value = self._checkpoint_reader(receipt)
        except Exception:
            self._recheck_subject(expected_subject, allowed_checkpoint=receipt)
            raise TurnOrchestrationError("checkpoint_unavailable") from None
        self._recheck_subject(expected_subject, allowed_checkpoint=receipt)
        if (
            type(value) is not ConfirmedCheckpoint
            or value.receipt != receipt
            or type(value.principal_id) is not int
            or value.principal_id <= 0
        ):
            raise TurnOrchestrationError("invalid_checkpoint_confirmation")
        _bounded_text(value.observation_reference, 256, "invalid_checkpoint_confirmation")
        return value

    def discard(self) -> None:
        """Clear only ephemeral Python coordinator state; perform no I/O."""
        if self._running_units:
            raise TurnOrchestrationError("unit_still_running")
        if self._coordinator is not None:
            try:
                self._coordinator.discard()
            except Exception:
                raise TurnOrchestrationError("turn_discard_failed") from None
        self._coordinator = None
        self._context = None
        self._initial_view = None
        self._initial_subject = None
        self._observations.clear()
        self._dispatch_bindings.clear()
        self._provider_failures.clear()
        self._operational_resolutions.clear()
        self._mechanical_decisions.clear()
        self._running_units.clear()
        self._invalidated = True
