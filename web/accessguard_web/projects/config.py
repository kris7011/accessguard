"""The project configuration document and its validation.

The draft is stored in exactly the scan spec format the runner executes (without a check
selection), so there is one schema and one validator: the engine's own
:func:`accessguard.adapters.scan_spec.parse_scan_spec`. Forms edit parts of the document;
nothing here duplicates the engine's rules.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from accessguard.adapters.scan_spec import ScanSpec, parse_scan_spec
from accessguard.application.gate import MAX_REQUEST_BUDGET
from accessguard.domain.checks import generate_checks
from accessguard.domain.errors import PolicyError
from accessguard.domain.policy import ANONYMOUS

DEFAULT_LIMITS = {"max_requests": 500, "min_interval_seconds": 0.0, "max_duration_seconds": 600}


def policy_id_for(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    return slug or "project"


def empty_config(name: str) -> dict[str, Any]:
    return {
        "spec_version": "1",
        "label": name,
        "target": {"origin": "", "path_prefixes": ["/"], "auth": {"kind": "bearer"}},
        "limits": dict(DEFAULT_LIMITS),
        "checks": None,
        "policy": {
            "schema_version": "1",
            "id": policy_id_for(name),
            "description": "",
            "session_probe": {"path": "", "subject_field": ""},
            "identities": [],
            "collections": [],
            "resources": [],
        },
    }


def section(config: dict[str, Any], key: str) -> Any:
    """Return a deep copy of one part of the document, so callers never mutate the stored one."""
    return copy.deepcopy(config.get(key))


@dataclass(frozen=True)
class SetupStep:
    key: str
    title: str
    done: bool
    url_name: str


@dataclass(frozen=True)
class ConfigStatus:
    spec: ScanSpec | None
    error: str | None
    steps: tuple[SetupStep, ...]
    planned_checks: int = 0
    recommended_requests: int = 0
    warnings: tuple[str, ...] = field(default=())

    @property
    def ready(self) -> bool:
        return self.spec is not None


def recommended_budget(identities: int, resources: int, checks: int) -> int:
    """Requests that guarantee complete coverage, retests of every check included.

    One session probe per identity, one owner baseline per resource, one request per check
    and, at most, one retest per check. A run with this budget cannot end early for lack of
    requests, which keeps reports complete rather than prioritised (docs/evaluation.md found
    no reliable gain from prioritising within a smaller budget).
    """
    return min(identities + resources + 2 * checks, MAX_REQUEST_BUDGET)


def evaluate(config: dict[str, Any]) -> ConfigStatus:
    policy = config.get("policy") or {}
    target = config.get("target") or {}
    steps = (
        SetupStep("target", "Target and authentication", bool(target.get("origin")),
                  "projects:setup_target"),
        SetupStep("users", "Test users", bool(policy.get("identities")), "projects:setup_users"),
        SetupStep("rules", "Endpoints and access rules", bool(policy.get("resources")),
                  "projects:setup_rules"),
    )  # fmt: skip
    try:
        spec = parse_scan_spec({**config, "checks": None})
    except PolicyError as error:
        return ConfigStatus(None, str(error), steps)
    engine_policy = spec.policy.policy
    checks = len(generate_checks(engine_policy))
    recommended = recommended_budget(
        len(engine_policy.identities), len(engine_policy.resources), checks
    )
    warnings: list[str] = []
    if spec.limits.max_requests < recommended:
        warnings.append(
            f"The request budget ({spec.limits.max_requests}) is below the {recommended} "
            "requests that guarantee complete coverage. A scan may stop early; its report "
            "will then be marked incomplete, never passed."
        )
    tenants = {identity.tenant for identity in engine_policy.identities}
    if len(tenants) < 2:
        warnings.append(
            "All test users belong to one tenant, so cross-tenant access is not tested. "
            "Add a user of a second tenant."
        )
    return ConfigStatus(spec, None, steps, checks, recommended, tuple(warnings))


def spec_document(config: dict[str, Any], checks: list[str] | None = None) -> dict[str, Any]:
    """The exact document a runner receives: the draft plus an optional check selection."""
    document = copy.deepcopy(config)
    document["checks"] = checks
    return document


def identity_choices(config: dict[str, Any]) -> list[tuple[str, str]]:
    identities = (config.get("policy") or {}).get("identities") or []
    return [
        (item["id"], f"{item['id']} ({item['tenant']} / {item['role']})")
        for item in identities
        if isinstance(item, dict) and item.get("id") != ANONYMOUS
    ]


def access_choices(config: dict[str, Any]) -> list[tuple[str, str]]:
    """Every (tenant, role) pair that occurs among the test users, as ``tenant:role``."""
    identities = (config.get("policy") or {}).get("identities") or []
    pairs = sorted({(i["tenant"], i["role"]) for i in identities if isinstance(i, dict)})
    return [(f"{tenant}:{role}", f"{tenant} / {role}") for tenant, role in pairs]


def grants_from_pairs(pairs: list[str]) -> list[dict[str, Any]]:
    by_tenant: dict[str, list[str]] = {}
    for pair in pairs:
        tenant, _, role = pair.partition(":")
        by_tenant.setdefault(tenant, []).append(role)
    return [{"tenant": t, "roles": sorted(roles)} for t, roles in sorted(by_tenant.items())]


def pairs_from_grants(grants: list[dict[str, Any]]) -> list[str]:
    return [f"{g['tenant']}:{role}" for g in grants for role in g.get("roles", [])]
