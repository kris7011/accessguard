"""Lab CLI: run the before/after demo or scan a single lab profile. Local 127.0.0.1 only."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from accessguard.adapters.ollama_planner import OllamaPlanner
from accessguard.application.gate import MAX_REQUEST_BUDGET, RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard.application.ports import Planner
from accessguard.domain.errors import PlannerError
from accessguard.entrypoints import exit_codes
from accessguard_lab import ground_truth
from accessguard_lab.profiles import PROFILES
from accessguard_lab.scan import POLICIES, LabScanResult, load_lab_policy, scan_lab


def _budget(value: str) -> int:
    number = int(value)
    if not 1 <= number <= MAX_REQUEST_BUDGET:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_REQUEST_BUDGET}")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="accessguard-lab", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def add_common(command: argparse.ArgumentParser, default_out: str) -> None:
        command.add_argument("--out", type=Path, default=Path(default_out))
        command.add_argument("--max-requests", type=_budget, default=500)
        command.add_argument(
            "--ollama-model",
            help="Optional locally installed Ollama model that may only reorder checks; "
            "errors are reported, never silently replaced by the baseline",
        )

    demo = commands.add_parser("demo", help="Scan the vulnerable and the fixed lab (before/after)")
    add_common(demo, "reports")

    scan = commands.add_parser("scan", help="Scan one lab profile")
    scan.add_argument("--profile", choices=sorted(PROFILES), required=True)
    scan.add_argument("--policy", choices=sorted(POLICIES), default="lab")
    scan.add_argument("--no-retest", action="store_true", help="Do not retest failures")
    add_common(scan, "reports")

    commands.add_parser("profiles", help="List lab profiles and their flaws")
    return parser


def _planner(model: str | None) -> Planner:
    return OllamaPlanner(model) if model else DeterministicPlanner()


def _print_summary(name: str, result: LabScanResult) -> None:
    report = result.report
    summary = ", ".join(f"{k} {v}" for k, v in report["summary"].items() if v)
    print(f"{name:<11} {report['status']['overall']:<14} {summary}")
    if result.report_path is not None:
        print(f"{'':<11} report: {result.report_path.with_suffix('.html').resolve()}")


def _demo(args: argparse.Namespace) -> int:
    policy = load_lab_policy("lab")
    planner = _planner(args.ollama_model)
    limits = RunLimits(max_requests=args.max_requests)
    comparisons = {}
    statuses = {}
    for name in ("vulnerable", "fixed"):
        result = scan_lab(
            profile=name,
            flaws=PROFILES[name],
            policy=policy,
            planner=planner,
            limits=limits,
            out_dir=args.out / name,
        )
        _print_summary(name, result)
        comparisons[name] = ground_truth.compare(result.report, PROFILES[name])
        statuses[name] = result.report["status"]["overall"]

    success = (
        comparisons["vulnerable"].exact
        and statuses["vulnerable"] == "findings"
        and comparisons["fixed"].exact
        and statuses["fixed"] == "checks_passed"
    )
    summary = {
        "demo_succeeded": success,
        "meaning": "The engine found exactly the seeded lab flaws and confirmed the fixed lab. "
        "Synthetic lab results say nothing about detection rates on real systems.",
        "runs": {
            name: {"status": statuses[name], "ground_truth": comparison.as_dict()}
            for name, comparison in comparisons.items()
        },
    }
    (args.out / "demo-summary.json").write_text(json.dumps(summary, indent=2) + "\n", "utf-8")
    print(
        f"ground truth: {'exact match' if success else 'MISMATCH'} "
        f"(details in {(args.out / 'demo-summary.json').resolve()})"
    )
    print("Exit 0 means the before/after demo behaved as expected, not that any system is secure.")
    return exit_codes.OK if success else exit_codes.FINDINGS


def _scan(args: argparse.Namespace) -> int:
    result = scan_lab(
        profile=args.profile,
        flaws=PROFILES[args.profile],
        policy=load_lab_policy(args.policy),
        planner=_planner(args.ollama_model),
        limits=RunLimits(max_requests=args.max_requests, retest_findings=not args.no_retest),
        out_dir=args.out / args.profile,
    )
    _print_summary(args.profile, result)
    return exit_codes.for_status(result.report["status"]["overall"])


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "profiles":
        for name, flaws in sorted(PROFILES.items()):
            print(f"{name:<20} {', '.join(sorted(flaws)) or '(no flaws)'}")
        return exit_codes.OK
    try:
        return _demo(args) if args.command == "demo" else _scan(args)
    except PlannerError as error:
        print(f"AI planner failed (no fallback was used): {error}", file=sys.stderr)
        return exit_codes.PLANNER_ERROR
