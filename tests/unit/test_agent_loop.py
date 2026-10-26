"""The bounded agent loop: one approved check at a time, deterministic verdicts, hard limits."""

import json
import threading
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from accessguard.adapters.scripted_model import (
    ScriptedModelClient,
    follow_up_responder,
    in_order_responder,
)
from accessguard.application.agent import AgentLimits, AgentRunResult, AgentStatus, StepAction
from accessguard.application.gate import InterruptReason, RequestGate, RunLimits
from accessguard.application.ports import (
    ModelDescriptor,
    ModelReply,
    ModelRequest,
    ProviderValue,
    RequestLimit,
)
from accessguard.application.runner import CheckRunner
from accessguard.application.scan import AgentStrategy, run_agent_scan
from accessguard.domain.checks import generate_checks
from accessguard.domain.errors import ModelError
from accessguard.domain.evaluation import Outcome
from accessguard.domain.results import CheckResult, VerificationStatus
from accessguard.domain.scope import TargetScope
from tests.helpers import (
    TOKENS,
    ScriptedClient,
    correct_routes,
    credentials,
    flawed_routes,
    run_agent,
    two_tenant_policy,
)

# Aliases follow generate_checks order for two_tenant_policy(with_collection=True):
# c001-c004 acme-doc@{acme-admin, acme-member, globex-member, anonymous},
# c005-c008 globex-doc@..., c009-c012 shared@..., c013-c016 list:docs@...
IDOR = "c003"  # acme-doc@globex-member: a FAIL in flawed_routes()
LIST_LEAK = "c014"  # list:docs@acme-member: a FAIL in flawed_routes()
OWNER_READ = "c001"  # acme-doc@acme-admin: always PASS


def run(alias: str) -> str:
    return json.dumps({"action": "run_check", "check_id": alias})


STOP = json.dumps({"action": "stop"})


def scripted(*replies: str | ModelError) -> ScriptedModelClient:
    return ScriptedModelClient(list(replies), name="unit-test")


def state_of(request: ModelRequest) -> dict[str, Any]:
    state = json.loads(request.messages[-1].content)
    assert isinstance(state, dict)
    return state


def by_id(result: AgentRunResult) -> dict[str, CheckResult]:
    return {r.check.id: r for r in result.outcome.results}


class FakeModel:
    """A ModelClient whose ``complete`` runs an arbitrary action (raise, set stop, ...)."""

    def __init__(self, action: Any) -> None:
        self._action = action
        self.limits: list[RequestLimit] = []

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("fake", None, False, "fake test model")

    def identify(self, limit: RequestLimit) -> Mapping[str, ProviderValue]:
        return {}

    def complete(self, request: ModelRequest, limit: RequestLimit) -> ModelReply:
        self.limits.append(limit)
        return ModelReply(self._action(request, limit), 1.0)


class AdaptiveSelectionTests(unittest.TestCase):
    @staticmethod
    def adaptive(request: ModelRequest) -> str:
        """Choose the second check from the first check's structured result."""
        done = {c["id"]: c for c in state_of(request)["checks"] if c["status"] == "done"}
        if not done:
            return run(IDOR)
        if len(done) == 1:
            return run(LIST_LEAK) if done[IDOR]["outcome"] == "FAIL" else run(OWNER_READ)
        return STOP

    def test_next_check_depends_on_the_previous_result(self) -> None:
        for routes, expected in ((flawed_routes(), LIST_LEAK), (correct_routes(), OWNER_READ)):
            model = ScriptedModelClient(self.adaptive, name="adaptive")
            result = run_agent(ScriptedClient(routes), model)
            steps = [s.check_id for s in result.trace.steps if s.action is StepAction.RUN_CHECK]
            with self.subTest(expected=expected):
                second = "list:docs@acme-member" if expected == LIST_LEAK else "acme-doc@acme-admin"
                self.assertEqual(steps, ["acme-doc@globex-member", second])
                self.assertEqual(result.trace.status, AgentStatus.STOPPED_EARLY)

    def test_model_receives_the_structured_result_of_the_previous_check(self) -> None:
        model = ScriptedModelClient(self.adaptive, name="adaptive")
        run_agent(ScriptedClient(flawed_routes()), model)
        first = {c["id"]: c for c in state_of(model.requests[0])["checks"]}
        second = {c["id"]: c for c in state_of(model.requests[1])["checks"]}
        self.assertEqual(first[IDOR]["status"], "pending")
        self.assertEqual(
            {k: second[IDOR][k] for k in ("status", "step", "outcome", "finding", "http_status")},
            {
                "status": "done",
                "step": 1,
                "outcome": "FAIL",
                "finding": "unauthorized_access",
                "http_status": 200,
            },
        )

    def test_follow_up_responder_prioritizes_the_resource_of_a_finding(self) -> None:
        model = ScriptedModelClient(follow_up_responder, name="follow-up")
        routes = correct_routes()
        routes[("/d/shared", None)] = routes[("/d/shared", TOKENS["acme-admin"])]  # anonymous leak
        result = run_agent(ScriptedClient(routes), model)
        order = [s.check_id for s in result.trace.steps if s.action is StepAction.RUN_CHECK]
        # In-order until the first finding (shared@anonymous, c012), then the same resource
        # first and the same actor next: the anonymous listing (c016).
        self.assertEqual(
            order[: order.index("shared@anonymous") + 1],
            [c.id for c in generate_checks(two_tenant_policy(with_collection=True))][:12],
        )
        self.assertEqual(order[12], "list:docs@anonymous")
        self.assertEqual(result.trace.status, AgentStatus.COMPLETED)


class VerdictEquivalenceTests(unittest.TestCase):
    """The same observations and policy give the same verdict with and without the agent."""

    FIELDS = ("outcome", "reason", "finding_type", "evidence", "verification", "executed")

    def verdicts(self, results: tuple[CheckResult, ...]) -> dict[str, tuple[object, ...]]:
        return {r.check.id: tuple(getattr(r, f) for f in self.FIELDS) for r in results}

    def test_agent_and_deterministic_run_agree_on_every_verdict(self) -> None:
        policy = two_tenant_policy(with_collection=True)
        runner = CheckRunner(
            policy=policy,
            client=ScriptedClient(flawed_routes()),
            credentials=credentials(),
            gate=RequestGate(RunLimits()),
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        baseline = self.verdicts(runner.run(generate_checks(policy)).results)
        self.assertIn(Outcome.FAIL, {v[0] for v in baseline.values()})
        for responder in (in_order_responder, follow_up_responder):
            model = ScriptedModelClient(responder, name=responder.__name__)
            agent = run_agent(ScriptedClient(flawed_routes()), model)
            with self.subTest(responder=responder.__name__):
                self.assertEqual(self.verdicts(agent.outcome.results), baseline)
                self.assertEqual(agent.trace.status, AgentStatus.COMPLETED)


class InvalidReplyTests(unittest.TestCase):
    def test_invalid_replies_are_rejected_with_feedback_and_never_executed(self) -> None:
        model = scripted(
            "not json",
            json.dumps({"action": "run_check", "check_id": OWNER_READ, "path": "/admin"}),
            json.dumps({"action": "fetch_url", "url": "http://169.254.169.254/"}),
            run("c999"),
            run(OWNER_READ),
            run(OWNER_READ),
            STOP,
        )
        client = ScriptedClient(correct_routes())
        result = run_agent(client, model, agent_limits=AgentLimits(max_invalid_replies=10))
        self.assertEqual(
            [(s.action.value, s.rejection) for s in result.trace.steps],
            [
                ("rejected", "invalid_json"),
                ("rejected", "unexpected_fields"),
                ("rejected", "unknown_action"),
                ("rejected", "unknown_check_id"),
                ("run_check", None),
                ("rejected", "already_executed"),
                ("stop", None),
            ],
        )
        feedback = [state_of(r)["last_reply_rejected"] for r in model.requests]
        self.assertEqual(
            feedback,
            [
                None,
                "invalid_json",
                "unexpected_fields",
                "unknown_action",
                "unknown_check_id",
                None,
                "already_executed",
            ],
        )
        # The repeated check was executed exactly once.
        self.assertEqual(client.calls.count(("/d/acme", TOKENS["acme-admin"])), 1)
        self.assertEqual(result.trace.invalid_replies, 5)

    def test_too_many_invalid_replies_end_the_run_explicitly(self) -> None:
        model = scripted(run(IDOR), "{", '{"action": "stop", "x": 1}', run(IDOR))
        result = run_agent(ScriptedClient(flawed_routes()), model)
        self.assertEqual(result.trace.status, AgentStatus.INVALID_REPLIES_EXHAUSTED)
        self.assertIn("3 invalid model replies", result.trace.failure or "")
        self.assertEqual(result.outcome.interrupted, InterruptReason.INVALID_REPLIES_EXHAUSTED)
        results = by_id(result)
        self.assertEqual(results["acme-doc@globex-member"].outcome, Outcome.FAIL)
        skipped = [r for r in result.outcome.results if r.outcome is Outcome.SKIPPED]
        self.assertEqual(len(skipped), 15)
        self.assertTrue(all("too many invalid model replies" in r.reason for r in skipped))


class StopAndBudgetTests(unittest.TestCase):
    def test_early_stop_marks_remaining_checks_as_not_executed(self) -> None:
        result = run_agent(ScriptedClient(flawed_routes()), scripted(run(IDOR), STOP))
        self.assertEqual(result.trace.status, AgentStatus.STOPPED_EARLY)
        self.assertEqual(result.outcome.interrupted, InterruptReason.AGENT_STOPPED)
        outcomes = [r.outcome for r in result.outcome.results]
        self.assertEqual(outcomes[0], Outcome.FAIL)
        self.assertEqual(outcomes[1:], [Outcome.SKIPPED] * 15)
        skipped = result.outcome.results[1]
        self.assertFalse(skipped.executed)
        self.assertEqual(
            skipped.reason, "Not executed: agent stopped before every check was executed"
        )

    def test_early_stop_report_never_shows_full_coverage(self) -> None:
        report = run_agent_scan(
            policy=two_tenant_policy(with_collection=True),
            policy_sha256="0" * 64,
            client=ScriptedClient(correct_routes()),
            credentials=credentials(),
            scope=TargetScope.from_origin("http://127.0.0.1:9"),
            target_label="unit-test",
            agent=AgentStrategy(scripted(run(OWNER_READ), STOP), AgentLimits()),
            limits=RunLimits(),
        )
        self.assertEqual(report["status"]["overall"], "incomplete")
        self.assertFalse(report["status"]["complete"])
        self.assertFalse(report["run"]["completed"])
        self.assertEqual(report["coverage"]["executed_checks"], 1)
        self.assertLess(report["coverage"]["conclusive_ratio"], 1.0)
        self.assertIn("15 planned checks did not complete", report["status"]["meaning"])
        self.assertEqual(report["run"]["agent"]["status"], "stopped_early")

    def test_model_call_budget(self) -> None:
        model = ScriptedModelClient(in_order_responder, name="in-order")
        result = run_agent(
            ScriptedClient(correct_routes()), model, agent_limits=AgentLimits(max_model_calls=2)
        )
        self.assertEqual(result.trace.status, AgentStatus.MODEL_CALLS_EXHAUSTED)
        self.assertEqual(result.trace.model_calls, 2)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(sum(r.executed for r in result.outcome.results), 2)
        self.assertEqual(result.outcome.interrupted, InterruptReason.MODEL_CALLS_EXHAUSTED)

    def test_http_budget_ends_the_run_and_counts_probes_and_baselines(self) -> None:
        model = ScriptedModelClient(in_order_responder, name="in-order")
        # 3 session probes + 1 baseline + 1 check = 5; the second check cannot start.
        result = run_agent(
            ScriptedClient(correct_routes()), model, limits=RunLimits(max_requests=5)
        )
        self.assertEqual(result.trace.status, AgentStatus.INTERRUPTED)
        self.assertEqual(result.outcome.interrupted, InterruptReason.BUDGET_EXHAUSTED)
        self.assertEqual(result.outcome.requests_used, 5)
        results = result.outcome.results
        self.assertEqual(results[0].outcome, Outcome.PASS)
        self.assertIn("Interrupted while in progress", results[1].reason)
        self.assertTrue(all(r.outcome is Outcome.SKIPPED for r in results[1:]))

    def test_retests_count_against_the_http_budget(self) -> None:
        full = run_agent(ScriptedClient(flawed_routes()), scripted(run(IDOR), STOP))
        # 3 probes + 1 baseline + 1 check + 1 retest
        self.assertEqual(full.outcome.requests_used, 6)
        self.assertEqual(full.outcome.requests_used_after["acme-doc@globex-member"], 5)
        self.assertEqual(
            by_id(full)["acme-doc@globex-member"].verification, VerificationStatus.REPRODUCED
        )
        short = run_agent(
            ScriptedClient(flawed_routes()),
            scripted(run(IDOR), STOP),
            limits=RunLimits(max_requests=5),
        )
        self.assertEqual(short.outcome.requests_used, 5)
        self.assertEqual(
            by_id(short)["acme-doc@globex-member"].verification, VerificationStatus.NOT_RETESTED
        )
        self.assertEqual(short.outcome.interrupted, InterruptReason.AGENT_STOPPED)


class ModelFailureTests(unittest.TestCase):
    def test_model_error_keeps_partial_results_without_fallback(self) -> None:
        client = ScriptedClient(flawed_routes())
        result = run_agent(client, scripted(run(IDOR), ModelError("connection refused")))
        self.assertEqual(result.trace.status, AgentStatus.MODEL_FAILED)
        self.assertEqual(result.trace.failure, "connection refused")
        self.assertEqual(result.outcome.interrupted, InterruptReason.MODEL_FAILED)
        self.assertEqual(
            [s.action for s in result.trace.steps], [StepAction.RUN_CHECK, StepAction.FAILED]
        )
        failure = by_id(result)["acme-doc@globex-member"]
        self.assertEqual(failure.outcome, Outcome.FAIL)
        self.assertEqual(failure.verification, VerificationStatus.REPRODUCED)
        self.assertEqual(sum(r.outcome is Outcome.SKIPPED for r in result.outcome.results), 15)
        # No fallback: nothing but probes, the baseline, the check and its retest was sent.
        actor_calls = {c for c in client.calls if c[0] != "/me"}
        self.assertEqual(
            actor_calls,
            {("/d/acme", TOKENS["acme-member"]), ("/d/acme", TOKENS["globex-member"])},
        )

    def test_exhausted_script_is_a_model_failure(self) -> None:
        result = run_agent(ScriptedClient(correct_routes()), scripted(run(OWNER_READ)))
        self.assertEqual(result.trace.status, AgentStatus.MODEL_FAILED)
        self.assertIn("exhausted", result.trace.failure or "")

    def test_model_timeout_is_a_model_failure_not_an_interruption(self) -> None:
        def timeout(request: ModelRequest, limit: RequestLimit) -> str:
            raise ModelError("Request exceeded its time limit")

        result = run_agent(ScriptedClient(correct_routes()), FakeModel(timeout))
        self.assertEqual(result.trace.status, AgentStatus.MODEL_FAILED)
        self.assertEqual(result.outcome.interrupted, InterruptReason.MODEL_FAILED)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class DeadlineAndCancellationTests(unittest.TestCase):
    def test_model_calls_are_bounded_by_the_run_deadline(self) -> None:
        clock = FakeClock()
        limits = RunLimits(max_duration_seconds=10.0)
        gate = RequestGate(limits, clock=clock)

        def slow(request: ModelRequest, limit: RequestLimit) -> str:
            clock.now += 4.0
            return run(OWNER_READ) if len(model.limits) == 1 else STOP

        model = FakeModel(slow)
        run_agent(
            ScriptedClient(correct_routes()),
            model,
            limits=limits,
            gate=gate,
            agent_limits=AgentLimits(model_timeout_seconds=60.0),
        )
        self.assertEqual([round(lim.seconds, 3) for lim in model.limits], [10.0, 6.0])
        clock.now = 100.0
        short = FakeModel(lambda request, limit: STOP)
        run_agent(
            ScriptedClient(correct_routes()),
            short,
            limits=limits,
            gate=RequestGate(limits, clock=clock),
            agent_limits=AgentLimits(model_timeout_seconds=2.0),
        )
        self.assertEqual(short.limits[0].seconds, 2.0)

    def test_model_error_after_the_deadline_is_an_interruption(self) -> None:
        clock = FakeClock()
        limits = RunLimits(max_duration_seconds=10.0)

        def overrun(request: ModelRequest, limit: RequestLimit) -> str:
            clock.now += 11.0
            raise ModelError("Request exceeded its time limit")

        result = run_agent(
            ScriptedClient(correct_routes()),
            FakeModel(overrun),
            limits=limits,
            gate=RequestGate(limits, clock=clock),
        )
        self.assertEqual(result.trace.status, AgentStatus.INTERRUPTED)
        self.assertEqual(result.outcome.interrupted, InterruptReason.DEADLINE_EXCEEDED)
        self.assertEqual(result.trace.steps[-1].action, StepAction.INTERRUPTED)

    def test_cancellation_during_a_model_call_is_an_interruption(self) -> None:
        stop = threading.Event()

        def cancelled(request: ModelRequest, limit: RequestLimit) -> str:
            self.assertIs(limit.cancel, stop, "the model call must see the run's stop signal")
            stop.set()
            raise ModelError("Request cancelled")

        result = run_agent(ScriptedClient(correct_routes()), FakeModel(cancelled), stop=stop)
        self.assertEqual(result.trace.status, AgentStatus.INTERRUPTED)
        self.assertEqual(result.outcome.interrupted, InterruptReason.CANCELLED)
        self.assertEqual({r.outcome for r in result.outcome.results}, {Outcome.SKIPPED})

    def test_stop_before_the_run_makes_no_model_call_and_no_request(self) -> None:
        stop = threading.Event()
        stop.set()
        model = scripted(STOP)
        client = ScriptedClient(correct_routes())
        result = run_agent(client, model, stop=stop)
        self.assertEqual(result.outcome.interrupted, InterruptReason.CANCELLED)
        self.assertEqual(model.requests, [])
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
