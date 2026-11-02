"""What an organisation may do now, derived from its subscription. Enforced on the server.

Rules:

* ``active`` and ``trialing`` give the subscribed plan. A scheduled cancellation
  (``cancel_at``, or ``cancel_at_period_end`` in classic billing mode) only adds a warning;
  the plan ends when Stripe sends ``customer.subscription.deleted``.
* ``past_due`` keeps the plan for AccessGuard's grace period (``GRACE_DAYS``, 7) with a
  visible warning; after it the organisation falls back to Free. Stripe's payment retries
  run on separately and longer (``RETRY_DAYS``, 14 in the Dashboard), so a customer can be
  on Free while Stripe still retries; a successful retry restores the plan. If every retry
  fails, Stripe cancels the subscription and the webhook voids the renewal invoice if it is
  still open with nothing paid (``webhooks._void_failed_renewal``).
* Every other status (none, incomplete, unpaid, canceled, ...) gives Free. Existing data
  is kept; only new work is limited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from accessguard_web import conf
from accessguard_web.accounts.models import Organization
from accessguard_web.billing.models import Subscription, SubscriptionStatus
from accessguard_web.billing.plans import PLANS, Plan, PlanId


@dataclass(frozen=True)
class PaymentRecovery:
    """The two separate clocks after a failed payment.

    Both count from ``past_due_since``: when AccessGuard first saw the subscription
    ``past_due``, which in production is seconds after the failed charge. The grace period is
    AccessGuard's own rule; the retry end is Stripe's, approximate (Smart Retries choose the
    exact times) and taken from the ``RETRY_DAYS`` setting, which mirrors the Dashboard.
    """

    failed_since: datetime
    grace_days: int
    grace_ends: datetime
    retry_days: int
    retries_end: datetime

    def in_grace(self, now: datetime) -> bool:
        return now <= self.grace_ends


@dataclass(frozen=True)
class Entitlements:
    plan: Plan
    subscription: Subscription
    warning: str | None

    @property
    def is_paying(self) -> bool:
        return self.plan.is_paid


class LimitExceeded(Exception):  # noqa: N818 - user-facing condition, not a programming error
    """An action would exceed the organisation's plan. The message is shown to the user."""


def subscription_for(organization: Organization) -> Subscription:
    subscription, _ = Subscription.objects.get_or_create(organization=organization)
    return subscription


def payment_recovery(
    subscription: Subscription, now: datetime | None = None
) -> PaymentRecovery | None:
    """The grace period and Stripe's retry window while a payment is overdue, else ``None``."""
    if subscription.status != SubscriptionStatus.PAST_DUE:
        return None
    since = subscription.past_due_since or now or timezone.now()
    grace_days, retry_days = conf.stripe()["GRACE_DAYS"], conf.stripe()["RETRY_DAYS"]
    return PaymentRecovery(
        failed_since=since,
        grace_days=grace_days,
        grace_ends=since + timedelta(days=grace_days),
        retry_days=retry_days,
        retries_end=since + timedelta(days=retry_days),
    )


def effective_plan(
    subscription: Subscription, now: datetime | None = None
) -> tuple[Plan, str | None]:
    now = now or timezone.now()
    status = subscription.status
    paid = PLANS[PlanId(subscription.plan)]
    if status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING):
        ends = subscription.cancel_at or (
            subscription.current_period_end if subscription.cancel_at_period_end else None
        )
        warning = None
        if ends:
            warning = (
                f"Your {paid.name} plan is cancelled and ends on {ends:%Y-%m-%d}; "
                "you then return to Free."
            )
        return paid, warning
    recovery = payment_recovery(subscription, now)
    if recovery is not None:
        retries = f"Stripe keeps retrying the payment until about {recovery.retries_end:%Y-%m-%d}"
        if recovery.in_grace(now):
            return paid, (
                f"The last payment failed. Your {paid.name} plan stays active until "
                f"{recovery.grace_ends:%Y-%m-%d} ({recovery.grace_days}-day grace period). "
                f"{retries}. Please update your payment method."
            )
        return PLANS[PlanId.FREE], (
            f"The payment failed and the {recovery.grace_days}-day grace period has ended, so "
            f"Free limits apply. {retries}; a successful retry or payment restores your "
            f"{paid.name} plan. Please update your payment method."
        )
    if status == SubscriptionStatus.UNPAID:
        return PLANS[PlanId.FREE], "Your subscription is unpaid, so Free limits apply."
    return PLANS[PlanId.FREE], None


def entitlements_for(organization: Organization) -> Entitlements:
    subscription = subscription_for(organization)
    plan, warning = effective_plan(subscription)
    return Entitlements(plan, subscription, warning)


def month_start(now: datetime | None = None) -> datetime:
    now = now or timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def require_scan_allowed(organization: Organization, *, planned_checks: int) -> None:
    from accessguard_web.scans.models import Scan  # noqa: PLC0415 - avoid an import cycle

    plan = entitlements_for(organization).plan
    used = Scan.objects.filter(
        organization=organization, is_demo=False, created_at__gte=month_start()
    ).count()
    if used >= plan.scans_per_month:
        raise LimitExceeded(
            f"Your {plan.name} plan includes {plan.scans_per_month} scans per month and they "
            "are used up. Upgrade or wait until next month."
        )
    if planned_checks > plan.checks_per_scan:
        raise LimitExceeded(
            f"This configuration has {planned_checks} checks; your {plan.name} plan allows "
            f"{plan.checks_per_scan} per scan. Upgrade or reduce users or endpoints."
        )


def require_capacity(organization: Organization, what: str, current: int) -> None:
    plan = entitlements_for(organization).plan
    limit = {
        "projects": plan.projects,
        "runners": plan.runners,
        "members": plan.members,
    }[what]
    if current >= limit:
        raise LimitExceeded(f"Your {plan.name} plan includes {limit} {what}. Upgrade for more.")


def require_assistant_quota(organization: Organization) -> None:
    from accessguard_web.assistant.models import AssistantRun  # noqa: PLC0415

    plan = entitlements_for(organization).plan
    used = AssistantRun.objects.filter(
        organization=organization, created_at__gte=month_start()
    ).count()
    if used >= plan.assistant_requests_per_month:
        raise LimitExceeded(
            f"Your {plan.name} plan includes {plan.assistant_requests_per_month} assistant "
            "requests per month and they are used up."
        )
