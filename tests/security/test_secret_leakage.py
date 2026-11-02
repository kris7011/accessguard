"""Credentials and protected content must never reach reports or console output."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard_lab import cli
from accessguard_lab.dataset import DOCUMENTS, INVOICES
from accessguard_lab.profiles import PROFILES
from accessguard_lab.scan import load_lab_policy, scan_lab
from accessguard_lab.server import run_lab


class SecretLeakageTests(unittest.TestCase):
    def test_reports_and_console_never_contain_credentials_or_protected_values(self) -> None:
        issued: list[str] = []
        real_run_lab = run_lab

        @contextlib.contextmanager
        def recording_run_lab(flaws):  # type: ignore[no-untyped-def]
            with real_run_lab(flaws) as lab:
                issued.extend(secret.reveal() for secret in lab.credentials.values())
                yield lab

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("accessguard_lab.scan.run_lab", recording_run_lab),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = cli.main(["demo", "--out", tmp])
            written = "".join(p.read_text("utf-8") for p in Path(tmp).rglob("*") if p.is_file())

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(issued), 14)  # 7 lab users x 2 lab starts
        output = written + stdout.getvalue() + stderr.getvalue()
        for token in issued:
            self.assertNotIn(token, output)
        protected = [d.canary for d in DOCUMENTS] + [d.title for d in DOCUMENTS]
        protected += [i.canary for i in INVOICES] + [i.reference for i in INVOICES]
        for value in protected:
            self.assertNotIn(value, output)

    def test_lab_credentials_are_random_per_start(self) -> None:
        with run_lab() as first:
            first_tokens = {s.reveal() for s in first.credentials.values()}
        with run_lab() as second:
            second_tokens = {s.reveal() for s in second.credentials.values()}
        self.assertFalse(first_tokens & second_tokens)

    def test_report_is_plain_json_without_credential_objects(self) -> None:
        result = scan_lab(
            profile="fixed",
            flaws=PROFILES["fixed"],
            policy=load_lab_policy("lab"),
            planner=DeterministicPlanner(),
            limits=RunLimits(),
        )
        # json.dumps raises TypeError on any non-JSON object such as a Secret.
        json.dumps(result.report)
        self.assertNotIn("Secret(", str(result.report))


if __name__ == "__main__":
    unittest.main()
