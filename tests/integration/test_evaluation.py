"""Evaluation harness and fixtures: answer keys hold, budgets are equal, results reproduce."""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from accessguard.adapters.scripted_model import ScriptedModelClient, follow_up_responder
from accessguard.application.agent import AgentLimits
from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard.application.ports import ModelRequest
from accessguard_eval import harness
from accessguard_eval.fixture_server import (
    FixtureApplication,
    FixtureResource,
    FixtureTarget,
    FixtureUser,
    Flaw,
)
from accessguard_eval.fixtures import (
    FixtureError,
    _target,
    fixture_ids,
    load_answer_key,
    load_fixture,
)
from accessguard_eval.harness import EvaluationConfig, evaluate, run_once, score
from tests.helpers import model_inputs

ALICE = FixtureUser("alice", "north", "staff")
BOB = FixtureUser("bob", "south", "staff")
CAROL = FixtureUser("carol", "north", "guest")
DOC = FixtureResource("north-doc", "docs", "north", frozenset({"staff"}), frozenset(), {"v": "x"})


def app(*flaws: Flaw) -> FixtureApplication:
    target = FixtureTarget("t", (ALICE, BOB, CAROL), (DOC,), flaws)
    return FixtureApplication(target, {"a": ALICE, "b": BOB, "c": CAROL})


def status(application: FixtureApplication, path: str, token: str | None) -> int:
    return application.handle(path, f"Bearer {token}" if token else None)[0]


class FixtureServerTests(unittest.TestCase):
    PATH = "/api/docs/north-doc"

    def test_correct_authorization(self) -> None:
        correct = app()
        self.assertEqual(status(correct, self.PATH, "a"), 200)
        self.assertEqual(status(correct, self.PATH, "b"), 403)
        self.assertEqual(status(correct, self.PATH, "c"), 403)
        self.assertEqual(status(correct, self.PATH, None), 401)
        self.assertEqual(correct.handle("/session", "Bearer a"), (200, {"subject": "alice"}))
        self.assertEqual(status(correct, "/session", "unknown"), 401)

    def test_each_flaw_type(self) -> None:
        self.assertEqual(status(app(Flaw("tenant_bypass", kind="docs")), self.PATH, "b"), 200)
        self.assertEqual(status(app(Flaw("tenant_bypass", kind="docs")), self.PATH, "c"), 403)
        self.assertEqual(
            status(app(Flaw("role_bypass", resource="north-doc")), self.PATH, "c"), 200
        )
        self.assertEqual(status(app(Flaw("role_bypass", kind="docs")), self.PATH, "b"), 403)
        self.assertEqual(status(app(Flaw("anonymous_read", kind="docs")), self.PATH, None), 200)
        leak = app(Flaw("denial_leak", kind="docs")).handle(self.PATH, "Bearer b")
        self.assertEqual(leak[0], 403)
        self.assertIn("resource", leak[1])
        listing = app(Flaw("list_leak", kind="docs")).handle("/api/docs", "Bearer b")
        self.assertEqual(listing, (200, {"items": [{"id": "north-doc", "v": "x"}]}))
        self.assertEqual(app().handle("/api/docs", "Bearer b"), (200, {"items": []}))
        # A flaw scoped to another kind or resource has no effect.
        self.assertEqual(status(app(Flaw("tenant_bypass", kind="other")), self.PATH, "b"), 403)


class FixtureLoadingTests(unittest.TestCase):
    def test_manifest_lists_the_four_fixtures(self) -> None:
        self.assertEqual(fixture_ids(), ("helpdesk", "clinic", "projects", "library"))

    def test_target_files_are_validated_strictly(self) -> None:
        base: dict[str, Any] = {"id": "x", "users": [], "resources": [], "flaws": []}
        for broken in (
            {**base, "extra": 1},
            {**base, "flaws": [{"type": "rce", "kind": "docs"}]},
            {**base, "flaws": [{"type": "list_leak"}]},
            {**base, "flaws": [{"type": "list_leak", "kind": "a", "resource": "b"}]},
            {**base, "users": [{"id": "u", "tenant": "t"}]},
        ):
            with self.subTest(broken=broken), self.assertRaises(FixtureError):
                _target(broken)

    def test_unknown_fixture_is_rejected(self) -> None:
        with self.assertRaises(FixtureError):
            load_fixture("../lab")
        with self.assertRaises(FixtureError):
            load_answer_key("nope")

    def test_complete_deterministic_run_reports_exactly_the_answer_key(self) -> None:
        """Validates the hand-written answer keys against the fixtures and the engine."""
        for fixture_id in fixture_ids():
            fixture = load_fixture(fixture_id)
            report = run_once(fixture, DeterministicPlanner(), RunLimits(max_requests=10_000))
            found = {
                r["check_id"] for r in report["results"] if r["outcome"] in ("FAIL", "SUSPECTED")
            }
            with self.subTest(fixture=fixture_id):
                self.assertTrue(report["run"]["completed"])
                self.assertEqual(found, load_answer_key(fixture_id))
                self.assertEqual(report["summary"]["ERROR"], 0)

    def test_fixtures_are_separate_from_the_lab(self) -> None:
        lab_ids = {"acme-doc", "globex-doc", "initech-doc", "acme-shared-brief", "acme-invoice"}
        for fixture_id in fixture_ids():
            policy = load_fixture(fixture_id).policy.policy
            with self.subTest(fixture=fixture_id):
                self.assertFalse({r.id for r in policy.resources} & lab_ids)
                self.assertNotEqual(policy.session_probe.path, "/me")


def scripted() -> ScriptedModelClient:
    return ScriptedModelClient(follow_up_responder, name="follow-up")


def small_config(**overrides: Any) -> EvaluationConfig:
    values: dict[str, Any] = {"fixtures": ("projects",), "fractions": (0.5, 1.0), "seeds": (1, 2)}
    return EvaluationConfig(**{**values, **overrides})


class HarnessTests(unittest.TestCase):
    def test_every_strategy_gets_the_same_budget(self) -> None:
        result = evaluate(small_config(), scripted)
        budgets: dict[int, set[str]] = {}
        for run in result["runs"]:
            budgets.setdefault(run["budget"], set()).add(run["strategy"])
            self.assertLessEqual(run["requests_used"], run["budget"])
        self.assertEqual(len(budgets), 2)
        for strategies in budgets.values():
            self.assertEqual(strategies, {"deterministic", "seeded-random", "agent"})
        self.assertEqual(len(result["runs"]), 2 * (1 + 2 + 1))
        meta = result["meta"]
        self.assertTrue(meta["fixtures"]["projects"]["deterministic_full_budget_exact"])
        self.assertFalse(meta["agent_adapter"]["is_real_model"])
        self.assertIn("SCRIPTED TEST ADAPTER", meta["interpretation"])
        self.assertIn("not AI quality", meta["interpretation"])

    def test_results_reproduce_except_wall_clock_times(self) -> None:
        timing = {"total_seconds", "seconds_to_first_finding"}

        def stable(result: dict[str, Any]) -> list[dict[str, Any]]:
            return [{k: v for k, v in run.items() if k not in timing} for run in result["runs"]]

        self.assertEqual(
            stable(evaluate(small_config(), scripted)), stable(evaluate(small_config(), scripted))
        )

    def test_agent_never_sees_answer_key_or_flaw_configuration(self) -> None:
        clients: list[ScriptedModelClient] = []

        def factory() -> ScriptedModelClient:
            clients.append(ScriptedModelClient(follow_up_responder, name="f"))
            return clients[-1]

        evaluate(small_config(fixtures=("projects", "helpdesk"), fractions=(1.0,)), factory)
        requests: list[ModelRequest] = [r for c in clients for r in c.requests]
        self.assertTrue(requests)
        sent = model_inputs(requests)
        forbidden = {"list_leak", "anonymous_read", "role_bypass", "denial_leak", "EVAL-CANARY"}
        for fixture_id in ("projects", "helpdesk"):
            forbidden |= set(load_answer_key(fixture_id))
            fixture = load_fixture(fixture_id)
            forbidden |= {r.id for r in fixture.target.resources} | {
                u.id for u in fixture.target.users
            }
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, sent)

    def test_answer_key_is_loaded_only_after_the_runs(self) -> None:
        loaded: list[str] = []
        keys_read_when_run_started: list[int] = []

        def recording(fixture_id: str) -> frozenset[str]:
            loaded.append(fixture_id)
            return load_answer_key(fixture_id)

        def counting_run_once(*args: Any, **kwargs: Any) -> dict[str, Any]:
            keys_read_when_run_started.append(len(loaded))
            return run_once(*args, **kwargs)

        with (
            mock.patch.object(harness, "load_answer_key", recording),
            mock.patch.object(harness, "run_once", counting_run_once),
        ):
            evaluate(small_config(), scripted)
        self.assertEqual(loaded, ["projects"])
        self.assertEqual(set(keys_read_when_run_started), {0}, "no run after reading the key")

    def test_score_counts_true_and_false_positives_and_first_finding(self) -> None:
        report = {
            "run": {
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:02+00:00",
                "requests_used": 20,
                "agent": None,
            },
            "status": {"overall": "findings"},
            "coverage": {"planned_checks": 4, "executed_checks": 4, "conclusive_ratio": 1.0},
            "results": [
                {"check_id": "a", "outcome": "PASS", "requests_used_after": 5, "observed_at": None},
                {"check_id": "b", "outcome": "FAIL", "requests_used_after": 12,
                 "observed_at": "2026-01-01T00:00:01.5+00:00"},
                {"check_id": "c", "outcome": "SUSPECTED", "requests_used_after": 9,
                 "observed_at": "2026-01-01T00:00:01+00:00"},
                {"check_id": "d", "outcome": "FAIL", "requests_used_after": 3,
                 "observed_at": "2026-01-01T00:00:00.5+00:00"},
            ],
        }  # fmt: skip
        metrics = score(
            report, frozenset({"b", "c", "x"}), fixture="f", strategy="s", seed=None, budget=20,
            fraction=1.0,
        )  # fmt: skip
        self.assertEqual((metrics.true_positives, metrics.false_positives), (2, 1))
        self.assertEqual(metrics.recall, round(2 / 3, 4))
        self.assertEqual(metrics.requests_to_first_finding, 9)
        self.assertEqual(metrics.seconds_to_first_finding, 1.0)
        self.assertEqual(metrics.findings_per_100_requests, 10.0)
        self.assertEqual(metrics.total_seconds, 2.0)


class AgentLimitsTests(unittest.TestCase):
    def test_invalid_limits_are_rejected(self) -> None:
        for kwargs in (
            {"max_model_calls": 0},
            {"max_invalid_replies": 0},
            {"max_output_chars": 8},
            {"model_timeout_seconds": 0},
            {"max_output_tokens": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                AgentLimits(**kwargs)


if __name__ == "__main__":
    unittest.main()
