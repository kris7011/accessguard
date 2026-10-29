"""Composition root for customer-side scans: spec + local trust + local credentials -> report.

Used by ``accessguard scan`` (offline, writes files) and ``accessguard runner`` (claims
jobs from the control plane). The order of checks is the deterministic policy order: the
evaluation in docs/evaluation.md found no prioritisation advantage of a model, and the
product sizes budgets for complete coverage instead.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from accessguard.adapters.control_plane import ClaimedJob, ControlPlaneClient, ControlPlaneError
from accessguard.adapters.http_target import ScopedHttpClient
from accessguard.adapters.runner_config import RunnerConfig, load_credentials
from accessguard.adapters.scan_spec import ScanSpec, parse_scan_spec
from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard.application.runner import Progress
from accessguard.application.scan import run_scan
from accessguard.domain.errors import AccessGuardError, PolicyError, ScopeViolationError
from accessguard.domain.redaction import scrub

HEARTBEAT_SECONDS = 3.0


@dataclass(frozen=True)
class ExecutedScan:
    report: dict[str, Any]
    """Scrubbed report: no credentials (they are replaced before it leaves this function)."""
    secrets: tuple[str, ...]


def check_spec_against_trust(spec: ScanSpec, config: RunnerConfig) -> None:
    """Refuse a spec before any request when it leaves what this runner trusts."""
    trusted = config.trusted_target(spec.target.origin)
    scope = trusted.scope(spec.target.path_prefixes)
    policy = spec.policy.policy
    paths = [policy.session_probe.path]
    paths += [resource.path for resource in policy.resources]
    paths += [collection.path for collection in policy.collections]
    outside = sorted({path for path in paths if not scope.allows_path(path)})
    if outside:
        raise ScopeViolationError(
            f"Policy paths outside the allowed prefixes: {', '.join(outside[:5])}"
        )


def execute_spec(
    spec: ScanSpec,
    config: RunnerConfig,
    *,
    stop: threading.Event | None = None,
    on_progress: Callable[[Progress], None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ExecutedScan:
    check_spec_against_trust(spec, config)
    trusted = config.trusted_target(spec.target.origin)
    scope = trusted.scope(spec.target.path_prefixes)
    limits = spec.limits
    if trusted.max_requests is not None and trusted.max_requests < limits.max_requests:
        limits = RunLimits(
            max_requests=trusted.max_requests,
            min_interval_seconds=limits.min_interval_seconds,
            max_duration_seconds=limits.max_duration_seconds,
            retest_findings=limits.retest_findings,
        )
    policy = spec.policy.policy
    credentials = load_credentials(
        trusted, [i.id for i in policy.identities], os.environ if environ is None else environ
    )
    report = run_scan(
        policy=policy,
        policy_sha256=spec.policy.sha256,
        client=ScopedHttpClient(scope, auth=spec.target.auth),
        credentials=credentials,
        scope=scope,
        target_label=spec.label or scope.origin,
        planner=DeterministicPlanner(),
        limits=limits,
        stop=stop,
        only_checks=spec.checks,
        on_progress=on_progress,
    )
    report["run"]["spec_sha256"] = spec.sha256
    secrets = tuple(credential.reveal() for credential in credentials.values())
    scrubbed = scrub(report, secrets)
    if not isinstance(scrubbed, dict):
        raise TypeError("A report must be a JSON object")
    return ExecutedScan(scrubbed, secrets)


class _Heartbeat:
    """Posts the latest progress periodically and turns a cancel answer into the stop signal.

    Runs in its own thread so a slow control plane never slows the scan itself.
    """

    def __init__(self, client: ControlPlaneClient, job_id: str, stop: threading.Event) -> None:
        self._client = client
        self._job_id = job_id
        self._stop = stop
        self._latest: Progress | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="accessguard-heartbeat")
        self.cancel_requested = False
        self.errors = 0

    def update(self, progress: Progress) -> None:
        with self._lock:
            self._latest = progress

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._done.set()
        self._thread.join()

    def _loop(self) -> None:
        while True:
            self._send()
            if self._done.wait(HEARTBEAT_SECONDS):
                return

    def _send(self) -> None:
        with self._lock:
            progress = self._latest
        payload: dict[str, object] = {"phase": "starting"}
        if progress is not None:
            payload = {
                "phase": progress.phase,
                "completed": progress.completed,
                "planned": progress.planned,
                "requests_used": progress.requests_used,
            }
        try:
            if self._client.heartbeat(self._job_id, payload):
                self.cancel_requested = True
                self._stop.set()
        except ControlPlaneError:
            self.errors += 1  # transient; the job's lease on the server tolerates gaps


def process_job(
    job: ClaimedJob,
    client: ControlPlaneClient,
    config: RunnerConfig,
    *,
    stop: threading.Event,
    log: Callable[[str], None],
    environ: Mapping[str, str] | None = None,
) -> str:
    """Execute one claimed job and report the outcome. Returns a short status word."""
    job_stop = threading.Event()
    try:
        spec = parse_scan_spec(job.spec)
        if spec.sha256 != job.spec_sha256:
            raise PolicyError("the job's spec does not match its approved hash")
        check_spec_against_trust(spec, config)
    except (ScopeViolationError, PolicyError) as error:
        category = "scope_rejected" if isinstance(error, ScopeViolationError) else "invalid_spec"
        log(f"job {job.id}: refused ({category}): {error}")
        client.fail(job.id, category, str(error))
        return category
    log(f"job {job.id}: scanning {spec.target.origin} ({spec.label})")
    with _Heartbeat(client, job.id, job_stop) as heartbeat:
        relay = threading.Thread(target=_relay_stop, args=(stop, job_stop), daemon=True)
        relay.start()
        try:
            executed = execute_spec(
                spec, config, stop=job_stop, on_progress=heartbeat.update, environ=environ
            )
        except AccessGuardError as error:
            log(f"job {job.id}: failed: {error}")
            client.fail(job.id, "runner_error", str(error))
            return "failed"
        finally:
            job_stop.set()  # ends the relay thread
    client.submit_report(job.id, executed.report)
    status = executed.report["status"]["overall"]
    log(f"job {job.id}: report submitted ({status})")
    return "cancelled" if heartbeat.cancel_requested else str(status)


def _relay_stop(source: threading.Event, target: threading.Event) -> None:
    """Forward a runner-wide stop (Ctrl+C) to the current job until the job ends."""
    while not target.wait(0.2):
        if source.is_set():
            target.set()


def run_forever(
    client: ControlPlaneClient,
    config: RunnerConfig,
    *,
    stop: threading.Event,
    once: bool,
    log: Callable[[str], None],
    sleep: Callable[[float], bool] | None = None,
) -> int:
    """Poll for jobs until ``stop`` is set. With ``once``, return after the first job."""
    wait = sleep or stop.wait
    failures = 0
    while not stop.is_set():
        try:
            job = client.claim()
        except ControlPlaneError as error:
            failures += 1
            log(f"claim failed ({error}); retrying")
            if error.status in (401, 403):
                log("the runner token was rejected; check the token or create a new one")
                return 4
            wait(min(config.poll_seconds * failures, 60.0))
            continue
        failures = 0
        if job is None:
            if once:
                return 0
            wait(config.poll_seconds)
            continue
        try:
            process_job(job, client, config, stop=stop, log=log)
        except ControlPlaneError as error:
            log(f"job {job.id}: could not report to the control plane ({error})")
        if once:
            return 0
    return 130
