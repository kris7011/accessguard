"""The local stack loads env files the way docker compose's ``env_file`` does."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import REPO_ROOT

_spec = importlib.util.spec_from_file_location(
    "run_stack", REPO_ROOT / "ops" / "dev" / "run_stack.py"
)
assert _spec and _spec.loader
run_stack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_stack)


class EnvFileTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "stack.env"
        path.write_text(text, "utf-8")
        return path

    def test_reads_key_value_lines_and_skips_comments_and_blanks(self) -> None:
        path = self.write("# Stripe sandbox\n\nSTRIPE_PRICE_TEAM = price_1\nURL=a=b\n")
        self.assertEqual(
            run_stack.read_env_file(path), {"STRIPE_PRICE_TEAM": "price_1", "URL": "a=b"}
        )

    def test_malformed_lines_stop_the_stack(self) -> None:
        with self.assertRaisesRegex(SystemExit, r"stack\.env:2: expected KEY=VALUE"):
            run_stack.read_env_file(self.write("A=1\nnot a pair\n"))

    def test_the_shell_wins_over_the_file_and_values_are_never_printed(self) -> None:
        path = self.write("STRIPE_PRICE_TEAM=from_file\nSTRIPE_WEBHOOK_SECRET=whsec_value\n")
        with (
            mock.patch.dict(os.environ, {"STRIPE_PRICE_TEAM": "from_shell"}),
            mock.patch("builtins.print") as printed,
        ):
            env = run_stack.environment([path])
        self.assertEqual(env["STRIPE_PRICE_TEAM"], "from_shell")
        self.assertEqual(env["STRIPE_WEBHOOK_SECRET"], "whsec_value")
        self.assertNotIn("whsec_value", str(printed.call_args_list))


if __name__ == "__main__":
    unittest.main()
