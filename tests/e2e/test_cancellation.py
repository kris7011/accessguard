"""Controlled cancellation of a real CLI run: partial report, documented exit code, clean exit."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import REPO_ROOT

CANCELLED_EXIT = 130
# The venv launcher on Windows would receive the console event too; use the real interpreter.
PYTHON = getattr(sys, "_base_executable", sys.executable)


class CliCancellationTests(unittest.TestCase):
    def test_interrupt_writes_partial_report_and_exits_130(self) -> None:
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(["src", "lab"])}
        # sys.platform (not os.name) lets mypy skip the other platform's API on every host.
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            creationflags = 0
        with tempfile.TemporaryDirectory() as out:
            process = subprocess.Popen(  # noqa: S603 - fixed interpreter and arguments
                [
                    PYTHON,
                    "-m",
                    "accessguard_lab",
                    "scan",
                    "--profile",
                    "fixed",
                    "--min-interval",
                    "0.2",
                    "--out",
                    out,
                ],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags,
            )
            assert process.stdout is not None
            first_line = process.stdout.readline()
            self.assertIn("Scanning", first_line)
            # ~14 s run (0.2 s pacing); 4 s leaves completed checks to keep, even on slow hosts.
            time.sleep(4.0)
            started = time.monotonic()
            if sys.platform == "win32":
                os.kill(process.pid, signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=30)
            shutdown_seconds = time.monotonic() - started

            self.assertEqual(process.returncode, CANCELLED_EXIT, stdout + stderr)
            self.assertLess(shutdown_seconds, 5.0)
            self.assertIn("cancelled", stdout.lower())
            report = json.loads((Path(out) / "fixed" / "report.json").read_text("utf-8"))
            self.assertTrue((Path(out) / "fixed" / "report.html").is_file())

        self.assertEqual(report["run"]["interrupted"], "run cancelled by stop signal")
        self.assertFalse(report["run"]["completed"])
        self.assertEqual(report["status"]["overall"], "incomplete")
        self.assertGreater(report["summary"]["PASS"], 0, "completed results are kept")
        self.assertGreater(report["summary"]["SKIPPED"], 0, "unfinished checks are marked")
        self.assertLess(report["coverage"]["conclusive_ratio"], 1.0)
        skipped = [r for r in report["results"] if r["outcome"] == "SKIPPED"]
        self.assertTrue(all(not r["executed"] for r in skipped))
        self.assertTrue(all("cancelled" in r["reason"] for r in skipped))


if __name__ == "__main__":
    unittest.main()
