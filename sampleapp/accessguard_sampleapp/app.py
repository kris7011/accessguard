"""Routing and access rules of the sample API, independent of any HTTP server.

Routes (GET only; every other method answers 405):

* ``/healthz``                       liveness, no authentication
* ``/api/v1/whoami``                 the authenticated user (``{"data": {"email", ...}}``)
* ``/api/v1/projects``               projects visible to the caller
* ``/api/v1/projects/{id}``          one project: own organisation, or shared with it
* ``/api/v1/orgs/{org}/billing``     billing details: organisation owners only

The rules are the intended policy. :class:`Variant.FLAWED` breaks three of them
(see :data:`FLAWS`); :class:`Variant.FIXED` implements all of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from accessguard_sampleapp.data import BILLING, PROJECTS, Project, User


class Variant(StrEnum):
    FLAWED = "flawed"
    FIXED = "fixed"


FLAWS = {
    "project-idor": "GET /api/v1/projects/{id} returns any project to any signed-in user.",
    "billing-role-bypass": "GET /api/v1/orgs/{org}/billing checks the organisation, not the role.",
    "project-list-leak": "GET /api/v1/projects lists the projects of every organisation.",
}
"""The deliberate bugs of the flawed variant, by name."""

Response = tuple[int, dict[str, object]]


def _error(status: int, code: str, message: str) -> Response:
    return status, {"error": {"code": code, "message": message}}


def _project_body(project: Project) -> dict[str, object]:
    return {
        "id": project.id,
        "org": project.org,
        "name": project.name,
        "budget_code": project.budget_code,
        "shared_with": sorted(project.shared_with),
    }


@dataclass(frozen=True)
class SampleApplication:
    """Answers requests for one variant. ``users_by_key`` maps API keys to users."""

    variant: Variant
    users_by_key: Mapping[str, User]

    def handle(self, method: str, path: str, api_key: str | None) -> Response:
        if method != "GET":
            return _error(405, "method_not_allowed", "Only GET is supported")
        if path == "/healthz":
            return 200, {"status": "ok"}
        user = self.users_by_key.get(api_key or "")
        if user is None:
            return _error(401, "unauthenticated", "A valid X-Api-Key header is required")
        parts = path.strip("/").split("/")
        if parts == ["api", "v1", "whoami"]:
            return 200, {"data": {"email": user.email, "org": user.org, "role": user.role}}
        if parts == ["api", "v1", "projects"]:
            return self._list_projects(user)
        if len(parts) == 4 and parts[:3] == ["api", "v1", "projects"]:
            return self._project(user, parts[3])
        if len(parts) == 5 and parts[:3] == ["api", "v1", "orgs"] and parts[4] == "billing":
            return self._billing(user, parts[3])
        return _error(404, "not_found", "No such route")

    def _can_see(self, user: User, project: Project) -> bool:
        return project.org == user.org or user.org in project.shared_with

    def _list_projects(self, user: User) -> Response:
        if self.variant is Variant.FLAWED:
            visible = list(PROJECTS.values())  # bug: project-list-leak
        else:
            visible = [p for p in PROJECTS.values() if self._can_see(user, p)]
        items = [_project_body(p) for p in visible]
        return 200, {"data": items, "page": 1, "total": len(items)}

    def _project(self, user: User, project_id: str) -> Response:
        project = PROJECTS.get(project_id)
        if project is None:
            return _error(404, "not_found", "Project not found")
        if self.variant is Variant.FIXED and not self._can_see(user, project):
            # Not found rather than forbidden, so other organisations' ids are not confirmed.
            return _error(404, "not_found", "Project not found")
        # bug in the flawed variant: project-idor (no organisation check at all)
        return 200, {"data": _project_body(project)}

    def _billing(self, user: User, org: str) -> Response:
        billing = BILLING.get(org)
        if billing is None or user.org != org:
            return _error(404, "not_found", "Organisation not found")
        if self.variant is Variant.FIXED and user.role != "owner":
            return _error(403, "forbidden", "Only organisation owners can view billing")
        # bug in the flawed variant: billing-role-bypass (viewers pass)
        return 200, {
            "data": {
                "org": org,
                "account_ref": billing.account_ref,
                "plan_note": billing.plan_note,
            }
        }
