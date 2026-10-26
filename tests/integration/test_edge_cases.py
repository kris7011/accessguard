"""Ambiguous target behaviour over real HTTP: none of it may turn into PASS."""

import threading
import unittest
from typing import Any

from accessguard.adapters.http_target import ScopedHttpClient
from accessguard.adapters.policy_json import parse_policy
from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard.application.scan import run_scan
from accessguard.domain.credentials import Secret
from accessguard.domain.scope import TargetScope
from accessguard_lab.server import run_lab

GRANT = [{"tenant": "acme", "roles": ["admin", "member"]}]


def edge_policy(*paths: tuple[str, str], identities: list[dict[str, str]] | None = None) -> Any:
    return parse_policy(
        {
            "schema_version": "1",
            "id": "edge-cases",
            "session_probe": {"path": "/me", "subject_field": "user"},
            "identities": identities
            or [
                {"id": "acme-member", "tenant": "acme", "role": "member"},
                {"id": "globex-member", "tenant": "globex", "role": "member"},
            ],
            "resources": [
                {
                    "id": rid,
                    "category": "document",
                    "path": path,
                    "owner": "acme-member",
                    "protected_fields": ["title", "canary"],
                    "grants": GRANT,
                }
                for rid, path in paths
            ],
        }
    )


def scan_edges(
    policy: Any,
    *,
    limits: RunLimits | None = None,
    timeout_seconds: float = 5.0,
    stop: threading.Event | None = None,
    credentials: dict[str, Secret] | None = None,
) -> dict[str, Any]:
    with run_lab() as lab:
        scope = TargetScope.from_origin(lab.origin)
        return run_scan(
            policy=policy,
            policy_sha256="0" * 64,
            client=ScopedHttpClient(scope, timeout_seconds=timeout_seconds),
            credentials=lab.credentials if credentials is None else credentials,
            scope=scope,
            target_label="edge-cases",
            planner=DeterministicPlanner(),
            limits=limits or RunLimits(),
            stop=stop,
        )


def outcome_of(report: dict[str, Any], check_id: str) -> dict[str, Any]:
    result: dict[str, Any] = next(r for r in report["results"] if r["check_id"] == check_id)
    return result


class EdgeCaseTests(unittest.TestCase):
    def test_ambiguous_responses_for_cross_tenant_actor(self) -> None:
        behaviours = {
            "soft-error": "INCONCLUSIVE",
            "login-page": "INCONCLUSIVE",
            "redirect": "INCONCLUSIVE",
            "rate-limited": "INCONCLUSIVE",
            "server-error": "INCONCLUSIVE",
            "partial": "SUSPECTED",
            "flaky": "SUSPECTED",
        }
        policy = edge_policy(*((b, f"/edge/{b}/acme-doc") for b in behaviours))
        report = scan_edges(policy)
        for behaviour, expected in behaviours.items():
            with self.subTest(behaviour=behaviour):
                result = outcome_of(report, f"{behaviour}@globex-member")
                self.assertEqual(result["outcome"], expected, result["reason"])
                self.assertEqual(outcome_of(report, f"{behaviour}@acme-member")["outcome"], "PASS")
        self.assertEqual(
            outcome_of(report, "flaky@globex-member")["verification"], "not_reproduced"
        )
        self.assertEqual(report["status"]["overall"], "needs_review")

    def test_redirect_to_metadata_endpoint_is_not_followed(self) -> None:
        report = scan_edges(edge_policy(("redirect", "/edge/redirect/acme-doc")))
        result = outcome_of(report, "redirect@globex-member")
        self.assertEqual(result["evidence"]["status"], 302)
        self.assertIn("redirect was not followed", result["reason"])

    def test_timeout_is_a_technical_error(self) -> None:
        report = scan_edges(edge_policy(("slow", "/edge/slow/acme-doc")), timeout_seconds=0.3)
        self.assertEqual(outcome_of(report, "slow@globex-member")["outcome"], "ERROR")
        self.assertEqual(report["status"]["overall"], "incomplete")

    def test_missing_resource_has_no_baseline(self) -> None:
        report = scan_edges(edge_policy(("ghost", "/documents/does-not-exist")))
        results = [r for r in report["results"] if r["resource"] == "ghost"]
        self.assertTrue(results)
        self.assertEqual({r["outcome"] for r in results}, {"INCONCLUSIVE"})
        self.assertIn("HTTP 404", results[0]["reason"])

    def test_expired_session_is_never_pass(self) -> None:
        identities = [
            {"id": "acme-member", "tenant": "acme", "role": "member"},
            {"id": "acme-expired", "tenant": "globex", "role": "member"},
        ]
        report = scan_edges(edge_policy(("doc", "/documents/acme-doc"), identities=identities))
        result = outcome_of(report, "doc@acme-expired")
        self.assertEqual(result["outcome"], "INCONCLUSIVE")
        self.assertEqual(report["sessions"]["acme-expired"]["state"], "invalid")
        self.assertTrue(any("acme-expired" in item for item in report["not_tested"]))

    def test_budget_and_stop_signal(self) -> None:
        policy = edge_policy(("doc", "/documents/acme-doc"))
        limited = scan_edges(policy, limits=RunLimits(max_requests=3))
        self.assertEqual(limited["run"]["interrupted"], "request budget exhausted")
        self.assertEqual(limited["status"]["overall"], "incomplete")
        self.assertLessEqual(limited["run"]["requests_used"], 3)
        stop = threading.Event()
        stop.set()
        stopped = scan_edges(policy, stop=stop)
        self.assertEqual(stopped["summary"]["SKIPPED"], len(stopped["results"]))
        self.assertEqual(stopped["status"]["overall"], "incomplete")


if __name__ == "__main__":
    unittest.main()
