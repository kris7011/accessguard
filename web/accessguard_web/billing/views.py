"""Billing page, Checkout and Customer Portal redirects, and the Stripe webhook endpoint."""

from __future__ import annotations

import logging

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accessguard_web import conf
from accessguard_web.accounts.models import Membership, Role
from accessguard_web.accounts.tenancy import current_org, current_user, membership_required
from accessguard_web.assistant.models import AssistantRun
from accessguard_web.billing.entitlements import entitlements_for, month_start
from accessguard_web.billing.gateway import (
    BillingNotConfigured,
    CheckoutRequest,
    InvalidWebhook,
    gateway,
    is_test_key,
)
from accessguard_web.billing.plans import PLANS, PlanId
from accessguard_web.billing.webhooks import process_event
from accessguard_web.projects.models import Project
from accessguard_web.scans.models import Runner, Scan

logger = logging.getLogger(__name__)


@membership_required()
def overview(request: HttpRequest) -> HttpResponse:
    organization = current_org(request)
    since = month_start()
    usage = {
        "scans": Scan.objects.filter(
            organization=organization, is_demo=False, created_at__gte=since
        ).count(),
        "projects": Project.objects.filter(organization=organization, is_demo=False).count(),
        "runners": Runner.objects.filter(
            organization=organization, revoked_at__isnull=True
        ).count(),
        "members": Membership.objects.filter(organization=organization).count(),
        "assistant": AssistantRun.objects.filter(
            organization=organization, created_at__gte=since
        ).count(),
    }
    config = conf.stripe()
    configured = bool(config["SECRET_KEY"] and config["WEBHOOK_SECRET"])
    test_mode = configured and is_test_key(config["SECRET_KEY"])
    return render(
        request,
        "billing/overview.html",
        {
            "plans": list(PLANS.values()),
            "usage": usage,
            "configured": configured,
            "test_mode": test_mode,
            "live_refused": configured and not test_mode and not config["ALLOW_LIVE"],
            "checkout_state": request.GET.get("checkout", ""),
            "is_owner": request.membership.has_role(Role.OWNER),  # type: ignore[attr-defined]
            "ent": entitlements_for(organization),
        },
    )


@require_POST
@membership_required(Role.OWNER)
def checkout(request: HttpRequest, plan_id: str) -> HttpResponse:
    organization = current_org(request)
    if plan_id not in (PlanId.TEAM, PlanId.BUSINESS):
        return HttpResponseBadRequest("Unknown plan")
    subscription = entitlements_for(organization).subscription
    billing_url = request.build_absolute_uri(reverse("billing:overview"))
    try:
        url = gateway().checkout_url(
            CheckoutRequest(
                organization_id=organization.pk,
                plan=PlanId(plan_id),
                customer_id=subscription.stripe_customer_id or None,
                customer_email=current_user(request).email,
                success_url=f"{billing_url}?checkout=success",
                cancel_url=f"{billing_url}?checkout=cancelled",
            )
        )
    except BillingNotConfigured as error:
        messages.error(request, str(error))
        return redirect("billing:overview")
    return redirect(url)


@require_POST
@membership_required(Role.OWNER)
def portal(request: HttpRequest) -> HttpResponse:
    subscription = entitlements_for(current_org(request)).subscription
    if not subscription.stripe_customer_id:
        messages.error(request, "There is no paid subscription to manage yet.")
        return redirect("billing:overview")
    try:
        url = gateway().portal_url(
            subscription.stripe_customer_id, request.build_absolute_uri(reverse("billing:overview"))
        )
    except BillingNotConfigured as error:
        messages.error(request, str(error))
        return redirect("billing:overview")
    return redirect(url)


@csrf_exempt
@require_POST
def webhook(request: HttpRequest) -> HttpResponse:
    """Stripe calls this; authenticity comes from the signature, not from a session or CSRF."""
    try:
        event = gateway().verified_event(request.body, request.headers.get("Stripe-Signature", ""))
    except InvalidWebhook:
        logger.warning("rejected a Stripe webhook with an invalid signature")
        return HttpResponse(status=400)
    outcome = process_event(event)
    return HttpResponse(outcome, content_type="text/plain")
