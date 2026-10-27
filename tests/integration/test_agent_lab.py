"""Agent runs over real HTTP against the lab: same verdicts and ground truth as the baseline."""

import unittest
from typing import Any

from accessguard.adapters.scripted_model import (
    ScriptedModelClient,
    follow_up_responder,
    in_order_responder,
    list_first_responder,
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
            for responder in (in_order_responder, follow_up_responder, list_first_responder):
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

    def test_list_first_runs_listings_first_within_the_shared_budget_and_retest_policy(
        self,
    ) -> None:
        baseline = scan("vulnerable", DeterministicPlanner())["results"]
        listings = [r["check_id"] for r in baseline if r["kind"] == "list"]
        report = scan("vulnerable", agent(list_first_responder), RunLimits(max_requests=30))
        executed = [r["check_id"] for r in report["results"] if r["executed"]]
        self.assertGreater(len(executed), len(listings))
        # Every listing first, in policy order; then the item checks in policy order.
        self.assertEqual(executed[: len(listings)], listings)
        items = [r["check_id"] for r in baseline if r["kind"] == "item"]
        self.assertEqual(executed[len(listings) :], items[: len(executed) - len(listings)])
        self.assertEqual(len(executed), len(set(executed)))  # nothing is chosen twice
        # The shared budget and retest reserve apply unchanged: the budget is never exceeded,
        # and a finding made with the last affordable request stays honestly unverified.
        self.assertEqual(report["run"]["requests_used"], 30)
        self.assertEqual(report["run"]["agent"]["status"], "interrupted")
        verification = [f["verification"] for f in report["findings"]]
        self.assertEqual(verification[:-1], ["reproduced"] * (len(verification) - 1))
        self.assertIn(verification[-1], {"reproduced", "not_retested"})


if __name__ == "__main__":
    unittest.main()
