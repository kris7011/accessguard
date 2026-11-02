# Launch checklist (self-serve beta)

> **Paused (2026-11-02).** The commercial launch is on hold and the project is closed as a
> portfolio project. This checklist is kept as the record of what was done and what a launch
> would still need; no open item below is being worked on.

The plan, the hosting decision basis and the open items by kind (technical, owner,
legal, optional): [beta-operations-plan.md](beta-operations-plan.md).

Status per item, updated 2026-11-02:

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
| Self-serve runner download (checked wheel served by the app with its SHA-256, members only) | done in code and tests (2026-11-02); needs the wheel placed on the server (`ACCESSGUARD_RUNNER_WHEEL`) |
| Support contact on the site | **open: needs the owner's support address** |
| AI assistants with a real model | done for the synthetic scenarios: one opt-in run against the Claude API on 2026-10-30 (`claude-opus-5-5`, 3 calls, USD 0.034 estimated, 10 tests OK; docs/ai-live-verification.md). Answer quality beyond those scenarios is not measured; the local Ollama provider is not exercised |
| Browser check desktop + phone (Chrome) | done: `ops/dev/browser_check.py` re-run on 2026-11-02 after the template changes, 0 problems; the demo path walked through in headless Chrome the same day (docs/demo.md) |
| Publish the runner on PyPI (`pip install accessguard`) | **optional distribution choice**; the beta uses the in-app download. Wheel and sdist build and install; publishing needs a PyPI account and approval. The names `accessguard` and `accessguard-runner` were free on PyPI on 2026-10-30 |

## Infrastructure

| Item | Status |
| --- | --- |
| Build and run `docker compose` | done locally (Docker 29.8 / WSL2, PostgreSQL 17.11): migrations, readiness, static files, demo runner, external runner, scheduler, persistence. Three defects fixed (`5ca94fc`) |
| Web tests on PostgreSQL | done locally (78/78 against the compose database) |
| Backup and restore drill on PostgreSQL | done locally (restore into a separate database, exact match); **repeat on the server** |
| Hosting account and domain (any Docker host or VPS; no vendor lock-in) | **blocked: needs the owner**; recommendation and prices in beta-operations-plan.md (Hetzner CX23 + Storage Box, about €10/month excl. VAT); runbook docs/deployment.md |
| Pin the base images by digest; build on the server | done in the files (2026-11-02): Python (the digest built locally on 2026-10-30), PostgreSQL 17.11, Caddy 2.11.6, restic 0.19.1; CI builds the app images. Building on the server: ready |
| TLS reverse proxy, HSTS (preload only once the domain is final) | ready: Caddy in `docker-compose.prod.yml` (merged config checked); not exercised without a domain |
| Visitor address behind the proxy (sign-in and reset limits) | done: `ACCESSGUARD_TRUSTED_PROXY_HOPS`, web tests |
| Production `.env` check | done: `ops/deploy/check_env.py` (names only), unit tests |
| Off-server copies of the daily backups | ready: encrypted restic copies with retention (`offsite-backup` service); loop logic checked with stubs; needs the storage account and a restore drill from it |
| Alert on a missing daily dump or off-site copy | ready: heartbeat ping after each successful off-site copy, skipped when the newest dump is older than 26 h; needs the heartbeat service choice |
| Error monitoring (`SENTRY_DSN`) and admin e-mail | ready (needs an account or self-hosted GlitchTip) |
| SMTP for password reset | **blocked: needs an SMTP provider** offering STARTTLS on 587 (implicit TLS on 465 is not supported by the settings). Reset flow and its e-mail limit tested with Django's test mail backend; real delivery not tested |
| Push the branch and let CI run (engine matrix incl. Python 3.11 and Linux, web on PostgreSQL, images) | done: all six jobs passed on PR #1 at `36256a5` (run 41464989398); the linked run records this verification snapshot |

## Payments

| Item | Status |
| --- | --- |
| Stripe test account: products/prices for Team and Business, webhook endpoint, Customer Portal | **done**: the sandbox was claimed by the owner (2026-11-02, account `acct_1UWAdYB9QLrE2rSI`, same prices and portal configuration); `stripe listen` only. A webhook endpoint on the server remains |
| Run a test-mode purchase, failed payment (card `4000 0000 0000 0341`), cancellation | **done in the sandbox** (docs/HANDOFF.md) |
| Renewal, its failure, Stripe's retries and final action (test clocks) | **done in the sandbox** (2026-11-02, ops/stripe_sandbox/README.md): renewal paid; renewal failed → 8 retries over 14 days → subscription cancelled → Free; recovery by retry and by payment; grace boundary in app time. Observed final action: cancel, invoice left `open` and still payable (owner decision below). The Dashboard retry settings themselves were not read |
| What happens to the open invoice after the last retry | **done in the sandbox** (2026-11-02): the webhook voids the open renewal invoice when Stripe cancels for `payment_failed` (ADR 0011); paying it is refused by Stripe; re-subscription through Checkout works. Dashboard settings confirmed by the owner (8 retries in 2 weeks, cancel, leave invoice past-due), unchanged. Still open for the owner: accountant's view on voiding invoices; refund policy for the rare renewal invoice paid in the seconds before the void (logged as a warning) |
| Grace period vs. Stripe's retries | 7-day grace kept for the beta; the billing page and banner show both dates. `ACCESSGUARD_STRIPE_RETRY_DAYS` must match the live account's Dashboard |
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
| Self-serve beta without mandatory calls: activation metrics, support address, optional survey | open (plan in beta-operations-plan.md, section 3) |
