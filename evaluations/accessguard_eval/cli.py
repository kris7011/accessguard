"""Evaluation CLI: compare deterministic, seeded random and agent selection. Local only.

python -m accessguard_eval run                         # scripted test adapter (not AI)
python -m accessguard_eval run --model ollama:NAME     # opt-in real local model
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from accessguard.application.agent import FAILED_STATUSES, AgentStatus
from accessguard.domain.errors import ModelError
from accessguard.entrypoints import exit_codes
from accessguard.entrypoints.agent_options import (
    add_agent_arguments,
    agent_limits,
    model_factory,
)
from accessguard_eval.fixtures import fixture_ids
from accessguard_eval.harness import (
    DEFAULT_FRACTIONS,
    DEFAULT_SEEDS,
    EvaluationConfig,
    evaluate,
)

DEFAULT_MODEL = "scripted:follow-up"


def _csv(convert: type) -> Any:
    def parse(value: str) -> tuple[Any, ...]:
        try:
            return tuple(convert(item) for item in value.split(",") if item)
        except ValueError as error:
            raise argparse.ArgumentTypeError(str(error)) from error

    return parse


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="accessguard-eval", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run the comparison and write results")
    run.add_argument("--out", type=Path, default=Path("reports/evaluation"))
    run.add_argument("--fixtures", type=_csv(str), default=None, help="Comma-separated ids")
    run.add_argument("--fractions", type=_csv(float), default=DEFAULT_FRACTIONS)
    run.add_argument("--seeds", type=_csv(int), default=DEFAULT_SEEDS)
    add_agent_arguments(run, model_required=False)
    run.set_defaults(model=DEFAULT_MODEL)
    commands.add_parser("fixtures", help="List evaluation fixtures")
    return parser


def _cell(value: object) -> str:
    return "-" if value is None else str(value)


def render_markdown(result: Mapping[str, Any]) -> str:
    meta = result["meta"]
    adapter = meta["agent_adapter"]
    lines = [
        "# AccessGuard selection-strategy evaluation",
        "",
        f"> {meta['interpretation']}",
        "",
        f"- Tool {meta['tool_version']}, prompt `{meta['prompt_version']}`, "
        f"tool contract `{meta['tool_contract_version']}`",
        f"- Agent adapter: {adapter['label']} (real model: {adapter['is_real_model']}), "
        f"settings `{json.dumps(adapter['settings'])}`",
        f"- Provider-reported metadata: `{json.dumps(adapter['provider_metadata_reported'])}`",
        f"- Random seeds: {meta['seeds']}; budget fractions of a complete run: "
        f"{meta['budget_fractions']}",
        "",
        "Seeded-random rows are means over the seeds (TP min-max in brackets). "
        '"First TP" is the number of target requests used when the first expected finding '
        "was observed (mean over runs that found one). Times are wall-clock and machine-dependent.",
        "",
        "| Fixture | Budget | Strategy | TP / expected | FP | TP per 100 req | First TP (req) "
        "| No TP runs | Conclusive | Model calls | Invalid replies | Seconds |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in result["summary"]:
        tp = _cell(row["true_positives"])
        if row["runs"] > 1:
            tp += f" [{row['true_positives_min']}-{row['true_positives_max']}]"
        lines.append(
            f"| {row['fixture']} | {row['budget']} ({row['budget_fraction']:.0%}) "
            f"| {row['strategy']} | {tp} / {row['expected_findings']} "
            f"| {_cell(row['false_positives'])} | {_cell(row['findings_per_100_requests'])} "
            f"| {_cell(row['requests_to_first_finding'])} | {row['runs_without_finding']} "
            f"| {_cell(row['conclusive_ratio'])} | {_cell(row['model_calls'])} "
            f"| {_cell(row['invalid_replies'])} | {_cell(row['total_seconds'])} |"
        )
    return "\n".join(lines) + "\n"


def _run(args: argparse.Namespace) -> int:
    try:
        factory = model_factory(args.model, args.ollama_port)
        config = EvaluationConfig(
            fixtures=args.fixtures or fixture_ids(),
            fractions=args.fractions,
            seeds=args.seeds,
            agent_limits=agent_limits(args),
        )
    except (ModelError, ValueError) as error:
        print(f"Invalid configuration: {error}", file=sys.stderr)
        return exit_codes.USAGE_ERROR
    unknown = set(config.fixtures) - set(fixture_ids())
    if unknown:
        print(f"Unknown fixtures: {', '.join(sorted(unknown))}", file=sys.stderr)
        return exit_codes.USAGE_ERROR
    print(
        f"Evaluating {len(config.fixtures)} fixtures with {factory().descriptor.label}", flush=True
    )
    result = evaluate(config, factory)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    (args.out / "summary.md").write_text(render_markdown(result), "utf-8")
    print(render_markdown(result))
    print(f"Results: {(args.out / 'results.json').resolve()}")
    mismatched = [
        fixture_id
        for fixture_id, meta in result["meta"]["fixtures"].items()
        if not meta["deterministic_full_budget_exact"]
    ]
    if mismatched:
        print(
            "Fixture sanity check failed: a complete deterministic run does not match the "
            f"answer key for {', '.join(mismatched)}",
            file=sys.stderr,
        )
        return exit_codes.FINDINGS
    failed = [
        run
        for run in result["runs"]
        if run["agent_status"] is not None and AgentStatus(run["agent_status"]) in FAILED_STATUSES
    ]
    if failed:
        print(
            f"{len(failed)} agent runs failed (model failure or invalid replies; no fallback). "
            "Their partial results are included and marked with agent_status.",
            file=sys.stderr,
        )
        return exit_codes.PLANNER_ERROR
    return exit_codes.OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "fixtures":
        for fixture_id in fixture_ids():
            print(fixture_id)
        return exit_codes.OK
    return _run(args)
