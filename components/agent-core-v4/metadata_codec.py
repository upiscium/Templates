"""Canonical, versioned Git metadata objects for Agent Core v4.

This module deliberately defines only the shared metadata envelope.  The
``payload`` is opaque here; its meaning and schema belong to its capability.
Decoded objects are ordinary dictionaries containing exactly the envelope
fields ``schema_version``, ``kind``, ``repository``, ``task``, ``subject``,
and ``payload``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_OBJECT_BYTES = MAX_PAYLOAD_BYTES + 4096
MAX_JSON_DEPTH = 64
MAX_TASK_LENGTH = 128
MAX_REPOSITORY_LENGTH = 512
KINDS = frozenset({"task-record", "contract", "evidence", "task-view-snapshot"})

_DOMAIN = b"agentcore-metadata-object/v1\n"
_ENVELOPE_KEYS = frozenset(
    {"schema_version", "kind", "repository", "task", "subject", "payload"}
)
_TASK_RE = re.compile(r"[1-9][0-9]*\Z", re.ASCII)
_REPOSITORY_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_OBJECT_ID_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MIN_INT = -(2**63)
_MAX_INT = 2**63 - 1


class MetadataCodecError(ValueError):
    """Raised when an object is invalid or is not in canonical wire form."""


def _validate_kind(kind: object) -> str:
    if type(kind) is not str or kind not in KINDS:
        raise MetadataCodecError("unknown metadata object kind")
    return kind


def _validate_repository(repository: object) -> str:
    if type(repository) is not str or len(repository) > MAX_REPOSITORY_LENGTH or repository.count("/") != 1:
        raise MetadataCodecError("repository must be an owner/name identity")
    owner, name = repository.split("/", 1)
    if not _REPOSITORY_PART_RE.fullmatch(owner) or not _REPOSITORY_PART_RE.fullmatch(name):
        raise MetadataCodecError("repository contains a malformed owner or name")
    return repository


def _validate_task(task: object) -> str:
    if type(task) is not str or len(task) > MAX_TASK_LENGTH or not _TASK_RE.fullmatch(task):
        raise MetadataCodecError("task must be a canonical positive decimal string")
    return task


def _validate_subject(subject: object) -> str:
    if type(subject) is not str or not _OID_RE.fullmatch(subject):
        raise MetadataCodecError("subject must be a full lowercase 40- or 64-hex Git OID")
    return subject


def _validate_json_value(value: object, location: str = "payload", depth: int = 0) -> None:
    """Validate the intentionally narrower-than-JSON payload value domain."""
    if depth > MAX_JSON_DEPTH:
        raise MetadataCodecError("JSON nesting exceeds the 64-level limit")
    value_type = type(value)
    if value is None or value_type is bool:
        return
    if value_type is int:
        if not _MIN_INT <= value <= _MAX_INT:
            raise MetadataCodecError(f"{location} integer is outside the signed 64-bit range")
        return
    if value_type is str:
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise MetadataCodecError(f"{location} contains a surrogate Unicode code point")
        return
    if value_type is list:
        for item in value:
            _validate_json_value(item, location, depth + 1)
        return
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise MetadataCodecError(f"{location} object keys must be strings")
            _validate_json_value(key, "object key")
            # Opaque payload keys can be very long. Copying the full ancestor
            # key into a diagnostic path for every sibling is quadratic work
            # on an otherwise byte-bounded, valid metadata object.
            _validate_json_value(item, location, depth + 1)
        return
    raise MetadataCodecError(f"{location} contains an unsupported JSON value")


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise MetadataCodecError("object cannot be represented as canonical JSON") from error


def _validate_payload(payload: object) -> dict[str, Any]:
    if type(payload) is not dict:
        raise MetadataCodecError("payload must be an object")
    try:
        # The wire object contains the payload one level beneath the envelope.
        # Apply the same depth budget here as when decoding the full envelope.
        _validate_json_value(payload, depth=1)
    except RecursionError as error:
        raise MetadataCodecError("payload nesting is too deep") from error
    payload_bytes = _canonical_json_bytes(payload)
    if len(payload_bytes) > MAX_PAYLOAD_BYTES:
        raise MetadataCodecError("payload exceeds the 1 MiB limit")
    return payload


def _reject_float(_value: str) -> None:
    raise MetadataCodecError("floating-point JSON values are not supported")


def _reject_constant(_value: str) -> None:
    raise MetadataCodecError("non-standard JSON constants are not supported")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MetadataCodecError("duplicate JSON object key")
        result[key] = value
    return result


def encode_object(
    kind: str,
    repository: str,
    task: str,
    subject: str,
    payload: dict[str, Any],
) -> tuple[str, bytes]:
    """Return ``(object_id, canonical_bytes)`` for a v1 metadata envelope.

    ``object_id`` is the lowercase SHA-256 digest of the domain prefix plus
    the canonical JSON bytes.  Payload contents are validated as JSON
    primitives but are otherwise left to their capability-owned schema.
    """
    kind = _validate_kind(kind)
    repository = _validate_repository(repository)
    task = _validate_task(task)
    subject = _validate_subject(subject)
    payload = _validate_payload(payload)
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "repository": repository,
        "task": task,
        "subject": subject,
        "payload": payload,
    }
    data = _canonical_json_bytes(envelope)
    if len(data) > MAX_OBJECT_BYTES:
        raise MetadataCodecError("metadata object exceeds the envelope byte limit")
    object_id = hashlib.sha256(_DOMAIN + data).hexdigest()
    return object_id, data


def decode_object(
    data: bytes,
    *,
    expected_id: str | None = None,
    expected_repository: str | None = None,
    expected_task: str | None = None,
    expected_subject: str | None = None,
    expected_kind: str | None = None,
) -> dict[str, Any]:
    """Decode and verify canonical bytes, returning an ordinary envelope dict.

    Canonical bytes and the v1 closed envelope are always verified. Supply
    ``expected_id`` to bind those bytes to a content-addressed path or caller
    identity; without it the digest is computed but has nothing to compare to.
    Other expected values additionally bind the decoded object to its identity.
    """
    if type(data) is not bytes:
        raise MetadataCodecError("metadata object data must be bytes")
    if len(data) > MAX_OBJECT_BYTES:
        raise MetadataCodecError("metadata object exceeds the envelope byte limit")
    try:
        text = data.decode("utf-8", errors="strict")
        envelope = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except MetadataCodecError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise MetadataCodecError("metadata object is not valid UTF-8 JSON") from error

    if type(envelope) is not dict:
        raise MetadataCodecError("metadata object envelope must be an object")
    try:
        _validate_json_value(envelope, "envelope")
    except RecursionError as error:
        raise MetadataCodecError("metadata object nesting is too deep") from error
    if _canonical_json_bytes(envelope) != data:
        raise MetadataCodecError("metadata object bytes are not canonical JSON")
    if envelope.keys() != _ENVELOPE_KEYS:
        raise MetadataCodecError("metadata object has missing or extra envelope fields")

    if type(envelope["schema_version"]) is not int:
        raise MetadataCodecError("unknown metadata object schema version")
    if envelope["schema_version"] != SCHEMA_VERSION:
        raise MetadataCodecError("unknown metadata object schema version")
    _validate_kind(envelope["kind"])
    _validate_repository(envelope["repository"])
    _validate_task(envelope["task"])
    _validate_subject(envelope["subject"])
    _validate_payload(envelope["payload"])

    actual_id = hashlib.sha256(_DOMAIN + data).hexdigest()
    if expected_id is not None:
        if type(expected_id) is not str or not _OBJECT_ID_RE.fullmatch(expected_id):
            raise MetadataCodecError("expected_id must be a lowercase SHA-256 object ID")
        if actual_id != expected_id:
            raise MetadataCodecError("metadata object digest does not match expected_id")

    expected_bindings = (
        ("repository", expected_repository, _validate_repository),
        ("task", expected_task, _validate_task),
        ("subject", expected_subject, _validate_subject),
        ("kind", expected_kind, _validate_kind),
    )
    for field, expected, validator in expected_bindings:
        if expected is not None and envelope[field] != validator(expected):
            raise MetadataCodecError(f"metadata object {field} does not match expected binding")
    return envelope


def object_path(kind: str, object_id: str) -> str:
    """Return the canonical relative Git tree path for a metadata object."""
    kind = _validate_kind(kind)
    if type(object_id) is not str or not _OBJECT_ID_RE.fullmatch(object_id):
        raise MetadataCodecError("object_id must be a lowercase SHA-256 object ID")
    return f"objects/{kind}/{object_id[:2]}/{object_id}.json"
