import threading
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime

from accessguard.application.gate import InterruptReason, RequestGate, RunLimits
from accessguard.application.runner import CheckRunner, RunOutcome
from accessguard.domain.checks import generate_checks
from accessguard.domain.credentials import Secret
from accessguard.domain.errors import TransportError
from accessguard.domain.evaluation import FindingType, Outcome, SessionState
from accessguard.domain.policy import AccessPolicy
from accessguard.domain.results import CheckResult, VerificationStatus
from tests.helpers import (
    DOC_BODIES,
    TOKENS,
    ScriptedClient,
    correct_routes,
    credentials,
    observation,
    two_tenant_policy,
)

FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def run(
    client: ScriptedClient,
    *,
    policy: AccessPolicy | None = None,
    creds: Mapping[str, Secret] | None = None,
    limits: RunLimits | None = None,
    stop: threading.Event | None = None,
) -> RunOutcome:
    policy = policy or two_tenant_policy(with_collection=True)
    limits = limits or RunLimits()
    runner = CheckRunner(
        policy=policy,
        client=client,
        credentials=credentials() if creds is None else creds,
        gate=RequestGate(limits, stop),
        now=lambda: FIXED_TIME,
        retest_findings=limits.retest_findings,
    )
    return runner.run(generate_checks(policy))


def by_id(outcome: RunOutcome) -> dict[str, CheckResult]:
    return {result.check.id: result for result in outcome.results}


class RunnerTests(unittest.TestCase):
    def test_correct_target_passes_every_check(self) -> None:
        outcome = run(ScriptedClient(correct_routes()))
        self.assertEqual({r.outcome for r in outcome.results}, {Outcome.PASS})
        self.assertIsNone(outcome.interrupted)
        self.assertTrue(all(s.state is SessionState.VALID for s in outcome.sessions.values()))

    def test_baselines_are_fetched_once_per_resource(self) -> None:
        client = ScriptedClient(correct_routes())
        run(client)
        owner_fetch = ("/d/acme", TOKENS["acme-member"])
        # once for the baseline, once as the owner's own item check
        self.assertEqual(client.calls.count(owner_fetch), 2)

    def test_reproduced_failure_is_verified(self) -> None:
        routes = correct_routes()
        routes[("/d/acme", TOKENS["globex-member"])] = observation(200, DOC_BODIES["acme-doc"])
        result = by_id(run(ScriptedClient(routes)))["acme-doc@globex-member"]
        self.assertEqual(result.outcome, Outcome.FAIL)
        self.assertEqual(result.finding_type, FindingType.UNAUTHORIZED_ACCESS)
        self.assertEqual(result.verification, VerificationStatus.REPRODUCED)
        assert result.evidence is not None
        self.assertEqual(result.evidence.matches[0].matched_fields, ("title", "canary"))

    def test_failure_that_does_not_reproduce_is_downgraded(self) -> None:
        routes = correct_routes()
        routes[("/d/acme", TOKENS["globex-member"])] = [
            observation(200, DOC_BODIES["acme-doc"]),
            observation(403, {}),
        ]
        result = by_id(run(ScriptedClient(routes)))["acme-doc@globex-member"]
        self.assertEqual(result.outcome, Outcome.SUSPECTED)
        self.assertEqual(result.verification, VerificationStatus.NOT_REPRODUCED)
        self.assertIn("did not reproduce", result.reason)

    def test_retest_can_be_disabled(self) -> None:
        routes = correct_routes()
        routes[("/d/acme", TOKENS["globex-member"])] = observation(200, DOC_BODIES["acme-doc"])
        outcome = run(ScriptedClient(routes), limits=RunLimits(retest_findings=False))
        result = by_id(outcome)["acme-doc@globex-member"]
        self.assertEqual(result.verification, VerificationStatus.NOT_RETESTED)

    def test_transport_error_is_error_not_pass(self) -> None:
        routes = correct_routes()
        routes[("/d/acme", TOKENS["globex-member"])] = TransportError("timed out")
        result = by_id(run(ScriptedClient(routes)))["acme-doc@globex-member"]
        self.assertEqual(result.outcome, Outcome.ERROR)
        self.assertTrue(result.executed)

    def test_missing_credential_makes_actor_checks_inconclusive(self) -> None:
        creds = credentials(["acme-admin", "acme-member"])
        outcome = run(ScriptedClient(correct_routes()), creds=creds)
        results = by_id(outcome)
        self.assertEqual(outcome.sessions["globex-member"].state, SessionState.INVALID)
        self.assertEqual(results["acme-doc@globex-member"].outcome, Outcome.INCONCLUSIVE)
        self.assertFalse(results["acme-doc@globex-member"].executed)
        # globex-member owns globex-doc, so its baseline is unavailable too
        self.assertEqual(results["globex-doc@acme-admin"].outcome, Outcome.INCONCLUSIVE)

    def test_session_expiring_mid_run_is_not_pass(self) -> None:
        routes = correct_routes()
        token = TOKENS["globex-member"]
        routes[("/me", token)] = [observation(200, {"user": "globex-member"}), observation(401, {})]
        routes[("/d/acme", token)] = observation(401, {})
        result = by_id(run(ScriptedClient(routes)))["acme-doc@globex-member"]
        self.assertEqual(result.outcome, Outcome.INCONCLUSIVE)
        self.assertIn("session", result.reason)

    def test_401_with_still_valid_session_counts_as_denial(self) -> None:
        routes = correct_routes()
        routes[("/d/acme", TOKENS["globex-member"])] = observation(401, {})
        result = by_id(run(ScriptedClient(routes)))["acme-doc@globex-member"]
        self.assertEqual(result.outcome, Outcome.PASS)

    def test_session_probe_must_echo_the_subject(self) -> None:
        routes = correct_routes()
        routes[("/me", TOKENS["globex-member"])] = observation(200, {"user": "acme-admin"})
        outcome = run(ScriptedClient(routes))
        self.assertEqual(outcome.sessions["globex-member"].state, SessionState.INVALID)

    def test_budget_exhaustion_skips_remaining_checks(self) -> None:
        outcome = run(ScriptedClient(correct_routes()), limits=RunLimits(max_requests=8))
        outcomes = [r.outcome for r in outcome.results]
        self.assertEqual(outcome.interrupted, InterruptReason.BUDGET_EXHAUSTED)
        self.assertEqual(outcome.requests_used, 8)
        self.assertIn(Outcome.SKIPPED, outcomes)
        self.assertEqual(
            len(outcomes), len(generate_checks(two_tenant_policy(with_collection=True)))
        )
        self.assertTrue(
            all(o is Outcome.SKIPPED for o in outcomes[outcomes.index(Outcome.SKIPPED) :])
        )

    def test_stop_signal_skips_everything(self) -> None:
        stop = threading.Event()
        stop.set()
        outcome = run(ScriptedClient(correct_routes()), stop=stop)
        self.assertEqual(outcome.interrupted, InterruptReason.CANCELLED)
        self.assertEqual({r.outcome for r in outcome.results}, {Outcome.SKIPPED})
        self.assertEqual(outcome.requests_used, 0)


if __name__ == "__main__":
    unittest.main()
