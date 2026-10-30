"""Stripe webhooks (signature, idempotency, ordering), entitlements and plan limits.

Signatures are generated with the official SDK's documented test helper, so the real
verification code runs. A real Stripe test account was not available for this suite.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import stripe
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accessguard_web.billing.entitlements import (
    LimitExceeded,
    effective_plan,
    entitlements_for,
    require_scan_allowed,
)
from accessguard_web.billing.gateway import (
    LIVE_KEY_REFUSED,
    StripeGateway,
    UnconfiguredGateway,
    gateway,
    is_test_key,
)
from accessguard_web.billing.models import StripeEvent, Subscription, SubscriptionStatus
from accessguard_web.billing.plans import PlanId
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


@override_settings(STRIPE=STRIPE_TEST)
class WebhookTests(TestCase):
    def setUp(self) -> None:
        self.org, self.owner = make_org("Paying")

    def deliver(self, payload: dict[str, Any], *, secret: str = WEBHOOK_SECRET) -> Any:
        body = json.dumps(payload)
        header = stripe.WebhookSignature.generate_signature_header(body, secret)
        return self.client.post(
            reverse("stripe_webhook"), data=body, content_type="application/json",
            HTTP_STRIPE_SIGNATURE=header,
        )  # fmt: skip

    def test_invalid_or_missing_signatures_are_rejected(self) -> None:
        payload = event(
            "evt_1", "customer.subscription.created", subscription(self.org.pk, "active")
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

    def test_checkout_then_active_subscription_grants_the_plan(self) -> None:
        checkout = {"id": "cs_1", "object": "checkout.session", "client_reference_id": str(self.org.pk),
                    "customer": "cus_123", "subscription": "sub_123", "metadata": {}}  # fmt: skip
        self.assertEqual(
            self.deliver(event("evt_1", "checkout.session.completed", checkout)).status_code, 200
        )
        self.assertEqual(
            entitlements_for(self.org).plan.id, PlanId.FREE, "checkout alone grants nothing"
        )
        self.deliver(
            event("evt_2", "customer.subscription.created", subscription(self.org.pk, "active"))
        )
        ent = entitlements_for(self.org)
        self.assertEqual((ent.plan.id, ent.subscription.status), (PlanId.TEAM, "active"))
        self.assertIsNotNone(ent.subscription.current_period_end)

    def test_duplicate_events_are_applied_once(self) -> None:
        payload = event(
            "evt_dup", "customer.subscription.created", subscription(self.org.pk, "active")
        )
        self.deliver(payload)
        response = self.deliver(payload)
        self.assertEqual(response.content, b"duplicate")
        self.assertEqual(StripeEvent.objects.filter(event_id="evt_dup").count(), 1)

    def test_older_events_cannot_undo_newer_state(self) -> None:
        self.deliver(event("evt_new", "customer.subscription.deleted",
                           subscription(self.org.pk, "canceled"), created=1_800_000_100))  # fmt: skip
        response = self.deliver(event("evt_old", "customer.subscription.updated",
                                      subscription(self.org.pk, "active"), created=1_800_000_000))  # fmt: skip
        self.assertIn(b"older", response.content)
        self.assertEqual(entitlements_for(self.org).plan.id, PlanId.FREE)

    def test_failed_payment_keeps_the_plan_for_the_grace_period_then_falls_back(self) -> None:
        self.deliver(
            event("evt_1", "customer.subscription.created", subscription(self.org.pk, "active"))
        )
        self.deliver(event("evt_2", "invoice.payment_failed", {"id": "in_1", "object": "invoice",
                     "customer": "cus_123", "subscription": "sub_123"}))  # fmt: skip
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
        self.deliver(event("evt_3", "invoice.paid", {"id": "in_1", "object": "invoice", "customer": "cus_123",
                     "subscription": "sub_123"}))  # fmt: skip
        ent = entitlements_for(self.org)
        self.assertEqual((ent.subscription.status, ent.warning), (SubscriptionStatus.ACTIVE, None))

    def test_cancellation_at_period_end_warns_then_downgrades(self) -> None:
        self.deliver(event("evt_1", "customer.subscription.updated",
                           subscription(self.org.pk, "active", cancel_at_period_end=True)))  # fmt: skip
        ent = entitlements_for(self.org)
        self.assertEqual(ent.plan.id, PlanId.TEAM)
        self.assertIn("cancelled", str(ent.warning))
        self.deliver(event("evt_2", "customer.subscription.deleted",
                           subscription(self.org.pk, "canceled"), created=1_800_000_500))  # fmt: skip
        self.assertEqual(entitlements_for(self.org).plan.id, PlanId.FREE)

    def test_flexible_billing_cancellation_uses_cancel_at(self) -> None:
        # Shape observed from the Customer Portal on a flexible billing mode subscription
        # (Stripe sandbox, 2026-10-30): cancel_at is set, cancel_at_period_end stays false.
        ends = 1_850_000_000
        self.deliver(event("evt_1", "customer.subscription.updated", subscription(
            self.org.pk, "active", cancel_at=ends, billing_mode={"type": "flexible"})))  # fmt: skip
        ent = entitlements_for(self.org)
        self.assertEqual(ent.plan.id, PlanId.TEAM, "access lasts until the subscription ends")
        self.assertIn(f"ends on {datetime.fromtimestamp(ends, tz=UTC):%Y-%m-%d}", str(ent.warning))
        self.deliver(event("evt_2", "customer.subscription.updated",
                           subscription(self.org.pk, "active", cancel_at=None),
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
        from unittest import mock

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
        from unittest import mock

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
