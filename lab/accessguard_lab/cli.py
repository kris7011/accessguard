"""Lab CLI: before/after demo, single-profile scans and agent runs. Local 127.0.0.1 only."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from accessguard.adapters.ollama_planner import OllamaPlanner
from accessguard.application.agent import FAILED_STATUSES, AgentStatus
from accessguard.application.gate import MAX_REQUEST_BUDGET, InterruptReason, RunLimits
from accessguard.application.planning import DeterministicPlanner, SeededRandomPlanner
from accessguard.application.ports import Planner
from accessguard.domain.errors import ModelError, PlannerError
from accessguard.entrypoints import exit_codes
from accessguard.entrypoints.agent_options import add_agent_arguments, agent_strategy
from accessguard.entrypoints.interrupt import run_in_worker, stop_on_signals
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

    def add_limits(command: argparse.ArgumentParser, default_out: str) -> None:
        command.add_argument("--out", type=Path, default=Path(default_out))
        command.add_argument("--max-requests", type=_budget, default=500)
        command.add_argument(
            "--min-interval",
            type=float,
            default=0.0,
            metavar="SECONDS",
            help="Minimum time between requests (rate limit), 0-60",
        )
        command.add_argument(
            "--max-duration",
            type=float,
            default=300.0,
            metavar="SECONDS",
            help="Hard wall-clock limit per run, including in-flight requests and model "
            "calls (max 3600)",
        )

    def add_common(command: argparse.ArgumentParser, default_out: str) -> None:
        add_limits(command, default_out)
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
    scan.add_argument(
        "--order",
        choices=("deterministic", "random"),
        default="deterministic",
        help="Check order without AI: policy order (baseline) or seeded random",
    )
    scan.add_argument("--seed", type=int, default=0, help="Seed for --order random")
    add_common(scan, "reports")

    agent = commands.add_parser(
        "agent", help="Let a model (or a scripted test adapter) choose checks one at a time"
    )
    agent.add_argument("--profile", choices=sorted(PROFILES), required=True)
    agent.add_argument("--policy", choices=sorted(POLICIES), default="lab")
    agent.add_argument("--no-retest", action="store_true", help="Do not retest failures")
    add_limits(agent, "reports/agent")
    add_agent_arguments(agent, model_required=True)

    commands.add_parser("profiles", help="List lab profiles and their flaws")
    return parser


def _planner(model: str | None, order: str = "deterministic", seed: int = 0) -> Planner:
    if model:
        return OllamaPlanner(model)
    return SeededRandomPlanner(seed) if order == "random" else DeterministicPlanner()


def _limits(args: argparse.Namespace, *, retest: bool = True) -> RunLimits:
    return RunLimits(
        max_requests=args.max_requests,
        min_interval_seconds=args.min_interval,
        max_duration_seconds=args.max_duration,
        retest_findings=retest,
    )


def _cancelled(result: LabScanResult) -> bool:
    return bool(result.report["run"]["interrupted"] == InterruptReason.CANCELLED.value)


def _report_cancellation(result: LabScanResult) -> int:
    print(
        "Run cancelled: completed results were kept, unfinished checks are marked SKIPPED, "
        "and the partial report above is not a completed scan."
    )
    return exit_codes.CANCELLED


def _print_summary(name: str, result: LabScanResult) -> None:
    report = result.report
    summary = ", ".join(f"{k} {v}" for k, v in report["summary"].items() if v)
    print(f"{name:<11} {report['status']['overall']:<14} {summary}")
    if result.report_path is not None:
        print(f"{'':<11} report: {result.report_path.with_suffix('.html').resolve()}")


def _demo(args: argparse.Namespace, stop: threading.Event) -> int:
    policy = load_lab_policy("lab")
    planner = _planner(args.ollama_model)
    limits = _limits(args)
    print(
        "Scanning the vulnerable and fixed lab (Ctrl+C stops and keeps a partial report)",
        flush=True,
    )
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
            stop=stop,
        )
        _print_summary(name, result)
        if _cancelled(result):
            return _report_cancellation(result)
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


def _scan(args: argparse.Namespace, stop: threading.Event) -> int:
    print(
        f"Scanning lab profile {args.profile!r} (Ctrl+C stops and keeps a partial report)",
        flush=True,
    )
    result = scan_lab(
        profile=args.profile,
        flaws=PROFILES[args.profile],
        policy=load_lab_policy(args.policy),
        planner=_planner(args.ollama_model, args.order, args.seed),
        limits=_limits(args, retest=not args.no_retest),
        out_dir=args.out / args.profile,
        stop=stop,
    )
    _print_summary(args.profile, result)
    if _cancelled(result):
        return _report_cancellation(result)
    return exit_codes.for_status(result.report["status"]["overall"])


def _agent(args: argparse.Namespace, stop: threading.Event) -> int:
    strategy = agent_strategy(args)
    print(
        f"Agent run on lab profile {args.profile!r} with {strategy.model.descriptor.label} "
        "(Ctrl+C stops and keeps a partial report)",
        flush=True,
    )
    result = scan_lab(
        profile=args.profile,
        flaws=PROFILES[args.profile],
        policy=load_lab_policy(args.policy),
        planner=strategy,
        limits=_limits(args, retest=not args.no_retest),
        out_dir=args.out / args.profile,
        stop=stop,
    )
    _print_summary(args.profile, result)
    agent = result.report["run"]["agent"]
    print(
        f"{'':<11} agent: {agent['status']}, {agent['model_calls']} model calls, "
        f"{agent['invalid_replies']} invalid replies, "
        f"{result.report['run']['requests_used']} target requests"
    )
    if _cancelled(result):
        return _report_cancellation(result)
    if AgentStatus(agent["status"]) in FAILED_STATUSES:
        print(
            f"Agent model failed (no fallback was used): {agent['failure']}. Completed results "
            "were kept; checks that were not executed are SKIPPED in the partial report.",
            file=sys.stderr,
        )
        return exit_codes.PLANNER_ERROR
    return exit_codes.for_status(result.report["status"]["overall"])


_COMMANDS = {"demo": _demo, "scan": _scan, "agent": _agent}


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "profiles":
        for name, flaws in sorted(PROFILES.items()):
            print(f"{name:<20} {', '.join(sorted(flaws)) or '(no flaws)'}")
        return exit_codes.OK
    try:
        _limits(args)
    except ValueError as error:
        print(f"Invalid limits: {error}", file=sys.stderr)
        return exit_codes.USAGE_ERROR
    if args.command == "agent":
        try:
            agent_strategy(args)  # validates the model name, port and agent limits offline
        except (ModelError, ValueError) as error:
            print(f"Invalid agent configuration: {error}", file=sys.stderr)
            return exit_codes.USAGE_ERROR
    command = _COMMANDS[args.command]
    try:
        with stop_on_signals() as stop:
            return run_in_worker(lambda: command(args, stop))
    except PlannerError as error:
        print(f"AI planner failed (no fallback was used): {error}", file=sys.stderr)
        return exit_codes.PLANNER_ERROR
