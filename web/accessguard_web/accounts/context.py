"""Template context: the current organisation, the user's other organisations and plan."""

from __future__ import annotations

from django.http import HttpRequest

from accessguard_web.accounts.models import Membership, User
from accessguard_web.accounts.tenancy import memberships


def organization(request: HttpRequest) -> dict[str, object]:
    membership = getattr(request, "membership", None)
    if not isinstance(membership, Membership) or not isinstance(request.user, User):
        return {}
    from accessguard_web.billing.entitlements import entitlements_for  # noqa: PLC0415

    return {
        "membership": membership,
        "current_org": membership.organization,
        "all_memberships": memberships(request.user),
        "entitlements": entitlements_for(membership.organization),
    }
