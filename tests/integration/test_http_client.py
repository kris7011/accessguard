"""The scoped HTTP client against a real local socket server."""

import os
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from typing import ClassVar
from unittest import mock

from accessguard.adapters.http_target import ScopedHttpClient
from accessguard.domain.credentials import Secret
from accessguard.domain.errors import TransportError
from accessguard.domain.scope import TargetScope
from accessguard_lab.server import QuietHTTPServer


class RecordingHandler(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, str]]] = []

    def log_message(self, format: str, *args: object) -> None:
        """Keep test output quiet."""

    def do_GET(self) -> None:
        RecordingHandler.seen.append({"path": self.path, **dict(self.headers.items())})
        if self.path == "/huge":
            payload = b"x" * 2_000_000
            content_type = "application/octet-stream"
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path == "/bad-json":
            payload = b"{not json"
            content_type = "application/json"
        else:
            payload = b'{"ok": "yes"}'
            content_type = "application/json; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def local_server() -> Iterator[TargetScope]:
    RecordingHandler.seen = []
    server = QuietHTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield TargetScope.from_origin(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ScopedHttpClientTests(unittest.TestCase):
    def test_json_response_and_headers(self) -> None:
        with local_server() as scope:
            obs = ScopedHttpClient(scope).get("/ok", Secret("token-abcdefghijklmnop"))
        self.assertEqual(
            (obs.status, obs.content_type, obs.json_body), (200, "application/json", {"ok": "yes"})
        )
        request = RecordingHandler.seen[0]
        self.assertEqual(request["Host"], scope.host_header)
        self.assertEqual(request["Authorization"], "Bearer token-abcdefghijklmnop")

    def test_anonymous_requests_carry_no_credential(self) -> None:
        with local_server() as scope:
            ScopedHttpClient(scope).get("/ok", None)
        self.assertNotIn("Authorization", RecordingHandler.seen[0])

    def test_oversized_response_is_rejected(self) -> None:
        with local_server() as scope, self.assertRaisesRegex(TransportError, "byte limit"):
            ScopedHttpClient(scope, max_response_bytes=1_000_000).get("/huge", None)

    def test_redirects_are_never_followed(self) -> None:
        with local_server() as scope:
            obs = ScopedHttpClient(scope).get("/redirect", None)
        self.assertEqual(obs.status, 302)
        self.assertEqual(len(RecordingHandler.seen), 1)

    def test_malformed_json_is_not_parsed(self) -> None:
        with local_server() as scope:
            obs = ScopedHttpClient(scope).get("/bad-json", None)
        self.assertFalse(obs.is_json)
        self.assertEqual(obs.string_leaves(), frozenset())

    def test_proxy_environment_is_ignored(self) -> None:
        bogus = "http://127.0.0.1:9"
        with (
            mock.patch.dict(os.environ, {"HTTP_PROXY": bogus, "http_proxy": bogus, "NO_PROXY": ""}),
            local_server() as scope,
        ):
            self.assertEqual(ScopedHttpClient(scope).get("/ok", None).status, 200)

    def test_connection_refused_is_transport_error(self) -> None:
        with local_server() as scope:
            pass  # server is closed again: nothing listens on the port
        with self.assertRaises(TransportError):
            ScopedHttpClient(scope, timeout_seconds=1).get("/ok", None)


if __name__ == "__main__":
    unittest.main()
