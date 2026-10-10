"""Pure, ephemeral work-unit tickets and descriptive Issue selection for v4.

This module owns no durable state and has no filesystem, Git, process, or agent
dispatch adapter.  A trusted host supplies the immutable Task observation and
exact role bindings, then independently revalidates every ticket before it
hands anything to a leaf.  Scope checks here are conservative lexical
scheduling checks; they are not filesystem permissions and do not resolve
symlinks, hard links, case folding, or Git administration paths.

Terminal leaf output is bounded metadata, not file content or execution proof.
Approval and decision requests are descriptive handoffs only.  In particular,
none of these values grants authority, persists a Task transition, or creates
an Evidence/READY result.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass


MAX_TEXT_BYTES = 16 * 1024
MAX_OBJECTIVE_BYTES = 8 * 1024
MAX_SCOPE_PATH_BYTES = 1024
MAX_SCOPE_ENTRIES = 64
MAX_LIST_ENTRIES = 32
MAX_TURN_UNITS = 128
MAX_UNIT_SECONDS = 3600
MAX_UNIT_OUTPUT_BYTES = 1024 * 1024
MIN_UNIT_OUTPUT_BYTES = 512
MAX_UNIT_CONTEXT_BYTES = 1024 * 1024
MAX_TURN_DISPATCHES = 128
MAX_CANDIDATES = 10_000

_HEX_64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_TASK = re.compile(r"[1-9][0-9]{0,127}\Z", re.ASCII)
_REPOSITORY_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
_TURN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)
_UNIT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)
_LABEL = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z", re.ASCII)
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_OPERATION_CLASS = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z", re.ASCII)

LEAF_ROLES = frozenset({
    "general", "explore", "investigator", "verifier", "reviewer",
    "security-reviewer", "architect", "scout",
})
OUTCOMES = frozenset({"COMPLETED", "BLOCKED", "NEEDS_APPROVAL", "NEEDS_DECISION"})
DECISION_CATEGORIES = frozenset({
    "mechanical", "product", "requirement", "architecture", "tradeoff", "conflict",
})


class WorkUnitError(ValueError):
    """A stable, sanitized work-ticket error code."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or not _CODE.fullmatch(code):
            code = "work_unit_error"
        self.code = code
        super().__init__(code)


def _error(code: str) -> None:
    raise WorkUnitError(code)


def _text(
    value: object,
    *,
    code: str,
    maximum: int = MAX_TEXT_BYTES,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or (not allow_empty and not value.strip()):
        _error(code)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _error(code)
    if len(encoded) > maximum or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        _error(code)
    return value


def _tuple_of_text(
    value: object,
    *,
    code: str,
    maximum_items: int = MAX_LIST_ENTRIES,
    maximum_text: int = MAX_TEXT_BYTES,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > maximum_items or (not allow_empty and not value):
        _error(code)
    return tuple(
        _text(item, code=code, maximum=maximum_text)
        for item in value
    )


def _serialized_size(value: object, *, code: str) -> int:
    try:
        encoded = json.dumps(
            asdict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        _error(code)
    return len(encoded)


def _require_task_context(value: object) -> "TaskTurnContext":
    if type(value) is not TaskTurnContext:
        _error("invalid_context")
    repository = _text(value.repository, code="invalid_context", maximum=512)
    if repository.count("/") != 1:
        _error("invalid_context")
    owner, name = repository.split("/", 1)
    if not _REPOSITORY_PART.fullmatch(owner) or not _REPOSITORY_PART.fullmatch(name):
        _error("invalid_context")
    if type(value.task) is not str or not _TASK.fullmatch(value.task):
        _error("invalid_context")
    branch = _text(value.branch_ref, code="invalid_context", maximum=1024)
    if not branch.startswith("refs/heads/") or branch.endswith(("/", ".")):
        _error("invalid_context")
    if ".." in branch or "@{" in branch or any(
        ord(ch) < 0x21 or ord(ch) == 0x7F or ch in "~^:?*[\\" for ch in branch
    ):
        _error("invalid_context")
    if any(
        not part or part.startswith(".") or part.endswith(".lock")
        for part in branch.split("/")
    ):
        _error("invalid_context")
    for oid in (value.head, value.tree):
        if type(oid) is not str or not _OID.fullmatch(oid):
            _error("invalid_context")
    if len(value.head) != len(value.tree):
        _error("invalid_context")
    if any(type(item) is not str or not _HEX_64.fullmatch(item) for item in (
        value.record_id, value.contract_id, value.view_id,
    )):
        _error("invalid_context")
    if type(value.turn_id) is not str or not _TURN_ID.fullmatch(value.turn_id):
        _error("invalid_context")
    if value.turn_id in {".", ".."}:
        _error("invalid_context")
    return value


@dataclass(frozen=True)
class TaskTurnContext:
    """Immutable descriptive binding to one host-observed Task turn."""

    repository: str
    task: str
    branch_ref: str
    turn_id: str
    record_id: str
    contract_id: str
    head: str
    tree: str
    view_id: str

    def __post_init__(self) -> None:
        _require_task_context(self)


def _validate_role(value: object) -> "LeafRole":
    if type(value) is not LeafRole:
        _error("invalid_role")
    if type(value.role) is not str or value.role not in LEAF_ROLES:
        _error("invalid_role")
    for item in (value.provider, value.model, value.executor_ref):
        _text(item, code="invalid_role", maximum=256)
    if type(value.read_only) is not bool or type(value.may_delegate) is not bool:
        _error("invalid_role")
    if value.may_delegate:
        _error("delegation_forbidden")
    if value.role != "general" and not value.read_only:
        _error("invalid_role")
    return value


@dataclass(frozen=True)
class LeafRole:
    """An exact host-installed leaf binding, never a caller-selected model."""

    role: str
    provider: str
    model: str
    executor_ref: str
    read_only: bool
    may_delegate: bool = False

    def __post_init__(self) -> None:
        _validate_role(self)


def _strict_positive_int(value: object, *, maximum: int, code: str) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        _error(code)
    return value


@dataclass(frozen=True)
class UnitBudget:
    """Finite hard maxima for one ephemeral ticket."""

    max_seconds: int = 300
    max_output_bytes: int = 32768
    max_context_bytes: int = 65536

    def __post_init__(self) -> None:
        _strict_positive_int(self.max_seconds, maximum=MAX_UNIT_SECONDS, code="invalid_budget")
        _strict_positive_int(
            self.max_output_bytes, maximum=MAX_UNIT_OUTPUT_BYTES, code="invalid_budget",
        )
        if self.max_output_bytes < MIN_UNIT_OUTPUT_BYTES:
            # A terminal provider/protocol failure must fit its bounded
            # control-plane result; otherwise a ticket cannot record BLOCKED.
            _error("invalid_budget")
        _strict_positive_int(
            self.max_context_bytes, maximum=MAX_UNIT_CONTEXT_BYTES, code="invalid_budget",
        )


@dataclass(frozen=True)
class TurnBudget:
    """Finite per-turn ticket, concurrency, and lifetime dispatch limits."""

    max_units: int = 32
    max_running: int = 4
    max_dispatches: int = 32

    def __post_init__(self) -> None:
        _strict_positive_int(self.max_units, maximum=MAX_TURN_UNITS, code="invalid_budget")
        _strict_positive_int(self.max_running, maximum=MAX_TURN_UNITS, code="invalid_budget")
        _strict_positive_int(
            self.max_dispatches, maximum=MAX_TURN_DISPATCHES, code="invalid_budget",
        )
        if self.max_running > self.max_units:
            _error("invalid_budget")


def _scope_path(value: object, *, code: str, allow_root: bool) -> str:
    path = _text(value, code=code, maximum=MAX_SCOPE_PATH_BYTES)
    if path == ".":
        if allow_root:
            return path
        _error(code)
    if path.startswith("/") or path.endswith("/") or "\\" in path:
        _error(code)
    segments = path.split("/")
    if any(not part or part in {".", ".."} or part.startswith("-") for part in segments):
        _error(code)
    # These characters are legal in some filenames, but accepting them here
    # makes accidental shell/pathspec use needlessly ambiguous. Tickets are
    # lexical descriptions; a host still performs its own path authorization.
    forbidden = set("*?[]{}$`'\";|&<>!#~:")
    if any(ch in forbidden for ch in path):
        _error(code)
    reserved = {".git", ".task-state", ".automation"}
    if any(part.lower() in reserved for part in segments):
        _error(code)
    return path


def _scope_tuple(value: object, *, code: str, allow_root: bool) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > MAX_SCOPE_ENTRIES:
        _error(code)
    paths = tuple(_scope_path(item, code=code, allow_root=allow_root) for item in value)
    if len(set(paths)) != len(paths):
        _error(code)
    return paths


def _unit_shape(unit: object) -> "WorkUnit":
    if type(unit) is not WorkUnit:
        _error("invalid_unit")
    if type(unit.unit_id) is not str or not _UNIT_ID.fullmatch(unit.unit_id):
        _error("invalid_unit")
    if unit.unit_id in {".", ".."}:
        _error("invalid_unit")
    _require_task_context(unit.context)
    _validate_role(unit.role)
    if type(unit.budget) is not UnitBudget:
        _error("invalid_budget")
    _text(unit.objective, code="invalid_unit", maximum=MAX_OBJECTIVE_BYTES)
    reads = _scope_tuple(unit.read_scope, code="invalid_read_scope", allow_root=True)
    edits = _scope_tuple(unit.edit_scope, code="invalid_edit_scope", allow_root=False)
    if not reads:
        _error("invalid_read_scope")
    if unit.role.read_only and edits:
        _error("read_only_role_has_edits")
    if any(not any(_path_contains(read, edit) for read in reads) for edit in edits):
        _error("edit_outside_read_scope")
    constraints = _tuple_of_text(
        unit.constraints, code="invalid_constraints", maximum_items=MAX_LIST_ENTRIES,
        maximum_text=4096,
    )
    outputs = _tuple_of_text(
        unit.expected_outputs, code="invalid_expected_outputs", maximum_items=MAX_LIST_ENTRIES,
        maximum_text=64,
    )
    if any(not _LABEL.fullmatch(label) for label in outputs) or len(set(outputs)) != len(outputs):
        _error("invalid_expected_outputs")
    stops = _tuple_of_text(
        unit.stop_conditions, code="invalid_stop_conditions", maximum_items=MAX_LIST_ENTRIES,
        maximum_text=4096,
    )
    dependencies = _tuple_of_text(
        unit.dependencies, code="invalid_dependencies", maximum_items=MAX_LIST_ENTRIES,
        maximum_text=64, allow_empty=True,
    )
    if any(not _UNIT_ID.fullmatch(dependency) for dependency in dependencies):
        _error("invalid_dependencies")
    if unit.unit_id in dependencies or len(set(dependencies)) != len(dependencies):
        _error("invalid_dependencies")
    # Count the full immutable handoff shape, not only its prose.
    if _serialized_size(unit, code="invalid_unit") > unit.budget.max_context_bytes:
        _error("context_budget_exceeded")
    return unit


@dataclass(frozen=True)
class WorkUnit:
    """A bounded, immutable leaf ticket; never an authorization token."""

    unit_id: str
    context: TaskTurnContext
    role: LeafRole
    objective: str
    read_scope: tuple[str, ...]
    edit_scope: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...] = ()
    budget: UnitBudget = UnitBudget()

    def __post_init__(self) -> None:
        _unit_shape(self)


@dataclass(frozen=True)
class OperationalRequest:
    """Descriptive request for independent host approval; it grants nothing."""

    operation_class: str
    operation_identity: str
    scope: tuple[str, ...]
    purpose: str
    evidence: tuple[str, ...]
    least_privilege: str
    safe_alternatives: tuple[str, ...]
    configured_authority: str

    def __post_init__(self) -> None:
        if type(self.operation_class) is not str or not _OPERATION_CLASS.fullmatch(self.operation_class):
            _error("invalid_operational_request")
        _text(self.operation_identity, code="invalid_operational_request", maximum=512)
        _tuple_of_text(
            self.scope, code="invalid_operational_request", maximum_items=MAX_SCOPE_ENTRIES,
            maximum_text=512,
        )
        _text(self.purpose, code="invalid_operational_request", maximum=4096)
        _tuple_of_text(
            self.evidence, code="invalid_operational_request", maximum_items=MAX_LIST_ENTRIES,
            maximum_text=4096,
        )
        _text(self.least_privilege, code="invalid_operational_request", maximum=4096)
        _tuple_of_text(
            self.safe_alternatives, code="invalid_operational_request",
            maximum_items=MAX_LIST_ENTRIES, maximum_text=4096,
        )
        _text(self.configured_authority, code="invalid_operational_request", maximum=4096)


@dataclass(frozen=True)
class DecisionRequest:
    """A typed human decision handoff, not a fabricated #216 decision view."""

    category: str
    question: str
    options: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.category) is not str or self.category not in DECISION_CATEGORIES:
            _error("invalid_decision_request")
        _text(self.question, code="invalid_decision_request", maximum=4096)
        options = _tuple_of_text(
            self.options, code="invalid_decision_request", maximum_items=MAX_LIST_ENTRIES,
            maximum_text=4096,
        )
        if len(options) < 2:
            _error("invalid_decision_request")


@dataclass(frozen=True)
class LeafOutput:
    """One bounded text metadata value; never file bytes or a filesystem write."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or not _LABEL.fullmatch(self.name):
            _error("invalid_output")
        _text(self.value, code="invalid_output", maximum=8192)


def _validate_leaf_result(value: object) -> "LeafResult":
    if type(value) is not LeafResult:
        _error("invalid_result")
    if type(value.unit_id) is not str or not _UNIT_ID.fullmatch(value.unit_id):
        _error("invalid_result")
    if type(value.outcome) is not str or value.outcome not in OUTCOMES:
        _error("invalid_result")
    _text(value.summary, code="invalid_result", maximum=4096)
    if type(value.outputs) is not tuple or len(value.outputs) > MAX_LIST_ENTRIES:
        _error("invalid_result")
    if any(type(output) is not LeafOutput for output in value.outputs):
        _error("invalid_result")
    names = tuple(output.name for output in value.outputs)
    if len(set(names)) != len(names):
        _error("duplicate_output")
    if value.approval is not None and type(value.approval) is not OperationalRequest:
        _error("invalid_result")
    if value.decision is not None and type(value.decision) is not DecisionRequest:
        _error("invalid_result")
    if value.failure_code is not None and (
        type(value.failure_code) is not str or not _CODE.fullmatch(value.failure_code)
    ):
        _error("invalid_result")
    if value.outcome == "NEEDS_APPROVAL":
        if value.approval is None or value.decision is not None or value.failure_code is not None:
            _error("invalid_result")
    elif value.outcome == "NEEDS_DECISION":
        if value.decision is None or value.approval is not None or value.failure_code is not None:
            _error("invalid_result")
    elif value.approval is not None or value.decision is not None:
        _error("invalid_result")
    if value.outcome == "COMPLETED" and value.failure_code is not None:
        _error("invalid_result")
    return value


def _result_size(result: LeafResult) -> int:
    """Bound the serialized terminal metadata, including typed handoffs."""
    return _serialized_size(result, code="invalid_result")


@dataclass(frozen=True)
class LeafResult:
    """Exactly one terminal outcome correlated to one ticket ID."""

    unit_id: str
    outcome: str
    summary: str
    outputs: tuple[LeafOutput, ...] = ()
    approval: OperationalRequest | None = None
    decision: DecisionRequest | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _validate_leaf_result(self)


def _path_contains(prefix: str, path: str) -> bool:
    return prefix == "." or path == prefix or path.startswith(prefix + "/")


def _path_sets_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return any(_path_contains(a, b) or _path_contains(b, a) for a in left for b in right)


def _units_conflict(left: WorkUnit, right: WorkUnit) -> bool:
    return (
        _path_sets_overlap(left.edit_scope, right.edit_scope)
        or _path_sets_overlap(left.edit_scope, right.read_scope)
        or _path_sets_overlap(right.edit_scope, left.read_scope)
    )


class TurnCoordinator:
    """In-memory ticket scheduler with a single serialized host owner.

    Control-plane calls must be serialized by that owner; this is not a
    multi-threaded executor. Leaves may run concurrently under their qualified
    host adapter, but no method here dispatches or launches a leaf.
    """

    def __init__(
        self,
        context: TaskTurnContext,
        roles: tuple[LeafRole, ...],
        budget: TurnBudget = TurnBudget(),
    ) -> None:
        _require_task_context(context)
        if type(roles) is not tuple or not roles or len(roles) > len(LEAF_ROLES):
            _error("invalid_role_roster")
        for role in roles:
            _validate_role(role)
        if len({role.role for role in roles}) != len(roles):
            _error("invalid_role_roster")
        if type(budget) is not TurnBudget:
            _error("invalid_budget")
        self._context: TaskTurnContext | None = context
        self._roles = roles
        self._budget = budget
        self._units: dict[str, WorkUnit] = {}
        self._states: dict[str, str] = {}
        self._results: dict[str, LeafResult] = {}
        self._dispatches = 0
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            _error("turn_discarded")

    def _depends_on(self, unit: WorkUnit, target_id: str) -> bool:
        pending = list(unit.dependencies)
        visited: set[str] = set()
        while pending:
            dependency = pending.pop()
            if dependency == target_id:
                return True
            if dependency in visited:
                continue
            visited.add(dependency)
            prior = self._units.get(dependency)
            if prior is not None:
                pending.extend(prior.dependencies)
        return False

    def validate_unit(self, unit: WorkUnit) -> None:
        """Validate ticket shape, turn/role binding, dependencies, and conflicts."""
        self._ensure_open()
        _unit_shape(unit)
        if unit.context != self._context:
            _error("turn_context_mismatch")
        if not any(unit.role == installed for installed in self._roles):
            _error("role_not_installed")
        if unit.unit_id in self._units:
            _error("duplicate_unit_id")
        if len(self._units) >= self._budget.max_units:
            _error("max_units_exceeded")
        for dependency in unit.dependencies:
            if dependency not in self._units:
                _error("missing_dependency")
        for prior in self._units.values():
            if (
                self._states[prior.unit_id] not in {"completed", "terminal"}
                and not self._dependency_failed(prior)
                and _units_conflict(prior, unit)
                and not self._depends_on(unit, prior.unit_id)
            ):
                _error("scope_conflict")

    def _dependency_failed(self, unit: WorkUnit) -> bool:
        """An unstartable dependent must not reserve scope for a correction.

        Keep the dependent pending/unresolved; do not fabricate a Leaf outcome
        or reopen its terminal predecessor. The older-ID DAG is bounded.
        """
        pending = list(unit.dependencies)
        seen: set[str] = set()
        while pending:
            dependency = pending.pop()
            if dependency in seen:
                continue
            seen.add(dependency)
            if self._states[dependency] == "terminal":
                return True
            pending.extend(self._units[dependency].dependencies)
        return False

    def add(self, unit: WorkUnit) -> None:
        """Register one ticket; identifiers remain consumed until discard."""
        self.validate_unit(unit)
        self._units[unit.unit_id] = unit
        self._states[unit.unit_id] = "pending"

    def _dependencies_completed(self, unit: WorkUnit) -> bool:
        return all(self._states.get(item) == "completed" for item in unit.dependencies)

    def _running(self) -> tuple[WorkUnit, ...]:
        return tuple(
            unit for unit_id, unit in self._units.items()
            if self._states[unit_id] == "running"
        )

    def _can_start(self, unit: WorkUnit) -> bool:
        if not self._dependencies_completed(unit):
            return False
        return not any(_units_conflict(unit, running) for running in self._running())

    def ready(self) -> tuple[WorkUnit, ...]:
        """Return currently dispatchable tickets in creation order."""
        if self._closed:
            return ()
        slots = min(
            self._budget.max_running - len(self._running()),
            self._budget.max_dispatches - self._dispatches,
        )
        if slots <= 0:
            return ()
        ready: list[WorkUnit] = []
        for unit_id, unit in self._units.items():
            if self._states[unit_id] == "pending" and self._can_start(unit):
                ready.append(unit)
                if len(ready) == slots:
                    break
        return tuple(ready)

    def start(self, unit_id: str) -> WorkUnit:
        """Mark one ready ticket running and consume one lifetime dispatch."""
        self._ensure_open()
        if type(unit_id) is not str or not _UNIT_ID.fullmatch(unit_id):
            _error("invalid_unit_id")
        unit = self._units.get(unit_id)
        if unit is None or self._states[unit_id] != "pending":
            _error("unit_not_ready")
        if self._dispatches >= self._budget.max_dispatches:
            _error("dispatch_limit_reached")
        if len(self._running()) >= self._budget.max_running:
            _error("max_running_reached")
        if not self._can_start(unit):
            _error("unit_not_ready")
        self._states[unit_id] = "running"
        self._dispatches += 1
        return unit

    def validate_result(self, result: LeafResult, unit: WorkUnit) -> None:
        """Validate one immutable result against its registered ticket."""
        self._ensure_open()
        _validate_leaf_result(result)
        if type(unit) is not WorkUnit or self._units.get(unit.unit_id) != unit:
            _error("unknown_unit")
        if result.unit_id != unit.unit_id:
            _error("result_unit_mismatch")
        expected = set(unit.expected_outputs)
        names = {output.name for output in result.outputs}
        if not names <= expected:
            _error("unexpected_output")
        if result.outcome == "COMPLETED" and names != expected:
            _error("missing_output")
        if _result_size(result) > unit.budget.max_output_bytes:
            _error("output_budget_exceeded")

    def finish(self, result: LeafResult) -> None:
        """Store a terminal result for a running ticket; no evidence is created."""
        self._ensure_open()
        _validate_leaf_result(result)
        unit = self._units.get(result.unit_id)
        if unit is None:
            _error("unknown_unit")
        if self._states[result.unit_id] != "running":
            _error("unit_not_running")
        self.validate_result(result, unit)
        self._results[result.unit_id] = result
        self._states[result.unit_id] = (
            "completed" if result.outcome == "COMPLETED" else "terminal"
        )

    def result(self, unit_id: str) -> LeafResult | None:
        if type(unit_id) is not str or not _UNIT_ID.fullmatch(unit_id):
            _error("invalid_unit_id")
        return self._results.get(unit_id)

    def units(self) -> tuple[WorkUnit, ...]:
        return tuple(self._units.values())

    def discard(self) -> None:
        """Forget this turn's ephemeral state and permanently close this coordinator."""
        self._units.clear()
        self._states.clear()
        self._results.clear()
        self._dispatches = 0
        self._context = None
        self._roles = ()
        self._closed = True


@dataclass(frozen=True)
class IssueCandidate:
    """Typed host-supplied descriptive Issue facts; not an Issue authority."""

    repository: str
    number: int
    state: str
    eligibility: str
    fact_id: str

    def __post_init__(self) -> None:
        repo = _text(self.repository, code="invalid_issue_candidate", maximum=512)
        if repo.count("/") != 1:
            _error("invalid_issue_candidate")
        owner, name = repo.split("/", 1)
        if not _REPOSITORY_PART.fullmatch(owner) or not _REPOSITORY_PART.fullmatch(name):
            _error("invalid_issue_candidate")
        _strict_positive_int(self.number, maximum=2**63 - 1, code="invalid_issue_candidate")
        if type(self.state) is not str or self.state not in {"open", "closed"}:
            _error("invalid_issue_candidate")
        if type(self.eligibility) is not str or self.eligibility not in {
            "eligible", "ineligible", "unknown",
        }:
            _error("invalid_issue_candidate")
        if type(self.fact_id) is not str or not _HEX_64.fullmatch(self.fact_id):
            _error("invalid_issue_candidate")


@dataclass(frozen=True)
class NextSelection:
    """Pure descriptive next-issue result; never marks an Issue READY."""

    kind: str
    issue: int | None
    candidates: tuple[int, ...]
    reason_code: str

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in {
            "explicit", "selected", "none", "needs_decision",
        }:
            _error("invalid_selection")
        if self.issue is not None:
            _strict_positive_int(self.issue, maximum=2**63 - 1, code="invalid_selection")
        if type(self.candidates) is not tuple or len(self.candidates) > MAX_CANDIDATES:
            _error("invalid_selection")
        for number in self.candidates:
            _strict_positive_int(number, maximum=2**63 - 1, code="invalid_selection")
        if len(set(self.candidates)) != len(self.candidates):
            _error("invalid_selection")
        if type(self.reason_code) is not str or not _CODE.fullmatch(self.reason_code):
            _error("invalid_selection")
        if (self.kind in {"explicit", "selected"}) != (self.issue is not None):
            _error("invalid_selection")
        if self.kind == "none" and self.candidates:
            _error("invalid_selection")


def _selection(kind: str, issue: int | None, candidates: tuple[int, ...], reason: str) -> NextSelection:
    return NextSelection(kind, issue, candidates, reason)


def select_next_issue(
    repository: str,
    candidates: tuple[IssueCandidate, ...],
    *,
    explicit_issue: int | None = None,
    complete: bool = True,
) -> NextSelection:
    """Select a descriptive Issue only from exact typed host observations.

    An explicit open Issue is returned even if eligibility is unknown or
    ineligible, with that fact represented in ``reason_code``. Autonomous
    selection requires a complete feed, exactly one eligible open candidate,
    and no open candidate with unknown eligibility. Human semantic priority is
    deliberately not inferred from issue number or feed ordering.
    """
    try:
        repo = _text(repository, code="invalid_repository", maximum=512)
        if repo.count("/") != 1:
            _error("invalid_repository")
        owner, name = repo.split("/", 1)
        if not _REPOSITORY_PART.fullmatch(owner) or not _REPOSITORY_PART.fullmatch(name):
            _error("invalid_repository")
    except WorkUnitError:
        return _selection("needs_decision", None, (), "invalid_repository")
    if type(complete) is not bool:
        return _selection("needs_decision", None, (), "invalid_feed_completeness")
    if type(candidates) is not tuple or len(candidates) > MAX_CANDIDATES:
        return _selection("needs_decision", None, (), "invalid_candidate_feed")
    numbers: set[int] = set()
    candidate_values: list[IssueCandidate] = []
    for candidate in candidates:
        if type(candidate) is not IssueCandidate or candidate.repository != repo:
            return _selection("needs_decision", None, (), "invalid_candidate_feed")
        if candidate.number in numbers:
            return _selection("needs_decision", None, (), "duplicate_candidate")
        numbers.add(candidate.number)
        candidate_values.append(candidate)
    if explicit_issue is not None:
        if type(explicit_issue) is not int or explicit_issue <= 0:
            return _selection("needs_decision", None, (), "invalid_explicit_issue")
        matches = tuple(item for item in candidate_values if item.number == explicit_issue)
        if len(matches) != 1:
            return _selection("needs_decision", None, (), "explicit_issue_not_observed")
        match = matches[0]
        if match.state != "open":
            return _selection("needs_decision", None, (), "explicit_issue_not_open")
        reason = {
            "eligible": "explicit_eligible",
            "ineligible": "explicit_ineligible",
            "unknown": "explicit_eligibility_unknown",
        }[match.eligibility]
        return _selection("explicit", match.number, (match.number,), reason)
    if not complete:
        return _selection("needs_decision", None, (), "candidate_feed_incomplete")
    open_eligible = tuple(
        item.number for item in candidate_values
        if item.state == "open" and item.eligibility == "eligible"
    )
    open_unknown = tuple(
        item.number for item in candidate_values
        if item.state == "open" and item.eligibility == "unknown"
    )
    if open_unknown:
        return _selection(
            "needs_decision", None, open_eligible + open_unknown,
            "candidate_eligibility_unknown",
        )
    if len(open_eligible) > 1:
        return _selection(
            "needs_decision", None, open_eligible, "multiple_eligible_candidates",
        )
    if len(open_eligible) == 1:
        return _selection("selected", open_eligible[0], open_eligible, "sole_eligible_candidate")
    return _selection("none", None, (), "no_eligible_open_candidates")


__all__ = [
    "DECISION_CATEGORIES", "LEAF_ROLES", "OUTCOMES", "DecisionRequest",
    "IssueCandidate", "LeafOutput", "LeafResult", "LeafRole", "NextSelection",
    "OperationalRequest", "TaskTurnContext", "TurnBudget", "TurnCoordinator",
    "UnitBudget", "WorkUnit", "WorkUnitError", "select_next_issue",
]
