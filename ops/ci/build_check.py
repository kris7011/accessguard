"""Build the product wheel into a temporary directory and verify that it excludes the lab.

Usage: python ops/ci/build_check.py  (requires the locked dev tooling: build, setuptools)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    with tempfile.TemporaryDirectory() as out:
        build = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
            [sys.executable, "-m", "build", "--no-isolation", "--outdir", out, str(ROOT)],
            check=False,
        )
        if build.returncode != 0:
            return build.returncode
        check = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
            [sys.executable, str(ROOT / "ops" / "ci" / "check_wheel.py"), out], check=False
        )
        return check.returncode


if __name__ == "__main__":
    raise SystemExit(main())
