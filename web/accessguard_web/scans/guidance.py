"""Deterministic fix guidance for each kind of finding. Always available, never AI-generated.

The guidance is selected from the finding's structured facts (type, check kind and the
actor's relation to the resource), so the same finding always gets the same advice. The
examples are illustrative patterns, not patches for the customer's code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Guidance:
    title: str
    why_it_matters: str
    likely_cause: str
    steps: tuple[str, ...]
    example: str
    regression_test: str


_CROSS_TENANT = Guidance(
    title="Object-level authorization is missing (broken object level authorization)",
    why_it_matters="A user of one customer can read another customer's record by asking for "
    "its id. This is the most common way multi-tenant data leaks.",
    likely_cause="The handler loads the record by id only and never compares the record's "
    "tenant with the caller's tenant.",
    steps=(
        "Load records through a query that is scoped to the caller's tenant, so records of "
        "other tenants are simply not found.",
        "Answer 404 (not 403) for records outside the caller's tenant, so ids are not confirmed.",
        "Apply the scope in one shared place (repository, query helper or policy object) "
        "instead of in each handler.",
        "Keep explicit shares as a separate, intentional rule and test them as allowed access.",
    ),
    example=(
        "# Before: any signed-in user can read any project\n"
        "project = Project.objects.get(id=project_id)\n\n"
        "# After: scoped to the caller's organisation (plus explicit shares)\n"
        "project = Project.objects.filter(\n"
        "    Q(org=request.user.org) | Q(shared_with=request.user.org), id=project_id\n"
        ").first()\n"
        "if project is None:\n"
        "    raise NotFound()"
    ),
    regression_test="Sign in as a user of another tenant, request the record's URL and assert "
    "a 404 without any of the record's fields; then rerun AccessGuard's retest.",
)

_SAME_TENANT_ROLE = Guidance(
    title="Role check is missing (function or property level authorization)",
    why_it_matters="A user with a lower role in the same organisation can read data the policy "
    "reserves for higher roles, such as billing or administrative records.",
    likely_cause="The handler checks that the caller belongs to the right organisation but not "
    "that their role is allowed to read this kind of record.",
    steps=(
        "Add an explicit role requirement to the endpoint (for example owner or admin only).",
        "Deny with 403 and an error body without any of the protected fields.",
        "Centralise role requirements (decorator, middleware or policy table) so new endpoints "
        "cannot forget them.",
    ),
    example=(
        "# After: organisation AND role are checked\n"
        "if request.user.org != org_id:\n"
        "    raise NotFound()\n"
        'if request.user.role not in {"owner"}:\n'
        '    raise PermissionDenied("Only owners can view billing")'
    ),
    regression_test="Sign in as a member with the lower role, request the endpoint and assert "
    "403 without protected fields; also assert the allowed role still gets 200.",
)

_ANONYMOUS = Guidance(
    title="Authentication is not enforced",
    why_it_matters="Anyone on the network can read the record without signing in.",
    likely_cause="The route is missing the authentication requirement, or a public route "
    "exposes a protected representation.",
    steps=(
        "Require authentication for the route (secure by default: protect all routes and "
        "list the public ones explicitly).",
        "Answer 401 without protected content for requests without valid credentials.",
    ),
    example="# Mark routes public explicitly; everything else requires authentication\n"
    "@require_authentication\ndef get_project(request, project_id): ...",
    regression_test="Send the request without credentials and assert 401 and an error body "
    "without protected fields.",
)

_LIST_LEAK = Guidance(
    title="A listing returns records the caller may not see",
    why_it_matters="Listings and search endpoints often bypass the per-record check, so one "
    "request can reveal many other customers' records at once.",
    likely_cause="The list query is not filtered by the caller's tenant (and role), or it is "
    "filtered after pagination.",
    steps=(
        "Apply the same tenant and role scope to list queries as to single-record reads, "
        "inside the database query.",
        "Filter before pagination and counting, so totals do not reveal other tenants' data.",
        "Reuse one scoped query helper for lists and details.",
    ),
    example=(
        "# After: the list uses the same scoped query as the detail view\n"
        'projects = visible_projects(request.user).order_by("name")\n'
        "return paginate(projects)"
    ),
    regression_test="List as a user of each tenant and assert that only that tenant's records "
    "(and explicit shares) appear.",
)

_OVER_RESTRICTIVE = Guidance(
    title="Legitimate access is denied",
    why_it_matters="The policy grants this user access, but the application refuses it. This "
    "often follows a fix that blocks too much, and it breaks the product for real users.",
    likely_cause="A recent authorization change ignores an explicit share or a granted role.",
    steps=(
        "Compare the endpoint's rule with the declared policy for this resource.",
        "Restore the granted access (for example explicit shares) while keeping the denial "
        "for everyone else.",
        "If the policy itself is out of date, update the access rules in AccessGuard instead.",
    ),
    example="# Keep the share rule next to the tenant rule\n"
    "visible = Q(org=user.org) | Q(shared_with=user.org)",
    regression_test="Sign in as the granted user and assert 200 with the record's fields; "
    "keep the negative test for other tenants.",
)

_SUSPECTED = (
    "This is a suspicion, not a confirmed leak: only some protected fields appeared, or the "
    "violation did not reproduce on its separate retest. Review the evidence and retest."
)


def guidance_for(finding: dict[str, Any]) -> Guidance:
    """Select guidance from a report finding entry (see docs/report-schema.md)."""
    if finding.get("type") == "expected_access_denied":
        return _OVER_RESTRICTIVE
    subject = finding.get("subject") or {}
    if subject.get("type") == "collection":
        return _LIST_LEAK
    actor = finding.get("actor") or {}
    if actor.get("id") == "anonymous":
        return _ANONYMOUS
    if actor.get("tenant") and actor.get("tenant") == subject.get("owner_tenant"):
        return _SAME_TENANT_ROLE
    return _CROSS_TENANT


def suspicion_note(finding: dict[str, Any]) -> str | None:
    return _SUSPECTED if finding.get("outcome") == "SUSPECTED" else None
