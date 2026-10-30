# Launch checklist (paid beta)

Status per item, updated 2026-10-30:

- **done**: verified on the development machine (tests or a recorded run; mocks do not count).
- **ready**: prepared and locally checked as far as possible; the last step needs an external
  account, a server or the owner's approval.
- **blocked**: cannot proceed until the owner provides what is named.
- **open**: not started.

## Product

| Item | Status |
| --- | --- |
| Sign-up, organisations, roles, tenant isolation (tests incl. cross-org 404s) | done |
| Demo with the synthetic API; flawed → findings → fixed → retest → resolved | done (tests + browser; demo runner also in Docker) |
| Project setup through forms, OpenAPI import, scope approval with attestation | done; a browser-only defect on the rules page was found by the acceptance run and fixed (`054abc8`) |
| Scan lifecycle: progress, cancel, lost runner, partial reports never green | done |
| Findings: evidence, guidance, triage, retest with positive controls, history | done |
| Runner from the distribution package: clean venv, no repository on the path, full customer journey | done: wheel on CPython 3.12.7 and source archive on 3.13.13, `ops/dev/customer_acceptance.py` PASS |
| In-app install instructions | done: wheel install, warning against the unpublished PyPI name (`0ccdd87`) |
| AI assistants with a real model | **blocked: needs `ANTHROPIC_API_KEY`** (about 5 calls, well under $1 at Opus 5.5 list prices), or a machine with more free memory for the local model. Fake providers only so far |
| Browser check desktop + phone (Chrome) | done (previous session; templates changed since only in the runner install text) |
| Reserve the name and publish the runner (`pip install accessguard`) | ready: wheel and sdist build and install. **Needs a PyPI account and approval.** The names `accessguard` and `accessguard-runner` were free on PyPI on 2026-10-30 |

## Infrastructure

| Item | Status |
| --- | --- |
| Build and run `docker compose` | done locally (Docker 29.8 / WSL2, PostgreSQL 17.11): migrations, readiness, static files, demo runner, external runner, scheduler, persistence. Three defects fixed (`5ca94fc`) |
| Web tests on PostgreSQL | done locally (78/78 against the compose database) |
| Backup and restore drill on PostgreSQL | done locally (restore into a separate database, exact match); **repeat on the server** |
| Hosting account and domain (any Docker host or VPS; no vendor lock-in) | **blocked: needs the owner** |
| Pin the base images by digest; build on the server | ready (python digest recorded in docs/install.md; record the postgres digest on the server) |
| TLS reverse proxy, HSTS (preload only once the domain is final) | ready (not exercised) |
| Off-server copies of the daily backups | ready (procedure in docs/operations.md) |
| Alert on `scheduler: ERROR` lines / missing daily dump | ready (documented; needs the monitoring choice) |
| Error monitoring (`SENTRY_DSN`) and admin e-mail | ready (needs an account or self-hosted GlitchTip) |
| SMTP for password reset | **blocked: needs an SMTP provider** offering STARTTLS on 587 (implicit TLS on 465 is not supported by the settings). Not tested beyond the console backend |
| Push the branch and let CI run (engine matrix incl. Python 3.11 and Linux, web on PostgreSQL, images) | **needs approval to push** (GitHub remote below) |

## Payments

| Item | Status |
| --- | --- |
| Stripe test account: products/prices for Team and Business, webhook endpoint, Customer Portal | **blocked: needs a Stripe account** (no `STRIPE_*` configuration exists) |
| Run a test-mode purchase, failed payment (card `4000 0000 0000 0341`), cancellation | open (after the account exists); webhook logic tested with SDK-signed fixtures only |
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
| Ad drafts and product texts | done (docs/business/); **publishing needs approval** |
| Economics model with explicit assumptions | done (docs/business/economics.md) |
| First 5-10 design-partner conversations (validate price and value) | open |
