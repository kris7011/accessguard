import threading
import unittest

from accessguard.application.gate import InterruptReason, RequestGate, RunInterrupted, RunLimits


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RequestGateTests(unittest.TestCase):
    def gate(self, limits: RunLimits, stop: threading.Event | None = None) -> RequestGate:
        self.clock = FakeClock()
        return RequestGate(limits, stop, clock=self.clock, sleep=self.clock.sleep)

    def interrupt_reason(self, gate: RequestGate) -> InterruptReason:
        with self.assertRaises(RunInterrupted) as raised:
            gate.acquire()
        return raised.exception.reason

    def test_budget_is_a_hard_limit(self) -> None:
        gate = self.gate(RunLimits(max_requests=2))
        gate.acquire()
        gate.acquire()
        self.assertEqual(self.interrupt_reason(gate), InterruptReason.BUDGET_EXHAUSTED)
        self.assertEqual(gate.used, 2)

    def test_stop_signal_cancels(self) -> None:
        stop = threading.Event()
        gate = self.gate(RunLimits(), stop)
        gate.acquire()
        stop.set()
        self.assertEqual(self.interrupt_reason(gate), InterruptReason.CANCELLED)

    def test_deadline(self) -> None:
        gate = self.gate(RunLimits(max_duration_seconds=10))
        gate.acquire()
        self.clock.now += 11
        self.assertEqual(self.interrupt_reason(gate), InterruptReason.DEADLINE_EXCEEDED)

    def test_pacing_spaces_requests(self) -> None:
        gate = self.gate(RunLimits(min_interval_seconds=0.5))
        gate.acquire()
        self.clock.now += 0.2
        gate.acquire()
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertAlmostEqual(self.clock.sleeps[0], 0.3)

    def test_limits_are_validated(self) -> None:
        for kwargs in (
            {"max_requests": 0},
            {"max_requests": 10_001},
            {"min_interval_seconds": -1},
            {"max_duration_seconds": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RunLimits(**kwargs)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
