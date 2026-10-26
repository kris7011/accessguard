"""Runs the real command-line entry points in a subprocess, as a user would."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import LAB_POLICY_PATH, REPO_ROOT


def run_module(*args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT / "lab")]),
    }
    return subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", *args],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=REPO_ROOT,
        check=False,
    )


class LabCliTests(unittest.TestCase):
    def test_demo_before_and_after(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_module("accessguard_lab", "demo", "--out", tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("ground truth: exact match", result.stdout)
            summary = json.loads((Path(tmp) / "demo-summary.json").read_text("utf-8"))
            self.assertTrue(summary["demo_succeeded"])
            for name in ("vulnerable", "fixed"):
                report = json.loads((Path(tmp) / name / "report.json").read_text("utf-8"))
                self.assertEqual(report["schema_version"], "1.1")
                self.assertTrue((Path(tmp) / name / "report.html").is_file())

    def test_scan_exit_codes_reflect_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                run_module(
                    "accessguard_lab", "scan", "--profile", "fixed", "--out", tmp
                ).returncode,
                0,
            )
            self.assertEqual(
                run_module(
                    "accessguard_lab", "scan", "--profile", "vulnerable", "--out", tmp
                ).returncode,
                1,
            )
            incomplete = run_module(
                "accessguard_lab", "scan", "--profile", "fixed", "--max-requests", "5", "--out", tmp
            )
            self.assertEqual(incomplete.returncode, 3, incomplete.stdout)

    def test_invalid_arguments(self) -> None:
        self.assertEqual(run_module("accessguard_lab", "scan", "--profile", "nope").returncode, 2)
        self.assertEqual(run_module("accessguard_lab", "demo", "--max-requests", "0").returncode, 2)


class ProductCliTests(unittest.TestCase):
    def test_validate_and_plan(self) -> None:
        valid = run_module("accessguard", "validate-policy", str(LAB_POLICY_PATH))
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertIn("56 planned checks", valid.stdout)
        plan = run_module("accessguard", "plan", str(LAB_POLICY_PATH), "--json")
        self.assertEqual(len(json.loads(plan.stdout)), 56)

    def test_invalid_policy_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"schema_version": "1"}', "utf-8")
            result = run_module("accessguard", "validate-policy", str(bad))
        self.assertEqual(result.returncode, 4)
        self.assertIn("missing required keys", result.stderr)

    def test_version(self) -> None:
        result = run_module("accessguard", "--version")
        self.assertEqual(result.stdout.strip(), "accessguard 0.2.0")


if __name__ == "__main__":
    unittest.main()
