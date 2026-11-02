# Beta operations plan: hosting, readiness and self-serve onboarding

Decision basis for the smallest useful operation of a **self-serve beta**: customers sign
up, install the runner and scan without talking to us. Prepared 2026-11-02. Prices are
quotes from official sources on that day, not invoices. Nothing has been bought, deployed or
published. The decisions marked "owner" are yours.

The AI assistants are verified for their synthetic scenarios (one real-model run on
2026-10-30, docs/ai-live-verification.md); nothing here changes that, and no model call was
made for this plan.

## 1. Hosting: two EU options

Both run the existing stack; both keep it portable (plain Docker on Linux, data in
PostgreSQL dumps and a restic repository that any other host can restore).

### Prices (EUR per month, excl. VAT, retrieved 2026-11-02)

| Component | A: Hetzner Cloud (Germany/Finland), PostgreSQL in compose | B: Scaleway (Paris), managed PostgreSQL |
| --- | --- | --- |
| Web app server | CX23: 2 vCPU, 4 GB RAM, 40 GB NVMe, 20 TB traffic, IPv4 included: **5.99** | DEV1-S: 2 vCPU, 2 GB RAM, €0.008976/h: **6.55** |
| Public IPv4 | included | flexible IPv4, €0.005/h: **3.65** |
| Persistent storage | 40 GB local NVMe, included (database, volumes) | 20 GB local SSD, €0.000049/GB·h: **0.72** |
| PostgreSQL | in compose on the server (tested locally, incl. restore): **0** | DB-DEV-S, node €0.0136/h + management €0.002/h: **11.39**; 10 GB SBS 5K storage: **0.99**; ~1 GB managed backups: **0.03** |
| Off-site backups (restic) | Storage Box BX11, 1 TB, SFTP/restic, other location (Germany or Finland): **3.20** | Object Storage Standard Multi-AZ, 10 GB at €0.000022/GB·h: **0.16** (+ €0.01/GB egress on restore) |
| Domain (.com) | registrar of choice; example Scaleway .com €12.34/year: **1.03** | same: **1.03** |
| SMTP | Scaleway Transactional Email, Essential plan, ≤ 300 e-mails/month free: **0** | same: **0** |
| **Total** | **≈ 10.22** (≈ 12.77 with 25 % VAT) | **≈ 24.52** (≈ 30.65 with 25 % VAT) |

Sources: Hetzner Cloud plan pages ([cost-optimized](https://www.hetzner.com/cloud/cost-optimized),
[regular performance](https://www.hetzner.com/cloud/regular-performance)) and
[Storage Box](https://www.hetzner.com/storage/storage-box); Scaleway's public product
catalogue API (`api.scaleway.com/product-catalog/v2alpha1/public-catalog/products`, the
data behind its price list; its pricing pages refused automated access) and Scaleway's
documentation for hourly instance billing, flexible IPv4 billing, the SMTP ports and the
Transactional Email limits. Hetzner's FAQ for the outbound port block (25 and 465 blocked,
587 open).

Assumptions:

- 730 hours per month for hourly prices (Scaleway documents hourly billing and no cap).
  Hetzner's monthly figure is its cap.
- VAT: shown excl. VAT; 25 % Danish VAT applies if bought without a VAT number (reverse
  charge with one). Check at order time.
- Volumes: database and dumps under 1 GB in the beta; 14 daily dumps; under 300 e-mails a
  month (the app sends only password resets and server-error e-mails). Above that, Scaleway
  lists €0.00025 per e-mail. The 300 free e-mails come from Scaleway's pricing page as
  quoted in search results; the catalogue lists only the per-e-mail price. **Check in the
  console.**
- Not included: Stripe fees (only with live payments), AI usage (off by default; the
  measured run cost USD 0.007-0.023 per call), optional monitoring services (free tiers or
  self-hosted exist), GitHub Actions minutes for the private repository.
- **Availability risk (A):** on 2026-11-02 Hetzner's public plan pages showed "This product
  is currently unavailable" for the listed shared plans. Check in the Hetzner Console when
  ordering; if CX23 cannot be ordered, the next plans with at least 4 GB on the same pages
  are CX33 (8.99) and CPX22 (19.99), or option B.

### Comparison

| | A: Hetzner, compose PostgreSQL | B: Scaleway, managed PostgreSQL |
| --- | --- | --- |
| Monthly cost | ≈ €10 | ≈ €25 |
| Matches what was tested | yes: the same compose stack ran locally with PostgreSQL 17, backups and a restore drill | partly: the app works with any PostgreSQL URL, but the compose files assume a `db` service; an overlay without `db` and a version check of `pg_dump` against the managed server are still to be written |
| Database operations | ours: daily dumps, off-site copies, restore drills (all prepared) | provider: automated backups, patching, monitoring; our dumps still useful for portability |
| RAM for the app | 4 GB for app and database | 2 GB for the app alone; enough for the beta but tight with 3 gunicorn workers, scheduler, demo, proxy and restic |
| Off-site separation | other location, **same provider** | other zone, **same provider** |
| Lock-in | low | low to medium (managed database) |

### Recommendation

**Option A: one Hetzner Cloud CX23 with the compose stack and the production overlay,
restic backups to a Hetzner Storage Box in the other country, Scaleway Transactional Email
for SMTP, a domain from any registrar. About €10.22 per month excl. VAT (≈ €12.77 incl.
25 % VAT).**

Why: lowest cost, enough memory, and it is exactly the stack that was verified locally
(including backup and restore), so the beta adds no untested architecture. The price of
that choice is operating PostgreSQL ourselves, which the daily dumps, the encrypted
off-site copies, the heartbeat alert and the quarterly restore drill cover. Revisit
managed PostgreSQL (option B) when customer data or uptime expectations grow, or when
someone other than the owner operates the service. A second off-site copy at another
provider is a cheap later improvement (provider-account loss is not covered by A or B).
Rationale as an ADR: docs/adr/0014-single-server-beta-hosting.md (proposed).

## 2. What is prepared in the repository (no server needed)

| Area | Prepared | Verified how |
| --- | --- | --- |
| Deployment | `docker-compose.prod.yml` (proxy, no public app port, log rotation, off-site backups), runbook `docs/deployment.md` | `docker compose config` with both files |
| TLS | Caddy with automatic certificates and http→https (`ops/deploy/Caddyfile`), HSTS from Django | not run (no Caddy, no domain) |
| Secrets | `.env` checked by `ops/deploy/check_env.py` (names only, test-mode Stripe unless approved, placeholders, domain/URL/hosts consistency); restic key material in git-ignored `.secrets/` | 13 unit tests |
| Client address behind the proxy | `ACCESSGUARD_TRUSTED_PROXY_HOPS`: sign-in and reset limits use the visitor's address, not the proxy's (before: every visitor would have shared the proxy's limit) | web tests |
| Password reset | limit of 3 e-mails per address and 10 per IP per hour, same page either way; e-mailed https link tested end to end; SMTP on 587 with STARTTLS (Hetzner leaves 587 open) | web tests; real delivery not tested |
| Monitoring | `/healthz`, `/readyz`, JSON logs, Sentry-compatible DSN (existing); backup heartbeat (dead man's switch) | heartbeat path with stubs |
| Backup and restore | daily `pg_dump` (existing), encrypted off-site copies with retention and weekly `restic check`, refusal when the newest dump is older than 26 h, restore drill procedure | loop logic with stubs; real restic not run |
| Images | Python, PostgreSQL, Caddy and restic pinned by digest | CI builds the web and demo images; the others are not pulled here |
| Runner distribution | in-app download of a checked wheel with its SHA-256 (`ACCESSGUARD_RUNNER_WHEEL`); PyPI not needed | web tests |
| Stripe renewals and retries | `sandbox.py clock-*` commands and a run plan with expected results (`ops/stripe_sandbox/README.md`) | `clock-probe` against the current sandbox: refused, as expected for a claimable sandbox |

## 3. Self-serve beta plan (no mandatory sales meetings)

**Journey, all in the product:** sign-up (no card, no call) → demo scan against the
synthetic API → project from forms or an OpenAPI import → scope approval with attestation →
runner token → **download the runner wheel from the Runners page and check its SHA-256** →
`runner.toml` (generated on the page) → scan → findings with evidence and fix guidance →
triage → retest to verified. Help pages cover the runner, configuration, data handling
and limitations. Optional AI explanations only when the organisation opts in and the server
has a provider configured (off by default).

**Distribution:** the runner wheel built and checked as in docs/install.md (clean-venv
acceptance run) is served by the app itself. Publishing to PyPI is a separate, optional
distribution choice (account, name reservation, release process); the in-app texts keep
warning not to `pip install accessguard` until then.

**Phases (owner decides):**

1. **Free beta.** No Stripe settings on the server: billing shows "Payments are not
   configured" and every organisation has the Free limits (1 project, 10 scans and 60
   checks per scan a month, 1 runner, 2 members). No legal or VAT setup for payments is
   needed yet, but terms, privacy policy and a DPA are (section 4).
2. **Paid beta.** After company registration, VAT handling, legal texts, the test-clock
   verification and the owner's explicit approval to switch to live keys.

**Feedback without meetings:** a support e-mail address on the site (to be provided),
an optional short survey after the first completed scan, and the activation numbers below.
Calls only when a customer asks for one.

**What to measure:** sign-up → demo scan → runner connected → first own scan → retest;
time to first own scan; support e-mails per active organisation; in the paid phase,
conversion and churn (replacing the assumptions in docs/business/economics.md).

## 4. What is still open

### Technical gaps (before inviting customers)

1. Provision the server and run docs/deployment.md, including certificate issuance and a
   restore drill **from the off-site repository**.
2. Real e-mail delivery: `sendtestemail` and a password reset through the chosen provider,
   with SPF/DKIM/DMARC passing.
3. Wire the alerts: uptime check on `/readyz`, backup heartbeat, error reporting.
4. A support contact on the site (the app has none yet; needs the address).
5. Paid phase only: a public Stripe webhook endpoint and the test-clock run (renewal,
   failure, retries, final action).

Known product limits that stay for the beta unless decided otherwise: no e-mail
verification of new accounts (anyone can sign up with any address; the reset link goes to
that address), login limits per installation, English UI.

### Owner decisions

- Host and region (recommendation: A), domain name, SMTP provider, backup target,
  monitoring services.
- Free beta first, or paid beta from the start; the Free limits for beta organisations.
- Stripe: claim the sandbox before **2026-11-06** or create a sandbox in your own account
  (needed for test clocks); the retry settings and the final action after failed retries.
- Whether to enable AI assistance on the server (API key in its own workspace with a spend
  limit) and for which plans.
- Whether and when to publish the runner on PyPI.
- Support address and response-time promise.

### Legal clarifications (lawyer or accountant)

- Terms of service, privacy policy and a data processing agreement for customers, with the
  list of sub-processors actually used (hosting, SMTP, Stripe, and Anthropic or Sentry if
  enabled).
- A DPA with the AI provider before customers can enable AI assistance.
- Company registration (CVR) and VAT handling (Stripe Tax or a merchant of record) before
  any paid plan.
- Review of the authorised-use rules (sign-up checkbox, per-scan attestation) and the
  cookie statement (the app sets only session and CSRF cookies).

### Optional improvements

- Managed PostgreSQL (option B) or a second off-site copy at another provider.
- E-mail verification at sign-up.
- Starting the app's grace period from Stripe's time instead of its own clock (only visible
  with test clocks; identical in production).
- SMTP over implicit TLS (465); HSTS preload once the domain is final; automated deploys
  from CI; image vulnerability scanning; a status page.

## 5. Recommended setup and what is missing

**Hetzner Cloud CX23 + Hetzner Storage Box BX11 (other country) + Scaleway Transactional
Email + a .com or .dk domain: about €10.22/month excl. VAT, about €12.77 incl. 25 % VAT.**

Information and approvals needed from the owner:

1. Approval to order the server and storage box, and the account they go on (company or
   private; VAT number if any).
2. The domain name, and who manages its DNS.
3. The SMTP account and the sender address (for example `no-reply@<domain>`).
4. The support e-mail address shown on the site.
5. The alert receivers: the e-mail for the certificate authority (`ACME_EMAIL`), the
   server-error e-mails, and the heartbeat/uptime services to use.
6. Free beta or paid beta; for paid: the Stripe decision above and, later, explicit
   approval of live keys.
7. Approval to deploy (first deployment and each later one).
