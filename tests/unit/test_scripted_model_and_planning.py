"""Scripted test adapter (never labelled AI), seeded random planner and the model view."""

import json
import threading
import unittest

from accessguard.adapters.scripted_model import (
    SCRIPTED_LABEL,
    ScriptedModelClient,
    follow_up_responder,
    in_order_responder,
)
from accessguard.application.agent_view import Budget, ModelView
from accessguard.application.planning import DeterministicPlanner, SeededRandomPlanner
from accessguard.application.ports import ModelMessage, ModelRequest, RequestLimit
from accessguard.application.runner import SessionStatus
from accessguard.domain.checks import generate_checks
from accessguard.domain.errors import ModelError
from accessguard.domain.evaluation import Outcome, SessionState
from accessguard.domain.results import CheckResult
from tests.helpers import two_tenant_policy

POLICY = two_tenant_policy(with_collection=True)
CHECKS = generate_checks(POLICY)
BUDGET = Budget(0, 10, 0, 10, 0, 3)


def request_for(state: object) -> ModelRequest:
    return ModelRequest(messages=(ModelMessage("user", json.dumps(state)),))


class ScriptedModelTests(unittest.TestCase):
    def test_is_never_described_as_a_real_model(self) -> None:
        descriptor = ScriptedModelClient([], name="x").descriptor
        self.assertFalse(descriptor.is_real_model)
        self.assertEqual(descriptor.label, SCRIPTED_LABEL)
        self.assertIn("not an AI model", descriptor.label)
        self.assertIsNone(descriptor.model)
        self.assertEqual(dict(ScriptedModelClient([], name="x").identify(RequestLimit(1))), {})

    def test_replies_in_order_then_fails_explicitly(self) -> None:
        client = ScriptedModelClient(["a", ModelError("boom"), "b"], name="x")
        request = request_for({})
        self.assertEqual(client.complete(request, RequestLimit(1)).content, "a")
        with self.assertRaisesRegex(ModelError, "boom"):
            client.complete(request, RequestLimit(1))
        self.assertEqual(client.complete(request, RequestLimit(1)).content, "b")
        with self.assertRaisesRegex(ModelError, "exhausted"):
            client.complete(request, RequestLimit(1))
        self.assertEqual(len(client.requests), 4)

    def test_honours_cancellation_and_time_limit(self) -> None:
        cancel = threading.Event()
        cancel.set()
        client = ScriptedModelClient(["a"], name="x")
        with self.assertRaisesRegex(ModelError, "cancelled"):
            client.complete(request_for({}), RequestLimit(1, cancel))
        with self.assertRaisesRegex(ModelError, "time limit"):
            client.complete(request_for({}), RequestLimit(0))

    def test_responders_stop_when_nothing_is_pending(self) -> None:
        state = {"checks": [{"id": "c001", "status": "done", "actor": "a1"}]}
        for responder in (in_order_responder, follow_up_responder):
            with self.subTest(responder=responder.__name__):
                self.assertEqual(json.loads(responder(request_for(state))), {"action": "stop"})

    def test_follow_up_scores_resource_category_and_actor(self) -> None:
        def item(alias: str, resource: str, actor: str, **extra: object) -> dict[str, object]:
            row: dict[str, object] = {"id": alias, "kind": "item", "resource": resource}
            row.update(category="k1", relation="cross_tenant", actor=actor, status="pending")
            return {**row, **extra}

        state = {
            "checks": [
                item("c001", "r1", "a1", status="done", outcome="FAIL"),
                item("c002", "r2", "a2"),  # same category and relation: +2
                item("c003", "r1", "a3"),  # same resource +4, category +2
                item("c004", "r3", "a1", relation="owner"),  # same actor: +1
            ]
        }
        reply = json.loads(follow_up_responder(request_for(state)))
        self.assertEqual(reply, {"action": "run_check", "check_id": "c003"})
        state["checks"][0]["outcome"] = "PASS"  # no findings: listed order
        reply = json.loads(follow_up_responder(request_for(state)))
        self.assertEqual(reply["check_id"], "c002")


class SeededRandomPlannerTests(unittest.TestCase):
    def test_same_seed_same_order_and_a_permutation(self) -> None:
        first = SeededRandomPlanner(7).plan(CHECKS)
        second = SeededRandomPlanner(7).plan(CHECKS)
        self.assertEqual(first.checks, second.checks)
        self.assertEqual(sorted(c.id for c in first.checks), sorted(c.id for c in CHECKS))
        self.assertNotEqual(first.checks, SeededRandomPlanner(8).plan(CHECKS).checks)
        self.assertNotEqual(first.checks, DeterministicPlanner().plan(CHECKS).checks)

    def test_labelled_without_ai_and_with_its_seed(self) -> None:
        info = SeededRandomPlanner(3).plan(CHECKS).info
        self.assertEqual((info.kind, info.uses_ai, info.seed), ("seeded-random", False, 3))

    def test_order_is_stable_across_releases(self) -> None:
        """Guards reproducibility of published evaluation results (Mersenne Twister)."""
        order = [c.id for c in SeededRandomPlanner(1).plan(CHECKS).checks][:5]
        self.assertEqual(
            order,
            [
                "acme-doc@globex-member",
                "shared@globex-member",
                "acme-doc@acme-admin",
                "list:docs@globex-member",
                "globex-doc@globex-member",
            ],
        )


class ModelViewTests(unittest.TestCase):
    def test_aliases_are_deterministic_and_round_trip(self) -> None:
        view = ModelView(POLICY, CHECKS)
        self.assertEqual(view.aliases[:3], ("c001", "c002", "c003"))
        self.assertEqual(len(set(view.aliases)), len(CHECKS))
        for check in CHECKS:
            self.assertIs(view.check(view.alias(check)), check)
        self.assertEqual(ModelView(POLICY, CHECKS).aliases, view.aliases)

    def test_state_reports_sessions_results_and_budget(self) -> None:
        view = ModelView(POLICY, CHECKS)
        result = CheckResult(CHECKS[2], Outcome.INCONCLUSIVE, "secret reason text", False)
        sessions = {
            "acme-admin": SessionStatus(SessionState.VALID, "ok"),
            "globex-member": SessionStatus(SessionState.INVALID, "probe failed"),
        }
        state = view.state(
            results=[result], sessions=sessions, budget=BUDGET, last_rejection="invalid_json"
        )
        self.assertEqual(state["last_reply_rejected"], "invalid_json")
        self.assertEqual(state["budget"]["http_requests_max"], 10)
        sessions_by_alias = {a["id"]: a.get("session") for a in state["actors"]}
        self.assertEqual(
            sessions_by_alias, {"a0": None, "a1": "valid", "a2": "unknown", "a3": "invalid"}
        )
        row = state["checks"][2]
        self.assertEqual(
            row,
            {"id": "c003", "kind": "item", "actor": "a3", "expectation": "deny", "resource": "r1",
             "category": "k1", "owner_tenant": "t1", "relation": "cross_tenant", "status": "done",
             "step": 1, "outcome": "INCONCLUSIVE", "finding": None, "http_status": None,
             "executed": False},
        )  # fmt: skip
        self.assertNotIn("secret reason text", json.dumps(state))
        listing = state["checks"][-1]
        self.assertEqual(
            {k: listing[k] for k in ("kind", "collection", "visible_count", "hidden_count")},
            {"kind": "list", "collection": "l1", "visible_count": 0, "hidden_count": 3},
        )


if __name__ == "__main__":
    unittest.main()
