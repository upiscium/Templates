"""Minimal v4 Task authority over the #217 metadata substrate.

This staged capability is not an authority issuer or a live Issue adapter.
A trusted host installs the authorization callback and validates the fetched
Issue source before supplying its exact title/body. Never expose that host
construction boundary or the underlying generic writer to untrusted agents.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import metadata_codec as codec
from metadata_ref import MetadataConflictError, MetadataRefError, MetadataStore


PAYLOAD_SCHEMA_VERSION = 1
_ID = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CONTRACT_FIELDS = frozenset({"schema_version", "title", "body"})
_RECORD_FIELDS = frozenset({"schema_version", "branch_ref", "contract_id"})


class TaskRecordError(ValueError):
    """Invalid closed schema, binding, or authority request."""


def validate_branch_ref(branch_ref: object) -> str:
    """Require a full heads ref using Git's non-normalizing ref-name grammar."""
    if type(branch_ref) is not str or not branch_ref.startswith("refs/heads/"):
        raise TaskRecordError("Task branch must be an explicit full refs/heads/ ref")
    if (
        branch_ref.endswith(("/", "."))
        or ".." in branch_ref or "@{" in branch_ref
        or any(ord(c) < 0x21 or ord(c) == 0x7F or c in "~^:?*[\\" for c in branch_ref)
        or any(not part or part.startswith(".") or part.endswith(".lock")
               for part in branch_ref.split("/"))
    ):
        raise TaskRecordError("malformed full Task branch ref")
    return branch_ref


def _schema(payload: object, required: frozenset[str], optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    if type(payload) is not dict or not required <= payload.keys() or payload.keys() - required - optional:
        raise TaskRecordError("payload has missing or forbidden fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != PAYLOAD_SCHEMA_VERSION:
        raise TaskRecordError("unsupported payload schema version")
    return payload


def _contract_payload(payload: object) -> None:
    value = _schema(payload, _CONTRACT_FIELDS)
    if type(value["title"]) is not str or not value["title"].strip() or type(value["body"]) is not str:
        raise TaskRecordError("Issue contract requires a nonblank title and a string body")


def _disposition(value: object, repository: str, task: str) -> None:
    if type(value) is not dict:
        raise TaskRecordError("disposition must be a closed object")
    if value == {"kind": "cancelled"}:
        return
    if value.keys() != {"kind", "replacement"} or value["kind"] != "superseded":
        raise TaskRecordError("only cancelled or superseded dispositions are supported")
    replacement = value["replacement"]
    if type(replacement) is not dict or replacement.keys() != {"repository", "task"}:
        raise TaskRecordError("superseded requires an explicit replacement Task identity")
    # The shared codec defines exact repository and canonical Task spelling.
    codec.encode_object("contract", replacement["repository"], replacement["task"], "0" * 40, {})
    if replacement == {"repository": repository, "task": task}:
        raise TaskRecordError("a Task cannot supersede itself")


def _record_payload(payload: object, repository: str, task: str) -> None:
    value = _schema(payload, _RECORD_FIELDS, frozenset({"disposition"}))
    validate_branch_ref(value["branch_ref"])
    if type(value["contract_id"]) is not str or not _ID.fullmatch(value["contract_id"]):
        raise TaskRecordError("contract_id must be an exact metadata object ID")
    if "disposition" in value:
        _disposition(value["disposition"], repository, task)


def encode_contract(repository: str, task: str, base_revision: str, payload: dict[str, Any]) -> tuple[str, bytes]:
    """Encode an Issue requirement snapshot; no live GitHub incidental metadata."""
    _contract_payload(payload)
    return codec.encode_object("contract", repository, task, base_revision, payload)


def encode_record(repository: str, task: str, base_revision: str, payload: dict[str, Any]) -> tuple[str, bytes]:
    """Pure encoding does not grant permission to establish durable authority."""
    _record_payload(payload, repository, task)
    return codec.encode_object("task-record", repository, task, base_revision, payload)


def decode_contract(data: bytes, *, object_id: str, repository: str, task: str, base_revision: str) -> dict[str, Any]:
    value = codec.decode_object(data, expected_id=object_id, expected_repository=repository,
                               expected_task=task, expected_subject=base_revision, expected_kind="contract")
    _contract_payload(value["payload"])
    return value


def decode_record(data: bytes, *, object_id: str, repository: str, task: str,
                  base_revision: str, branch_ref: str) -> dict[str, Any]:
    validate_branch_ref(branch_ref)
    value = codec.decode_object(data, expected_id=object_id, expected_repository=repository,
                               expected_task=task, expected_subject=base_revision, expected_kind="task-record")
    _record_payload(value["payload"], repository, task)
    if value["payload"]["branch_ref"] != branch_ref:
        raise TaskRecordError("Task Record branch does not match expected binding")
    return value


def _bytes(envelope: dict[str, Any]) -> bytes:
    return codec.encode_object(envelope["kind"], envelope["repository"], envelope["task"],
                               envelope["subject"], envelope["payload"])[1]


@dataclass(frozen=True)
class AuthorityRequest:
    """Exact proposed decision for the trusted host, never a JSON credential.

    proposed_record_id binds the complete disposition/replacement and Contract
    bytes. For create, host validates Issue/Task source authority; for disposition
    and reauthorize it must additionally validate explicit Human-owned intent.
    """

    operation: str
    repository: str
    task: str
    branch_ref: str
    base_revision: str
    expected_record_id: str | None
    proposed_record_id: str
    contract_id: str


@dataclass(frozen=True)
class Publication:
    metadata_commit: str
    record_id: str
    contract_id: str


class TaskRecords:
    """Narrow semantic writer. Authorization is installed by a trusted host.

    It is not authentication of arbitrary Python callers: code able to replace
    the host callback or access MetadataStore.publish is outside this boundary.
    Both the semantic authorization and #217 direct-ref CAS are required.
    """

    def __init__(self, store: MetadataStore, *, authorize: Callable[[AuthorityRequest], bool]) -> None:
        if not callable(authorize):
            raise TaskRecordError("a trusted host authorization capability is required")
        self._store = store
        self._authorize = authorize

    def read(self, commit: str, *, task: str, base_revision: str, branch_ref: str) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        """Resolve pointer and closed Contract link in the same pinned commit."""
        entry = self._store.read_record(commit, task)
        if entry is None:
            return None
        record_id, envelope = entry
        record = decode_record(_bytes(envelope), object_id=record_id, repository=self._store.repository,
                               task=task, base_revision=base_revision, branch_ref=branch_ref)
        contract_id = record["payload"]["contract_id"]
        contract = self._store.read_object(commit, "contract", contract_id, task=task, subject=base_revision)
        contract = decode_contract(_bytes(contract), object_id=contract_id, repository=self._store.repository,
                                   task=task, base_revision=base_revision)
        return record_id, record, contract

    def _prior(self, task: str, base_revision: str, branch_ref: str, expected_record_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if type(expected_record_id) is not str or not _ID.fullmatch(expected_record_id):
            raise TaskRecordError("mutation requires an exact expected prior record ID")
        tip = self._store.fetch_tip()
        if tip is None:
            raise MetadataConflictError("expected Task Record is absent")
        prior = self.read(tip, task=task, base_revision=base_revision, branch_ref=branch_ref)
        if prior is None or prior[0] != expected_record_id:
            raise MetadataConflictError("expected prior Task Record does not match pinned pointer")
        return prior[1], prior[2]

    def _publish(self, operation: str, task: str, base_revision: str, payload: dict[str, Any],
                 contract_data: bytes, expected: str | None,
                 on_candidate: Callable[[str], object] | None) -> Publication:
        repository = self._store.repository
        # Capture canonical bytes before the authority boundary; later mutation
        # of caller-owned dicts cannot change the authorized publication.
        record_id, record_data = encode_record(repository, task, base_revision, payload)
        request = AuthorityRequest(operation, repository, task, payload["branch_ref"], base_revision,
                                   expected, record_id, payload["contract_id"])
        decode_contract(contract_data, object_id=request.contract_id, repository=repository,
                        task=task, base_revision=base_revision)
        try:
            allowed = self._authorize(request) is True
        except Exception as error:
            raise TaskRecordError("host authority validation failed") from error
        if not allowed:
            raise TaskRecordError("explicit caller authority was not granted")
        commit = self._store.publish([contract_data, record_data], {task: (expected, record_id)},
                                     on_candidate=on_candidate)
        return Publication(commit, record_id, request.contract_id)

    def create(self, *, task: str, branch_ref: str, base_revision: str, title: str, body: str,
               on_candidate: Callable[[str], object] | None = None) -> Publication:
        """First pointer creation always requires expected absence, no disposition."""
        validate_branch_ref(branch_ref)
        contract_id, data = encode_contract(self._store.repository, task, base_revision,
                                           {"schema_version": 1, "title": title, "body": body})
        self._store.validate_base_revision(base_revision)
        return self._publish("create", task, base_revision,
                             {"schema_version": 1, "branch_ref": branch_ref, "contract_id": contract_id},
                             data, None, on_candidate)

    def set_disposition(self, *, task: str, branch_ref: str, base_revision: str, expected_record_id: str,
                        disposition: dict[str, Any], on_candidate: Callable[[str], object] | None = None) -> Publication:
        """Only the trusted host may authorize an explicit Human decision."""
        prior, contract = self._prior(task, base_revision, branch_ref, expected_record_id)
        payload = {**prior["payload"], "disposition": disposition}
        _record_payload(payload, self._store.repository, task)
        if payload == prior["payload"]:
            raise TaskRecordError("disposition is unchanged; no new decision to persist")
        return self._publish("disposition", task, base_revision, payload, _bytes(contract),
                             expected_record_id, on_candidate)

    def reauthorize(self, *, task: str, branch_ref: str, base_revision: str, expected_record_id: str,
                    title: str, body: str, on_candidate: Callable[[str], object] | None = None) -> Publication:
        """Append a new immutable snapshot and CAS only its Record binding."""
        prior, _contract = self._prior(task, base_revision, branch_ref, expected_record_id)
        contract_id, data = encode_contract(self._store.repository, task, base_revision,
                                           {"schema_version": 1, "title": title, "body": body})
        if contract_id == prior["payload"]["contract_id"]:
            raise TaskRecordError("reauthorization requires a new immutable contract snapshot")
        return self._publish("reauthorize", task, base_revision,
                             {**prior["payload"], "contract_id": contract_id}, data,
                             expected_record_id, on_candidate)
