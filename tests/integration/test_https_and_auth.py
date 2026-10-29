"""TLS verification with IP pinning, and the credential placement schemes, over real sockets."""

import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import ClassVar

from accessguard.adapters.http_target import AuthKind, AuthScheme, ScopedHttpClient
from accessguard.domain.credentials import Secret
from accessguard.domain.errors import ScopeViolationError, TransportError
from accessguard.domain.scope import NetworkZone, TargetScope
from accessguard_lab.server import QuietHTTPServer


class EchoHandler(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, str]]] = []

    def log_message(self, format: str, *args: object) -> None:
        """Keep test output quiet."""

    def do_GET(self) -> None:
        EchoHandler.seen.append({"path": self.path, **dict(self.headers.items())})
        payload = b'{"ok": "yes"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def make_certificate(directory: Path) -> tuple[Path, Path]:
    """A throwaway self-signed certificate for ``localhost`` created by the openssl CLI."""
    openssl = shutil.which("openssl")
    if openssl is None:
        raise unittest.SkipTest("openssl CLI not available")
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(  # noqa: S603 - fixed arguments, test-only
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert, key


@contextmanager
def tls_server() -> Iterator[tuple[int, Path]]:
    EchoHandler.seen = []
    with tempfile.TemporaryDirectory() as tmp:
        cert, key = make_certificate(Path(tmp))
        server = QuietHTTPServer(("127.0.0.1", 0), EchoHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            yield server.server_port, cert
        finally:
            server.shutdown()
            server.server_close()


def localhost(host: str, port: int) -> list[str]:
    return ["127.0.0.1"]


class HttpsTests(unittest.TestCase):
    def test_trusted_certificate_is_accepted_over_a_pinned_address(self) -> None:
        with tls_server() as (port, cert):
            scope = TargetScope.authorized(f"https://localhost:{port}", zone=NetworkZone.LOOPBACK)
            trust = ssl.create_default_context(cafile=str(cert))
            client = ScopedHttpClient(scope, resolver=localhost, tls_context=trust)
            observation = client.get("/x", Secret("token-0123456789abcdef"))
        self.assertEqual(observation.status, 200)
        self.assertEqual(EchoHandler.seen[0]["Host"], f"localhost:{port}")

    def test_untrusted_certificate_is_rejected(self) -> None:
        with tls_server() as (port, _):
            scope = TargetScope.authorized(f"https://localhost:{port}", zone=NetworkZone.LOOPBACK)
            client = ScopedHttpClient(scope, resolver=localhost)
            with self.assertRaisesRegex(TransportError, "certificate verification failed"):
                client.get("/x", Secret("token-0123456789abcdef"))
        self.assertEqual(EchoHandler.seen, [], "no request may be sent over an untrusted channel")

    def test_wrong_host_name_is_rejected(self) -> None:
        with tls_server() as (port, cert):
            scope = TargetScope.authorized(
                f"https://other.localhost:{port}", zone=NetworkZone.LOOPBACK
            )
            trust = ssl.create_default_context(cafile=str(cert))
            client = ScopedHttpClient(scope, resolver=localhost, tls_context=trust)
            with self.assertRaises(TransportError):
                client.get("/x", None)

    def test_non_verifying_contexts_are_refused(self) -> None:
        scope = TargetScope.authorized("https://localhost:1", zone=NetworkZone.LOOPBACK)
        insecure = ssl.create_default_context()
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        with self.assertRaises(ValueError):
            ScopedHttpClient(scope, tls_context=insecure)


@contextmanager
def plain_server() -> Iterator[TargetScope]:
    EchoHandler.seen = []
    server = QuietHTTPServer(("127.0.0.1", 0), EchoHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield TargetScope.authorized(
            f"http://127.0.0.1:{server.server_port}",
            zone=NetworkZone.LOOPBACK,
            path_prefixes=("/api",),
        )
    finally:
        server.shutdown()
        server.server_close()


class AuthSchemeTests(unittest.TestCase):
    def test_each_scheme_places_the_credential_where_configured(self) -> None:
        secret = Secret("key-0123456789abcdef")
        cases = [
            (AuthScheme(), "Authorization", "Bearer key-0123456789abcdef"),
            (AuthScheme(AuthKind.HEADER, "X-Api-Key"), "X-Api-Key", "key-0123456789abcdef"),
            (AuthScheme(AuthKind.COOKIE, "session"), "Cookie", "session=key-0123456789abcdef"),
        ]
        for scheme, header, value in cases:
            with self.subTest(scheme=scheme.kind), plain_server() as scope:
                ScopedHttpClient(scope, auth=scheme).get("/api/x", secret)
                self.assertEqual(EchoHandler.seen[0][header], value)

    def test_anonymous_requests_carry_no_credential(self) -> None:
        with plain_server() as scope:
            ScopedHttpClient(scope, auth=AuthScheme(AuthKind.HEADER, "X-Api-Key")).get(
                "/api/x", None
            )
        self.assertNotIn("X-Api-Key", EchoHandler.seen[0])
        self.assertNotIn("Authorization", EchoHandler.seen[0])

    def test_paths_outside_the_prefixes_never_reach_the_network(self) -> None:
        with plain_server() as scope, self.assertRaises(ScopeViolationError):
            ScopedHttpClient(scope).get("/admin/users", None)
        self.assertEqual(EchoHandler.seen, [])

    def test_invalid_scheme_names_and_values_are_rejected(self) -> None:
        for kind, name in (
            (AuthKind.HEADER, "Host"),
            (AuthKind.HEADER, "Bad Header"),
            (AuthKind.COOKIE, "a;b"),
            (AuthKind.BEARER, "X-Other"),
        ):
            with self.subTest(kind=kind, name=name), self.assertRaises(ValueError):
                AuthScheme(kind, name)
        with self.assertRaises(TransportError) as raised:
            AuthScheme().headers(Secret("abc\r\nX-Injected: 1"))
        self.assertNotIn("Injected", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
