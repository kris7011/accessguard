"""The lab HTTP API with switchable, documented access-control flaws.

Routes (GET only):

* ``/me``                     authenticated subject (session probe)
* ``/documents``              documents visible to the caller
* ``/documents/{id}``         one document (own tenant or explicitly shared)
* ``/invoices/{id}``          one invoice (own tenant, admin role only)
* ``/edge/{behaviour}/{id}``  owner-tenant users get document ``id``; everyone else gets
  the named misbehaviour. Used to test how the engine treats ambiguous responses,
  including 403 bodies in formats the engine cannot analyse (``*-denial``).

Credentials are random per lab start; there are no fixed tokens.
"""

from __future__ import annotations

import html
import json
import secrets
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from accessguard.domain.credentials import Secret
from accessguard_lab.dataset import DOCUMENTS, INVOICES, USERS, LabDocument, LabUser


class Flaw(StrEnum):
    DOCUMENT_IDOR = "document-idor"
    """GET /documents/{id} skips the tenant/share check (classic IDOR/BOLA)."""
    SHARE_OVERBROAD = "share-overbroad"
    """Any shared document is treated as visible to every authenticated user."""
    INVOICE_ROLE_BYPASS = "invoice-role-bypass"
    """GET /invoices/{id} checks the tenant but not the admin role."""
    LIST_LEAK = "list-leak"
    """GET /documents returns documents of every tenant."""
    SHARE_IGNORED = "share-ignored"
    """Over-restrictive "fix": explicit shares are no longer honoured."""
    DENIAL_BODY_LEAK = "denial-body-leak"
    """GET /documents/{id} answers 403 but echoes the document in the error body."""


Response = tuple[int, dict[str, object] | str, str]

_JSON = "application/json"
_EDGE_BEHAVIOURS = frozenset(
    {
        "soft-error",
        "login-page",
        "redirect",
        "rate-limited",
        "server-error",
        "partial",
        "flaky",
        "slow",
        "leaky-denial",
        "html-denial",
        "text-denial",
        "invalid-json-denial",
        "html-as-json-denial",
        "mislabelled-json-denial",
        "empty-denial",
    }
)
SLOW_RESPONSE_SECONDS = 1.5


class LabApplication:
    """Pure request handling, testable without sockets."""

    def __init__(self, flaws: frozenset[Flaw], users_by_token: Mapping[str, LabUser]) -> None:
        self._flaws = flaws
        self._users_by_token = users_by_token
        self._documents = {d.id: d for d in DOCUMENTS}
        self._invoices = {i.id: i for i in INVOICES}
        self._flaky_served: set[str] = set()
        self._lock = threading.Lock()

    def handle(self, method: str, path: str, authorization: str | None) -> Response:
        if method != "GET":
            return 405, {"error": "method not allowed"}, _JSON
        user = self._authenticate(authorization)
        segments = path.split("/")[1:]
        if segments[:1] == ["edge"] and len(segments) == 3:
            return self._edge(segments[1], segments[2], user)
        if user is None:
            return 401, {"error": "unauthorized"}, _JSON
        if path == "/me":
            return 200, {"user": user.id, "tenant": user.tenant, "role": user.role}, _JSON
        if path == "/documents":
            items = [_document_body(d) for d in DOCUMENTS if self._listed(user, d)]
            return 200, {"items": items}, _JSON
        if len(segments) == 2 and segments[0] == "documents":
            return self._document(user, segments[1])
        if len(segments) == 2 and segments[0] == "invoices":
            return self._invoice(user, segments[1])
        return 404, {"error": "not found"}, _JSON

    def _authenticate(self, authorization: str | None) -> LabUser | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        user = self._users_by_token.get(authorization.removeprefix("Bearer "))
        if user is None or user.expired:
            return None
        return user

    def _visible(self, user: LabUser, document: LabDocument) -> bool:
        if document.tenant == user.tenant:
            return True
        if not document.shared_with or Flaw.SHARE_IGNORED in self._flaws:
            return False
        return Flaw.SHARE_OVERBROAD in self._flaws or user.tenant in document.shared_with

    def _listed(self, user: LabUser, document: LabDocument) -> bool:
        return Flaw.LIST_LEAK in self._flaws or self._visible(user, document)

    def _document(self, user: LabUser, document_id: str) -> Response:
        document = self._documents.get(document_id)
        if document is None:
            return 404, {"error": "not found"}, _JSON
        if Flaw.DOCUMENT_IDOR not in self._flaws and not self._visible(user, document):
            if Flaw.DENIAL_BODY_LEAK in self._flaws:
                return 403, {"error": "forbidden", "requested": _document_body(document)}, _JSON
            return 403, {"error": "forbidden"}, _JSON
        return 200, _document_body(document), _JSON

    def _invoice(self, user: LabUser, invoice_id: str) -> Response:
        invoice = self._invoices.get(invoice_id)
        if invoice is None:
            return 404, {"error": "not found"}, _JSON
        if invoice.tenant != user.tenant:
            return 403, {"error": "forbidden"}, _JSON
        if user.role != "admin" and Flaw.INVOICE_ROLE_BYPASS not in self._flaws:
            return 403, {"error": "admin role required"}, _JSON
        return 200, asdict(invoice), _JSON

    def _edge(self, behaviour: str, document_id: str, user: LabUser | None) -> Response:
        document = self._documents.get(document_id)
        if behaviour not in _EDGE_BEHAVIOURS or document is None:
            return 404, {"error": "not found"}, _JSON
        if user is not None and user.tenant == document.tenant:
            return 200, _document_body(document), _JSON
        if behaviour == "leaky-denial":
            return 403, {"error": "forbidden", "debug": _document_body(document)}, _JSON
        if behaviour.endswith("-denial"):
            return _unanalysable_denial(behaviour, document)
        if behaviour == "soft-error":
            return 200, {"error": "forbidden", "success": False}, _JSON
        if behaviour == "login-page":
            return 200, "<html><body><form>Log in</form></body></html>", "text/html"
        if behaviour == "redirect":
            return 302, "http://169.254.169.254/latest/meta-data/", _JSON
        if behaviour == "rate-limited":
            return 429, {"error": "too many requests"}, _JSON
        if behaviour == "server-error":
            return 500, {"error": "internal error"}, _JSON
        if behaviour == "partial":
            return 200, {"id": document.id, "title": document.title}, _JSON
        if behaviour == "slow":
            time.sleep(SLOW_RESPONSE_SECONDS)
            return 200, _document_body(document), _JSON
        # "flaky": leaks exactly once, then denies; models a non-reproducible observation.
        with self._lock:
            first = document.id not in self._flaky_served
            self._flaky_served.add(document.id)
        return (
            (200, _document_body(document), _JSON)
            if first
            else (403, {"error": "forbidden"}, _JSON)
        )


def _unanalysable_denial(behaviour: str, document: LabDocument) -> Response:
    """A 403 whose body the engine cannot parse; all but ``empty-denial`` leak the document.

    Models error pages in formats the engine does not analyse (HTML, plain text), broken
    JSON and a Content-Type that does not match the body.
    """
    as_json = json.dumps({"error": "forbidden", "requested": _document_body(document)})
    page = (
        f"<html><body><h1>Forbidden</h1><p>{html.escape(document.title)}</p>"
        f"<pre>{html.escape(document.canary)}</pre></body></html>"
    )
    bodies: dict[str, tuple[str, str]] = {
        "html-denial": (page, "text/html"),
        "text-denial": (f"Forbidden: {document.title} {document.canary}", "text/plain"),
        "invalid-json-denial": (as_json[:-1], _JSON),
        "html-as-json-denial": (page, _JSON),
        "mislabelled-json-denial": (as_json, "text/plain"),
        "empty-denial": ("", _JSON),
    }
    body, content_type = bodies[behaviour]
    return 403, body, content_type


def _document_body(document: LabDocument) -> dict[str, object]:
    return {
        "id": document.id,
        "tenant": document.tenant,
        "title": document.title,
        "canary": document.canary,
        "shared_with": sorted(document.shared_with),
    }


class QuietHTTPServer(ThreadingHTTPServer):
    """Ignores clients that disconnect early (timeouts, size limits); reports anything else."""

    def handle_error(self, request: object, client_address: object) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)  # type: ignore[arg-type]


@dataclass(frozen=True)
class LabInstance:
    origin: str
    credentials: Mapping[str, Secret]
    flaws: frozenset[Flaw]


@contextmanager
def run_lab(flaws: frozenset[Flaw] = frozenset()) -> Iterator[LabInstance]:
    """Start the lab on 127.0.0.1 with an ephemeral port and fresh random credentials."""
    tokens = {user.id: secrets.token_urlsafe(24) for user in USERS}
    users_by_token = {tokens[user.id]: user for user in USERS}
    application = LabApplication(flaws, users_by_token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Silence request logging; it would otherwise print to stderr."""

        def _respond(self) -> None:
            status, body, content_type = application.handle(
                self.command, self.path, self.headers.get("Authorization")
            )
            if status in (301, 302, 303, 307, 308) and isinstance(body, str):
                payload = b""
                self.send_response(status)
                self.send_header("Location", body)
            else:
                payload = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _respond  # noqa: N815 - required by BaseHTTPRequestHandler
        do_POST = _respond  # noqa: N815
        do_PUT = _respond  # noqa: N815
        do_DELETE = _respond  # noqa: N815

    server = QuietHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    try:
        yield LabInstance(
            origin=f"http://127.0.0.1:{server.server_port}",
            credentials={user_id: Secret(token) for user_id, token in tokens.items()},
            flaws=flaws,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
