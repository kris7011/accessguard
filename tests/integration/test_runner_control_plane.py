"""The runner loop against a fake control plane and the real sample app over HTTP."""

import json
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from accessguard.adapters.control_plane import ControlPlaneClient, ControlPlaneError
from accessguard.adapters.scan_spec import sha256_of
from accessguard.domain.credentials import Secret
from accessguard.entrypoints import runner_service
from accessguard.entrypoints.runner_service import run_forever
from accessguard_lab.server import QuietHTTPServer
from accessguard_sampleapp.app import Variant
from accessguard_sampleapp.server import run_sample_app
from tests.sampleapp_support import expected_failures, runner_config, sample_spec

RUNNER_TOKEN = "agr_test_token_0123456789abcdef"


class FakeControlPlane(BaseHTTPRequestHandler):
    """Records every runner call; serves the queued jobs once each."""

    jobs: ClassVar[list[dict[str, Any]]] = []
    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []
    cancel_after_heartbeats: ClassVar[int | None] = None
    redirect_claim: ClassVar[bool] = False

    def log_message(self, format: str, *args: object) -> None:
        """Quiet."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.headers.get("Authorization") != f"Bearer {RUNNER_TOKEN}":
            self._send(401, {"error": "invalid runner token"})
            return
        FakeControlPlane.calls.append((self.path, body))
        if self.path.endswith("/claim"):
            if FakeControlPlane.redirect_claim:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/steal")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not FakeControlPlane.jobs:
                self._send(204, None)
                return
            self._send(200, {"job": FakeControlPlane.jobs.pop(0)})
        elif self.path.endswith("/heartbeat"):
            beats = sum(1 for path, _ in FakeControlPlane.calls if path.endswith("/heartbeat"))
            limit = FakeControlPlane.cancel_after_heartbeats
            self._send(200, {"cancel": limit is not None and beats >= limit})
        else:
            self._send(200, {"ok": True})

    def _send(self, status: int, body: object) -> None:
        payload = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def control_plane() -> Iterator[str]:
    FakeControlPlane.jobs = []
    FakeControlPlane.calls = []
    FakeControlPlane.cancel_after_heartbeats = None
    FakeControlPlane.redirect_claim = False
    server = QuietHTTPServer(("127.0.0.1", 0), FakeControlPlane)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def job(spec: dict[str, Any], job_id: str = "job-1") -> dict[str, Any]:
    return {"id": job_id, "spec": spec, "spec_sha256": sha256_of(spec)}


def calls(suffix: str) -> list[dict[str, Any]]:
    return [body for path, body in FakeControlPlane.calls if path.endswith(suffix)]


class RunnerLoopTests(unittest.TestCase):
    def run_once(self, url: str, config: Any, token: str = RUNNER_TOKEN) -> int:
        client = ControlPlaneClient(url, Secret(token))
        return run_forever(
            client, config, stop=threading.Event(), once=True, log=lambda message: None
        )

    def test_claims_scans_and_uploads_a_scrubbed_report(self) -> None:
        with (
            control_plane() as url,
            run_sample_app(Variant.FLAWED) as app,
            tempfile.TemporaryDirectory() as tmp,
        ):
            spec = sample_spec(app.origin)
            FakeControlPlane.jobs = [job(spec)]
            self.assertEqual(self.run_once(url, runner_config(app, Path(tmp))), 0)
        reports = calls("/jobs/job-1/report")
        self.assertEqual(len(reports), 1)
        report = reports[0]["report"]
        self.assertEqual({f["id"] for f in report["findings"]}, expected_failures("flawed"))
        self.assertEqual(report["run"]["spec_sha256"], sha256_of(spec))
        uploaded = json.dumps(FakeControlPlane.calls)
        for key in app.api_keys.values():
            self.assertNotIn(key, uploaded)
        for protected in ("SAMPLE-BUDGET-ORBIT-7731", "SAMPLE-ACCT-ORBIT-0042", "Apollo launch"):
            self.assertNotIn(protected, uploaded, "response values must stay with the runner")
        self.assertTrue(calls("/heartbeat"))

    def test_untrusted_origin_is_reported_as_scope_rejected_without_scanning(self) -> None:
        with (
            control_plane() as url,
            run_sample_app(Variant.FLAWED) as app,
            tempfile.TemporaryDirectory() as tmp,
        ):
            FakeControlPlane.jobs = [job(sample_spec("http://10.0.0.5:8800"))]
            self.assertEqual(self.run_once(url, runner_config(app, Path(tmp))), 0)
        self.assertEqual(calls("/fail")[0]["category"], "scope_rejected")
        self.assertEqual(calls("/report"), [])

    def test_spec_that_does_not_match_its_hash_is_refused(self) -> None:
        with (
            control_plane() as url,
            run_sample_app(Variant.FLAWED) as app,
            tempfile.TemporaryDirectory() as tmp,
        ):
            tampered = job(sample_spec(app.origin))
            tampered["spec"]["label"] = "changed after approval"
            FakeControlPlane.jobs = [tampered]
            self.run_once(url, runner_config(app, Path(tmp)))
        self.assertEqual(calls("/fail")[0]["category"], "invalid_spec")

    def test_cancel_from_the_control_plane_stops_the_scan_with_a_partial_report(self) -> None:
        with (
            control_plane() as url,
            run_sample_app(Variant.FLAWED) as app,
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(runner_service, "HEARTBEAT_SECONDS", 0.1),
        ):
            spec = sample_spec(
                app.origin, limits={"max_requests": 300, "min_interval_seconds": 0.1}
            )
            FakeControlPlane.jobs = [job(spec)]
            FakeControlPlane.cancel_after_heartbeats = 3
            self.run_once(url, runner_config(app, Path(tmp)))
        report = calls("/report")[0]["report"]
        self.assertEqual(report["run"]["interrupted"], "run cancelled by stop signal")
        self.assertIn(report["status"]["overall"], ("incomplete", "findings"))
        self.assertFalse(report["status"]["complete"])
        self.assertTrue(any(r["outcome"] == "SKIPPED" for r in report["results"]))

    def test_rejected_token_stops_the_runner(self) -> None:
        with control_plane() as url, tempfile.TemporaryDirectory() as tmp:
            with run_sample_app(Variant.FLAWED) as app:
                config = runner_config(app, Path(tmp))
            self.assertEqual(self.run_once(url, config, token="wrong-token-0123456789"), 4)

    def test_redirects_are_never_followed(self) -> None:
        with control_plane() as url:
            FakeControlPlane.redirect_claim = True
            client = ControlPlaneClient(url, Secret(RUNNER_TOKEN))
            with self.assertRaises(ControlPlaneError) as raised:
                client.claim()
        self.assertEqual(raised.exception.status, 302)


if __name__ == "__main__":
    unittest.main()
