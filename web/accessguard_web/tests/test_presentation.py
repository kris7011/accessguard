"""A partial scan, an invalid session or a missing report is never shown as secure."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from django.test import TestCase
from django.urls import reverse

from accessguard_web.scans.models import Scan, ScanKind, ScanStatus
from accessguard_web.scans.presenters import banner_for, report_view
from accessguard_web.tests.support import SAMPLE_DIR, expected_failures

EXAMPLE = Path(__file__).resolve().parents[1] / "pages" / "example_report.json"


def report(**status: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(EXAMPLE.read_text("utf-8"))
    data = copy.deepcopy(data)
    data["status"].update(status)
    return data


def scan(report_data: dict[str, Any] | None, **fields: Any) -> Scan:
    defaults: dict[str, Any] = {
        "status": ScanStatus.COMPLETED,
        "kind": ScanKind.FULL,
        "planned_checks": 30,
        "report": report_data,
        "report_status": report_data["status"]["overall"] if report_data else "",
        "report_complete": report_data["status"]["complete"] if report_data else False,
    }
    defaults.update(fields)
    return Scan(**defaults)


def passing() -> dict[str, Any]:
    data = report(overall="checks_passed", complete=True)
    data["summary"] = {
        "PASS": 30,
        "FAIL": 0,
        "SUSPECTED": 0,
        "INCONCLUSIVE": 0,
        "SKIPPED": 0,
        "ERROR": 0,
    }
    data["findings"] = []
    return data


class BannerTests(TestCase):
    def test_only_a_complete_full_pass_is_green(self) -> None:
        self.assertEqual(banner_for(scan(passing())).tone, "pass")
        cases = {
            "incomplete report": scan(passing(), report_complete=False),
            "retest subset": scan(passing(), kind=ScanKind.RETEST),
            "cancelled": scan(passing(), status=ScanStatus.CANCELLED),
            "inconclusive checks": scan(passing(), report_status="incomplete"),
            "no report": scan(None, status=ScanStatus.FAILED),
            "lost runner": scan(None, status=ScanStatus.LOST),
            "running": scan(None, status=ScanStatus.RUNNING),
            "queued": scan(None, status=ScanStatus.QUEUED),
        }
        for name, item in cases.items():
            with self.subTest(case=name):
                self.assertNotEqual(banner_for(item).tone, "pass")
                self.assertFalse(item.shows_as_passed)

    def test_failures_without_a_report_say_nothing_about_security(self) -> None:
        banner = banner_for(scan(None, status=ScanStatus.LOST, failure_message="runner gone"))
        self.assertIn("says nothing about the target's security", banner.text)

    def test_violations_take_precedence(self) -> None:
        banner = banner_for(scan(report()))
        self.assertEqual(banner.tone, "fail")
        self.assertIn("10 confirmed", banner.title)

    def test_invalid_sessions_are_called_out(self) -> None:
        data = passing()
        data["status"]["overall"] = "incomplete"
        data["sessions"]["ben"] = {"state": "invalid", "reason": "no credential supplied"}
        view = report_view(scan(data, report_status="incomplete"))
        assert view is not None
        self.assertEqual(view.banner.tone, "warn")
        self.assertEqual(view.invalid_sessions, [("ben", "no credential supplied")])
        self.assertTrue(any("could not be verified" in a for a in view.attention))


class ExampleReportTests(TestCase):
    def test_public_example_is_real_engine_output_matching_the_answer_key(self) -> None:
        data = report()
        self.assertEqual({f["id"] for f in data["findings"]}, expected_failures("flawed"))
        self.assertEqual(data["tool"]["name"], "accessguard")
        self.assertTrue(data["status"]["complete"])
        text = json.dumps(data)
        self.assertNotIn("sk_sample_", text, "no sample API key may appear in the public report")
        for value in ("SAMPLE-BUDGET-ORBIT-7731", "SAMPLE-ACCT-QUASAR-0917", "Apollo launch plan"):
            self.assertNotIn(value, text, "no protected value may appear in a report")

    def test_example_page_renders_with_findings_and_disclaimer(self) -> None:
        response = self.client.get(reverse("pages:example_report"))
        self.assertContains(response, "10 confirmed access-control violations")
        self.assertContains(response, "synthetic")
        self.assertContains(response, "not a guarantee of security")

    def test_demo_spec_is_the_sample_apps_spec(self) -> None:
        shipped = json.loads((SAMPLE_DIR / "scan-spec.json").read_text("utf-8"))
        demo = json.loads(
            (Path(__file__).resolve().parents[1] / "projects" / "demo_spec.json").read_text("utf-8")
        )
        self.assertEqual(demo, shipped)
