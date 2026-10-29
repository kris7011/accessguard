"""Organisation context and role checks: the single place that enforces tenant isolation.

Every view that touches product data resolves the current membership with
:func:`membership_required` and loads records only through :func:`get_owned`, which
filters by organisation. A record of another organisation is therefore indistinguishable
from a missing one (HTTP 404), so ids of other tenants are not confirmed.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db.models import Model, QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect

from accessguard_web.accounts.models import Membership, Organization, Role, User

SESSION_KEY = "accessguard_org_id"
M = TypeVar("M", bound=Model)
View = Callable[..., HttpResponse]


def memberships(user: User) -> QuerySet[Membership]:
    return Membership.objects.filter(user=user).select_related("organization").order_by("id")


def resolve_membership(request: HttpRequest) -> Membership | None:
    user = request.user
    if not user.is_authenticated:
        return None
    assert isinstance(user, User)  # noqa: S101 - AUTH_USER_MODEL guarantees it
    wanted = request.session.get(SESSION_KEY)
    available = memberships(user)
    membership = available.filter(organization_id=wanted).first() if wanted else None
    return membership or available.first()


def switch_organization(request: HttpRequest, organization: Organization) -> None:
    request.session[SESSION_KEY] = organization.pk


def current_membership(request: HttpRequest) -> Membership:
    membership = getattr(request, "membership", None)
    if not isinstance(membership, Membership):
        raise PermissionDenied("No organisation selected")
    return membership


def current_org(request: HttpRequest) -> Organization:
    return current_membership(request).organization


def current_user(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User):
        raise PermissionDenied("Sign in required")
    return user


def membership_required(role: str = Role.VIEWER) -> Callable[[View], View]:
    """Require a signed-in member of the current organisation with at least ``role``."""

    def decorator(view: View) -> View:
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            membership = getattr(request, "membership", None)
            if not isinstance(membership, Membership):
                return redirect("accounts:create_organization")
            if not membership.has_role(role):
                raise PermissionDenied(f"This action needs the {role} role")
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def get_owned(queryset: QuerySet[M], organization: Organization, **lookup: Any) -> M:
    """Fetch one record of ``organization`` or raise 404 (also for other tenants' records)."""
    try:
        return queryset.filter(organization=organization).get(**lookup)
    except ObjectDoesNotExist:
        raise Http404("Not found") from None
