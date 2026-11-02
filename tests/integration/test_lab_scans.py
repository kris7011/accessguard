"""End-to-end engine runs over real HTTP against the local lab, compared with ground truth."""

import unittest
from typing import Any, ClassVar

from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard_lab import ground_truth
from accessguard_lab.profiles import PROFILES
from accessguard_lab.scan import load_lab_policy, scan_lab
from accessguard_lab.server import Flaw


def scan(profile: str, policy: str = "lab") -> dict[str, Any]:
    return scan_lab(
        profile=profile,
        flaws=PROFILES[profile],
        policy=load_lab_policy(policy),
        planner=DeterministicPlanner(),
        limits=RunLimits(),
    ).report


class LabScanTests(unittest.TestCase):
    def assert_matches_ground_truth(self, profile: str) -> dict[str, Any]:
        report = scan(profile)
        comparison = ground_truth.compare(report, PROFILES[profile])
        self.assertEqual(comparison.false_positives, frozenset(), profile)
        self.assertEqual(comparison.false_negatives, frozenset(), profile)
        self.assertTrue(comparison.exact, comparison.as_dict())
        return report

    def test_fixed_lab_passes_every_check(self) -> None:
        report = self.assert_matches_ground_truth("fixed")
        self.assertEqual(report["status"]["overall"], "checks_passed")
        self.assertEqual(report["summary"]["PASS"], 56)
        self.assertEqual(report["coverage"]["conclusive_ratio"], 1.0)

    def test_vulnerable_lab_reports_exactly_the_seeded_flaws(self) -> None:
        report = self.assert_matches_ground_truth("vulnerable")
        self.assertEqual(report["status"]["overall"], "findings")
        self.assertEqual(report["summary"]["FAIL"], 23)
        self.assertTrue(all(f["verification"] == "reproduced" for f in report["findings"]))

    def test_each_flaw_in_isolation(self) -> None:
        for flaw in Flaw:
            with self.subTest(flaw=flaw.value):
                self.assert_matches_ground_truth(flaw.value)

    def test_explicit_share_is_a_negative_control(self) -> None:
        """Legitimate cross-tenant access to the shared brief is never reported as a leak."""
        results = {r["check_id"]: r for r in scan("vulnerable")["results"]}
        for actor in ("globex-admin", "globex-member"):
            result = results[f"acme-shared-brief@{actor}"]
            self.assertEqual((result["relation"], result["outcome"]), ("shared_grant", "PASS"))

    def test_over_restrictive_fix_is_reported(self) -> None:
        report = scan("share-ignored")
        types = {f["type"] for f in report["findings"]}
        self.assertEqual(types, {"expected_access_denied"})
        self.assertNotEqual(report["status"]["overall"], "checks_passed")

    def test_same_tenant_role_boundary(self) -> None:
        results = {r["check_id"]: r for r in scan("invoice-role-bypass")["results"]}
        self.assertEqual(results["acme-invoice@acme-member"]["outcome"], "FAIL")
        self.assertEqual(results["acme-invoice@acme-member"]["relation"], "same_tenant")
        self.assertEqual(results["acme-invoice@globex-admin"]["outcome"], "PASS")


class PrototypeParityTests(unittest.TestCase):
    """The v0.1 scenario (two tenants, own/cross/anonymous) still produces v0.1's results.

    Ported from v0.1 ``test_real_http_before_after``.
    """

    V01_CASES: ClassVar[dict[str, str]] = {
        "a-own": "acme-doc@acme-member",
        "b-own": "globex-doc@globex-member",
        "a-cross": "globex-doc@acme-member",
        "b-cross": "acme-doc@globex-member",
        "anonymous": "acme-doc@anonymous",
    }

    def outcomes(self, profile: str) -> dict[str, str]:
        results = {r["check_id"]: r["outcome"] for r in scan(profile, "v0.1-parity")["results"]}
        return {case: results[check_id] for case, check_id in self.V01_CASES.items()}

    def test_vulnerable_before_and_fixed_after(self) -> None:
        self.assertEqual(
            self.outcomes("vulnerable"),
            {
                "a-own": "PASS",
                "b-own": "PASS",
                "a-cross": "FAIL",
                "b-cross": "FAIL",
                "anonymous": "PASS",
            },
        )
        self.assertEqual(set(self.outcomes("fixed").values()), {"PASS"})


if __name__ == "__main__":
    unittest.main()
