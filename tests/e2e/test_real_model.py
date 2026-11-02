"""Opt-in tests with a REAL local model. Skipped unless explicitly enabled.

Enable with an installed Ollama model, for example (PowerShell)::

    $env:ACCESSGUARD_REAL_MODEL = "<model name>"
    py -3 -m unittest tests.e2e.test_real_model -v

Without the variable both tests are reported as skipped, never as passed. They run the
adaptive agent loop (not the reorder planner) against the synthetic lab and check the
contract and the safety properties of a real-model run: no fallback, at least one valid
model choice, deterministic verdicts, data minimisation and recorded provenance. They do
not assert that the model finds anything, because model quality is what the evaluation
measures.

The smoke test is small and time-bounded (3 model calls, 120 s per call, 600 s per run);
run it first. The full test lets the model select every check (up to 1800 s).
"""

import os
import unittest
from typing import Any

from accessguard.adapters.ollama_client import OLLAMA_PORT, OllamaModelClient
from accessguard.application.agent import FAILED_STATUSES, AgentLimits, AgentStatus
from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard.application.ports import Planner
from accessguard.application.scan import AgentStrategy
from accessguard_lab.dataset import DOCUMENTS
from accessguard_lab.profiles import PROFILES
from accessguard_lab.scan import load_lab_policy, scan_lab

MODEL = os.environ.get("ACCESSGUARD_REAL_MODEL", "")
PORT = int(os.environ.get("ACCESSGUARD_OLLAMA_PORT", str(OLLAMA_PORT)))


@unittest.skipUnless(MODEL, "real-model test not enabled (set ACCESSGUARD_REAL_MODEL)")
class RealModelAgentTests(unittest.TestCase):
    def scan(self, planner: Planner | AgentStrategy, max_seconds: float) -> dict[str, Any]:
        return scan_lab(
            profile="vulnerable",
            flaws=PROFILES["vulnerable"],
            policy=load_lab_policy("lab"),
            planner=planner,
            limits=RunLimits(max_duration_seconds=max_seconds),
        ).report

    def agent_scan(self, limits: AgentLimits, max_seconds: float) -> dict[str, Any]:
        report = self.scan(AgentStrategy(OllamaModelClient(MODEL, port=PORT), limits), max_seconds)
        agent = report["run"]["agent"]
        print(
            f"\nreal model {MODEL}: status {agent['status']}, {agent['model_calls']} calls, "
            f"{agent['invalid_replies']} invalid, {agent['model_latency_ms_total']} ms model "
            f"time, metadata {agent['provider_metadata']}, "
            f"metadata error {agent['provider_metadata_error']}"
        )
        return report

    def assert_real_model_contract(self, report: dict[str, Any]) -> None:
        agent = report["run"]["agent"]
        self.assertTrue(agent["adapter"]["is_real_model"])
        self.assertTrue(report["run"]["planner"]["uses_ai"])
        # invalid_replies_exhausted is a failure too: the model never worked as required.
        self.assertNotIn(AgentStatus(agent["status"]), FAILED_STATUSES, agent["failure"])
        accepted = [step for step in agent["steps"] if step["action"] == "run_check"]
        self.assertTrue(accepted, "the model never made a valid run_check choice")
        text = str(report)
        for document in DOCUMENTS:
            self.assertNotIn(document.canary, text)

    def test_smoke_a_few_bounded_model_calls(self) -> None:
        limits = AgentLimits(max_model_calls=3, model_timeout_seconds=120)
        report = self.agent_scan(limits, max_seconds=600)
        self.assert_real_model_contract(report)
        self.assertLessEqual(report["run"]["agent"]["model_calls"], 3)

    def test_real_model_run_keeps_every_guarantee(self) -> None:
        report = self.agent_scan(AgentLimits(model_timeout_seconds=300), max_seconds=1800)
        self.assert_real_model_contract(report)
        baseline = {
            r["check_id"]: r["outcome"]
            for r in self.scan(DeterministicPlanner(), max_seconds=1800)["results"]
        }
        for result in report["results"]:
            if result["executed"]:
                self.assertEqual(result["outcome"], baseline[result["check_id"]])


if __name__ == "__main__":
    unittest.main()
