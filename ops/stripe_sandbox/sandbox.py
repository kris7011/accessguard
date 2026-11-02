"""Drive AccessGuard billing against a Stripe sandbox (test mode only, explicit opt-in).

    ACCESSGUARD_STRIPE_SANDBOX=1 python ops/stripe_sandbox/sandbox.py <command> [...]

Commands (``<email>`` is an organisation owner in the sandbox database):

    setup                         create or update products, EUR prices and the portal
                                  configuration; writes the non-secret ids to the env file
    events                        the event types to pass to ``stripe listen --events``
    show <email>                  the subscription as Stripe and as AccessGuard see it
    card <email> fail|ok          make the customer's next charges fail or succeed
    pay-open <email>              pay the subscription's open invoice with a working card
    fail-renewal <email>          bill a new period now with a failing card (no test clock;
                                  a credit for the unused period can make the invoice EUR 0)
    fail-upgrade <email>          upgrade to Business with allow_incomplete and a failing
                                  card: the price applies before payment, status past_due
    end-in <email> <seconds>      schedule the end of the subscription
    events-since <email> <epoch>  the organisation's handled events since then, newest
                                  first (to redeliver them late with ``stripe events resend``)

Test clocks (renewals and Stripe's retry schedule; need a sandbox that allows test clocks):

    clock-probe                   create and delete an empty test clock: are clocks allowed?
    clock-start <email> <plan>    a subscription on a new test clock (customer with our
                                  metadata, working card); creates the local owner if missing
    clock-renew <email>           advance past the period end and the hour Stripe keeps the
                                  renewal invoice in draft, so it is finalised and charged
    clock-next-retry <email>      advance just past the open invoice's next payment attempt
    clock-advance <email> <hours> advance by a number of hours
    clock-delete <email>          delete the clock with its customer and subscription

Safety: nothing runs without ``ACCESSGUARD_STRIPE_SANDBOX=1``; only test-mode keys are
accepted (``is_test_key``); every Stripe object touched must say ``livemode: false``; the
app database must be the separate sandbox database, never ``.local/dev.sqlite3``. The key
is read from a git-ignored env file and never printed. See README.md in this folder.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import stripe

if TYPE_CHECKING:
    from accessguard_web.accounts.models import Organization

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = Path(os.environ.get("ACCESSGUARD_STRIPE_ENV_FILE", ROOT / ".local/stripe/stripe.env"))
DATABASE = ROOT / ".local/stripe-e2e.sqlite3"
BASE_URL = os.environ.get("ACCESSGUARD_SANDBOX_BASE_URL", "http://127.0.0.1:8000")
# Password of the local test users only (never a real account).
PASSWORD = "correct horse battery 42"  # noqa: S105  # pragma: allowlist secret
PLANS = {
    "team": ("AccessGuard Team", 4900, "STRIPE_PRICE_TEAM"),
    "business": ("AccessGuard Business", 19900, "STRIPE_PRICE_BUSINESS"),
}
# Stripe's documented test payment methods.
CARD_OK, CARD_FAILS = "pm_card_visa", "pm_card_chargeCustomerFail"
# Stripe keeps a renewal invoice in draft for about an hour before it finalises and charges it.
DRAFT_SECONDS = 60 * 60
# Margin past a point in simulated time, so the event at that point has happened.
CLOCK_MARGIN_SECONDS = 5 * 60
CLOCK_TIMEOUT_SECONDS = 180
# Stripe's error messages can quote a masked key ("rkcs_test_51***...cZIB"); never print it.
_KEY_IN_TEXT = re.compile(r"'?(?<![A-Za-z0-9_])[a-z]+_(?:test|live)_[A-Za-z0-9*]+'?")


def require_opt_in() -> None:
    if os.environ.get("ACCESSGUARD_STRIPE_SANDBOX") != "1":
        raise SystemExit(
            "Refusing to call Stripe: set ACCESSGUARD_STRIPE_SANDBOX=1 to opt in (test mode "
            "only; see ops/stripe_sandbox/README.md)."
        )
    if not ENV_FILE.is_file():
        raise SystemExit(f"Missing env file {ENV_FILE} (see README.md: never commit it)")


def read_env() -> dict[str, str]:
    values = {}
    for raw in ENV_FILE.read_text("utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    return values


def write_env(values: dict[str, str]) -> None:
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in values.items()), "utf-8")


def client() -> stripe.StripeClient:
    require_opt_in()
    sys.path.insert(0, str(ROOT / "web"))
    from accessguard_web.billing.gateway import is_test_key  # noqa: PLC0415

    key = read_env().get("STRIPE_SECRET_KEY", "")
    if not is_test_key(key):
        raise SystemExit("Refusing: STRIPE_SECRET_KEY is not a test-mode key")
    return stripe.StripeClient(key)


def assert_test_mode(*objects: Any) -> None:
    for obj in objects:
        if getattr(obj, "livemode", None) is not False:
            raise SystemExit(f"Refusing: {getattr(obj, 'id', obj)} is not a test-mode object")


def django_setup() -> None:
    """Point Django at the sandbox database with the sandbox's Stripe settings."""
    require_opt_in()
    url = f"sqlite:///{DATABASE.as_posix()}"
    if os.environ.get("DATABASE_URL", url) != url:
        raise SystemExit(f"Refusing: DATABASE_URL must be the sandbox database {url}")
    os.environ.update(read_env())
    os.environ["DATABASE_URL"] = url
    os.environ.setdefault("ACCESSGUARD_DEBUG", "1")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "accessguard_web.settings")
    for path in ("src", "web"):
        sys.path.insert(0, str(ROOT / path))
    import django  # noqa: PLC0415

    django.setup()


def organization(email: str) -> Organization:
    from accessguard_web.accounts.models import Membership  # noqa: PLC0415

    return Membership.objects.select_related("organization").get(user__email=email).organization


def app_state(email: str) -> dict[str, Any]:
    from accessguard_web.billing.entitlements import entitlements_for  # noqa: PLC0415

    ent = entitlements_for(organization(email))
    row = ent.subscription
    return {
        "effective_plan": ent.plan.id.value, "plan": row.plan, "status": row.status,
        "subscription": row.stripe_subscription_id, "customer": row.stripe_customer_id,
        "cancel_at": row.cancel_at, "past_due_since": row.past_due_since,
        "updated_at": row.updated_at, "warning": ent.warning,
    }  # fmt: skip


def stripe_state(c: stripe.StripeClient, subscription_id: str) -> dict[str, Any]:
    s = c.v1.subscriptions.retrieve(subscription_id, params={"expand": ["latest_invoice"]})
    assert_test_mode(s)
    item = s["items"].data[0]
    invoice = s.latest_invoice
    pending = s.pending_update
    return {
        "id": s.id, "status": s.status, "price": item.price.id,
        "pending_price": (
            pending.subscription_items[0].price.id
            if pending and pending.subscription_items
            else None
        ),
        "latest_invoice": (
            f"{invoice.id} {invoice.status} {invoice.billing_reason} {invoice.amount_due} "
            f"attempts={invoice.attempt_count} next_attempt={invoice.next_payment_attempt}"
            if isinstance(invoice, stripe.Invoice) else invoice
        ),
        "cancel_at": s.cancel_at,
    }  # fmt: skip


def handled_events() -> list[str]:
    sys.path.insert(0, str(ROOT / "web"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "accessguard_web.settings")
    os.environ.setdefault("ACCESSGUARD_TESTING", "1")
    import django  # noqa: PLC0415

    django.setup()
    from accessguard_web.billing.webhooks import HANDLED_EVENTS  # noqa: PLC0415

    return sorted(HANDLED_EVENTS)


def setup(c: stripe.StripeClient, portal_proration: str) -> None:
    values = read_env()
    products = {}
    for plan, (name, amount, env_name) in PLANS.items():
        lookup = f"accessguard_{plan}_monthly_eur"
        found = c.v1.prices.list(params={"lookup_keys": [lookup], "expand": ["data.product"]})
        if found.data:
            price, product = found.data[0], found.data[0].product
        else:
            product = c.v1.products.create(params={"name": name, "metadata": {"plan": plan}})
            price = c.v1.prices.create(params={
                "product": product.id, "currency": "eur", "unit_amount": amount,
                "recurring": {"interval": "month"}, "lookup_key": lookup,
            })  # fmt: skip
        assert_test_mode(price, product)
        products[plan] = product if isinstance(product, str) else product.id
        values[env_name] = price.id
        print(f"{plan}: price {price.id} {price.unit_amount} {price.currency}/month")
    params: dict[str, Any] = {
        "name": "AccessGuard sandbox",
        "default_return_url": f"{BASE_URL}/app/billing/",
        "business_profile": {"headline": "AccessGuard (Stripe sandbox, test mode)"},
        "features": {
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
            "subscription_update": {
                "enabled": True,
                "default_allowed_updates": ["price"],
                "proration_behavior": portal_proration,
                "products": [
                    {"product": products[p], "prices": [values[PLANS[p][2]]]} for p in PLANS
                ],
            },
        },
    }
    existing = values.get("STRIPE_PORTAL_CONFIGURATION")
    config = (
        c.v1.billing_portal.configurations.update(existing, params=params)  # type: ignore[arg-type]
        if existing
        else c.v1.billing_portal.configurations.create(params=params)  # type: ignore[arg-type]
    )
    assert_test_mode(config)
    values["STRIPE_PORTAL_CONFIGURATION"] = config.id
    print(f"portal configuration {config.id}: plan switch proration {portal_proration}")
    write_env(values)


def set_card(c: stripe.StripeClient, email: str, token: str) -> str:
    state = app_state(email)
    pm = c.v1.payment_methods.attach(token, params={"customer": state["customer"]})
    assert_test_mode(pm)
    c.v1.customers.update(
        state["customer"], params={"invoice_settings": {"default_payment_method": pm.id}}
    )
    # Checkout stores the card on the subscription too, and that one is charged first.
    c.v1.subscriptions.update(state["subscription"], params={"default_payment_method": pm.id})
    return str(pm.id)


def pay_open(c: stripe.StripeClient, email: str) -> str:
    pm = set_card(c, email, CARD_OK)
    s = c.v1.subscriptions.retrieve(app_state(email)["subscription"])
    invoice = c.v1.invoices.pay(str(s.latest_invoice), params={"payment_method": pm})
    assert_test_mode(invoice)
    return f"invoice {invoice.id} {invoice.status}"


def fail_renewal(c: stripe.StripeClient, email: str) -> str:
    pm = set_card(c, email, CARD_FAILS)
    s = c.v1.subscriptions.update(app_state(email)["subscription"], params={
        "default_payment_method": pm, "billing_cycle_anchor": {"type": "now"},
        # With flexible billing mode, "none" resets the anchor without invoicing.
        "proration_behavior": "always_invoice", "payment_behavior": "allow_incomplete",
    })  # fmt: skip
    return f"latest invoice {s.latest_invoice}"


def events_since(c: stripe.StripeClient, email: str, since: int) -> list[str]:
    state = app_state(email)
    handled = set(handled_events())
    found = c.v1.events.list(params={"created": {"gte": since}, "limit": 100})
    lines = []
    for e in found.data:  # Stripe lists newest first
        obj = e.data.object.to_dict()
        related = {obj.get("id"), obj.get("subscription"), obj.get("customer")}
        if e.type in handled and related & {state["subscription"], state["customer"]}:
            lines.append(f"{e.id} {e.type} {e.created}")
    return lines


def fail_upgrade(c: stripe.StripeClient, email: str) -> str:
    """Upgrade to Business the way Stripe does by default (``allow_incomplete``), card failing.

    Stands in for any path that applies a price before payment; the app must keep the
    previous plan while the subscription is ``past_due``.
    """
    pm = set_card(c, email, CARD_FAILS)
    sub_id = app_state(email)["subscription"]
    item = c.v1.subscriptions.retrieve(sub_id)["items"].data[0]
    s = c.v1.subscriptions.update(sub_id, params={
        "default_payment_method": pm,
        "items": [{"id": item.id, "price": read_env()["STRIPE_PRICE_BUSINESS"]}],
        "proration_behavior": "always_invoice", "payment_behavior": "allow_incomplete",
    })  # fmt: skip
    return f"latest invoice {s.latest_invoice}"


def renewal_steps(period_end: int) -> list[int]:
    """Simulated times for a renewal: just past the period end, then past the draft hour."""
    return [period_end + CLOCK_MARGIN_SECONDS, period_end + DRAFT_SECONDS + CLOCK_MARGIN_SECONDS]


def clock_probe(c: stripe.StripeClient) -> str:
    clock = c.v1.test_helpers.test_clocks.create(
        params={"frozen_time": int(time.time()), "name": "accessguard probe"}
    )
    assert_test_mode(clock)
    c.v1.test_helpers.test_clocks.delete(clock.id)
    return f"test clocks are available (created and deleted {clock.id})"


def ensure_owner(email: str) -> Organization:
    """The organisation of a local test owner, created in the sandbox database if missing."""
    from accessguard_web.accounts.models import Membership, User  # noqa: PLC0415
    from accessguard_web.accounts.views import create_organization  # noqa: PLC0415

    if not Membership.objects.filter(user__email=email).exists():
        user = User.objects.create_user(email, PASSWORD)
        create_organization(user, f"Clock {email.split('@', maxsplit=1)[0]}")
    return organization(email)


def clock_start(c: stripe.StripeClient, email: str, plan: str) -> str:
    org = ensure_owner(email)
    if app_state(email)["subscription"]:
        raise SystemExit("Refusing: this organisation already has a subscription; use a new e-mail")
    metadata = {"organization_id": str(org.pk)}
    clock = c.v1.test_helpers.test_clocks.create(
        params={"frozen_time": int(time.time()), "name": f"accessguard {email}"}
    )
    customer = c.v1.customers.create(
        params={"email": email, "test_clock": clock.id, "metadata": metadata}
    )
    pm = c.v1.payment_methods.attach(CARD_OK, params={"customer": customer.id})
    c.v1.customers.update(
        customer.id, params={"invoice_settings": {"default_payment_method": pm.id}}
    )
    s = c.v1.subscriptions.create(params={
        "customer": customer.id, "items": [{"price": read_env()[PLANS[plan][2]]}],
        "default_payment_method": pm.id, "metadata": metadata,
    })  # fmt: skip
    assert_test_mode(clock, customer, pm, s)
    deadline = time.monotonic() + 60
    while app_state(email)["subscription"] != s.id:
        if time.monotonic() > deadline:
            return f"{clock.id} {s.id} {s.status}: not in the app yet; is stripe listen running?"
        time.sleep(2)
    return f"clock {clock.id}, subscription {s.id} {s.status}"


def subscription_clock(c: stripe.StripeClient, email: str) -> tuple[stripe.Subscription, str]:
    state = app_state(email)
    s = c.v1.subscriptions.retrieve(state["subscription"], params={"expand": ["latest_invoice"]})
    customer = c.v1.customers.retrieve(state["customer"])
    assert_test_mode(s, customer)
    if not customer.test_clock:
        raise SystemExit("Refusing: this customer is not on a test clock (see clock-start)")
    return s, str(customer.test_clock)


def advance_clock(c: stripe.StripeClient, clock_id: str, frozen_time: int) -> str:
    clocks = c.v1.test_helpers.test_clocks
    clocks.advance(clock_id, params={"frozen_time": frozen_time})
    deadline = time.monotonic() + CLOCK_TIMEOUT_SECONDS
    while (clock := clocks.retrieve(clock_id)).status != "ready":
        if clock.status == "internal_failure":
            raise SystemExit(f"Stripe could not advance {clock_id}: {clock.status_details}")
        if time.monotonic() > deadline:
            raise SystemExit(f"{clock_id} is still {clock.status} after {CLOCK_TIMEOUT_SECONDS} s")
        time.sleep(2)
    return f"clock {clock_id} at {clock.frozen_time}"


def clock_renew(c: stripe.StripeClient, email: str) -> str:
    s, clock_id = subscription_clock(c, email)
    period_end = s["items"].data[0].current_period_end
    return "\n".join(advance_clock(c, clock_id, at) for at in renewal_steps(period_end))


def clock_next_retry(c: stripe.StripeClient, email: str) -> str:
    s, clock_id = subscription_clock(c, email)
    invoice = s.latest_invoice
    if not isinstance(invoice, stripe.Invoice) or not invoice.next_payment_attempt:
        return "no payment attempt is scheduled (paid, or the retries are exhausted)"
    return advance_clock(c, clock_id, invoice.next_payment_attempt + CLOCK_MARGIN_SECONDS)


def clock_advance(c: stripe.StripeClient, email: str, hours: int) -> str:
    _, clock_id = subscription_clock(c, email)
    now = c.v1.test_helpers.test_clocks.retrieve(clock_id).frozen_time
    return advance_clock(c, clock_id, now + hours * 3600)


def end_in(c: stripe.StripeClient, email: str, seconds: int) -> str:
    at = int(time.time()) + seconds
    c.v1.subscriptions.update(
        app_state(email)["subscription"], params={"cancel_at": at, "proration_behavior": "none"}
    )
    return f"cancel_at {at}"


def clock_delete(c: stripe.StripeClient, email: str) -> str:
    _, clock_id = subscription_clock(c, email)
    c.v1.test_helpers.test_clocks.delete(clock_id)
    return f"deleted {clock_id} with its customer and subscription"


Action = Callable[[stripe.StripeClient, argparse.Namespace], str]
# Commands on one organisation; each prints its result and then both views of the subscription.
ACTIONS: dict[str, Action] = {
    "show": lambda c, a: "",
    "card": lambda c, a: set_card(c, a.email, CARD_FAILS if a.outcome == "fail" else CARD_OK),
    "pay-open": lambda c, a: pay_open(c, a.email),
    "fail-renewal": lambda c, a: fail_renewal(c, a.email),
    "fail-upgrade": lambda c, a: fail_upgrade(c, a.email),
    "end-in": lambda c, a: end_in(c, a.email, a.seconds),
    "clock-start": lambda c, a: clock_start(c, a.email, a.plan),
    "clock-renew": lambda c, a: clock_renew(c, a.email),
    "clock-next-retry": lambda c, a: clock_next_retry(c, a.email),
    "clock-advance": lambda c, a: clock_advance(c, a.email, a.hours),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    setup_cmd = sub.add_parser("setup")
    setup_cmd.add_argument(
        "--portal-proration", default="always_invoice",
        choices=["always_invoice", "create_prorations", "none"],
    )  # fmt: skip
    sub.add_parser("events")
    sub.add_parser("clock-probe")
    for name in ("show", "pay-open", "fail-renewal", "fail-upgrade", "clock-renew",
                 "clock-next-retry", "clock-delete"):  # fmt: skip
        sub.add_parser(name).add_argument("email")
    card = sub.add_parser("card")
    card.add_argument("email")
    card.add_argument("outcome", choices=["fail", "ok"])
    end = sub.add_parser("end-in")
    end.add_argument("email")
    end.add_argument("seconds", type=int)
    since = sub.add_parser("events-since")
    since.add_argument("email")
    since.add_argument("epoch", type=int)
    start = sub.add_parser("clock-start")
    start.add_argument("email")
    start.add_argument("plan", choices=sorted(PLANS))
    forward = sub.add_parser("clock-advance")
    forward.add_argument("email")
    forward.add_argument("hours", type=int)
    return parser.parse_args()


def run(c: stripe.StripeClient, args: argparse.Namespace) -> None:
    if args.command == "setup":
        setup(c, args.portal_proration)
        return
    if args.command == "clock-probe":
        print(clock_probe(c))
        return
    django_setup()
    if args.command == "events-since":
        print("\n".join(events_since(c, args.email, args.epoch)))
        return
    if args.command == "clock-delete":
        print(clock_delete(c, args.email))
        return
    result = ACTIONS[args.command](c, args)
    if result:
        print(result)
    state = app_state(args.email)
    print("stripe:", stripe_state(c, state["subscription"]))
    print("app:   ", state)
    found = c.v1.subscriptions.list(params={"customer": state["customer"], "status": "all"})
    print("subscriptions of the customer at Stripe:", [f"{s.id} {s.status}" for s in found.data])


def main() -> None:
    args = parse_args()
    if args.command == "events":
        print(",".join(handled_events()))
        return
    c = client()
    try:
        run(c, args)
    except stripe.StripeError as error:
        reason = _KEY_IN_TEXT.sub("<key>", error.user_message or str(error))
        raise SystemExit(f"stripe refused: {type(error).__name__} {error.code}: {reason}") from None


if __name__ == "__main__":
    main()
