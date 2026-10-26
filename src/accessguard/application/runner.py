"""Executes planned checks: session preflight, owner baselines, observation, verdict, retest."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from accessguard.application.gate import InterruptReason, RequestGate, RunInterrupted
from accessguard.application.ports import TargetClient
from accessguard.domain.checks import Check, ItemCheck
from accessguard.domain.credentials import Secret
from accessguard.domain.errors import ScopeViolationError, TransportError
from accessguard.domain.evaluation import (
    BaselineResult,
    BaselineUnavailable,
    Outcome,
    SessionState,
    Verdict,
    establish_baseline,
    evaluate_item,
    evaluate_list,
)
from accessguard.domain.observation import HttpObservation
from accessguard.domain.policy import ANONYMOUS, AccessPolicy, Resource
from accessguard.domain.results import CheckResult, Evidence, VerificationStatus


@dataclass(frozen=True)
class SessionStatus:
    state: SessionState
    reason: str


@dataclass(frozen=True)
class RunOutcome:
    results: tuple[CheckResult, ...]
    sessions: Mapping[str, SessionStatus]
    requests_used: int
    interrupted: InterruptReason | None


_NO_SESSION = SessionStatus(SessionState.NOT_APPLICABLE, "anonymous actor")


class CheckRunner:
    """Runs one scan. Create a new instance per run; it caches sessions and baselines.

    Requests are sent strictly sequentially through the :class:`RequestGate`.
    """

    def __init__(
        self,
        *,
        policy: AccessPolicy,
        client: TargetClient,
        credentials: Mapping[str, Secret],
        gate: RequestGate,
        now: Callable[[], datetime],
        retest_findings: bool = True,
    ) -> None:
        self._policy = policy
        self._client = client
        self._credentials = credentials
        self._gate = gate
        self._now = now
        self._retest_findings = retest_findings
        self._sessions: dict[str, SessionStatus] = {}
        self._baselines: dict[str, BaselineResult] = {}

    def run(self, checks: Sequence[Check]) -> RunOutcome:
        interrupted: InterruptReason | None = None
        try:
            for identity in self._policy.identities:
                self._sessions[identity.id] = self._probe_session(identity.id)
        except RunInterrupted as stop:
            interrupted = stop.reason

        results: list[CheckResult] = []
        for check in checks:
            if interrupted is None:
                try:
                    results.append(self._execute(check))
                    continue
                except RunInterrupted as stop:
                    interrupted = stop.reason
            results.append(_skipped(check, interrupted))

        verified, interrupted = self._verify_failures(results, interrupted)
        return RunOutcome(tuple(verified), dict(self._sessions), self._gate.used, interrupted)

    def _verify_failures(
        self, results: list[CheckResult], interrupted: InterruptReason | None
    ) -> tuple[list[CheckResult], InterruptReason | None]:
        """Retest every FAIL separately with the same immutable check definition."""
        verified: list[CheckResult] = []
        for result in results:
            if result.outcome is not Outcome.FAIL:
                verified.append(result)
                continue
            if interrupted is not None or not self._retest_findings:
                verified.append(replace(result, verification=VerificationStatus.NOT_RETESTED))
                continue
            try:
                retest = self._execute(result.check)
            except RunInterrupted as stop:
                interrupted = stop.reason
                verified.append(replace(result, verification=VerificationStatus.NOT_RETESTED))
                continue
            if retest.outcome is Outcome.FAIL:
                verified.append(replace(result, verification=VerificationStatus.REPRODUCED))
            else:
                verified.append(
                    replace(
                        result,
                        outcome=Outcome.SUSPECTED,
                        reason=(
                            f"{result.reason}. The separate retest did not reproduce it "
                            f"({retest.outcome.value}: {retest.reason})"
                        ),
                        verification=VerificationStatus.NOT_REPRODUCED,
                    )
                )
        return verified, interrupted

    def _execute(self, check: Check) -> CheckResult:
        actor_session = self._session_of(check.actor)
        if actor_session.state is SessionState.INVALID:
            return CheckResult(
                check,
                Outcome.INCONCLUSIVE,
                f"Actor session could not be verified: {actor_session.reason}",
                executed=False,
            )
        baselines: dict[str, BaselineResult]
        if isinstance(check, ItemCheck):
            resource = self._policy.resource(check.resource)
            baseline = self._baseline(resource)
            if isinstance(baseline, BaselineUnavailable):
                return CheckResult(
                    check,
                    Outcome.INCONCLUSIVE,
                    f"Owner baseline unavailable: {baseline.reason}",
                    executed=False,
                )
            path = resource.path
            baselines = {resource.id: baseline}
        else:
            path = self._policy.collection(check.collection).path
            baselines = {
                rid: self._baseline(self._policy.resource(rid))
                for rid in (*check.visible, *check.hidden)
            }

        observed_at = self._now()
        try:
            observation = self._send(path, check.actor)
        except (TransportError, ScopeViolationError) as error:
            return CheckResult(
                check, Outcome.ERROR, f"Request failed: {error}", True, observed_at=observed_at
            )
        session = self._session_after(observation, check.actor)
        verdict: Verdict
        if isinstance(check, ItemCheck):
            verdict = evaluate_item(
                check.expectation, baselines[check.resource], observation, session
            )
        else:
            verdict = evaluate_list(check, baselines, observation, session)
        evidence = Evidence(
            method="GET",
            path=path,
            status=observation.status,
            content_type=observation.content_type,
            size=observation.size,
            body_sha256=observation.body_sha256,
            matches=verdict.matches,
        )
        return CheckResult(
            check,
            verdict.outcome,
            verdict.reason,
            executed=True,
            observed_at=observed_at,
            evidence=evidence,
            finding_type=verdict.finding_type,
        )

    def _send(self, path: str, actor: str) -> HttpObservation:
        self._gate.acquire()
        credential = None if actor == ANONYMOUS else self._credentials[actor]
        return self._client.get(path, credential)

    def _session_of(self, actor: str) -> SessionStatus:
        if actor == ANONYMOUS:
            return _NO_SESSION
        return self._sessions[actor]

    def _session_after(self, observation: HttpObservation, actor: str) -> SessionState:
        """Re-verify the actor's session when the target answers 401."""
        if actor == ANONYMOUS:
            return SessionState.NOT_APPLICABLE
        if observation.status != 401:
            return SessionState.VALID
        status = self._probe_session(actor)
        self._sessions[actor] = status
        return status.state

    def _probe_session(self, identity_id: str) -> SessionStatus:
        if identity_id not in self._credentials:
            return SessionStatus(SessionState.INVALID, "no credential supplied for this identity")
        probe = self._policy.session_probe
        try:
            observation = self._send(probe.path, identity_id)
        except (TransportError, ScopeViolationError) as error:
            return SessionStatus(SessionState.INVALID, f"session probe failed: {error}")
        body = observation.json_body
        if (
            observation.status == 200
            and isinstance(body, dict)
            and body.get(probe.subject_field) == identity_id
        ):
            return SessionStatus(SessionState.VALID, "session probe confirmed the subject")
        return SessionStatus(
            SessionState.INVALID,
            f"session probe returned HTTP {observation.status} without the expected subject",
        )

    def _baseline(self, resource: Resource) -> BaselineResult:
        cached = self._baselines.get(resource.id)
        if cached is not None:
            return cached
        owner_session = self._session_of(resource.owner)
        result: BaselineResult
        if owner_session.state is not SessionState.VALID:
            result = BaselineUnavailable(
                resource.id, f"owner session could not be verified: {owner_session.reason}"
            )
        else:
            try:
                result = establish_baseline(resource, self._send(resource.path, resource.owner))
            except (TransportError, ScopeViolationError) as error:
                result = BaselineUnavailable(resource.id, f"owner request failed: {error}")
        self._baselines[resource.id] = result
        return result


def _skipped(check: Check, reason: InterruptReason) -> CheckResult:
    return CheckResult(check, Outcome.SKIPPED, f"Not executed: {reason.value}", executed=False)
