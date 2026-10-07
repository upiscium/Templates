"""Deterministic, operation-local prerequisite diagnoses for Agent Core v4.

This module is a read-only substrate.  A trusted host installs one immutable
operation policy and its read-only check capabilities, then supplies a
validated live Task View and the exact requested operation.  The evaluator
does not infer a default branch, remote, PR role, repair, lifecycle state, or
next action.  In particular, a factual Task View can be evaluated locally
without asking for GitHub facts unless the selected operation policy declares
a check which needs them.

Checks are trusted host callbacks, not credentials supplied by an operation
request.  They receive only immutable bindings and canonical bytes; a check
must authenticate and bound its own observations.  A diagnosis is descriptive
only: execution owners must recheck identity, freshness, authority, and locks
at the point of mutation.  This module performs no Git, network, metadata, or
product writes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

import metadata_codec as codec
import task_record
import task_view


SCHEMA_VERSION = 1
MAX_JSON_BYTES = codec.MAX_PAYLOAD_BYTES
MAX_JSON_DEPTH = codec.MAX_JSON_DEPTH
MAX_JSON_NODES = 100_000
MAX_EVIDENCE = task_view.MAX_SELECTED_EVIDENCE
MAX_POLICY_CHECKS = 64
MAX_CHECK_DATA_BYTES = 1024

OUTCOMES = frozenset({
    "SATISFIED",
    "MISSING_PREREQUISITE",
    "IDENTITY_CONFLICT",
    "AUTHORITY_CONFLICT",
    "STALE_SUBJECT",
    "SEMANTIC_DECISION_REQUIRED",
    "UNAVAILABLE_DEPENDENCY",
    "UNSAFE_TO_CONVERGE",
})
CONVERGENCE_CLASSES = frozenset({"none", "mechanical", "unsafe", "semantic"})
DIAGNOSIS_RESULTS = OUTCOMES - {"SATISFIED"} | {"PREREQUISITES_SATISFIED"}

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_OWNER = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z", re.ASCII)
_OPERATION = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z", re.ASCII)
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_REQUEST_DOMAIN = b"agentcore-operation-request/v1\n"
_POLICY_DOMAIN = b"agentcore-operation-policy/v1\n"
_VIEW_DOMAIN = b"agentcore-live-task-view-fingerprint/v1\n"

# This explicit order makes the top-level result stable if multiple checks
# fail.  The complete ordered report remains available in ``checks``.
_FAILURE_PRECEDENCE = (
    "IDENTITY_CONFLICT",
    "AUTHORITY_CONFLICT",
    "STALE_SUBJECT",
    "SEMANTIC_DECISION_REQUIRED",
    "UNSAFE_TO_CONVERGE",
    "UNAVAILABLE_DEPENDENCY",
    "MISSING_PREREQUISITE",
)

_DIAGNOSIS_FIELDS = frozenset({
    "schema_version", "repository", "task", "branch_ref", "subject",
    "operation", "request_id", "policy_id", "view_id", "policy_operation",
    "policy_owner", "authority_mode", "required_checks", "evidence",
    "result", "reason_code", "precheck",
    "human_decision_required", "mechanical_convergence_candidate",
    "human_authority_required", "checks",
})
_CHECK_DEFINITION_FIELDS = frozenset({"check_id", "owner", "category"})
_PRECHECK_FIELDS = frozenset({"outcome", "reason_code", "observed", "expected"})
_CHECK_REPORT_FIELDS = frozenset({
    "check_id", "owner", "category", "outcome", "reason_code",
    "observed", "expected", "convergence",
})
_SYNTHETIC_REASONS = frozenset({
    "invalid_check_result", "check_binding_mismatch",
    "check_unavailable", "check_callback_failed",
})


class OperationPrerequisiteError(ValueError):
    """A safe bounded validation code; never includes callback detail."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or not _IDENTIFIER.fullmatch(code):
            code = "operation_prerequisite_error"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OperationRequest:
    """Exact requested operation and opaque, operation-owned JSON parameters."""

    repository: str
    task: str
    branch_ref: str
    subject: str
    operation: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class CheckSpec:
    """One policy-owned prerequisite check, sorted by its stable check ID."""

    check_id: str
    owner: str
    category: str | None = None


@dataclass(frozen=True)
class OperationPolicy:
    """Closed policy for exactly one operation; no cross-operation defaults."""

    operation: str
    owner: str
    authority_mode: str
    required_checks: tuple[CheckSpec, ...]


@dataclass(frozen=True)
class CheckBinding:
    """Immutable exact binding echoed by a check result."""

    repository: str
    task: str
    branch_ref: str
    subject: str
    operation: str
    request_id: str
    policy_id: str
    view_id: str
    check_id: str
    check_owner: str


@dataclass(frozen=True)
class CheckContext:
    """Read-only callback input; all potentially mutable inputs are bytes."""

    binding: CheckBinding
    request_bytes: bytes
    policy_bytes: bytes
    view_bytes: bytes


@dataclass(frozen=True)
class CheckResult:
    """Typed factual result from a policy-selected trusted check callback."""

    binding: CheckBinding
    outcome: str
    reason_code: str
    observed: dict[str, Any]
    expected: dict[str, Any]
    convergence: str = "none"


CheckCallback = Callable[[CheckContext], object]


def _identifier(value: object, code: str = "invalid_identifier") -> str:
    if type(value) is not str or len(value) > 64 or not _IDENTIFIER.fullmatch(value):
        raise OperationPrerequisiteError(code)
    return value


def _operation_name(value: object, code: str = "invalid_operation") -> str:
    if type(value) is not str or len(value) > 64 or not _OPERATION.fullmatch(value):
        raise OperationPrerequisiteError(code)
    return value


def _owner_name(value: object, code: str) -> str:
    if type(value) is not str or len(value) > 64 or not _OWNER.fullmatch(value):
        raise OperationPrerequisiteError(code)
    return value


def _json_value(value: object, depth: int = 0, budget: list[int] | None = None) -> None:
    """Accept the shared codec's bounded JSON value domain, with no floats."""
    if budget is None:
        budget = [MAX_JSON_NODES, MAX_JSON_BYTES]
    budget[0] -= 1
    if budget[0] < 0:
        raise OperationPrerequisiteError("json_node_limit_exceeded")
    if depth > MAX_JSON_DEPTH:
        raise OperationPrerequisiteError("json_depth_exceeded")
    value_type = type(value)
    def account(size: int) -> None:
        budget[1] -= size
        if budget[1] < 0:
            raise OperationPrerequisiteError("json_size_exceeded")

    if value is None or value_type is bool:
        account(4 if value is None or value is True else 5)
        return
    if value_type is int:
        if not -(2**63) <= value <= 2**63 - 1:
            raise OperationPrerequisiteError("json_integer_out_of_range")
        account(len(str(value)))
        return
    if value_type is str:
        if len(value) > MAX_JSON_BYTES:
            raise OperationPrerequisiteError("json_string_too_long")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise OperationPrerequisiteError("invalid_json_string")
        # Charge escaped UTF-8 bytes during traversal, before assembling the
        # whole document. Many individually small strings cannot amplify the
        # later serializer into an unbounded aggregate allocation.
        account(len(json.dumps(value, ensure_ascii=False).encode("utf-8")))
        return
    if value_type is list:
        account(2 + max(0, len(value) - 1))
        for item in value:
            _json_value(item, depth + 1, budget)
        return
    if value_type is dict:
        account(2 + len(value) + max(0, len(value) - 1))
        for key, item in value.items():
            if type(key) is not str:
                raise OperationPrerequisiteError("invalid_json_key")
            _json_value(key, budget=budget)
            _json_value(item, depth + 1, budget)
        return
    raise OperationPrerequisiteError("unsupported_json_value")


def _canonical_json(value: object, code: str = "invalid_json", *, initial_depth: int = 0) -> bytes:
    try:
        _json_value(value, initial_depth)
        data = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except OperationPrerequisiteError:
        raise
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise OperationPrerequisiteError(code) from None
    if len(data) > MAX_JSON_BYTES:
        raise OperationPrerequisiteError("json_size_exceeded")
    return data


def _detached_json(data: bytes) -> Any:
    """Decode bytes produced locally, yielding a detached JSON-only tree."""
    try:
        return json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise OperationPrerequisiteError("invalid_canonical_json") from None


def _validate_identity(repository: object, task: object, subject: object) -> None:
    # The shared public codec owns these grammars. Encoding here is pure and
    # ephemeral; it does not create or publish an Evidence metadata object.
    if (
        type(repository) is not str or len(repository) > codec.MAX_REPOSITORY_LENGTH
        or type(task) is not str or len(task) > codec.MAX_TASK_LENGTH
        or type(subject) is not str or len(subject) not in {40, 64}
    ):
        raise OperationPrerequisiteError("invalid_task_identity")
    try:
        codec.encode_object("evidence", repository, task, subject, {})
    except (codec.MetadataCodecError, TypeError, ValueError):
        raise OperationPrerequisiteError("invalid_task_identity") from None


def _validate_branch(branch_ref: object) -> str:
    if type(branch_ref) is not str or len(branch_ref) > task_view.MAX_BRANCH_REF_LENGTH:
        raise OperationPrerequisiteError("invalid_branch_ref")
    try:
        value = task_record.validate_branch_ref(branch_ref)
    except (task_record.TaskRecordError, TypeError, ValueError):
        raise OperationPrerequisiteError("invalid_branch_ref") from None
    if len(value) > task_view.MAX_BRANCH_REF_LENGTH:
        raise OperationPrerequisiteError("invalid_branch_ref")
    return value


def _check_spec_value(spec: object) -> dict[str, Any]:
    if type(spec) is not CheckSpec:
        raise OperationPrerequisiteError("invalid_check_spec")
    check_id = _identifier(spec.check_id, "invalid_check_id")
    owner = _owner_name(spec.owner, "invalid_check_owner")
    if spec.category is not None:
        category = _identifier(spec.category, "invalid_check_category")
    else:
        category = None
    return {"check_id": check_id, "owner": owner, "category": category}


def _policy_wire(policy: object) -> tuple[dict[str, Any], bytes]:
    if type(policy) is not OperationPolicy:
        raise OperationPrerequisiteError("invalid_operation_policy")
    operation = _operation_name(policy.operation)
    owner = _owner_name(policy.owner, "invalid_policy_owner")
    if (
        type(policy.authority_mode) is not str
        or len(policy.authority_mode) > 16
        or policy.authority_mode not in {"execution", "human_owned"}
    ):
        raise OperationPrerequisiteError("invalid_authority_mode")
    if (
        type(policy.required_checks) is not tuple
        or not policy.required_checks
        or len(policy.required_checks) > MAX_POLICY_CHECKS
    ):
        raise OperationPrerequisiteError("invalid_required_checks")
    checks = [_check_spec_value(spec) for spec in policy.required_checks]
    ids = [item["check_id"] for item in checks]
    if len(ids) != len(set(ids)):
        raise OperationPrerequisiteError("duplicate_check_id")
    checks.sort(key=lambda item: item["check_id"])
    if policy.authority_mode == "human_owned" and not any(
        item["category"] == "human_authority" for item in checks
    ):
        raise OperationPrerequisiteError("human_authority_check_required")
    value = {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "owner": owner,
        "authority_mode": policy.authority_mode,
        "required_checks": checks,
    }
    return value, _canonical_json(value)


def _request_wire(request: object) -> tuple[dict[str, Any], bytes]:
    if type(request) is not OperationRequest:
        raise OperationPrerequisiteError("invalid_operation_request")
    _validate_identity(request.repository, request.task, request.subject)
    branch = _validate_branch(request.branch_ref)
    operation = _operation_name(request.operation)
    if type(request.parameters) is not dict:
        raise OperationPrerequisiteError("invalid_request_parameters")
    value = {
        "schema_version": SCHEMA_VERSION,
        "repository": request.repository,
        "task": request.task,
        "branch_ref": branch,
        "subject": request.subject,
        "operation": operation,
        "parameters": request.parameters,
    }
    data = _canonical_json(value, "invalid_operation_request")
    # Return only the detached JSON tree from this point onward.  The caller's
    # parameter dictionary is never passed into a policy callback.
    return _detached_json(data), data


def _validated_view(view: object) -> tuple[dict[str, Any], bytes, str]:
    if type(view) is not dict:
        raise OperationPrerequisiteError("invalid_live_task_view")
    raw_bytes = _canonical_json(view, "invalid_live_task_view")
    detached = _detached_json(raw_bytes)
    if type(detached) is not dict:
        raise OperationPrerequisiteError("invalid_live_task_view")
    try:
        repository = detached["repository"]
        task = detached["task"]
        subject = detached["subject"]
        _validate_identity(repository, task, subject)
        task_view.encode_snapshot(
            repository,
            task,
            subject,
            "turn-end",
            detached,
        )
    except OperationPrerequisiteError:
        raise
    except (KeyError, task_view.TaskViewError, TypeError, ValueError):
        raise OperationPrerequisiteError("invalid_live_task_view") from None
    canonical = _canonical_json(detached, "invalid_live_task_view")
    return detached, canonical, hashlib.sha256(_VIEW_DOMAIN + canonical).hexdigest()


def _safe_code(value: object) -> bool:
    return type(value) is str and len(value) <= 64 and _IDENTIFIER.fullmatch(value) is not None


def _check_json(value: object, code: str) -> bytes:
    # A report nests observations several levels inside the diagnosis wire
    # object; reserve that depth here so a later encoder cannot overflow.
    data = _canonical_json(value, code, initial_depth=8)
    if len(data) > MAX_CHECK_DATA_BYTES:
        raise OperationPrerequisiteError("check_data_size_exceeded")
    return data


def _valid_result_semantics(outcome: str, convergence: str, category: str | None) -> bool:
    if len(outcome) > 32 or len(convergence) > 16:
        return False
    if outcome not in OUTCOMES or convergence not in CONVERGENCE_CLASSES:
        return False
    if (convergence == "semantic") != (outcome == "SEMANTIC_DECISION_REQUIRED"):
        return False
    if (convergence == "unsafe") != (outcome == "UNSAFE_TO_CONVERGE"):
        return False
    if convergence == "mechanical" and outcome not in {"SATISFIED", "MISSING_PREREQUISITE"}:
        return False
    if category == "human_authority" and convergence == "mechanical":
        return False
    return True


def _check_report(spec: dict[str, Any], result: object, binding: CheckBinding) -> dict[str, Any]:
    """Validate and detach one callback result, mapping every bad claim closed."""
    failure = "AUTHORITY_CONFLICT"
    reason = "invalid_check_result"
    if type(result) is not CheckResult:
        outcome, reason, observed, expected, convergence = (
            failure, reason, {"result_valid": False}, {"result_valid": True}, "none",
        )
    elif type(result.binding) is not CheckBinding or any(
        type(getattr(result.binding, name)) is not str
        or len(getattr(result.binding, name)) != len(getattr(binding, name))
        or getattr(result.binding, name) != getattr(binding, name)
        for name in CheckBinding.__dataclass_fields__
    ):
        outcome, reason, observed, expected, convergence = (
            "IDENTITY_CONFLICT", "check_binding_mismatch",
            {"binding_matches": False}, {"binding_matches": True}, "none",
        )
    else:
        try:
            if (
                type(result.outcome) is not str
                or type(result.convergence) is not str
                or not _safe_code(result.reason_code)
                or result.reason_code in _SYNTHETIC_REASONS
                or type(result.observed) is not dict
                or type(result.expected) is not dict
                or not _valid_result_semantics(result.outcome, result.convergence, spec["category"])
            ):
                raise OperationPrerequisiteError("invalid_check_result")
            observed_bytes = _check_json(result.observed, "invalid_check_observation")
            expected_bytes = _check_json(result.expected, "invalid_check_expectation")
            observed = _detached_json(observed_bytes)
            expected = _detached_json(expected_bytes)
            outcome = result.outcome
            reason = result.reason_code
            convergence = result.convergence
        except (OperationPrerequisiteError, RecursionError):
            outcome, reason, observed, expected, convergence = (
                failure, "invalid_check_result", {"result_valid": False}, {"result_valid": True}, "none",
            )
    return {
        "check_id": spec["check_id"],
        "owner": spec["owner"],
        "category": spec["category"],
        "outcome": outcome,
        "reason_code": reason,
        "observed": observed,
        "expected": expected,
        "convergence": convergence,
    }


def _unavailable_report(spec: dict[str, Any], reason: str) -> dict[str, Any]:
    if reason == "check_unavailable":
        observed, expected = {"check_available": False}, {"check_available": True}
    else:
        observed, expected = {"check_completed": False}, {"check_completed": True}
    return {
        "check_id": spec["check_id"],
        "owner": spec["owner"],
        "category": spec["category"],
        "outcome": "UNAVAILABLE_DEPENDENCY",
        "reason_code": reason,
        "observed": observed,
        "expected": expected,
        "convergence": "none",
    }


def _aggregate(checks: list[dict[str, Any]]) -> tuple[str, str]:
    for outcome in _FAILURE_PRECEDENCE:
        for report in checks:
            if report["outcome"] == outcome:
                return outcome, report["reason_code"]
    return "PREREQUISITES_SATISFIED", "all_prerequisites_satisfied"


def _human_decision_required(checks: list[dict[str, Any]]) -> bool:
    return any(report["outcome"] == "SEMANTIC_DECISION_REQUIRED" for report in checks)


def _mechanical_convergence_candidate(checks: list[dict[str, Any]]) -> bool:
    if not checks or not any(report["convergence"] == "mechanical" for report in checks):
        return False
    unsafe_gap = {
        "IDENTITY_CONFLICT", "AUTHORITY_CONFLICT", "STALE_SUBJECT",
        "SEMANTIC_DECISION_REQUIRED", "UNAVAILABLE_DEPENDENCY", "UNSAFE_TO_CONVERGE",
    }
    if any(report["outcome"] in unsafe_gap for report in checks):
        return False
    return all(
        report["outcome"] == "SATISFIED"
        or (report["outcome"] == "MISSING_PREREQUISITE" and report["convergence"] == "mechanical")
        for report in checks
    )


def _validate_report(report: object, definition: dict[str, Any]) -> dict[str, Any]:
    if type(report) is not dict or report.keys() != _CHECK_REPORT_FIELDS:
        raise OperationPrerequisiteError("invalid_diagnosis_check")
    if any(report[field] != definition[field] for field in _CHECK_DEFINITION_FIELDS):
        raise OperationPrerequisiteError("diagnosis_check_binding_mismatch")
    outcome = report["outcome"]
    convergence = report["convergence"]
    if (
        type(outcome) is not str
        or type(convergence) is not str
        or not _valid_result_semantics(outcome, convergence, definition["category"])
        or not _safe_code(report["reason_code"])
        or type(report["observed"]) is not dict
        or type(report["expected"]) is not dict
    ):
        raise OperationPrerequisiteError("invalid_diagnosis_check")
    _check_json(report["observed"], "invalid_diagnosis_observation")
    _check_json(report["expected"], "invalid_diagnosis_expectation")
    synthetic = {
        "invalid_check_result": (
            "AUTHORITY_CONFLICT", {"result_valid": False}, {"result_valid": True},
        ),
        "check_binding_mismatch": (
            "IDENTITY_CONFLICT", {"binding_matches": False}, {"binding_matches": True},
        ),
        "check_unavailable": (
            "UNAVAILABLE_DEPENDENCY", {"check_available": False}, {"check_available": True},
        ),
        "check_callback_failed": (
            "UNAVAILABLE_DEPENDENCY", {"check_completed": False}, {"check_completed": True},
        ),
    }
    if report["reason_code"] in _SYNTHETIC_REASONS and (
        report["outcome"], report["observed"], report["expected"]
    ) != synthetic[report["reason_code"]]:
        raise OperationPrerequisiteError("invalid_synthetic_check_report")
    if report["reason_code"] in _SYNTHETIC_REASONS and any(
        type(flag) is not bool
        for fields in (report["observed"], report["expected"])
        for flag in fields.values()
    ):
        raise OperationPrerequisiteError("invalid_synthetic_check_report")
    return report


def _validate_precheck(value: dict[str, Any], policy_operation: str) -> tuple[str, str]:
    precheck = value["precheck"]
    if type(precheck) is not dict or precheck.keys() != _PRECHECK_FIELDS:
        raise OperationPrerequisiteError("invalid_precheck_schema")
    outcome = precheck["outcome"]
    reason = precheck["reason_code"]
    observed = precheck["observed"]
    expected = precheck["expected"]
    if type(observed) is not dict or type(expected) is not dict:
        raise OperationPrerequisiteError("invalid_precheck_observation")
    if type(outcome) is not str or len(outcome) > 32 or type(reason) is not str or len(reason) > 64:
        raise OperationPrerequisiteError("invalid_precheck_classification")
    _canonical_json(observed, "invalid_precheck_observation")
    _canonical_json(expected, "invalid_precheck_expectation")

    if reason == "request_view_identity_mismatch":
        required = {"repository", "task", "branch_ref"}
        if (
            outcome != "IDENTITY_CONFLICT"
            or observed.keys() != required
            or expected.keys() != required
            or expected != {
                "repository": value["repository"],
                "task": value["task"],
                "branch_ref": value["branch_ref"],
            }
            or observed == expected
        ):
            raise OperationPrerequisiteError("invalid_precheck_classification")
        _validate_identity(observed["repository"], observed["task"], value["subject"])
        _validate_branch(observed["branch_ref"])
    elif reason == "request_view_subject_mismatch":
        if (
            outcome != "STALE_SUBJECT"
            or observed.keys() != {"subject"}
            or expected.keys() != {"subject"}
            or expected.get("subject") != value["subject"]
            or observed.get("subject") == expected.get("subject")
        ):
            raise OperationPrerequisiteError("invalid_precheck_classification")
        if type(observed["subject"]) is not str or len(observed["subject"]) not in {40, 64} or not _OID.fullmatch(observed["subject"]):
            raise OperationPrerequisiteError("invalid_precheck_observation")
    elif reason == "policy_operation_mismatch":
        if (
            outcome != "AUTHORITY_CONFLICT"
            or observed.keys() != {"operation"}
            or expected.keys() != {"operation"}
            or observed.get("operation") != policy_operation
            or expected.get("operation") != value["operation"]
            or observed.get("operation") == expected.get("operation")
        ):
            raise OperationPrerequisiteError("invalid_precheck_classification")
    else:
        raise OperationPrerequisiteError("invalid_precheck_reason")

    if outcome not in {"IDENTITY_CONFLICT", "STALE_SUBJECT", "AUTHORITY_CONFLICT"}:
        raise OperationPrerequisiteError("invalid_precheck_classification")
    if not _safe_code(reason):
        raise OperationPrerequisiteError("invalid_precheck_reason")
    if value["result"] != outcome or value["reason_code"] != reason:
        raise OperationPrerequisiteError("precheck_aggregation_mismatch")
    return outcome, reason


def _validate_diagnosis(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise OperationPrerequisiteError("invalid_diagnosis_schema")
    _canonical_json(value, "invalid_diagnosis")
    if value.keys() != _DIAGNOSIS_FIELDS:
        raise OperationPrerequisiteError("invalid_diagnosis_schema")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise OperationPrerequisiteError("unsupported_diagnosis_schema")
    _validate_identity(value["repository"], value["task"], value["subject"])
    _validate_branch(value["branch_ref"])
    _operation_name(value["operation"])
    for field in ("request_id", "policy_id", "view_id"):
        if type(value[field]) is not str or len(value[field]) != 64 or not _HEX_64.fullmatch(value[field]):
            raise OperationPrerequisiteError("invalid_diagnosis_identity")
    if (
        type(value["authority_mode"]) is not str
        or len(value["authority_mode"]) > 16
        or value["authority_mode"] not in {"execution", "human_owned"}
    ):
        raise OperationPrerequisiteError("invalid_diagnosis_policy")
    policy_operation = _operation_name(value["policy_operation"], "invalid_diagnosis_policy")
    policy_owner = _owner_name(value["policy_owner"], "invalid_diagnosis_policy")
    definitions = value["required_checks"]
    if type(definitions) is not list or not definitions or len(definitions) > MAX_POLICY_CHECKS:
        raise OperationPrerequisiteError("invalid_diagnosis_policy")
    normalized_definitions = []
    for definition in definitions:
        if type(definition) is not dict or definition.keys() != _CHECK_DEFINITION_FIELDS:
            raise OperationPrerequisiteError("invalid_diagnosis_policy")
        normalized_definitions.append(_check_spec_value(CheckSpec(
            definition["check_id"], definition["owner"], definition["category"],
        )))
    definition_ids = [item["check_id"] for item in normalized_definitions]
    if definition_ids != sorted(definition_ids) or len(definition_ids) != len(set(definition_ids)):
        raise OperationPrerequisiteError("invalid_diagnosis_policy")
    if value["authority_mode"] == "human_owned" and not any(
        item["category"] == "human_authority" for item in normalized_definitions
    ):
        raise OperationPrerequisiteError("invalid_diagnosis_policy")
    policy_wire = {
        "schema_version": SCHEMA_VERSION,
        "operation": policy_operation,
        "owner": policy_owner,
        "authority_mode": value["authority_mode"],
        "required_checks": normalized_definitions,
    }
    policy_bytes = _canonical_json(policy_wire, "invalid_diagnosis_policy")
    expected_policy_id = hashlib.sha256(_POLICY_DOMAIN + policy_bytes).hexdigest()
    if value["policy_id"] != expected_policy_id:
        raise OperationPrerequisiteError("diagnosis_policy_binding_mismatch")

    evidence = value["evidence"]
    if type(evidence) is not list or len(evidence) > MAX_EVIDENCE:
        raise OperationPrerequisiteError("invalid_diagnosis_evidence")
    evidence_keys = []
    for selected in evidence:
        if type(selected) is not dict or selected.keys() != {"evidence_id", "subject"}:
            raise OperationPrerequisiteError("invalid_diagnosis_evidence")
        if (
            type(selected["evidence_id"]) is not str
            or len(selected["evidence_id"]) != 64
            or not _HEX_64.fullmatch(selected["evidence_id"])
        ):
            raise OperationPrerequisiteError("invalid_diagnosis_evidence")
        if (
            type(selected["subject"]) is not str
            or len(selected["subject"]) not in {40, 64}
            or not _OID.fullmatch(selected["subject"])
        ):
            raise OperationPrerequisiteError("invalid_diagnosis_evidence")
        evidence_keys.append((selected["evidence_id"], selected["subject"]))
    if evidence_keys != sorted(evidence_keys) or len({item[0] for item in evidence_keys}) != len(evidence_keys):
        raise OperationPrerequisiteError("invalid_diagnosis_evidence")

    reports_value = value["checks"]
    if type(reports_value) is not list:
        raise OperationPrerequisiteError("invalid_diagnosis_checks")
    precheck = value["precheck"]
    if precheck is None:
        if len(reports_value) != len(normalized_definitions):
            raise OperationPrerequisiteError("invalid_diagnosis_checks")
    elif reports_value:
        raise OperationPrerequisiteError("invalid_diagnosis_checks")
    reports = [
        _validate_report(report, definition)
        for report, definition in zip(reports_value, normalized_definitions)
    ]
    if type(value["result"]) is not str or len(value["result"]) > 32 or value["result"] not in DIAGNOSIS_RESULTS:
        raise OperationPrerequisiteError("invalid_diagnosis_result")
    if not _safe_code(value["reason_code"]):
        raise OperationPrerequisiteError("invalid_diagnosis_reason")
    for field in ("human_decision_required", "mechanical_convergence_candidate", "human_authority_required"):
        if type(value[field]) is not bool:
            raise OperationPrerequisiteError("invalid_diagnosis_flags")
    expected_human = any(item["category"] == "human_authority" for item in normalized_definitions)
    expected_human = expected_human or value["authority_mode"] == "human_owned"
    if value["human_authority_required"] is not expected_human:
        raise OperationPrerequisiteError("invalid_diagnosis_flags")
    if value["human_decision_required"] is not _human_decision_required(reports):
        raise OperationPrerequisiteError("invalid_diagnosis_flags")
    if value["mechanical_convergence_candidate"] is not _mechanical_convergence_candidate(reports):
        raise OperationPrerequisiteError("invalid_diagnosis_flags")
    if precheck is None:
        if value["operation"] != policy_operation:
            raise OperationPrerequisiteError("diagnosis_operation_binding_mismatch")
        result, reason = _aggregate(reports)
        if value["result"] != result or value["reason_code"] != reason:
            raise OperationPrerequisiteError("diagnosis_aggregation_mismatch")
    else:
        _validate_precheck(value, policy_operation)
    return value


def encode_diagnosis(diagnosis: object) -> bytes:
    """Validate and encode a closed diagnosis, not authenticate or authorize it."""
    _validate_diagnosis(diagnosis)
    return _canonical_json(diagnosis, "invalid_diagnosis")


class OperationPrerequisites:
    """Evaluate only one frozen policy's declared checks for one operation.

    ``checks`` is a trusted-host registry.  Extra entries are ignored, and only
    callbacks named by the immutable policy can ever be invoked.  Missing or
    failing dependencies become safe report codes, never exception text.
    """

    def __init__(self, policy: OperationPolicy, *, checks: dict[str, object]) -> None:
        policy_value, policy_bytes = _policy_wire(policy)
        if type(checks) is not dict:
            raise OperationPrerequisiteError("invalid_check_registry")
        self._policy_value = policy_value
        self._policy_bytes = policy_bytes
        self._policy_id = hashlib.sha256(_POLICY_DOMAIN + policy_bytes).hexdigest()
        # Copy only selected callbacks. Mutating the host's registry later does
        # not replace a capability already installed in this evaluator.
        self._checks = {
            spec["check_id"]: checks.get(spec["check_id"])
            if callable(checks.get(spec["check_id"])) else None
            for spec in policy_value["required_checks"]
        }

    def _diagnosis(
        self,
        request: dict[str, Any],
        view: dict[str, Any],
        view_id: str,
        request_id: str,
        *,
        checks: list[dict[str, Any]],
        precheck: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Each result gets an independent copy so mutating a returned diagnosis
        # cannot alter the evaluator's installed policy snapshot.
        definitions = _detached_json(self._policy_bytes)["required_checks"]
        if precheck is None:
            result, reason = _aggregate(checks)
        else:
            result, reason = precheck["outcome"], precheck["reason_code"]
        evidence = [
            {"evidence_id": item["evidence_id"], "subject": item["subject"]}
            for item in view["evidence"]
        ]
        diagnosis = {
            "schema_version": SCHEMA_VERSION,
            "repository": request["repository"],
            "task": request["task"],
            "branch_ref": request["branch_ref"],
            "subject": request["subject"],
            "operation": request["operation"],
            "request_id": request_id,
            "policy_id": self._policy_id,
            "view_id": view_id,
            "policy_operation": self._policy_value["operation"],
            "policy_owner": self._policy_value["owner"],
            "authority_mode": self._policy_value["authority_mode"],
            "required_checks": definitions,
            "evidence": evidence,
            "result": result,
            "reason_code": reason,
            "precheck": precheck,
            "human_decision_required": _human_decision_required(checks),
            "mechanical_convergence_candidate": _mechanical_convergence_candidate(checks),
            "human_authority_required": (
                self._policy_value["authority_mode"] == "human_owned"
                or any(item["category"] == "human_authority" for item in definitions)
            ),
            "checks": checks,
        }
        _validate_diagnosis(diagnosis)
        _canonical_json(diagnosis, "invalid_diagnosis")
        return diagnosis

    def diagnose(self, request: OperationRequest, view: dict[str, Any]) -> dict[str, Any]:
        """Return a deterministic diagnosis; invoke only declared checks.

        Request, policy, and view JSON are canonicalized and detached before
        the first callback.  Binding conflicts are operation-local diagnoses
        and do not trigger callbacks against mismatched facts.
        """
        request_value, request_bytes = _request_wire(request)
        request_id = hashlib.sha256(_REQUEST_DOMAIN + request_bytes).hexdigest()
        view_value, view_bytes, view_id = _validated_view(view)

        if (
            request_value["repository"] != view_value["repository"]
            or request_value["task"] != view_value["task"]
            or request_value["branch_ref"] != view_value["branch_ref"]
        ):
            return self._diagnosis(
                request_value, view_value, view_id, request_id, checks=[],
                precheck={
                    "outcome": "IDENTITY_CONFLICT",
                    "reason_code": "request_view_identity_mismatch",
                    "observed": {
                        "repository": view_value["repository"],
                        "task": view_value["task"],
                        "branch_ref": view_value["branch_ref"],
                    },
                    "expected": {
                        "repository": request_value["repository"],
                        "task": request_value["task"],
                        "branch_ref": request_value["branch_ref"],
                    },
                },
            )
        if request_value["subject"] != view_value["subject"]:
            return self._diagnosis(
                request_value, view_value, view_id, request_id, checks=[],
                precheck={
                    "outcome": "STALE_SUBJECT",
                    "reason_code": "request_view_subject_mismatch",
                    "observed": {"subject": view_value["subject"]},
                    "expected": {"subject": request_value["subject"]},
                },
            )
        if request_value["operation"] != self._policy_value["operation"]:
            return self._diagnosis(
                request_value, view_value, view_id, request_id, checks=[],
                precheck={
                    "outcome": "AUTHORITY_CONFLICT",
                    "reason_code": "policy_operation_mismatch",
                    "observed": {"operation": self._policy_value["operation"]},
                    "expected": {"operation": request_value["operation"]},
                },
            )

        reports: list[dict[str, Any]] = []
        for spec in self._policy_value["required_checks"]:
            binding = CheckBinding(
                repository=request_value["repository"],
                task=request_value["task"],
                branch_ref=request_value["branch_ref"],
                subject=request_value["subject"],
                operation=request_value["operation"],
                request_id=request_id,
                policy_id=self._policy_id,
                view_id=view_id,
                check_id=spec["check_id"],
                check_owner=spec["owner"],
            )
            callback = self._checks[spec["check_id"]]
            if callback is None:
                reports.append(_unavailable_report(spec, "check_unavailable"))
                continue
            context = CheckContext(binding, request_bytes, self._policy_bytes, view_bytes)
            try:
                result = callback(context)
            except Exception:
                reports.append(_unavailable_report(spec, "check_callback_failed"))
                continue
            reports.append(_check_report(spec, result, binding))
        return self._diagnosis(request_value, view_value, view_id, request_id, checks=reports)
