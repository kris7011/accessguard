"""Negative controls: a failing verification step must fail the whole verification.

Runs the real ops/verify.sh (bash) and ops/verify.ps1 (Windows PowerShell or pwsh) with
the deliberately failing ``selftest-fail`` step and checks that its original exit code (3)
is propagated, even when passing steps run before and after it.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import REPO_ROOT

DELIBERATE_EXIT = 3


def find_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    # On Windows only Git for Windows' bash is used: `bash` on PATH may be a WSL launcher
    # (System32\bash.exe or the WindowsApps alias) depending on the calling shell.
    git = shutil.which("git")
    for parent in Path(git).parents if git else ():
        candidate = parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def find_powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def run(command: list[str], log_dir: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHON": sys.executable, "VERIFY_LOG_DIR": log_dir}
    return subprocess.run(  # noqa: S603 - fixed script paths and arguments
        command,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=REPO_ROOT,
        check=False,
    )


@unittest.skipIf(find_bash() is None, "bash is not available")
class BashVerifyTests(unittest.TestCase):
    def verify(self, *steps: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as logs:
            bash = find_bash()
            assert bash is not None
            return run([bash, "ops/verify.sh", *steps], logs)

    def test_failing_step_fails_verification_with_its_original_code(self) -> None:
        result = self.verify("selftest-pass", "selftest-fail", "selftest-pass")
        self.assertEqual(result.returncode, DELIBERATE_EXIT, result.stdout + result.stderr)
        self.assertIn("FAILED  selftest-fail (exit 3", result.stdout)
        self.assertIn("Verification FAILED (exit 3)", result.stdout)
        self.assertEqual(result.stdout.count("ok      selftest-pass"), 2)

    def test_passing_steps_pass(self) -> None:
        result = self.verify("selftest-pass")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Verification passed", result.stdout)

    def test_unknown_step_is_a_usage_error(self) -> None:
        self.assertEqual(self.verify("no-such-step").returncode, 2)

    def test_missing_interpreter_fails(self) -> None:
        bash = find_bash()
        assert bash is not None
        with tempfile.TemporaryDirectory() as logs:
            env_run = run([bash, "ops/verify.sh", "selftest-pass"], logs)
            missing = subprocess.run(  # noqa: S603
                [bash, "ops/verify.sh", "selftest-pass"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=REPO_ROOT,
                check=False,
                env={**os.environ, "PYTHON": "/no/such/python", "VERIFY_LOG_DIR": logs},
            )
        self.assertEqual(env_run.returncode, 0)
        self.assertNotEqual(missing.returncode, 0)

    def test_why_pipes_are_not_used(self) -> None:
        """Documents the hazard: without pipefail, `cmd | tail` reports tail's success."""
        bash = find_bash()
        assert bash is not None
        failing = f'"{Path(sys.executable).as_posix()}" -c "raise SystemExit(3)"'
        hidden = subprocess.run(  # noqa: S603
            [bash, "-c", f"{failing} | tail -n 1"], capture_output=True, check=False
        )
        kept = subprocess.run(  # noqa: S603
            [bash, "-o", "pipefail", "-c", f"{failing} | tail -n 1"],
            capture_output=True,
            check=False,
        )
        self.assertEqual(hidden.returncode, 0)
        self.assertEqual(kept.returncode, DELIBERATE_EXIT)


@unittest.skipIf(find_powershell() is None, "PowerShell is not available")
class PowerShellVerifyTests(unittest.TestCase):
    def verify(self, steps: str) -> subprocess.CompletedProcess[str]:
        shell = find_powershell()
        assert shell is not None
        with tempfile.TemporaryDirectory() as logs:
            return run(
                [
                    shell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    "ops/verify.ps1",
                    "-Steps",
                    steps,
                    "-Python",
                    sys.executable,
                    "-LogDir",
                    logs,
                ],
                logs,
            )

    def test_failing_step_fails_verification_with_its_original_code(self) -> None:
        result = self.verify("selftest-pass,selftest-fail,selftest-pass")
        self.assertEqual(result.returncode, DELIBERATE_EXIT, result.stdout + result.stderr)
        self.assertIn("FAILED  selftest-fail (exit 3", result.stdout)
        self.assertIn("Verification FAILED (exit 3)", result.stdout)

    def test_passing_steps_pass(self) -> None:
        result = self.verify("selftest-pass")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknown_step_is_a_usage_error(self) -> None:
        self.assertEqual(self.verify("no-such-step").returncode, 2)

    def test_missing_interpreter_fails(self) -> None:
        shell = find_powershell()
        assert shell is not None
        with tempfile.TemporaryDirectory() as logs:
            result = run(
                [
                    shell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    "ops/verify.ps1",
                    "-Steps",
                    "selftest-pass",
                    "-Python",
                    "C:/no/such/python.exe",
                    "-LogDir",
                    logs,
                ],
                logs,
            )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
