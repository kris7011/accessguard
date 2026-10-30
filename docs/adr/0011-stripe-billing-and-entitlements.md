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

## Revisit when

VAT administration becomes a burden (consider option 3), or usage-based pricing is introduced.
