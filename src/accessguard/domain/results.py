"""Check results, overall run status and coverage."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from accessguard.domain.checks import Check, ItemCheck
from accessguard.domain.evaluation import ContentMatch, FindingType, Outcome


class VerificationStatus(StrEnum):
    REPRODUCED = "reproduced"
    """A separate retest produced the same FAIL verdict."""
    NOT_REPRODUCED = "not_reproduced"
    """The retest did not confirm the FAIL; the result is downgraded to SUSPECTED."""
    NOT_RETESTED = "not_retested"
    """No retest was performed (disabled, budget exhausted or run stopped)."""


@dataclass(frozen=True)
class Evidence:
    """Minimal, redacted evidence: no credentials, no headers, no response content.

    ``matches`` lists protected field *names* that appeared, never their values.
    """

    method: str
    path: str
    status: int
    content_type: str
    size: int
    body_sha256: str
    matches: tuple[ContentMatch, ...] = ()


@dataclass(frozen=True)
class CheckResult:
    check: Check
    outcome: Outcome
    reason: str
    executed: bool
    observed_at: datetime | None = None
    evidence: Evidence | None = None
    finding_type: FindingType | None = None
    verification: VerificationStatus | None = None


class OverallStatus(StrEnum):
    FINDINGS = "findings"
    NEEDS_REVIEW = "needs_review"
    INCOMPLETE = "incomplete"
    CHECKS_PASSED = "checks_passed"


OVERALL_STATUS_MEANING = {
    OverallStatus.FINDINGS: "At least one check failed: the observed behaviour contradicts "
    "the declared policy.",
    OverallStatus.NEEDS_REVIEW: "No confirmed failure, but at least one suspected violation "
    "needs human review.",
    OverallStatus.INCOMPLETE: "Not every planned check reached a conclusive result. "
    "This is not a security approval.",
    OverallStatus.CHECKS_PASSED: "Every planned check passed. This covers only the declared "
    "policy and the listed checks; it is not a security certification.",
}


def overall_status(results: Sequence[CheckResult]) -> OverallStatus:
    """Aggregate results. Green requires at least one check and every check to PASS."""
    outcomes = {result.outcome for result in results}
    if Outcome.FAIL in outcomes:
        return OverallStatus.FINDINGS
    if Outcome.SUSPECTED in outcomes:
        return OverallStatus.NEEDS_REVIEW
    if not results or outcomes != {Outcome.PASS}:
        return OverallStatus.INCOMPLETE
    return OverallStatus.CHECKS_PASSED


@dataclass(frozen=True)
class Coverage:
    """How much of the planned check matrix produced a conclusive (PASS/FAIL) result."""

    planned: int
    executed: int
    conclusive: int
    by_outcome: dict[str, int]
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    by_relation: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def conclusive_ratio(self) -> float:
        return self.conclusive / self.planned if self.planned else 0.0


def compute_coverage(results: Sequence[CheckResult]) -> Coverage:
    by_outcome = Counter(result.outcome.value for result in results)
    by_kind: dict[str, Counter[str]] = {}
    by_relation: dict[str, Counter[str]] = {}
    for result in results:
        by_kind.setdefault(result.check.kind, Counter())[result.outcome.value] += 1
        if isinstance(result.check, ItemCheck):
            relation = result.check.relation.value
            by_relation.setdefault(relation, Counter())[result.outcome.value] += 1
    return Coverage(
        planned=len(results),
        executed=sum(result.executed for result in results),
        conclusive=by_outcome[Outcome.PASS.value] + by_outcome[Outcome.FAIL.value],
        by_outcome={outcome.value: by_outcome[outcome.value] for outcome in Outcome},
        by_kind={kind: dict(counts) for kind, counts in by_kind.items()},
        by_relation={relation: dict(counts) for relation, counts in by_relation.items()},
    )
