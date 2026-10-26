"""A data-driven synthetic multi-tenant API for evaluation fixtures (127.0.0.1 only).

Deliberately separate from the development lab: different routes, session endpoint,
data and flaw placements, so that strategies are not evaluated on the scenarios they
were developed against.

Routes (GET only): ``/session`` (authenticated subject), ``/api/{kind}`` (listing of the
caller's visible resources of that kind) and ``/api/{kind}/{id}``.

Correct authorization: a user may read a resource of their own tenant when their role is
listed in the resource's ``roles``, and any resource shared with their tenant.

Flaw types (each scoped to a resource ``kind`` or a single ``resource``):

* ``tenant_bypass``  item reads skip the tenant check (the role check still applies)
* ``role_bypass``    item reads skip the role check within the tenant
* ``anonymous_read`` item reads succeed without any credential
* ``list_leak``      listings return every resource of the kind
* ``denial_leak``    item denials answer 403 but echo the resource in the error body

The flaws are part of the fixture's *target* definition; they are never given to the
engine or to an agent.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler

from accessguard.domain.credentials import Secret
from accessguard_lab.server import QuietHTTPServer

FLAW_TYPES = frozenset(
    {"tenant_bypass", "role_bypass", "anonymous_read", "list_leak", "denial_leak"}
)


@dataclass(frozen=True)
class FixtureUser:
    id: str
    tenant: str
    role: str


@dataclass(frozen=True)
class FixtureResource:
    id: str
    kind: str
    tenant: str
    roles: frozenset[str]
    shared_with: frozenset[str]
    data: Mapping[str, str]

    def body(self) -> dict[str, object]:
        return {"id": self.id, **self.data}


@dataclass(frozen=True)
class Flaw:
    type: str
    kind: str | None = None
    resource: str | None = None

    def applies_to(self, resource: FixtureResource) -> bool:
        return self.kind == resource.kind or self.resource == resource.id


@dataclass(frozen=True)
class FixtureTarget:
    id: str
    users: tuple[FixtureUser, ...]
    resources: tuple[FixtureResource, ...]
    flaws: tuple[Flaw, ...]


Response = tuple[int, dict[str, object]]


class FixtureApplication:
    """Pure request handling, testable without sockets."""

    def __init__(self, target: FixtureTarget, users_by_token: Mapping[str, FixtureUser]) -> None:
        self._target = target
        self._users_by_token = users_by_token

    def _flawed(self, flaw_type: str, resource: FixtureResource) -> bool:
        return any(f.type == flaw_type and f.applies_to(resource) for f in self._target.flaws)

    def _readable(self, user: FixtureUser, resource: FixtureResource) -> bool:
        if user.tenant in resource.shared_with:
            return True
        same_tenant = user.tenant == resource.tenant or self._flawed("tenant_bypass", resource)
        role_ok = user.role in resource.roles or self._flawed("role_bypass", resource)
        return same_tenant and role_ok

    def handle(self, path: str, authorization: str | None) -> Response:
        user = None
        if authorization and authorization.startswith("Bearer "):
            user = self._users_by_token.get(authorization.removeprefix("Bearer "))
        segments = path.split("/")[1:]
        if len(segments) == 3 and segments[0] == "api":
            return self._item(segments[1], segments[2], user)
        if user is None:
            return 401, {"error": "authentication required"}
        if path == "/session":
            return 200, {"subject": user.id}
        if len(segments) == 2 and segments[0] == "api":
            return self._listing(segments[1], user)
        return 404, {"error": "not found"}

    def _item(self, kind: str, resource_id: str, user: FixtureUser | None) -> Response:
        resource = next(
            (r for r in self._target.resources if r.id == resource_id and r.kind == kind), None
        )
        if user is None:
            if resource is not None and self._flawed("anonymous_read", resource):
                return 200, resource.body()
            return 401, {"error": "authentication required"}
        if resource is None:
            return 404, {"error": "not found"}
        if self._readable(user, resource):
            return 200, resource.body()
        if self._flawed("denial_leak", resource):
            return 403, {"error": "forbidden", "resource": resource.body()}
        return 403, {"error": "forbidden"}

    def _listing(self, kind: str, user: FixtureUser) -> Response:
        items = [
            r.body()
            for r in self._target.resources
            if r.kind == kind and (self._readable(user, r) or self._flawed("list_leak", r))
        ]
        return 200, {"items": items}


@dataclass(frozen=True)
class FixtureInstance:
    origin: str
    credentials: Mapping[str, Secret]


@contextmanager
def run_fixture(target: FixtureTarget) -> Iterator[FixtureInstance]:
    """Serve ``target`` on 127.0.0.1 with an ephemeral port and fresh random tokens."""
    tokens = {user.id: secrets.token_urlsafe(24) for user in target.users}
    application = FixtureApplication(target, {tokens[u.id]: u for u in target.users})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Silence request logging."""

        def do_GET(self) -> None:
            status, body = application.handle(self.path, self.headers.get("Authorization"))
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = QuietHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    try:
        yield FixtureInstance(
            origin=f"http://127.0.0.1:{server.server_port}",
            credentials={user_id: Secret(token) for user_id, token in tokens.items()},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
