"""Opt-in test with a REAL local model. Skipped unless explicitly enabled.

Enable with an installed Ollama model, for example (PowerShell)::

    $env:ACCESSGUARD_REAL_MODEL = "llama3.2"
    py -3 -m unittest tests.e2e.test_real_model -v

Without the variable the test is reported as skipped, never as passed. It checks the
contract and the safety properties of a real-model run (no fallback, deterministic
verdicts, data minimisation, recorded provenance); it does not assert that the model
finds anything, because model quality is what the evaluation measures.
"""

import os
import unittest
from typing import Any

from accessguard.adapters.ollama_client import OLLAMA_PORT, OllamaModelClient
from accessguard.application.agent import AgentLimits
from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard.application.scan import AgentStrategy
from accessguard_lab.dataset import DOCUMENTS
from accessguard_lab.profiles import PROFILES
from accessguard_lab.scan import load_lab_policy, scan_lab

MODEL = os.environ.get("ACCESSGUARD_REAL_MODEL", "")
PORT = int(os.environ.get("ACCESSGUARD_OLLAMA_PORT", str(OLLAMA_PORT)))


@unittest.skipUnless(MODEL, "real-model test not enabled (set ACCESSGUARD_REAL_MODEL)")
class RealModelAgentTests(unittest.TestCase):
    def scan(self, planner: Any) -> dict[str, Any]:
        return scan_lab(
            profile="vulnerable",
            flaws=PROFILES["vulnerable"],
            policy=load_lab_policy("lab"),
            planner=planner,
            limits=RunLimits(max_duration_seconds=1800),
        ).report

    def test_real_model_run_keeps_every_guarantee(self) -> None:
        client = OllamaModelClient(MODEL, port=PORT)
        report = self.scan(AgentStrategy(client, AgentLimits(model_timeout_seconds=300)))
        agent = report["run"]["agent"]
        print(
            f"\nreal model {MODEL}: status {agent['status']}, {agent['model_calls']} calls, "
            f"{agent['invalid_replies']} invalid, metadata {agent['provider_metadata']}"
        )
        self.assertTrue(agent["adapter"]["is_real_model"])
        self.assertTrue(report["run"]["planner"]["uses_ai"])
        self.assertNotEqual(agent["status"], "model_failed", agent["failure"])
        baseline = {
            r["check_id"]: r["outcome"] for r in self.scan(DeterministicPlanner())["results"]
        }
        for result in report["results"]:
            if result["executed"]:
                self.assertEqual(result["outcome"], baseline[result["check_id"]])
        text = str(report)
        for document in DOCUMENTS:
            self.assertNotIn(document.canary, text)


if __name__ == "__main__":
    unittest.main()
