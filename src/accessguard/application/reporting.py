"""Builds the versioned, machine-readable report (schema documented in docs/report-schema.md)."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from accessguard.application.agent import AgentTrace
from accessguard.application.agent_contract import PROMPT_VERSION, TOOL_CONTRACT_VERSION, Action
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

REPORT_SCHEMA_VERSION = "1.3"

NOT_TESTED = (
    "Write operations (POST, PUT, PATCH, DELETE): only GET requests are sent.",
    "Endpoints, resources and identities that are not declared in the policy.",
    "Pagination, filtering and query parameters on listings.",
    "Protected values that appear only as substrings or in transformed form "
    "(exact JSON string matching only).",
    "Response bodies that are not JSON (HTML, plain text, unknown Content-Type) or are invalid "
    "JSON: they are not analysed for protected data, so such responses are INCONCLUSIVE "
    "rather than PASS.",
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
    agent: AgentTrace | None = None
    full_matrix_checks: int | None = None
    """Size of the full check matrix when only a selection ran (a retest), else ``None``."""


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
                "zone": context.scope.zone.value,
                "path_prefixes": list(context.scope.path_prefixes),
                "constraints": context.scope.constraints(),
            },
            "selection": _selection(context, len(results)),
            "policy": {"id": policy.id, "sha256": context.policy_sha256},
            "planner": {
                "kind": context.planner.kind,
                "uses_ai": context.planner.uses_ai,
                "model": context.planner.model,
                "prompt_version": context.planner.prompt_version,
                "latency_ms": context.planner.latency_ms,
                "seed": context.planner.seed,
            },
            "agent": _agent(context.agent) if context.agent is not None else None,
            "limits": {
                "max_requests": context.limits.max_requests,
                "min_interval_seconds": context.limits.min_interval_seconds,
                "max_duration_seconds": context.limits.max_duration_seconds,
                "retest_findings": context.limits.retest_findings,
                "concurrency": 1,
            },
            "requests_used": outcome.requests_used,
            "interrupted": outcome.interrupted.value if outcome.interrupted else None,
            "completed": outcome.interrupted is None,
            "python": platform.python_version(),
        },
        "status": {
            "overall": status.value,
            "complete": outcome.interrupted is None,
            "meaning": OVERALL_STATUS_MEANING[status]
            + _interruption_note(outcome)
            + _selection_note(context, len(results)),
        },
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
        "results": [
            _result(result, outcome.requests_used_after.get(result.check.id)) for result in results
        ],
        "not_tested": [*NOT_TESTED, *invalid_sessions],
        "disclaimer": DISCLAIMER,
    }


def _selection(context: ReportContext, selected: int) -> dict[str, Any]:
    if context.full_matrix_checks is None:
        return {"mode": "all", "selected_checks": selected, "full_matrix_checks": selected}
    return {
        "mode": "subset",
        "selected_checks": selected,
        "full_matrix_checks": context.full_matrix_checks,
    }


def _selection_note(context: ReportContext, selected: int) -> str:
    if context.full_matrix_checks is None:
        return ""
    return (
        f" Only {selected} of {context.full_matrix_checks} policy checks were selected "
        "for this run (a retest); it says nothing about the checks that were not selected."
    )


def _interruption_note(outcome: RunOutcome) -> str:
    if outcome.interrupted is None:
        return ""
    unfinished = sum(1 for r in outcome.results if r.outcome is Outcome.SKIPPED)
    return (
        f" The run was interrupted ({outcome.interrupted.value}); {unfinished} planned checks "
        "did not complete and coverage is partial."
    )


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


def _agent(trace: AgentTrace) -> dict[str, Any]:
    descriptor = trace.descriptor
    limits = trace.limits
    return {
        "adapter": {
            "provider": descriptor.provider,
            "model": descriptor.model,
            "is_real_model": descriptor.is_real_model,
            "label": descriptor.label,
            "settings": dict(descriptor.settings),
        },
        "provider_metadata": dict(trace.provider_metadata),
        "provider_metadata_error": trace.provider_metadata_error,
        "prompt_version": PROMPT_VERSION,
        "tool_contract_version": TOOL_CONTRACT_VERSION,
        "allowed_actions": [action.value for action in Action],
        "verdicts_by": "deterministic engine; the model only selects approved checks",
        "limits": {
            "max_model_calls": limits.max_model_calls,
            "max_invalid_replies": limits.max_invalid_replies,
            "max_output_chars": limits.max_output_chars,
            "model_timeout_seconds": limits.model_timeout_seconds,
            "max_output_tokens": limits.max_output_tokens,
        },
        "status": trace.status.value,
        "failure": trace.failure,
        "model_calls": trace.model_calls,
        "invalid_replies": trace.invalid_replies,
        "model_latency_ms_total": trace.model_latency_ms,
        "steps": [
            {
                "call": step.call,
                "action": step.action.value,
                "check_id": step.check_id,
                "rejection": step.rejection,
                "latency_ms": step.latency_ms,
                "provider_metadata": dict(step.provider_metadata),
            }
            for step in trace.steps
        ],
    }


def _result(result: CheckResult, requests_used_after: int | None) -> dict[str, Any]:
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
        "requests_used_after": requests_used_after,
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
