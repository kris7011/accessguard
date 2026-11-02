"""Controlled cancellation for command-line runs.

Ctrl+C (SIGINT), SIGTERM and, on Windows, Ctrl+Break set a stop event instead of raising
``KeyboardInterrupt`` somewhere inside a request. The work runs in a worker thread so the
main thread stays responsive to signals; the engine sees the event through its request
gate, finishes with the remaining checks marked SKIPPED, and the caller writes a partial
report.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import TypeVar

T = TypeVar("T")

_JOIN_POLL_SECONDS = 0.1


def _signals() -> list[signal.Signals]:
    names = ("SIGINT", "SIGTERM", "SIGBREAK")
    return [getattr(signal, name) for name in names if hasattr(signal, name)]


@contextmanager
def stop_on_signals() -> Iterator[threading.Event]:
    """Yield an event that is set by the first interrupt signal; restore handlers on exit.

    Outside the main thread (where Python forbids installing handlers) the event is
    still returned, but only the caller can set it.
    """
    stop = threading.Event()
    if threading.current_thread() is not threading.main_thread():
        yield stop
        return

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    previous = {sig: signal.signal(sig, request_stop) for sig in _signals()}
    try:
        yield stop
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_in_worker(work: Callable[[], T]) -> T:
    """Run ``work`` in a worker thread while the main thread keeps handling signals."""
    outcome: dict[str, T] = {}
    failure: list[BaseException] = []

    def target() -> None:
        try:
            outcome["value"] = work()
        except BaseException as error:  # re-raised in the calling thread below
            failure.append(error)

    worker = threading.Thread(target=target, name="accessguard-run")
    worker.start()
    while worker.is_alive():
        worker.join(_JOIN_POLL_SECONDS)
    if failure:
        raise failure[0]
    return outcome["value"]
