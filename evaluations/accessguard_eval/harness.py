"""Reproducible comparison of check-selection strategies under the same HTTP budget.

Strategies: the deterministic policy order (baseline), seeded random orders, and the
agent (with a scripted test adapter by default, or an opt-in real model). Every run of a
fixture uses the same target, the same engine and the same request budget; probes,
baselines and retests count against it for every strategy.

Isolation: a run receives only the fixture's declared policy and the served target's
origin and credentials. The answer key is loaded after the run, for scoring only; the
fixture's flaw configuration is only known to the fixture server.

Every agent run is kept: with ``agent_repeats`` above 1 the agent runs repeatedly on the
same fixture and budget (a fresh client each time) and all repetitions are scored and
averaged, never selected.

Scripted agent results validate the evaluation mechanics, not the quality of any AI.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from accessguard import __version__
from accessguard.adapters.http_target import ScopedHttpClient
from accessguard.application.agent import AgentLimits
from accessguard.application.agent_contract import PROMPT_VERSION, TOOL_CONTRACT_VERSION
from accessguard.application.gate import MAX_REQUEST_BUDGET, RunLimits
from accessguard.application.planning import DeterministicPlanner, SeededRandomPlanner
from accessguard.application.ports import ModelClient, ModelDescriptor, Planner, ProviderValue
from accessguard.application.scan import AgentStrategy, run_agent_scan, run_scan
from accessguard.domain.scope import TargetScope
from accessguard_eval.fixture_server import run_fixture
from accessguard_eval.fixtures import EvaluationFixture, load_answer_key, load_fixture

FINDING_OUTCOMES = ("FAIL", "SUSPECTED")
DEFAULT_FRACTIONS = (0.25, 0.5, 1.0)
DEFAULT_SEEDS = (1, 2, 3, 4, 5)
DEFAULT_MAX_RUN_SECONDS = 300.0
MAX_AGENT_REPEATS = 20
UNKNOWN = "unknown"
"""Provenance value for metadata the provider did not report."""

ModelFactory = Callable[[], ModelClient]


@dataclass(frozen=True)
class RunMetrics:
    fixture: str
    strategy: str
    seed: int | None
    repeat: int | None
    budget: int
    budget_fraction: float
    requests_used: int
    planned_checks: int
    executed_checks: int
    conclusive_ratio: float
    expected_findings: int
    true_positives: int
    confirmed_true_positives: int
    """True positives with outcome FAIL whose separate retest reproduced the verdict."""
    false_positives: int
    recall: float | None
    findings_per_100_requests: float
    requests_to_first_finding: int | None
    seconds_to_first_finding: float | None
    total_seconds: float
    """Wall-clock time of the whole run, including model calls."""
    overall_status: str
    model_calls: int | None
    invalid_replies: int | None
    model_seconds: float | None
    """Time spent waiting for the model (part of ``total_seconds``)."""
    agent_status: str | None
    selection_digest: str | None
    """Short hash of the order in which the agent selected checks; equal digests mean
    two agent runs made identical choices."""


def score(
    report: Mapping[str, Any],
    answer_key: frozenset[str],
    *,
    fixture: str,
    strategy: str,
    seed: int | None,
    budget: int,
    fraction: float,
    repeat: int | None = None,
) -> RunMetrics:
    """Score one report against the answer key. Deterministic except for the seconds."""
    results = report["results"]
    findings = [r for r in results if r["outcome"] in FINDING_OUTCOMES]
    true_positives = [r for r in findings if r["check_id"] in answer_key]
    confirmed = [
        r
        for r in true_positives
        if r["outcome"] == "FAIL" and r.get("verification") == "reproduced"
    ]
    started = datetime.fromisoformat(report["run"]["started_at"])
    finished = datetime.fromisoformat(report["run"]["finished_at"])
    first = min(true_positives, key=lambda r: r["requests_used_after"], default=None)
    requests_used = report["run"]["requests_used"]
    agent = report["run"]["agent"]
    return RunMetrics(
        fixture=fixture,
        strategy=strategy,
        seed=seed,
        repeat=repeat,
        budget=budget,
        budget_fraction=fraction,
        requests_used=requests_used,
        planned_checks=report["coverage"]["planned_checks"],
        executed_checks=report["coverage"]["executed_checks"],
        conclusive_ratio=report["coverage"]["conclusive_ratio"],
        expected_findings=len(answer_key),
        true_positives=len(true_positives),
        confirmed_true_positives=len(confirmed),
        false_positives=len(findings) - len(true_positives),
        recall=round(len(true_positives) / len(answer_key), 4) if answer_key else None,
        findings_per_100_requests=round(100 * len(true_positives) / max(requests_used, 1), 2),
        requests_to_first_finding=first["requests_used_after"] if first else None,
        seconds_to_first_finding=(
            round((datetime.fromisoformat(first["observed_at"]) - started).total_seconds(), 4)
            if first
            else None
        ),
        total_seconds=round((finished - started).total_seconds(), 4),
        overall_status=report["status"]["overall"],
        model_calls=agent["model_calls"] if agent else None,
        invalid_replies=agent["invalid_replies"] if agent else None,
        model_seconds=round(agent["model_latency_ms_total"] / 1000, 3) if agent else None,
        agent_status=agent["status"] if agent else None,
        selection_digest=_selection_digest(agent["steps"]) if agent else None,
    )


def _selection_digest(steps: Sequence[Mapping[str, Any]]) -> str:
    selected = [step["check_id"] for step in steps if step["action"] == "run_check"]
    return hashlib.sha256("\n".join(selected).encode("utf-8")).hexdigest()[:16]


def run_once(
    fixture: EvaluationFixture,
    strategy: Planner | AgentStrategy,
    limits: RunLimits,
) -> dict[str, Any]:
    """Serve the fixture and scan it with ``strategy``. No answer key is involved."""
    with run_fixture(fixture.target) as instance:
        scope = TargetScope.from_origin(instance.origin)
        client = ScopedHttpClient(scope)
        label = f"evaluation-fixture:{fixture.id}"
        if isinstance(strategy, AgentStrategy):
            return run_agent_scan(
                policy=fixture.policy.policy,
                policy_sha256=fixture.policy.sha256,
                client=client,
                credentials=instance.credentials,
                scope=scope,
                target_label=label,
                agent=strategy,
                limits=limits,
            )
        return run_scan(
            policy=fixture.policy.policy,
            policy_sha256=fixture.policy.sha256,
            client=client,
            credentials=instance.credentials,
            scope=scope,
            target_label=label,
            planner=strategy,
            limits=limits,
        )


def reference_run(fixture: EvaluationFixture) -> dict[str, Any]:
    """A complete deterministic run without a budget limit.

    Its request count (probes, baselines, checks and retests) defines the full budget.
    """
    return run_once(fixture, DeterministicPlanner(), RunLimits(max_requests=MAX_REQUEST_BUDGET))


@dataclass(frozen=True)
class EvaluationConfig:
    fixtures: Sequence[str]
    fractions: Sequence[float] = DEFAULT_FRACTIONS
    seeds: Sequence[int] = DEFAULT_SEEDS
    agent_limits: AgentLimits = field(default_factory=AgentLimits)
    max_duration_seconds: float = DEFAULT_MAX_RUN_SECONDS
    """The same run deadline for every strategy; for the agent it includes model time."""
    agent_repeats: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.agent_repeats <= MAX_AGENT_REPEATS:
            raise ValueError(f"agent_repeats must be between 1 and {MAX_AGENT_REPEATS}")
        RunLimits(max_duration_seconds=self.max_duration_seconds)  # validate before any run


def evaluate(config: EvaluationConfig, model_factory: ModelFactory) -> dict[str, Any]:
    """Run every strategy on every fixture and budget; return a JSON-compatible result."""
    runs: list[RunMetrics] = []
    fixtures_meta: dict[str, Any] = {}
    agent_reports: list[Mapping[str, Any]] = []
    for fixture_id in config.fixtures:
        fixture = load_fixture(fixture_id)
        reference = reference_run(fixture)
        complete = int(reference["run"]["requests_used"])
        budgets = sorted({max(1, math.ceil(complete * f)) for f in config.fractions})
        fixtures_meta[fixture_id] = {"sha256": fixture.sha256, "full_budget": complete}
        reports: list[tuple[str, int | None, int | None, int, dict[str, Any]]] = []
        for budget in budgets:
            limits = RunLimits(
                max_requests=budget, max_duration_seconds=config.max_duration_seconds
            )
            deterministic = run_once(fixture, DeterministicPlanner(), limits)
            reports.append(("deterministic", None, None, budget, deterministic))
            for seed in config.seeds:
                planner = SeededRandomPlanner(seed)
                reports.append(
                    ("seeded-random", seed, None, budget, run_once(fixture, planner, limits))
                )
            for repeat in range(1, config.agent_repeats + 1):
                agent = AgentStrategy(model_factory(), config.agent_limits)
                agent_report = run_once(fixture, agent, limits)
                agent_reports.append(agent_report)
                reports.append(("agent", None, repeat, budget, agent_report))
        # Scoring happens only after every run of this fixture has finished.
        answer_key = load_answer_key(fixture_id)
        fixtures_meta[fixture_id]["deterministic_full_budget_exact"] = _exact(reference, answer_key)
        for strategy, run_seed, run_repeat, budget, report in reports:
            runs.append(
                score(
                    report,
                    answer_key,
                    fixture=fixture_id,
                    strategy=strategy,
                    seed=run_seed,
                    budget=budget,
                    fraction=round(budget / complete, 4),
                    repeat=run_repeat,
                )
            )
    return {
        "meta": _meta(config, model_factory, fixtures_meta, agent_reports),
        "runs": [asdict(run) for run in runs],
        "summary": summarize(runs),
    }


def _exact(report: Mapping[str, Any], answer_key: frozenset[str]) -> bool:
    """Fixture sanity check: a complete deterministic run reports exactly the answer key."""
    reported = {r["check_id"] for r in report["results"] if r["outcome"] in FINDING_OUTCOMES}
    return bool(report["run"]["completed"]) and reported == answer_key


def _meta(
    config: EvaluationConfig,
    model_factory: ModelFactory,
    fixtures: Mapping[str, Any],
    agent_reports: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    descriptor = model_factory().descriptor
    # Provenance exactly as the provider reported it in the runs (empty if it reported none).
    reported = [dict(r["run"]["agent"]["provider_metadata"]) for r in agent_reports]
    distinct = [dict(item) for item in {tuple(sorted(m.items())) for m in reported}]
    errors = sorted(
        {
            error
            for r in agent_reports
            if (error := r["run"]["agent"]["provider_metadata_error"]) is not None
        }
    )
    return {
        "tool_version": __version__,
        "prompt_version": PROMPT_VERSION,
        "tool_contract_version": TOOL_CONTRACT_VERSION,
        "agent_adapter": {
            "provider": descriptor.provider,
            "model": descriptor.model,
            "is_real_model": descriptor.is_real_model,
            "label": descriptor.label,
            "settings": dict(descriptor.settings),
            "provider_metadata_reported": distinct,
            "provenance": provenance(descriptor, reported),
            "provider_metadata_errors": errors,
        },
        "agent_limits": asdict(config.agent_limits),
        "agent_repeats": config.agent_repeats,
        "max_run_seconds": config.max_duration_seconds,
        "seeds": list(config.seeds),
        "budget_fractions": list(config.fractions),
        "fixtures": dict(fixtures),
        "interpretation": (
            "Synthetic fixtures only; results say nothing about real-world detection rates. "
            + (
                "The agent used a real model; the result describes this model, prompt and "
                "settings on these fixtures only."
                if descriptor.is_real_model
                else "The agent used a SCRIPTED TEST ADAPTER (a deterministic heuristic, not "
                "an AI model): these results validate the evaluation mechanics, not AI quality."
            )
        ),
    }


def provenance(
    descriptor: ModelDescriptor, reported: Sequence[Mapping[str, ProviderValue]]
) -> dict[str, ProviderValue | list[ProviderValue]]:
    """Every provenance key the adapter declares, with the value(s) reported across runs.

    A key no run reported is :data:`UNKNOWN`. Several distinct values (a list) mean the
    model or server changed during the evaluation, which makes the runs incomparable.
    """
    keys = dict.fromkeys([*descriptor.provenance_keys, *(key for m in reported for key in m)])
    result: dict[str, ProviderValue | list[ProviderValue]] = {}
    for key in keys:
        values = list(dict.fromkeys(m[key] for m in reported if key in m))
        result[key] = UNKNOWN if not values else values[0] if len(values) == 1 else values
    return result


def _mean(values: Sequence[float | int | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(statistics.fmean(present), 2) if present else None


def summarize(runs: Sequence[RunMetrics]) -> list[dict[str, Any]]:
    """One row per fixture, budget and strategy; random seeds are averaged."""
    groups: dict[tuple[str, int, str], list[RunMetrics]] = {}
    for run in runs:
        groups.setdefault((run.fixture, run.budget, run.strategy), []).append(run)
    rows = []
    for (fixture, budget, strategy), group in groups.items():
        first_found = [r.requests_to_first_finding for r in group]
        statuses = Counter(r.agent_status for r in group if r.agent_status is not None)
        digests = {r.selection_digest for r in group if r.selection_digest is not None}
        rows.append(
            {
                "fixture": fixture,
                "budget": budget,
                "budget_fraction": group[0].budget_fraction,
                "strategy": strategy,
                "runs": len(group),
                "expected_findings": group[0].expected_findings,
                "true_positives": _mean([r.true_positives for r in group]),
                "true_positives_min": min(r.true_positives for r in group),
                "true_positives_max": max(r.true_positives for r in group),
                "confirmed_true_positives": _mean([r.confirmed_true_positives for r in group]),
                "false_positives": _mean([r.false_positives for r in group]),
                "findings_per_100_requests": _mean([r.findings_per_100_requests for r in group]),
                "requests_to_first_finding": _mean(first_found),
                "runs_without_finding": sum(v is None for v in first_found),
                "seconds_to_first_finding": _mean([r.seconds_to_first_finding for r in group]),
                "conclusive_ratio": _mean([r.conclusive_ratio for r in group]),
                "executed_checks": _mean([r.executed_checks for r in group]),
                "model_calls": _mean([r.model_calls for r in group]),
                "invalid_replies": _mean([r.invalid_replies for r in group]),
                "model_seconds": _mean([r.model_seconds for r in group]),
                "agent_statuses": dict(sorted(statuses.items())) or None,
                "distinct_selection_orders": len(digests) or None,
                "total_seconds": _mean([r.total_seconds for r in group]),
            }
        )
    return rows
