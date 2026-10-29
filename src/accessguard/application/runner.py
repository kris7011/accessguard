"""Executes planned checks: session preflight, owner baselines, observation, verdict, retest."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
from accessguard.domain.policy import ANONYMOUS, AccessPolicy, Resource, lookup_field
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
    requests_used_after: Mapping[str, int] = field(default_factory=dict)
    """Target requests the run had used when each executed check got its first verdict."""


@dataclass(frozen=True)
class Progress:
    """A snapshot of how far a run has come, for live status displays.

    ``phase`` is ``checks`` (first verdicts), ``retests`` (separate retests of failures;
    ``completed`` and ``planned`` then count retests) or ``done``.
    """

    phase: str
    completed: int
    planned: int
    requests_used: int


ProgressListener = Callable[[Progress], None]
"""Receives progress snapshots in the scanning thread. It must be fast and must not raise."""

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
        on_progress: ProgressListener | None = None,
    ) -> None:
        self._policy = policy
        self._client = client
        self._credentials = credentials
        self._gate = gate
        self._now = now
        self._retest_findings = retest_findings
        self._sessions: dict[str, SessionStatus] = {}
        self._baselines: dict[str, BaselineResult] = {}
        self._progress: dict[str, int] = {}
        self._on_progress = on_progress
        self._planned = 0
        self._completed = 0

    def run(self, checks: Sequence[Check]) -> RunOutcome:
        """Execute ``checks`` in the given order, then retest failures."""
        self.begin(len(checks))
        interrupted = self.preflight()
        results: list[CheckResult] = []
        for check in checks:
            if interrupted is None:
                try:
                    results.append(self.execute(check))
                    continue
                except RunInterrupted as stop:
                    interrupted = stop.reason
                    results.append(skipped(check, interrupted, in_progress=True))
                    continue
            results.append(skipped(check, interrupted))
        return self.finish(results, interrupted)

    def begin(self, planned: int) -> None:
        """Announce how many checks the run plans; emits the first progress snapshot."""
        self._planned = planned
        self._emit("checks", 0, planned)

    def _emit(self, phase: str, completed: int, planned: int) -> None:
        if self._on_progress is not None:
            self._on_progress(Progress(phase, completed, planned, self._gate.used))

    def preflight(self) -> InterruptReason | None:
        """Probe every identity's session. Returns the interruption reason, if any."""
        try:
            for identity in self._policy.identities:
                self._sessions[identity.id] = self._probe_session(identity.id)
        except RunInterrupted as stop:
            return stop.reason
        return None

    def execute(self, check: Check) -> CheckResult:
        """Execute one approved check and return its deterministic verdict.

        Raises :class:`RunInterrupted` when a run limit ends the run before a verdict.
        """
        result = self._execute(check)
        if result.executed:
            self._progress.setdefault(check.id, self._gate.used)
        if result.outcome is Outcome.FAIL and self._retest_findings:
            # Hold back one request so this finding can be retested within the same budget.
            self._gate.reserve_for_retest()
        self._completed += 1
        self._emit("checks", self._completed, self._planned)
        return result

    def finish(
        self, results: Sequence[CheckResult], interrupted: InterruptReason | None
    ) -> RunOutcome:
        """Retest failures (unless the run was stopped) and assemble the outcome."""
        self._gate.release_reserve()
        verified, interrupted = self._verify_failures(list(results), interrupted)
        self._emit("done", len(verified), len(verified))
        return RunOutcome(
            tuple(verified),
            dict(self._sessions),
            self._gate.used,
            interrupted,
            dict(self._progress),
        )

    @property
    def sessions(self) -> Mapping[str, SessionStatus]:
        return dict(self._sessions)

    def _verify_failures(
        self, results: list[CheckResult], interrupted: InterruptReason | None
    ) -> tuple[list[CheckResult], InterruptReason | None]:
        """Retest every FAIL separately with the same immutable check definition.

        A scan that reached the reserved budget boundary reports ``BUDGET_EXHAUSTED``, but
        the reserve was held back for exactly these retests, so they still run. A deadline
        or a cancellation is a real stop: retests are skipped (and the gate would refuse
        them anyway). A finding that cannot be retested keeps its FAIL verdict and is
        reported ``NOT_RETESTED`` -- it neither disappears nor is presented as reproduced.
        """
        retests_allowed = interrupted in (None, InterruptReason.BUDGET_EXHAUSTED)
        verified: list[CheckResult] = []
        planned_retests = sum(result.outcome is Outcome.FAIL for result in results)
        retested = 0
        for result in results:
            if result.outcome is not Outcome.FAIL:
                verified.append(result)
                continue
            if retests_allowed and self._retest_findings:
                self._emit("retests", retested, planned_retests)
                retested += 1
            if not retests_allowed or not self._retest_findings:
                verified.append(replace(result, verification=VerificationStatus.NOT_RETESTED))
                continue
            try:
                retest = self._execute(result.check)
            except RunInterrupted as stop:
                # The budget, deadline or stop signal ended retesting: the remaining
                # findings keep their verdict and are reported NOT_RETESTED, not dropped.
                interrupted = stop.reason
                retests_allowed = False
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
        limit = self._gate.acquire()
        credential = None if actor == ANONYMOUS else self._credentials[actor]
        try:
            return self._client.get(path, credential, limit)
        except TransportError:
            # A request aborted by the run deadline or the stop signal is an interruption
            # of the run, not a technical error of the target.
            self._gate.raise_if_interrupted()
            raise

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
        expected = self._policy.identity(identity_id).expected_subject
        if (
            observation.status == 200
            and observation.is_json
            and lookup_field(observation.json_body, probe.subject_field) == expected
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


def skipped(check: Check, reason: InterruptReason, *, in_progress: bool = False) -> CheckResult:
    """Mark a check that did not complete. Partial observations are discarded, not judged."""
    prefix = "Interrupted while in progress, no verdict" if in_progress else "Not executed"
    return CheckResult(check, Outcome.SKIPPED, f"{prefix}: {reason.value}", executed=False)
