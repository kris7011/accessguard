"""Access checks derived deterministically from the declared policy.

Every identity (and the anonymous actor) is checked against every resource and
every collection, so coverage is a property of the policy, not of a planner.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from accessguard.domain.errors import PlanValidationError
from accessguard.domain.policy import ANONYMOUS, AccessPolicy, Resource


class Expectation(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Relation(StrEnum):
    """How the actor relates to a resource; used for coverage reporting."""

    OWNER = "owner"
    SAME_TENANT = "same_tenant"
    SHARED_GRANT = "shared_grant"
    CROSS_TENANT = "cross_tenant"
    ANONYMOUS = "anonymous"


@dataclass(frozen=True)
class ItemCheck:
    """Read one resource as ``actor`` and compare the outcome with the policy."""

    kind: ClassVar[str] = "item"

    id: str
    actor: str
    resource: str
    expectation: Expectation
    relation: Relation


@dataclass(frozen=True)
class ListCheck:
    """List a collection as ``actor``: ``visible`` must appear and ``hidden`` must not."""

    kind: ClassVar[str] = "list"

    id: str
    actor: str
    collection: str
    visible: tuple[str, ...]
    hidden: tuple[str, ...]

    @property
    def expectation(self) -> Expectation:
        return Expectation.ALLOW if self.visible else Expectation.DENY


Check = ItemCheck | ListCheck


def classify_relation(policy: AccessPolicy, actor: str, resource: Resource) -> Relation:
    if actor == ANONYMOUS:
        return Relation.ANONYMOUS
    if actor == resource.owner:
        return Relation.OWNER
    if policy.identity(actor).tenant == policy.owner_tenant(resource):
        return Relation.SAME_TENANT
    if policy.is_allowed(actor, resource):
        return Relation.SHARED_GRANT
    return Relation.CROSS_TENANT


def generate_checks(policy: AccessPolicy) -> tuple[Check, ...]:
    """Return the full, deterministically ordered check matrix for ``policy``."""
    actors = [identity.id for identity in policy.identities] + [ANONYMOUS]
    checks: list[Check] = []
    for resource in policy.resources:
        for actor in actors:
            allowed = policy.is_allowed(actor, resource)
            checks.append(
                ItemCheck(
                    id=f"{resource.id}@{actor}",
                    actor=actor,
                    resource=resource.id,
                    expectation=Expectation.ALLOW if allowed else Expectation.DENY,
                    relation=classify_relation(policy, actor, resource),
                )
            )
    for collection in policy.collections:
        members = policy.resources_in(collection.id)
        for actor in actors:
            checks.append(
                ListCheck(
                    id=f"list:{collection.id}@{actor}",
                    actor=actor,
                    collection=collection.id,
                    visible=tuple(r.id for r in members if policy.is_allowed(actor, r)),
                    hidden=tuple(r.id for r in members if not policy.is_allowed(actor, r)),
                )
            )
    return tuple(checks)


def order_checks(checks: Sequence[Check], proposed_order: object) -> tuple[Check, ...]:
    """Apply an untrusted proposed order. It must name every approved check exactly once.

    A planner may only change the order; it can never add, drop or alter a check.
    """
    if not isinstance(proposed_order, list) or not all(
        isinstance(item, str) for item in proposed_order
    ):
        raise PlanValidationError("A plan must be a list of check ids")
    by_id = {check.id: check for check in checks}
    if len(proposed_order) != len(by_id) or set(proposed_order) != set(by_id):
        raise PlanValidationError("A plan must contain every approved check id exactly once")
    return tuple(by_id[check_id] for check_id in proposed_order)
