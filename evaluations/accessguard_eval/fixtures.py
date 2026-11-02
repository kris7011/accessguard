"""Loads evaluation fixtures. The answer key has its own loader, used only for scoring.

A run only receives :class:`EvaluationFixture` (the declared policy and the served
target). The answer key is read after the run by :func:`load_answer_key`, so no code path
can hand it to a planner or an agent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

from accessguard.adapters.policy_json import LoadedPolicy, load_policy_bytes
from accessguard_eval.fixture_server import (
    FLAW_TYPES,
    FixtureResource,
    FixtureTarget,
    FixtureUser,
    Flaw,
)

_ROOT = files("accessguard_eval").joinpath("fixtures")


class FixtureError(ValueError):
    """A fixture file is malformed."""


@dataclass(frozen=True)
class EvaluationFixture:
    id: str
    policy: LoadedPolicy
    target: FixtureTarget
    sha256: dict[str, str]
    """SHA-256 of policy.json and target.json, recorded with every result."""


def fixture_ids() -> tuple[str, ...]:
    manifest = json.loads(_ROOT.joinpath("manifest.json").read_text("utf-8"))
    return tuple(entry["id"] for entry in manifest["fixtures"])


def _file(fixture_id: str, name: str) -> Traversable:
    if fixture_id not in fixture_ids():
        raise FixtureError(f"Unknown fixture {fixture_id!r}")
    return _ROOT.joinpath(fixture_id, name)


def _keys(document: dict[str, Any], required: set[str], where: str) -> None:
    if set(document) != required:
        raise FixtureError(f"{where}: expected keys {sorted(required)}, got {sorted(document)}")


def _target(document: dict[str, Any]) -> FixtureTarget:
    _keys(document, {"id", "users", "resources", "flaws"}, "target")
    users = []
    for user in document["users"]:
        _keys(user, {"id", "tenant", "role"}, "target user")
        users.append(FixtureUser(**user))
    resources = []
    for resource in document["resources"]:
        _keys(resource, {"id", "kind", "tenant", "roles", "shared_with", "data"}, "resource")
        resources.append(
            FixtureResource(
                id=resource["id"],
                kind=resource["kind"],
                tenant=resource["tenant"],
                roles=frozenset(resource["roles"]),
                shared_with=frozenset(resource["shared_with"]),
                data=dict(resource["data"]),
            )
        )
    flaws = []
    for flaw in document["flaws"]:
        if flaw.get("type") not in FLAW_TYPES or set(flaw) not in (
            {"type", "kind"},
            {"type", "resource"},
        ):
            raise FixtureError(f"Invalid flaw {flaw!r}")
        flaws.append(Flaw(**flaw))
    return FixtureTarget(document["id"], tuple(users), tuple(resources), tuple(flaws))


def load_fixture(fixture_id: str) -> EvaluationFixture:
    policy_bytes = _file(fixture_id, "policy.json").read_bytes()
    target_bytes = _file(fixture_id, "target.json").read_bytes()
    target = _target(json.loads(target_bytes))
    if target.id != fixture_id:
        raise FixtureError(f"target.json id {target.id!r} does not match {fixture_id!r}")
    return EvaluationFixture(
        id=fixture_id,
        policy=load_policy_bytes(policy_bytes),
        target=target,
        sha256={
            "policy.json": hashlib.sha256(policy_bytes).hexdigest(),
            "target.json": hashlib.sha256(target_bytes).hexdigest(),
        },
    )


def load_answer_key(fixture_id: str) -> frozenset[str]:
    """Hand-written expected findings (check ids). For scoring only."""
    document = json.loads(_file(fixture_id, "answer_key.json").read_text("utf-8"))
    _keys(document, {"fixture", "expected_findings", "notes"}, "answer key")
    if document["fixture"] != fixture_id:
        raise FixtureError(f"answer key belongs to {document['fixture']!r}")
    return frozenset(document["expected_findings"])
