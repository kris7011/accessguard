"""Applies verified Stripe events to subscription state, exactly once and in order.

* **Idempotent:** every event id is stored with a unique constraint in the same
  transaction as its effect; a redelivered event is acknowledged and ignored.
* **Ordered:** Stripe does not guarantee delivery order. Subscription events older than
  the newest one already applied (by ``created``) are ignored, so a late
  ``subscription.updated`` can never undo a later cancellation or upgrade.
* **Safe by default:** unknown event types are acknowledged and recorded, never applied.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from accessguard_web.accounts.models import Organization, record
from accessguard_web.billing.gateway import plan_for_price
from accessguard_web.billing.models import StripeEvent, Subscription, SubscriptionStatus
from accessguard_web.billing.plans import PlanId

logger = logging.getLogger(__name__)

SUBSCRIPTION_EVENTS = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "customer.subscription.paused",
        "customer.subscription.resumed",
    }
)


def process_event(event: dict[str, Any]) -> str:
    """Apply ``event`` once. Returns a short outcome for logs and tests."""
    event_id, event_type = str(event.get("id", "")), str(event.get("type", ""))
    created = int(event.get("created") or 0)
    obj = (event.get("data") or {}).get("object") or {}
    if not event_id or not isinstance(obj, dict):
        return "ignored: malformed"
    try:
        with transaction.atomic():
            stored = StripeEvent.objects.create(
                event_id=event_id, event_type=event_type, created=created
            )
            outcome = _apply(event_type, created, obj)
            stored.outcome = outcome[:200]
            stored.save(update_fields=["outcome"])
    except IntegrityError:
        return "duplicate"
    logger.info("stripe event %s %s: %s", event_type, event_id, outcome)
    return outcome


def _apply(event_type: str, created: int, obj: dict[str, Any]) -> str:
    if event_type == "checkout.session.completed":
        return _checkout_completed(obj)
    if event_type in SUBSCRIPTION_EVENTS:
        return _subscription_changed(event_type, created, obj)
    if event_type == "invoice.payment_failed":
        return _payment_failed(obj)
    if event_type in ("invoice.paid", "invoice.payment_succeeded"):
        return _payment_succeeded(obj)
    return f"ignored: {event_type}"


def _organization(obj: dict[str, Any]) -> Organization | None:
    metadata = obj.get("metadata") or {}
    reference = metadata.get("organization_id") or obj.get("client_reference_id")
    if reference and str(reference).isdigit():
        return Organization.objects.filter(pk=int(reference)).first()
    return None


def _subscription_by_stripe(
    obj: dict[str, Any], *, subscription_id: str | None
) -> Subscription | None:
    if subscription_id:
        found = Subscription.objects.filter(stripe_subscription_id=subscription_id).first()
        if found:
            return found
    customer = obj.get("customer")
    if customer:
        found = Subscription.objects.filter(stripe_customer_id=str(customer)).first()
        if found:
            return found
    organization = _organization(obj)
    if organization is not None:
        return Subscription.objects.get_or_create(organization=organization)[0]
    return None


def _checkout_completed(obj: dict[str, Any]) -> str:
    organization = _organization(obj)
    if organization is None:
        return "ignored: checkout without organisation"
    subscription = Subscription.objects.select_for_update().get_or_create(
        organization=organization
    )[0]
    subscription.stripe_customer_id = str(obj.get("customer") or subscription.stripe_customer_id)
    subscription.stripe_subscription_id = str(
        obj.get("subscription") or subscription.stripe_subscription_id
    )
    subscription.save()
    record(organization, None, "billing.checkout_completed", "stripe")
    return "linked customer"


def _timestamp(value: object) -> datetime | None:
    return datetime.fromtimestamp(value, tz=UTC) if isinstance(value, int) else None


def _subscription_changed(event_type: str, created: int, obj: dict[str, Any]) -> str:
    subscription = _subscription_by_stripe(obj, subscription_id=str(obj.get("id") or ""))
    if subscription is None:
        return "ignored: unknown subscription"
    subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
    if created < subscription.last_event_created:
        return "ignored: older than the applied state"
    items = ((obj.get("items") or {}).get("data")) or []
    first = items[0] if items and isinstance(items[0], dict) else {}
    price_id = str((first.get("price") or {}).get("id") or "")
    status = str(obj.get("status") or "")
    if event_type == "customer.subscription.deleted":
        status = SubscriptionStatus.CANCELED
    if status not in SubscriptionStatus.values:
        return f"ignored: unknown status {status}"
    previous = subscription.status
    subscription.status = status
    subscription.stripe_subscription_id = str(obj.get("id") or subscription.stripe_subscription_id)
    subscription.stripe_customer_id = str(obj.get("customer") or subscription.stripe_customer_id)
    plan = plan_for_price(price_id)
    if status == SubscriptionStatus.CANCELED:
        subscription.plan = PlanId.FREE
    elif plan is not None:
        subscription.plan = plan
    subscription.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
    # Flexible billing mode (the default for new subscriptions) schedules a cancellation
    # with cancel_at, also from the Customer Portal; cancel_at_period_end then stays false.
    subscription.cancel_at = _timestamp(obj.get("cancel_at"))
    # Newer API versions report the period on the subscription item.
    subscription.current_period_end = _timestamp(
        obj.get("current_period_end") or first.get("current_period_end")
    )
    if status == SubscriptionStatus.PAST_DUE:
        subscription.past_due_since = subscription.past_due_since or timezone.now()
    else:
        subscription.past_due_since = None
    subscription.last_event_created = created
    subscription.save()
    record(subscription.organization, None, "billing.subscription_changed", "stripe",
           status=status, previous=previous, plan=subscription.plan)  # fmt: skip
    return f"subscription {status} ({subscription.plan})"


def _payment_failed(obj: dict[str, Any]) -> str:
    subscription = _subscription_by_stripe(obj, subscription_id=_invoice_subscription(obj))
    if subscription is None:
        return "ignored: unknown customer"
    subscription.past_due_since = subscription.past_due_since or timezone.now()
    if subscription.status == SubscriptionStatus.ACTIVE:
        subscription.status = SubscriptionStatus.PAST_DUE
    subscription.save()
    record(subscription.organization, None, "billing.payment_failed", "stripe")
    return "payment failed: grace period started"


def _payment_succeeded(obj: dict[str, Any]) -> str:
    subscription = _subscription_by_stripe(obj, subscription_id=_invoice_subscription(obj))
    if subscription is None:
        return "ignored: unknown customer"
    subscription.past_due_since = None
    if subscription.status == SubscriptionStatus.PAST_DUE:
        subscription.status = SubscriptionStatus.ACTIVE
    subscription.save()
    return "payment succeeded"


def _invoice_subscription(obj: dict[str, Any]) -> str | None:
    value = obj.get("subscription")
    if isinstance(value, str):
        return value
    details = ((obj.get("parent") or {}).get("subscription_details")) or {}
    found = details.get("subscription")
    return str(found) if found else None
