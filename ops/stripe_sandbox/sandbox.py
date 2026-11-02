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
    cancel-now <email>            cancel at once through the API (a requested cancellation,
                                  cancellation_details.reason cancellation_requested)
    invoices <email>              every invoice of the customer: status, billing reason,
                                  subscription, amounts and attempts
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

    With ``ACCESSGUARD_STRIPE_CLI_PROFILE`` set, the test-clock endpoints go through that
    logged-in Stripe CLI profile (``ACCESSGUARD_STRIPE_CLI_CONFIG``, default
    ``.local/stripe/config.toml``) instead of the env file's key: for a key that may not use
    test clocks, such as a claimable sandbox key. Everything else still uses the key.

Application time (the app's grace period runs on its own clock, not Stripe's):

    app-at <email> <seconds>      the billing page and a project creation through the app's
                                  views at past_due_since + <seconds>, in a transaction that
                                  is rolled back

Safety: nothing runs without ``ACCESSGUARD_STRIPE_SANDBOX=1``; only test-mode keys are
accepted (``is_test_key``); every Stripe object touched must say ``livemode: false``; the
app database must be the separate sandbox database, never ``.local/dev.sqlite3``. The key
is read from a git-ignored env file and never printed. See README.md in this folder.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from unittest import mock

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
# Webhooks reach the app a few seconds after Stripe's state changes.
WEBHOOK_WAIT_SECONDS = 30
CLI_PROFILE = os.environ.get("ACCESSGUARD_STRIPE_CLI_PROFILE", "")
CLI_CONFIG = Path(
    os.environ.get("ACCESSGUARD_STRIPE_CLI_CONFIG", ROOT / ".local/stripe/config.toml")
)
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
        livemode = obj.get("livemode") if isinstance(obj, dict) else getattr(obj, "livemode", None)
        if livemode is not False:
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
        "cancel_at": row.cancel_at, "current_period_end": row.current_period_end,
        "past_due_since": row.past_due_since, "cancellation_reason": row.cancellation_reason,
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
        "cancellation_reason": s.cancellation_details.reason if s.cancellation_details else None,
    }  # fmt: skip


def await_app(email: str, status: str) -> dict[str, Any]:
    """The app's view once it has caught up with Stripe's status, or after the wait."""
    deadline = time.monotonic() + WEBHOOK_WAIT_SECONDS
    while (state := app_state(email))["status"] != status and time.monotonic() < deadline:
        time.sleep(2)
    return state


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


class ClockApi(Protocol):
    """Stripe's test-clock endpoints; a clock is returned as its JSON fields."""

    via: str

    def create(self, frozen_time: int, name: str) -> dict[str, Any]: ...
    def retrieve(self, clock_id: str) -> dict[str, Any]: ...
    def advance(self, clock_id: str, frozen_time: int) -> None: ...
    def delete(self, clock_id: str) -> None: ...


class SdkClocks:
    """Test clocks with the env file's key."""

    via = "the env file's key"

    def __init__(self, c: stripe.StripeClient) -> None:
        self._clocks = c.v1.test_helpers.test_clocks

    def create(self, frozen_time: int, name: str) -> dict[str, Any]:
        params = {"frozen_time": frozen_time, "name": name}
        return self._clocks.create(params=params).to_dict()  # type: ignore[arg-type]

    def retrieve(self, clock_id: str) -> dict[str, Any]:
        return self._clocks.retrieve(clock_id).to_dict()

    def advance(self, clock_id: str, frozen_time: int) -> None:
        self._clocks.advance(clock_id, params={"frozen_time": frozen_time})

    def delete(self, clock_id: str) -> None:
        self._clocks.delete(clock_id)


def parse_cli_output(stdout: str, stderr: str) -> dict[str, Any]:
    """The JSON object the Stripe CLI printed after its banner, or exit with its error."""
    start = stdout.find("{")
    if start < 0:
        reason = _KEY_IN_TEXT.sub("<key>", (stderr or stdout).strip()[-300:])
        raise SystemExit(f"stripe CLI gave no JSON: {reason}")
    found: dict[str, Any] = json.JSONDecoder().raw_decode(stdout[start:])[0]
    if "error" in found:
        error = found["error"]
        reason = _KEY_IN_TEXT.sub("<key>", str(error.get("message")))
        raise SystemExit(f"stripe CLI refused: {error.get('type')} {error.get('code')}: {reason}")
    return found


class CliClocks:
    """Test clocks through a logged-in Stripe CLI profile; its credentials stay in the CLI."""

    def __init__(self, profile: str, config: Path, price_id: str) -> None:
        executable = shutil.which("stripe")
        if executable is None:
            raise SystemExit("Refusing: the Stripe CLI is not on PATH")
        self._base = [executable, "--config", str(config), "-p", profile]
        self.via = f"Stripe CLI profile {profile}"
        # A price id exists in one account only: the profile must be the key's account.
        assert_test_mode(self._call("get", f"/v1/prices/{price_id}"))

    def _call(self, *args: str) -> dict[str, Any]:
        done = subprocess.run(  # noqa: S603 - fixed executable, arguments built here
            [*self._base, *args], capture_output=True, text=True, encoding="utf-8", check=False
        )
        return parse_cli_output(done.stdout, done.stderr)

    def create(self, frozen_time: int, name: str) -> dict[str, Any]:
        path = "/v1/test_helpers/test_clocks"
        return self._call("post", path, "-d", f"frozen_time={frozen_time}", "-d", f"name={name}")

    def retrieve(self, clock_id: str) -> dict[str, Any]:
        return self._call("get", f"/v1/test_helpers/test_clocks/{clock_id}")

    def advance(self, clock_id: str, frozen_time: int) -> None:
        path = f"/v1/test_helpers/test_clocks/{clock_id}/advance"
        self._call("post", path, "-d", f"frozen_time={frozen_time}")

    def delete(self, clock_id: str) -> None:
        self._call("delete", f"/v1/test_helpers/test_clocks/{clock_id}", "--confirm")


def clock_api(c: stripe.StripeClient) -> ClockApi:
    if CLI_PROFILE:
        return CliClocks(CLI_PROFILE, CLI_CONFIG, read_env()["STRIPE_PRICE_TEAM"])
    return SdkClocks(c)


def clock_probe(c: stripe.StripeClient) -> str:
    clocks = clock_api(c)
    clock = clocks.create(int(time.time()), "accessguard probe")
    assert_test_mode(clock)
    clocks.delete(clock["id"])
    return f"test clocks are available via {clocks.via} (created and deleted {clock['id']})"


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
    clock = clock_api(c).create(int(time.time()), f"accessguard {email}")
    assert_test_mode(clock)
    customer = c.v1.customers.create(
        params={"email": email, "test_clock": clock["id"], "metadata": metadata}
    )
    pm = c.v1.payment_methods.attach(CARD_OK, params={"customer": customer.id})
    c.v1.customers.update(
        customer.id, params={"invoice_settings": {"default_payment_method": pm.id}}
    )
    s = c.v1.subscriptions.create(params={
        "customer": customer.id, "items": [{"price": read_env()[PLANS[plan][2]]}],
        "default_payment_method": pm.id, "metadata": metadata,
    })  # fmt: skip
    assert_test_mode(customer, pm, s)
    deadline = time.monotonic() + 60
    while app_state(email)["subscription"] != s.id:
        if time.monotonic() > deadline:
            return f"{clock['id']} {s.id} {s.status}: not in the app yet; is stripe listen running?"
        time.sleep(2)
    return f"clock {clock['id']}, subscription {s.id} {s.status}"


def subscription_clock(c: stripe.StripeClient, email: str) -> tuple[stripe.Subscription, str]:
    state = app_state(email)
    s = c.v1.subscriptions.retrieve(state["subscription"], params={"expand": ["latest_invoice"]})
    customer = c.v1.customers.retrieve(state["customer"])
    assert_test_mode(s, customer)
    if not customer.test_clock:
        raise SystemExit("Refusing: this customer is not on a test clock (see clock-start)")
    return s, str(customer.test_clock)


def advance_clock(c: stripe.StripeClient, clock_id: str, frozen_time: int) -> str:
    clocks = clock_api(c)
    clocks.advance(clock_id, frozen_time)
    deadline = time.monotonic() + CLOCK_TIMEOUT_SECONDS
    while (clock := clocks.retrieve(clock_id))["status"] != "ready":
        status = clock["status"]
        if status == "internal_failure":
            raise SystemExit(f"Stripe could not advance {clock_id}: {clock.get('status_details')}")
        if time.monotonic() > deadline:
            raise SystemExit(f"{clock_id} is still {status} after {CLOCK_TIMEOUT_SECONDS} s")
        time.sleep(2)
    return f"clock {clock_id} at {clock['frozen_time']}"


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
    now = int(clock_api(c).retrieve(clock_id)["frozen_time"])
    return advance_clock(c, clock_id, now + hours * 3600)


def end_in(c: stripe.StripeClient, email: str, seconds: int) -> str:
    at = int(time.time()) + seconds
    c.v1.subscriptions.update(
        app_state(email)["subscription"], params={"cancel_at": at, "proration_behavior": "none"}
    )
    return f"cancel_at {at}"


def cancel_now(c: stripe.StripeClient, email: str) -> str:
    """Cancel at once through the API, as a requested cancellation (not Stripe's dunning)."""
    s = c.v1.subscriptions.cancel(app_state(email)["subscription"])
    assert_test_mode(s)
    return f"subscription {s.id} {s.status}"


def invoices(c: stripe.StripeClient, email: str) -> list[str]:
    """Every invoice of the organisation's customer: whose it is, why, and its state."""
    found = c.v1.invoices.list(params={"customer": app_state(email)["customer"], "limit": 100})
    lines = []
    for invoice in found.data:
        assert_test_mode(invoice)
        details = invoice.parent.subscription_details if invoice.parent else None
        lines.append(
            f"{invoice.id} {invoice.status} {invoice.billing_reason} "
            f"subscription={details.subscription if details else None} due={invoice.amount_due} "
            f"paid={invoice.amount_paid} attempts={invoice.attempt_count}"
        )
    return lines


def app_at(email: str, seconds: int) -> str:
    """The app at a controlled time in its grace period, through its own views.

    Stripe's test clock does not move the app's clock: the grace period starts when the
    app first sees ``past_due`` (``past_due_since``, its own time). This sets the app's time
    to ``past_due_since + seconds`` for the billing page and two project creations, and
    rolls everything back, so the stored state is never edited.
    """
    from django.db import transaction  # noqa: PLC0415
    from django.test import Client  # noqa: PLC0415
    from django.utils.html import escape  # noqa: PLC0415

    from accessguard_web.accounts.models import User  # noqa: PLC0415
    from accessguard_web.billing.entitlements import entitlements_for  # noqa: PLC0415
    from accessguard_web.projects.models import Project  # noqa: PLC0415

    since = app_state(email)["past_due_since"]
    if since is None:
        raise SystemExit("Refusing: no grace period is running (past_due_since is empty)")
    at = since + timedelta(seconds=seconds)
    org = organization(email)
    projects = Project.objects.filter(organization=org, is_demo=False)
    with mock.patch("django.utils.timezone.now", return_value=at), transaction.atomic():
        ent = entitlements_for(org)
        browser = Client(HTTP_HOST="127.0.0.1")
        browser.force_login(User.objects.get(email=email))
        page = browser.get("/app/billing/").content.decode()
        before = projects.count()
        answers = [
            browser.post("/app/projects/new/", {"name": f"grace probe {n}"}).get("Location", "")
            for n in (1, 2)
        ]
        after = projects.count()
        transaction.set_rollback(True)
    shown = ent.warning is not None and escape(ent.warning) in page
    return (
        f"app time {at.isoformat()} = past_due_since + {seconds} s: plan {ent.plan.name}; "
        f"warning shown on the billing page: {shown}: "
        f"{ent.warning}\nprojects {before} -> {after} after two creations {answers} "
        "(rolled back)"
    )


def clock_delete(c: stripe.StripeClient, email: str) -> str:
    _, clock_id = subscription_clock(c, email)
    clock_api(c).delete(clock_id)
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
    "cancel-now": lambda c, a: cancel_now(c, a.email),
    "clock-start": lambda c, a: clock_start(c, a.email, a.plan),
    "clock-renew": lambda c, a: clock_renew(c, a.email),
    "clock-next-retry": lambda c, a: clock_next_retry(c, a.email),
    "clock-advance": lambda c, a: clock_advance(c, a.email, a.hours),
    "app-at": lambda c, a: app_at(a.email, a.seconds),
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
                 "clock-next-retry", "clock-delete", "cancel-now", "invoices"):  # fmt: skip
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
    at = sub.add_parser("app-at")
    at.add_argument("email")
    at.add_argument("seconds", type=int)
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
    if args.command == "invoices":
        print("\n".join(invoices(c, args.email)))
        return
    result = ACTIONS[args.command](c, args)
    if result:
        print(result)
    remote = stripe_state(c, app_state(args.email)["subscription"])
    state = await_app(args.email, remote["status"])
    print("stripe:", remote)
    print("app:   ", state)
    if state["status"] != remote["status"]:
        print(f"app status differs after {WEBHOOK_WAIT_SECONDS} s: is stripe listen running?")
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
