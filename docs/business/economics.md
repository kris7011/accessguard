# Economics model (hypotheses)

**Nothing here is a forecast or a promise.** Prices, conversion, churn and acquisition cost
are unvalidated assumptions. The model exists so the assumptions are explicit and can be
replaced with measured numbers as soon as the first customers pay.

Reproduce: `python ops/business/economics.py` (assumptions in `ops/business/assumptions.json`).

## Three different numbers

| Number | Meaning |
| --- | --- |
| **Revenue** | What customers pay, excluding VAT. Not money the owner can spend. |
| **Operating profit** | Revenue minus payment fees, AI cost, hosting, support, fixed costs and customer acquisition. Before tax. |
| **Private payout** | What reaches the owner if a Danish ApS pays 22 % corporate tax and distributes the rest as dividends (27 % up to a yearly bracket, 42 % above). No salary is modelled. |

## Assumptions and where to check them

| Assumption | Value | Check against |
| --- | --- | --- |
| Prices | Team €49, Business €199 per month | Conversion in the paid beta phase; feedback customers give voluntarily (survey, support e-mail); competitor price pages |
| Plan mix | 20 % Business | First 20 paying customers |
| Payment fees | 1.5 % + €0.25 per charge | Current Stripe pricing for your country and card mix |
| AI use | 30 assistant requests per paying customer and month, 3k input / 1k output tokens | `AssistantRun` usage records |
| AI price | $4 / $20 per million input/output tokens (Claude Opus 5.5 list price), €0.92 per $ | Anthropic pricing page at the time of use |
| Hosting | €60 fixed + €0.5 per customer | Actual invoices. Conservative: the recommended beta set-up was quoted at about €10/month excl. VAT on 2026-11-02 (docs/beta-operations-plan.md); not changed here until invoices exist |
| Support | €4 per customer and month (owner's time valued conservatively) | Time tracking |
| Fixed costs | €80 tools + €150 accounting and legal | Actual invoices |
| Acquisition | €300 per new paying customer | Ad spend and conversion per channel |
| Churn | 3 % per month | Cohorts after 3-6 months |
| Danish taxes | 22 % corporate; dividend 27 % / 42 %, bracket ≈ €10,000 per year | Skat.dk for the current year (the bracket changes yearly); an accountant |

## Scenarios (output of the script, EUR)

AI cost per paying customer and month: **€0.88**.

| Scenario | Paying customers | Revenue / month | Payment fees | AI cost | Hosting + support | Fixed costs | Acquisition | Operating profit / month | Operating profit / year | Private payout / year | Customers lost / month (churn) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| first_beta | 5 | 395 | 7 | 4 | 82 | 230 | 600 | -529 | -6,349 | 0 | 0.1 |
| early_traction | 40 | 3,160 | 57 | 35 | 240 | 230 | 1,800 | 797 | 9,567 | 5,448 | 1.2 |
| established_small | 250 | 19,750 | 359 | 221 | 1,185 | 230 | 4,500 | 13,255 | 159,065 | 73,461 | 7.5 |
| ambitious | 1500 | 118,500 | 2,152 | 1,325 | 6,810 | 230 | 18,000 | 89,983 | 1,079,792 | 489,998 | 45.0 |

## How to read it

- **Gross margin is high** (fees, AI and hosting are under 10 % of revenue), because the
  customer's runner does the scanning. Acquisition and the owner's time are the real costs.
- **The first beta loses money** under these assumptions; that is expected and cheap.
- **The deciding numbers are acquisition cost and churn.** At 3 % churn, 250 customers need
  about 7-8 new customers a month just to stay flat.
- **The "ambitious" row is not a plan.** 1,500 customers would need employees (support,
  sales, engineering), which the model does not include; profit would be much lower.
- A seven-figure private income is not supported by evidence today. Revenue at that level
  needs validated demand, retention and a repeatable channel, none of which exist yet.

## Next measurements

1. Activation (self-serve, no mandatory calls): share of sign-ups that run the demo,
   download and connect a runner and complete a scan of their own.
2. Willingness to pay: conversion once paid plans are switched on; optional feedback calls
   only when a customer asks for one.
3. Conversion and churn from the first paying cohort; replace the assumptions file.
