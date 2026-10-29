"""Command line: serve the sample API (flawed or fixed) for local integration tests and demos."""

from __future__ import annotations

import argparse
import contextlib
import json
import signal
import threading
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from accessguard_sampleapp.app import FLAWS, Variant
from accessguard_sampleapp.server import new_api_keys, run_sample_app


def _write_credentials(path: Path, keys: dict[str, str]) -> None:
    """Write the API keys as ``{identity: key}`` for the AccessGuard runner (owner-only file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, indent=2) + "\n", "utf-8")
    with contextlib.suppress(OSError):  # best effort on Windows, effective on POSIX
        path.chmod(0o600)


def _read_credentials(path: Path) -> dict[str, str] | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and all(isinstance(v, str) for v in data.values()):
        return {str(k): v for k, v in data.items()}
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="accessguard-sampleapp", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Serve the sample API on 127.0.0.1")
    serve.add_argument("--variant", choices=[v.value for v in Variant], default="flawed")
    serve.add_argument("--port", type=int, default=8800)
    serve.add_argument(
        "--credentials-out",
        type=Path,
        required=True,
        help="Where to write the test API keys (JSON). Reused if it already exists, so a "
        "flawed and a fixed instance can share the same keys.",
    )
    commands.add_parser("flaws", help="List the deliberate bugs of the flawed variant")
    args = parser.parse_args(argv)

    if args.command == "flaws":
        for name, description in FLAWS.items():
            print(f"{name:<22} {description}")
        return 0

    keys = _read_credentials(args.credentials_out) or new_api_keys()
    _write_credentials(args.credentials_out, keys)
    stop = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)
    with run_sample_app(Variant(args.variant), port=args.port, api_keys=keys) as app:
        print(
            f"Sample API ({app.variant.value}) on {app.origin}; "
            f"API keys in {args.credentials_out}. Press Ctrl+C to stop.",
            flush=True,
        )
        while not stop.wait(0.5):
            pass
    return 0
