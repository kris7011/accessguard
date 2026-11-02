"""Composition root for lab scans: wires the product engine to a freshly started lab."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from accessguard.adapters.http_target import ScopedHttpClient
from accessguard.adapters.policy_json import LoadedPolicy, load_policy_bytes
from accessguard.adapters.report_files import write_report
from accessguard.application.gate import RunLimits
from accessguard.application.ports import Planner
from accessguard.application.scan import AgentStrategy, run_agent_scan, run_scan
from accessguard.domain.scope import TargetScope
from accessguard_lab.server import Flaw, run_lab

POLICIES = {"lab": "lab.json", "v0.1-parity": "v0_1_parity.json"}


def load_lab_policy(name: str) -> LoadedPolicy:
    resource = files("accessguard_lab").joinpath("policies", POLICIES[name])
    return load_policy_bytes(resource.read_bytes())


@dataclass(frozen=True)
class LabScanResult:
    report: dict[str, Any]
    report_path: Path | None


def scan_lab(
    *,
    profile: str,
    flaws: frozenset[Flaw],
    policy: LoadedPolicy,
    planner: Planner | AgentStrategy,
    limits: RunLimits,
    out_dir: Path | None = None,
    timeout_seconds: float = 5.0,
    stop: threading.Event | None = None,
) -> LabScanResult:
    """Start the lab with ``flaws``, scan it and optionally write the report files.

    ``planner`` is either a planner (fixed order) or an :class:`AgentStrategy`. The agent
    receives only the model-visible state; the active flaws and the ground truth are
    never passed to it. Setting ``stop`` ends the run early; the report is still written
    and marks every unfinished check as SKIPPED. The lab is shut down in every case.
    """
    with run_lab(flaws) as lab:
        scope = TargetScope.from_origin(lab.origin)
        client = ScopedHttpClient(scope, timeout_seconds=timeout_seconds)
        label = f"accessguard-lab:{profile}"
        if isinstance(planner, AgentStrategy):
            report = run_agent_scan(
                policy=policy.policy,
                policy_sha256=policy.sha256,
                client=client,
                credentials=lab.credentials,
                scope=scope,
                target_label=label,
                agent=planner,
                limits=limits,
                stop=stop,
            )
        else:
            report = run_scan(
                policy=policy.policy,
                policy_sha256=policy.sha256,
                client=client,
                credentials=lab.credentials,
                scope=scope,
                target_label=label,
                planner=planner,
                limits=limits,
                stop=stop,
            )
        secrets = [credential.reveal() for credential in lab.credentials.values()]
    report_path = write_report(report, out_dir, secrets) if out_dir is not None else None
    return LabScanResult(report, report_path)
