import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from accessguard.adapters.report_files import render_html, write_report
from accessguard.adapters.scripted_model import ScriptedModelClient, in_order_responder
from accessguard.application.agent import AgentLimits
from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard.application.scan import AgentStrategy, run_agent_scan, run_scan
from accessguard.domain.errors import ModelError
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
        self.assertEqual(report["schema_version"], "1.2")
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

    def test_baseline_report_has_no_agent_section_and_records_request_progress(self) -> None:
        report = scan(correct_routes())
        self.assertIsNone(report["run"]["agent"])
        self.assertIsNone(report["run"]["planner"]["seed"])
        progress = [r["requests_used_after"] for r in report["results"]]
        self.assertEqual(progress, sorted(progress))
        self.assertTrue(all(isinstance(value, int) for value in progress))
        self.assertIn("Deterministisk baseline (ingen AI)", render_html(report))


def agent_report(model: ScriptedModelClient) -> dict[str, Any]:
    return run_agent_scan(
        policy=two_tenant_policy(with_collection=True),
        policy_sha256="0" * 64,
        client=ScriptedClient(correct_routes()),
        credentials=credentials(),
        scope=TargetScope.from_origin("http://127.0.0.1:9"),
        target_label="unit-test",
        agent=AgentStrategy(model, AgentLimits(max_model_calls=50)),
        limits=RunLimits(),
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )


class AgentReportTests(unittest.TestCase):
    def test_scripted_agent_report_is_never_labelled_ai(self) -> None:
        report = agent_report(ScriptedModelClient(in_order_responder, name="in-order"))
        planner = report["run"]["planner"]
        self.assertEqual((planner["kind"], planner["uses_ai"]), ("agent", False))
        agent = report["run"]["agent"]
        self.assertFalse(agent["adapter"]["is_real_model"])
        self.assertIn("not an AI model", agent["adapter"]["label"])
        self.assertEqual(agent["status"], "completed")
        self.assertEqual(agent["allowed_actions"], ["run_check", "stop"])
        self.assertEqual((agent["prompt_version"], agent["tool_contract_version"]),
                         ("agent-v1", "agent-tools-v1"))  # fmt: skip
        self.assertEqual(agent["model_calls"], 16)
        self.assertEqual(agent["provider_metadata"], {})
        self.assertEqual(report["status"]["overall"], "checks_passed")
        page = render_html(report)
        self.assertIn("scriptet testadapter (ikke en AI-model)", page)
        self.assertNotIn("AI-planlægning", page)

    def test_failed_agent_is_visible_in_json_and_html(self) -> None:
        report = agent_report(ScriptedModelClient([ModelError("model unavailable")], name="x"))
        agent = report["run"]["agent"]
        self.assertEqual((agent["status"], agent["failure"]), ("model_failed", "model unavailable"))
        self.assertEqual(report["run"]["interrupted"], "model failure ended the agent run")
        self.assertEqual(report["status"]["overall"], "incomplete")
        self.assertEqual(agent["steps"], [{"call": 1, "action": "failed", "check_id": None,
            "rejection": None, "latency_ms": None, "provider_metadata": {}}])  # fmt: skip
        self.assertIn("modelfejl", render_html(report))
        skipped = [r for r in report["results"] if r["outcome"] == "SKIPPED"]
        self.assertTrue(all(r["requests_used_after"] is None for r in skipped))


if __name__ == "__main__":
    unittest.main()
