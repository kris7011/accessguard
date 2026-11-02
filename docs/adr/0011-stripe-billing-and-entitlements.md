# ADR 0011: Stripe Checkout and Customer Portal, webhook-driven entitlements

- Status: accepted (V1; test mode only until live payments are approved)
- Date: 2026-10-29

## Problem

Customers must be able to buy, change and cancel a subscription without contacting us.
Card data must never touch AccessGuard, and access rights must follow the payment state
reliably even when webhooks are retried, duplicated or delivered out of order.

## Options

1. Own card forms with a payment API.
2. **Hosted Stripe Checkout for purchase, the Stripe Customer Portal for changes,
   cancellation and invoices, webhooks as the source of truth.**
3. A merchant of record (Paddle, Lemon Squeezy) that also handles VAT.

## Decision

Option 2, behind a small port (`billing/gateway.py`: `checkout_url`, `portal_url`,
`verified_event`), using the official `stripe` SDK.

- Plans and limits live in one place (`billing/plans.py`); they are hypotheses.
- A plan is granted only by a verified webhook, never by the success redirect.
- Webhooks (`billing/webhooks.py`): signature verified by the SDK; each event id stored with
  a unique constraint in the same transaction as its effect (idempotent); subscription events
  older than the newest applied one are ignored (ordering); unknown types acknowledged only.
- Entitlements (`billing/entitlements.py`): `active`/`trialing` give the plan; `past_due`
  keeps it for a grace period with a warning, then Free; cancellation at period end shows a
  warning until the period ends; everything else is Free. Data is never deleted by billing.
- Limits are enforced on the server when a scan is approved or a project, runner, member or
  assistant request is created. Demo scans never count.

## Consequences

- Little payment code to maintain; PCI scope stays with Stripe.
- VAT/OSS handling is not solved by this decision: Stripe Tax or a merchant of record is
  needed before selling to consumers or across EU borders at scale (launch checklist).
- Verified locally with SDK-generated signatures and mocked Checkout calls only; a Stripe test
  account is needed to verify the real flow.

## Update 2026-10-30: verified against a Stripe sandbox

The flow ran against a claimable Stripe sandbox (`stripe sandbox create`, restricted
`rkcs_test_` key) with real Checkout, Customer Portal and webhooks forwarded by
`stripe listen` (API version `2026-09-30.endive`). Results: docs/HANDOFF.md. Three changes:

- **Cancellation in flexible billing mode.** New subscriptions use `billing_mode: flexible`.
  The Customer Portal then schedules a cancellation with `cancel_at` and leaves
  `cancel_at_period_end` false, so the app showed no cancellation warning. The subscription
  now stores `cancel_at`, and the warning uses it, or the period end in classic mode.
  Access still ends only on `customer.subscription.deleted`.
- **Explicit portal configuration.** `STRIPE_PORTAL_CONFIGURATION` (`bpc_...`) is passed to
  every portal session, so the portal's features (cancel at period end, plan switch between
  the two prices, payment methods, invoices) are fixed by a configuration created through the
  API instead of the account's Dashboard default.
- **Live keys are refused.** A key counts as a test key only when its mode segment says
  `test` (`sk_test_`, `rk_test_`, `rkcs_test_`). Any other key is refused unless
  `ACCESSGUARD_ALLOW_LIVE_PAYMENTS=true`, which needs the owner's explicit approval. Before
  this change, a live key only changed a banner.

## Update 2026-10-30 (later): current state instead of timestamps, pending upgrades

Regression tests written first (`WebhookOrderingTests`, `PlanChangeTests`) failed 13 times
against the previous design, and the sandbox showed two more gaps. Problems found:

- `invoice.payment_failed` and `invoice.paid` changed the state with no ordering guard, so
  a late failure after a successful retry restarted the grace period.
- Lookups fell back from the subscription id to the customer, so an event of an earlier
  (cancelled) subscription changed the organisation's current one, including a cancellation
  that downgraded an active re-subscription to Free.
- Comparing `created` cannot order events of the same second, which Stripe produces
  routinely (observed: three events with one `created` value).
- "Choose Business" while on Team opened a new Checkout, which creates a second subscription.
- An upgrade applied before payment (`allow_incomplete`, or a portal configured with
  `create_prorations`) granted the higher plan while unpaid.

**Decision.**

1. *Events are signals; Stripe's current state is applied.* For each event the webhook
   finds the organisation (subscription id, customer id or our metadata), locks its row
   (`SELECT ... FOR UPDATE`), fetches the named subscription from Stripe inside the lock and
   mirrors it. This follows Stripe's guidance not to rely on order or `created`
   (<https://docs.stripe.com/webhooks#event-ordering>). `last_event_created` was removed
   (migration `0003`).
2. *One followed subscription.* An event of another subscription is applied only when the
   followed one has ended at Stripe; customer and metadata must match the organisation.
3. *Paid rights only.* Only `active`/`trialing` can raise the plan; any other state keeps
   at most the plan already held. So a grace period never grants an unpaid upgrade.
4. *Plan changes update the subscription.* With a live subscription, "Switch to …" calls
   `subscriptions.update` with `proration_behavior=always_invoice` and
   `payment_behavior=pending_if_incomplete`
   (<https://docs.stripe.com/billing/subscriptions/pending-updates>); a declined payment
   redirects to Stripe's hosted invoice (CSP `form-action` allows `invoice.stripe.com`).
   Checkout is refused while Stripe lists a live subscription for the customer.
5. *Portal configuration.* Plan switching in the portal must use `always_invoice`. Observed
   in the sandbox: the portal then charges at once and, on a decline, leaves a pending update
   (old price, open invoice) exactly like the app's own path. With `create_prorations` it
   switches at once and bills only at renewal; that cannot be told apart from a paid upgrade.

**Alternatives considered.**

- *Per-subscription timestamp plus event-type rules* (e.g. ignore `payment_failed` after a
  later `paid`): fewer API calls, but it still cannot order same-second events and grows a
  rule per event type. Rejected.
- *Fetch first, then lock:* shorter lock, but two deliveries can fetch in one order and write
  in the other. The lock is held across one API call (seconds at most) on one organisation's
  row; acceptable at this scale.
- *Asynchronous queue with a periodic reconciliation job:* the robust choice at high volume
  (Stripe recommends asynchronous handling); more moving parts than V1 needs. Revisit with
  volume.
- *Disable plan switching in the portal:* simpler, but the observed portal behaviour with
  `always_invoice` is safe, and customers expect to switch there.

**Consequences.** The webhook depends on Stripe's API being reachable: when it is not, the
event is not stored and the endpoint answers 503, so Stripe retries (up to three days in live
mode). One extra API call per event. Natural renewals, their failure, Stripe's retries and
its final action were verified with test clocks on 2026-11-02 (ops/stripe_sandbox/README.md):
every retry left the row unchanged, the cancellation after the last retry gave Free, and a
late `invoice.payment_failed` after the cancellation changed nothing. The grace period runs
on the app's clock from the first `past_due` it sees; Stripe's retries ran for 14 days, the
grace period lasts 7, so a customer can be on Free while Stripe still retries, and returns
to the paid plan when a retry or a payment succeeds. The open invoice left after the final
action is handled by the update below.

## Update 2026-11-02 (later): void the failed renewal invoice after Stripe's final action

**Problem.** The test-clock run showed that after the last failed retry Stripe cancels the
subscription but leaves its renewal invoice `open`, and that invoice could still be paid
(EUR 49 for a plan that had ended). The owner checked the KRL sandbox Dashboard
(Billing > Revenue recovery): Smart Retries, up to 8 retries within 2 weeks; first failed
renewal → leave the subscription past-due; all retries failed → cancel the subscription;
invoice → leave it past-due. The settings were not changed.

**Options.**

1. *Dashboard only:* set the invoice action after the last retry to "mark uncollectible".
   No code, but an uncollectible invoice can still be paid, and the rule lives in a setting
   the API cannot read or test.
2. *Void in the webhook, scoped to Stripe's own cancellation.* Code and tests in this
   repository; Stripe's Dashboard stays as checked.
3. *Void every open invoice of a cancelled subscription,* or *a periodic reconciliation job.*
   Broader than the requirement (would also hit a customer-requested cancellation), or a new
   moving part.

**Decision: option 2.** `webhooks._void_failed_renewal`, called for every handled event after
the tenant check (`_belongs_to`) and inside the same row lock and transaction:

- applies only when Stripe's **current** subscription is `canceled` with
  `cancellation_details.reason == "payment_failed"`. Stripe sets that reason itself; a
  cancellation through the API, Dashboard or portal says `cancellation_requested` (observed:
  `cancel-now` in the sandbox), and `payment_disputed` is not part of this rule;
- voids only that subscription's `latest_invoice`, and only while it is `open`,
  `billing_reason=subscription_cycle`, of the same subscription and customer, with
  `amount_paid == 0`. Paid, partly paid, uncollectible, draft, first and upgrade invoices
  and other subscriptions' invoices are kept, with the reason in the stored outcome;
- runs *before* the "followed subscription" check, so a delayed cancellation of an earlier
  subscription still voids that subscription's own invoice without touching the current one;
- is idempotent: every later event of the subscription finds the invoice `void` and stops;
- concurrency with a payment is resolved by Stripe, which applies either the payment or the
  void. `StripeGateway.void_invoice` turns a refusal (`InvalidRequestError`) into "return the
  invoice as it is now"; the webhook then re-checks it (see the review fix below), so a paid
  or partly paid invoice is kept and a paid one logs a warning (possible refund);
- a transient API error raises `ProviderUnavailable`: the transaction rolls back, nothing is
  stored, the endpoint answers 503 and Stripe redelivers;
- each void writes an audit event `billing.renewal_invoice_voided`, and the row stores
  `cancellation_reason` (migration `0004`) so the billing page can explain the end.

The port gained `invoice` and `void_invoice`; `process_event` takes a `StripeApi` protocol
(subscription, invoice, void) instead of a single fetch function.

**Grace period vs. retries.** The app keeps its 7-day grace period for now. The new setting
`ACCESSGUARD_STRIPE_RETRY_DAYS` (default 14, must match the Dashboard, which the API cannot
read) is shown to customers only: the banner names both dates, and the billing page shows a
timeline (failure → end of grace → end of Stripe's retries → cancellation and void).

**Alternatives rejected.** Option 1 leaves the invoice payable and untestable. Option 3
breaks the requirement that requested cancellations are untouched. Voiding on
`invoice.payment_failed` with `next_payment_attempt == null` would act on the payload and on
one event type; the current-state rule works for any late or repeated event.

**Consequences.** Verified in the sandbox on 2026-11-02 (ops/stripe_sandbox/README.md):
renewal invoice voided after the 9th attempt, payment of it refused by Stripe, re-subscription
through Checkout with the same customer, a requested cancellation and other customers'
invoices untouched. Limits: if Stripe gives up redelivering (three days in live mode) the
invoice stays open until another event of that subscription arrives; there is no
reconciliation job. Voiding is irreversible; local law on invoice cancellation is an
accounting question for the owner (Stripe's API documentation says the same).

**Revisit when** a reconciliation job is introduced, the Dashboard's invoice action changes,
or the grace period is aligned with the retry window.

### Review fix (2026-11-02): an unfinished void is retried, and the page claims only a confirmed void

A review of `ee1af63` found two defects:

1. **A refused void counted as handled.** `StripeGateway.void_invoice` catches every
   `InvalidRequestError` and returns the re-fetched invoice. If that invoice was still
   `open` and unpaid, `_void_failed_renewal` stored the outcome `kept: void refused, status
   open` and the endpoint answered 200, so Stripe never redelivered and the invoice stayed
   payable. Now the webhook runs `_keep_invoice_because` again on the invoice the refusal
   returned. A *confirmed change* (`paid`, partly paid, `void`, ...) is kept as before,
   with its reason. If the invoice *still qualifies*, the void did not happen: the webhook
   raises `VoidNotApplied` (a `ProviderUnavailable`), the transaction rolls back, nothing is
   stored, the endpoint answers 503 and Stripe redelivers. The gateway still only reports
   Stripe's state, plus a warning with the refusal's code. The decision stays in the webhook
   because that is where the eligibility rule lives, so a partly paid invoice is kept at
   once instead of after one more retry.
2. **The billing page promised a void it had not checked.** It said "voids the unpaid
   renewal invoice, so nothing is owed" whenever the status was `canceled` with reason
   `payment_failed`, also when the invoice had been paid or partly paid and was kept. Now
   `webhooks.renewal_voided` looks for the `billing.renewal_invoice_voided` audit event of
   the row's subscription. That event is written only after Stripe returned the invoice
   `void`, in the same transaction. If it exists, the page says the invoice was voided and
   nothing is owed. Otherwise it says the invoice was not voided and points to the
   Customer Portal for its status, with no promise of a void or a refund. The past-due
   timeline now states the condition ("if the renewal invoice is then still open with
   nothing paid") instead of a promise.

*Alternatives considered:* raise in the gateway whenever a refused void leaves the invoice
`open`. That is simpler, but it splits the rule across two layers and retries a partly paid
invoice once more before keeping it. For the page, a new `Subscription` column (e.g. the
voided invoice id) would also work, but it needs a migration and must be reset whenever the
row starts following another subscription. The audit event is already keyed by
subscription, and if it is missing the page shows the neutral text (fails safe).

*Limits:* a void that Stripe keeps refusing although the invoice stays open and unpaid makes
Stripe redeliver until it gives up (three days in live mode). Until then the row keeps its
previous state (`past_due`, Free once the grace period has ended), because the cancellation
rolls back with the event. Each attempt logs a warning, and Stripe flags the failing
endpoint. This branch was tested with mocks only, not in the sandbox: Stripe offers no way
to make it refuse a void on demand.

## Revisit when

VAT administration becomes a burden (consider option 3), or usage-based pricing is introduced.
