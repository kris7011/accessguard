"""Payment provider port with a Stripe implementation (test mode until approved for live).

Checkout, the Customer Portal and invoice payment pages are hosted by Stripe, so card data
never touches AccessGuard. A plan is granted only by verified webhooks, never by the
success redirect.

An organisation has at most one live subscription: a first purchase goes through
Checkout, every later plan change updates that subscription with
``payment_behavior=pending_if_incomplete``, so a higher plan applies only once it is paid
(https://docs.stripe.com/billing/subscriptions/pending-updates).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

import stripe

from accessguard_web import conf
from accessguard_web.billing.plans import PlanId


class BillingNotConfigured(Exception):  # noqa: N818 - a deployment state, shown to the user
    """Stripe keys or price ids are missing in this environment."""


class InvalidWebhook(Exception):  # noqa: N818
    """The webhook payload or its signature did not verify."""


class BillingRefused(Exception):  # noqa: N818 - user-facing condition, shown to the user
    """The requested purchase or plan change is not allowed in the current state."""


class ProviderUnavailable(Exception):  # noqa: N818 - a transient state, retried by Stripe
    """Stripe's API could not be reached or answered with an error."""


# Subscriptions in these states are over; any other state is live and blocks a second one.
ENDED_STATUSES = frozenset({"canceled", "incomplete_expired"})


@dataclass(frozen=True)
class CheckoutRequest:
    organization_id: int
    plan: PlanId
    customer_id: str | None
    customer_email: str
    success_url: str
    cancel_url: str


@dataclass(frozen=True)
class PlanChange:
    """Outcome of a plan change; ``invoice_url`` is set while the change waits for payment."""

    applied: bool
    invoice_url: str | None = None


class PaymentGateway(Protocol):
    def checkout_url(self, request: CheckoutRequest) -> str: ...

    def portal_url(self, customer_id: str, return_url: str) -> str: ...

    def change_plan(self, subscription_id: str, plan: PlanId) -> PlanChange: ...

    def verified_event(self, payload: bytes, signature: str) -> dict[str, Any]: ...

    def subscription(self, subscription_id: str) -> dict[str, Any] | None: ...

    def invoice(self, invoice_id: str) -> dict[str, Any] | None: ...

    def void_invoice(self, invoice_id: str) -> dict[str, Any]: ...


def price_for(plan: PlanId) -> str:
    prices = {
        PlanId.TEAM: conf.stripe()["PRICE_TEAM"],
        PlanId.BUSINESS: conf.stripe()["PRICE_BUSINESS"],
    }
    price = prices.get(plan)
    if not price:
        raise BillingNotConfigured(f"No Stripe price is configured for the {plan.value} plan")
    return str(price)


def plan_for_price(price_id: str) -> PlanId | None:
    prices = {
        conf.stripe()["PRICE_TEAM"]: PlanId.TEAM,
        conf.stripe()["PRICE_BUSINESS"]: PlanId.BUSINESS,
    }
    return prices.get(price_id) if price_id else None


class StripeGateway:
    def __init__(
        self, secret_key: str, webhook_secret: str, portal_configuration: str = ""
    ) -> None:
        self._client = stripe.StripeClient(secret_key, max_network_retries=2)
        self._webhook_secret = webhook_secret
        self._portal_configuration = portal_configuration

    def checkout_url(self, request: CheckoutRequest) -> str:
        params: dict[str, Any] = {
            "mode": "subscription",
            "line_items": [{"price": price_for(request.plan), "quantity": 1}],
            "success_url": request.success_url,
            "cancel_url": request.cancel_url,
            "client_reference_id": str(request.organization_id),
            "subscription_data": {"metadata": {"organization_id": str(request.organization_id)}},
            "metadata": {
                "organization_id": str(request.organization_id),
                "plan": request.plan.value,
            },
        }
        if request.customer_id:
            params["customer"] = request.customer_id
        else:
            params["customer_email"] = request.customer_email
        try:
            # The local state can lag behind the webhooks; Stripe's list cannot.
            if request.customer_id and self._has_live_subscription(request.customer_id):
                raise BillingRefused(
                    "This organisation already has a subscription. Change the plan here "
                    "once its status has updated, or manage it in the Customer Portal."
                )
            session = self._client.v1.checkout.sessions.create(params=params)  # type: ignore[arg-type]
        except stripe.StripeError as error:
            raise BillingNotConfigured(_provider_message(error)) from None
        if not session.url:
            raise BillingNotConfigured("Stripe did not return a checkout URL")
        return str(session.url)

    def portal_url(self, customer_id: str, return_url: str) -> str:
        params: dict[str, Any] = {"customer": customer_id, "return_url": return_url}
        if self._portal_configuration:
            params["configuration"] = self._portal_configuration
        try:
            session = self._client.v1.billing_portal.sessions.create(params=params)  # type: ignore[arg-type]
        except stripe.StripeError as error:
            raise BillingNotConfigured(_provider_message(error)) from None
        return str(session.url)

    def change_plan(self, subscription_id: str, plan: PlanId) -> PlanChange:
        price = price_for(plan)
        try:
            current = self._client.v1.subscriptions.retrieve(subscription_id).to_dict()
            if current.get("status") not in ("active", "trialing"):
                raise BillingRefused(
                    "The subscription has an open payment. Settle it in the Customer Portal "
                    "before changing the plan."
                )
            items = (current.get("items") or {}).get("data") or []
            if len(items) != 1:
                raise BillingRefused("This subscription cannot be changed here.")
            if (items[0].get("price") or {}).get("id") == price:
                return PlanChange(applied=True)
            # always_invoice charges the difference now; pending_if_incomplete keeps the old
            # price until that invoice is paid, and Stripe discards the change after 23 hours.
            updated = self._client.v1.subscriptions.update(
                subscription_id,
                params={
                    "items": [{"id": items[0]["id"], "price": price}],
                    "proration_behavior": "always_invoice",
                    "payment_behavior": "pending_if_incomplete",
                    "expand": ["latest_invoice"],
                },
            ).to_dict()
        except stripe.StripeError as error:
            raise BillingRefused(_provider_message(error)) from None
        if not updated.get("pending_update"):
            return PlanChange(applied=True)
        invoice = updated.get("latest_invoice")
        url = invoice.get("hosted_invoice_url") if isinstance(invoice, dict) else None
        return PlanChange(applied=False, invoice_url=str(url) if url else None)

    def verified_event(self, payload: bytes, signature: str) -> dict[str, Any]:
        try:
            event = self._client.construct_event(payload, signature, self._webhook_secret)
        except (ValueError, stripe.SignatureVerificationError) as error:
            raise InvalidWebhook(str(error)) from None
        return event.to_dict()

    def subscription(self, subscription_id: str) -> dict[str, Any] | None:
        """The subscription as it is now; ``None`` when Stripe does not know it."""
        try:
            return self._client.v1.subscriptions.retrieve(subscription_id).to_dict()
        except stripe.InvalidRequestError as error:
            if error.code == "resource_missing":
                return None
            raise ProviderUnavailable(type(error).__name__) from None
        except stripe.StripeError as error:
            raise ProviderUnavailable(type(error).__name__) from None

    def invoice(self, invoice_id: str) -> dict[str, Any] | None:
        """The invoice as it is now; ``None`` when Stripe does not know it."""
        try:
            return self._client.v1.invoices.retrieve(invoice_id).to_dict()
        except stripe.InvalidRequestError as error:
            if error.code == "resource_missing":
                return None
            raise ProviderUnavailable(type(error).__name__) from None
        except stripe.StripeError as error:
            raise ProviderUnavailable(type(error).__name__) from None

    def void_invoice(self, invoice_id: str) -> dict[str, Any]:
        """Void an open invoice and return it as it is afterwards.

        Stripe refuses to void an invoice that is no longer open, for instance one the
        customer paid a moment earlier. Such a refusal is an answer, not an outage: the
        invoice is fetched again and returned as it is, so the caller sees what happened.
        """
        try:
            return self._client.v1.invoices.void_invoice(invoice_id).to_dict()
        except stripe.InvalidRequestError:
            current = self.invoice(invoice_id)
            if current is None:
                raise ProviderUnavailable("invoice vanished while voiding") from None
            return current
        except stripe.StripeError as error:
            raise ProviderUnavailable(type(error).__name__) from None

    def _has_live_subscription(self, customer_id: str) -> bool:
        found = self._client.v1.subscriptions.list(
            params={"customer": customer_id, "status": "all", "limit": 100}
        )
        return any(s.status not in ENDED_STATUSES for s in found.data)


class UnconfiguredGateway:
    def __init__(self, reason: str = "Payments are not configured in this environment") -> None:
        self._reason = reason

    def checkout_url(self, request: CheckoutRequest) -> str:
        raise BillingNotConfigured(self._reason)

    def portal_url(self, customer_id: str, return_url: str) -> str:
        raise BillingNotConfigured(self._reason)

    def change_plan(self, subscription_id: str, plan: PlanId) -> PlanChange:
        raise BillingNotConfigured(self._reason)

    def verified_event(self, payload: bytes, signature: str) -> dict[str, Any]:
        raise InvalidWebhook("Webhooks are not configured in this environment")

    def subscription(self, subscription_id: str) -> dict[str, Any] | None:
        raise ProviderUnavailable(self._reason)

    def invoice(self, invoice_id: str) -> dict[str, Any] | None:
        raise ProviderUnavailable(self._reason)

    def void_invoice(self, invoice_id: str) -> dict[str, Any]:
        raise ProviderUnavailable(self._reason)


def _provider_message(error: stripe.StripeError) -> str:
    reason = error.user_message or type(error).__name__
    return f"The payment provider refused the request: {reason}"


LIVE_KEY_REFUSED = "Live Stripe keys are refused until live payments are approved"


def gateway() -> PaymentGateway:
    config = conf.stripe()
    if not (config["SECRET_KEY"] and config["WEBHOOK_SECRET"]):
        return UnconfiguredGateway()
    if not is_test_key(config["SECRET_KEY"]) and not config["ALLOW_LIVE"]:
        return UnconfiguredGateway(LIVE_KEY_REFUSED)
    return StripeGateway(
        config["SECRET_KEY"], config["WEBHOOK_SECRET"], config["PORTAL_CONFIGURATION"]
    )


# Secret (sk_), restricted (rk_) and claimable-sandbox (rkcs_) keys all carry their mode
# after the first underscore. Anything that is not clearly a test key counts as live.
_TEST_KEY = re.compile(r"^[a-z]+_test_")


def is_test_key(secret_key: str) -> bool:
    return bool(_TEST_KEY.match(secret_key))
