import unittest

from accessguard.domain.checks import Expectation, ItemCheck, ListCheck, Relation
from accessguard.domain.evaluation import Outcome
from accessguard.domain.results import (
    CheckResult,
    OverallStatus,
    compute_coverage,
    overall_status,
)
from accessguard.entrypoints import exit_codes


def result(outcome: Outcome, relation: Relation = Relation.CROSS_TENANT) -> CheckResult:
    check = ItemCheck(f"r@{outcome}", "a", "r", Expectation.DENY, relation)
    return CheckResult(check, outcome, "reason", executed=outcome is not Outcome.SKIPPED)


class OverallStatusTests(unittest.TestCase):
    def test_only_all_pass_is_green(self) -> None:
        self.assertEqual(overall_status([result(Outcome.PASS)] * 3), OverallStatus.CHECKS_PASSED)

    def test_no_checks_is_not_green(self) -> None:
        self.assertEqual(overall_status([]), OverallStatus.INCOMPLETE)

    def test_uncertainty_is_never_approval(self) -> None:
        for outcome in (Outcome.INCONCLUSIVE, Outcome.SKIPPED, Outcome.ERROR):
            with self.subTest(outcome=outcome):
                status = overall_status([result(Outcome.PASS), result(outcome)])
                self.assertEqual(status, OverallStatus.INCOMPLETE)
                self.assertNotEqual(exit_codes.for_status(status), exit_codes.OK)

    def test_precedence(self) -> None:
        self.assertEqual(
            overall_status([result(Outcome.SUSPECTED), result(Outcome.ERROR)]),
            OverallStatus.NEEDS_REVIEW,
        )
        self.assertEqual(
            overall_status([result(Outcome.SUSPECTED), result(Outcome.FAIL)]),
            OverallStatus.FINDINGS,
        )

    def test_exit_codes(self) -> None:
        self.assertEqual(exit_codes.for_status("checks_passed"), 0)
        self.assertEqual(exit_codes.for_status("findings"), 1)
        self.assertEqual(exit_codes.for_status("needs_review"), 3)
        self.assertEqual(exit_codes.for_status("incomplete"), 3)


class CoverageTests(unittest.TestCase):
    def test_counts_and_ratio(self) -> None:
        results = [
            result(Outcome.PASS, Relation.OWNER),
            result(Outcome.FAIL, Relation.CROSS_TENANT),
            result(Outcome.INCONCLUSIVE, Relation.CROSS_TENANT),
            result(Outcome.SKIPPED, Relation.ANONYMOUS),
            CheckResult(ListCheck("l@a", "a", "c", ("x",), ()), Outcome.PASS, "ok", True),
        ]
        coverage = compute_coverage(results)
        self.assertEqual(coverage.planned, 5)
        self.assertEqual(coverage.executed, 4)
        self.assertEqual(coverage.conclusive, 3)
        self.assertAlmostEqual(coverage.conclusive_ratio, 0.6)
        self.assertEqual(coverage.by_outcome["SKIPPED"], 1)
        self.assertEqual(coverage.by_outcome["ERROR"], 0)
        self.assertEqual(coverage.by_kind["list"], {"PASS": 1})
        self.assertEqual(coverage.by_relation["cross_tenant"], {"FAIL": 1, "INCONCLUSIVE": 1})

    def test_empty_coverage(self) -> None:
        self.assertEqual(compute_coverage([]).conclusive_ratio, 0.0)


if __name__ == "__main__":
    unittest.main()
