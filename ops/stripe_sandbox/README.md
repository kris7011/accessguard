# Stripe sandbox drivers (opt-in, test mode only)

Reusable drivers for checking AccessGuard's billing against a real Stripe test environment:
hosted Checkout, the Customer Portal, plan changes, failed payments and webhooks. They are
**not** part of `ops/verify.sh` or CI. Nothing here calls Stripe unless you opt in.

| File | What it does |
| --- | --- |
| `sandbox.py` | Stripe SDK side: sandbox setup, subscription state, failing or working cards, paying an open invoice, listing an organisation's events |
| `sandbox_browser.py` | Browser side (Playwright, headless Chrome or Edge): sign up and buy, switch plan in the app, switch or cancel in the Customer Portal |

## Safety rules

- **Opt-in:** every command exits unless `ACCESSGUARD_STRIPE_SANDBOX=1` is set.
- **Test mode only:** the key must be a test key (`sk_test_`, `rk_test_`, `rkcs_test_`;
  checked with the app's own `is_test_key`), and every Stripe object created must report
  `livemode: false`. Payments use Stripe's documented test cards (`4242…`,
  `pm_card_visa`, `pm_card_chargeCustomerFail`); no money moves.
- **No credentials in the repository:** the key and the webhook secret live only in a
  git-ignored env file (default `.local/stripe/stripe.env`, or
  `ACCESSGUARD_STRIPE_ENV_FILE`). The drivers never print them.
- **Separate test data:** the app runs on its own database,
  `.local/stripe-e2e.sqlite3`; the drivers refuse any other `DATABASE_URL`, so the
  development database `.local/dev.sqlite3` is never touched. Test users are
  `owner-<slug>@example.test`.

## One-time setup

1. A Stripe test environment. Either a test-mode account, or a claimable sandbox created
   by the Stripe CLI (`stripe sandbox create`; it expires after a week unless claimed and
   cannot use test clocks).
2. The Stripe CLI, logged in to that environment. Use a dedicated config file:
   `stripe --config .local/stripe/config.toml -p <profile> ...`.
3. The env file, with **names** as below and your own values (never commit it):

   ```text
   STRIPE_SECRET_KEY=<test key>
   STRIPE_WEBHOOK_SECRET=<from: stripe listen --print-secret>
   STRIPE_PRICE_TEAM=<written by setup>
   STRIPE_PRICE_BUSINESS=<written by setup>
   STRIPE_PORTAL_CONFIGURATION=<written by setup>
   ```

4. Products, prices and the portal configuration (idempotent; reuses prices by lookup key):

   ```bash
   export ACCESSGUARD_STRIPE_SANDBOX=1
   python ops/stripe_sandbox/sandbox.py setup        # portal plan switch: always_invoice
   ```

   `always_invoice` is required. With `create_prorations`, the portal switches the plan at
   once and bills the difference only at renewal, so a higher plan would be active before
   it is paid (observed 2026-10-30, see HANDOFF.md).

## Each run

Three terminals, from the repository root:

```bash
# 1. the app on the sandbox database
DATABASE_URL=sqlite:///$PWD/.local/stripe-e2e.sqlite3 \
  python ops/dev/run_stack.py --env-file .local/stripe/stripe.env

# 2. webhooks to the app (the event list comes from the webhook module)
stripe --config .local/stripe/config.toml -p <profile> listen \
  --events "$(ACCESSGUARD_STRIPE_SANDBOX=1 python ops/stripe_sandbox/sandbox.py events)" \
  --forward-to http://127.0.0.1:8000/webhooks/stripe/

# 3. the drivers
export ACCESSGUARD_STRIPE_SANDBOX=1
```

On Windows PowerShell, set `$env:DATABASE_URL` and `$env:ACCESSGUARD_STRIPE_SANDBOX`
instead. `run_stack.py` runs Django with `--noreload`: restart it after changing settings.

## Scenarios and expected results

Each line was run against the sandbox on 2026-10-30. Check with
`python ops/stripe_sandbox/sandbox.py show <email>`, which prints Stripe's view, the app's
view and the customer's subscriptions at Stripe.

| Scenario | Commands | Expected |
| --- | --- | --- |
| Purchase | `sandbox_browser.py buy s1 team` | Team, `active`; one subscription |
| Declined upgrade in the app, then paid | `sandbox.py card <email> fail`; `sandbox_browser.py switch <email> business`; `sandbox.py pay-open <email>` | after the switch: browser on Stripe's hosted invoice page, Stripe `active` with a `pending_update`, app still Team; after paying: Business; still one subscription |
| Declined upgrade in the portal, then paid there | `sandbox.py card <email> fail`; `sandbox_browser.py portal-switch <email> business --then-pay` | portal says the card was declined; app Team until the portal payment with the saved 4242 card, then Business |
| Price applied before payment (`allow_incomplete`) | `sandbox.py fail-upgrade <email>`; later `pay-open` | Stripe `past_due` with the Business price; app keeps **Team** with the grace warning; after paying: Business |
| Late first delivery | stop `stripe listen`; `fail-upgrade`, `pay-open`; start the listener; `sandbox.py events-since <email> <epoch>` and `stripe events resend <id>` for each, newest first | the first event applied gives Business `active`; every later one, including the old `invoice.payment_failed` and `past_due` snapshot, is stored as `unchanged` |
| Repeated delivery | `stripe events resend <id>` of an event already processed | answered `duplicate`; nothing changes |
| Cancellation | `sandbox_browser.py portal-cancel <email>`; `sandbox.py end-in <email> 60` | warning with the end date; Free after `customer.subscription.deleted` |
| Re-subscription | `sandbox_browser.py buy <same slug> team` after the end | Checkout allowed (no live subscription at Stripe); same customer, new subscription; the app follows the new one |

## Not covered (needs a Stripe account with test clocks)

- A natural renewal at the end of a billing period, and its failure. `fail-renewal`
  bills a new period by resetting the billing cycle anchor; with flexible billing mode a
  credit for the unused period can make that invoice EUR 0, so it is not a substitute.
- Stripe's own retry schedule (Smart Retries) and its final action (cancel, `unpaid`).
- A webhook endpoint on a public server (only `stripe listen` to localhost).

## Clean up

Stop the listener and the stack. Delete `.local/stripe-e2e.sqlite3` whenever convenient. A
claimable sandbox and its test data expire by themselves.
