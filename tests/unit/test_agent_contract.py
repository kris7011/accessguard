"""The tool contract is the security boundary for model output: strict, no repair."""

import json
import unittest

from accessguard.application.agent_contract import (
    Rejection,
    ReplyRejectedError,
    RunCheck,
    Stop,
    parse_action,
    response_schema,
)

PENDING = ("c001", "c002", "c003")
EXECUTED = ("c004",)


def parse(content: str, max_chars: int = 512) -> object:
    return parse_action(content, pending=PENDING, executed=EXECUTED, max_chars=max_chars)


class AgentContractTests(unittest.TestCase):
    def assert_rejected(self, content: str, rejection: Rejection, max_chars: int = 512) -> None:
        with self.assertRaises(ReplyRejectedError) as raised:
            parse(content, max_chars)
        self.assertEqual(raised.exception.rejection, rejection, content)

    def test_valid_actions(self) -> None:
        self.assertEqual(parse('{"action": "run_check", "check_id": "c002"}'), RunCheck("c002"))
        self.assertEqual(parse('{"action":"stop"}'), Stop())
        # Whitespace around a JSON document is valid JSON, not repair.
        self.assertEqual(parse('\n {"action":"stop"} \n'), Stop())

    def test_invalid_json_is_rejected_not_repaired(self) -> None:
        for content in (
            "",
            "run c001",
            '{"action": "run_check", "check_id": "c001"',
            '```json\n{"action":"stop"}\n```',
            '{"action":"stop"}{"action":"stop"}',
            "{'action': 'stop'}",
            '{"action": "run_check", "check_id": NaN}',
            '{"action": "run_check", "check_id": Infinity}',
        ):
            with self.subTest(content=content):
                self.assert_rejected(content, Rejection.INVALID_JSON)

    def test_reply_must_be_an_object(self) -> None:
        for content in ('["run_check", "c001"]', '"stop"', "null", "42"):
            with self.subTest(content=content):
                self.assert_rejected(content, Rejection.NOT_AN_OBJECT)

    def test_extra_fields_are_rejected(self) -> None:
        for reply in (
            {"action": "run_check", "check_id": "c001", "path": "/admin"},
            {"action": "run_check", "check_id": "c001", "expectation": "allow"},
            {"action": "run_check", "check_id": "c001", "credential": "acme-admin"},
            {"action": "run_check", "check_id": "c001", "reason": "because"},
            {"action": "stop", "check_id": "c001"},
            {"action": "stop", "scope": "http://169.254.169.254"},
        ):
            with self.subTest(reply=reply):
                self.assert_rejected(json.dumps(reply), Rejection.UNEXPECTED_FIELDS)

    def test_missing_fields_are_rejected(self) -> None:
        self.assert_rejected('{"action": "run_check"}', Rejection.MISSING_FIELDS)
        self.assert_rejected('{"check_id": "c001"}', Rejection.MISSING_FIELDS)
        self.assert_rejected("{}", Rejection.MISSING_FIELDS)

    def test_unknown_actions_are_rejected(self) -> None:
        for action in ("add_check", "set_policy", "http_get", "RUN_CHECK", "run_check ", "", 1):
            with self.subTest(action=action):
                reply = json.dumps({"action": action, "check_id": "c001"})
                self.assert_rejected(reply, Rejection.UNKNOWN_ACTION)

    def test_duplicate_keys_cannot_smuggle_a_second_value(self) -> None:
        self.assert_rejected(
            '{"action": "run_check", "check_id": "c001", "check_id": "c999"}',
            Rejection.DUPLICATE_KEYS,
        )

    def test_check_id_must_be_an_approved_pending_alias(self) -> None:
        for invalid in (1, None, ["c001"], {"id": "c001"}, True):
            with self.subTest(check_id=invalid):
                reply = json.dumps({"action": "run_check", "check_id": invalid})
                self.assert_rejected(reply, Rejection.INVALID_CHECK_ID)
        for check_id in ("c999", "C001", "c001 ", "acme-doc@globex-member", "/d/acme", "../c001"):
            with self.subTest(check_id=check_id):
                reply = json.dumps({"action": "run_check", "check_id": check_id})
                self.assert_rejected(reply, Rejection.UNKNOWN_CHECK_ID)

    def test_already_executed_check_is_rejected(self) -> None:
        self.assert_rejected(
            '{"action": "run_check", "check_id": "c004"}', Rejection.ALREADY_EXECUTED
        )

    def test_output_size_is_checked_before_parsing(self) -> None:
        padded = '{"action": "stop"}' + " " * 600
        self.assert_rejected(padded, Rejection.OUTPUT_TOO_LARGE)
        self.assert_rejected("[" * 100_000, Rejection.OUTPUT_TOO_LARGE)
        self.assertEqual(parse('{"action":"stop"}', max_chars=17), Stop())

    def test_deep_nesting_within_the_size_limit_is_invalid_json(self) -> None:
        deep = "[" * 5000 + "]" * 5000
        self.assert_rejected(deep, Rejection.INVALID_JSON, max_chars=20_000)

    def test_response_schema_only_offers_pending_ids_and_two_actions(self) -> None:
        schema = response_schema(PENDING)
        self.assertEqual(schema["additionalProperties"], False)
        properties = schema["properties"]
        assert isinstance(properties, dict)
        self.assertEqual(properties["action"]["enum"], ["run_check", "stop"])
        self.assertEqual(properties["check_id"]["enum"], list(PENDING))


if __name__ == "__main__":
    unittest.main()
