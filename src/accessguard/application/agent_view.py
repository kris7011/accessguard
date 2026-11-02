"""The data-minimisation boundary between the engine and a model: what the agent may see.

Policy strings (identity, tenant, role, resource, category and collection ids, paths,
field names, descriptions) are customer input and therefore untrusted: an identifier
such as ``ignore-previous-instructions`` passes policy validation. The model never sees
any of them. Every entity is replaced by an opaque alias generated here (``c001``,
``a1``, ``t2``, ``r3`` ...), so the structure that matters for choosing the next check
(which checks share a resource, an actor, a tenant, a relation) is preserved without any
free text.

Results are reduced to engine-defined enumerations and integers: outcome, finding type
and HTTP status. Verdict reasons, response bodies, protected values, field names,
credentials and target error texts are never included.

Note that the prompt is not the security boundary; the tool contract
(:mod:`accessguard.application.agent_contract`) enforces what the model can do.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from accessguard.application.runner import SessionStatus
from accessguard.domain.checks import Check, ItemCheck
from accessguard.domain.policy import ANONYMOUS, AccessPolicy
from accessguard.domain.results import CheckResult

ANONYMOUS_ALIAS = "a0"


@dataclass(frozen=True)
class Budget:
    """Numbers the model may use to pace itself. Enforcement happens elsewhere."""

    http_requests_used: int
    http_requests_max: int
    model_calls_used: int
    model_calls_max: int
    invalid_replies: int
    invalid_replies_max: int


def _aliases(values: Sequence[str], prefix: str) -> dict[str, str]:
    """Assign ``prefix1``, ``prefix2`` ... in first-appearance order (deterministic)."""
    unique = list(dict.fromkeys(values))
    return {value: f"{prefix}{index}" for index, value in enumerate(unique, 1)}


class ModelView:
    """Maps one run's approved checks to aliases and renders the model-visible state."""

    def __init__(self, policy: AccessPolicy, checks: Sequence[Check]) -> None:
        width = max(3, len(str(len(checks))))
        self._checks = tuple(checks)
        self._alias_of = {check.id: f"c{index:0{width}d}" for index, check in enumerate(checks, 1)}
        self._check_of = {self._alias_of[check.id]: check for check in checks}
        identities = policy.identities
        self._actors = {
            ANONYMOUS: ANONYMOUS_ALIAS,
            **_aliases([i.id for i in identities], "a"),
        }
        self._tenants = _aliases([i.tenant for i in identities], "t")
        self._roles = _aliases([i.role for i in identities], "p")
        self._resources = _aliases([r.id for r in policy.resources], "r")
        self._categories = _aliases([r.category for r in policy.resources], "k")
        self._collections = _aliases([c.id for c in policy.collections], "l")
        self._actor_rows: list[tuple[str | None, dict[str, Any]]] = [
            (None, {"id": ANONYMOUS_ALIAS, "anonymous": True}),
            *(
                (
                    i.id,
                    {
                        "id": self._actors[i.id],
                        "tenant": self._tenants[i.tenant],
                        "role": self._roles[i.role],
                    },
                )
                for i in identities
            ),
        ]
        self._static = [self._describe(policy, check) for check in checks]

    def alias(self, check: Check) -> str:
        return self._alias_of[check.id]

    def check(self, alias: str) -> Check:
        return self._check_of[alias]

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(self._alias_of.values())

    def _describe(self, policy: AccessPolicy, check: Check) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self._alias_of[check.id],
            "kind": check.kind,
            "actor": self._actors[check.actor],
            "expectation": check.expectation.value,
        }
        if isinstance(check, ItemCheck):
            resource = policy.resource(check.resource)
            row.update(
                resource=self._resources[resource.id],
                category=self._categories[resource.category],
                owner_tenant=self._tenants[policy.owner_tenant(resource)],
                relation=check.relation.value,
            )
        else:
            row.update(
                collection=self._collections[check.collection],
                visible_count=len(check.visible),
                hidden_count=len(check.hidden),
            )
        return row

    def state(
        self,
        *,
        results: Sequence[CheckResult],
        sessions: Mapping[str, SessionStatus],
        budget: Budget,
        last_rejection: str | None,
    ) -> dict[str, Any]:
        """Return the JSON-compatible state for the next selection.

        ``results`` are the executed checks in execution order.
        """
        done = {result.check.id: (step, result) for step, result in enumerate(results, 1)}
        rows = []
        for check, static in zip(self._checks, self._static, strict=True):
            row = dict(static)
            if check.id in done:
                step, result = done[check.id]
                row.update(status="done", step=step, **_result_fields(result))
            else:
                row["status"] = "pending"
            rows.append(row)
        actors = []
        for identity, actor in self._actor_rows:
            row = dict(actor)
            if identity is not None:
                status = sessions.get(identity)
                row["session"] = status.state.value if status is not None else "unknown"
            actors.append(row)
        return {
            "task": "select_next_check",
            "budget": {
                "http_requests_used": budget.http_requests_used,
                "http_requests_max": budget.http_requests_max,
                "model_calls_used": budget.model_calls_used,
                "model_calls_max": budget.model_calls_max,
                "invalid_replies": budget.invalid_replies,
                "invalid_replies_max": budget.invalid_replies_max,
            },
            "last_reply_rejected": last_rejection,
            "actors": actors,
            "checks": rows,
        }


def _result_fields(result: CheckResult) -> dict[str, Any]:
    """Only engine enumerations and the integer status code; never reasons or content."""
    status = result.evidence.status if result.evidence is not None else None
    return {
        "outcome": result.outcome.value,
        "finding": result.finding_type.value if result.finding_type else None,
        "http_status": status if isinstance(status, int) else None,
        "executed": result.executed,
    }
