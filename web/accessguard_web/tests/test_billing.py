"""Stripe webhooks (signature, idempotency, ordering), entitlements and plan limits.

Signatures are generated with the official SDK's documented test helper, so the real
verification code runs. A real Stripe test account was not available for this suite.
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock, skipUnless

import stripe
from django.conf import settings
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accessguard_web.accounts.models import AuditEvent
from accessguard_web.billing.entitlements import (
    LimitExceeded,
    effective_plan,
    entitlements_for,
    require_scan_allowed,
)
from accessguard_web.billing.gateway import (
    LIVE_KEY_REFUSED,
    ProviderUnavailable,
    StripeGateway,
    UnconfiguredGateway,
    gateway,
    is_test_key,
)
from accessguard_web.billing.models import StripeEvent, Subscription, SubscriptionStatus
from accessguard_web.billing.plans import PlanId
from accessguard_web.billing.webhooks import process_event
from accessguard_web.scans import services
from accessguard_web.tests.support import login, make_org, sample_project

WEBHOOK_SECRET = "whsec_test_secret_for_unit_tests"  # pragma: allowlist secret
WRONG_SECRET = "whsec_wrong_secret_value"  # pragma: allowlist secret
STRIPE_TEST = {
    **settings.STRIPE,
    "SECRET_KEY": "sk_test_dummy_key_for_unit_tests",  # pragma: allowlist secret
    "WEBHOOK_SECRET": WEBHOOK_SECRET,
    "PRICE_TEAM": "price_team_test",
    "PRICE_BUSINESS": "price_business_test",
    "PORTAL_CONFIGURATION": "",
    "ALLOW_LIVE": False,
}
STRIPE_LIVE = {**STRIPE_TEST, "SECRET_KEY": "rk_live_fake"}  # pragma: allowlist secret


def event(
    event_id: str, event_type: str, obj: dict[str, Any], created: int = 1_800_000_000
) -> dict[str, Any]:
    return {"id": event_id, "object": "event", "type": event_type, "created": created,
            "data": {"object": obj}, "livemode": False, "api_version": "2026-10-21"}  # fmt: skip


def subscription(
    org_id: int, status: str, price: str = "price_team_test", **extra: Any
) -> dict[str, Any]:
    return {
        "id": "sub_123",
        "object": "subscription",
        "customer": "cus_123",
        "status": status,
        "metadata": {"organization_id": str(org_id)},
        "cancel_at_period_end": False,
        "items": {"data": [{"price": {"id": price}, "current_period_end": 1_900_000_000}]},
        **extra,
    }


class FakeStripe:
    """Subscriptions as Stripe's API returns them now, for ``StripeGateway.subscription``."""

    def __init__(self) -> None:
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.retrieved: list[str] = []

    def put(self, obj: dict[str, Any]) -> dict[str, Any]:
        self.subscriptions[obj["id"]] = copy.deepcopy(obj)
        return obj

    def retrieve(self, _gateway: object, subscription_id: str) -> dict[str, Any] | None:
        self.retrieved.append(subscription_id)
        found = self.subscriptions.get(subscription_id)
        return copy.deepcopy(found) if found else None


def invoice(invoice_id: str, subscription_id: str, org_id: int) -> dict[str, Any]:
    # Invoice shape of API 2026-09-30: the subscription is under parent.subscription_details.
    return {"id": invoice_id, "object": "invoice", "customer": "cus_123",
            "parent": {"type": "subscription_details", "subscription_details": {
                "subscription": subscription_id,
                "metadata": {"organization_id": str(org_id)}}}}  # fmt: skip


@override_settings(STRIPE=STRIPE_TEST)
class StripeWebhookCase(TestCase):
    """Signed deliveries to the real endpoint; Stripe's API is replaced by ``FakeStripe``."""

    def setUp(self) -> None:
        self.stripe = FakeStripe()
        patcher = mock.patch.object(
            StripeGateway, "subscription", autospec=True, side_effect=self.stripe.retrieve
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def deliver(self, payload: dict[str, Any], *, secret: str = WEBHOOK_SECRET) -> Any:
        body = json.dumps(payload)
        header = stripe.WebhookSignature.generate_signature_header(body, secret)
        return self.client.post(
            reverse("stripe_webhook"), data=body, content_type="application/json",
            HTTP_STRIPE_SIGNATURE=header,
        )  # fmt: skip


class WebhookTests(StripeWebhookCase):
    """Each event is applied by fetching the subscription from (fake) Stripe.

    ``self.now(obj)`` sets what Stripe's API returns for that subscription from then on.
    """

    def setUp(self) -> None:
        super().setUp()
        self.org, self.owner = make_org("Paying")

    def now(self, obj: dict[str, Any]) -> dict[str, Any]:
        return self.stripe.put(obj)

    def test_invalid_or_missing_signatures_are_rejected(self) -> None:
        payload = event(
            "evt_1", "customer.subscription.created", self.now(subscription(self.org.pk, "active"))
        )
        self.assertEqual(
            self.deliver(payload, secret=WRONG_SECRET).status_code,
            400,
        )
        unsigned = self.client.post(
            reverse("stripe_webhook"), data=json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(unsigned.status_code, 400)
        self.assertFalse(StripeEvent.objects.exists())
        self.assertEqual(entitlements_for(self.org).plan.id, PlanId.FREE)

    def test_checkout_links_the_customer_and_grants_only_a_paid_subscription(self) -> None:
        checkout = {"id": "cs_1", "object": "checkout.session", "client_reference_id": str(self.org.pk),
                    "customer": "cus_123", "subscription": "sub_123", "metadata": {}}  # fmt: skip
        self.now(subscription(self.org.pk, "incomplete"))
        self.assertEqual(
            self.deliver(event("evt_1", "checkout.session.completed", checkout)).status_code, 200
        )
        ent = entitlements_for(self.org)
        self.assertEqual(ent.plan.id, PlanId.FREE, "an unpaid subscription grants nothing")
        self.assertEqual(ent.subscription.stripe_customer_id, "cus_123")
        self.deliver(event("evt_2", "customer.subscription.updated",
                           self.now(subscription(self.org.pk, "active"))))  # fmt: skip
        ent = entitlements_for(self.org)
        self.assertEqual((ent.plan.id, ent.subscription.status), (PlanId.TEAM, "active"))
        self.assertIsNotNone(ent.subscription.current_period_end)

    def test_payment_event_before_checkout_event_finds_the_organisation(self) -> None:
        # Observed in the sandbox: invoice.paid arrived before checkout.session.completed.
        self.now(subscription(self.org.pk, "active"))
        self.deliver(event("evt_1", "invoice.paid", invoice("in_1", "sub_123", self.org.pk)))
        self.assertEqual(entitlements_for(self.org).plan.id, PlanId.TEAM)

    def test_duplicate_events_are_applied_once(self) -> None:
        payload = event(
            "evt_dup",
            "customer.subscription.created",
            self.now(subscription(self.org.pk, "active")),
        )
        self.deliver(payload)
        response = self.deliver(payload)
        self.assertEqual(response.content, b"duplicate")
        self.assertEqual(StripeEvent.objects.filter(event_id="evt_dup").count(), 1)

    def test_older_events_cannot_undo_newer_state(self) -> None:
        self.deliver(event("evt_new", "customer.subscription.deleted",
                           self.now(subscription(self.org.pk, "canceled")), created=1_800_000_100))  # fmt: skip
        self.deliver(event("evt_old", "customer.subscription.updated",
                           subscription(self.org.pk, "active"), created=1_800_000_000))  # fmt: skip
        self.assertEqual(entitlements_for(self.org).plan.id, PlanId.FREE)

    def test_failed_payment_keeps_the_plan_for_the_grace_period_then_falls_back(self) -> None:
        self.deliver(event("evt_1", "customer.subscription.created",
                           self.now(subscription(self.org.pk, "active"))))  # fmt: skip
        self.now(subscription(self.org.pk, "past_due"))
        self.deliver(
            event("evt_2", "invoice.payment_failed", invoice("in_1", "sub_123", self.org.pk))
        )
        ent = entitlements_for(self.org)
        self.assertEqual(
            (ent.plan.id, ent.subscription.status), (PlanId.TEAM, SubscriptionStatus.PAST_DUE)
        )
        assert ent.warning is not None
        self.assertIn("payment failed", ent.warning.lower())
        later = timezone.now() + timedelta(days=settings.STRIPE["GRACE_DAYS"] + 1)
        plan, warning = effective_plan(ent.subscription, now=later)
        self.assertEqual(plan.id, PlanId.FREE)
        self.assertIn("grace period has ended", str(warning))
        self.now(subscription(self.org.pk, "active"))
        self.deliver(event("evt_3", "invoice.paid", invoice("in_1", "sub_123", self.org.pk)))
        ent = entitlements_for(self.org)
        self.assertEqual((ent.subscription.status, ent.warning), (SubscriptionStatus.ACTIVE, None))

    def test_cancellation_at_period_end_warns_then_downgrades(self) -> None:
        self.deliver(event("evt_1", "customer.subscription.updated",
                           self.now(subscription(self.org.pk, "active", cancel_at_period_end=True))))  # fmt: skip
        ent = entitlements_for(self.org)
        self.assertEqual(ent.plan.id, PlanId.TEAM)
        self.assertIn("cancelled", str(ent.warning))
        self.deliver(event("evt_2", "customer.subscription.deleted",
                           self.now(subscription(self.org.pk, "canceled")), created=1_800_000_500))  # fmt: skip
        self.assertEqual(entitlements_for(self.org).plan.id, PlanId.FREE)

    def test_flexible_billing_cancellation_uses_cancel_at(self) -> None:
        # Shape observed from the Customer Portal on a flexible billing mode subscription
        # (Stripe sandbox, 2026-10-30): cancel_at is set, cancel_at_period_end stays false.
        ends = 1_850_000_000
        self.deliver(event("evt_1", "customer.subscription.updated", self.now(subscription(
            self.org.pk, "active", cancel_at=ends, billing_mode={"type": "flexible"}))))  # fmt: skip
        ent = entitlements_for(self.org)
        self.assertEqual(ent.plan.id, PlanId.TEAM, "access lasts until the subscription ends")
        self.assertIn(f"ends on {datetime.fromtimestamp(ends, tz=UTC):%Y-%m-%d}", str(ent.warning))
        self.deliver(event("evt_2", "customer.subscription.updated",
                           self.now(subscription(self.org.pk, "active", cancel_at=None)),
                           created=1_800_000_100))  # fmt: skip
        ent = entitlements_for(self.org)
        self.assertEqual((ent.subscription.cancel_at, ent.warning), (None, None), "reactivated")

    def test_unknown_events_are_acknowledged_but_not_applied(self) -> None:
        response = self.deliver(
            event("evt_x", "customer.created", {"id": "cus_9", "object": "customer"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ignored", response.content)

    def test_checkout_redirects_to_stripe_only_for_owners(self) -> None:
        session = mock.Mock(url="https://checkout.stripe.com/c/pay/cs_test_1")
        with mock.patch(
            "stripe.checkout._session_service.SessionService.create", return_value=session
        ) as create:
            response = login(self.owner).post(reverse("billing:checkout", args=["team"]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], session.url)
        params = create.call_args.kwargs["params"]
        self.assertEqual(params["mode"], "subscription")
        self.assertEqual(params["line_items"], [{"price": "price_team_test", "quantity": 1}])
        self.assertEqual(params["client_reference_id"], str(self.org.pk))


class WebhookOrderingTests(StripeWebhookCase):
    """Late, repeated and same-second events, and events of an earlier subscription.

    Stripe does not deliver events in order and ``created`` has one-second resolution
    (https://docs.stripe.com/webhooks#event-ordering), so the outcome must follow Stripe's
    current state of the subscription, whatever order the events arrive in.
    """

    def setUp(self) -> None:
        super().setUp()
        self.org, self.owner = make_org("Ordered")

    def state(self) -> tuple[str, str, Any]:
        row = Subscription.objects.get(organization=self.org)
        return entitlements_for(self.org).plan.id, row.status, row.past_due_since

    def test_late_payment_failure_after_a_successful_payment_changes_nothing(self) -> None:
        active = self.stripe.put(subscription(self.org.pk, "active"))
        self.deliver(event("evt_1", "customer.subscription.created", active, created=100))
        self.deliver(event("evt_paid", "invoice.paid", invoice("in_1", "sub_123", self.org.pk),
                           created=102))  # fmt: skip
        # The failure of the first attempt arrives after the retry that succeeded.
        self.deliver(event("evt_failed", "invoice.payment_failed",
                           invoice("in_1", "sub_123", self.org.pk), created=101))  # fmt: skip
        self.assertEqual(self.state(), (PlanId.TEAM, SubscriptionStatus.ACTIVE, None))
        self.assertIsNone(entitlements_for(self.org).warning)

    def test_old_failure_of_a_previous_subscription_does_not_touch_the_new_one(self) -> None:
        self.stripe.put(subscription(self.org.pk, "canceled", id="sub_old"))
        new = self.stripe.put(subscription(self.org.pk, "active", id="sub_new"))
        self.deliver(event("evt_new", "customer.subscription.created", new, created=200))
        self.deliver(event("evt_old_fail", "invoice.payment_failed",
                           invoice("in_old", "sub_old", self.org.pk), created=300))  # fmt: skip
        self.assertEqual(self.state(), (PlanId.TEAM, SubscriptionStatus.ACTIVE, None))
        row = Subscription.objects.get(organization=self.org)
        self.assertEqual(row.stripe_subscription_id, "sub_new")

    def test_old_payment_success_does_not_end_the_grace_period_of_the_new_one(self) -> None:
        self.stripe.put(subscription(self.org.pk, "canceled", id="sub_old"))
        new = self.stripe.put(subscription(self.org.pk, "active", id="sub_new"))
        self.deliver(event("evt_new", "customer.subscription.created", new, created=200))
        overdue = self.stripe.put(subscription(self.org.pk, "past_due", id="sub_new"))
        self.deliver(event("evt_due", "customer.subscription.updated", overdue, created=250))
        self.deliver(event("evt_old_paid", "invoice.paid",
                           invoice("in_old", "sub_old", self.org.pk), created=300))  # fmt: skip
        plan, status, since = self.state()
        self.assertEqual((plan, status), (PlanId.TEAM, SubscriptionStatus.PAST_DUE))
        self.assertIsNotNone(since, "the grace period keeps running")

    def test_cancellation_of_a_previous_subscription_keeps_the_new_plan(self) -> None:
        old = self.stripe.put(subscription(self.org.pk, "canceled", id="sub_old"))
        new = self.stripe.put(subscription(self.org.pk, "active", id="sub_new"))
        self.deliver(event("evt_new", "customer.subscription.created", new, created=200))
        # Both the late copy (older) and a genuinely later end of the old subscription.
        self.deliver(event("evt_old_late", "customer.subscription.deleted", old, created=150))
        self.deliver(event("evt_old_later", "customer.subscription.deleted", old, created=250))
        self.assertEqual(self.state(), (PlanId.TEAM, SubscriptionStatus.ACTIVE, None))

    def test_events_with_the_same_created_second_follow_stripes_current_state(self) -> None:
        cancelled = subscription(self.org.pk, "active", cancel_at=1_850_000_000)
        reactivated = self.stripe.put(subscription(self.org.pk, "active", cancel_at=None))
        # Same second, delivered in the wrong order: the stale snapshot arrives last.
        self.deliver(event("evt_b", "customer.subscription.updated", reactivated, created=500))
        self.deliver(event("evt_a", "customer.subscription.updated", cancelled, created=500))
        row = Subscription.objects.get(organization=self.org)
        self.assertEqual((row.cancel_at, entitlements_for(self.org).warning), (None, None))

    def test_repeated_and_redundant_events_change_nothing(self) -> None:
        active = self.stripe.put(subscription(self.org.pk, "active"))
        self.deliver(event("evt_1", "customer.subscription.created", active))
        before = Subscription.objects.get(organization=self.org)
        audit = AuditEvent.objects.filter(organization=self.org).count()
        failed = event("evt_f", "invoice.payment_failed", invoice("in_1", "sub_123", self.org.pk))
        self.deliver(failed)
        self.assertEqual(self.deliver(failed).content, b"duplicate")
        # A second Event object for the same change (Stripe can send two).
        self.deliver(event("evt_2", "customer.subscription.updated", active))
        after = Subscription.objects.get(organization=self.org)
        self.assertEqual((after.status, after.updated_at), (before.status, before.updated_at))
        self.assertEqual(AuditEvent.objects.filter(organization=self.org).count(), audit)

    def test_an_event_cannot_attach_another_customers_subscription(self) -> None:
        other_org, _ = make_org("Other")
        Subscription.objects.filter(organization=other_org).update(stripe_customer_id="cus_999")
        mine = self.stripe.put(subscription(self.org.pk, "active"))
        self.deliver(event("evt_1", "customer.subscription.created", mine))
        # Metadata names this organisation, but the subscription belongs to cus_999.
        foreign = self.stripe.put(subscription(self.org.pk, "active", id="sub_x",
                                               customer="cus_999", price="price_business_test"))  # fmt: skip
        self.deliver(event("evt_2", "customer.subscription.created", foreign))
        row = Subscription.objects.get(organization=self.org)
        self.assertEqual((row.stripe_subscription_id, row.plan), ("sub_123", PlanId.TEAM))

    def test_failed_upgrade_keeps_the_previous_plan_during_the_grace_period(self) -> None:
        team = self.stripe.put(subscription(self.org.pk, "active"))
        self.deliver(event("evt_1", "customer.subscription.created", team))
        # allow_incomplete (Stripe's default): the new price applies although payment failed.
        upgraded = self.stripe.put(
            subscription(self.org.pk, "past_due", price="price_business_test")
        )
        self.deliver(event("evt_2", "customer.subscription.updated", upgraded))
        self.assertEqual(self.state()[:2], (PlanId.TEAM, SubscriptionStatus.PAST_DUE))
        self.stripe.put(subscription(self.org.pk, "active", price="price_business_test"))
        self.deliver(event("evt_3", "invoice.paid", invoice("in_2", "sub_123", self.org.pk)))
        self.assertEqual(self.state(), (PlanId.BUSINESS, SubscriptionStatus.ACTIVE, None))

    def test_pending_upgrade_grants_the_new_plan_only_when_applied(self) -> None:
        team = self.stripe.put(subscription(self.org.pk, "active"))
        self.deliver(event("evt_1", "customer.subscription.created", team))
        # pending_if_incomplete: the payment failed, the price is unchanged until it is paid.
        pending = self.stripe.put(subscription(self.org.pk, "active", pending_update={
            "expires_at": 1_800_082_800,
            "subscription_items": [{"id": "si_1", "price": {"id": "price_business_test"}}]}))  # fmt: skip
        self.deliver(event("evt_2", "customer.subscription.updated", pending))
        self.deliver(event("evt_3", "invoice.payment_failed",
                           invoice("in_2", "sub_123", self.org.pk)))  # fmt: skip
        self.assertEqual(self.state(), (PlanId.TEAM, SubscriptionStatus.ACTIVE, None))
        applied = self.stripe.put(subscription(self.org.pk, "active", price="price_business_test"))
        self.deliver(event("evt_4", "customer.subscription.pending_update_applied", applied))
        self.assertEqual(self.state(), (PlanId.BUSINESS, SubscriptionStatus.ACTIVE, None))

    def test_unreachable_stripe_api_makes_stripe_retry_the_event(self) -> None:
        self.stripe.put(subscription(self.org.pk, "active"))
        with mock.patch.object(StripeGateway, "subscription", side_effect=ProviderUnavailable):
            response = self.deliver(
                event("evt_1", "customer.subscription.created", subscription(self.org.pk, "active"))
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(StripeEvent.objects.filter(event_id="evt_1").exists(), "not consumed")
        self.assertEqual(self.deliver(event("evt_1", "customer.subscription.created",
                                            subscription(self.org.pk, "active"))).status_code, 200)  # fmt: skip
        self.assertEqual(self.state()[0], PlanId.TEAM)


@skipUnless(connection.vendor == "postgresql", "needs row locks (SELECT ... FOR UPDATE)")
@override_settings(STRIPE=STRIPE_TEST)
class ConcurrentWebhookTests(TransactionTestCase):
    """Two deliveries at the same time, each in its own thread and database connection."""

    def setUp(self) -> None:
        self.org, _ = make_org("Concurrent")
        self.stripe = FakeStripe()
        self.fetching, self.release = threading.Event(), threading.Event()

    def slow_retrieve(self, subscription_id: str) -> dict[str, Any] | None:
        snapshot = self.stripe.retrieve(None, subscription_id)
        if not self.fetching.is_set():  # only the first caller is held back
            self.fetching.set()
            self.release.wait(10)
        return snapshot

    def run_in_thread(self, payload: dict[str, Any], outcomes: list[str]) -> threading.Thread:
        def target() -> None:
            try:
                outcomes.append(process_event(payload, self.slow_retrieve))
            finally:
                connection.close()

        thread = threading.Thread(target=target)
        thread.start()
        return thread

    def test_a_slow_older_fetch_cannot_overwrite_a_newer_state(self) -> None:
        self.stripe.put(subscription(self.org.pk, "active", cancel_at=1_850_000_000))
        outcomes: list[str] = []
        first = self.run_in_thread(event("evt_a", "customer.subscription.updated",
                                         subscription(self.org.pk, "active")), outcomes)  # fmt: skip
        self.assertTrue(self.fetching.wait(10))
        # While the first delivery waits for its (now stale) answer, the customer reactivates.
        self.stripe.put(subscription(self.org.pk, "active", cancel_at=None))
        second = self.run_in_thread(event("evt_b", "customer.subscription.updated",
                                          subscription(self.org.pk, "active")), outcomes)  # fmt: skip
        second.join(0.5)
        self.assertTrue(second.is_alive(), "the second delivery waits for the row lock")
        self.release.set()
        first.join(10)
        second.join(10)
        self.assertEqual(len(outcomes), 2)
        self.assertIsNone(Subscription.objects.get(organization=self.org).cancel_at)

    def test_simultaneous_copies_of_one_event_are_applied_once(self) -> None:
        self.stripe.put(subscription(self.org.pk, "active"))
        payload = event("evt_same", "customer.subscription.created",
                        subscription(self.org.pk, "active"))  # fmt: skip
        outcomes: list[str] = []
        first = self.run_in_thread(payload, outcomes)
        self.assertTrue(self.fetching.wait(10))
        second = self.run_in_thread(payload, outcomes)
        second.join(0.5)
        self.release.set()
        first.join(10)
        second.join(10)
        self.assertEqual(sorted(outcomes)[0], "duplicate")
        self.assertEqual(self.stripe.retrieved, ["sub_123"], "Stripe was asked once")
        self.assertEqual(StripeEvent.objects.filter(event_id="evt_same").count(), 1)


SUBSCRIPTION_SERVICE = "stripe._subscription_service.SubscriptionService"


def stripe_object(values: dict[str, Any]) -> stripe.Subscription:
    return stripe.Subscription.construct_from(values, "sk_test_dummy")


@override_settings(STRIPE=STRIPE_TEST)
class PlanChangeTests(TestCase):
    """A plan change updates the one subscription; it never starts a second Checkout."""

    def setUp(self) -> None:
        self.org, self.owner = make_org("Changing")
        Subscription.objects.filter(organization=self.org).update(
            plan=PlanId.TEAM, status=SubscriptionStatus.ACTIVE,
            stripe_customer_id="cus_123", stripe_subscription_id="sub_123",
        )  # fmt: skip
        self.current = stripe_object({**subscription(self.org.pk, "active"), "items": {
            "data": [{"id": "si_1", "price": {"id": "price_team_test"}}]}})  # fmt: skip

    def choose(self, plan: str) -> Any:
        return login(self.owner).post(reverse("billing:checkout", args=[plan]))

    def test_upgrade_updates_the_subscription_and_waits_for_payment(self) -> None:
        updated = stripe_object({**subscription(self.org.pk, "active", price="price_business_test"),
                                 "pending_update": None, "latest_invoice": {"id": "in_1"}})  # fmt: skip
        with (
            mock.patch("stripe.checkout._session_service.SessionService.create") as checkout,
            mock.patch(f"{SUBSCRIPTION_SERVICE}.retrieve", return_value=self.current),
            mock.patch(f"{SUBSCRIPTION_SERVICE}.update", return_value=updated) as update,
        ):
            response = self.choose("business")
        checkout.assert_not_called()
        self.assertRedirects(response, reverse("billing:overview"), fetch_redirect_response=False)
        args, kwargs = update.call_args
        self.assertEqual(args[0], "sub_123")
        params = kwargs["params"]
        self.assertEqual(params["items"], [{"id": "si_1", "price": "price_business_test"}])
        self.assertEqual(
            (params["payment_behavior"], params["proration_behavior"]),
            ("pending_if_incomplete", "always_invoice"),
        )
        self.assertEqual(
            Subscription.objects.get(organization=self.org).plan,
            PlanId.TEAM,
            "the plan changes only through the webhook that follows the payment",
        )

    def test_declined_upgrade_sends_the_owner_to_the_open_invoice(self) -> None:
        pending = stripe_object({**subscription(self.org.pk, "active"),
            "pending_update": {"expires_at": 1_800_082_800, "subscription_items": [
                {"id": "si_1", "price": {"id": "price_business_test"}}]},
            "latest_invoice": {"id": "in_1", "status": "open",
                               "hosted_invoice_url": "https://invoice.stripe.com/i/test_1"}})  # fmt: skip
        with (
            mock.patch(f"{SUBSCRIPTION_SERVICE}.retrieve", return_value=self.current),
            mock.patch(f"{SUBSCRIPTION_SERVICE}.update", return_value=pending),
        ):
            response = self.choose("business")
        self.assertEqual(response["Location"], "https://invoice.stripe.com/i/test_1")
        self.assertEqual(entitlements_for(self.org).plan.id, PlanId.TEAM)
        # The billing page's CSP must allow that redirect, or the browser aborts it
        # (found in the Stripe sandbox: net::ERR_ABORTED).
        policy = login(self.owner).get(reverse("billing:overview"))["Content-Security-Policy"]
        form_action = next(d for d in policy.split(";") if d.strip().startswith("form-action"))
        self.assertIn("https://invoice.stripe.com", form_action)

    def test_no_plan_change_while_a_payment_is_overdue(self) -> None:
        Subscription.objects.filter(organization=self.org).update(
            status=SubscriptionStatus.PAST_DUE
        )
        with mock.patch(f"{SUBSCRIPTION_SERVICE}.update") as update:
            response = login(self.owner).post(
                reverse("billing:checkout", args=["business"]), follow=True
            )
        update.assert_not_called()
        self.assertContains(response, "open payment")

    def test_checkout_is_refused_when_stripe_already_has_a_live_subscription(self) -> None:
        # The local row lags behind (cancelled), but Stripe has a new active subscription.
        Subscription.objects.filter(organization=self.org).update(
            plan=PlanId.FREE, status=SubscriptionStatus.CANCELED
        )
        live = mock.Mock(data=[stripe_object(subscription(self.org.pk, "active", id="sub_new"))])
        with (
            mock.patch(f"{SUBSCRIPTION_SERVICE}.list", return_value=live),
            mock.patch("stripe.checkout._session_service.SessionService.create") as checkout,
        ):
            response = login(self.owner).post(
                reverse("billing:checkout", args=["team"]), follow=True
            )
        checkout.assert_not_called()
        self.assertContains(response, "already has a subscription")


class GatewaySafetyTests(TestCase):
    def test_test_keys_of_every_documented_kind_are_recognised(self) -> None:
        # rkcs_test_ is the restricted key of a claimable sandbox (`stripe sandbox create`).
        for key in ("sk_test_a", "rk_test_a", "rkcs_test_a"):  # pragma: allowlist secret
            self.assertTrue(is_test_key(key), key)
        for key in (
            "sk_live_a",
            "rk_live_a",
            "rkcs_live_a",
            "sk_a",
            "",
            "test_a",
        ):  # pragma: allowlist secret
            self.assertFalse(is_test_key(key), key)

    def test_live_keys_are_refused_until_live_payments_are_approved(self) -> None:
        _, owner = make_org("Live")
        with override_settings(STRIPE=STRIPE_LIVE):
            self.assertIsInstance(gateway(), UnconfiguredGateway)
            client = login(owner)
            self.assertContains(client.get(reverse("billing:overview")), "not been approved")
            response = client.post(reverse("billing:checkout", args=["team"]), follow=True)
            self.assertContains(response, LIVE_KEY_REFUSED)
        with override_settings(STRIPE={**STRIPE_LIVE, "ALLOW_LIVE": True}):
            self.assertIsInstance(gateway(), StripeGateway)

    @override_settings(STRIPE={**STRIPE_TEST, "PORTAL_CONFIGURATION": "bpc_test_1"})
    def test_portal_sessions_use_the_configured_portal_configuration(self) -> None:
        org, owner = make_org("Portal")
        Subscription.objects.filter(organization=org).update(stripe_customer_id="cus_portal")
        session = mock.Mock(url="https://billing.stripe.com/p/session/test_1")
        with mock.patch(
            "stripe.billing_portal._session_service.SessionService.create", return_value=session
        ) as create:
            response = login(owner).post(reverse("billing:portal"))
        self.assertEqual(response["Location"], session.url)
        params = create.call_args.kwargs["params"]
        self.assertEqual(
            (params["customer"], params["configuration"]), ("cus_portal", "bpc_test_1")
        )


class UnconfiguredBillingTests(TestCase):
    def test_billing_page_works_without_stripe_and_refuses_checkout(self) -> None:
        _, owner = make_org("Local")
        client = login(owner)
        self.assertContains(client.get(reverse("billing:overview")), "Payments are not configured")
        response = client.post(reverse("billing:checkout", args=["team"]), follow=True)
        self.assertContains(response, "not configured")


class LimitTests(TestCase):
    def test_free_plan_limits_are_enforced_on_the_server(self) -> None:
        org, owner = make_org("Limited")
        project = sample_project(org, "http://127.0.0.1:9")
        with self.assertRaises(LimitExceeded):
            require_scan_allowed(org, planned_checks=61)
        for _ in range(10):
            scan = services.approve_scan(project, owner)
            services.cancel_scan(scan, owner)
        with self.assertRaisesRegex(services.ScanError, "scans per month"):
            services.approve_scan(project, owner)
        demo = org.projects.get(is_demo=True)
        self.assertIsNotNone(services.approve_scan(demo, owner), "demo scans never count")

    def test_project_limit_redirects_to_billing(self) -> None:
        org, owner = make_org("One")
        sample_project(org, "http://127.0.0.1:9")
        response = login(owner).post(reverse("projects:new"), {"name": "Second"})
        self.assertRedirects(response, reverse("billing:overview"))
        self.assertEqual(org.projects.filter(is_demo=False).count(), 1)
