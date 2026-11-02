"""The engine against the separate sample app, configured only by a scan spec and runner config.

The sample app has its own auth (X-Api-Key), envelopes and rules; nothing in the engine is
specific to it. The answer key is hand-written from the app's documented flaws.
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from accessguard.adapters.scan_spec import parse_scan_spec
from accessguard.domain.errors import ScopeViolationError
from accessguard.entrypoints.runner_service import execute_spec
from accessguard_sampleapp.app import Variant
from accessguard_sampleapp.server import run_sample_app
from tests.sampleapp_support import expected_failures, runner_config, sample_spec


def failing(report: dict[str, Any]) -> set[str]:
    return {r["check_id"] for r in report["results"] if r["outcome"] != "PASS"}


class SampleAppScanTests(unittest.TestCase):
    def scan(self, variant: Variant, **spec_changes: object) -> dict[str, Any]:
        with run_sample_app(variant) as app, tempfile.TemporaryDirectory() as tmp:
            spec = parse_scan_spec(sample_spec(app.origin, **spec_changes))
            executed = execute_spec(spec, runner_config(app, Path(tmp)), environ={})
            self.assertTrue(all(key not in str(executed.report) for key in app.api_keys.values()))
            return executed.report

    def test_flawed_variant_yields_exactly_the_answer_key(self) -> None:
        report = self.scan(Variant.FLAWED)
        self.assertEqual(failing(report), expected_failures("flawed"))
        self.assertEqual({f["id"] for f in report["findings"]}, expected_failures("flawed"))
        self.assertTrue(all(f["verification"] == "reproduced" for f in report["findings"]))
        self.assertEqual(report["status"]["overall"], "findings")
        self.assertEqual(report["run"]["scope"]["zone"], "loopback")
        self.assertIn("paths under: /api/v1", report["run"]["scope"]["constraints"])

    def test_fixed_variant_passes_every_check(self) -> None:
        report = self.scan(Variant.FIXED)
        self.assertEqual(failing(report), set())
        self.assertEqual(report["status"]["overall"], "checks_passed")
        self.assertEqual(report["coverage"]["conclusive_ratio"], 1.0)

    def test_retest_of_fixed_findings_passes_and_says_it_is_a_subset(self) -> None:
        findings = sorted(expected_failures("flawed"))
        report = self.scan(Variant.FIXED, checks=findings)
        self.assertEqual(report["run"]["selection"]["mode"], "subset")
        self.assertEqual(report["coverage"]["planned_checks"], len(findings))
        self.assertEqual(report["status"]["overall"], "checks_passed")

    def test_missing_credentials_are_inconclusive_never_pass(self) -> None:
        with run_sample_app(Variant.FIXED) as app, tempfile.TemporaryDirectory() as tmp:
            config = runner_config(app, Path(tmp))
            Path(tmp, "credentials.json").write_text('{"ana": "' + app.api_keys["ana"] + '"}')
            executed = execute_spec(parse_scan_spec(sample_spec(app.origin)), config, environ={})
        report = executed.report
        self.assertEqual(report["sessions"]["ben"]["state"], "invalid")
        self.assertEqual(report["status"]["overall"], "incomplete")
        ben = [r for r in report["results"] if r["actor"] == "ben"]
        self.assertTrue(ben and all(r["outcome"] == "INCONCLUSIVE" for r in ben))

    def test_wrong_credentials_are_detected_by_the_session_probe(self) -> None:
        with run_sample_app(Variant.FIXED) as app, tempfile.TemporaryDirectory() as tmp:
            config = runner_config(app, Path(tmp))
            swapped = dict(app.api_keys, ben=app.api_keys["cleo"])  # ben's key is someone else's
            Path(tmp, "credentials.json").write_text(json.dumps(swapped))
            report = execute_spec(
                parse_scan_spec(sample_spec(app.origin)), config, environ={}
            ).report
        self.assertEqual(report["sessions"]["ben"]["state"], "invalid")
        self.assertNotEqual(report["status"]["overall"], "checks_passed")

    def test_untrusted_origin_is_refused_before_any_request(self) -> None:
        with run_sample_app(Variant.FLAWED) as app, tempfile.TemporaryDirectory() as tmp:
            config = runner_config(app, Path(tmp))
            other = sample_spec("http://127.0.0.1:1")
            with self.assertRaisesRegex(ScopeViolationError, "allowlist"):
                execute_spec(parse_scan_spec(other), config, environ={})

    def test_policy_paths_outside_the_allowed_prefixes_are_refused(self) -> None:
        with run_sample_app(Variant.FLAWED) as app, tempfile.TemporaryDirectory() as tmp:
            document = sample_spec(app.origin)
            document["target"]["path_prefixes"] = ["/api/v1/projects"]
            with self.assertRaisesRegex(ScopeViolationError, "outside the allowed prefixes"):
                execute_spec(parse_scan_spec(document), runner_config(app, Path(tmp)), environ={})

    def test_local_budget_cap_limits_the_run_honestly(self) -> None:
        with run_sample_app(Variant.FLAWED) as app, tempfile.TemporaryDirectory() as tmp:
            config = runner_config(app, Path(tmp), max_requests=12)
            report = execute_spec(
                parse_scan_spec(sample_spec(app.origin)), config, environ={}
            ).report
        self.assertEqual(report["run"]["limits"]["max_requests"], 12)
        self.assertLessEqual(report["run"]["requests_used"], 12)
        self.assertEqual(report["run"]["interrupted"], "request budget exhausted")
        self.assertFalse(report["status"]["complete"])
        self.assertNotEqual(report["status"]["overall"], "checks_passed")

    def test_cancellation_produces_a_partial_report(self) -> None:
        stop = threading.Event()
        stop.set()
        with run_sample_app(Variant.FIXED) as app, tempfile.TemporaryDirectory() as tmp:
            report = execute_spec(
                parse_scan_spec(sample_spec(app.origin)),
                runner_config(app, Path(tmp)),
                stop=stop,
                environ={},
            ).report
        self.assertEqual(report["run"]["interrupted"], "run cancelled by stop signal")
        self.assertEqual(report["status"]["overall"], "incomplete")


if __name__ == "__main__":
    unittest.main()
