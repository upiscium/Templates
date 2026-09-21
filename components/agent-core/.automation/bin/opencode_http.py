#!/usr/bin/env python3
"""Small, pinned OpenCode V1 HTTP client for the local loopback server.

The client deliberately accepts a port instead of an arbitrary URL.  This
keeps the API limited to the local OpenCode server and makes it impossible to
accidentally put credentials in a URL or follow a redirect to another host.
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import re
from typing import Any
from urllib.parse import quote


USERNAME = "opencode"
DEFAULT_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
REPLIES = frozenset(("once", "always", "reject"))


class OpenCodeError(RuntimeError):
    """Base class for safe adapter failures."""


class OpenCodeTransportError(OpenCodeError):
    """The loopback request could not be completed."""


class OpenCodeHTTPError(OpenCodeError):
    """OpenCode returned a non-success HTTP status."""


class OpenCodeProtocolError(OpenCodeError):
    """OpenCode returned an invalid or unbounded response."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number: {value}")


def _parse_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _require_text(
    value: object,
    name: str,
    *,
    allow_empty: bool = False,
    allow_controls: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{name} must be a non-empty string")
    if not allow_controls and any(ord(character) < 0x20 for character in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _require_port(port: object) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 through 65535")
    return port


def _require_timeout(timeout: object) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a positive finite number")
    value = float(timeout)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive finite number")
    return value


def _require_limit(limit: object, name: str) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"{name} must be a positive integer")
    return limit


class OpenCodeHTTPAdapter:
    """Authenticated OpenCode V1 adapter pinned to ``127.0.0.1``.

    ``agent`` is optional at construction time so callers can pin one agent
    for all prompts.  A prompt may also provide it explicitly; in either case
    it is written into the request body rather than selected by the server.
    """

    def __init__(
        self,
        port: int,
        password: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        agent: str | None = None,
    ) -> None:
        self.port = _require_port(port)
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        self._password = password
        self.timeout = _require_timeout(timeout)
        self.max_response_bytes = _require_limit(
            max_response_bytes, "max_response_bytes"
        )
        self.agent = (
            None if agent is None else _require_text(agent, "agent")
        )
        self.base_url = f"http://127.0.0.1:{self.port}"
        encoded = base64.b64encode(f"{USERNAME}:{password}".encode("utf-8"))
        self._authorization_token = encoded.decode("ascii")
        self._authorization = f"Basic {self._authorization_token}"

    def _redact(self, value: object) -> str:
        """Return an error detail with the password and auth value removed."""

        detail = str(value)
        detail = detail.replace(self._authorization, "Authorization: <redacted>")
        detail = detail.replace(self._authorization_token, "<redacted>")
        if self._password:
            detail = detail.replace(self._password, "<redacted>")
        detail = re.sub(
            r"(?i)(authorization\s*[:=]\s*)(?:basic\s+)?[^\s,;]+",
            r"\1<redacted>",
            detail,
        )
        return detail

    def _operation(self, method: str, path: str) -> str:
        return f"{method} {self._redact(path)}"

    @staticmethod
    def _validate_path(path: str) -> None:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("request path must be a path without query or fragment")

    def _encode_json(self, method: str, path: str, payload: object) -> bytes:
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise OpenCodeProtocolError(
                f"{self._operation(method, path)} request body is not valid JSON"
            ) from None
        if len(encoded) > MAX_REQUEST_BYTES:
            raise OpenCodeProtocolError(
                f"{self._operation(method, path)} request body exceeds "
                f"{MAX_REQUEST_BYTES} bytes"
            )
        return encoded

    def _read_bounded(self, response: http.client.HTTPResponse, operation: str) -> bytes:
        declared = response.getheader("Content-Length")
        if declared is not None:
            value = declared.strip()
            if not value.isdigit():
                raise OpenCodeProtocolError(
                    f"{operation} returned an invalid Content-Length"
                )
            try:
                length = int(value, 10)
            except (TypeError, ValueError, OverflowError):
                raise OpenCodeProtocolError(
                    f"{operation} returned an invalid Content-Length"
                ) from None
            if length < 0:
                raise OpenCodeProtocolError(
                    f"{operation} returned an invalid Content-Length"
                )
            if length > self.max_response_bytes:
                raise OpenCodeProtocolError(
                    f"{operation} response exceeds {self.max_response_bytes} bytes"
                )

        result = bytearray()
        while len(result) <= self.max_response_bytes:
            chunk = response.read(self.max_response_bytes - len(result) + 1)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise OpenCodeProtocolError(
                    f"{operation} returned a non-byte response body"
                )
            result.extend(chunk)
            if len(result) > self.max_response_bytes:
                raise OpenCodeProtocolError(
                    f"{operation} response exceeds {self.max_response_bytes} bytes"
                )
        return bytes(result)

    def _decode_json(self, operation: str, body: bytes) -> Any:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise OpenCodeProtocolError(
                f"{operation} returned invalid UTF-8 JSON"
            ) from None
        try:
            return json.loads(
                text,
                strict=True,
                parse_constant=_reject_json_constant,
                parse_float=_parse_json_float,
                object_pairs_hook=_unique_json_object,
            )
        except (json.JSONDecodeError, RecursionError, ValueError):
            raise OpenCodeProtocolError(
                f"{operation} returned malformed JSON"
            ) from None

    def _request(self, method: str, path: str, payload: object | None = None) -> Any:
        self._validate_path(path)
        operation = self._operation(method, path)
        body = None if payload is None else self._encode_json(method, path, payload)
        headers = {
            "Accept": "application/json",
            "Authorization": self._authorization,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))

        connection: http.client.HTTPConnection | None = None
        response: http.client.HTTPResponse | None = None
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", self.port, timeout=self.timeout
            )
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = self._read_bounded(response, operation)
            status = response.status
            if not 200 <= status < 300:
                reason = response.reason or "unknown status"
                raise OpenCodeHTTPError(
                    f"{operation} returned HTTP {status} ({self._redact(reason)})"
                )
            if not response_body:
                return None
            return self._decode_json(operation, response_body)
        except OpenCodeError:
            raise
        except (http.client.HTTPException, OSError, TimeoutError) as exc:
            raise OpenCodeTransportError(
                f"{operation} failed: {self._redact(exc)}"
            ) from None
        except Exception as exc:
            # Keep unexpected client-library failures on the same safe error
            # boundary; in particular, never surface a library message that
            # might contain request headers.
            raise OpenCodeTransportError(
                f"{operation} failed: {self._redact(exc)}"
            ) from None
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

    def _json_request(self, method: str, path: str, payload: object | None = None) -> Any:
        value = self._request(method, path, payload)
        if value is None:
            raise OpenCodeProtocolError(
                f"{self._operation(method, path)} returned an empty JSON body"
            )
        return value

    @staticmethod
    def _mapping(value: object, operation: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise OpenCodeProtocolError(f"{operation} expected a JSON object")
        return value

    def global_health(self) -> dict[str, Any]:
        """Get the authenticated OpenCode global health document."""

        operation = self._operation("GET", "/global/health")
        return self._mapping(self._json_request("GET", "/global/health"), operation)

    health = global_health

    def create_session(self, title: str | None = None) -> dict[str, Any]:
        """Create an authenticated OpenCode session."""

        payload: dict[str, str] = {}
        if title is not None:
            payload["title"] = _require_text(title, "title", allow_empty=True)
        operation = self._operation("POST", "/session")
        return self._mapping(
            self._json_request("POST", "/session", payload), operation
        )

    @staticmethod
    def _quoted_id(identifier: str, name: str) -> str:
        value = _require_text(identifier, name)
        try:
            return quote(value, safe="")
        except UnicodeError as exc:
            raise ValueError(f"{name} is not a valid identifier") from exc

    def prompt_async(
        self, session_id: str, text: str, agent: str | None = None
    ) -> Any:
        """Submit one text part to a caller-selected fixed agent."""

        session_path = self._quoted_id(session_id, "session_id")
        prompt_agent = self.agent if agent is None else _require_text(agent, "agent")
        if prompt_agent is None:
            raise ValueError("agent must be supplied for prompt_async")
        prompt_text = _require_text(
            text, "text", allow_empty=True, allow_controls=True
        )
        path = f"/session/{session_path}/prompt_async"
        payload = {
            "agent": prompt_agent,
            "parts": [{"type": "text", "text": prompt_text}],
        }
        return self._request("POST", path, payload)

    def session_status(self, session_id: str | None = None) -> dict[str, Any]:
        """Get all session statuses, or retrieve one exact session by ID."""

        if session_id is None:
            path = "/session/status"
        else:
            path = f"/session/{self._quoted_id(session_id, 'session_id')}"
        operation = self._operation("GET", path)
        return self._mapping(self._json_request("GET", path), operation)

    get_session_status = session_status

    def get_session(self, session_id: str) -> dict[str, Any]:
        """Retrieve one exact session using a URL-quoted ID."""

        return self.session_status(session_id)

    def list_permissions(self) -> list[Any]:
        """List pending authenticated OpenCode permission requests."""

        path = "/permission"
        value = self._json_request("GET", path)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise OpenCodeProtocolError(
                f"{self._operation('GET', path)} returned an invalid permission list"
            )
        return value

    permissions = list_permissions

    def reply(self, permission_id: str, reply: str) -> Any:
        """Answer a permission request with ``once``, ``always``, or ``reject``."""

        if not isinstance(reply, str) or reply not in REPLIES:
            raise ValueError("reply must be one of: once, always, reject")
        path = f"/permission/{self._quoted_id(permission_id, 'permission_id')}/reply"
        return self._request("POST", path, {"reply": reply})

    reply_permission = reply


OpenCodeClient = OpenCodeHTTPAdapter
OpenCodeV1Client = OpenCodeHTTPAdapter


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "OpenCodeClient",
    "OpenCodeError",
    "OpenCodeHTTPAdapter",
    "OpenCodeHTTPError",
    "OpenCodeProtocolError",
    "OpenCodeTransportError",
    "OpenCodeV1Client",
    "REPLIES",
    "USERNAME",
]
