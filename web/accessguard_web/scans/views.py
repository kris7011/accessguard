"""Scan review and approval, live status, cancellation, findings, triage, retests, runners."""

from __future__ import annotations

import json

from django.contrib import messages
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accessguard_web.accounts.models import Role, record
from accessguard_web.accounts.tenancy import (
    current_membership,
    current_org,
    current_user,
    get_owned,
    membership_required,
)
from accessguard_web.assistant.models import AssistantKind, AssistantOutcome, AssistantRun
from accessguard_web.assistant.providers import configured_provider
from accessguard_web.assistant.service import AssistantUnavailable, availability, run_task
from accessguard_web.assistant.tasks import (
    EXPLAIN_PROMPT_VERSION,
    EXPLAIN_SCHEMA,
    EXPLAIN_SYSTEM,
    explain_user_message,
    validate_explanation,
)
from accessguard_web.billing.entitlements import LimitExceeded, require_capacity
from accessguard_web.projects.config import evaluate
from accessguard_web.projects.models import Project
from accessguard_web.scans import services
from accessguard_web.scans.distribution import runner_wheel
from accessguard_web.scans.guidance import guidance_for, suspicion_note
from accessguard_web.scans.models import Finding, FindingStatus, Runner, Scan, ScanStatus
from accessguard_web.scans.presenters import banner_for, progress_view, report_view


def _scan(request: HttpRequest, scan_id: int) -> Scan:
    return get_owned(Scan.objects.select_related("project"), current_org(request), pk=scan_id)


@membership_required(Role.ADMIN)
def review(request: HttpRequest, project_id: int) -> HttpResponse:
    """Shows exactly what will be sent and asks for an explicit authorisation."""
    project = get_owned(Project.objects.all(), current_org(request), pk=project_id)
    finding_ids = [
        int(i)
        for i in request.GET.getlist("finding") + request.POST.getlist("finding")
        if i.isdigit()
    ]
    findings = list(Finding.objects.filter(project=project, pk__in=finding_ids))
    try:
        checks = (
            services.with_positive_controls(project, [f.check_id for f in findings])
            if findings
            else None
        )
        summary = services.scope_summary(project, checks)
    except services.ScanError as error:
        messages.error(request, str(error))
        return redirect("projects:detail", project.pk)
    if request.method == "POST":
        if request.POST.get("authorised") != "yes":
            messages.error(request, "Confirm that you are authorised to test this target.")
        else:
            try:
                scan = (
                    services.create_retest(project, current_user(request), findings)
                    if findings
                    else services.approve_scan(project, current_user(request))
                )
            except services.ScanError as error:
                messages.error(request, str(error))
            else:
                return redirect("scans:detail", scan.pk)
    runners = Runner.objects.filter(revoked_at__isnull=True).filter(
        Q(is_platform_demo=True) if project.is_demo else Q(organization=project.organization)
    )
    return render(
        request,
        "scans/review.html",
        {
            "project": project,
            "summary": summary,
            "status": evaluate(project.config),
            "findings": findings,
            "attestation": services.ATTESTATION,
            "runners": runners,
        },
    )


@membership_required()
def detail(request: HttpRequest, scan_id: int) -> HttpResponse:
    scan = _scan(request, scan_id)
    services.sweep_lost_scans()
    scan.refresh_from_db()
    waiting_long = (
        scan.status == ScanStatus.QUEUED and (timezone.now() - scan.created_at).total_seconds() > 60
    )
    return render(
        request,
        "scans/detail.html",
        {
            "scan": scan,
            "progress": progress_view(scan),
            "report": report_view(scan),
            "banner": banner_for(scan),
            "waiting_long": waiting_long,
        },
    )


@membership_required()
def status_json(request: HttpRequest, scan_id: int) -> JsonResponse:
    scan = _scan(request, scan_id)
    services.sweep_lost_scans()
    scan.refresh_from_db()
    return JsonResponse(progress_view(scan))


@require_POST
@membership_required(Role.ADMIN)
def cancel(request: HttpRequest, scan_id: int) -> HttpResponse:
    scan = _scan(request, scan_id)
    try:
        services.cancel_scan(scan, current_user(request))
    except services.ScanError as error:
        messages.error(request, str(error))
    else:
        messages.info(request, "Stopping the scan. Results gathered so far will be kept.")
    return redirect("scans:detail", scan.pk)


@membership_required()
def report_json(request: HttpRequest, scan_id: int) -> HttpResponse:
    scan = _scan(request, scan_id)
    if scan.report is None:
        raise Http404("No report")
    response = HttpResponse(json.dumps(scan.report, indent=2), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="accessguard-scan-{scan.pk}.json"'
    return response


@membership_required()
def findings(request: HttpRequest) -> HttpResponse:
    org = current_org(request)
    status = request.GET.get("status", "open")
    queryset = Finding.objects.filter(organization=org).select_related("project")
    if status == "open":
        queryset = queryset.filter(status__in=[FindingStatus.OPEN, FindingStatus.CONFIRMED])
    elif status in FindingStatus.values:
        queryset = queryset.filter(status=status)
    project_id = request.GET.get("project", "")
    if project_id.isdigit():
        queryset = queryset.filter(project_id=int(project_id))
    rows = [(f, guidance_for(f.details)) for f in queryset[:500]]
    return render(
        request,
        "scans/findings.html",
        {
            "rows": rows,
            "status": status,
            "statuses": FindingStatus.choices,
            "projects": Project.objects.filter(organization=org),
            "project_id": project_id,
        },
    )


@membership_required()
def finding_detail(request: HttpRequest, finding_id: int) -> HttpResponse:
    finding = get_owned(
        Finding.objects.select_related("project"), current_org(request), pk=finding_id
    )
    explanation = AssistantRun.objects.filter(finding=finding, outcome=AssistantOutcome.OK).first()
    return render(
        request,
        "scans/finding_detail.html",
        {
            "finding": finding,
            "guidance": guidance_for(finding.details),
            "suspicion": suspicion_note(finding.details),
            "events": finding.events.select_related("actor", "scan"),
            "statuses": FindingStatus.choices,
            "explanation": explanation,
            "assistant_unavailable": availability(finding.organization, configured_provider()),
            "can_edit": current_membership(request).has_role(Role.ADMIN),
        },
    )


@require_POST
@membership_required(Role.ADMIN)
def triage(request: HttpRequest, finding_id: int) -> HttpResponse:
    finding = get_owned(Finding.objects.all(), current_org(request), pk=finding_id)
    try:
        services.triage(finding, current_user(request), request.POST.get("status", ""),
                        request.POST.get("note", "")[:1000])  # fmt: skip
    except services.ScanError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Status updated.")
    return redirect("scans:finding", finding.pk)


@require_POST
@membership_required(Role.ADMIN)
def explain(request: HttpRequest, finding_id: int) -> HttpResponse:
    finding = get_owned(Finding.objects.all(), current_org(request), pk=finding_id)
    try:
        run = run_task(
            kind=AssistantKind.EXPLAIN_FINDING,
            organization=finding.organization,
            user=current_user(request),
            system=EXPLAIN_SYSTEM,
            message=explain_user_message(finding.details, request.POST.get("code", "")),
            schema=EXPLAIN_SCHEMA,
            validate=validate_explanation,
            prompt_version=EXPLAIN_PROMPT_VERSION,
            max_tokens=6000,
            finding=finding,
        )
    except AssistantUnavailable as error:
        messages.error(request, str(error))
    else:
        if run.outcome == AssistantOutcome.OK:
            messages.success(
                request, "The assistant wrote an explanation. Review it before acting on it."
            )
        else:
            messages.error(request, f"The assistant could not help this time: {run.error}")
    return redirect("scans:finding", finding.pk)


def _runners_page(request: HttpRequest, new_token: str | None = None) -> HttpResponse:
    org = current_org(request)
    response = render(
        request,
        "scans/runners.html",
        {
            "runners": Runner.objects.filter(organization=org).order_by("-created_at"),
            "new_token": new_token,
            "public_url": request.build_absolute_uri("/").rstrip("/"),
            "projects": Project.objects.filter(organization=org, is_demo=False),
            "runner_wheel": runner_wheel(),
        },
    )
    if new_token:
        response["Cache-Control"] = "no-store"
    return response


@membership_required(Role.ADMIN)
def runners(request: HttpRequest) -> HttpResponse:
    return _runners_page(request)


@membership_required()
def runner_download(request: HttpRequest) -> HttpResponse:
    wheel = runner_wheel()
    if wheel is None:
        raise Http404("No runner download is configured on this server")
    # The wheel is small (the engine only, no dependencies), so it is sent in one piece.
    response = HttpResponse(wheel.path.read_bytes(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{wheel.name}"'
    return response


@require_POST
@membership_required(Role.ADMIN)
def runner_create(request: HttpRequest) -> HttpResponse:
    org = current_org(request)
    name = (request.POST.get("name") or "Runner").strip()[:80]
    try:
        require_capacity(
            org, "runners", Runner.objects.filter(organization=org, revoked_at__isnull=True).count()
        )
    except LimitExceeded as error:
        messages.error(request, str(error))
        return redirect("scans:runners")
    runner, token = Runner.issue(name=name, organization=org)
    record(org, current_user(request), "runner.created", f"runner:{runner.pk}")
    # The token is shown exactly once in this response; only its hash is stored.
    return _runners_page(request, new_token=token)


@require_POST
@membership_required(Role.ADMIN)
def runner_revoke(request: HttpRequest, runner_id: int) -> HttpResponse:
    runner = get_owned(Runner.objects.all(), current_org(request), pk=runner_id)
    runner.revoked_at = timezone.now()
    runner.save(update_fields=["revoked_at"])
    record(current_org(request), current_user(request), "runner.revoked", f"runner:{runner.pk}")
    messages.success(request, f"Runner {runner.name} can no longer claim scans.")
    return redirect("scans:runners")
