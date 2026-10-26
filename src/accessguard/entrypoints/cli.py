"""Product CLI. Milestone 1 only validates and plans policies; it never contacts a target.

Scanning is available only against the isolated local laboratory (``accessguard-lab``)
until external targets are supported with an explicit authorization model (milestone 3).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from accessguard import __version__
from accessguard.adapters.policy_json import load_policy_file
from accessguard.domain.checks import ItemCheck, generate_checks
from accessguard.domain.errors import PolicyError
from accessguard.entrypoints import exit_codes


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="accessguard", description=__doc__)
    parser.add_argument("--version", action="version", version=f"accessguard {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-policy", help="Validate a policy file")
    validate.add_argument("policy", type=Path)

    plan = commands.add_parser("plan", help="Print the deterministic check matrix of a policy")
    plan.add_argument("policy", type=Path)
    plan.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
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
