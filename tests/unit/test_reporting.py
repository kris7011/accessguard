import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from accessguard.adapters.report_files import render_html, write_report
from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard.application.scan import run_scan
from accessguard.domain.scope import TargetScope
from tests.helpers import (
    DOC_BODIES,
    TOKENS,
    ScriptedClient,
    correct_routes,
    credentials,
    observation,
    two_tenant_policy,
)

FINDING_FIELDS = {
    "id",
    "outcome",
    "type",
    "rule",
    "actor",
    "subject",
    "expected",
    "actual",
    "evidence",
    "reproduction",
    "scope",
    "observed_at",
    "versions",
    "verification",
}


def scan(routes: dict[Any, Any]) -> dict[str, Any]:
    return run_scan(
        policy=two_tenant_policy(with_collection=True),
        policy_sha256="0" * 64,
        client=ScriptedClient(routes),
        credentials=credentials(),
        scope=TargetScope.from_origin("http://127.0.0.1:9"),
        target_label="unit-test",
        planner=DeterministicPlanner(),
        limits=RunLimits(),
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )


class ReportTests(unittest.TestCase):
    def test_passing_report(self) -> None:
        report = scan(correct_routes())
        self.assertEqual(report["schema_version"], "1.1")
        self.assertEqual(report["status"]["overall"], "checks_passed")
        self.assertIn("not a security certification", report["status"]["meaning"])
        self.assertEqual(report["coverage"]["conclusive_ratio"], 1.0)
        self.assertFalse(report["run"]["planner"]["uses_ai"])
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["not_tested"])

    def test_finding_has_every_required_field(self) -> None:
        routes = correct_routes()
        routes[("/d/acme", TOKENS["globex-member"])] = observation(200, DOC_BODIES["acme-doc"])
        report = scan(routes)
        self.assertEqual(report["status"]["overall"], "findings")
        finding = report["findings"][0]
        self.assertEqual(set(finding), FINDING_FIELDS)
        self.assertEqual(
            finding["actor"], {"id": "globex-member", "tenant": "globex", "role": "member"}
        )
        self.assertEqual(finding["subject"]["category"], "document")
        self.assertEqual(finding["verification"], "reproduced")
        self.assertEqual(
            finding["evidence"]["matched_protected_fields"][0]["fields"], ["title", "canary"]
        )

    def test_report_contains_no_protected_values_or_credentials(self) -> None:
        routes = correct_routes()
        routes[("/d/acme", TOKENS["globex-member"])] = observation(200, DOC_BODIES["acme-doc"])
        text = json.dumps(scan(routes))
        for token in TOKENS.values():
            self.assertNotIn(token, text)
        for body in DOC_BODIES.values():
            self.assertNotIn(body["canary"], text)

    def test_list_finding_names_leaked_resources(self) -> None:
        routes = correct_routes()
        routes[("/d", TOKENS["globex-member"])] = observation(
            200, {"items": list(DOC_BODIES.values())}
        )
        report = scan(routes)
        finding = next(f for f in report["findings"] if f["id"] == "list:docs@globex-member")
        self.assertEqual([r["id"] for r in finding["subject"]["leaked_resources"]], ["acme-doc"])

    def test_html_escapes_untrusted_text_and_files_are_scrubbed(self) -> None:
        report = scan(correct_routes())
        report["run"]["target"] = "<script>alert(1)</script>"
        page = render_html(report)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        report["run"]["target"] = "leaked secret-value-xyz"
        with tempfile.TemporaryDirectory() as tmp:
            write_report(report, Path(tmp), ["secret-value-xyz"])
            for name in ("report.json", "report.html"):
                self.assertNotIn("secret-value-xyz", (Path(tmp) / name).read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
