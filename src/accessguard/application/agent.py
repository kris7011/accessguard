"""The bounded adaptive agent loop (milestone 2).

One agent, one tool. Each turn the model sees the minimised run state
(:mod:`~accessguard.application.agent_view`), proposes one action, and the action is
validated against the tool contract (:mod:`~accessguard.application.agent_contract`).
A valid ``run_check`` is executed by the deterministic :class:`CheckRunner`, which also
decides the verdict; the model only chooses *which* approved check runs next, or stops.

Limits: model calls, invalid replies and output size (here), the target request budget,
run deadline and stop signal (the shared :class:`RequestGate`, which also bounds every
model call). Session probes, owner baselines and retests go through the same gate, so
they count against the same HTTP budget as the checks themselves.

Failure handling: a model failure ends the loop with an explicit status, completed
results are kept and every check that was not executed is reported as SKIPPED. There is
no fallback to another model or to the deterministic order.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import partial
from typing import TypeVar

from accessguard.application.agent_contract import (
    SYSTEM_PROMPT,
    ReplyRejectedError,
    RunCheck,
    Stop,
    parse_action,
    response_schema,
)
from accessguard.application.agent_view import Budget, ModelView
from accessguard.application.gate import (
    InterruptReason,
    RequestGate,
    RunInterrupted,
    RunLimits,
)
from accessguard.application.ports import (
    ModelClient,
    ModelDescriptor,
    ModelMessage,
    ModelRequest,
    ProviderValue,
    RequestLimit,
)
from accessguard.application.runner import CheckRunner, RunOutcome, skipped
from accessguard.domain.checks import Check
from accessguard.domain.errors import ModelError
from accessguard.domain.policy import AccessPolicy
from accessguard.domain.results import CheckResult

T = TypeVar("T")

MAX_MODEL_CALLS = 10_000
_GATE_REASONS = frozenset(
    {
        InterruptReason.BUDGET_EXHAUSTED,
        InterruptReason.DEADLINE_EXCEEDED,
        InterruptReason.CANCELLED,
    }
)


@dataclass(frozen=True)
class AgentLimits:
    """Bounds on the model side of an agent run. Validated on construction."""

    max_model_calls: int = 200
    max_invalid_replies: int = 3
    """The run ends when this many replies have been rejected."""
    max_output_chars: int = 512
    """A valid reply is under 60 characters; anything longer than this is rejected unread."""
    model_timeout_seconds: float = 60.0
    """Per call; every call is also bounded by the remaining run time."""
    max_output_tokens: int = 64
    """Generation limit requested from the provider (a hint; the size check is enforced)."""

    def __post_init__(self) -> None:
        if not 1 <= self.max_model_calls <= MAX_MODEL_CALLS:
            raise ValueError(f"max_model_calls must be between 1 and {MAX_MODEL_CALLS}")
        if not 1 <= self.max_invalid_replies <= 100:
            raise ValueError("max_invalid_replies must be between 1 and 100")
        if not 16 <= self.max_output_chars <= 65_536:
            raise ValueError("max_output_chars must be between 16 and 65536")
        if not 0 < self.model_timeout_seconds <= 600:
            raise ValueError("model_timeout_seconds must be greater than 0 and at most 600")
        if not 1 <= self.max_output_tokens <= 4096:
            raise ValueError("max_output_tokens must be between 1 and 4096")


class AgentStatus(StrEnum):
    COMPLETED = "completed"
    """Every approved check was selected and executed."""
    STOPPED_EARLY = "stopped_early"
    """The model chose ``stop`` while checks were pending."""
    MODEL_FAILED = "model_failed"
    """A model call failed (unreachable, timeout, malformed provider response)."""
    INVALID_REPLIES_EXHAUSTED = "invalid_replies_exhausted"
    MODEL_CALLS_EXHAUSTED = "model_calls_exhausted"
    INTERRUPTED = "interrupted"
    """A run limit ended the run: request budget, run deadline or stop signal."""


FAILED_STATUSES = frozenset({AgentStatus.MODEL_FAILED, AgentStatus.INVALID_REPLIES_EXHAUSTED})
"""Statuses that mean the model did not work as required (exit code 5 in the CLI)."""

_STATUS_REASON = {
    AgentStatus.STOPPED_EARLY: InterruptReason.AGENT_STOPPED,
    AgentStatus.MODEL_FAILED: InterruptReason.MODEL_FAILED,
    AgentStatus.INVALID_REPLIES_EXHAUSTED: InterruptReason.INVALID_REPLIES_EXHAUSTED,
    AgentStatus.MODEL_CALLS_EXHAUSTED: InterruptReason.MODEL_CALLS_EXHAUSTED,
}


class StepAction(StrEnum):
    RUN_CHECK = "run_check"
    STOP = "stop"
    REJECTED = "rejected"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class AgentStep:
    """One model call. ``check_id`` is set only for an accepted ``run_check``."""

    call: int
    action: StepAction
    check_id: str | None = None
    rejection: str | None = None
    latency_ms: float | None = None
    provider_metadata: Mapping[str, ProviderValue] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTrace:
    """Auditable record of the model side of a run (no prompts, no model text)."""

    descriptor: ModelDescriptor
    provider_metadata: Mapping[str, ProviderValue]
    provider_metadata_error: str | None
    limits: AgentLimits
    status: AgentStatus
    failure: str | None
    steps: tuple[AgentStep, ...]

    @property
    def model_calls(self) -> int:
        return len(self.steps)

    @property
    def invalid_replies(self) -> int:
        return sum(step.action is StepAction.REJECTED for step in self.steps)

    @property
    def model_latency_ms(self) -> float:
        return round(sum(step.latency_ms or 0.0 for step in self.steps), 1)


@dataclass(frozen=True)
class AgentRunResult:
    outcome: RunOutcome
    trace: AgentTrace


class _AgentEnded(Exception):  # noqa: N818 - control flow inside the loop
    def __init__(self, status: AgentStatus, failure: str | None = None) -> None:
        super().__init__(status.value)
        self.status = status
        self.failure = failure


class AgentLoop:
    """Runs one agent session. Create a new instance (and runner and gate) per run."""

    def __init__(
        self,
        *,
        policy: AccessPolicy,
        checks: Sequence[Check],
        runner: CheckRunner,
        gate: RequestGate,
        run_limits: RunLimits,
        model: ModelClient,
        limits: AgentLimits,
    ) -> None:
        self._checks = tuple(checks)
        self._view = ModelView(policy, checks)
        self._runner = runner
        self._gate = gate
        self._run_limits = run_limits
        self._model = model
        self._limits = limits
        self._steps: list[AgentStep] = []
        self._results: list[CheckResult] = []
        self._pending = list(self._view.aliases)
        self._executed: set[str] = set()
        self._last_rejection: str | None = None

    def run(self) -> AgentRunResult:
        metadata: Mapping[str, ProviderValue] = {}
        metadata_error: str | None = None
        status = AgentStatus.COMPLETED
        failure: str | None = None
        interrupted: InterruptReason | None = None
        try:
            try:
                metadata = self._guarded(self._model.identify)
            except ModelError as error:
                # Missing provenance is recorded, not hidden; a broken model fails below.
                metadata_error = str(error)
            self._runner.begin(len(self._checks))
            interrupted = self._runner.preflight()
            if interrupted is None:
                self._select_and_execute()
        except RunInterrupted as stop:
            interrupted = stop.reason
        except _AgentEnded as ended:
            status, failure = ended.status, ended.failure
            interrupted = _STATUS_REASON[status]
        if interrupted in _GATE_REASONS:
            status = AgentStatus.INTERRUPTED

        results = list(self._results)
        executed = {result.check.id for result in results}
        if interrupted is not None:
            results += [skipped(c, interrupted) for c in self._checks if c.id not in executed]
        # Retests are deterministic engine work. They run after the model ended the loop,
        # and after the request budget is spent they draw on the reserve the runner held
        # back per finding; a deadline or cancellation still stops them.
        outcome = self._runner.finish(
            results, interrupted if interrupted in _GATE_REASONS else None
        )
        if interrupted is not None and interrupted not in _GATE_REASONS:
            outcome = replace(outcome, interrupted=interrupted)
        trace = AgentTrace(
            descriptor=self._model.descriptor,
            provider_metadata=dict(metadata),
            provider_metadata_error=metadata_error,
            limits=self._limits,
            status=status,
            failure=failure,
            steps=tuple(self._steps),
        )
        return AgentRunResult(outcome, trace)

    def _guarded(self, call: Callable[[RequestLimit], T]) -> T:
        """Run a model call within the run deadline and stop signal.

        A provider error caused by the deadline or the stop signal is an interruption of
        the run (``RunInterrupted``), not a model failure.
        """
        limit = self._gate.call_limit(self._limits.model_timeout_seconds)
        try:
            return call(limit)
        except ModelError:
            self._gate.raise_if_interrupted()
            raise

    def _select_and_execute(self) -> None:
        while self._pending:
            if len(self._steps) >= self._limits.max_model_calls:
                raise _AgentEnded(
                    AgentStatus.MODEL_CALLS_EXHAUSTED,
                    f"{self._limits.max_model_calls} model calls used with "
                    f"{len(self._pending)} checks pending",
                )
            call = len(self._steps) + 1
            request = self._request()
            try:
                reply = self._guarded(partial(self._model.complete, request))
            except RunInterrupted:
                self._steps.append(AgentStep(call, StepAction.INTERRUPTED))
                raise
            except ModelError as error:
                self._steps.append(AgentStep(call, StepAction.FAILED))
                raise _AgentEnded(AgentStatus.MODEL_FAILED, str(error)) from error
            try:
                action = parse_action(
                    reply.content,
                    pending=self._pending,
                    executed=self._executed,
                    max_chars=self._limits.max_output_chars,
                )
            except ReplyRejectedError as rejected:
                self._reject(call, rejected, reply.latency_ms, reply.provider_metadata)
                continue
            self._last_rejection = None
            if isinstance(action, Stop):
                self._steps.append(
                    AgentStep(
                        call, StepAction.STOP, None, None, reply.latency_ms, reply.provider_metadata
                    )
                )
                raise _AgentEnded(AgentStatus.STOPPED_EARLY)
            self._run_check(call, action, reply.latency_ms, reply.provider_metadata)

    def _reject(
        self,
        call: int,
        rejected: ReplyRejectedError,
        latency_ms: float,
        metadata: Mapping[str, ProviderValue],
    ) -> None:
        reason = rejected.rejection.value
        self._steps.append(AgentStep(call, StepAction.REJECTED, None, reason, latency_ms, metadata))
        self._last_rejection = reason
        invalid = sum(step.action is StepAction.REJECTED for step in self._steps)
        if invalid >= self._limits.max_invalid_replies:
            raise _AgentEnded(
                AgentStatus.INVALID_REPLIES_EXHAUSTED,
                f"{invalid} invalid model replies (last rejection: {reason})",
            )

    def _run_check(
        self,
        call: int,
        action: RunCheck,
        latency_ms: float,
        metadata: Mapping[str, ProviderValue],
    ) -> None:
        check = self._view.check(action.alias)
        self._steps.append(
            AgentStep(call, StepAction.RUN_CHECK, check.id, None, latency_ms, metadata)
        )
        self._pending.remove(action.alias)
        self._executed.add(action.alias)
        try:
            result = self._runner.execute(check)
        except RunInterrupted as stop:
            self._results.append(skipped(check, stop.reason, in_progress=True))
            raise
        self._results.append(result)

    def _request(self) -> ModelRequest:
        invalid = sum(step.action is StepAction.REJECTED for step in self._steps)
        state = self._view.state(
            results=self._results,
            sessions=self._runner.sessions,
            budget=Budget(
                http_requests_used=self._gate.used,
                http_requests_max=self._run_limits.max_requests,
                model_calls_used=len(self._steps),
                model_calls_max=self._limits.max_model_calls,
                invalid_replies=invalid,
                invalid_replies_max=self._limits.max_invalid_replies,
            ),
            last_rejection=self._last_rejection,
        )
        return ModelRequest(
            messages=(
                ModelMessage("system", SYSTEM_PROMPT),
                ModelMessage("user", json.dumps(state, separators=(",", ":"))),
            ),
            response_schema=response_schema(self._pending),
            max_output_tokens=self._limits.max_output_tokens,
        )
