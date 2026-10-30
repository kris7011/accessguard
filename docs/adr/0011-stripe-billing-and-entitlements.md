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

## Revisit when

VAT administration becomes a burden (consider option 3), or usage-based pricing is introduced.
