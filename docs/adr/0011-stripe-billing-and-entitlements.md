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
to the paid plan when a retry or a payment succeeds.

## Revisit when

VAT administration becomes a burden (consider option 3), or usage-based pricing is introduced.
