"""Run limits enforced before every outbound request: budget, pacing, deadline and stop signal."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from accessguard.application.ports import RequestLimit

MAX_REQUEST_BUDGET = 10_000


class InterruptReason(StrEnum):
    """Why a run ended before every planned check produced a verdict."""

    BUDGET_EXHAUSTED = "request budget exhausted"
    DEADLINE_EXCEEDED = "run deadline exceeded"
    CANCELLED = "run cancelled by stop signal"
    AGENT_STOPPED = "agent stopped before every check was executed"
    MODEL_FAILED = "model failure ended the agent run"
    INVALID_REPLIES_EXHAUSTED = "too many invalid model replies"
    MODEL_CALLS_EXHAUSTED = "model call budget exhausted"


class RunInterrupted(Exception):  # noqa: N818 - control-flow signal, not an error condition
    def __init__(self, reason: InterruptReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class RunLimits:
    max_requests: int = 500
    min_interval_seconds: float = 0.0
    max_duration_seconds: float = 300.0
    retest_findings: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.max_requests <= MAX_REQUEST_BUDGET:
            raise ValueError(f"max_requests must be between 1 and {MAX_REQUEST_BUDGET}")
        if not 0 <= self.min_interval_seconds <= 60:
            raise ValueError("min_interval_seconds must be between 0 and 60")
        if not 0 < self.max_duration_seconds <= 3600:
            raise ValueError("max_duration_seconds must be greater than 0 and at most 3600")


class RequestGate:
    """Admits one request at a time (concurrency 1) within the configured limits.

    Every outbound request must call :meth:`acquire` first; it raises
    :class:`RunInterrupted` instead of letting a request exceed a limit, and returns a
    :class:`RequestLimit` that lets the transport abort the request when the run deadline
    passes or the stop signal is set while the request is in flight.
    """

    def __init__(
        self,
        limits: RunLimits,
        stop: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self._limits = limits
        self._stop = stop or threading.Event()
        self._clock = clock
        # Waiting on the stop event makes pacing interruptible by cancellation.
        self._sleep = sleep or self._stop.wait
        self._deadline = clock() + limits.max_duration_seconds
        self._last_request: float | None = None
        self._used = 0

    @property
    def used(self) -> int:
        return self._used

    def raise_if_interrupted(self) -> None:
        """Raise :class:`RunInterrupted` if the run was cancelled or its deadline passed."""
        if self._stop.is_set():
            raise RunInterrupted(InterruptReason.CANCELLED)
        if self._clock() >= self._deadline:
            raise RunInterrupted(InterruptReason.DEADLINE_EXCEEDED)

    def call_limit(self, max_seconds: float) -> RequestLimit:
        """Bound a non-target call (e.g. a model call) by the run deadline and stop signal.

        It does not consume the target request budget and is not paced.
        """
        self.raise_if_interrupted()
        remaining = self._deadline - self._clock()
        return RequestLimit(seconds=min(max_seconds, remaining), cancel=self._stop)

    def acquire(self) -> RequestLimit:
        self.raise_if_interrupted()
        if self._used >= self._limits.max_requests:
            raise RunInterrupted(InterruptReason.BUDGET_EXHAUSTED)
        if self._last_request is not None:
            wait = self._last_request + self._limits.min_interval_seconds - self._clock()
            if wait > 0:
                self._sleep(min(wait, max(self._deadline - self._clock(), 0.0)))
        self.raise_if_interrupted()
        now = self._clock()
        self._last_request = now
        self._used += 1
        return RequestLimit(seconds=self._deadline - now, cancel=self._stop)
