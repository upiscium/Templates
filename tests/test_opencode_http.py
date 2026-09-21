from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "components/agent-core/.automation/bin/opencode_http.py"
SPEC = importlib.util.spec_from_file_location("opencode_http_test_target", MODULE_PATH)
assert SPEC and SPEC.loader
opencode_http = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = opencode_http
SPEC.loader.exec_module(opencode_http)


class _FakeOpenCodeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _request(self) -> None:
        length = self.headers.get("Content-Length")
        body = self.rfile.read(int(length)) if length else b""
        server = self.server
        assert isinstance(server, _FakeOpenCodeHTTPServer)
        server.calls.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        with server.response_lock:
            if server.responses:
                status, response_body, headers, omit_length = server.responses.pop(0)
            else:
                status, response_body, headers, omit_length = (
                    500,
                    b"no queued response",
                    {},
                    False,
                )
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if not omit_length:
            self.send_header("Content-Length", str(len(response_body)))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if response_body:
            self.wfile.write(response_body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._request()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._request()


class _FakeOpenCodeHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeOpenCodeHandler)
        self.calls: list[dict[str, Any]] = []
        self.responses: list[tuple[int, bytes, dict[str, str], bool]] = []
        self.response_lock = threading.Lock()

    def queue_json(self, value: object, status: int = 200) -> None:
        self.responses.append(
            (status, json.dumps(value, separators=(",", ":")).encode("utf-8"), {}, False)
        )

    def queue_empty(self, status: int = 204) -> None:
        self.responses.append((status, b"", {}, False))

    def queue_raw(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        omit_length: bool = False,
    ) -> None:
        self.responses.append((status, body, headers or {}, omit_length))


class FakeOpenCode:
    def __enter__(self) -> "FakeOpenCode":
        self.server = _FakeOpenCodeHTTPServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def port(self) -> int:
        return self.server.server_port

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.server.calls

    def queue_json(self, value: object, status: int = 200) -> None:
        self.server.queue_json(value, status)

    def queue_empty(self, status: int = 204) -> None:
        self.server.queue_empty(status)

    def queue_raw(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        omit_length: bool = False,
    ) -> None:
        self.server.queue_raw(
            body, status=status, headers=headers, omit_length=omit_length
        )


class OpenCodeHTTPAdapterTest(unittest.TestCase):
    password = "test-password/with secret"

    def client(self, port: int, **kwargs: object) -> Any:
        return opencode_http.OpenCodeHTTPAdapter(
            port, self.password, **kwargs
        )

    def assert_authenticated(self, call: dict[str, Any]) -> None:
        expected = "Basic " + base64.b64encode(
            f"opencode:{self.password}".encode("utf-8")
        ).decode("ascii")
        self.assertEqual(call["headers"]["authorization"], expected)
        self.assertNotIn("?", call["path"])

    def test_v1_methods_use_loopback_basic_auth_and_exact_json_bodies(self) -> None:
        with FakeOpenCode() as fake:
            fake.queue_json({"healthy": True, "version": "1"})
            fake.queue_json({"id": "session-1", "title": "control"})
            fake.queue_empty()
            fake.queue_json({"session-1": {"type": "busy"}})
            fake.queue_json({"id": "session/id", "title": "exact"})
            fake.queue_json([{"id": "permission/1", "permission": "file.read"}])
            fake.queue_empty()
            client = self.client(fake.port, agent="general")

            self.assertEqual(client.base_url, f"http://127.0.0.1:{fake.port}")
            self.assertEqual(client.global_health()["healthy"], True)
            self.assertEqual(client.create_session("control")["id"], "session-1")
            self.assertIsNone(
                client.prompt_async("session/id with space", "hello", "general")
            )
            self.assertEqual(client.session_status(), {"session-1": {"type": "busy"}})
            self.assertEqual(
                client.get_session("session/id")["title"], "exact"
            )
            self.assertEqual(len(client.list_permissions()), 1)
            self.assertIsNone(client.reply("permission/1", "once"))

            calls = fake.calls
            self.assertEqual(
                [call["method"] for call in calls],
                ["GET", "POST", "POST", "GET", "GET", "GET", "POST"],
            )
            self.assertEqual(
                [call["path"] for call in calls],
                [
                    "/global/health",
                    "/session",
                    "/session/session%2Fid%20with%20space/prompt_async",
                    "/session/status",
                    "/session/session%2Fid",
                    "/permission",
                    "/permission/permission%2F1/reply",
                ],
            )
            for call in calls:
                self.assert_authenticated(call)
            self.assertEqual(
                json.loads(calls[1]["body"]), {"title": "control"}
            )
            self.assertEqual(
                json.loads(calls[2]["body"]),
                {
                    "agent": "general",
                    "parts": [{"type": "text", "text": "hello"}],
                },
            )
            self.assertEqual(json.loads(calls[6]["body"]), {"reply": "once"})

    def test_malformed_and_oversized_json_are_rejected_without_secret_details(self) -> None:
        with FakeOpenCode() as fake:
            fake.queue_raw(
                b'{"secret":"test-password/with secret",',
                headers={"Content-Type": "application/json"},
            )
            client = self.client(fake.port)
            with self.assertRaises(opencode_http.OpenCodeProtocolError) as context:
                client.health()
            message = str(context.exception)
            self.assertNotIn(self.password, message)
            self.assertNotIn(
                "Basic "
                + base64.b64encode(
                    f"opencode:{self.password}".encode("utf-8")
                ).decode("ascii"),
                message,
            )

        with FakeOpenCode() as fake:
            fake.queue_raw(b"{}" * 20)
            client = self.client(fake.port, max_response_bytes=16)
            with self.assertRaisesRegex(
                opencode_http.OpenCodeProtocolError, "exceeds 16 bytes"
            ):
                client.health()

    def test_http_errors_do_not_echo_response_secrets_or_auth_headers(self) -> None:
        auth = "Basic " + base64.b64encode(
            f"opencode:{self.password}".encode("utf-8")
        ).decode("ascii")
        with FakeOpenCode() as fake:
            fake.queue_raw(
                f"password={self.password}; Authorization: {auth}".encode("utf-8"),
                status=401,
            )
            with self.assertRaises(opencode_http.OpenCodeHTTPError) as context:
                self.client(fake.port).health()
            message = str(context.exception)
            self.assertIn("HTTP 401", message)
            self.assertNotIn(self.password, message)
            self.assertNotIn(auth, message)

    def test_reply_values_and_prompt_agent_are_explicit(self) -> None:
        with FakeOpenCode() as fake:
            client = self.client(fake.port)
            with self.assertRaises(ValueError):
                client.reply("permission", "later")
            with self.assertRaises(ValueError):
                client.prompt_async("session", "hello")

    def test_timeout_and_port_validation_are_bounded_inputs(self) -> None:
        with self.assertRaises(ValueError):
            opencode_http.OpenCodeHTTPAdapter(0, self.password)
        with self.assertRaises(ValueError):
            opencode_http.OpenCodeHTTPAdapter(65536, self.password)
        with self.assertRaises(ValueError):
            opencode_http.OpenCodeHTTPAdapter(1, self.password, timeout=0)


if __name__ == "__main__":
    unittest.main()
