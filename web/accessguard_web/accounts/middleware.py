"""Attaches the current organisation membership to each request (see tenancy.py)."""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from accessguard_web.accounts.tenancy import resolve_membership


class CurrentOrganizationMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.membership = resolve_membership(request)  # type: ignore[attr-defined]
        return self.get_response(request)
