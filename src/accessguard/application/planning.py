"""Planners without AI: the deterministic baseline and a seeded random order for evaluation."""

from __future__ import annotations

import random
from collections.abc import Sequence

from accessguard.application.ports import Plan, PlannerInfo
from accessguard.domain.checks import Check


class DeterministicPlanner:
    """Keeps the policy-derived order. Reports produced with it are never labelled AI."""

    def plan(self, checks: Sequence[Check]) -> Plan:
        return Plan(tuple(checks), PlannerInfo(kind="deterministic", uses_ai=False))


class SeededRandomPlanner:
    """Shuffles the approved checks with a fixed seed: a reproducible comparison baseline.

    The same seed and the same check list always give the same order, on every platform
    (``random.Random`` is a documented, seeded Mersenne Twister).
    """

    def __init__(self, seed: int) -> None:
        self._seed = seed

    def plan(self, checks: Sequence[Check]) -> Plan:
        ordered = list(checks)
        random.Random(self._seed).shuffle(ordered)  # noqa: S311 - ordering, not security
        return Plan(
            tuple(ordered), PlannerInfo(kind="seeded-random", uses_ai=False, seed=self._seed)
        )
