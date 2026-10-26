"""Hand-written ground truth for ``policies/lab.json``.

Each set lists the check ids that a correct engine must report for one flaw.
They are written out literally, not computed from the server or engine logic,
so that a bug in either cannot silently redefine what "correct" means.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from accessguard_lab.server import Flaw

EXPECTED_FAILURES: Mapping[Flaw, frozenset[str]] = {
    Flaw.DOCUMENT_IDOR: frozenset(
        {
            "acme-doc@globex-admin",
            "acme-doc@globex-member",
            "acme-doc@initech-admin",
            "acme-doc@initech-member",
            "globex-doc@acme-admin",
            "globex-doc@acme-member",
            "globex-doc@initech-admin",
            "globex-doc@initech-member",
            "initech-doc@acme-admin",
            "initech-doc@acme-member",
            "initech-doc@globex-admin",
            "initech-doc@globex-member",
            "acme-shared-brief@initech-admin",
            "acme-shared-brief@initech-member",
        }
    ),
    Flaw.SHARE_OVERBROAD: frozenset(
        {
            "acme-shared-brief@initech-admin",
            "acme-shared-brief@initech-member",
            "list:documents@initech-admin",
            "list:documents@initech-member",
        }
    ),
    Flaw.INVOICE_ROLE_BYPASS: frozenset(
        {
            "acme-invoice@acme-member",
            "globex-invoice@globex-member",
            "initech-invoice@initech-member",
        }
    ),
    Flaw.LIST_LEAK: frozenset(
        {
            "list:documents@acme-admin",
            "list:documents@acme-member",
            "list:documents@globex-admin",
            "list:documents@globex-member",
            "list:documents@initech-admin",
            "list:documents@initech-member",
        }
    ),
    # Over-restrictive "fix": the legitimate share is denied. Shared resources act as
    # negative controls, so this must be reported rather than counted as secure.
    Flaw.SHARE_IGNORED: frozenset(
        {
            "acme-shared-brief@globex-admin",
            "acme-shared-brief@globex-member",
        }
    ),
}

EXPECTED_INCONCLUSIVE: Mapping[Flaw, frozenset[str]] = {
    # Listings that omit a granted resource cannot prove that hidden ones are excluded.
    Flaw.SHARE_IGNORED: frozenset({"list:documents@globex-admin", "list:documents@globex-member"}),
}


def expected_failures(flaws: Iterable[Flaw]) -> frozenset[str]:
    return frozenset().union(*(EXPECTED_FAILURES[flaw] for flaw in flaws))


def expected_inconclusive(flaws: Iterable[Flaw]) -> frozenset[str]:
    return frozenset().union(*(EXPECTED_INCONCLUSIVE.get(flaw, frozenset()) for flaw in flaws))


@dataclass(frozen=True)
class GroundTruthComparison:
    """Compares a lab report with the ground truth. Synthetic: not a real-world detection rate."""

    expected_failures: frozenset[str]
    reported_failures: frozenset[str]
    expected_inconclusive: frozenset[str]
    reported_other: frozenset[str]
    """Results that are neither PASS nor FAIL."""

    @property
    def true_positives(self) -> frozenset[str]:
        return self.reported_failures & self.expected_failures

    @property
    def false_positives(self) -> frozenset[str]:
        return self.reported_failures - self.expected_failures

    @property
    def false_negatives(self) -> frozenset[str]:
        return self.expected_failures - self.reported_failures

    @property
    def exact(self) -> bool:
        return (
            self.reported_failures == self.expected_failures
            and self.reported_other == self.expected_inconclusive
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "exact_match": self.exact,
            "expected_failures": len(self.expected_failures),
            "true_positives": len(self.true_positives),
            "false_positives": sorted(self.false_positives),
            "false_negatives": sorted(self.false_negatives),
            "unexpected_non_pass": sorted(self.reported_other - self.expected_inconclusive),
            "missing_expected_inconclusive": sorted(
                self.expected_inconclusive - self.reported_other
            ),
        }


def compare(report: Mapping[str, Any], flaws: Iterable[Flaw]) -> GroundTruthComparison:
    flaw_set = frozenset(flaws)
    results = report["results"]
    return GroundTruthComparison(
        expected_failures=expected_failures(flaw_set),
        reported_failures=frozenset(r["check_id"] for r in results if r["outcome"] == "FAIL"),
        expected_inconclusive=expected_inconclusive(flaw_set),
        reported_other=frozenset(
            r["check_id"] for r in results if r["outcome"] not in ("PASS", "FAIL")
        ),
    )
