"""Deterministic verdicts: compare an observation with the owner's baseline and the policy.

Principles (see docs/adr/0002):

* A verdict is only conclusive when the owner baseline proves the resource exists
  and is readable, and the protected content can be compared.
* HTTP 200 alone is never a leak; the response must contain the resource's
  protected values. HTTP 200 alone is never a pass either.
* Redirects, rate limiting, server errors, invalid sessions and soft error pages
  are inconclusive, never PASS.
* Only JSON bodies are analysed. A denial status is PASS only when the body was read
  completely (parsed JSON or empty); a body that cannot be analysed (HTML, plain text,
  invalid JSON, misleading Content-Type) might carry the data and is INCONCLUSIVE.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from accessguard.domain.checks import Expectation, ListCheck
from accessguard.domain.observation import BodyFormat, HttpObservation
from accessguard.domain.policy import Resource, lookup_field

MIN_PROTECTED_VALUE_LENGTH = 4
"""Shorter values would match unrelated content too easily to serve as evidence."""


class Outcome(StrEnum):
    PASS = "PASS"  # noqa: S105 - outcome label, not a credential
    """The declared expectation was verified."""
    FAIL = "FAIL"
    """The observed behaviour contradicts the declared policy (confirmed by evidence)."""
    SUSPECTED = "SUSPECTED"
    """Evidence points to a violation but is incomplete; needs human review."""
    INCONCLUSIVE = "INCONCLUSIVE"
    """Insufficient basis for a conclusion (baseline, session or response unusable)."""
    SKIPPED = "SKIPPED"
    """Not executed, e.g. because the request budget, deadline or a stop signal ended the run."""
    ERROR = "ERROR"
    """Technical error: the request could not be completed."""


class FindingType(StrEnum):
    UNAUTHORIZED_ACCESS = "unauthorized_access"
    """An actor received protected content the policy does not grant."""
    EXPECTED_ACCESS_DENIED = "expected_access_denied"
    """An actor was denied access the policy grants (e.g. an over-broad fix)."""


class SessionState(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    NOT_APPLICABLE = "not_applicable"
    """The anonymous actor has no session to verify."""


@dataclass(frozen=True)
class Baseline:
    """Protected values the owner can read. Values stay in memory and are never reported."""

    resource: str
    protected: Mapping[str, str]


@dataclass(frozen=True)
class BaselineUnavailable:
    resource: str
    reason: str


BaselineResult = Baseline | BaselineUnavailable


@dataclass(frozen=True)
class ContentMatch:
    """Which protected fields of ``resource`` appeared verbatim in a response."""

    resource: str
    matched_fields: tuple[str, ...]
    total_fields: int

    @property
    def complete(self) -> bool:
        return len(self.matched_fields) == self.total_fields

    @property
    def partial(self) -> bool:
        return 0 < len(self.matched_fields) < self.total_fields


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    reason: str
    finding_type: FindingType | None = None
    matches: tuple[ContentMatch, ...] = field(default=())


def establish_baseline(resource: Resource, observation: HttpObservation) -> BaselineResult:
    """Derive the owner baseline, or explain why no reliable baseline exists."""
    if observation.status != 200:
        return BaselineUnavailable(
            resource.id, f"owner request returned HTTP {observation.status}, expected 200"
        )
    unanalysable = _unanalysable_body(observation)
    if unanalysable is not None:
        return BaselineUnavailable(resource.id, f"owner response: {unanalysable}")
    body = observation.json_body
    if not observation.is_json or not isinstance(body, dict):
        return BaselineUnavailable(resource.id, "owner response is not a JSON object")
    protected: dict[str, str] = {}
    for name in resource.protected_fields:
        value = lookup_field(body, name)
        if not isinstance(value, str) or len(value) < MIN_PROTECTED_VALUE_LENGTH:
            return BaselineUnavailable(
                resource.id,
                f"protected field {name!r} is missing or not a string of at least "
                f"{MIN_PROTECTED_VALUE_LENGTH} characters in the owner response",
            )
        protected[name] = value
    return Baseline(resource.id, protected)


def match_content(baseline: Baseline, leaves: frozenset[str]) -> ContentMatch:
    """Exact-value comparison; substrings are deliberately not matched (see limitations)."""
    matched = tuple(name for name, value in baseline.protected.items() if value in leaves)
    return ContentMatch(baseline.resource, matched, len(baseline.protected))


def _unanalysable_body(observation: HttpObservation) -> str | None:
    """Explain why the body could not be analysed, or ``None`` if it was read completely.

    The explanation names only the size and the failure, never body content.
    """
    if observation.is_analysable:
        return None
    if observation.body_format is BodyFormat.INVALID_JSON:
        return (
            f"the {observation.size}-byte body is declared as JSON but is not valid JSON "
            "and cannot be analysed"
        )
    return f"the {observation.size}-byte body is not JSON and cannot be analysed"


def _without(observation: HttpObservation, content: str) -> str:
    return "an empty body" if observation.body_format is BodyFormat.EMPTY else content


def _is_denial(status: int, session: SessionState) -> bool:
    if status in (403, 404):
        return True
    # A 401 only counts as an authorization decision when the actor's session is known
    # to be valid (or there is none); otherwise an expired session would look like a PASS.
    return status == 401 and session is not SessionState.INVALID


def _unusable_response(status: int, session: SessionState) -> Verdict:
    if status == 401 and session is SessionState.INVALID:
        reason = "HTTP 401 and the actor's session no longer verifies (expired or revoked)"
    elif 300 <= status < 400:
        reason = f"HTTP {status} redirect was not followed (possibly a login redirect)"
    elif status == 429:
        reason = "HTTP 429: the target rate-limited the request"
    elif status >= 500:
        reason = f"HTTP {status}: server error"
    else:
        reason = f"HTTP {status} is not interpretable as an access decision"
    return Verdict(Outcome.INCONCLUSIVE, reason)


def _fields(match: ContentMatch) -> str:
    return ", ".join(match.matched_fields)


def _leak_verdict(status: int, matches: list[ContentMatch], what: str) -> Verdict | None:
    """Return FAIL/SUSPECTED when protected content of non-granted resources is present.

    Evaluated before the status code: a denial status does not make a response safe if
    its body still carries the data. Complete matches take precedence over partial ones.
    """
    despite = "" if 200 <= status < 300 else " despite the non-success status"
    complete = tuple(m for m in matches if m.complete)
    if complete:
        return Verdict(
            Outcome.FAIL,
            f"HTTP {status}{despite}: {what} contained all protected fields of "
            f"{_describe(complete)}, which the policy does not grant",
            FindingType.UNAUTHORIZED_ACCESS,
            complete,
        )
    partial = tuple(m for m in matches if m.partial)
    if partial:
        return Verdict(
            Outcome.SUSPECTED,
            f"HTTP {status}{despite}: {what} contained some protected fields of "
            f"{_describe(partial)}",
            FindingType.UNAUTHORIZED_ACCESS,
            partial,
        )
    return None


def _describe(matches: tuple[ContentMatch, ...]) -> str:
    return "; ".join(f"{m.resource!r} ({_fields(m)})" for m in matches)


def evaluate_item(
    expectation: Expectation,
    baseline: BaselineResult,
    observation: HttpObservation,
    session: SessionState,
) -> Verdict:
    """Judge a single-resource read against the expectation derived from the policy.

    Protected content is matched for every status code. For a non-granted actor a
    complete match is FAIL and a partial match SUSPECTED, regardless of the status.
    """
    if isinstance(baseline, BaselineUnavailable):
        return Verdict(Outcome.INCONCLUSIVE, f"Owner baseline unavailable: {baseline.reason}")
    status = observation.status
    match = match_content(baseline, observation.string_leaves())
    unanalysable = _unanalysable_body(observation)
    if expectation is Expectation.DENY:
        leak = _leak_verdict(status, [match], "the response")
        if leak is not None:
            return leak
        if observation.is_success:
            if unanalysable is not None:
                return Verdict(
                    Outcome.INCONCLUSIVE,
                    f"HTTP {status}, but {unanalysable}; protected content cannot be ruled out "
                    "and the response is not counted as a denial",
                )
            return Verdict(
                Outcome.INCONCLUSIVE,
                f"HTTP {status} without protected content: possibly a soft error, "
                "login page or different representation; not counted as a denial",
            )
        if _is_denial(status, session):
            if unanalysable is not None:
                return Verdict(
                    Outcome.INCONCLUSIVE,
                    f"HTTP {status} looks like a denial, but {unanalysable}; leaked protected "
                    "content cannot be ruled out, so it is not counted as PASS",
                )
            return Verdict(
                Outcome.PASS,
                f"Access denied with HTTP {status} without "
                f"{_without(observation, 'protected content')}",
            )
        return _unusable_response(status, session)

    if observation.is_success:
        if match.complete:
            return Verdict(Outcome.PASS, f"HTTP {status} with the expected protected content")
        if unanalysable is not None:
            return Verdict(
                Outcome.INCONCLUSIVE,
                f"HTTP {status}, but {unanalysable}; granted access cannot be verified",
            )
        return Verdict(
            Outcome.INCONCLUSIVE,
            f"HTTP {status} but the response does not contain all protected fields of the baseline",
            matches=(match,) if match.matched_fields else (),
        )
    if match.matched_fields:
        return Verdict(
            Outcome.INCONCLUSIVE,
            f"HTTP {status} contradicts its body, which contains protected content the actor "
            "is granted; neither a confirmed denial nor a verified access",
            matches=(match,),
        )
    if _is_denial(status, session):
        # The denial status alone contradicts the grant. An unanalysable body could at most
        # make the response contradictory; it can never turn it into verified access.
        note = "" if unanalysable is None else f" ({unanalysable})"
        return Verdict(
            Outcome.FAIL,
            f"HTTP {status}: access the policy grants was denied{note}",
            FindingType.EXPECTED_ACCESS_DENIED,
        )
    return _unusable_response(status, session)


def evaluate_list(
    check: ListCheck,
    baselines: Mapping[str, BaselineResult],
    observation: HttpObservation,
    session: SessionState,
) -> Verdict:
    """Judge a collection listing: hidden resources must be absent, visible ones present.

    Hidden content is matched for every status code and takes precedence. The visible
    resources are a positive control: an empty or truncated listing cannot prove that
    hidden resources are excluded.
    """
    status = observation.status
    leaves = observation.string_leaves()
    hidden_matches = [
        match_content(baseline, leaves)
        for resource_id in check.hidden
        if isinstance(baseline := baselines[resource_id], Baseline)
    ]
    leak = _leak_verdict(status, hidden_matches, "the listing")
    if leak is not None:
        return leak
    unanalysable = _unanalysable_body(observation)
    if not observation.is_success:
        if _is_denial(status, session):
            if check.expectation is Expectation.DENY:
                if unanalysable is not None:
                    return Verdict(
                        Outcome.INCONCLUSIVE,
                        f"Listing denied with HTTP {status}, but {unanalysable}; leaked hidden "
                        "content cannot be ruled out, so it is not counted as PASS",
                    )
                return Verdict(
                    Outcome.PASS,
                    f"Listing denied with HTTP {status} without "
                    f"{_without(observation, 'hidden content')}",
                )
            return Verdict(
                Outcome.INCONCLUSIVE,
                f"Listing denied with HTTP {status}; "
                "exclusion of hidden resources cannot be verified",
            )
        return _unusable_response(status, session)

    if unanalysable is not None:
        return Verdict(
            Outcome.INCONCLUSIVE,
            f"HTTP {status} listing, but {unanalysable}; hidden content cannot be ruled out "
            "and granted resources cannot be verified",
        )
    unavailable = [
        rid
        for rid in (*check.visible, *check.hidden)
        if isinstance(baselines[rid], BaselineUnavailable)
    ]
    if unavailable:
        return Verdict(
            Outcome.INCONCLUSIVE,
            f"Owner baseline unavailable for: {', '.join(unavailable)}",
        )
    if check.expectation is Expectation.DENY:
        return Verdict(
            Outcome.INCONCLUSIVE,
            f"HTTP {status} listing without hidden content, but no positive control is available "
            "for this actor; absence of data is not proof of denial",
        )
    missing = [
        rid
        for rid in check.visible
        if not match_content(_baseline(baselines, rid), leaves).complete
    ]
    if missing:
        return Verdict(
            Outcome.INCONCLUSIVE,
            f"Listing is missing granted resources ({', '.join(missing)}): possibly pagination, "
            "filtering or an over-restrictive rule; exclusion cannot be verified",
        )
    return Verdict(
        Outcome.PASS,
        f"Listing contained all {len(check.visible)} granted resources "
        f"and none of {len(check.hidden)} hidden resources",
    )


def _baseline(baselines: Mapping[str, BaselineResult], resource_id: str) -> Baseline:
    baseline = baselines[resource_id]
    if not isinstance(baseline, Baseline):
        raise ValueError(f"Baseline for {resource_id!r} is unavailable")
    return baseline
