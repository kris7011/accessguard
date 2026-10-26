"""The deterministic baseline planner (no AI)."""

from __future__ import annotations

from collections.abc import Sequence

from accessguard.application.ports import Plan, PlannerInfo
from accessguard.domain.checks import Check


class DeterministicPlanner:
    """Keeps the policy-derived order. Reports produced with it are never labelled AI."""

    def plan(self, checks: Sequence[Check]) -> Plan:
        return Plan(tuple(checks), PlannerInfo(kind="deterministic", uses_ai=False))
