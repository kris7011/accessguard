# Launch checklist (paid beta)

Each item is either **done** (verified on the development machine), **ready** (prepared,
needs an external account or the owner's approval) or **open**.

## Product

| Item | Status |
| --- | --- |
| Sign-up, organisations, roles, tenant isolation (tests incl. cross-org 404s) | done |
| Demo with the synthetic API; flawed → findings → fixed → retest → resolved | done (tests + browser) |
| Project setup through forms, OpenAPI import, scope approval with attestation | done |
| Scan lifecycle: progress, cancel, lost runner, partial reports never green | done |
| Findings: evidence, guidance, triage, retest with positive controls, history | done |
| Runner: install, allowlist, credentials local, offline CI mode | done (tested on Windows) |
| AI assistants | done in code; **real model not verified** (needs `ANTHROPIC_API_KEY` or a local Ollama run) |
| Browser check desktop + phone (Chrome) | done |
| Publish the runner package (`pip install accessguard`) | ready: wheel builds and passes its content check; publishing needs approval and a PyPI account |

## Infrastructure

| Item | Status |
| --- | --- |
| Hosting account and domain (any Docker host or VPS; no vendor lock-in) | **open: needs the owner** |
| Build and run `docker compose` on the server; pin the base images by digest | ready (not built locally) |
| TLS reverse proxy, HSTS (preload only once the domain is final) | ready |
| PostgreSQL backups copied off-server; first restore drill on the server | ready (procedure in docs/operations.md) |
| Error monitoring (`SENTRY_DSN`) and admin e-mail | ready (needs an account or self-hosted GlitchTip) |
| SMTP for password reset | ready (needs an SMTP provider) |
| Push the branch and let CI run (engine matrix, web on PostgreSQL, images) | **needs approval to push** |

## Payments

| Item | Status |
| --- | --- |
| Stripe test account: products/prices for Team and Business, webhook endpoint, Customer Portal | **open: needs a Stripe account** |
| Run a test-mode purchase, failed payment (card `4000 0000 0000 0341`), cancellation | open (after the account exists) |
| VAT: Stripe Tax or a merchant of record; Danish CVR registration for the business | **open: owner + accountant** |
| Switch to live keys | **needs explicit approval**; only after the test-mode run and legal texts |

## Legal and trust

| Item | Status |
| --- | --- |
| Terms of service, privacy policy, data processing agreement (DPA) | **open: draft with a lawyer**; the site shows a beta summary only |
| DPA with the AI provider before enabling AI assistance for customers | open |
| Responsible-use rule (only authorised targets) | done in product (sign-up checkbox, per-scan attestation, runner allowlist) |
| No certifications, testimonials or guarantees claimed on the site | done (review before every copy change) |

## Go-to-market

| Item | Status |
| --- | --- |
| Landing, pricing, example report, help pages | done |
| Ad drafts and product texts | done (docs/business/) — **publishing needs approval** |
| Economics model with explicit assumptions | done (docs/business/economics.md) |
| First 5-10 design-partner conversations (validate price and value) | open |
