"""Turns scans and reports into what the UI shows. The one place that decides the banner.

Rules (product requirement): a partial scan, an invalid session, a retest subset or a
missing report is never shown as evidence that an application is secure. Green is only
used for a completed full-matrix scan whose report says every check passed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from accessguard_web.scans.guidance import Guidance, guidance_for
from accessguard_web.scans.models import Scan, ScanKind, ScanStatus

RELATION_LABELS = {
    "owner": "Owner reads own record",
    "same_tenant": "Other users of the same tenant",
    "shared_grant": "Explicitly shared with another tenant",
    "cross_tenant": "Users of other tenants",
    "anonymous": "No credentials (anonymous)",
}
OUTCOME_LABELS = {
    "PASS": "Passed",
    "FAIL": "Violation (confirmed evidence)",
    "SUSPECTED": "Suspected violation",
    "INCONCLUSIVE": "Inconclusive",
    "SKIPPED": "Not run",
    "ERROR": "Technical error",
}


@dataclass(frozen=True)
class Banner:
    tone: str  # "pass", "fail", "warn", "info", "neutral"
    title: str
    text: str


@dataclass(frozen=True)
class FindingRow:
    check_id: str
    outcome: str
    title: str
    actor: str
    subject: str
    actual: str
    verification: str
    guidance: Guidance


@dataclass(frozen=True)
class ReportView:
    banner: Banner
    summary: dict[str, int]
    planned: int
    conclusive: int
    conclusive_percent: int
    findings: list[FindingRow]
    invalid_sessions: list[tuple[str, str]]
    coverage_rows: list[tuple[str, dict[str, int]]]
    not_tested: list[str]
    results: list[dict[str, Any]]
    interrupted: str | None
    disclaimer: str
    requests_used: int
    max_requests: int
    selection_note: str | None
    attention: list[str] = field(default_factory=list)


def banner_for(scan: Scan) -> Banner:
    if scan.status == ScanStatus.QUEUED:
        return Banner("neutral", "Waiting for a runner", "The scan starts when a runner claims it.")
    if scan.status in (ScanStatus.RUNNING, ScanStatus.CANCEL_REQUESTED):
        return Banner("neutral", "Scan in progress", "Results appear when the runner reports back.")
    report = scan.report
    if report is None:
        reason = scan.failure_message or "No report was received."
        title = {
            ScanStatus.CANCELLED: "Cancelled: no results",
            ScanStatus.LOST: "Runner stopped responding: no results",
        }.get(ScanStatus(scan.status), "Scan failed: no results")
        return Banner("neutral", title, f"{reason} This says nothing about the target's security.")
    summary = report.get("summary", {})
    fails, suspected = summary.get("FAIL", 0), summary.get("SUSPECTED", 0)
    if fails:
        extra = f" and {suspected} suspected" if suspected else ""
        return Banner(
            "fail",
            f"{fails} confirmed access-control violation{'s' if fails != 1 else ''}{extra}",
            "The target behaved differently from the access rules you declared. Each finding "
            "below has evidence, reproduction steps and fix guidance.",
        )
    if suspected:
        return Banner("warn", f"{suspected} suspected violation(s) need review",
                      "No confirmed violation, but some evidence points to one.")  # fmt: skip
    if scan.status == ScanStatus.CANCELLED or not scan.report_complete:
        return Banner(
            "warn",
            "Incomplete scan: not a security approval",
            "The scan ended before every planned check produced a result. Checks that did not "
            "run or were inconclusive tell you nothing about the target.",
        )
    if scan.report_status == "incomplete":
        return Banner(
            "warn",
            "Incomplete results: not a security approval",
            "Some checks were inconclusive (for example an invalid test session, a server error "
            "or an unreadable response). Fix the causes listed below and scan again.",
        )
    if scan.kind == ScanKind.RETEST:
        return Banner(
            "info",
            f"Retest passed for the {scan.planned_checks} selected checks",
            "Only the retested checks and their positive controls ran. Run a full scan to "
            "check everything else.",
        )
    if scan.shows_as_passed:
        return Banner(
            "pass",
            f"All {scan.planned_checks} declared checks passed",
            "This covers only the access rules, users and endpoints you declared. It is not a "
            "security certification.",
        )
    return Banner("neutral", "Results", "")


def report_view(scan: Scan) -> ReportView | None:
    report = scan.report
    if report is None:
        return None
    coverage = report.get("coverage", {})
    planned = int(coverage.get("planned_checks", 0))
    conclusive = int(coverage.get("conclusive_checks", 0))
    sessions = report.get("sessions", {})
    invalid = [(i, s.get("reason", "")) for i, s in sessions.items() if s.get("state") == "invalid"]
    findings = []
    for entry in report.get("findings", []):
        actor = entry.get("actor") or {}
        subject = entry.get("subject") or {}
        who = (
            "anonymous request"
            if actor.get("id") == "anonymous"
            else (f"{actor.get('id')} ({actor.get('tenant')} / {actor.get('role')})")
        )
        guidance = guidance_for(entry)
        findings.append(
            FindingRow(
                check_id=entry.get("id", ""),
                outcome=entry.get("outcome", ""),
                title=guidance.title,
                actor=who,
                subject=f"{subject.get('category', subject.get('type', ''))} "
                f"{subject.get('id', '')}",
                actual=entry.get("actual", ""),
                verification=entry.get("verification") or "",
                guidance=guidance,
            )
        )
    attention = []
    if invalid:
        attention.append(
            f"{len(invalid)} test user session(s) could not be verified; "
            "their checks are inconclusive."
        )
    counts = Counter(r.get("outcome") for r in report.get("results", []))
    if counts.get("ERROR"):
        attention.append(
            f"{counts['ERROR']} checks hit technical errors (timeouts, connection errors)."
        )
    run = report.get("run", {})
    selection = run.get("selection") or {}
    return ReportView(
        banner=banner_for(scan),
        summary=report.get("summary", {}),
        planned=planned,
        conclusive=conclusive,
        conclusive_percent=round(100 * conclusive / planned) if planned else 0,
        findings=findings,
        invalid_sessions=invalid,
        coverage_rows=[
            (RELATION_LABELS.get(k, k), v) for k, v in (coverage.get("by_relation") or {}).items()
        ],
        not_tested=list(report.get("not_tested", [])),
        results=[
            {**r, "outcome_label": OUTCOME_LABELS.get(r.get("outcome", ""), r.get("outcome"))}
            for r in report.get("results", [])
        ],
        interrupted=run.get("interrupted"),
        disclaimer=str(report.get("disclaimer", "")),
        requests_used=int(run.get("requests_used", 0)),
        max_requests=int((run.get("limits") or {}).get("max_requests", 0)),
        selection_note=(
            f"Retest of {selection.get('selected_checks')} of "
            f"{selection.get('full_matrix_checks')} checks"
            if selection.get("mode") == "subset"
            else None
        ),
        attention=attention,
    )


def progress_view(scan: Scan) -> dict[str, Any]:
    progress = scan.progress or {}
    planned = progress.get("planned") or scan.planned_checks
    completed = progress.get("completed", 0)
    phase = progress.get("phase", "")
    percent = round(100 * completed / planned) if planned else 0
    if phase == "retests":
        text = f"Retesting findings ({completed} of {planned})"
    elif phase in ("checks", "done"):
        text = f"{completed} of {planned} checks done"
    else:
        text = "Starting: verifying test sessions"
    return {
        "status": scan.status,
        "status_label": scan.get_status_display(),
        "active": scan.is_active,
        "percent": min(percent, 100),
        "text": text,
        "requests_used": progress.get("requests_used", 0),
    }
