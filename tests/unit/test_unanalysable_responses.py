"""Regression matrix: a body the engine cannot analyse never turns a denial status into PASS.

Leak detection only reads JSON. Before this was fixed, a 401/403/404 whose body was HTML,
plain text, broken JSON or JSON behind a misleading Content-Type was PASS even when it
carried the protected values, because no values could be extracted from it. Every body
below except the empty ones contains the synthetic protected values of the resource.

Observations are built with the real decoder so the matrix covers bytes → verdict.
"""

import json
import unittest
from collections.abc import Mapping
from typing import ClassVar

from accessguard.adapters.http_transport import RawResponse, decode_observation
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
from accessguard.domain.observation import BodyFormat, HttpObservation
from tests.helpers import DOC_BODIES

ACME = Baseline("acme-doc", {"title": "Acme title", "canary": "CANARY-ACME-0001"})
GLOBEX = Baseline("globex-doc", {"title": "Globex title", "canary": "CANARY-GLOBEX-0001"})
SHARED = Baseline("shared", {"title": "Shared brief", "canary": "CANARY-SHARED-0001"})
PROTECTED_VALUES = [v for b in (ACME, GLOBEX, SHARED) for v in b.protected.values()]

DENIALS = (401, 403, 404)
VALID = SessionState.VALID
EAD = FindingType.EXPECTED_ACCESS_DENIED


def _html(payload: Mapping[str, object]) -> bytes:
    values = " ".join(_strings(payload))
    return f"<html><body><h1>Forbidden</h1><pre>{values}</pre></body></html>".encode()


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


# name -> (render payload to bytes, Content-Type header or None for no header)
UNANALYSABLE = {
    "html": (_html, "text/html; charset=utf-8"),
    "plain-text": (lambda p: ("Forbidden: " + " ".join(_strings(p))).encode(), "text/plain"),
    "invalid-json": (lambda p: json.dumps(p).encode()[:-1], "application/json"),
    "html-as-json": (_html, "application/json"),
    "json-as-text": (lambda p: json.dumps(p).encode(), "text/plain"),
    "json-without-content-type": (lambda p: json.dumps(p).encode(), None),
}
EMPTY_CONTENT_TYPES = ("application/json", "text/html", None)


def decoded(status: int, body: bytes, content_type: str | None) -> HttpObservation:
    headers = {} if content_type is None else {"content-type": content_type}
    return decode_observation(RawResponse(status, headers, body))


def render(fmt: str, status: int, payload: Mapping[str, object]) -> HttpObservation:
    encode, content_type = UNANALYSABLE[fmt]
    return decoded(status, encode(payload), content_type)


class BodyClassificationTests(unittest.TestCase):
    """The decoder distinguishes an empty body from one it could not read."""

    def test_formats(self) -> None:
        cases = {
            ("application/json", b'{"a": "b"}'): BodyFormat.JSON,
            ("application/problem+json; charset=utf-8", b"[]"): BodyFormat.JSON,
            ("APPLICATION/JSON", b'"text"'): BodyFormat.JSON,
            ("application/json", b""): BodyFormat.EMPTY,
            ("text/html", b""): BodyFormat.EMPTY,
            (None, b""): BodyFormat.EMPTY,
            ("application/json", b" "): BodyFormat.INVALID_JSON,
            ("application/json", b"{"): BodyFormat.INVALID_JSON,
            ("application/json", b"<html></html>"): BodyFormat.INVALID_JSON,
            ("application/json", b"\xff\xfe\xfa"): BodyFormat.INVALID_JSON,
            ("text/html", b"<html></html>"): BodyFormat.UNSUPPORTED,
            ("text/plain", b'{"a": "b"}'): BodyFormat.UNSUPPORTED,
            (None, b'{"a": "b"}'): BodyFormat.UNSUPPORTED,
        }
        for (content_type, body), expected in cases.items():
            with self.subTest(content_type=content_type, body=body):
                obs = decoded(200, body, content_type)
                self.assertEqual(obs.body_format, expected)
                self.assertEqual(obs.is_analysable, expected in (BodyFormat.JSON, BodyFormat.EMPTY))
                if expected is not BodyFormat.JSON:
                    self.assertIsNone(obs.json_body)

    def test_deeply_nested_json_is_invalid_not_a_crash(self) -> None:
        obs = decoded(403, b"[" * 100_000 + b"]" * 100_000, "application/json")
        self.assertEqual(obs.body_format, BodyFormat.INVALID_JSON)


class UnanalysableItemTests(unittest.TestCase):
    LEAKED: ClassVar[dict[str, object]] = {
        "error": "forbidden",
        "requested": DOC_BODIES["acme-doc"],
    }

    def assert_safe_reason(self, reason: str) -> None:
        for value in PROTECTED_VALUES:
            self.assertNotIn(value, reason)

    def test_denial_with_unanalysable_body_is_inconclusive_not_pass(self) -> None:
        for fmt in UNANALYSABLE:
            for status in DENIALS:
                with self.subTest(format=fmt, status=status):
                    verdict = evaluate_item(
                        Expectation.DENY, ACME, render(fmt, status, self.LEAKED), VALID
                    )
                    self.assertEqual(verdict.outcome, Outcome.INCONCLUSIVE, verdict.reason)
                    self.assertIsNone(verdict.finding_type)
                    self.assertIn(f"HTTP {status}", verdict.reason)
                    self.assertIn("cannot be ruled out", verdict.reason)
                    self.assert_safe_reason(verdict.reason)

    def test_empty_denial_is_still_pass(self) -> None:
        for content_type in EMPTY_CONTENT_TYPES:
            for status in DENIALS:
                with self.subTest(content_type=content_type, status=status):
                    verdict = evaluate_item(
                        Expectation.DENY, ACME, decoded(status, b"", content_type), VALID
                    )
                    self.assertEqual(verdict.outcome, Outcome.PASS, verdict.reason)
                    self.assertIn("empty body", verdict.reason)

    def test_success_with_unanalysable_body_is_inconclusive(self) -> None:
        for fmt in UNANALYSABLE:
            for expectation in Expectation:
                with self.subTest(format=fmt, expectation=expectation):
                    obs = render(fmt, 200, DOC_BODIES["acme-doc"])
                    verdict = evaluate_item(expectation, ACME, obs, VALID)
                    self.assertEqual(verdict.outcome, Outcome.INCONCLUSIVE, verdict.reason)
                    self.assertIn("cannot be analysed", verdict.reason)
                    self.assert_safe_reason(verdict.reason)

    def test_denied_granted_access_stays_a_finding(self) -> None:
        """The legitimate-access control is kept: a denial status contradicts the grant."""
        for fmt in UNANALYSABLE:
            with self.subTest(format=fmt):
                obs = render(fmt, 403, {"error": "forbidden"})
                verdict = evaluate_item(Expectation.ALLOW, ACME, obs, VALID)
                self.assertEqual((verdict.outcome, verdict.finding_type), (Outcome.FAIL, EAD))
                self.assertIn("cannot be analysed", verdict.reason)
        empty = evaluate_item(Expectation.ALLOW, ACME, decoded(403, b"", None), VALID)
        self.assertEqual((empty.outcome, empty.finding_type), (Outcome.FAIL, EAD))

    def test_json_leak_still_takes_precedence(self) -> None:
        for content_type in ("application/json", "application/problem+json; charset=utf-8"):
            with self.subTest(content_type=content_type):
                obs = decoded(403, json.dumps(self.LEAKED).encode(), content_type)
                verdict = evaluate_item(Expectation.DENY, ACME, obs, VALID)
                self.assertEqual(verdict.outcome, Outcome.FAIL, verdict.reason)

    def test_expired_session_reason_is_unchanged(self) -> None:
        obs = render("html", 401, self.LEAKED)
        verdict = evaluate_item(Expectation.DENY, ACME, obs, SessionState.INVALID)
        self.assertEqual(verdict.outcome, Outcome.INCONCLUSIVE)
        self.assertIn("session no longer verifies", verdict.reason)


class UnanalysableListTests(unittest.TestCase):
    """globex-member lists /d (globex-doc and shared granted); anonymous is granted nothing."""

    GRANTED = ListCheck(
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
    EVERYTHING: ClassVar[dict[str, object]] = {"items": list(DOC_BODIES.values())}

    def test_denied_listing_with_unanalysable_body_is_inconclusive_not_pass(self) -> None:
        for fmt in UNANALYSABLE:
            for status in DENIALS:
                with self.subTest(format=fmt, status=status):
                    obs = render(fmt, status, self.EVERYTHING)
                    verdict = evaluate_list(
                        self.ANONYMOUS, self.BASELINES, obs, SessionState.NOT_APPLICABLE
                    )
                    self.assertEqual(verdict.outcome, Outcome.INCONCLUSIVE, verdict.reason)
                    self.assertIn("cannot be ruled out", verdict.reason)
                    for value in PROTECTED_VALUES:
                        self.assertNotIn(value, verdict.reason)

    def test_empty_denied_listing_is_still_pass(self) -> None:
        for content_type in EMPTY_CONTENT_TYPES:
            with self.subTest(content_type=content_type):
                obs = decoded(401, b"", content_type)
                verdict = evaluate_list(
                    self.ANONYMOUS, self.BASELINES, obs, SessionState.NOT_APPLICABLE
                )
                self.assertEqual(verdict.outcome, Outcome.PASS, verdict.reason)
                self.assertIn("empty body", verdict.reason)

    def test_successful_listing_with_unanalysable_body_is_inconclusive(self) -> None:
        for fmt in UNANALYSABLE:
            with self.subTest(format=fmt):
                obs = render(fmt, 200, self.EVERYTHING)
                verdict = evaluate_list(self.GRANTED, self.BASELINES, obs, VALID)
                self.assertEqual(verdict.outcome, Outcome.INCONCLUSIVE, verdict.reason)
                self.assertIn("cannot be ruled out", verdict.reason)

    def test_json_listing_leak_still_takes_precedence(self) -> None:
        obs = decoded(401, json.dumps(self.EVERYTHING).encode(), "application/json")
        verdict = evaluate_list(self.ANONYMOUS, self.BASELINES, obs, SessionState.NOT_APPLICABLE)
        self.assertEqual(verdict.outcome, Outcome.FAIL)


if __name__ == "__main__":
    unittest.main()
