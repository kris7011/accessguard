import unittest
from typing import ClassVar

from accessguard.domain.checks import Expectation, ListCheck
from accessguard.domain.evaluation import (
    Baseline,
    BaselineResult,
    BaselineUnavailable,
    FindingType,
    Outcome,
    SessionState,
    establish_baseline,
    evaluate_item,
    evaluate_list,
)
from accessguard.domain.policy import Grant, Resource
from tests.helpers import DOC_BODIES, observation

RESOURCE = Resource(
    "acme-doc",
    "document",
    "/d/acme",
    "acme-member",
    ("title", "canary"),
    (Grant("acme", frozenset({"member"})),),
)
BASELINE = Baseline("acme-doc", {"title": "Acme title", "canary": "CANARY-ACME-0001"})
VALID = SessionState.VALID
ALLOW, DENY = Expectation.ALLOW, Expectation.DENY


class BaselineTests(unittest.TestCase):
    def test_owner_response_becomes_baseline(self) -> None:
        result = establish_baseline(RESOURCE, observation(200, DOC_BODIES["acme-doc"]))
        self.assertEqual(result, BASELINE)

    def test_unusable_owner_responses(self) -> None:
        cases = {
            "owner denied": observation(403, {}),
            "not json": observation(200, None, "text/html"),
            "json list": observation(200, [DOC_BODIES["acme-doc"]]),
            "missing field": observation(200, {"title": "Acme title"}),
            "value too short": observation(200, {"title": "Acme title", "canary": "x"}),
            "non-string value": observation(200, {"title": "Acme title", "canary": 12345}),
        }
        for name, obs in cases.items():
            with self.subTest(name):
                self.assertIsInstance(establish_baseline(RESOURCE, obs), BaselineUnavailable)


class ItemVerdictTests(unittest.TestCase):
    def verdict(
        self,
        obs_status: int,
        body: object = None,
        expectation: Expectation = DENY,
        *,
        session: SessionState = VALID,
        baseline: BaselineResult = BASELINE,
        content_type: str = "application/json",
    ) -> tuple[Outcome, FindingType | None]:
        result = evaluate_item(
            expectation, baseline, observation(obs_status, body, content_type), session
        )
        return result.outcome, result.finding_type

    def test_confirmed_leak_requires_all_protected_values(self) -> None:
        self.assertEqual(
            self.verdict(200, DOC_BODIES["acme-doc"]),
            (Outcome.FAIL, FindingType.UNAUTHORIZED_ACCESS),
        )

    def test_leak_nested_in_envelope_is_detected(self) -> None:
        body = {"data": {"items": [DOC_BODIES["acme-doc"]]}}
        self.assertEqual(self.verdict(200, body)[0], Outcome.FAIL)

    def test_partial_disclosure_is_suspected_not_confirmed(self) -> None:
        self.assertEqual(
            self.verdict(200, {"title": "Acme title"}),
            (Outcome.SUSPECTED, FindingType.UNAUTHORIZED_ACCESS),
        )

    def test_status_200_alone_is_neither_leak_nor_pass(self) -> None:
        """Ported from v0.1 ``test_login_page_is_not_leak`` and extended to soft errors."""
        for body, content_type in (
            ({"login": True}, "application/json"),
            ({"error": "forbidden"}, "application/json"),
            (None, "text/html"),
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    self.verdict(200, body, content_type=content_type)[0], Outcome.INCONCLUSIVE
                )

    def test_substring_is_not_a_match(self) -> None:
        body = {"text": "prefix CANARY-ACME-0001 suffix", "title": "Acme title!"}
        self.assertEqual(self.verdict(200, body)[0], Outcome.INCONCLUSIVE)

    def test_denials_pass_for_deny_expectation(self) -> None:
        for status in (403, 404):
            with self.subTest(status=status):
                self.assertEqual(self.verdict(status)[0], Outcome.PASS)
        self.assertEqual(self.verdict(401, session=SessionState.NOT_APPLICABLE)[0], Outcome.PASS)

    def test_expired_session_is_not_pass(self) -> None:
        self.assertEqual(self.verdict(401, session=SessionState.INVALID)[0], Outcome.INCONCLUSIVE)

    def test_server_errors_rate_limits_and_redirects_are_not_pass(self) -> None:
        """Ported from v0.1 ``test_server_error_is_not_pass``; adds 429, 3xx and odd codes."""
        for status in (500, 502, 503, 429, 302, 301, 400, 409):
            with self.subTest(status=status):
                self.assertEqual(self.verdict(status)[0], Outcome.INCONCLUSIVE)

    def test_missing_baseline_is_not_pass(self) -> None:
        """Ported from v0.1 ``test_baseline_failure_is_not_pass``."""
        missing = BaselineUnavailable("acme-doc", "owner request returned HTTP 401")
        self.assertEqual(self.verdict(403, baseline=missing)[0], Outcome.INCONCLUSIVE)
        self.assertEqual(
            self.verdict(200, DOC_BODIES["acme-doc"], baseline=missing)[0], Outcome.INCONCLUSIVE
        )

    def test_granted_access(self) -> None:
        self.assertEqual(self.verdict(200, DOC_BODIES["acme-doc"], ALLOW)[0], Outcome.PASS)
        self.assertEqual(self.verdict(200, {"title": "Acme title"}, ALLOW)[0], Outcome.INCONCLUSIVE)

    def test_denied_granted_access_is_a_finding(self) -> None:
        """Supersedes v0.1 ``test_positive_access_failure`` (was INCONCLUSIVE).

        The baseline proves the resource exists and the session is verified, so a
        403/404 for a granted actor is a definite policy violation (over-restriction).
        """
        for status in (403, 404):
            with self.subTest(status=status):
                self.assertEqual(
                    self.verdict(status, expectation=ALLOW),
                    (Outcome.FAIL, FindingType.EXPECTED_ACCESS_DENIED),
                )
        self.assertEqual(
            self.verdict(401, expectation=ALLOW, session=SessionState.INVALID)[0],
            Outcome.INCONCLUSIVE,
        )


class ListVerdictTests(unittest.TestCase):
    CHECK = ListCheck(
        "list:docs@globex-member",
        "globex-member",
        "docs",
        visible=("globex-doc",),
        hidden=("acme-doc",),
    )
    BASELINES: ClassVar[dict[str, BaselineResult]] = {
        "acme-doc": BASELINE,
        "globex-doc": Baseline(
            "globex-doc", {"title": "Globex title", "canary": "CANARY-GLOBEX-0001"}
        ),
    }

    def verdict(
        self,
        status: int,
        body: object = None,
        check: ListCheck = CHECK,
        baselines: dict[str, BaselineResult] | None = None,
    ) -> Outcome:
        return evaluate_list(
            check, baselines or self.BASELINES, observation(status, body), VALID
        ).outcome

    def test_correct_listing_passes(self) -> None:
        body = {"items": [DOC_BODIES["globex-doc"]]}
        self.assertEqual(self.verdict(200, body), Outcome.PASS)

    def test_hidden_resource_in_listing_fails(self) -> None:
        body = {"items": [DOC_BODIES["globex-doc"], DOC_BODIES["acme-doc"]]}
        self.assertEqual(self.verdict(200, body), Outcome.FAIL)

    def test_partial_hidden_fields_are_suspected(self) -> None:
        body = {"items": [DOC_BODIES["globex-doc"], {"title": "Acme title"}]}
        self.assertEqual(self.verdict(200, body), Outcome.SUSPECTED)

    def test_empty_listing_is_not_proof_of_exclusion(self) -> None:
        self.assertEqual(self.verdict(200, {"items": []}), Outcome.INCONCLUSIVE)

    def test_denied_or_failing_listing_is_inconclusive(self) -> None:
        for status in (403, 429, 500):
            with self.subTest(status=status):
                self.assertEqual(self.verdict(status, {}), Outcome.INCONCLUSIVE)

    def test_anonymous_listing(self) -> None:
        anonymous = ListCheck(
            "list:docs@anonymous",
            "anonymous",
            "docs",
            visible=(),
            hidden=("acme-doc", "globex-doc"),
        )
        self.assertEqual(
            evaluate_list(
                anonymous, self.BASELINES, observation(401, {}), SessionState.NOT_APPLICABLE
            ).outcome,
            Outcome.PASS,
        )
        self.assertEqual(self.verdict(200, {"items": []}, check=anonymous), Outcome.INCONCLUSIVE)

    def test_missing_baseline_blocks_pass_but_not_confirmed_leak(self) -> None:
        baselines: dict[str, BaselineResult] = {
            **self.BASELINES,
            "globex-doc": BaselineUnavailable("globex-doc", "HTTP 500"),
        }
        self.assertEqual(
            self.verdict(200, {"items": []}, baselines=baselines), Outcome.INCONCLUSIVE
        )
        leaked = {"items": [DOC_BODIES["acme-doc"]]}
        self.assertEqual(self.verdict(200, leaked, baselines=baselines), Outcome.FAIL)


if __name__ == "__main__":
    unittest.main()
