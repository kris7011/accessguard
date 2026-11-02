"""Versioned check features and a learned ranker (a linear model over those features).

The features are computed from the engine's own enumerations and counts only: never ids,
names, paths or field names, so they carry no customer identifiers. The same function is
used for training (``ml/``) and inference, so a model can never see features computed
differently from the ones it was trained on.

A ranker only reorders the approved checks (like every planner). It is used only when a
model has been promoted after an evaluation against simple baselines; see
docs/adr/0013-ml-ranking-foundation.md. The product's default remains complete coverage in
policy order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal

from accessguard.application.ports import Plan, PlannerInfo
from accessguard.domain.checks import Check, Expectation, ItemCheck, ListCheck, Relation
from accessguard.domain.errors import PlannerError
from accessguard.domain.policy import AccessPolicy

FEATURE_VERSION = "features-v1"
FEATURE_NAMES: tuple[str, ...] = (
    "bias",
    "kind_list",
    "expect_deny",
    "rel_same_tenant",
    "rel_shared_grant",
    "rel_cross_tenant",
    "rel_anonymous",
    "resource_shared_across_tenants",
    "resource_in_collection",
    "log_protected_fields",
    "log_grants",
    "path_depth",
    "list_log_hidden",
    "list_log_visible",
)


_LN_CONTEXT = Context(prec=40)


def _log1p(value: int) -> float:
    # Correctly rounded on every platform. ``math.log1p`` comes from the C library, whose
    # last bit differs between Windows and glibc, and dataset versions hash these values.
    return float(Decimal(max(value, 0) + 1).ln(_LN_CONTEXT))


def check_features(policy: AccessPolicy, check: Check) -> dict[str, float]:
    """Return the ``features-v1`` vector of one check as a name -> value mapping."""
    values = dict.fromkeys(FEATURE_NAMES, 0.0)
    values["bias"] = 1.0
    values["expect_deny"] = float(check.expectation is Expectation.DENY)
    if isinstance(check, ItemCheck):
        resource = policy.resource(check.resource)
        relation_feature = {
            Relation.SAME_TENANT: "rel_same_tenant",
            Relation.SHARED_GRANT: "rel_shared_grant",
            Relation.CROSS_TENANT: "rel_cross_tenant",
            Relation.ANONYMOUS: "rel_anonymous",
        }.get(check.relation)
        if relation_feature is not None:
            values[relation_feature] = 1.0
        values["resource_shared_across_tenants"] = float(
            len({g.tenant for g in resource.grants}) > 1
        )
        values["resource_in_collection"] = float(resource.collection is not None)
        values["log_protected_fields"] = _log1p(len(resource.protected_fields))
        values["log_grants"] = _log1p(sum(len(g.roles) for g in resource.grants))
        values["path_depth"] = float(len([s for s in resource.path.split("/") if s]))
    else:
        assert isinstance(check, ListCheck)  # noqa: S101 - Check is a closed union
        values["kind_list"] = 1.0
        values["rel_anonymous"] = float(check.actor == "anonymous")
        values["path_depth"] = float(
            len([s for s in policy.collection(check.collection).path.split("/") if s])
        )
        values["list_log_hidden"] = _log1p(len(check.hidden))
        values["list_log_visible"] = _log1p(len(check.visible))
    return values


@dataclass(frozen=True)
class LinearRanker:
    """``score = sigmoid(w · x)`` over ``features-v1``. Loaded from a registry artifact."""

    version: str
    weights: Mapping[str, float]
    feature_version: str = FEATURE_VERSION

    def __post_init__(self) -> None:
        if self.feature_version != FEATURE_VERSION:
            raise PlannerError(
                f"Model {self.version} expects {self.feature_version}, engine has {FEATURE_VERSION}"
            )
        unknown = set(self.weights) - set(FEATURE_NAMES)
        if unknown:
            raise PlannerError(f"Model {self.version} has unknown features: {sorted(unknown)}")

    def score(self, features: Mapping[str, float]) -> float:
        z = sum(self.weights.get(name, 0.0) * value for name, value in features.items())
        return 1.0 / (1.0 + math.exp(-max(min(z, 50.0), -50.0)))

    @classmethod
    def from_artifact(cls, artifact: Mapping[str, object]) -> LinearRanker:
        weights = artifact.get("weights")
        version = artifact.get("version")
        feature_version = artifact.get("feature_version")
        if not isinstance(weights, dict) or not isinstance(version, str):
            raise PlannerError("A ranker artifact needs a version and a weights object")
        if not all(isinstance(v, int | float) for v in weights.values()):
            raise PlannerError("Ranker weights must be numbers")
        return cls(version, {str(k): float(v) for k, v in weights.items()}, str(feature_version))


class LearnedRankerPlanner:
    """Orders checks by a promoted model's score (highest first; ties keep policy order)."""

    def __init__(self, policy: AccessPolicy, ranker: LinearRanker) -> None:
        self._policy = policy
        self._ranker = ranker

    def plan(self, checks: Sequence[Check]) -> Plan:
        scored = [
            (-self._ranker.score(check_features(self._policy, check)), index, check)
            for index, check in enumerate(checks)
        ]
        ordered = tuple(check for _, _, check in sorted(scored, key=lambda item: item[:2]))
        return Plan(
            ordered,
            PlannerInfo(
                kind="learned-ranker", uses_ai=True, model=f"ranker:{self._ranker.version}"
            ),
        )
