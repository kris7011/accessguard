"""Agent, random-order and evaluation commands run in a subprocess, as a user would."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.e2e.test_cli import run_module
from tests.integration.test_ollama_contract import fake_ollama


def load(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text("utf-8"))
    assert isinstance(document, dict)
    return document


class AgentCliTests(unittest.TestCase):
    def test_scripted_agent_run_is_labelled_and_reports_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_module(
                "accessguard_lab", "agent", "--profile", "vulnerable",
                "--model", "scripted:follow-up", "--out", tmp,
            )  # fmt: skip
            report = load(Path(tmp) / "vulnerable" / "report.json")
            page = (Path(tmp) / "vulnerable" / "report.html").read_text("utf-8")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Scripted test adapter (not an AI model)", result.stdout)
        self.assertIn("agent: completed, 56 model calls", result.stdout)
        self.assertFalse(report["run"]["planner"]["uses_ai"])
        self.assertEqual(report["summary"]["FAIL"], 23)
        self.assertIn("ikke en AI-model", page)

    def test_budget_limited_agent_run_exits_with_findings_and_partial_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_module(
                "accessguard_lab", "agent", "--profile", "fixed", "--model",
                "scripted:in-order", "--max-requests", "20", "--out", tmp,
            )  # fmt: skip
            report = load(Path(tmp) / "fixed" / "report.json")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(report["status"]["overall"], "incomplete")
        self.assertEqual(report["run"]["interrupted"], "request budget exhausted")

    def test_model_failure_exits_5_with_a_partial_report(self) -> None:
        calls: list[int] = []

        def chat(payload: dict[str, Any]) -> tuple[int, object]:
            calls.append(1)
            return 500, {"error": "model crashed"}

        with fake_ollama(chat) as (_, port), tempfile.TemporaryDirectory() as tmp:
            result = run_module(
                "accessguard_lab", "agent", "--profile", "fixed", "--model",
                "ollama:fake-model", "--ollama-port", str(port), "--out", tmp,
            )  # fmt: skip
            report = load(Path(tmp) / "fixed" / "report.json")
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        self.assertIn("no fallback was used", result.stderr)
        self.assertIn("model crashed", result.stderr)
        self.assertEqual(report["run"]["agent"]["status"], "model_failed")
        self.assertEqual(report["summary"]["SKIPPED"], 56)
        self.assertEqual(len(calls), 1)

    def test_invalid_agent_configuration_is_a_usage_error(self) -> None:
        for args in (
            ("--model", "gpt"),
            ("--model", "scripted:unknown"),
            ("--model", "ollama:bad name"),
            ("--model", "ollama:x", "--ollama-port", "0"),
            ("--model", "scripted:in-order", "--max-model-calls", "0"),
            ("--model", "scripted:in-order", "--model-timeout", "0"),
        ):
            with self.subTest(args=args):
                result = run_module("accessguard_lab", "agent", "--profile", "fixed", *args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_seeded_random_order_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_module(
                "accessguard_lab", "scan", "--profile", "vulnerable", "--order", "random",
                "--seed", "3", "--out", tmp,
            )  # fmt: skip
            report = load(Path(tmp) / "vulnerable" / "report.json")
        self.assertEqual(result.returncode, 1)
        planner = report["run"]["planner"]
        self.assertEqual(
            (planner["kind"], planner["seed"], planner["uses_ai"]), ("seeded-random", 3, False)
        )
        self.assertEqual(report["summary"]["FAIL"], 23)


class EvaluationCliTests(unittest.TestCase):
    def test_quick_evaluation_writes_results_and_labels_the_scripted_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_module(
                "accessguard_eval", "run", "--fixtures", "library,clinic", "--fractions",
                "0.5,1.0", "--seeds", "1", "--out", tmp,
            )  # fmt: skip
            results = load(Path(tmp) / "results.json")
            summary = (Path(tmp) / "summary.md").read_text("utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SCRIPTED TEST ADAPTER", summary)
        self.assertEqual(len(results["runs"]), 2 * 2 * 3)
        self.assertTrue(
            all(f["deterministic_full_budget_exact"] for f in results["meta"]["fixtures"].values())
        )

    def test_fixture_listing_and_invalid_arguments(self) -> None:
        listing = run_module("accessguard_eval", "fixtures")
        self.assertEqual(listing.stdout.split(), ["helpdesk", "clinic", "projects", "library"])
        self.assertEqual(run_module("accessguard_eval", "run", "--fixtures", "nope").returncode, 2)
        self.assertEqual(run_module("accessguard_eval", "run", "--seeds", "x").returncode, 2)


if __name__ == "__main__":
    unittest.main()
