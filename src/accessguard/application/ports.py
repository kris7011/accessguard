"""Narrow interfaces the use cases depend on. Adapters implement them."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from accessguard.domain.checks import Check
from accessguard.domain.credentials import Secret
from accessguard.domain.observation import HttpObservation


@dataclass(frozen=True)
class RequestLimit:
    """Bounds one in-flight request: a wall-clock limit and an external cancel signal."""

    seconds: float
    cancel: threading.Event = field(default_factory=threading.Event)


class TargetClient(Protocol):
    """Sends a read-only request to the authorized target.

    Implementations must enforce the target scope, abort the request when ``limit``
    expires or is cancelled, and raise ``ScopeViolationError`` or ``TransportError``
    instead of returning partial data.
    """

    def get(
        self, path: str, credential: Secret | None, limit: RequestLimit | None = None
    ) -> HttpObservation: ...


@dataclass(frozen=True)
class PlannerInfo:
    """Provenance of the check order, recorded in every report."""

    kind: str
    uses_ai: bool
    model: str | None = None
    prompt_version: str | None = None
    latency_ms: float | None = None
    seed: int | None = None


@dataclass(frozen=True)
class Plan:
    checks: tuple[Check, ...]
    info: PlannerInfo


class Planner(Protocol):
    """Orders the approved checks. A planner can never add, drop or alter checks."""

    def plan(self, checks: Sequence[Check]) -> Plan: ...


ProviderValue = str | int | float | bool
"""A metadata value exactly as a model provider reported it."""


@dataclass(frozen=True)
class ModelDescriptor:
    """What a :class:`ModelClient` is, as configured locally (never inferred).

    ``is_real_model`` is False for scripted test adapters; reports must then never be
    labelled AI. ``settings`` are the generation settings this client sends.
    """

    provider: str
    model: str | None
    is_real_model: bool
    label: str
    settings: Mapping[str, ProviderValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ModelRequest:
    """One provider-neutral completion request.

    ``response_schema`` is a JSON schema the provider may use to constrain output. It is
    only a hint: callers must still validate every reply themselves.
    """

    messages: tuple[ModelMessage, ...]
    response_schema: Mapping[str, object] | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class ModelReply:
    """Raw, untrusted model text plus metadata the provider actually reported."""

    content: str
    latency_ms: float
    provider_metadata: Mapping[str, ProviderValue] = field(default_factory=dict)


class ModelClient(Protocol):
    """A language model (or a clearly labelled scripted stand-in) behind a narrow interface.

    Implementations must honour ``limit`` (abort when it expires or is cancelled), raise
    :class:`~accessguard.domain.errors.ModelError` for every failure, and never fall back
    to another model or to canned output.
    """

    @property
    def descriptor(self) -> ModelDescriptor: ...

    def identify(self, limit: RequestLimit) -> Mapping[str, ProviderValue]:
        """Return version/digest metadata the provider reports; empty if it reports none."""
        ...

    def complete(self, request: ModelRequest, limit: RequestLimit) -> ModelReply: ...
