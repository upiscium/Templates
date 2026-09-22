#!/usr/bin/env python3
"""Prepare and validate evidence-backed pull request metadata."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import task_contract
import task_lifecycle


class PublicationMetadataError(RuntimeError):
    pass


PLACEHOLDER_PATTERNS = (
    r"@@[A-Z0-9_]+@@",
    r"\bTBD\b",
    r"Describe the implemented Task outcome\.",
    r"Task-specific criteria copied from `?\.task-state/task\.md`?",
    r"Define Task-specific acceptance criteria",
    r"\.task-state/[A-Za-z0-9_.-]+(?:#[A-Za-z0-9_.-]+)?",
    r"(?i)\bAuthoritative source\s*:",
)
NOT_RUN_RE = re.compile(r"(?im)^.*(?:NOT[ _-]?RUN|not run).*$")
CLOSING_DIRECTIVE_RE = re.compile(
    r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+"
    r"(?:#\d+|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+|"
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+)"
)
ISSUE_H2_RE = re.compile(r"^\s{0,3}##(?!#)\s+(.+?)\s*$")
ISSUE_FENCE_RE = re.compile(r"^\s{0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
ISSUE_LIST_ITEM_RE = re.compile(
    r"^\s{0,3}(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(?P<value>\S.*)\s*$"
)


@dataclass(frozen=True)
class _PublicationSnapshot:
    state: str
    verification: dict | None
    work_units: dict | None
    issue: dict | None


def _sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^## (.+?)\s*$", text))
    return {
        match.group(1): text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip()
        for index, match in enumerate(matches)
    }


def _content_lines(value: str) -> list[str]:
    ignored = {"none yet.", "none yet", "none recorded", "none recorded yet."}
    return [
        _neutralize_closing_directives(line)
        for line in value.splitlines()
        if line.strip() and line.strip().lower().lstrip("- ") not in ignored
    ]


def _neutralize_closing_directives(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return re.sub(
            r"(?i)^(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+",
            "References ",
            match.group(0),
        )

    return CLOSING_DIRECTIVE_RE.sub(replace, value)


def _identity(text: str, name: str) -> str:
    match = re.search(rf"(?m)^- {re.escape(name)}: (.+)$", text)
    if match is None or not match.group(1).strip():
        raise PublicationMetadataError(f"Task State is missing {name}")
    return match.group(1).strip()


def _current_state_value(text: str, name: str) -> str:
    match = re.search(rf"(?m)^- {re.escape(name)}: (.+)$", text)
    if match is None or not match.group(1).strip():
        raise PublicationMetadataError(f"Task State is missing Current state {name}")
    return match.group(1).strip()


def _is_none_value(value: str) -> bool:
    return value.casefold().rstrip(".") in {"none", "none recorded"}


def _requirements(lines: list[str]) -> list[str]:
    requirements = []
    for line in lines:
        value = re.sub(r"^-\s*\[[ xX]\]\s*", "", line.strip())
        value = value.removeprefix("- ").strip()
        if value:
            requirements.append(f"- Requirement: {value}")
    return requirements


def _issue_acceptance_requirements(body: str) -> list[str]:
    """Extract only explicit list items from one exact Issue criteria section."""
    collecting = False
    found = False
    requirements: list[str] = []
    fence: tuple[str, int] | None = None
    for line in body.splitlines():
        fence_match = ISSUE_FENCE_RE.match(line)
        if fence is not None:
            if (
                fence_match is not None
                and fence_match.group("marker")[0] == fence[0]
                and len(fence_match.group("marker")) >= fence[1]
                and not fence_match.group("info").strip()
            ):
                fence = None
            continue
        if fence_match is not None:
            marker = fence_match.group("marker")
            fence = (marker[0], len(marker))
            continue

        heading_match = ISSUE_H2_RE.match(line)
        if heading_match is not None:
            heading = re.sub(r"\s+#+\s*$", "", heading_match.group(1)).strip()
            if heading.casefold() == "acceptance criteria":
                if found:
                    return []
                found = True
                collecting = True
            elif collecting:
                break
            continue
        if not collecting:
            continue

        item = ISSUE_LIST_ITEM_RE.match(line)
        if item is None:
            continue
        value = _neutralize_closing_directives(item.group("value").strip())
        if value:
            requirements.append(f"- Requirement: {value}")
    return requirements


def _issue_snapshot(root: Path, task: str, state: str, directory_fd: int) -> dict | None:
    """Resolve Issue-backed publication authority from the pinned snapshot."""
    issue_pointer = (
        task_contract.SNAPSHOT in state
        or "canonical-contract sha256=" in state
    )
    issue_metadata = (
        task_contract._read_state_file(directory_fd, "issue.json") is not None
        or task_contract._read_state_file(directory_fd, "contract.json") is not None
        or issue_pointer
    )
    if not issue_metadata:
        return None
    try:
        return task_contract.load_issue_snapshot(root, task, directory_fd=directory_fd)
    except task_contract.ContractError as exc:
        raise PublicationMetadataError(str(exc)) from exc


def _issue_purpose(snapshot: dict) -> list[str]:
    payload = snapshot["payload"]
    body_lines = payload["body"].strip().splitlines()
    excerpt = [
        f"> {_neutralize_closing_directives(line).rstrip()}" if line.strip() else ">"
        for line in body_lines
    ]
    return [
        f"Issue #{snapshot['issue']}: {_neutralize_closing_directives(payload['title'].strip())}",
        f"Bound Issue: {payload['url']}",
        f"Closes #{snapshot['issue']}",
        "Bound Issue source content (preserved language):",
        *excerpt,
    ]


def _publication_directives(title: str, body: str) -> list[str]:
    """Find semantic closing relations without treating rendered paths as prose."""
    semantic_body = re.sub(
        r"(?ms)^## Changed paths\n\n.*?(?=^## |\Z)",
        "",
        body,
        count=1,
    )
    return [
        match.group(0).casefold()
        for match in CLOSING_DIRECTIVE_RE.finditer(title + "\n" + semantic_body)
    ]


def _decode_json(raw: bytes | None, name: str) -> dict | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationMetadataError(f"invalid persisted evidence: {name}") from exc
    if not isinstance(value, dict):
        raise PublicationMetadataError(f"invalid persisted evidence: {name}")
    return value


def _load_publication_snapshot(root: Path, task: str) -> _PublicationSnapshot:
    try:
        with task_contract.contract_state_lock(root) as directory_fd:
            state_bytes = task_contract._read_state_file(directory_fd, "task.md")
            if state_bytes is None:
                raise PublicationMetadataError("missing Task State")
            try:
                state = state_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PublicationMetadataError("Task State is not valid UTF-8") from exc
            verification = _decode_json(
                task_contract._read_state_file(directory_fd, "verification.json"),
                "verification.json",
            )
            work_units = _decode_json(
                task_contract._read_state_file(directory_fd, "work-units.json"),
                "work-units.json",
            )
            issue = _issue_snapshot(root, task, state, directory_fd)
            task_contract._assert_state_dir_binding(root, directory_fd)
            return _PublicationSnapshot(state, verification, work_units, issue)
    except PublicationMetadataError:
        raise
    except task_contract.ContractError as exc:
        raise PublicationMetadataError(str(exc)) from exc
    except (OSError, UnicodeError) as exc:
        raise PublicationMetadataError("persisted publication evidence is not safely readable") from exc


def _verification_evidence_from_value(receipt: dict | None, task: str, head: str) -> dict:
    if receipt is None:
        raise PublicationMetadataError("missing persisted project verification evidence")
    if set(receipt) != {
        "schema_version", "task_id", "head", "clean_tracked_worktree",
        "worktree_stable", "project_check",
    } or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        raise PublicationMetadataError("invalid project verification evidence schema")
    if not isinstance(receipt.get("task_id"), str) or receipt["task_id"] != task:
        raise PublicationMetadataError("project verification evidence belongs to another Task")
    if (
        not isinstance(receipt.get("head"), str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", receipt["head"])
        or receipt["head"] != head
    ):
        raise PublicationMetadataError("project verification evidence is stale")
    if type(receipt.get("clean_tracked_worktree")) is not bool or type(receipt.get("worktree_stable")) is not bool:
        raise PublicationMetadataError("project verification evidence has invalid worktree flags")
    check = receipt.get("project_check")
    if not isinstance(check, dict) or set(check) != {"command", "returncode", "executed_at"}:
        raise PublicationMetadataError("invalid project verification evidence schema")
    if (
        check.get("command") != ["just", "project::check"]
        or type(check.get("returncode")) is not int
        or check["returncode"] != 0
    ):
        raise PublicationMetadataError("project::check has no persisted PASS evidence")
    if receipt["clean_tracked_worktree"] is not True or receipt["worktree_stable"] is not True:
        raise PublicationMetadataError("project verification is not bound to a clean stable worktree")
    if not isinstance(check.get("executed_at"), str) or not check["executed_at"]:
        raise PublicationMetadataError("project verification evidence has no execution time")
    return receipt


def _validate_work_units(value: dict | None, task: str, state: str) -> None:
    if value is None:
        return
    if set(value) == {"schema_version", "task_id", "units"}:
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise PublicationMetadataError("invalid legacy Work Unit evidence schema")
        if value.get("task_id") != task or not isinstance(value.get("units"), dict):
            raise PublicationMetadataError("legacy Work Unit evidence does not match the Task")
        for identifier, unit in value["units"].items():
            if (
                not isinstance(identifier, str)
                or not isinstance(unit, dict)
                or set(unit) != {"requested_role", "state", "transitions"}
                or not isinstance(unit["requested_role"], str)
                or unit["requested_role"] not in task_lifecycle.WORK_UNIT_ROLES
                or not isinstance(unit["state"], str)
                or unit["state"] not in task_lifecycle.WORK_UNIT_STATES
                or not isinstance(unit["transitions"], list)
            ):
                raise PublicationMetadataError(f"invalid legacy Work Unit record: {identifier}")
            for transition in unit["transitions"]:
                if (
                    not isinstance(transition, dict)
                    or set(transition) != {"evidence_sha256"}
                    or not isinstance(transition["evidence_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", transition["evidence_sha256"])
                ):
                    raise PublicationMetadataError(f"invalid legacy Work Unit transition: {identifier}")
        return
    expected = {"schema_version", "task_id", "worktree", "branch", "units"}
    if set(value) != expected or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise PublicationMetadataError("invalid Work Unit evidence schema")
    if not isinstance(value.get("task_id"), str) or value["task_id"] != task:
        raise PublicationMetadataError("Work Unit evidence does not match the Task")
    if value.get("worktree") != _identity(state, "Worktree") or value.get("branch") != _identity(state, "Branch"):
        raise PublicationMetadataError("Work Unit evidence identity does not match the Task")
    units = value.get("units")
    if not isinstance(units, dict):
        raise PublicationMetadataError("invalid Work Unit evidence schema")
    unit_keys = {"id", "requested_role", "objective", "semantic_sha256", "state", "transitions", "created_at", "updated_at"}
    transition_keys = {"from", "to", "evidence", "evidence_sha256", "recorded_at"}
    for identifier, unit in units.items():
        if not isinstance(identifier, str) or not isinstance(unit, dict) or set(unit) != unit_keys:
            raise PublicationMetadataError(f"invalid Work Unit record: {identifier}")
        if (
            unit["id"] != identifier
            or not isinstance(unit["requested_role"], str)
            or unit["requested_role"] not in task_lifecycle.WORK_UNIT_ROLES
        ):
            raise PublicationMetadataError(f"invalid Work Unit identity: {identifier}")
        objective = unit["objective"]
        if not isinstance(objective, str) or not objective or len(objective) > 2000 or any(ord(char) < 32 for char in objective):
            raise PublicationMetadataError(f"invalid Work Unit objective: {identifier}")
        if unit["semantic_sha256"] != task_lifecycle.semantic_digest(objective):
            raise PublicationMetadataError(f"Work Unit objective digest mismatch: {identifier}")
        if (
            not isinstance(unit["state"], str)
            or unit["state"] not in task_lifecycle.WORK_UNIT_STATES
            or not isinstance(unit["transitions"], list)
        ):
            raise PublicationMetadataError(f"invalid Work Unit state: {identifier}")
        if not all(isinstance(unit[name], str) and unit[name] for name in ("created_at", "updated_at")):
            raise PublicationMetadataError(f"invalid Work Unit timestamps: {identifier}")
        previous = "in-flight"
        for transition in unit["transitions"]:
            if (
                not isinstance(transition, dict)
                or not transition_keys.issubset(transition)
                or set(transition) - transition_keys - {"provider_failure"}
                or not isinstance(transition["from"], str)
                or not isinstance(transition["to"], str)
            ):
                raise PublicationMetadataError(f"invalid Work Unit transition: {identifier}")
            if transition["from"] != previous or transition["to"] not in task_lifecycle.WORK_UNIT_TRANSITIONS.get(previous, set()):
                raise PublicationMetadataError(f"invalid Work Unit transition chain: {identifier}")
            evidence = transition["evidence"]
            if not isinstance(evidence, str) or not evidence or len(evidence) > 4000 or any(ord(char) < 32 for char in evidence):
                raise PublicationMetadataError(f"invalid Work Unit evidence: {identifier}")
            if transition["evidence_sha256"] != task_lifecycle.semantic_digest(evidence):
                raise PublicationMetadataError(f"Work Unit evidence digest mismatch: {identifier}")
            if not isinstance(transition["recorded_at"], str) or not transition["recorded_at"]:
                raise PublicationMetadataError(f"invalid Work Unit transition timestamp: {identifier}")
            if "provider_failure" in transition:
                try:
                    task_lifecycle.validate_provider_failure_record(
                        transition["provider_failure"], transition["to"]
                    )
                except task_lifecycle.LifecycleError as exc:
                    raise PublicationMetadataError(
                        f"invalid Work Unit provider failure: {identifier}"
                    ) from exc
            previous = transition["to"]
        if unit["transitions"] and unit["transitions"][-1]["to"] != unit["state"]:
            raise PublicationMetadataError(f"Work Unit final state mismatch: {identifier}")
        if not unit["transitions"] and unit["state"] != "in-flight":
            raise PublicationMetadataError(f"Work Unit has no state transition: {identifier}")


def _completed_reviews_from_value(value: dict | None, task: str, state: str) -> list[str]:
    _validate_work_units(value, task, state)
    if value is None:
        return []
    effective: dict[str, tuple[int, str, dict]] = {}
    for identifier, unit in value["units"].items():
        if unit["requested_role"] not in {"reviewer", "security-reviewer"}:
            continue
        sequence = task_lifecycle.canonical_work_unit_sequence(task, identifier)
        if sequence is None:
            raise PublicationMetadataError(
                f"review Work Unit has no canonical sequence: {identifier}"
            )
        role = unit["requested_role"]
        if role not in effective or sequence > effective[role][0]:
            effective[role] = (sequence, identifier, unit)
    reviews = []
    for role in ("reviewer", "security-reviewer"):
        if role not in effective:
            continue
        _, identifier, unit = effective[role]
        if unit["state"] != "completed":
            raise PublicationMetadataError(
                f"required {role} Work Unit is not completed: {identifier}"
            )
        if not unit["transitions"]:
            raise PublicationMetadataError(
                f"completed {role} Work Unit has no evidence: {identifier}"
            )
        digest = unit["transitions"][-1]["evidence_sha256"]
        reviews.append(f"- `{identifier}` — `{role}` — completed — evidence `{digest}`")
    return reviews


def verification_evidence(root: Path, task: str, head: str) -> dict:
    snapshot = _load_publication_snapshot(root, task)
    return _verification_evidence_from_value(snapshot.verification, task, head)


def completed_reviews(root: Path, task: str) -> list[str]:
    snapshot = _load_publication_snapshot(root, task)
    return _completed_reviews_from_value(snapshot.work_units, task, snapshot.state)


def publication_evidence_snapshot(root: Path) -> dict[str, bytes | None]:
    """Capture the mutable publication evidence under one pinned State lock."""
    try:
        with task_contract.contract_state_lock(root) as directory_fd:
            evidence = {
                name: task_contract._read_state_file(directory_fd, name)
                for name in ("verification.json", "work-units.json")
            }
            task_contract._assert_state_dir_binding(root, directory_fd)
            return evidence
    except task_contract.ContractError as exc:
        raise PublicationMetadataError(str(exc)) from exc
    except OSError as exc:
        raise PublicationMetadataError("publication evidence is not safely readable") from exc


def canonical_metadata(root: Path, task: str, *, head: str, changed_paths: list[str]) -> tuple[str, str]:
    snapshot_state = _load_publication_snapshot(root, task)
    text = snapshot_state.state
    if _identity(text, "Task ID") != task:
        raise PublicationMetadataError("Task State identity does not match requested Task")
    sections = _sections(text)
    snapshot = snapshot_state.issue
    if snapshot is None:
        purpose = _content_lines(sections.get("Purpose", ""))
        criteria = _content_lines(sections.get("Acceptance criteria", ""))
        title_summary = purpose[0].lstrip("- ").strip() if purpose else ""
        relation = None
        requirements = _requirements(criteria)
    else:
        payload = snapshot["payload"]
        purpose = _issue_purpose(snapshot)
        title_summary = _neutralize_closing_directives(payload["title"].strip())
        relation = snapshot["issue"]
        requirements = _issue_acceptance_requirements(payload["body"])
        if not requirements:
            requirements = [
                f"- Requirement: Satisfy the authoritative requirements in Issue #{relation}."
            ]
    if not purpose:
        raise PublicationMetadataError("Task purpose and acceptance criteria must be resolved")
    if not requirements:
        raise PublicationMetadataError("Task acceptance criteria contain no authoritative requirements")
    blockers = _neutralize_closing_directives(
        _current_state_value(sections.get("Current state", ""), "Blockers")
    )
    unverified = _neutralize_closing_directives(
        _current_state_value(sections.get("Current state", ""), "Unverified")
    )
    risks = []
    if not _is_none_value(blockers):
        risks.append(f"- Blockers: {blockers}")
    if not _is_none_value(unverified):
        risks.append(f"- Unverified: {unverified}")
    if not risks:
        risks = ["- None recorded."]
    verification = _verification_evidence_from_value(snapshot_state.verification, task, head)
    title = f"{task}: {title_summary}"
    changes = [f"- `{path.replace('`', '')}`" for path in changed_paths] or ["- No tracked changes recorded."]
    reviews = _completed_reviews_from_value(snapshot_state.work_units, task, text)
    if not any("— `reviewer` — completed" in review for review in reviews):
        raise PublicationMetadataError(
            "publication requires a completed reviewer Work Unit"
        )
    followups = _content_lines(sections.get("Follow-up Task candidates", "")) or ["- None recorded."]
    body = "\n".join(
        [
            "## Summary", "", f"Task: {task}", "", *purpose,
            "", "## Changed paths", "", *changes,
            "", "## Acceptance criteria", "",
            (
                "The following are authoritative requirements from the pinned Issue snapshot; "
                "completion evidence is reported separately under Validation and Reviews."
                if relation is not None
                else "The following are authoritative Task requirements; completion evidence is reported separately under Validation and Reviews."
            ),
            "", *requirements,
            "", "## Validation", "",
            f"- `just project::check`: PASS at `{head}` (persisted executed evidence)",
            "", "## Reviews", "", *(reviews or ["- None recorded."]),
            "", "## Risks and unverified areas", "", *risks,
            "", "## Follow-up Tasks", "", *followups, "",
        ]
    )
    directives = _publication_directives(title, body)
    expected_directives = [f"closes #{relation}".casefold()] if relation is not None else []
    if directives != expected_directives:
        raise PublicationMetadataError(
            "pull request metadata contains an unauthorized Issue-closing directive"
        )
    validate_metadata(title, body, receipt=verification)
    return title, body


def validate_metadata(title: str, body: str, *, receipt: dict | None = None) -> None:
    if not title.strip() or not body.strip():
        raise PublicationMetadataError("pull request title and body must be non-empty")
    combined = title + "\n" + body
    for pattern in PLACEHOLDER_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            raise PublicationMetadataError("unresolved pull request publication placeholder")
    not_run = NOT_RUN_RE.findall(body)
    if not_run:
        if receipt and receipt.get("project_check", {}).get("returncode") == 0:
            raise PublicationMetadataError("pull request metadata contradicts persisted PASS evidence with NOT RUN")
        raise PublicationMetadataError("unresolved NOT RUN publication metadata")
    required = ("## Summary", "## Acceptance criteria", "## Validation", "## Risks and unverified areas", "## Follow-up Tasks")
    if any(heading not in body for heading in required):
        raise PublicationMetadataError("pull request body is missing required sections")


def canonical_pr_body_matches(canonical: str, live: str | None) -> bool:
    """Compare PR bodies while tolerating only one transport-level terminal LF."""

    if not isinstance(live, str) or canonical.endswith("\r\n") or live.endswith(
        "\r\n"
    ):
        return False

    def without_transport_lf(body: str) -> str:
        if body.endswith("\n") and not body.endswith("\n\n"):
            return body[:-1]
        return body

    return without_transport_lf(canonical) == without_transport_lf(live)


def write_metadata(root: Path, title: str, body: str) -> None:
    try:
        task_contract.write_publication_metadata(
            root,
            (title + "\n").encode(),
            body.encode(),
        )
    except (OSError, UnicodeError, task_contract.ContractError) as exc:
        raise PublicationMetadataError(str(exc)) from exc


def read_and_validate_metadata(root: Path, *, receipt: dict | None = None) -> tuple[str, str]:
    try:
        title_bytes, body_bytes = task_contract.read_publication_metadata(root)
        title = title_bytes.decode("utf-8")
        if title.endswith("\n"):
            title = title[:-1]
        body = body_bytes.decode("utf-8")
    except (OSError, UnicodeError, task_contract.ContractError) as exc:
        raise PublicationMetadataError("run agent::pr-prepare before publication") from exc
    validate_metadata(title, body, receipt=receipt)
    return title, body
