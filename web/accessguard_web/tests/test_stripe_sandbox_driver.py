"""The sandbox driver's reading of Stripe CLI output: JSON after the banner, errors, no keys.

The driver (ops/stripe_sandbox/sandbox.py) needs the web dependencies (``stripe``), so its
tests live here rather than with the dependency-free engine tests.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "stripe_sandbox", Path(__file__).resolve().parents[3] / "ops" / "stripe_sandbox" / "sandbox.py"
)
assert _spec and _spec.loader
sandbox = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sandbox
_spec.loader.exec_module(sandbox)

BANNER = "▸ Running in Example sandbox · sandbox (acct_000)\n"


class ParseCliOutputTests(unittest.TestCase):
    def test_the_object_after_the_banner_is_returned(self) -> None:
        stdout = BANNER + '{\n  "id": "clock_1", "livemode": false, "status": "ready"\n}\n'
        self.assertEqual(
            sandbox.parse_cli_output(stdout, ""),
            {"id": "clock_1", "livemode": False, "status": "ready"},
        )

    def test_a_stripe_error_stops_the_driver(self) -> None:
        stdout = BANNER + '{"error": {"type": "invalid_request_error", "message": "No such clock"}}'
        with self.assertRaises(SystemExit) as raised:
            sandbox.parse_cli_output(stdout, "")
        self.assertIn("invalid_request_error", str(raised.exception.code))
        self.assertIn("No such clock", str(raised.exception.code))

    def test_no_json_stops_the_driver_without_quoting_a_key(self) -> None:
        # A made-up masked key in the shape Stripe quotes them (not a credential).
        stderr = "authorization failed for rk_test_51***abcd, run stripe login"
        with self.assertRaises(SystemExit) as raised:
            sandbox.parse_cli_output("", stderr)
        message = str(raised.exception.code)
        self.assertIn("<key>", message)
        self.assertNotIn("rk_test_", message)

    def test_an_error_message_quoting_a_key_is_scrubbed(self) -> None:
        stdout = '{"error": {"type": "permission_error", "message": "key rkcs_test_51***x denied"}}'
        with self.assertRaises(SystemExit) as raised:
            sandbox.parse_cli_output(stdout, "")
        self.assertNotIn("rkcs_test_", str(raised.exception.code))


class AssertTestModeTests(unittest.TestCase):
    def test_plain_json_objects_are_checked_like_sdk_objects(self) -> None:
        sandbox.assert_test_mode({"id": "clock_1", "livemode": False})
        for live in ({"id": "clock_2", "livemode": True}, {"id": "clock_3"}):
            with self.assertRaises(SystemExit):
                sandbox.assert_test_mode(live)


if __name__ == "__main__":
    unittest.main()
