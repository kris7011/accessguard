"""Payment provider port with a Stripe implementation (test mode until approved for live).

Checkout and the Customer Portal are hosted by Stripe, so card data never touches
AccessGuard. A plan is granted only by verified webhooks, never by the success redirect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import stripe

from accessguard_web import conf
from accessguard_web.billing.plans import PlanId


class BillingNotConfigured(Exception):  # noqa: N818 - a deployment state, shown to the user
    """Stripe keys or price ids are missing in this environment."""


class InvalidWebhook(Exception):  # noqa: N818
    """The webhook payload or its signature did not verify."""


@dataclass(frozen=True)
class CheckoutRequest:
    organization_id: int
    plan: PlanId
    customer_id: str | None
    customer_email: str
    success_url: str
    cancel_url: str


class PaymentGateway(Protocol):
    def checkout_url(self, request: CheckoutRequest) -> str: ...

    def portal_url(self, customer_id: str, return_url: str) -> str: ...

    def verified_event(self, payload: bytes, signature: str) -> dict[str, Any]: ...


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
    def __init__(self, secret_key: str, webhook_secret: str) -> None:
        self._client = stripe.StripeClient(secret_key, max_network_retries=2)
        self._webhook_secret = webhook_secret

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
            session = self._client.v1.checkout.sessions.create(params=params)  # type: ignore[arg-type]
        except stripe.StripeError as error:
            raise BillingNotConfigured(_provider_message(error)) from None
        if not session.url:
            raise BillingNotConfigured("Stripe did not return a checkout URL")
        return str(session.url)

    def portal_url(self, customer_id: str, return_url: str) -> str:
        try:
            session = self._client.v1.billing_portal.sessions.create(
                params={"customer": customer_id, "return_url": return_url}
            )
        except stripe.StripeError as error:
            raise BillingNotConfigured(_provider_message(error)) from None
        return str(session.url)

    def verified_event(self, payload: bytes, signature: str) -> dict[str, Any]:
        try:
            event = self._client.construct_event(payload, signature, self._webhook_secret)
        except (ValueError, stripe.SignatureVerificationError) as error:
            raise InvalidWebhook(str(error)) from None
        return event.to_dict()


class UnconfiguredGateway:
    def checkout_url(self, request: CheckoutRequest) -> str:
        raise BillingNotConfigured("Payments are not configured in this environment")

    def portal_url(self, customer_id: str, return_url: str) -> str:
        raise BillingNotConfigured("Payments are not configured in this environment")

    def verified_event(self, payload: bytes, signature: str) -> dict[str, Any]:
        raise InvalidWebhook("Webhooks are not configured in this environment")


def _provider_message(error: stripe.StripeError) -> str:
    reason = error.user_message or type(error).__name__
    return f"The payment provider refused the request: {reason}"


def gateway() -> PaymentGateway:
    config = conf.stripe()
    if config["SECRET_KEY"] and config["WEBHOOK_SECRET"]:
        return StripeGateway(config["SECRET_KEY"], config["WEBHOOK_SECRET"])
    return UnconfiguredGateway()


def is_live_key(secret_key: str) -> bool:
    return secret_key.startswith(("sk_live_", "rk_live_"))
