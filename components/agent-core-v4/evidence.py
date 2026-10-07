"""Minimal immutable Evidence envelope over the staged #217 metadata plane.

No producer lookup, result interpretation, authority issuance, or publication
adapter lives here. Trusted callers persist canonical bytes through #217 only.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import metadata_codec as codec
from metadata_ref import MetadataStore


SCHEMA_VERSION = 1
MAX_SUPERSESSION_LINKS = 64
_FIELDS = frozenset({"schema_version", "kind", "producer", "created_at", "payload"})
_ID = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z", re.ASCII)


class EvidenceError(ValueError):
    """Invalid Evidence envelope or explicit reference graph."""


def _validate(envelope: object) -> dict[str, Any]:
    if type(envelope) is not dict or not _FIELDS <= envelope.keys() or envelope.keys() - _FIELDS - {"supersedes"}:
        raise EvidenceError("Evidence envelope has missing or forbidden fields")
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != SCHEMA_VERSION:
        raise EvidenceError("unsupported Evidence schema version")
    if type(envelope["kind"]) is not str or not envelope["kind"].strip():
        raise EvidenceError("Evidence kind must be a nonblank capability-owned string")
    if type(envelope["producer"]) is not dict or type(envelope["payload"]) is not dict:
        raise EvidenceError("producer and opaque capability payload must be JSON objects")
    timestamp = envelope["created_at"]
    if type(timestamp) is not str or not _TIMESTAMP.fullmatch(timestamp):
        raise EvidenceError("created_at must be an exact UTC YYYY-MM-DDTHH:MM:SSZ timestamp")
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise EvidenceError("created_at is not a valid UTC date/time") from error
    if "supersedes" in envelope:
        prior = envelope["supersedes"]
        if type(prior) is not str or not _ID.fullmatch(prior):
            raise EvidenceError("supersedes must be an exact Evidence content ID")
    return envelope


def encode_evidence(repository: str, task: str, subject: str, envelope: dict[str, Any]) -> tuple[str, bytes]:
    """Capture canonical immutable bytes; encoding grants no write authority.

    The shared envelope binds the exact repository, Task, and immutable Git
    subject. Additional capability-specific subject identity belongs in payload
    and must be checked by that capability before it interprets the result.
    """
    _validate(envelope)
    return codec.encode_object("evidence", repository, task, subject, envelope)


def decode_evidence(data: bytes, *, evidence_id: str, repository: str, task: str,
                    subject: str) -> dict[str, Any]:
    """Validate bytes and exact applicability binding, not domain semantics.

    Pure decoding cannot establish referential integrity without a pinned
    metadata tree; use Evidence.read for persisted objects with references.
    """
    # Shared decode's optional expectations use None as 'unbound'. This API
    # instead requires every exact binding, including for arbitrary Python calls.
    codec.object_path("evidence", evidence_id)
    codec.encode_object("evidence", repository, task, subject, {})
    value = codec.decode_object(data, expected_id=evidence_id, expected_repository=repository,
                                expected_task=task, expected_subject=subject, expected_kind="evidence")
    _validate(value["payload"])
    return value


class Evidence:
    """Read exact objects and explicit reference integrity, never 'effective' facts.

    There is no mutable current pointer, enumeration/order policy, or write API.
    Both the requested object and all supersedes links resolve in the same
    pinned metadata commit. Cross-subject/Task/kind links have no Kernel-owned
    replacement meaning; only their identity and integrity are checked.
    """

    def __init__(self, store: MetadataStore) -> None:
        self._store = store

    def read(self, commit: str, evidence_id: str, *, task: str, subject: str) -> dict[str, Any]:
        with self._store.validation_scope():
            return self._read(commit, evidence_id, task=task, subject=subject)

    def _read(self, commit: str, evidence_id: str, *, task: str, subject: str) -> dict[str, Any]:
        value = self._store.read_object(commit, "evidence", evidence_id, task=task, subject=subject)
        current = _validate(value["payload"])
        visited = {evidence_id}
        for _ in range(MAX_SUPERSESSION_LINKS + 1):
            prior = current.get("supersedes")
            if prior is None:
                return value
            if prior in visited:
                raise EvidenceError("cyclic Evidence supersession reference")
            if len(visited) > MAX_SUPERSESSION_LINKS:
                raise EvidenceError("Evidence supersession chain exceeds the validation limit")
            visited.add(prior)
            target = self._store.read_object_by_id(commit, "evidence", prior)
            current = _validate(target["payload"])
        raise EvidenceError("Evidence supersession chain exceeds the validation limit")
