"""Builds the versioned, machine-readable report (schema documented in docs/report-schema.md)."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from accessguard.application.gate import RunLimits
from accessguard.application.ports import PlannerInfo
from accessguard.application.runner import RunOutcome
from accessguard.domain.checks import Expectation, ItemCheck, ListCheck
from accessguard.domain.evaluation import Outcome, SessionState
from accessguard.domain.policy import ANONYMOUS, AccessPolicy
from accessguard.domain.results import (
    OVERALL_STATUS_MEANING,
    CheckResult,
    Evidence,
    compute_coverage,
    overall_status,
)
from accessguard.domain.scope import TargetScope

REPORT_SCHEMA_VERSION = "1.0"

NOT_TESTED = (
    "Write operations (POST, PUT, PATCH, DELETE): only GET requests are sent.",
    "Endpoints, resources and identities that are not declared in the policy.",
    "Pagination, filtering and query parameters on listings.",
    "Protected values that appear only as substrings or in transformed form "
    "(exact JSON string matching only).",
    "Field-level authorization beyond the declared protected fields.",
    "Side channels such as timing, error-message differences and identifier enumeration.",
    "Authentication strength and session management of the target itself.",
)

DISCLAIMER = (
    "Results cover only the declared policy, identities and checks against the named target. "
    "A passing check is not a guarantee of security."
)

_FINDING_OUTCOMES = (Outcome.FAIL, Outcome.SUSPECTED)


@dataclass(frozen=True)
class ReportContext:
    tool_version: str
    run_id: str
    target_label: str
    scope: TargetScope
    policy_sha256: str
    planner: PlannerInfo
    limits: RunLimits
    started_at: datetime
    finished_at: datetime


def build_report(
    policy: AccessPolicy, outcome: RunOutcome, context: ReportContext
) -> dict[str, Any]:
    results = outcome.results
    coverage = compute_coverage(results)
    status = overall_status(results)
    invalid_sessions = [
        f"Checks for identity {identity!r}: session could not be verified ({session.reason})."
        for identity, session in outcome.sessions.items()
        if session.state is SessionState.INVALID
    ]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool": {"name": "accessguard", "version": context.tool_version},
        "run": {
            "id": context.run_id,
            "started_at": context.started_at.isoformat(),
            "finished_at": context.finished_at.isoformat(),
            "target": context.target_label,
            "scope": {
                "origin": context.scope.origin,
                "constraints": ["loopback destinations only", "GET requests only"],
            },
            "policy": {"id": policy.id, "sha256": context.policy_sha256},
            "planner": {
                "kind": context.planner.kind,
                "uses_ai": context.planner.uses_ai,
                "model": context.planner.model,
                "prompt_version": context.planner.prompt_version,
                "latency_ms": context.planner.latency_ms,
            },
            "limits": {
                "max_requests": context.limits.max_requests,
                "min_interval_seconds": context.limits.min_interval_seconds,
                "max_duration_seconds": context.limits.max_duration_seconds,
                "retest_findings": context.limits.retest_findings,
                "concurrency": 1,
            },
            "requests_used": outcome.requests_used,
            "interrupted": outcome.interrupted.value if outcome.interrupted else None,
            "python": platform.python_version(),
        },
        "status": {"overall": status.value, "meaning": OVERALL_STATUS_MEANING[status]},
        "summary": coverage.by_outcome,
        "coverage": {
            "planned_checks": coverage.planned,
            "executed_checks": coverage.executed,
            "conclusive_checks": coverage.conclusive,
            "conclusive_ratio": round(coverage.conclusive_ratio, 4),
            "definition": "conclusive_ratio = (PASS + FAIL) / planned checks",
            "by_kind": coverage.by_kind,
            "by_relation": coverage.by_relation,
        },
        "sessions": {
            identity: {"state": session.state.value, "reason": session.reason}
            for identity, session in outcome.sessions.items()
        },
        "findings": [
            _finding(result, policy, context)
            for result in results
            if result.outcome in _FINDING_OUTCOMES
        ],
        "results": [_result(result) for result in results],
        "not_tested": [*NOT_TESTED, *invalid_sessions],
        "disclaimer": DISCLAIMER,
    }


def _evidence(evidence: Evidence | None) -> dict[str, Any] | None:
    if evidence is None:
        return None
    return {
        "request": f"{evidence.method} {evidence.path}",
        "status": evidence.status,
        "content_type": evidence.content_type,
        "size_bytes": evidence.size,
        "body_sha256": evidence.body_sha256,
        "matched_protected_fields": [
            {"resource": m.resource, "fields": list(m.matched_fields), "complete": m.complete}
            for m in evidence.matches
        ],
    }


def _result(result: CheckResult) -> dict[str, Any]:
    check = result.check
    target: dict[str, Any]
    if isinstance(check, ItemCheck):
        target = {"resource": check.resource, "relation": check.relation.value}
    else:
        target = {
            "collection": check.collection,
            "visible": list(check.visible),
            "hidden": list(check.hidden),
        }
    return {
        "check_id": check.id,
        "kind": check.kind,
        "actor": check.actor,
        **target,
        "expectation": check.expectation.value,
        "outcome": result.outcome.value,
        "reason": result.reason,
        "executed": result.executed,
        "observed_at": result.observed_at.isoformat() if result.observed_at else None,
        "finding_type": result.finding_type.value if result.finding_type else None,
        "verification": result.verification.value if result.verification else None,
        "evidence": _evidence(result.evidence),
    }


def _actor(policy: AccessPolicy, actor: str) -> dict[str, Any]:
    if actor == ANONYMOUS:
        return {"id": ANONYMOUS, "tenant": None, "role": None}
    identity = policy.identity(actor)
    return {"id": identity.id, "tenant": identity.tenant, "role": identity.role}


def _finding(result: CheckResult, policy: AccessPolicy, context: ReportContext) -> dict[str, Any]:
    check = result.check
    subject: dict[str, Any]
    if isinstance(check, ItemCheck):
        resource = policy.resource(check.resource)
        rule = policy.describe_rule(resource)
        path = resource.path
        subject = {
            "type": "resource",
            "id": resource.id,
            "category": resource.category,
            "owner_tenant": policy.owner_tenant(resource),
        }
        expected = (
            "The actor can read the resource and receives its protected content."
            if check.expectation is Expectation.ALLOW
            else "The request is denied (HTTP 401, 403 or 404) without protected content."
        )
    else:
        rule, path, subject, expected = _list_details(policy, check, result)
    actor = _actor(policy, check.actor)
    authentication = (
        "Send the request without any credential."
        if check.actor == ANONYMOUS
        else f"Authenticate as test identity {actor['id']!r} "
        f"(tenant {actor['tenant']}, role {actor['role']})."
    )
    return {
        "id": check.id,
        "outcome": result.outcome.value,
        "type": result.finding_type.value if result.finding_type else None,
        "rule": rule,
        "actor": actor,
        "subject": subject,
        "expected": expected,
        "actual": result.reason,
        "evidence": _evidence(result.evidence),
        "reproduction": [
            f"Use policy {policy.id!r} (sha256 {context.policy_sha256}).",
            authentication,
            f"Send GET {path} to {context.scope.origin}.",
            f"Expected: {expected}",
            f"Observed: {result.reason}.",
        ],
        "scope": context.scope.origin,
        "observed_at": result.observed_at.isoformat() if result.observed_at else None,
        "versions": {
            "tool": context.tool_version,
            "policy_sha256": context.policy_sha256,
            "target": context.target_label,
        },
        "verification": result.verification.value if result.verification else None,
    }


def _list_details(
    policy: AccessPolicy, check: ListCheck, result: CheckResult
) -> tuple[str, str, dict[str, Any], str]:
    collection = policy.collection(check.collection)
    leaked_ids = [m.resource for m in result.evidence.matches] if result.evidence else []
    leaked = [policy.resource(rid) for rid in leaked_ids]
    rule = (
        f"Listing {collection.path} may only include resources whose grants match the actor; "
        "every resource in it follows its own declared rule."
    )
    subject = {
        "type": "collection",
        "id": collection.id,
        "category": "collection",
        "leaked_resources": [
            {"id": r.id, "category": r.category, "owner_tenant": policy.owner_tenant(r)}
            for r in leaked
        ],
    }
    expected = (
        f"The listing includes {list(check.visible)} and excludes {list(check.hidden)}."
        if check.visible
        else "The listing is denied or contains none of the hidden resources."
    )
    return rule, collection.path, subject, expected
