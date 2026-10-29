"""Dashboard, project setup (target, test users, rules, limits), OpenAPI import, spec export."""

from __future__ import annotations

import json
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accessguard_web.accounts.models import Role, record
from accessguard_web.accounts.tenancy import (
    current_membership,
    current_org,
    current_user,
    get_owned,
    membership_required,
)
from accessguard_web.assistant.models import AssistantKind
from accessguard_web.assistant.service import AssistantUnavailable, run_task
from accessguard_web.assistant.tasks import (
    SETUP_PROMPT_VERSION,
    SETUP_SCHEMA,
    SETUP_SYSTEM,
    apply_setup_answer,
    setup_user_message,
    validate_setup_answer,
)
from accessguard_web.billing.entitlements import LimitExceeded, require_capacity
from accessguard_web.projects import config as cfg
from accessguard_web.projects.demo import demo_is_fixed, set_demo_variant
from accessguard_web.projects.forms import (
    CollectionFormSet,
    IdentityFormSet,
    LimitsForm,
    OpenApiImportForm,
    ProjectForm,
    ResourceFormSet,
    TargetForm,
    resource_initial,
)
from accessguard_web.projects.models import Project
from accessguard_web.projects.openapi import (
    Draft,
    OpenApiError,
    catalogue,
    deterministic_draft,
    parse_document,
)
from accessguard_web.scans.models import Finding, FindingStatus, Runner, Scan


def _project(request: HttpRequest, project_id: int) -> Project:
    return get_owned(Project.objects.all(), current_org(request), pk=project_id)


@membership_required()
def dashboard(request: HttpRequest) -> HttpResponse:
    org = current_org(request)
    projects = Project.objects.filter(organization=org).annotate(
        open_findings=Count(
            "findings", filter=Q(findings__status__in=[FindingStatus.OPEN, FindingStatus.CONFIRMED])
        )
    )
    demo = projects.filter(is_demo=True).first()
    own = [p for p in projects if not p.is_demo]
    runners = Runner.objects.filter(organization=org, revoked_at__isnull=True)
    onboarding = [
        ("Run the demo scan", Scan.objects.filter(organization=org, is_demo=True).exists(),
         reverse("projects:detail", args=[demo.pk]) if demo else ""),
        ("Create a project for your test environment", bool(own), reverse("projects:new")),
        ("Install a runner", runners.exists(), reverse("scans:runners")),
        ("Run your first scan", Scan.objects.filter(organization=org, is_demo=False).exists(),
         reverse("projects:detail", args=[own[0].pk]) if own else reverse("projects:new")),
    ]  # fmt: skip
    return render(
        request,
        "projects/dashboard.html",
        {
            "projects": projects,
            "recent_scans": Scan.objects.filter(organization=org).select_related("project")[:8],
            "onboarding": onboarding,
            "onboarding_done": all(done for _, done, _ in onboarding),
        },
    )


@membership_required(Role.ADMIN)
def project_new(request: HttpRequest) -> HttpResponse:
    org = current_org(request)
    form = ProjectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            require_capacity(
                org, "projects", Project.objects.filter(organization=org, is_demo=False).count()
            )
        except LimitExceeded as error:
            messages.error(request, str(error))
            return redirect("billing:overview")
        name = form.cleaned_data["name"]
        project = Project.objects.create(organization=org, name=name, config=cfg.empty_config(name))
        record(org, current_user(request), "project.created", f"project:{project.pk}")
        return redirect("projects:setup_target", project.pk)
    return render(request, "projects/new.html", {"form": form})


@membership_required()
def detail(request: HttpRequest, project_id: int) -> HttpResponse:
    project = _project(request, project_id)
    status = cfg.evaluate(project.config)
    findings = Finding.objects.filter(project=project)
    return render(
        request,
        "projects/detail.html",
        {
            "project": project,
            "status": status,
            "scans": Scan.objects.filter(project=project)[:10],
            "open_findings": findings.filter(
                status__in=[FindingStatus.OPEN, FindingStatus.CONFIRMED]
            ),
            "resolved_count": findings.filter(status=FindingStatus.RESOLVED).count(),
            "demo_fixed": demo_is_fixed(project) if project.is_demo else False,
            "can_edit": current_membership(request).has_role(Role.ADMIN),
            "runner_online": Runner.objects.filter(
                Q(organization=project.organization) | Q(is_platform_demo=project.is_demo),
                revoked_at__isnull=True,
            ).exists(),
        },
    )


def _guard_editable(request: HttpRequest, project: Project) -> HttpResponse | None:
    """Changes need the admin role; the demo configuration is never editable."""
    if not current_membership(request).has_role(Role.ADMIN):
        raise PermissionDenied("Changing the configuration needs the admin role")
    if project.is_demo:
        messages.info(request, "The demo configuration is fixed so the demo always works.")
        return redirect("projects:detail", project.pk)
    return None


def _save(request: HttpRequest, project: Project, what: str) -> None:
    project.save(update_fields=["config", "updated_at"])
    record(
        project.organization,
        current_user(request),
        f"project.{what}_updated",
        f"project:{project.pk}",
    )
    status = cfg.evaluate(project.config)
    if status.error and all(step.done for step in status.steps):
        messages.warning(request, f"Saved, but the configuration is not valid yet: {status.error}")
    else:
        messages.success(request, "Saved.")


def _setup_context(project: Project, **extra: Any) -> dict[str, Any]:
    return {"project": project, "status": cfg.evaluate(project.config), **extra}


@membership_required()
def setup_target(request: HttpRequest, project_id: int) -> HttpResponse:
    project = _project(request, project_id)
    target = project.config["target"]
    probe = project.config["policy"]["session_probe"]
    auth = target.get("auth", {})
    initial = {
        "origin": target.get("origin", ""),
        "path_prefixes": "\n".join(target.get("path_prefixes", ["/"])),
        "auth_kind": auth.get("kind", "bearer"),
        "auth_name": auth.get("name", "") if auth.get("kind") != "bearer" else "",
        "session_path": probe.get("path", ""),
        "subject_field": probe.get("subject_field", ""),
    }
    form = TargetForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if (blocked := _guard_editable(request, project)) is not None:
            return blocked
        if form.is_valid():
            data = form.cleaned_data
            kind = data["auth_kind"]
            project.config["target"] = {
                "origin": data["origin"],
                "path_prefixes": data["prefixes"],
                "auth": {"kind": kind}
                if kind == "bearer"
                else {"kind": kind, "name": data["auth_name"]},
            }
            project.config["policy"]["session_probe"] = {
                "path": data["session_path"],
                "subject_field": data["subject_field"],
            }
            _save(request, project, "target")
            return redirect("projects:setup_users", project.pk)
    return render(request, "projects/setup_target.html", _setup_context(project, form=form))


@membership_required()
def setup_users(request: HttpRequest, project_id: int) -> HttpResponse:
    project = _project(request, project_id)
    initial = [
        {k: i.get(k, "") for k in ("id", "tenant", "role", "subject")}
        for i in project.config["policy"]["identities"]
    ]
    formset = IdentityFormSet(request.POST or None, initial=initial, prefix="users")
    if request.method == "POST":
        if (blocked := _guard_editable(request, project)) is not None:
            return blocked
        if formset.is_valid():
            identities = []
            for form in formset:
                data = form.cleaned_data
                if not data.get("id") or data.get("DELETE"):
                    continue
                entry = {"id": data["id"], "tenant": data["tenant"], "role": data["role"]}
                if data.get("subject"):
                    entry["subject"] = data["subject"]
                identities.append(entry)
            project.config["policy"]["identities"] = identities
            _save(request, project, "users")
            return redirect("projects:setup_rules", project.pk)
    return render(request, "projects/setup_users.html", _setup_context(project, formset=formset))


@membership_required()
def setup_rules(request: HttpRequest, project_id: int) -> HttpResponse:
    project = _project(request, project_id)
    policy = project.config["policy"]
    resources_initial = resource_initial(policy["resources"])
    template_index = request.GET.get("template")
    if template_index is not None and template_index.isdigit():
        index = int(template_index)
        if 0 <= index < len(project.endpoint_templates):
            template = project.endpoint_templates[index]
            resources_initial.append(
                {"path": template["path"], "category": template["category"],
                 "protected_fields": ", ".join(template.get("protected_fields", []))}
            )  # fmt: skip
    collections = CollectionFormSet(
        request.POST or None,
        initial=[{"id": c["id"], "path": c["path"]} for c in policy["collections"]],
        prefix="lists",
    )
    resources = ResourceFormSet(
        request.POST or None,
        initial=resources_initial,
        prefix="records",
        form_kwargs={"config": project.config},
    )
    if request.method == "POST":
        if (blocked := _guard_editable(request, project)) is not None:
            return blocked
        if collections.is_valid() and resources.is_valid():
            policy["collections"] = [
                {"id": f.cleaned_data["id"], "path": f.cleaned_data["path"]}
                for f in collections
                if f.cleaned_data.get("id") and not f.cleaned_data.get("DELETE")
            ]
            known = {c["id"] for c in policy["collections"]}
            policy["resources"] = [
                {
                    "id": d["id"],
                    "category": d["category"],
                    "path": d["path"],
                    "owner": d["owner"],
                    "protected_fields": d["fields_list"],
                    "grants": d["grants"],
                    **({"collection": d["collection"]} if d.get("collection") in known else {}),
                }
                for f in resources
                if (d := f.cleaned_data).get("id") and not d.get("DELETE")
            ]
            _save(request, project, "rules")
            return redirect("projects:setup_limits", project.pk)
    return render(
        request,
        "projects/setup_rules.html",
        _setup_context(project, collections=collections, resources=resources,
                       templates=list(enumerate(project.endpoint_templates))),
    )  # fmt: skip


@membership_required()
def setup_limits(request: HttpRequest, project_id: int) -> HttpResponse:
    project = _project(request, project_id)
    status = cfg.evaluate(project.config)
    initial = dict(project.config["limits"])
    if status.ready and initial.get("max_requests") == cfg.DEFAULT_LIMITS["max_requests"]:
        initial["max_requests"] = status.recommended_requests
    form = LimitsForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if (blocked := _guard_editable(request, project)) is not None:
            return blocked
        if form.is_valid():
            project.config["limits"] = {
                "max_requests": form.cleaned_data["max_requests"],
                "min_interval_seconds": form.cleaned_data["min_interval_seconds"],
                "max_duration_seconds": form.cleaned_data["max_duration_seconds"],
            }
            _save(request, project, "limits")
            return redirect("projects:detail", project.pk)
    return render(request, "projects/setup_limits.html", _setup_context(project, form=form))


@membership_required(Role.ADMIN)
def import_openapi(request: HttpRequest, project_id: int) -> HttpResponse:
    project = _project(request, project_id)
    if (blocked := _guard_editable(request, project)) is not None:
        return blocked
    form = OpenApiImportForm(request.POST or None, request.FILES or None)
    draft: Draft | None = None
    assistant_note = ""
    if request.method == "POST" and form.is_valid():
        try:
            operations = catalogue(parse_document(form.raw()))
        except OpenApiError as error:
            form.add_error(None, str(error))
        else:
            draft = deterministic_draft(operations)
            if form.cleaned_data["use_assistant"]:
                try:
                    run = run_task(
                        kind=AssistantKind.SETUP_DRAFT,
                        organization=project.organization,
                        user=current_user(request),
                        system=SETUP_SYSTEM,
                        message=setup_user_message(operations),
                        schema=SETUP_SCHEMA,
                        validate=validate_setup_answer,
                        prompt_version=SETUP_PROMPT_VERSION,
                        max_tokens=8000,
                        project=project,
                    )
                except AssistantUnavailable as error:
                    assistant_note = str(error)
                else:
                    if run.result is not None:
                        draft = apply_setup_answer(draft, operations, run.result)
                        assistant_note = (
                            f"Suggestions by {run.model} ({run.prompt_version}). Review them."
                        )
                    else:
                        assistant_note = (
                            f"The setup assistant failed: {run.error}. "
                            "The draft below is rule-based."
                        )
            request.session[f"openapi_draft_{project.pk}"] = draft.as_dict()
    return render(
        request,
        "projects/import_openapi.html",
        {"project": project, "form": form, "draft": draft, "assistant_note": assistant_note},
    )


@require_POST
@membership_required(Role.ADMIN)
def apply_openapi(request: HttpRequest, project_id: int) -> HttpResponse:
    project = _project(request, project_id)
    if (blocked := _guard_editable(request, project)) is not None:
        return blocked
    draft = request.session.pop(f"openapi_draft_{project.pk}", None)
    if not isinstance(draft, dict):
        messages.error(request, "The import expired; please import the document again.")
        return redirect("projects:import_openapi", project.pk)
    chosen = set(request.POST.getlist("templates"))
    chosen_lists = set(request.POST.getlist("collections"))
    target = project.config["target"]
    if request.POST.get("apply_prefix") and draft["path_prefix"]:
        target["path_prefixes"] = [draft["path_prefix"]]
    if request.POST.get("apply_session") and draft["session_probe_path"]:
        project.config["policy"]["session_probe"] = {
            "path": draft["session_probe_path"],
            "subject_field": draft["subject_field"],
        }
    existing = {c["id"] for c in project.config["policy"]["collections"]}
    for collection in draft["collections"]:
        if collection["path"] in chosen_lists and collection["id"] not in existing:
            project.config["policy"]["collections"].append(
                {"id": collection["id"], "path": collection["path"]}
            )
    project.endpoint_templates = [t for t in draft["templates"] if t["path"] in chosen]
    project.save(update_fields=["config", "endpoint_templates", "updated_at"])
    record(
        project.organization,
        current_user(request),
        "project.openapi_applied",
        f"project:{project.pk}",
    )
    messages.success(
        request,
        "Applied. Now add concrete records: pick an endpoint template and fill in an id "
        "from your test data.",
    )
    return redirect("projects:setup_rules", project.pk)


@membership_required()
def export_spec(request: HttpRequest, project_id: int) -> HttpResponse:
    project = _project(request, project_id)
    response = HttpResponse(
        json.dumps(cfg.spec_document(project.config), indent=2), content_type="application/json"
    )
    response["Content-Disposition"] = f'attachment; filename="accessguard-spec-{project.pk}.json"'
    return response


@require_POST
@membership_required(Role.ADMIN)
def demo_variant(request: HttpRequest, project_id: int) -> HttpResponse:
    project = _project(request, project_id)
    if not project.is_demo:
        return redirect("projects:detail", project.pk)
    fixed = request.POST.get("variant") == "fixed"
    set_demo_variant(project, fixed=fixed)
    messages.success(
        request,
        "The demo API now runs the fixed version. Retest the open findings to verify the fix."
        if fixed
        else "The demo API runs the flawed version again.",
    )
    return redirect("projects:detail", project.pk)


@require_POST
@membership_required(Role.ADMIN)
def delete(request: HttpRequest, project_id: int) -> HttpResponse:
    project = _project(request, project_id)
    if project.is_demo or request.POST.get("confirm") != project.name:
        messages.error(request, "Type the project name to confirm deletion.")
        return redirect("projects:detail", project.pk)
    record(project.organization, current_user(request), "project.deleted", project.name)
    project.delete()
    messages.success(request, "The project, its scans and findings were deleted.")
    return redirect("dashboard")
