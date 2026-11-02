"""Run Django's deployment checks with production settings (no debug, no test mode).

Usage: python ops/ci/deploy_check.py
Uses a throwaway random secret key and a placeholder host; fails on any warning.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"ACCESSGUARD_TESTING", "ACCESSGUARD_DEBUG"}
    }
    env.update(
        ACCESSGUARD_SECRET_KEY=secrets.token_urlsafe(50),
        ACCESSGUARD_ALLOWED_HOSTS="app.example.com",
        ACCESSGUARD_CSRF_TRUSTED_ORIGINS="https://app.example.com",
        DATABASE_URL="sqlite:///:memory:",
    )
    command = [
        sys.executable,
        str(ROOT / "web" / "manage.py"),
        "check",
        "--deploy",
        "--fail-level",
        "WARNING",
    ]
    return subprocess.run(command, env=env, check=False).returncode  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
