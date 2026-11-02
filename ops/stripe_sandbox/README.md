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
   by the Stripe CLI (`stripe sandbox create`; it expires after a week unless claimed, and
   its `rkcs_test_` key cannot use test clocks even after the claim: see below).
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

## Renewals and payment retries with test clocks

A natural renewal, its failure, Stripe's retry schedule and its final action need **test
clocks**. They run only in sandboxes and test mode; nothing here moves money.

### Which credentials reach the clocks

A key from an unclaimed claimable sandbox (`rkcs_test_`) gets `PermissionError` on every
test-clock endpoint, and still did after the sandbox was claimed (2026-11-02); everything
else works with it. The Stripe CLI 1.55 login stores an OAuth token in the Windows
Credential Manager, not an API key, so it cannot be copied into the env file (and should
not be). Two ways therefore work:

- **A full test key** (`sk_test_` or a restricted `rk_test_` with test-clock access) from
  the Dashboard in `STRIPE_SECRET_KEY`: the drivers use it for everything.
- **The logged-in CLI profile for the clocks only:** set
  `ACCESSGUARD_STRIPE_CLI_PROFILE=<profile>` (and `ACCESSGUARD_STRIPE_CLI_CONFIG` if the
  config file is not `.local/stripe/config.toml`). Clock create/retrieve/advance/delete then
  run through `stripe --config <file> -p <profile>`; customers, subscriptions, cards and
  invoices still use the env file's key. Before the first clock call the CLI must be able to
  read the env file's Team price, which exists in one account only, so a profile logged in
  to another account is refused. The CLI's token never leaves the CLI.

`sandbox.py clock-probe` prints which way it used.

### Retry settings

The Dashboard's **Billing > Revenue recovery > Retries** settings cannot be read through the
API. The owner checked them visually in the KRL sandbox on 2026-11-02 (not changed):

| Setting | Value |
| --- | --- |
| Smart Retries | up to 8 retries within 2 weeks |
| First failed renewal | leave the subscription past-due |
| All retries failed | cancel the subscription |
| Invoice after all retries failed | leave the invoice past-due (AccessGuard then voids it, see below) |

`ACCESSGUARD_STRIPE_RETRY_DAYS` (default 14) must match the retry window; the app only shows
it to customers.

### Run

Three terminals as above (the app on `.local/stripe-e2e.sqlite3`); every command prints
Stripe's state (invoice status, attempt count, next attempt) and then the app's, after
waiting up to 30 s for the webhooks to bring the app to Stripe's status.

| Step | Command | Expected (Stripe / app) |
| --- | --- | --- |
| Start | `clock-start <email> team` | clock at real now; subscription `active`, Team; app Team after the webhook |
| Renewal paid | `clock-renew <email>` | invoice `subscription_cycle` paid, new period; app Team, `current_period_end` one month later, no warning |
| Renewal fails | new e-mail: `clock-start`, `card <email> fail`, `clock-renew` | invoice `open`, `attempts=1`, `next_attempt` set, `past_due`; app Team **with the grace warning** |
| Grace in app time | `app-at <email> <seconds>` | the billing page and two project creations at `past_due_since + seconds`, rolled back: Team up to and including 604800 s, Free from 604801 s |
| Each retry | `clock-next-retry <email>` (repeat) | `invoice.payment_failed` with a higher attempt count; app row unchanged |
| Final action | `clock-next-retry` until "no payment attempt is scheduled" | depends on the retry settings; observed: `customer.subscription.deleted`, app **Free** |
| Recovery by retry | new e-mail, fail the renewal and some retries, `card <email> ok`, `clock-next-retry` | the retry is charged, `invoice.paid`, `active`; app Team, warning gone |
| Recovery by payment | new e-mail, fail the renewal, `pay-open <email>` | invoice paid, `active`; app Team, warning gone |
| Clean up | `clock-delete <email>` | the clock with its customer and subscription is deleted (Stripe also deletes clocks after 30 days) |

### Results (2026-11-02, account `acct_1UWAdYB9QLrE2rSI`, claimed sandbox, CLI profile for the clocks)

| Scenario | Owner | Observed |
| --- | --- | --- |
| Renewal paid | `renew-a@example.test` | `subscription_cycle` EUR 49.00 paid at the first attempt; app Team, period end 2026-12-02 → 2027-01-02, no warning |
| Renewal fails, retries exhausted | `retry-b@example.test` | card `pm_card_chargeCustomerFail` (attaches, every charge `card_declined` / `generic_decline`); 9 attempts: the renewal plus **8 retries over 14.02 simulated days**, 36-44 h apart (Smart Retries pattern); then **subscription cancelled**, invoice left `open`. App: Team with warning through every retry (`past_due_since` and `updated_at` unchanged), then Free / `canceled` |
| Grace in app time | `retry-b@example.test` | `past_due_since` 2026-11-02 08:32:32 UTC (app clock). +0 s, +604799 s, +604800 s: Team, warning on the page, 2nd project created; +604801 s and +10 days: Free, "grace period has ended" banner, 2nd project refused |
| Recovery by retry after the app's grace | `recover-c@example.test` | attempts 1-5 failed; attempt 5 came 7.02 simulated days after the first, and `app-at` at that offset gave Free; card fixed, attempt 6 paid: Stripe `active`, app Team, warning and `past_due_since` cleared |
| Recovery by payment | `pay-d@example.test` | 2 failed attempts, open invoice paid with 4242: `active`; app Team, warning gone |
| Payment after the final action | `retry-b@example.test` | before the void rule: the `open` invoice of the cancelled subscription could still be paid (EUR 49.00); the subscription stayed `canceled` and the app Free (`invoice.paid` → `unchanged`). Since then the webhook voids it (next section) |

All 49 webhook deliveries were answered with HTTP 200; every handled event of these
customers was stored. The last `invoice.payment_failed` arrived after
`customer.subscription.deleted` and was stored as `unchanged`.

**Stripe time and app time are separate.** The app starts its grace period from its own
clock when it first sees `past_due` (`webhooks._mirror`), not from the test clock, so
advancing a clock by weeks does not end the app's grace period; Stripe's final action does.
`app-at` checks the grace period through the app's views at a chosen app time, from the
`past_due_since` the webhook stored; it never edits stored timestamps. It runs in-process
(Django's test client on the sandbox database), not against the running server. In
production both clocks are the same: the 7-day grace period then ends between Stripe's
4th and 5th retry, so a customer whose retries fail for a week is on Free until a later
retry or a payment succeeds.

### Void of the failed renewal after the final action

Rule (`webhooks._void_failed_renewal`, ADR 0011): when Stripe's current subscription is
`canceled` with `cancellation_details.reason == "payment_failed"`, its latest invoice is
voided if it is an open, unpaid `subscription_cycle` invoice of the same subscription and
customer. Commands added for this run: `cancel-now <email>` (a requested cancellation
through the API) and `invoices <email>` (every invoice of the customer).

| Step | Command | Expected |
| --- | --- | --- |
| Baseline | `invoices <email>` for earlier owners | unchanged afterwards |
| Failed renewal, A | `clock-start`, `card fail`, `clock-renew` | `past_due`, invoice `open`; banner names the grace end and the end of Stripe's retries |
| Second organisation, B | the same with another e-mail | `past_due`, invoice `open` |
| Final action, A | `clock-next-retry` until no attempt is scheduled | `canceled`, reason `payment_failed`; stored outcome `voided renewal invoice …`; audit `billing.renewal_invoice_voided`; invoice `void` |
| Pay the voided invoice | `invoices.pay` with a working test card | refused by Stripe |
| Redelivery | `stripe events resend <deleted event>` | HTTP 200, no new row, no second void |
| Requested cancellation, B | `cancel-now <email B>` | reason `cancellation_requested`; invoice stays `open` |
| Re-subscription, A | `sandbox_browser.py buy <slug of A> team` | Checkout allowed; same customer, new subscription `active`; app Team |

**Results (2026-11-02, same account, app with migration `0004`):**

| Scenario | Owner | Observed |
| --- | --- | --- |
| Final action and void | `owner-void-e@example.test` | renewal `in_…WyB9…` failed 9 times (renewal + 8 retries); Stripe `canceled`, `cancellation_details.reason = payment_failed`. `customer.subscription.deleted` → `subscription canceled (free); voided renewal invoice in_…`; the `invoice.payment_failed` of the 9th attempt, same second → `unchanged …; invoice … kept: status void`. One audit row. First invoice (`subscription_create`) stays `paid` |
| Pay the voided invoice | `owner-void-e` | Stripe: `InvalidRequestError` "Voided invoices cannot be paid."; invoice still `void`, `amount_paid` 0 |
| Redelivery | `owner-void-e` | `stripe events resend` of the deletion: HTTP 200, no new stored event, still one void and one audit row |
| Billing page after the end | `owner-void-e` | Free; "cancelled because the renewal payment still failed after Stripe's retries … voids the unpaid renewal invoice …; choose a plan below to subscribe again" |
| Re-subscription | `owner-void-e` | hosted Checkout with the 4242 card: same customer `cus_VYRP…`, new subscription `active`, new invoice EUR 49 paid (the voided amount was not added); app Team, `cancellation_reason` cleared |
| Requested cancellation with an open renewal | `owner-req-f@example.test` | `cancel-now` while `past_due`: reason `cancellation_requested`, outcome `subscription canceled (free)` with no invoice step; renewal invoice stays `open` |
| Other customers | `renew-a`, `retry-b`, `recover-c`, `pay-d` | all 8 invoices `paid` before and after |
| Webhooks | | 29 deliveries, all HTTP 200; 28 stored events, exactly one void |

Not run in the sandbox (covered by tests in `test_billing.py` only): an API error during the
void (503 and redelivery), a payment that reaches Stripe just before the void, partly paid
or non-renewal invoices, a delayed cancellation of an earlier subscription, two concurrent
deliveries (PostgreSQL-only test, runs in CI).

Limits from Stripe's documentation (checked 2026-11-02): up to three customers per clock
and three subscriptions per customer; a clock advances at most two billing intervals at a
time; clocks are deleted after 30 days; a renewal invoice stays in draft for about an hour
before it is finalised and charged (`clock-renew` advances past that hour).

## Not covered

- A webhook endpoint on a public server (only `stripe listen` to localhost); see
  docs/deployment.md, section 5.
- The retry settings were read by the owner in the Dashboard, not through the API (it
  cannot read them); the drivers only observe their effect.
- Smart Retries timing with real card networks: test cards decline the same way every time.

## Clean up

Stop the listener and the stack (on Windows, check that no `stripe.exe` is left running).
Delete `.local/stripe-e2e.sqlite3` whenever convenient. An unclaimed sandbox and its test
data expire by themselves; a claimed one keeps them until deleted in the Dashboard.
