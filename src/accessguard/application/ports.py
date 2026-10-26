"""Narrow interfaces the use cases depend on. Adapters implement them."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from accessguard.domain.checks import Check
from accessguard.domain.credentials import Secret
from accessguard.domain.observation import HttpObservation


class TargetClient(Protocol):
    """Sends a read-only request to the authorized target.

    Implementations must enforce the target scope and raise ``ScopeViolationError`` or
    ``TransportError`` instead of returning partial data.
    """

    def get(self, path: str, credential: Secret | None) -> HttpObservation: ...


@dataclass(frozen=True)
class PlannerInfo:
    """Provenance of the check order, recorded in every report."""

    kind: str
    uses_ai: bool
    model: str | None = None
    prompt_version: str | None = None
    latency_ms: float | None = None


@dataclass(frozen=True)
class Plan:
    checks: tuple[Check, ...]
    info: PlannerInfo


class Planner(Protocol):
    """Orders the approved checks. A planner can never add, drop or alter checks."""

    def plan(self, checks: Sequence[Check]) -> Plan: ...
