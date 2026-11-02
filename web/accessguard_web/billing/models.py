"""Subscription state per organisation, mirrored from Stripe webhooks, and processed events.

The row follows one Stripe subscription at a time; ``plan`` is the plan granted, which can
differ from the subscription's price while a higher price is not yet paid.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from accessguard_web.accounts.models import Organization
from accessguard_web.billing.plans import PlanId


class SubscriptionStatus(models.TextChoices):
    NONE = "none", "No subscription"
    INCOMPLETE = "incomplete", "Waiting for the first payment"
    TRIALING = "trialing", "Trial"
    ACTIVE = "active", "Active"
    PAST_DUE = "past_due", "Payment failed, retrying"
    UNPAID = "unpaid", "Unpaid"
    CANCELED = "canceled", "Cancelled"
    INCOMPLETE_EXPIRED = "incomplete_expired", "First payment failed"
    PAUSED = "paused", "Paused"


class Subscription(models.Model):
    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.CharField(
        max_length=20, choices=[(p.value, p.value) for p in PlanId], default=PlanId.FREE
    )
    status = models.CharField(
        max_length=20, choices=SubscriptionStatus.choices, default=SubscriptionStatus.NONE
    )
    stripe_customer_id = models.CharField(max_length=80, blank=True, db_index=True)
    stripe_subscription_id = models.CharField(max_length=80, blank=True, db_index=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    cancel_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When a scheduled cancellation ends the subscription (flexible billing mode).",
    )  # fmt: skip
    cancellation_reason = models.CharField(
        max_length=40, blank=True,
        help_text="Stripe's cancellation_details.reason, e.g. payment_failed or "
        "cancellation_requested.",
    )  # fmt: skip
    past_due_since = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.organization}: {self.plan} ({self.status})"


class StripeEvent(models.Model):
    """Every processed webhook event id, so retries and duplicates are applied once."""

    event_id = models.CharField(max_length=80, unique=True)
    event_type = models.CharField(max_length=80)
    created = models.BigIntegerField()
    received_at = models.DateTimeField(auto_now_add=True)
    outcome = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering: ClassVar = ["-received_at"]
