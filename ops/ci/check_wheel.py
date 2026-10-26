"""Fail if the built product wheel contains lab or test code (deliberate flaws must never ship).

Usage: python ops/ci/check_wheel.py dist/
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

FORBIDDEN_PREFIXES = ("accessguard_lab/", "tests/", "lab/")
FORBIDDEN_TEXT = (b"SYNTHETIC-CANARY",)


def main(dist: Path) -> int:
    wheels = sorted(dist.glob("accessguard-*.whl"))
    if len(wheels) != 1:
        print(f"Expected exactly one accessguard wheel in {dist}, found {len(wheels)}")
        return 1
    problems: list[str] = []
    with zipfile.ZipFile(wheels[0]) as wheel:
        for name in wheel.namelist():
            if name.startswith(FORBIDDEN_PREFIXES):
                problems.append(f"forbidden path: {name}")
            elif name.endswith(".py") and any(t in wheel.read(name) for t in FORBIDDEN_TEXT):
                problems.append(f"lab marker in: {name}")
        count = len(wheel.namelist())
    for problem in problems:
        print(problem)
    print(f"{wheels[0].name}: {count} files, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "dist")))
