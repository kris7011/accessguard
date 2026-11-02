"""One pass of the off-site backup loop (ops/docker/offsite_backup.sh) with stubbed tools.

The real script runs under ``sh``; ``date``, ``find``, ``restic``, ``wget`` and ``sleep`` are
replaced by stubs on ``PATH`` that record their calls. The ``sleep`` stub ends the loop after
its first pass.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import REPO_ROOT

SCRIPT = REPO_ROOT / "ops" / "docker" / "offsite_backup.sh"
SH = shutil.which("sh")

STUBS = {
    "date": """case "$*" in
  *%Y-%m-%d*) echo 2026-11-08 ;;
  *%H*) echo "$FAKE_HOUR" ;;
  *%u*) echo "$FAKE_WEEKDAY" ;;
  *) echo 1000 ;;
esac""",
    "find": '[ "$FAKE_DUMP" = 1 ] && echo /backups/accessguard-20261108.dump; exit 0',
    "restic": 'echo "restic $1" >> "$CALLS"; case " $FAKE_FAIL " in *" $1 "*) exit 1 ;; esac',
    "wget": 'echo "wget $*" >> "$CALLS"',
    "sleep": 'kill -TERM "$PPID"',
}


@unittest.skipIf(SH is None, "needs a POSIX sh")
class OffsiteBackupLoopTests(unittest.TestCase):
    def run_once(self, **fake: str) -> tuple[list[str], str]:
        with tempfile.TemporaryDirectory() as tmp:
            stubs = Path(tmp)
            for name, body in STUBS.items():
                stub = stubs / name
                stub.write_text(f"#!/bin/sh\n{body}\n", "utf-8", newline="\n")
                stub.chmod(0o755)
            calls = stubs / "calls.log"
            calls.touch()
            env = {
                **os.environ,
                "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
                "CALLS": str(calls),
                "FAKE_HOUR": "03",
                "FAKE_WEEKDAY": "3",
                "FAKE_DUMP": "1",
                "FAKE_FAIL": "",
                "ACCESSGUARD_BACKUP_HEARTBEAT_URL": "https://heartbeat.invalid/ping",
                **fake,
            }
            done = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
                [str(SH), str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30,
                check=False,
            )  # fmt: skip
            return calls.read_text("utf-8").split(), done.stderr

    def test_weekday_backs_up_prunes_and_pings_without_a_check(self) -> None:
        calls, stderr = self.run_once()
        self.assertEqual(
            calls, ["restic", "backup", "restic", "forget", "wget", "-q", "-O", "/dev/null",
                    "-T", "15", "https://heartbeat.invalid/ping"],
        )  # fmt: skip
        self.assertNotIn("ERROR", stderr)

    def test_sunday_checks_the_repository_before_the_ping(self) -> None:
        calls, stderr = self.run_once(FAKE_WEEKDAY="7")
        self.assertEqual(calls[:6], ["restic", "backup", "restic", "forget", "restic", "check"])
        self.assertIn("wget", calls)
        self.assertNotIn("ERROR", stderr)

    def test_a_failed_sunday_check_withholds_the_ping(self) -> None:
        calls, stderr = self.run_once(FAKE_WEEKDAY="7", FAKE_FAIL="check")
        self.assertNotIn("wget", calls)
        self.assertIn("offsite-backup: ERROR restic check failed", stderr)

    def test_a_failed_backup_skips_retention_and_the_ping(self) -> None:
        calls, stderr = self.run_once(FAKE_FAIL="backup")
        self.assertEqual(calls, ["restic", "backup"])
        self.assertIn("offsite-backup: ERROR restic backup or retention failed", stderr)

    def test_no_recent_dump_means_no_backup_and_no_ping(self) -> None:
        calls, stderr = self.run_once(FAKE_DUMP="0")
        self.assertEqual(calls, [])
        self.assertIn("ERROR no database dump younger than 26 hours", stderr)

    def test_nothing_runs_before_two_o_clock(self) -> None:
        calls, stderr = self.run_once(FAKE_HOUR="01")
        self.assertEqual(calls, [])
        self.assertEqual(stderr.strip(), "")


if __name__ == "__main__":
    unittest.main()
