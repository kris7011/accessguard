"""Mirrors Stripe's current subscription state into the organisation's row, once per event.

Stripe does not deliver events in order, and ``created`` has one-second resolution, so
neither the payload nor its timestamp says what is true now
(https://docs.stripe.com/webhooks#event-ordering). An event is therefore only a signal:

* **Idempotent:** every event id is stored with a unique constraint in the same
  transaction as its effect; a redelivered event is acknowledged and ignored. Copies
  delivered at the same moment wait on that constraint and then count as duplicates.
* **Current state, not the payload:** the event names a subscription (directly, or through
  its invoice or Checkout Session). Under a row lock on the organisation's subscription,
  that subscription is fetched from Stripe and its current state is applied. A late or
  repeated event therefore re-applies today's state, and a slow delivery that fetched
  earlier cannot overwrite a later one, because the fetch happens inside the lock.
* **One subscription per organisation:** an event for another subscription than the one
  the organisation follows is applied only when that one has ended at Stripe, so a late
  event of an earlier subscription cannot touch the new one.
* **Paid rights only:** only ``active`` or ``trialing`` can raise the plan. Every other
  state keeps at most the plan the organisation already had, so a failed upgrade payment
  never grants the higher plan during the grace period.
* **Retry on failure:** if Stripe's API cannot be reached, nothing is stored and the
  endpoint answers 503, so Stripe delivers the event again.
* **Safe by default:** unknown event types are acknowledged and recorded, never applied.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from accessguard_web.accounts.models import Organization, record
from accessguard_web.billing.gateway import ENDED_STATUSES, plan_for_price
from accessguard_web.billing.models import StripeEvent, Subscription, SubscriptionStatus
from accessguard_web.billing.plans import PLANS, PlanId

logger = logging.getLogger(__name__)

FetchSubscription = Callable[[str], "dict[str, Any] | None"]

SUBSCRIPTION_EVENTS = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "customer.subscription.paused",
        "customer.subscription.resumed",
        "customer.subscription.pending_update_applied",
        "customer.subscription.pending_update_expired",
    }
)
INVOICE_EVENTS = frozenset({"invoice.paid", "invoice.payment_succeeded", "invoice.payment_failed"})
CHECKOUT_COMPLETED = "checkout.session.completed"
HANDLED_EVENTS = SUBSCRIPTION_EVENTS | INVOICE_EVENTS | {CHECKOUT_COMPLETED}

# States in which the plan is paid up; only these may raise it.
PAID_UP = frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING})


@dataclass(frozen=True)
class Reference:
    """What an event says about whose subscription it concerns."""

    subscription_id: str
    customer_id: str
    organization_id: int | None


def process_event(event: dict[str, Any], fetch_subscription: FetchSubscription) -> str:
    """Apply ``event`` once. Returns a short outcome for logs and tests.

    Raises ``ProviderUnavailable`` (from ``fetch_subscription``) with nothing stored.
    """
    event_id, event_type = str(event.get("id", "")), str(event.get("type", ""))
    created = int(event.get("created") or 0)
    obj = (event.get("data") or {}).get("object") or {}
    if not event_id or not isinstance(obj, dict):
        return "ignored: malformed"
    with transaction.atomic():
        try:
            with transaction.atomic():
                stored = StripeEvent.objects.create(
                    event_id=event_id, event_type=event_type, created=created
                )
        except IntegrityError:
            return "duplicate"
        outcome = _apply(event_type, obj, fetch_subscription)
        stored.outcome = outcome[:200]
        stored.save(update_fields=["outcome"])
    logger.info("stripe event %s %s: %s", event_type, event_id, outcome)
    return outcome


def _apply(event_type: str, obj: dict[str, Any], fetch: FetchSubscription) -> str:
    if event_type not in HANDLED_EVENTS:
        return f"ignored: {event_type}"
    reference = _reference(event_type, obj)
    if not reference.subscription_id:
        return "ignored: no subscription"
    row = _locked_row(reference)
    if row is None:
        return "ignored: unknown organisation"
    # Fetched only now, inside the lock: a concurrent delivery waits and then fetches later.
    current = fetch(reference.subscription_id)
    if current is None:
        return "ignored: subscription unknown at Stripe"
    if not _belongs_to(row, current):
        return "ignored: subscription of another customer or organisation"
    followed = row.stripe_subscription_id
    if followed and followed != current.get("id") and not _replaces(followed, current, fetch):
        return "ignored: not the organisation's current subscription"
    if event_type == CHECKOUT_COMPLETED:
        record(row.organization, None, "billing.checkout_completed", "stripe")
    return _mirror(row, current)


def _reference(event_type: str, obj: dict[str, Any]) -> Reference:
    if event_type in SUBSCRIPTION_EVENTS:
        subscription_id, metadata = obj.get("id"), obj.get("metadata") or {}
    elif event_type in INVOICE_EVENTS:
        details = ((obj.get("parent") or {}).get("subscription_details")) or {}
        # Before API 2025-03-31 the subscription was a top-level invoice field.
        subscription_id = details.get("subscription") or obj.get("subscription")
        metadata = details.get("metadata") or {}
    else:
        subscription_id = obj.get("subscription")
        metadata = {"organization_id": obj.get("client_reference_id")} | (obj.get("metadata") or {})
    reference = str(metadata.get("organization_id") or "")
    return Reference(
        subscription_id=str(subscription_id or ""),
        customer_id=str(obj.get("customer") or ""),
        organization_id=int(reference) if reference.isdigit() else None,
    )


def _locked_row(reference: Reference) -> Subscription | None:
    """The organisation's subscription row, locked until the transaction ends.

    The subscription and customer ids only find the organisation; whether this
    subscription is the one it follows is decided later, against Stripe's state.
    """
    found = Subscription.objects.filter(stripe_subscription_id=reference.subscription_id).first()
    if found is None and reference.customer_id:
        found = Subscription.objects.filter(stripe_customer_id=reference.customer_id).first()
    if found is None and reference.organization_id is not None:
        organization = Organization.objects.filter(pk=reference.organization_id).first()
        if organization is not None:
            found = Subscription.objects.get_or_create(organization=organization)[0]
    if found is None:
        return None
    return Subscription.objects.select_for_update().select_related("organization").get(pk=found.pk)


def _belongs_to(row: Subscription, current: dict[str, Any]) -> bool:
    """Tenant check: the Stripe customer and our metadata must both match the organisation."""
    customer = str(current.get("customer") or "")
    reference = str((current.get("metadata") or {}).get("organization_id") or "")
    if reference and reference != str(row.organization_id):
        return False
    if row.stripe_customer_id:
        return customer == row.stripe_customer_id
    return not Subscription.objects.filter(stripe_customer_id=customer).exclude(pk=row.pk).exists()


def _replaces(followed_id: str, current: dict[str, Any], fetch: FetchSubscription) -> bool:
    """Whether ``current`` takes over from the subscription the organisation follows."""
    if current.get("status") in ENDED_STATUSES:
        return False
    followed = fetch(followed_id)
    if followed is None or followed.get("status") in ENDED_STATUSES:
        return True
    logger.warning(
        "organisation subscription row %s: live subscription %s ignored, %s is still live",
        followed_id, current.get("id"), followed_id,
    )  # fmt: skip
    return False


def _timestamp(value: object) -> datetime | None:
    return datetime.fromtimestamp(value, tz=UTC) if isinstance(value, int) else None


def _mirror(row: Subscription, current: dict[str, Any]) -> str:
    status = str(current.get("status") or "")
    if status not in SubscriptionStatus.values:
        return f"ignored: unknown status {status}"
    items = (current.get("items") or {}).get("data") or []
    first = items[0] if items and isinstance(items[0], dict) else {}
    price_plan = plan_for_price(str((first.get("price") or {}).get("id") or ""))
    previous = {field: getattr(row, field) for field in _MIRRORED}

    row.status = status
    row.stripe_subscription_id = str(current.get("id") or row.stripe_subscription_id)
    row.stripe_customer_id = str(current.get("customer") or row.stripe_customer_id)
    row.plan = _granted_plan(PlanId(row.plan), price_plan, status)
    row.cancel_at_period_end = bool(current.get("cancel_at_period_end"))
    # Flexible billing mode (the default for new subscriptions) schedules a cancellation
    # with cancel_at, also from the Customer Portal; cancel_at_period_end then stays false.
    row.cancel_at = _timestamp(current.get("cancel_at"))
    # Newer API versions report the period on the subscription item.
    row.current_period_end = _timestamp(
        current.get("current_period_end") or first.get("current_period_end")
    )
    if status == SubscriptionStatus.PAST_DUE:
        row.past_due_since = row.past_due_since or timezone.now()
    else:
        row.past_due_since = None

    changed = [field for field in _MIRRORED if getattr(row, field) != previous[field]]
    if not changed:
        return f"unchanged: subscription {status} ({row.plan})"
    row.save(update_fields=[*changed, "updated_at"])
    if row.status != previous["status"] or row.plan != previous["plan"]:
        record(row.organization, None, "billing.subscription_changed", "stripe",
               status=status, previous=previous["status"], plan=row.plan)  # fmt: skip
    return f"subscription {status} ({row.plan})"


_MIRRORED = (
    "status", "plan", "stripe_subscription_id", "stripe_customer_id", "cancel_at_period_end",
    "cancel_at", "current_period_end", "past_due_since",
)  # fmt: skip


def _granted_plan(previous: PlanId, price_plan: PlanId | None, status: str) -> PlanId:
    """The plan this state entitles to. Only a paid-up state can raise it.

    In any other state (``past_due``, ``incomplete``, ...) the organisation keeps at most
    what it already had: a renewal that fails keeps the paid plan for the grace period, an
    upgrade whose payment failed keeps the lower one, and a subscription that was never
    seen paid up grants nothing yet.
    """
    if status == SubscriptionStatus.CANCELED:
        return PlanId.FREE
    if price_plan is None:  # a price this deployment does not sell: keep what was granted
        return previous
    if status in PAID_UP:
        return price_plan
    return min(previous, price_plan, key=lambda plan: PLANS[plan].monthly_price_eur)
