"""Product CLI: validate and plan policies, scan authorized targets, run the job runner.

``scan`` and ``runner`` only contact origins listed in the local runner configuration
(``--config``), in the network zone given there. Credentials come from that
configuration's credential sources and never leave the machine.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from accessguard import __version__
from accessguard.adapters.control_plane import ControlPlaneClient
from accessguard.adapters.policy_json import load_policy_file
from accessguard.adapters.report_files import write_report
from accessguard.adapters.runner_config import load_runner_config
from accessguard.adapters.scan_spec import load_scan_spec_file
from accessguard.domain.checks import ItemCheck, generate_checks
from accessguard.domain.credentials import Secret
from accessguard.domain.errors import PolicyError, ScopeViolationError
from accessguard.entrypoints import exit_codes
from accessguard.entrypoints.interrupt import run_in_worker, stop_on_signals
from accessguard.entrypoints.runner_service import execute_spec, run_forever


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="accessguard", description=__doc__)
    parser.add_argument("--version", action="version", version=f"accessguard {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-policy", help="Validate a policy file")
    validate.add_argument("policy", type=Path)

    plan = commands.add_parser("plan", help="Print the deterministic check matrix of a policy")
    plan.add_argument("policy", type=Path)
    plan.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    scan = commands.add_parser("scan", help="Scan an authorized target offline from a scan spec")
    scan.add_argument("--spec", type=Path, required=True, help="Scan spec JSON (policy + target)")
    scan.add_argument("--config", type=Path, required=True, help="Runner config (TOML)")
    scan.add_argument("--out", type=Path, default=Path("reports/scan"))

    runner = commands.add_parser(
        "runner", help="Claim and run approved jobs from the control plane"
    )
    runner.add_argument("--config", type=Path, required=True, help="Runner config (TOML)")
    runner.add_argument("--once", action="store_true", help="Process at most one job, then exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "scan":
        return _scan(args)
    if args.command == "runner":
        return _runner(args)
    try:
        loaded = load_policy_file(args.policy)
    except PolicyError as error:
        print(f"Invalid policy: {error}", file=sys.stderr)
        return exit_codes.CONFIG_ERROR
    policy = loaded.policy
    checks = generate_checks(policy)

    if args.command == "validate-policy":
        print(
            f"Policy {policy.id!r} is valid: {len(policy.identities)} identities, "
            f"{len(policy.resources)} resources, {len(policy.collections)} collections, "
            f"{len(checks)} planned checks (sha256 {loaded.sha256})."
        )
        return exit_codes.OK

    rows = [
        {
            "id": check.id,
            "kind": check.kind,
            "actor": check.actor,
            "expectation": check.expectation.value,
            "relation": check.relation.value if isinstance(check, ItemCheck) else None,
        }
        for check in checks
    ]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            relation = row["relation"] or "listing"
            print(f"{row['expectation']:<6} {relation:<13} {row['id']}")
    return exit_codes.OK


def _scan(args: argparse.Namespace) -> int:
    try:
        spec = load_scan_spec_file(args.spec)
        config = load_runner_config(args.config)
    except PolicyError as error:
        print(f"Invalid configuration: {error}", file=sys.stderr)
        return exit_codes.CONFIG_ERROR
    with stop_on_signals() as stop:
        try:
            executed = run_in_worker(lambda: execute_spec(spec, config, stop=stop))
        except (PolicyError, ScopeViolationError) as error:
            print(f"Refused: {error}", file=sys.stderr)
            return exit_codes.CONFIG_ERROR
    path = write_report(executed.report, args.out, executed.secrets)
    status = executed.report["status"]
    print(f"{status['overall']}: {status['meaning']}")
    print(f"Report: {path}")
    if stop.is_set():
        return exit_codes.CANCELLED
    return exit_codes.for_status(status["overall"])


def _runner(args: argparse.Namespace) -> int:
    try:
        config = load_runner_config(args.config)
    except PolicyError as error:
        print(f"Invalid configuration: {error}", file=sys.stderr)
        return exit_codes.CONFIG_ERROR
    if config.control_plane_url is None:
        print("The runner config has no [control_plane] table", file=sys.stderr)
        return exit_codes.CONFIG_ERROR
    token = os.environ.get(config.token_env)
    if not token:
        print(
            f"Set the runner token in the environment variable {config.token_env}", file=sys.stderr
        )
        return exit_codes.CONFIG_ERROR
    client = ControlPlaneClient(config.control_plane_url, Secret(token))
    print(f"Runner started for {config.control_plane_url}; trusted origins: "
          f"{', '.join(t.origin for t in config.targets)}", flush=True)  # fmt: skip

    def log(message: str) -> None:
        print(message, flush=True)

    with stop_on_signals() as stop:
        return run_in_worker(
            lambda: run_forever(client, config, stop=stop, once=args.once, log=log)
        )
