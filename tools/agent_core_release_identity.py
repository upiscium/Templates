"""Pure validation for the planned Agent Core/Templates release identity.

Callers are responsible for verifying the exact tracked Git blob before parsing
the contract. This module deliberately performs no filesystem or subprocess
operations so a release gate can import it after that verification.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


CONTRACT_SCHEMA = "agent-core-release-identity"
CONTRACT_SCHEMA_VERSION = 1
AGENT_CORE_ARCHITECTURE = "v4"
FIRST_AGENT_CORE_VERSION = "1.0.0"
FIRST_TEMPLATES_VERSION = "4.0.0"
IDENTITY_KEYS = (
    "agentCoreVersion",
    "agentCorePayload",
    "templatesVersion",
    "templatesSourceRevision",
)

_SEMVER_RE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z", re.ASCII
)
_GIT_IDENTITY_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_ALLOWED_STATUSES = frozenset({"planned", "candidate", "released"})


class ReleaseIdentityError(ValueError):
    """A malformed or release-ineligible identity contract/evidence value."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseIdentityError(f"release identity JSON has duplicate key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ReleaseIdentityError(f"release identity JSON contains invalid constant: {value}")


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReleaseIdentityError(f"{label} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, object], keys: tuple[str, ...], label: str) -> None:
    if set(value.keys()) != set(keys):
        raise ReleaseIdentityError(f"{label} has missing or unexpected keys")


def _require_string(value: object, label: str) -> str:
    if type(value) is not str:
        raise ReleaseIdentityError(f"{label} must be a string")
    return value


def _validate_semver(value: object, label: str) -> str:
    version = _require_string(value, label)
    if _SEMVER_RE.fullmatch(version) is None:
        raise ReleaseIdentityError(
            f"{label} must be a stable numeric SemVer triplet without leading zeros"
        )
    return version


def _version_parts(version: str) -> tuple[str, str, str]:
    match = _SEMVER_RE.fullmatch(version)
    if match is None:  # All callers validate first; keep comparisons fail-closed.
        raise ReleaseIdentityError("version is not a stable numeric SemVer triplet")
    return match.group(1), match.group(2), match.group(3)


def _compare_versions(left: str, right: str) -> int:
    """Compare canonical numeric SemVer without integer-size limits."""
    for left_part, right_part in zip(_version_parts(left), _version_parts(right)):
        left_number = (len(left_part), left_part)
        right_number = (len(right_part), right_part)
        if left_number < right_number:
            return -1
        if left_number > right_number:
            return 1
    return 0


def _validate_git_identity(value: object, label: str) -> str:
    identity = _require_string(value, label)
    if _GIT_IDENTITY_RE.fullmatch(identity) is None:
        raise ReleaseIdentityError(f"{label} must be a lowercase 40-hex Git identity")
    return identity


def _validate_contract_mapping(contract: object) -> dict[str, object]:
    top = _require_mapping(contract, "release identity contract")
    _require_exact_keys(top, ("schema", "schemaVersion", "agentCore", "templates"), "contract")

    schema = _require_string(top["schema"], "contract schema")
    if schema != CONTRACT_SCHEMA:
        raise ReleaseIdentityError("contract schema is unsupported")
    schema_version = top["schemaVersion"]
    if type(schema_version) is not int or schema_version != CONTRACT_SCHEMA_VERSION:
        raise ReleaseIdentityError("contract schemaVersion is unsupported")

    agent_core = _require_mapping(top["agentCore"], "agentCore")
    _require_exact_keys(agent_core, ("version", "architecture", "status"), "agentCore")
    agent_core_version = _validate_semver(agent_core["version"], "agentCore version")
    architecture = _require_string(agent_core["architecture"], "agentCore architecture")
    if architecture != AGENT_CORE_ARCHITECTURE:
        raise ReleaseIdentityError(f"agentCore architecture must be {AGENT_CORE_ARCHITECTURE}")
    status = _require_string(agent_core["status"], "agentCore status")
    if status not in _ALLOWED_STATUSES:
        raise ReleaseIdentityError("agentCore status is unsupported")

    templates = _require_mapping(top["templates"], "templates")
    _require_exact_keys(templates, ("version",), "templates")
    templates_version = _validate_semver(templates["version"], "Templates version")

    # Return fresh plain dictionaries so callers don't retain custom Mapping
    # implementations or any extra fields from an input object.
    return {
        "schema": schema,
        "schemaVersion": schema_version,
        "agentCore": {
            "version": agent_core_version,
            "architecture": architecture,
            "status": status,
        },
        "templates": {"version": templates_version},
    }


def parse_contract(raw: bytes) -> dict[str, object]:
    """Decode and strictly validate a release identity contract byte string."""
    if type(raw) is not bytes:
        raise ReleaseIdentityError("release identity contract must be bytes")
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise ReleaseIdentityError("release identity contract must be UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseIdentityError("release identity contract is invalid JSON") from exc
    return _validate_contract_mapping(value)


def _validate_prior(prior: object) -> dict[str, str]:
    previous = _require_mapping(prior, "prior release identity")
    _require_exact_keys(previous, IDENTITY_KEYS, "prior release identity")
    return {
        "agentCoreVersion": _validate_semver(
            previous["agentCoreVersion"], "prior agentCoreVersion"
        ),
        "agentCorePayload": _validate_git_identity(
            previous["agentCorePayload"], "prior agentCorePayload"
        ),
        "templatesVersion": _validate_semver(
            previous["templatesVersion"], "prior templatesVersion"
        ),
        "templatesSourceRevision": _validate_git_identity(
            previous["templatesSourceRevision"], "prior templatesSourceRevision"
        ),
    }


def evaluate_release(
    contract: Mapping[str, object],
    installed_marker: str,
    payload_tree: str,
    templates_source_revision: str,
    proposed_tag: str,
    prior: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Validate stable release evidence and return its bound identity mapping.

    The prior value must be an identity mapping obtained from the exact,
    independently verified previous release. With no prior identity, only the
    explicitly planned first stable pair can be proposed.
    """
    parsed_contract = _validate_contract_mapping(contract)
    agent_core = parsed_contract["agentCore"]
    templates = parsed_contract["templates"]
    assert isinstance(agent_core, dict) and isinstance(templates, dict)
    agent_core_version = agent_core["version"]
    templates_version = templates["version"]
    assert isinstance(agent_core_version, str) and isinstance(templates_version, str)

    if agent_core["status"] != "candidate":
        raise ReleaseIdentityError("agentCore status must be candidate to pass the release check")

    installed_version = _validate_semver(installed_marker, "installed AgentCore marker")
    if installed_version != agent_core_version:
        raise ReleaseIdentityError("installed AgentCore marker does not match the contract version")

    payload = _validate_git_identity(payload_tree, "AgentCore payload tree")
    source_revision = _validate_git_identity(
        templates_source_revision, "Templates source revision"
    )
    tag = _require_string(proposed_tag, "proposed Templates tag")
    expected_tag = f"v{templates_version}"
    if tag != expected_tag:
        raise ReleaseIdentityError(f"proposed Templates tag must be exactly {expected_tag}")

    previous = _validate_prior(prior) if prior is not None else None
    if previous is None:
        if (
            agent_core_version != FIRST_AGENT_CORE_VERSION
            or templates_version != FIRST_TEMPLATES_VERSION
        ):
            raise ReleaseIdentityError(
                "without a verified prior release only AgentCore 1.0.0 / Templates 4.0.0 is allowed"
            )
    else:
        previous_templates_version = previous["templatesVersion"]
        if _compare_versions(templates_version, previous_templates_version) <= 0:
            raise ReleaseIdentityError("Templates version must increment beyond the prior release")

        previous_agent_core_version = previous["agentCoreVersion"]
        agent_core_comparison = _compare_versions(
            agent_core_version, previous_agent_core_version
        )
        if agent_core_comparison < 0:
            raise ReleaseIdentityError("AgentCore version must not decrease from the prior release")
        if agent_core_comparison == 0 and payload != previous["agentCorePayload"]:
            raise ReleaseIdentityError(
                "an unchanged AgentCore version must retain the identical payload tree"
            )
        if agent_core_comparison > 0 and payload == previous["agentCorePayload"]:
            raise ReleaseIdentityError(
                "a changed AgentCore version must have a changed payload tree"
            )

    return {
        "agentCoreVersion": agent_core_version,
        "agentCorePayload": payload,
        "templatesVersion": templates_version,
        "templatesSourceRevision": source_revision,
    }
