"""Wall-clock limits and cancellation must also bound requests that are already in flight."""

import socket
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from typing import Any
from unittest import mock

from accessguard.adapters import http_transport
from accessguard.adapters.http_target import ScopedHttpClient
from accessguard.application.gate import RequestGate, RunInterrupted, RunLimits
from accessguard.domain.errors import TransportError
from accessguard.domain.scope import TargetScope
from accessguard_lab.server import QuietHTTPServer
from tests.integration.test_edge_cases import edge_policy, outcome_of, scan_edges

DRIP_SECONDS = 3.0


class DripHandler(BaseHTTPRequestHandler):
    """Sends headers at once, then one body byte every 0.1 s (a slow-drip response)."""

    def log_message(self, format: str, *args: object) -> None:
        """Keep test output quiet."""

    def do_GET(self) -> None:
        chunks = int(DRIP_SECONDS / 0.1)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(chunks))
        self.end_headers()
        for _ in range(chunks):
            self.wfile.write(b" ")
            self.wfile.flush()
            time.sleep(0.1)


@contextmanager
def drip_server() -> Iterator[TargetScope]:
    server = QuietHTTPServer(("127.0.0.1", 0), DripHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield TargetScope.from_origin(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def timed(function: Any) -> tuple[Any, float]:
    started = time.monotonic()
    try:
        return function(), time.monotonic() - started
    except Exception as error:  # returned so tests can assert on it
        return error, time.monotonic() - started


class InFlightLimitTests(unittest.TestCase):
    def test_request_timeout_is_a_total_limit_not_per_read(self) -> None:
        with drip_server() as scope:
            client = ScopedHttpClient(scope, timeout_seconds=0.5)
            result, elapsed = timed(lambda: client.get("/x", None))
        self.assertIsInstance(result, TransportError)
        self.assertLess(elapsed, 1.5, "a slow-drip body must not extend the request timeout")

    def test_aborted_request_releases_its_socket(self) -> None:
        """The response's file object must not keep the socket open after an abort."""
        opened: list[socket.socket] = []
        original_connect = http_transport._GuardedConnection.connect

        def recording_connect(connection: http_transport._GuardedConnection) -> None:
            original_connect(connection)
            assert connection.guarded is not None
            opened.append(connection.guarded)

        with (
            mock.patch.object(http_transport._GuardedConnection, "connect", recording_connect),
            drip_server() as scope,
            self.assertRaises(TransportError) as raised,
        ):
            ScopedHttpClient(scope, timeout_seconds=0.3).get("/x", None)
        # ``raised`` keeps the traceback (and the request's frame) alive, as a caller would.
        self.assertIn("time limit", str(raised.exception))
        self.assertEqual(opened[0].fileno(), -1, "socket still open after the aborted request")

    def test_run_deadline_bounds_a_slow_in_flight_request(self) -> None:
        policy = edge_policy(("slow", "/edge/slow/acme-doc"))
        limits = RunLimits(max_duration_seconds=0.6)
        report, elapsed = timed(lambda: scan_edges(policy, limits=limits))
        self.assertLess(elapsed, 1.4, "the run deadline must interrupt the 1.5 s response")
        self.assertEqual(report["run"]["interrupted"], "run deadline exceeded")
        result = outcome_of(report, "slow@globex-member")
        self.assertEqual(result["outcome"], "SKIPPED")
        self.assertIn("in progress", result["reason"])
        self.assertEqual(report["status"]["overall"], "incomplete")
        self.assertFalse(report["run"]["completed"])

    def test_stop_signal_cancels_an_in_flight_request(self) -> None:
        policy = edge_policy(("slow", "/edge/slow/acme-doc"))
        stop = threading.Event()
        timer = threading.Timer(0.3, stop.set)
        timer.start()
        try:
            report, elapsed = timed(lambda: scan_edges(policy, stop=stop))
        finally:
            timer.cancel()
        self.assertLess(elapsed, 1.2, "cancellation must not wait for the 1.5 s response")
        self.assertEqual(report["run"]["interrupted"], "run cancelled by stop signal")
        outcomes = {r["check_id"]: r["outcome"] for r in report["results"]}
        self.assertEqual(outcomes["slow@acme-member"], "PASS", "completed results are kept")
        self.assertEqual(outcomes["slow@globex-member"], "SKIPPED")
        self.assertEqual(outcomes["slow@anonymous"], "SKIPPED")
        self.assertGreater(report["summary"]["SKIPPED"], 0)
        self.assertLess(report["coverage"]["conclusive_ratio"], 1.0)

    def test_pacing_wait_is_interruptible(self) -> None:
        stop = threading.Event()
        gate = RequestGate(RunLimits(min_interval_seconds=5.0), stop)
        gate.acquire()
        threading.Timer(0.2, stop.set).start()
        result, elapsed = timed(gate.acquire)
        self.assertIsInstance(result, RunInterrupted)
        self.assertLess(elapsed, 1.0)

    def test_per_request_timeout_without_interruption_is_a_technical_error(self) -> None:
        report = scan_edges(edge_policy(("slow", "/edge/slow/acme-doc")), timeout_seconds=0.3)
        self.assertEqual(outcome_of(report, "slow@globex-member")["outcome"], "ERROR")
        self.assertIsNone(report["run"]["interrupted"])


if __name__ == "__main__":
    unittest.main()
