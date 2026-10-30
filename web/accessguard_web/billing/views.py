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
from accessguard_web.accounts.models import Membership, Role, record
from accessguard_web.accounts.tenancy import current_org, current_user, membership_required
from accessguard_web.assistant.models import AssistantRun
from accessguard_web.billing.entitlements import entitlements_for, month_start
from accessguard_web.billing.gateway import (
    ENDED_STATUSES,
    BillingNotConfigured,
    BillingRefused,
    CheckoutRequest,
    InvalidWebhook,
    ProviderUnavailable,
    gateway,
    is_test_key,
)
from accessguard_web.billing.models import Subscription, SubscriptionStatus
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
    ent = entitlements_for(organization)
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
            "ent": ent,
            "has_subscription": _has_subscription(ent.subscription),
        },
    )


@require_POST
@membership_required(Role.OWNER)
def checkout(request: HttpRequest, plan_id: str) -> HttpResponse:
    """Choose a paid plan: Checkout for a first purchase, otherwise change the subscription."""
    organization = current_org(request)
    if plan_id not in (PlanId.TEAM, PlanId.BUSINESS):
        return HttpResponseBadRequest("Unknown plan")
    plan = PlanId(plan_id)
    subscription = entitlements_for(organization).subscription
    if _has_subscription(subscription):
        return _change_plan(request, subscription, plan)
    billing_url = request.build_absolute_uri(reverse("billing:overview"))
    try:
        url = gateway().checkout_url(
            CheckoutRequest(
                organization_id=organization.pk,
                plan=plan,
                customer_id=subscription.stripe_customer_id or None,
                customer_email=current_user(request).email,
                success_url=f"{billing_url}?checkout=success",
                cancel_url=f"{billing_url}?checkout=cancelled",
            )
        )
    except (BillingNotConfigured, BillingRefused) as error:
        messages.error(request, str(error))
        return redirect("billing:overview")
    return redirect(url)


def _has_subscription(subscription: Subscription) -> bool:
    """A live subscription exists, so a plan change must update it rather than add one."""
    return bool(subscription.stripe_subscription_id) and subscription.status not in (
        ENDED_STATUSES | {SubscriptionStatus.NONE}
    )


def _change_plan(request: HttpRequest, subscription: Subscription, plan: PlanId) -> HttpResponse:
    if subscription.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING):
        messages.error(
            request,
            "The subscription has an open payment. Settle it in the Customer Portal before "
            "changing the plan.",
        )
        return redirect("billing:overview")
    try:
        change = gateway().change_plan(subscription.stripe_subscription_id, plan)
    except (BillingNotConfigured, BillingRefused) as error:
        messages.error(request, str(error))
        return redirect("billing:overview")
    record(subscription.organization, current_user(request), "billing.plan_change_requested",
           "subscription", plan=plan.value, applied=change.applied)  # fmt: skip
    name = PLANS[plan].name
    if change.applied:
        messages.success(
            request, f"Your plan changes to {name} as soon as Stripe confirms it (seconds)."
        )
        return redirect("billing:overview")
    messages.warning(
        request,
        f"The payment for {name} did not go through, so your current plan stays. Pay the open "
        "invoice within 23 hours to switch; otherwise the change is discarded.",
    )
    return redirect(change.invoice_url or reverse("billing:overview"))


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
    provider = gateway()
    try:
        event = provider.verified_event(request.body, request.headers.get("Stripe-Signature", ""))
    except InvalidWebhook:
        logger.warning("rejected a Stripe webhook with an invalid signature")
        return HttpResponse(status=400)
    try:
        outcome = process_event(event, provider.subscription)
    except ProviderUnavailable as error:
        # Nothing was stored, so Stripe's automatic retry delivers the event again.
        logger.warning("stripe event %s not applied, API unavailable: %s", event.get("id"), error)
        return HttpResponse(status=503)
    return HttpResponse(outcome, content_type="text/plain")
