"""Act as an organisation owner in the running app and in Stripe's hosted pages (headless).

    ACCESSGUARD_STRIPE_SANDBOX=1 python ops/stripe_sandbox/sandbox_browser.py <command> [...]

Commands:

    buy <slug> <plan>             sign up owner-<slug>@example.test and buy Team or Business
                                  through hosted Checkout with Stripe's test card 4242
    billing <email>               the plan heading and banners of the billing page
    switch <email> <plan>         press "Switch to <plan>" in the app; prints where it led
    portal-switch <email> <plan>  switch the plan in the Customer Portal; ``--then-pay``
                                  pays a declined switch with the saved 4242 card
    portal-cancel <email>         cancel in the Customer Portal

Needs the stack (ops/dev/run_stack.py --env-file ...) and ``stripe listen`` running, and
Chrome or Edge for Playwright (``channel``). Screenshots go to .local/stripe/shots/.
See README.md in this folder.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import sandbox
from playwright.sync_api import Page, sync_playwright

SHOTS = sandbox.ROOT / ".local/stripe/shots"
CHECKOUT_HOST = "https://checkout.stripe.com/"
PORTAL_HOST = "https://billing.stripe.com/"


@contextmanager
def browser_page(channel: str) -> Iterator[Page]:
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=channel, headless=True)
        try:
            yield browser.new_page(locale="en-US")
        finally:
            browser.close()


def shot(page: Page, name: str) -> None:
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def log_in(page: Page, email: str) -> None:
    page.goto(sandbox.BASE_URL + "/accounts/login/")
    page.fill("input[name=username]", email)
    page.fill("input[name=password]", sandbox.PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def sign_up(page: Page, email: str, organization: str) -> None:
    page.goto(sandbox.BASE_URL + "/accounts/signup/")
    page.fill("#id_email", email)
    page.fill("#id_password", sandbox.PASSWORD)
    page.fill("#id_organization", organization)
    page.check("#id_accept_terms")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    if "/accounts/signup" in page.url:  # already registered
        log_in(page, email)


def billing_summary(page: Page) -> str:
    page.goto(sandbox.BASE_URL + "/app/billing/")
    lines = [page.locator("h2").first.inner_text()]
    lines += [f"  {b.inner_text()}" for b in page.locator(".banner, .messages li").all()]
    return "\n".join(lines)


def buy(page: Page, slug: str, plan: str) -> None:
    email = f"owner-{slug}@example.test"
    sign_up(page, email, f"Sandbox {slug}")
    page.goto(sandbox.BASE_URL + "/app/billing/")
    page.click(f"text=Choose {plan.title()}")
    page.wait_for_url(CHECKOUT_HOST + "**", timeout=30_000)
    page.wait_for_load_state("networkidle")
    page.locator("[data-testid='card-accordion-item-button']").dispatch_event("click")
    page.wait_for_selector("#cardNumber", timeout=15_000)
    page.fill("#cardNumber", "4242 4242 4242 4242")
    page.fill("#cardExpiry", "12 / 34")
    page.fill("#cardCvc", "123")
    page.fill("#billingName", f"Owner {slug}")
    if page.locator("#billingCountry").count():
        page.locator("#billingCountry").select_option("DK")
    postal = page.locator("#billingPostalCode")
    if postal.count() and postal.is_visible():
        postal.fill("2100")
    link = page.locator("#enableStripePass")
    if link.count() and link.is_checked():
        link.uncheck()
    page.click("[data-testid='hosted-payment-submit-button']")
    page.wait_for_url(sandbox.BASE_URL + "/app/billing/**", timeout=90_000)
    page.wait_for_timeout(4000)  # webhooks
    print(email)
    print(billing_summary(page))


def switch(page: Page, email: str, plan: str) -> None:
    log_in(page, email)
    page.goto(sandbox.BASE_URL + "/app/billing/")
    with page.expect_navigation(timeout=60_000, wait_until="domcontentloaded"):
        page.click(f"text=Switch to {plan.title()}")
    page.wait_for_timeout(3000)  # Stripe's pages keep connections open: no networkidle
    shot(page, f"switch-{plan}")
    # A declined payment leads to Stripe's hosted invoice page (invoice.stripe.com).
    print("after switch:", page.url.split("?")[0].rsplit("/", 1)[0])
    if page.url.startswith(sandbox.BASE_URL):
        print("  ", " | ".join(m.inner_text() for m in page.locator(".messages li").all()))
    page.wait_for_timeout(4000)
    print(billing_summary(page))


def open_portal(page: Page, email: str) -> None:
    log_in(page, email)
    page.goto(sandbox.BASE_URL + "/app/billing/")
    page.click("text=Manage payment method, invoices or cancel")
    page.wait_for_url(PORTAL_HOST + "**", timeout=30_000)
    page.wait_for_load_state("networkidle")


def portal_switch(page: Page, email: str, plan: str, *, then_pay: bool = False) -> None:
    open_portal(page, email)
    page.get_by_role("link", name="Update subscription").or_(
        page.get_by_role("button", name="Update subscription")
    ).first.click()
    page.wait_for_load_state("networkidle")
    shot(page, "portal-switch-options")
    page.locator(f"[data-testid='pricing-table-card']:has-text('{plan.title()}')").get_by_role(
        "button"
    ).first.click()
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="Continue").or_(
        page.get_by_role("button", name="Confirm")
    ).first.click()
    page.wait_for_timeout(2000)
    shot(page, "portal-switch-review")
    confirm = page.get_by_role("button", name="Subscribe and pay").or_(
        page.get_by_role("button", name="Confirm")
    )
    if confirm.count():
        confirm.first.click()
    page.wait_for_timeout(6000)
    shot(page, "portal-switch-result")
    print("portal says:", " | ".join(page.locator("body").inner_text().splitlines()[:25]))
    if then_pay and page.get_by_text("was declined").count():
        # The portal offers the customer's other cards; 4242 is Stripe's working test card.
        page.get_by_text("4242").first.click()
        page.get_by_role("button", name="Subscribe and pay").first.click()
        page.wait_for_timeout(8000)
        shot(page, "portal-switch-paid")
        print("after paying:", " | ".join(page.locator("body").inner_text().splitlines()[:25]))


def portal_cancel(page: Page, email: str) -> None:
    open_portal(page, email)
    page.click("text=Cancel subscription")
    page.wait_for_load_state("networkidle")
    page.locator("[data-testid='confirm']").or_(
        page.get_by_role("button", name="Cancel subscription")
    ).first.click()
    page.wait_for_timeout(5000)
    shot(page, "portal-cancel-result")
    print("portal says:", " | ".join(page.locator("body").inner_text().splitlines()[:15]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--channel", default="chrome", help="chrome or msedge")
    sub = parser.add_subparsers(dest="command", required=True)
    buy_cmd = sub.add_parser("buy")
    buy_cmd.add_argument("slug")
    buy_cmd.add_argument("plan", choices=["team", "business"])
    sub.add_parser("billing").add_argument("email")
    for name in ("switch", "portal-switch"):
        cmd = sub.add_parser(name)
        cmd.add_argument("email")
        cmd.add_argument("plan", choices=["team", "business"])
        if name == "portal-switch":
            cmd.add_argument(
                "--then-pay",
                action="store_true",
                help="after a decline, pay with the saved 4242 card in the portal",
            )
    sub.add_parser("portal-cancel").add_argument("email")
    args = parser.parse_args()
    sandbox.require_opt_in()

    with browser_page(args.channel) as page:
        try:
            if args.command == "buy":
                buy(page, args.slug, args.plan)
            elif args.command == "billing":
                log_in(page, args.email)
                print(billing_summary(page))
            elif args.command == "switch":
                switch(page, args.email, args.plan)
            elif args.command == "portal-switch":
                portal_switch(page, args.email, args.plan, then_pay=args.then_pay)
            elif args.command == "portal-cancel":
                portal_cancel(page, args.email)
        except Exception as error:
            shot(page, f"failure-{args.command}")
            print(f"FAILED at {page.url.split('#')[0][:80]}: {type(error).__name__}: {error}")
            sys.exit(1)


if __name__ == "__main__":
    main()
