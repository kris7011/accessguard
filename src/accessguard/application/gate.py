"""Run limits enforced before every outbound request: budget, pacing, deadline and stop signal."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

MAX_REQUEST_BUDGET = 10_000


class InterruptReason(StrEnum):
    BUDGET_EXHAUSTED = "request budget exhausted"
    DEADLINE_EXCEEDED = "run deadline exceeded"
    CANCELLED = "run cancelled by stop signal"


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
    :class:`RunInterrupted` instead of letting a request exceed a limit.
    """

    def __init__(
        self,
        limits: RunLimits,
        stop: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._limits = limits
        self._stop = stop or threading.Event()
        self._clock = clock
        self._sleep = sleep
        self._deadline = clock() + limits.max_duration_seconds
        self._last_request: float | None = None
        self._used = 0

    @property
    def used(self) -> int:
        return self._used

    def acquire(self) -> None:
        if self._stop.is_set():
            raise RunInterrupted(InterruptReason.CANCELLED)
        if self._used >= self._limits.max_requests:
            raise RunInterrupted(InterruptReason.BUDGET_EXHAUSTED)
        now = self._clock()
        if self._last_request is not None:
            wait = self._last_request + self._limits.min_interval_seconds - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        if now >= self._deadline:
            raise RunInterrupted(InterruptReason.DEADLINE_EXCEEDED)
        self._last_request = now
        self._used += 1
