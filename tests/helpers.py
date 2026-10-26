"""Shared test builders. Kept small and explicit so each test shows its own inputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

from accessguard.domain.credentials import Secret
from accessguard.domain.observation import HttpObservation
from accessguard.domain.policy import (
    AccessPolicy,
    Collection,
    Grant,
    Identity,
    Resource,
    SessionProbe,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LAB_POLICY_PATH = REPO_ROOT / "lab" / "accessguard_lab" / "policies" / "lab.json"


def observation(
    status: int = 200, body: object = None, content_type: str = "application/json"
) -> HttpObservation:
    raw = b"" if body is None else json.dumps(body).encode()
    return HttpObservation(
        status=status,
        content_type=content_type,
        size=len(raw),
        body_sha256=hashlib.sha256(raw).hexdigest(),
        is_json=content_type == "application/json" and body is not None,
        json_body=body,
    )


def two_tenant_policy(*, with_collection: bool = False) -> AccessPolicy:
    """acme (admin, member) and globex (member); acme-doc, globex-doc and a share to globex."""
    own = {
        "acme": (Grant("acme", frozenset({"admin", "member"})),),
        "globex": (Grant("globex", frozenset({"member"})),),
    }
    collection = "docs" if with_collection else None
    return AccessPolicy(
        id="test-policy",
        identities=(
            Identity("acme-admin", "acme", "admin"),
            Identity("acme-member", "acme", "member"),
            Identity("globex-member", "globex", "member"),
        ),
        resources=(
            Resource(
                "acme-doc",
                "document",
                "/d/acme",
                "acme-member",
                ("title", "canary"),
                own["acme"],
                collection,
            ),
            Resource(
                "globex-doc",
                "document",
                "/d/globex",
                "globex-member",
                ("title", "canary"),
                own["globex"],
                collection,
            ),
            Resource(
                "shared",
                "document",
                "/d/shared",
                "acme-admin",
                ("title", "canary"),
                (*own["acme"], Grant("globex", frozenset({"member"}))),
                collection,
            ),
        ),
        collections=(Collection("docs", "/d"),) if with_collection else (),
        session_probe=SessionProbe("/me", "user"),
    )


DOC_BODIES = {
    "acme-doc": {"id": "acme-doc", "title": "Acme title", "canary": "CANARY-ACME-0001"},
    "globex-doc": {"id": "globex-doc", "title": "Globex title", "canary": "CANARY-GLOBEX-0001"},
    "shared": {"id": "shared", "title": "Shared brief", "canary": "CANARY-SHARED-0001"},
}

TOKENS = {
    "acme-admin": "token-acme-admin-0123456789",
    "acme-member": "token-acme-member-0123456789",
    "globex-member": "token-globex-member-0123456789",
}


def credentials(ids: Iterable[str] = TOKENS) -> dict[str, Secret]:
    return {identity: Secret(TOKENS[identity]) for identity in ids}


Response = HttpObservation | Exception


class ScriptedClient:
    """Fake TargetClient. ``routes`` maps (path, token or None) to a response or a list of
    responses consumed in order (the last one repeats). Unknown routes return 404."""

    def __init__(self, routes: Mapping[tuple[str, str | None], Response | list[Response]]) -> None:
        self._routes = {
            key: value if isinstance(value, list) else [value] for key, value in routes.items()
        }
        self.calls: list[tuple[str, str | None]] = []

    def get(self, path: str, credential: Secret | None) -> HttpObservation:
        key = (path, credential.reveal() if credential else None)
        self.calls.append(key)
        queue = self._routes.get(key)
        if queue is None:
            return observation(404, {"error": "not found"})
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        return response


def correct_routes() -> dict[tuple[str, str | None], Response | list[Response]]:
    """Routes for :func:`two_tenant_policy` implementing the policy correctly."""
    routes: dict[tuple[str, str | None], Response | list[Response]] = {}
    tenants = {"acme-admin": "acme", "acme-member": "acme", "globex-member": "globex"}
    for identity, token in TOKENS.items():
        routes[("/me", token)] = observation(200, {"user": identity})
        tenant = tenants[identity]
        for doc, path in (
            ("acme-doc", "/d/acme"),
            ("globex-doc", "/d/globex"),
            ("shared", "/d/shared"),
        ):
            allowed = doc.startswith(tenant) or doc == "shared"
            routes[(path, token)] = (
                observation(200, DOC_BODIES[doc]) if allowed else observation(403, {})
            )
        visible = [b for d, b in DOC_BODIES.items() if d.startswith(tenant) or d == "shared"]
        routes[("/d", token)] = observation(200, {"items": visible})
    for path in ("/d/acme", "/d/globex", "/d/shared", "/d", "/me"):
        routes[(path, None)] = observation(401, {"error": "unauthorized"})
    return routes
