"""Run detect-secrets over every tracked file against the reviewed baseline.

Replaces ``git ls-files | xargs detect-secrets-hook`` so that a failure of either part
propagates as this process's exit code in every shell. Usage: python ops/ci/secret_scan.py
"""

from __future__ import annotations

import subprocess
import sys

from detect_secrets.pre_commit_hook import main as detect_secrets_hook


def main() -> int:
    listing = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607 - git from PATH is the intended tool
        capture_output=True,
        check=False,
    )
    if listing.returncode != 0:
        print(listing.stderr.decode(errors="replace"), file=sys.stderr)
        return listing.returncode
    files = [name for name in listing.stdout.decode("utf-8").split("\0") if name]
    if not files:
        print("No tracked files found; refusing to report a clean scan", file=sys.stderr)
        return 1
    code = int(detect_secrets_hook(["--baseline", ".secrets.baseline", *files]))
    print(f"detect-secrets scanned {len(files)} tracked files: exit {code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
