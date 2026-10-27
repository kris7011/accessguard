"""Agent runs over real HTTP against the lab: same verdicts and ground truth as the baseline."""

import unittest
from typing import Any

from accessguard.adapters.scripted_model import (
    ScriptedModelClient,
    follow_up_responder,
    in_order_responder,
)
from accessguard.application.agent import AgentLimits
from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard.application.scan import AgentStrategy
from accessguard_lab import ground_truth
from accessguard_lab.profiles import PROFILES
from accessguard_lab.scan import load_lab_policy, scan_lab
from accessguard_lab.server import Flaw

VERDICT_FIELDS = ("outcome", "finding_type", "verification", "executed")


def scan(profile: str, planner: Any, limits: RunLimits | None = None) -> dict[str, Any]:
    return scan_lab(
        profile=profile,
        flaws=PROFILES[profile],
        policy=load_lab_policy("lab"),
        planner=planner,
        limits=limits or RunLimits(),
    ).report


def agent(responder: Any = follow_up_responder, **limits: Any) -> AgentStrategy:
    return AgentStrategy(ScriptedModelClient(responder, name="test"), AgentLimits(**limits))


def verdicts(report: dict[str, Any]) -> dict[str, tuple[Any, ...]]:
    return {r["check_id"]: tuple(r[f] for f in VERDICT_FIELDS) for r in report["results"]}


class AgentLabTests(unittest.TestCase):
    def test_agent_verdicts_equal_the_baseline_for_every_profile(self) -> None:
        for profile in (
            "vulnerable",
            "fixed",
            Flaw.DENIAL_BODY_LEAK.value,
            Flaw.SHARE_IGNORED.value,
        ):
            baseline = scan(profile, DeterministicPlanner())
            for responder in (in_order_responder, follow_up_responder):
                report = scan(profile, agent(responder))
                with self.subTest(profile=profile, responder=responder.__name__):
                    self.assertEqual(verdicts(report), verdicts(baseline))
                    self.assertEqual(report["status"], baseline["status"])
                    self.assertTrue(ground_truth.compare(report, PROFILES[profile]).exact)
                    self.assertEqual(report["run"]["agent"]["status"], "completed")
                    self.assertEqual(
                        report["run"]["requests_used"], baseline["run"]["requests_used"]
                    )

    def test_in_order_script_reproduces_the_baseline_order(self) -> None:
        baseline = scan("vulnerable", DeterministicPlanner())
        report = scan("vulnerable", agent(in_order_responder))
        self.assertEqual(
            [r["check_id"] for r in report["results"]],
            [r["check_id"] for r in baseline["results"]],
        )

    def test_budget_limited_agent_run_shows_partial_coverage(self) -> None:
        report = scan("vulnerable", agent(), RunLimits(max_requests=30))
        self.assertEqual(report["run"]["requests_used"], 30)
        self.assertEqual(report["run"]["interrupted"], "request budget exhausted")
        self.assertEqual(report["run"]["agent"]["status"], "interrupted")
        self.assertFalse(report["status"]["complete"])
        self.assertLess(report["coverage"]["conclusive_ratio"], 1.0)
        self.assertGreater(report["summary"]["SKIPPED"], 0)
        # Findings that were made are kept, and the report never claims a pass.
        self.assertEqual(report["status"]["overall"], "findings")
        self.assertTrue(report["findings"])
        # The per-finding retest reserve confirms the findings within the same budget: the
        # run is budget-limited (partial coverage) yet every finding is reproduced, never
        # left unverified and never presented as a pass. The total still respects the budget.
        self.assertTrue(all(f["verification"] == "reproduced" for f in report["findings"]))
        self.assertLessEqual(report["run"]["requests_used"], 30)


if __name__ == "__main__":
    unittest.main()
