"""Regression matrix: protected content is judged independently of the HTTP status.

A denial status (401/403/404) must never turn into PASS when the response body still
contains protected data, and error responses without a leak must be neither a finding
nor a pass. Covers single resources and listings, full/partial/no match, and legitimate
sharing as a negative control.
"""

import unittest
from typing import ClassVar

from accessguard.domain.checks import Expectation, ListCheck
from accessguard.domain.evaluation import (
    Baseline,
    BaselineResult,
    FindingType,
    Outcome,
    SessionState,
    evaluate_item,
    evaluate_list,
)
from tests.helpers import DOC_BODIES, observation

ACME = Baseline("acme-doc", {"title": "Acme title", "canary": "CANARY-ACME-0001"})
GLOBEX = Baseline("globex-doc", {"title": "Globex title", "canary": "CANARY-GLOBEX-0001"})
SHARED = Baseline("shared", {"title": "Shared brief", "canary": "CANARY-SHARED-0001"})

STATUSES = (200, 401, 403, 404, 500)
UA = FindingType.UNAUTHORIZED_ACCESS
EAD = FindingType.EXPECTED_ACCESS_DENIED


def body_with(match: str, document: str = "acme-doc") -> dict[str, object]:
    """An error-shaped body that embeds a full, partial or no copy of ``document``."""
    if match == "full":
        return {"error": "forbidden", "requested": DOC_BODIES[document]}
    if match == "partial":
        return {"error": "forbidden", "title": DOC_BODIES[document]["title"]}
    return {"error": "forbidden"}


# (status, match) -> expected (outcome, finding type) for an actor the policy does NOT grant.
DENY_EXPECTED: dict[tuple[int, str], tuple[Outcome, FindingType | None]] = {
    **{(status, "full"): (Outcome.FAIL, UA) for status in STATUSES},
    **{(status, "partial"): (Outcome.SUSPECTED, UA) for status in STATUSES},
    (200, "none"): (Outcome.INCONCLUSIVE, None),
    (401, "none"): (Outcome.PASS, None),
    (403, "none"): (Outcome.PASS, None),
    (404, "none"): (Outcome.PASS, None),
    (500, "none"): (Outcome.INCONCLUSIVE, None),
}

# (status, match) -> expected for an actor the policy DOES grant (e.g. legitimate sharing).
ALLOW_EXPECTED: dict[tuple[int, str], tuple[Outcome, FindingType | None]] = {
    (200, "full"): (Outcome.PASS, None),
    (200, "partial"): (Outcome.INCONCLUSIVE, None),
    (200, "none"): (Outcome.INCONCLUSIVE, None),
    **{(s, "full"): (Outcome.INCONCLUSIVE, None) for s in (401, 403, 404)},
    **{(s, "partial"): (Outcome.INCONCLUSIVE, None) for s in (401, 403, 404)},
    **{(s, "none"): (Outcome.FAIL, EAD) for s in (401, 403, 404)},
    **{(500, m): (Outcome.INCONCLUSIVE, None) for m in ("full", "partial", "none")},
}


class ItemStatusMatrixTests(unittest.TestCase):
    def test_unauthorized_actor(self) -> None:
        for (status, match), expected in DENY_EXPECTED.items():
            # 200 responses carry the document itself, not an error envelope
            body = DOC_BODIES["acme-doc"] if (status, match) == (200, "full") else body_with(match)
            with self.subTest(status=status, match=match):
                verdict = evaluate_item(
                    Expectation.DENY, ACME, observation(status, body), SessionState.VALID
                )
                self.assertEqual((verdict.outcome, verdict.finding_type), expected, verdict.reason)

    def test_leak_wins_even_when_the_session_is_invalid_or_absent(self) -> None:
        for session in (SessionState.INVALID, SessionState.NOT_APPLICABLE):
            with self.subTest(session=session):
                verdict = evaluate_item(
                    Expectation.DENY, ACME, observation(401, body_with("full")), session
                )
                self.assertEqual(verdict.outcome, Outcome.FAIL)

    def test_granted_actor_via_shared_grant_is_a_negative_control(self) -> None:
        for (status, match), expected in ALLOW_EXPECTED.items():
            body = (
                DOC_BODIES["shared"]
                if (status, match) == (200, "full")
                else body_with(match, "shared")
            )
            with self.subTest(status=status, match=match):
                verdict = evaluate_item(
                    Expectation.ALLOW, SHARED, observation(status, body), SessionState.VALID
                )
                self.assertEqual((verdict.outcome, verdict.finding_type), expected, verdict.reason)
                self.assertNotEqual(verdict.finding_type, UA)

    def test_leak_evidence_names_fields_not_values(self) -> None:
        verdict = evaluate_item(
            Expectation.DENY, ACME, observation(403, body_with("full")), SessionState.VALID
        )
        self.assertEqual(verdict.matches[0].matched_fields, ("title", "canary"))
        self.assertNotIn("CANARY-ACME-0001", verdict.reason)
        self.assertIn("HTTP 403", verdict.reason)


class ListStatusMatrixTests(unittest.TestCase):
    """globex-member lists /d: globex-doc and shared are granted, acme-doc is hidden."""

    CHECK = ListCheck(
        "list:docs@globex-member",
        "globex-member",
        "docs",
        visible=("globex-doc", "shared"),
        hidden=("acme-doc",),
    )
    ANONYMOUS = ListCheck(
        "list:docs@anonymous",
        "anonymous",
        "docs",
        visible=(),
        hidden=("acme-doc", "globex-doc", "shared"),
    )
    BASELINES: ClassVar[dict[str, BaselineResult]] = {
        "acme-doc": ACME,
        "globex-doc": GLOBEX,
        "shared": SHARED,
    }

    def list_body(self, check: ListCheck, status: int, match: str) -> dict[str, object]:
        # Only a successful listing for a granted actor carries the granted items.
        granted = [DOC_BODIES[rid] for rid in check.visible] if status == 200 else []
        hidden = {
            "full": [DOC_BODIES["acme-doc"]],
            "partial": [{"title": DOC_BODIES["acme-doc"]["title"]}],
            "none": [],
        }[match]
        return {"items": granted + hidden, "error": None if status == 200 else "denied"}

    def verdict(self, check: ListCheck, status: int, match: str) -> tuple[Outcome, object]:
        session = SessionState.NOT_APPLICABLE if check is self.ANONYMOUS else SessionState.VALID
        result = evaluate_list(
            check,
            self.BASELINES,
            observation(status, self.list_body(check, status, match)),
            session,
        )
        return result.outcome, result.finding_type

    def test_hidden_content_is_judged_for_every_status(self) -> None:
        for status in STATUSES:
            for check in (self.CHECK, self.ANONYMOUS):
                with self.subTest(status=status, actor=check.actor):
                    self.assertEqual(self.verdict(check, status, "full"), (Outcome.FAIL, UA))
                    self.assertEqual(
                        self.verdict(check, status, "partial"), (Outcome.SUSPECTED, UA)
                    )

    def test_no_hidden_content(self) -> None:
        expected = {
            # granted lister: success needs the positive control; denials prove nothing
            (self.CHECK.actor, 200): Outcome.PASS,
            (self.CHECK.actor, 401): Outcome.INCONCLUSIVE,
            (self.CHECK.actor, 403): Outcome.INCONCLUSIVE,
            (self.CHECK.actor, 404): Outcome.INCONCLUSIVE,
            (self.CHECK.actor, 500): Outcome.INCONCLUSIVE,
            # anonymous: denial is the expected behaviour; 200 without data proves nothing
            (self.ANONYMOUS.actor, 200): Outcome.INCONCLUSIVE,
            (self.ANONYMOUS.actor, 401): Outcome.PASS,
            (self.ANONYMOUS.actor, 403): Outcome.PASS,
            (self.ANONYMOUS.actor, 404): Outcome.PASS,
            (self.ANONYMOUS.actor, 500): Outcome.INCONCLUSIVE,
        }
        for check in (self.CHECK, self.ANONYMOUS):
            for status in STATUSES:
                with self.subTest(status=status, actor=check.actor):
                    outcome, finding = self.verdict(check, status, "none")
                    self.assertEqual(outcome, expected[(check.actor, status)])
                    self.assertIsNone(finding)

    def test_shared_resource_in_listing_is_not_a_leak(self) -> None:
        """The shared brief appears for its grantee: a negative control, never a finding."""
        outcome, finding = self.verdict(self.CHECK, 200, "none")
        self.assertEqual((outcome, finding), (Outcome.PASS, None))


if __name__ == "__main__":
    unittest.main()
