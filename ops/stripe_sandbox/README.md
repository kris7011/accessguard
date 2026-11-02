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

## Renewals and payment retries with test clocks (prepared, not run)

A natural renewal, its failure, Stripe's retry schedule and its final action need **test
clocks**. The claimable sandbox used on 2026-10-30 cannot create them: on 2026-11-02
`sandbox.py clock-probe` got `PermissionError` from Stripe, "This is a claimable sandbox key
with limited permissions. Use the claim URL provided when the sandbox was created to get a
full API key". Test clocks run only in sandboxes and test mode; nothing here moves money.

### Owner steps (manual; the assistant does none of them)

1. **Get a test environment with a full key**, before the claimable sandbox expires
   (**2026-11-06**): either claim it with the claim URL in the git-ignored
   `.local/stripe/sandbox-create.out` (claiming moves it into a Stripe account you choose
   or create, and accepts Stripe's terms), or create a sandbox in your own Stripe account.
   Choosing or creating the account is your decision.
2. **Log the separate CLI configuration in to it**, not the default one:
   `stripe --config .local/stripe/config.toml login` (choose the sandbox, not live mode) or
   `stripe --config .local/stripe/config.toml login --api-key <test key>`, entered locally.
3. **Put the test secret key into `.local/stripe/stripe.env`** (`STRIPE_SECRET_KEY`;
   `sk_test_` or `rk_test_`; keep `stripe.env.bak` as it is). In a new sandbox, run
   `sandbox.py setup` again: it writes new price and portal ids.
4. **Choose the retry settings** in that sandbox's Dashboard, **Billing > Revenue recovery
   > Retries**. For a reproducible run use a custom schedule (for example retries 1, 3 and
   5 days after the previous attempt) and **cancel the subscription** after the last one;
   Smart Retries picks its own times (recommended default: 8 tries within 2 weeks), which
   `clock-next-retry` follows as well. Record what was chosen.
5. `sandbox.py clock-probe` must print "test clocks are available".

### Run (three terminals as above; the app on `.local/stripe-e2e.sqlite3`)

| Step | Command | Expected (Stripe / app) |
| --- | --- | --- |
| Start | `clock-start clock1@example.test team` | clock at real now; subscription `active`, Team; app Team after the webhook |
| Renewal paid | `clock-renew clock1@example.test` | invoice `subscription_cycle` paid, new period; app Team, `current_period_end` one month later, no warning |
| Renewal fails | `clock-start clock2@example.test team`, `card clock2@example.test fail`, `clock-renew clock2@example.test` | invoice open, `attempts=1`, `next_attempt` set, subscription `past_due`; app Team **with the grace warning** |
| Each retry | `clock-next-retry clock2@example.test` (repeat) | `invoice.payment_failed` with a higher `attempt_count`; app unchanged |
| Final action | `clock-next-retry` until "no payment attempt is scheduled" | with "cancel": `customer.subscription.deleted`, app **Free**; with "mark unpaid": `unpaid`, app Free; with "leave past due": app keeps the grace rules |
| Recovery | `clock-start clock3@example.test team`, `card … fail`, `clock-renew …`, then `card clock3@example.test ok`, `clock-next-retry …` | the retry is charged to the new card, `invoice.paid`, `active`; app Team, warning gone |
| Clean up | `clock-delete <email>` for each | the clock with its customer and subscription is deleted |

`show <email>` prints both views at any point; `events-since` lists the events. Each
command prints Stripe's state (invoice status, attempt count, next attempt) and the app's.

**Known difference to expect.** The app starts its grace period
(`ACCESSGUARD_PAYMENT_GRACE_DAYS`, 7) from its own clock when it first sees `past_due`
(`webhooks._mirror`), not from the test clock. Advancing the clock by weeks therefore does
not end the app's grace period; only Stripe's final action does. In production both clocks
are the same. The grace expiry itself is covered by `test_billing.py`.

Limits from Stripe's documentation (checked 2026-11-02): up to three customers per clock
and three subscriptions per customer; a clock advances at most two billing intervals at a
time; clocks are deleted after 30 days; a renewal invoice stays in draft for about an hour
before it is finalised and charged (`clock-renew` advances past that hour).

## Not covered

- A webhook endpoint on a public server (only `stripe listen` to localhost); see
  docs/deployment.md, section 5.
- Renewals, retries and the final action until the test-clock run above has been done.

## Clean up

Stop the listener and the stack. Delete `.local/stripe-e2e.sqlite3` whenever convenient. A
claimable sandbox and its test data expire by themselves.
