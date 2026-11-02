"""Scan lifecycle and findings: the use cases behind the web views and the runner API.

Verdicts are never made here. The control plane stores the engine's report, checks that it
belongs to the approved spec, and turns its results into tracked findings. A finding is
resolved as *verified* only by a conclusive PASS of the same check; inconclusive, skipped
or failed checks never resolve anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accessguard.adapters.scan_spec import parse_scan_spec, sha256_of
from accessguard.application.scan import select_checks
from accessguard.domain.checks import Check, Expectation, ItemCheck, ListCheck, generate_checks
from accessguard.domain.errors import PolicyError
from accessguard_web import conf
from accessguard_web.accounts.models import User, record
from accessguard_web.billing.entitlements import LimitExceeded, require_scan_allowed
from accessguard_web.projects.config import evaluate, spec_document
from accessguard_web.projects.demo import demo_origins
from accessguard_web.projects.models import Project
from accessguard_web.scans.models import (
    ACTIVE_STATUSES,
    EventSource,
    Finding,
    FindingEvent,
    FindingStatus,
    Label,
    Runner,
    Scan,
    ScanKind,
    ScanStatus,
)

ATTESTATION = (
    "I confirm that my organisation owns this target or is explicitly authorised to test it, "
    "that the test users are dedicated test accounts, and that the listed GET requests may be "
    "sent within the stated limits."
)

OUTCOMES = frozenset({"PASS", "FAIL", "SUSPECTED", "INCONCLUSIVE", "SKIPPED", "ERROR"})
OVERALL = frozenset({"findings", "needs_review", "incomplete", "checks_passed"})
FAILURE_CATEGORIES = frozenset({"scope_rejected", "invalid_spec", "runner_error", "invalid_report"})


class ScanError(Exception):
    """A user-facing reason why a scan action is not possible."""


class InvalidReport(Exception):  # noqa: N818 - a rejected input, reported to the runner
    """The uploaded report does not match the approved scan."""


# --- Approval -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeSummary:
    """What the user approves: shown on the review page and frozen into the scan."""

    origin: str
    path_prefixes: tuple[str, ...]
    auth: str
    identities: int
    resources: int
    collections: int
    checks: int
    max_requests: int
    min_interval_seconds: float
    max_duration_seconds: float
    recommended_requests: int
    check_ids: tuple[str, ...]
    is_subset: bool


def with_positive_controls(project: Project, check_ids: Iterable[str]) -> list[str]:
    """Add the granted-access checks of every retested resource or listing.

    A fix that simply blocks everyone would make the retested denials pass. Retesting the
    legitimate access to the same resource at the same time exposes such an over-broad fix
    as a new ``expected_access_denied`` finding.
    """
    try:
        spec = parse_scan_spec(spec_document(project.config))
    except PolicyError as error:
        raise ScanError(f"The configuration is not complete yet: {error}") from error
    checks = generate_checks(spec.policy.policy)
    wanted = set(check_ids)
    resources = {c.resource for c in checks if isinstance(c, ItemCheck) and c.id in wanted}
    collections = {c.collection for c in checks if isinstance(c, ListCheck) and c.id in wanted}
    controls = {
        c.id
        for c in checks
        if (
            isinstance(c, ItemCheck)
            and c.resource in resources
            and c.expectation is Expectation.ALLOW
        )
        or (isinstance(c, ListCheck) and c.collection in collections)
    }
    return [c.id for c in checks if c.id in wanted | controls]


def scope_summary(project: Project, checks: Sequence[str] | None = None) -> ScopeSummary:
    status = evaluate(project.config)
    if status.spec is None:
        raise ScanError(f"The configuration is not complete yet: {status.error}")
    try:
        spec = parse_scan_spec(spec_document(project.config, list(checks) if checks else None))
        selected, _ = select_checks(spec.policy.policy, spec.checks)
    except PolicyError as error:
        raise ScanError(str(error)) from error
    policy = spec.policy.policy
    auth = spec.target.auth
    return ScopeSummary(
        origin=spec.target.origin,
        path_prefixes=spec.target.path_prefixes,
        auth=auth.kind.value if auth.kind.value == "bearer" else f"{auth.kind.value}: {auth.name}",
        identities=len(policy.identities),
        resources=len(policy.resources),
        collections=len(policy.collections),
        checks=len(selected),
        max_requests=spec.limits.max_requests,
        min_interval_seconds=spec.limits.min_interval_seconds,
        max_duration_seconds=spec.limits.max_duration_seconds,
        recommended_requests=status.recommended_requests,
        check_ids=tuple(c.id for c in selected),
        is_subset=checks is not None,
    )


@transaction.atomic
def approve_scan(
    project: Project,
    user: User,
    *,
    checks: Sequence[str] | None = None,
    findings: Sequence[Finding] = (),
) -> Scan:
    """Freeze the current configuration into an approved, queued scan job."""
    Project.objects.select_for_update().filter(pk=project.pk).first()
    summary = scope_summary(project, checks)
    if project.is_demo and summary.origin not in demo_origins():
        raise ScanError("The demo project can only scan the synthetic demo API.")
    if not project.is_demo:
        try:
            require_scan_allowed(project.organization, planned_checks=summary.checks)
        except LimitExceeded as error:
            raise ScanError(str(error)) from error
    if Scan.objects.filter(project=project, status__in=ACTIVE_STATUSES).exists():
        raise ScanError("A scan of this project is already queued or running.")
    document = spec_document(project.config, list(checks) if checks is not None else None)
    scan = Scan.objects.create(
        organization=project.organization,
        project=project,
        kind=ScanKind.RETEST if checks is not None else ScanKind.FULL,
        is_demo=project.is_demo,
        spec=document,
        spec_sha256=sha256_of(document),
        planned_checks=summary.checks,
        approved_by=user,
        approved_at=timezone.now(),
        attestation=ATTESTATION,
    )
    if findings:
        scan.retest_findings.set(findings)
    record(project.organization, user, "scan.approved", f"scan:{scan.pk}",
           project=project.pk, checks=summary.checks, origin=summary.origin,
           spec_sha256=scan.spec_sha256)  # fmt: skip
    return scan


def create_retest(project: Project, user: User, findings: Sequence[Finding]) -> Scan:
    if not findings:
        raise ScanError("Select at least one finding to retest.")
    try:
        spec = parse_scan_spec(spec_document(project.config))
    except PolicyError as error:
        raise ScanError(f"The configuration is not complete yet: {error}") from error
    known = {c.id for c in generate_checks(spec.policy.policy)}
    missing = [f.check_id for f in findings if f.check_id not in known]
    if missing:
        raise ScanError(
            "These findings' checks no longer exist in the current configuration: "
            + ", ".join(missing[:5])
        )
    checks = with_positive_controls(project, [f.check_id for f in findings])
    return approve_scan(project, user, checks=checks, findings=findings)


def cancel_scan(scan: Scan, user: User) -> None:
    with transaction.atomic():
        locked = Scan.objects.select_for_update().get(pk=scan.pk)
        if locked.status == ScanStatus.QUEUED:
            locked.status = ScanStatus.CANCELLED
            locked.finished_at = timezone.now()
            locked.failure_message = "Cancelled before a runner started it."
        elif locked.status == ScanStatus.RUNNING:
            locked.status = ScanStatus.CANCEL_REQUESTED
        else:
            raise ScanError("Only queued or running scans can be cancelled.")
        locked.save()
    record(scan.organization, user, "scan.cancel_requested", f"scan:{scan.pk}")


# --- Runner API ---------------------------------------------------------------------------


def authenticate_runner(token: str) -> Runner | None:
    from accessguard_web.accounts.models import hash_token  # noqa: PLC0415

    runner = Runner.objects.filter(token_hash=hash_token(token), revoked_at__isnull=True).first()
    if runner is not None:
        now = timezone.now()
        if runner.last_seen_at is None or now - runner.last_seen_at > timedelta(seconds=10):
            Runner.objects.filter(pk=runner.pk).update(last_seen_at=now)
    return runner


def sweep_lost_scans() -> int:
    """Mark running scans whose runner stopped sending heartbeats. Never a pass."""
    lease = timedelta(seconds=conf.product()["SCAN_LEASE_SECONDS"])
    cutoff = timezone.now() - lease
    return Scan.objects.filter(
        status__in=(ScanStatus.RUNNING, ScanStatus.CANCEL_REQUESTED), last_heartbeat_at__lt=cutoff
    ).update(
        status=ScanStatus.LOST,
        finished_at=timezone.now(),
        failure_category="runner_lost",
        failure_message="The runner stopped sending progress; no complete report was received.",
    )


def claim_next(runner: Runner) -> Scan | None:
    sweep_lost_scans()
    queue = Scan.objects.filter(status=ScanStatus.QUEUED)
    if runner.is_platform_demo:
        queue = queue.filter(is_demo=True)
    else:
        queue = queue.filter(organization=runner.organization, is_demo=False).filter(
            Q(project__runner__isnull=True) | Q(project__runner=runner)
        )
    now = timezone.now()
    for candidate in queue.order_by("created_at")[:10]:
        claimed = Scan.objects.filter(pk=candidate.pk, status=ScanStatus.QUEUED).update(
            status=ScanStatus.RUNNING, runner=runner, claimed_at=now, last_heartbeat_at=now
        )
        if claimed:  # compare-and-set: two runners can never claim the same scan
            return Scan.objects.get(pk=candidate.pk)
    return None


def runner_scan(runner: Runner, scan_id: int) -> Scan | None:
    """A scan this runner claimed; anything else is invisible to it."""
    return Scan.objects.filter(pk=scan_id, runner=runner).first()


def heartbeat(scan: Scan, payload: dict[str, Any]) -> bool:
    """Record progress; returns True when the runner must stop."""
    if scan.status not in (ScanStatus.RUNNING, ScanStatus.CANCEL_REQUESTED):
        return True
    # Merge: a heartbeat that carries no counters (for example while starting) keeps the
    # last known progress instead of erasing it.
    progress = dict(scan.progress or {})
    progress.update(
        {
            key: value
            for key in ("completed", "planned", "requests_used")
            if isinstance(value := payload.get(key), int) and 0 <= value <= 1_000_000
        }
    )
    phase = payload.get("phase")
    if phase in ("starting", "checks", "retests", "done"):
        progress["phase"] = phase
    Scan.objects.filter(pk=scan.pk).update(progress=progress, last_heartbeat_at=timezone.now())
    return scan.status == ScanStatus.CANCEL_REQUESTED


def fail_scan(scan: Scan, category: str, message: str) -> None:
    if scan.status not in (*ACTIVE_STATUSES, ScanStatus.LOST):
        return
    scan.status = ScanStatus.FAILED
    scan.failure_category = category if category in FAILURE_CATEGORIES else "runner_error"
    scan.failure_message = message[:500]
    scan.finished_at = timezone.now()
    scan.save()


def planned_check_ids(scan: Scan) -> list[str]:
    spec = parse_scan_spec(scan.spec)
    checks: tuple[Check, ...]
    checks, _ = select_checks(spec.policy.policy, spec.checks)
    return [c.id for c in checks]


def validate_report(scan: Scan, report: object) -> dict[str, Any]:
    """Check shape and provenance. A runner cannot add results for checks it was not given."""
    if not isinstance(report, dict):
        raise InvalidReport("report must be a JSON object")
    version = report.get("schema_version")
    if not isinstance(version, str) or not version.startswith("1."):
        raise InvalidReport("unsupported report schema version")
    run, status = report.get("run"), report.get("status")
    if not isinstance(run, dict) or not isinstance(status, dict):
        raise InvalidReport("report has no run or status object")
    if run.get("spec_sha256") != scan.spec_sha256:
        raise InvalidReport("report belongs to a different spec than the approved one")
    if status.get("overall") not in OVERALL or not isinstance(status.get("complete"), bool):
        raise InvalidReport("report status is invalid")
    results, findings = report.get("results"), report.get("findings")
    if not isinstance(results, list) or not isinstance(findings, list):
        raise InvalidReport("report has no results or findings list")
    ids: list[str] = []
    for result in results:
        if not isinstance(result, dict) or result.get("outcome") not in OUTCOMES:
            raise InvalidReport("a result is malformed")
        if not isinstance(result.get("check_id"), str):
            raise InvalidReport("a result has no check id")
        ids.append(result["check_id"])
    if sorted(ids) != sorted(planned_check_ids(scan)):
        raise InvalidReport("report results do not match the approved checks")
    finding_ids = {f.get("id") for f in findings if isinstance(f, dict)}
    failing = {r["check_id"] for r in results if r["outcome"] in ("FAIL", "SUSPECTED")}
    if finding_ids != failing:
        raise InvalidReport("report findings do not match its results")
    return report


@transaction.atomic
def accept_report(scan: Scan, report: object) -> Scan:
    scan = Scan.objects.select_for_update().get(pk=scan.pk)
    if scan.status not in (*ACTIVE_STATUSES, ScanStatus.LOST):
        raise InvalidReport("this scan is already finished")
    valid = validate_report(scan, report)
    interrupted = valid["run"].get("interrupted")
    cancelled = (
        scan.status == ScanStatus.CANCEL_REQUESTED or interrupted == "run cancelled by stop signal"
    )
    scan.report = valid
    scan.report_status = valid["status"]["overall"]
    scan.report_complete = valid["status"]["complete"]
    scan.status = ScanStatus.CANCELLED if cancelled else ScanStatus.COMPLETED
    scan.finished_at = timezone.now()
    executed = sum(1 for r in valid["results"] if r.get("executed"))
    scan.progress = {
        "phase": "done",
        "completed": executed,
        "planned": len(valid["results"]),
        "requests_used": int(valid["run"].get("requests_used") or 0),
    }
    scan.save()
    apply_report(scan, valid)
    return scan


# --- Findings -----------------------------------------------------------------------------


def _event(
    finding: Finding,
    *,
    source: str,
    to_status: str,
    note: str,
    scan: Scan | None = None,
    actor: User | None = None,
    label: str = "",
    engine_version: str = "",
    from_status: str | None = None,
) -> FindingEvent:
    return FindingEvent.objects.create(
        organization=finding.organization,
        finding=finding,
        scan=scan,
        actor=actor,
        source=source,
        from_status=finding.status if from_status is None else from_status,
        to_status=to_status,
        note=note[:1000],
        label=label,
        engine_version=engine_version[:30],
    )


def apply_report(scan: Scan, report: dict[str, Any]) -> None:
    """Turn report results into finding state changes (see the module docstring)."""
    source = EventSource.RETEST if scan.kind == ScanKind.RETEST else EventSource.SCAN
    engine = str(report.get("tool", {}).get("version", ""))
    entries = {f["id"]: f for f in report["findings"] if isinstance(f, dict)}
    targets = set(scan.retest_findings.values_list("pk", flat=True))
    for result in report["results"]:
        check_id, outcome = result["check_id"], result["outcome"]
        if outcome in ("FAIL", "SUSPECTED"):
            _record_failure(scan, source, engine, result, entries.get(check_id, {}))
        elif outcome == "PASS":
            for finding in Finding.objects.filter(project=scan.project, check_id=check_id).exclude(
                status=FindingStatus.REJECTED
            ):
                if finding.status == FindingStatus.RESOLVED and finding.resolution_verified:
                    continue
                _event(finding, source=source, to_status=FindingStatus.RESOLVED, scan=scan,
                       label=Label.FIXED_VERIFIED, engine_version=engine,
                       note=f"The check passed in scan #{scan.pk}: fix verified.")  # fmt: skip
                finding.status = FindingStatus.RESOLVED
                finding.resolution_verified = True
                finding.last_seen_scan = scan
                finding.save()
        else:
            for finding in Finding.objects.filter(pk__in=targets, check_id=check_id):
                _event(finding, source=source, to_status=finding.status, scan=scan,
                       engine_version=engine,
                       note=f"Retest {outcome}: {str(result.get('reason', ''))[:300]}. "
                       "This is not evidence of a fix.")  # fmt: skip


def _record_failure(
    scan: Scan, source: str, engine: str, result: dict[str, Any], entry: dict[str, Any]
) -> None:
    finding_type = str(result.get("finding_type") or "unauthorized_access")
    finding, created = Finding.objects.get_or_create(
        project=scan.project,
        fingerprint=f"{result['check_id']}|{finding_type}",
        defaults={
            "organization": scan.organization,
            "check_id": result["check_id"],
            "finding_type": finding_type,
            "outcome": result["outcome"],
            "details": entry,
            "first_seen_scan": scan,
            "last_seen_scan": scan,
        },
    )
    if created:
        _event(finding, source=source, to_status=FindingStatus.OPEN, scan=scan, from_status="",
               engine_version=engine,
               note=f"Found in scan #{scan.pk} ({result['outcome']}).")  # fmt: skip
        return
    previous = finding.status
    finding.details, finding.outcome, finding.last_seen_scan = entry, result["outcome"], scan
    if previous == FindingStatus.RESOLVED:
        finding.status = FindingStatus.OPEN
        finding.resolution_verified = False
        _event(finding, source=source, to_status=FindingStatus.OPEN, scan=scan,
               from_status=previous, engine_version=engine,
               note=f"Regression: the violation reappeared in scan #{scan.pk}.")  # fmt: skip
    elif source == EventSource.RETEST:
        _event(finding, source=source, to_status=previous, scan=scan, engine_version=engine,
               note=f"Retest #{scan.pk} reproduced the violation; it is not fixed.")  # fmt: skip
    finding.save()


TRIAGE_LABELS = {
    FindingStatus.CONFIRMED: Label.TRUE_POSITIVE,
    FindingStatus.REJECTED: Label.FALSE_POSITIVE,
}


def triage(finding: Finding, user: User, status: str, note: str) -> None:
    """A person's decision. Manual resolution is recorded as unverified until a retest passes."""
    if status not in FindingStatus.values:
        raise ScanError("Unknown status.")
    if status == FindingStatus.REJECTED and not note.strip():
        raise ScanError("Explain why the finding is not a problem, for example an intended share.")
    if status == finding.status:
        raise ScanError("The finding already has this status.")
    label = TRIAGE_LABELS.get(FindingStatus(status), "")
    if status == FindingStatus.RESOLVED:
        note = (
            note + " " if note else ""
        ) + "(Marked resolved by a person; not yet verified by a retest.)"
    _event(finding, source=EventSource.USER, to_status=status, actor=user, label=label, note=note)
    finding.status = status
    finding.resolution_verified = False
    finding.save()
    record(finding.organization, user, "finding.triaged", f"finding:{finding.pk}", status=status)
