"""The scan use case: policy -> planned checks -> execution -> verified report."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from accessguard import __version__
from accessguard.application.gate import RequestGate, RunLimits
from accessguard.application.ports import Planner, TargetClient
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
