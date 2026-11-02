"""HTTP server for the sample API with fresh random API keys per start."""

from __future__ import annotations

import json
import secrets
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from accessguard_sampleapp.app import SampleApplication, Variant
from accessguard_sampleapp.data import USERS


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)  # type: ignore[arg-type]


@dataclass(frozen=True)
class RunningSampleApp:
    origin: str
    variant: Variant
    api_keys: Mapping[str, str]
    """User id -> API key. Test credentials for the runner; never commit them."""


def make_server(
    application: SampleApplication, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SampleProjectsAPI/1.0"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            """Silence per-request logging on stderr."""

        def _respond(self) -> None:
            status, body = application.handle(
                self.command, self.path, self.headers.get("X-Api-Key")
            )
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _respond  # noqa: N815 - required by BaseHTTPRequestHandler
        do_POST = _respond  # noqa: N815
        do_PUT = _respond  # noqa: N815
        do_DELETE = _respond  # noqa: N815

    return _QuietServer((host, port), Handler)


def new_api_keys() -> dict[str, str]:
    return {user.id: f"sk_sample_{secrets.token_urlsafe(24)}" for user in USERS}


@contextmanager
def run_sample_app(
    variant: Variant, *, port: int = 0, api_keys: Mapping[str, str] | None = None
) -> Iterator[RunningSampleApp]:
    """Serve ``variant`` on 127.0.0.1 in a background thread until the block exits."""
    keys = dict(api_keys) if api_keys is not None else new_api_keys()
    users = {keys[user.id]: user for user in USERS}
    server = make_server(SampleApplication(variant, users), port=port)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    try:
        yield RunningSampleApp(f"http://127.0.0.1:{server.server_port}", variant, keys)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
