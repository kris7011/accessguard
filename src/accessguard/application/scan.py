"""The scan use cases: policy -> checks -> execution (planned or agent-selected) -> report."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from accessguard import __version__
from accessguard.application.agent import AgentLimits, AgentLoop
from accessguard.application.agent_contract import PROMPT_VERSION
from accessguard.application.gate import RequestGate, RunLimits
from accessguard.application.ports import ModelClient, Planner, PlannerInfo, TargetClient
from accessguard.application.reporting import ReportContext, build_report
from accessguard.application.runner import CheckRunner
from accessguard.domain.checks import generate_checks
from accessguard.domain.credentials import Secret
from accessguard.domain.policy import AccessPolicy
from accessguard.domain.scope import TargetScope


def utc_now() -> datetime:
    return datetime.now(UTC)


def run_scan(
    *,
    policy: AccessPolicy,
    policy_sha256: str,
    client: TargetClient,
    credentials: Mapping[str, Secret],
    scope: TargetScope,
    target_label: str,
    planner: Planner,
    limits: RunLimits,
    stop: threading.Event | None = None,
    now: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """Run every policy-derived check once (plus retests of failures) and return the report.

    Planner failures propagate as ``PlannerError``: there is no silent fallback to
    another planner, so a report never misstates how its check order was produced.
    """
    checks = generate_checks(policy)
    plan = planner.plan(checks)
    started_at = now()
    runner = CheckRunner(
        policy=policy,
        client=client,
        credentials=credentials,
        gate=RequestGate(limits, stop),
        now=now,
        retest_findings=limits.retest_findings,
    )
    outcome = runner.run(plan.checks)
    context = ReportContext(
        tool_version=__version__,
        run_id=uuid.uuid4().hex,
        target_label=target_label,
        scope=scope,
        policy_sha256=policy_sha256,
        planner=plan.info,
        limits=limits,
        started_at=started_at,
        finished_at=now(),
    )
    return build_report(policy, outcome, context)


@dataclass(frozen=True)
class AgentStrategy:
    """Let ``model`` choose the next approved check, one at a time, within ``limits``."""

    model: ModelClient
    limits: AgentLimits


def run_agent_scan(
    *,
    policy: AccessPolicy,
    policy_sha256: str,
    client: TargetClient,
    credentials: Mapping[str, Secret],
    scope: TargetScope,
    target_label: str,
    agent: AgentStrategy,
    limits: RunLimits,
    stop: threading.Event | None = None,
    now: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """Run the adaptive agent over the policy's approved checks and return the report.

    The approved checks, their expectations and every verdict are the same as in
    :func:`run_scan`; only the selection order (and possibly an early stop) differ. A model
    failure does not raise: the report records it (``run.agent.status``) together with
    the partial results.
    """
    checks = generate_checks(policy)
    started_at = now()
    gate = RequestGate(limits, stop)
    runner = CheckRunner(
        policy=policy,
        client=client,
        credentials=credentials,
        gate=gate,
        now=now,
        retest_findings=limits.retest_findings,
    )
    result = AgentLoop(
        policy=policy,
        checks=checks,
        runner=runner,
        gate=gate,
        run_limits=limits,
        model=agent.model,
        limits=agent.limits,
    ).run()
    descriptor = result.trace.descriptor
    context = ReportContext(
        tool_version=__version__,
        run_id=uuid.uuid4().hex,
        target_label=target_label,
        scope=scope,
        policy_sha256=policy_sha256,
        planner=PlannerInfo(
            kind="agent",
            uses_ai=descriptor.is_real_model,
            model=descriptor.model,
            prompt_version=PROMPT_VERSION,
            latency_ms=result.trace.model_latency_ms,
        ),
        limits=limits,
        started_at=started_at,
        finished_at=now(),
        agent=result.trace,
    )
    return build_report(policy, result.outcome, context)
