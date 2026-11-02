# Installation

Three ways to run AccessGuard, from quickest to production-like.

| Path | Status (2026-10-30, Windows 11) |
| --- | --- |
| 1. Local product in one command | **tested** (CPython 3.13.13) |
| 2. Docker compose with PostgreSQL | **tested locally** on Docker 29.8 / WSL2: build, migrations, readiness, static files, demo runner, a customer runner against it, scheduled backups, restore into a separate database, 78 web tests on PostgreSQL 17. **Not run on a server** |
| 3. Runner in the customer's environment | **tested** from the built wheel (CPython 3.12.7) and from the source archive (CPython 3.13.13), each in a clean venv on Windows. The engine's tests run in CI on Ubuntu (Python 3.11 and 3.13) and Windows (3.13); installing the wheel on Linux or macOS has not been tried |

The runner is **not on PyPI** yet; publishing it there is an optional distribution choice.
During the beta, customers download the checked wheel from the app's Runners page, with its
SHA-256 shown next to the link (`ACCESSGUARD_RUNNER_WHEEL`, section 3), or install a wheel we
send them. Do not tell anyone to run `pip install accessguard` before the release: the
name is not registered to us on PyPI, so pip would install whatever someone else publishes
under it.

## 1. Local product in one command (development and demos)

Requirements: Python 3.12 or newer for the web part (3.11+ for the engine/runner), Git.

```powershell
cd C:\AccessGuard
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python ops\dev\run_stack.py
```

Open <http://127.0.0.1:8000/>, create an account and run the demo scan. The script migrates a
SQLite database in `.local/`, starts the synthetic sample API (flawed on 8800, fixed on
8801), the platform demo runner and the Django development server. Ctrl+C stops everything.
On Linux/macOS use `.venv/bin/python`.

Optional browser check while the stack runs (uses the installed Chrome, downloads nothing):

```powershell
.\.venv\Scripts\python -m pip install --require-hashes -r requirements-e2e.txt
$env:PYTHONPATH = "src"; .\.venv\Scripts\python ops\dev\browser_check.py --out reports\browser
```

## 2. Single server with Docker compose (production-like)

```bash
cp .env.example .env          # fill in every placeholder; never commit .env
docker compose up -d --build
docker compose exec web python web/manage.py createsuperuser
```

Services: `db` (PostgreSQL 17), `web` (gunicorn on 127.0.0.1:8000; runs `migrate` before it
starts), `scheduler` (starts after `web` is healthy; lost-scan sweep every minute, retention
purge and a database backup daily into the `backups` volume), `demo` (sample API and demo
runner). Every service restarts unless stopped, so the stack comes back after a host reboot
once the Docker daemon runs. For a public server add the production overlay
`docker-compose.prod.yml` (Caddy for TLS, no public port on `web`, encrypted off-site
backups, log rotation) and follow [deployment.md](deployment.md); it needs
`ACCESSGUARD_BEHIND_PROXY=true` and `ACCESSGUARD_TRUSTED_PROXY_HOPS=1`. The admin console is
at `ACCESSGUARD_ADMIN_PATH`.

The images are built from `python:3.13-slim-trixie` (Debian 13, whose `postgresql-client` 17
matches the server; see docs/operations.md). Only the paths in `.dockerignore`'s allowlist
enter the build: no `.env`, local databases, caches or tests. The base images are pinned by
digest (2026-11-02): `python:3.13-slim-trixie@sha256:2d44738e…` in the Dockerfile (the one
built and run locally on 2026-10-30; still the tag's current digest on 2026-11-02) and
`postgres:17.11-bookworm@sha256:839fbb5a…` in `docker-compose.yml` and CI (read from Docker
Hub; the local run on 2026-10-30 also reported PostgreSQL 17.11, but its digest was not
recorded). Updating them: deployment.md, "Updates".

**What the local compose verification covered (2026-10-30)**, with an override that only
pointed `env_file` at a throwaway env file and published the database on 127.0.0.1 for tests:

- Fresh `up`: db healthy, web healthy after migrations, `/healthz` and `/readyz` 200
  (`database: ok`).
- Static files served by WhiteNoise with hashed names and `immutable` caching; CSP and
  `X-Frame-Options: DENY` present.
- Sign-up started the demo scan; the demo runner in the `demo` container claimed it over the
  compose network and completed it with findings.
- A runner installed from the wheel on the host scanned the sample app for a customer project
  on the containerised control plane and uploaded its report (10 findings). The retest after
  that was interrupted by a host crash (below), so the full retest-to-resolved path was
  verified on the local stack (path 1 and `ops/dev/customer_acceptance.py`), not in Docker.
- Scheduler: sweep, purge and backup (`accessguard-*.dump`) after the fixes in commit
  `5ca94fc`.
- Persistence: the host crashed twice during the session. Once Docker Desktop ran again after
  the second reboot, the services came back by their restart policy without a `compose up`,
  and users, scans, findings and the backup file were still there.
- Backup restored into a separate database with identical row counts and content digest.
- `web/manage.py test accessguard_web.tests` against the compose PostgreSQL: 78 tests OK.

Not covered: a deliberate `docker compose restart` on a quiet machine, the TLS reverse proxy,
a real domain, real SMTP, Sentry, and any server other than the development laptop.

Stripe (test mode): create the Team (EUR 49/month) and Business (EUR 199/month) prices and a
Customer Portal configuration (cancel at period end, switch between the two prices with
proration behaviour **`always_invoice`**, payment methods, invoice history), set
`STRIPE_SECRET_KEY`, `STRIPE_PRICE_TEAM`, `STRIPE_PRICE_BUSINESS` and
`STRIPE_PORTAL_CONFIGURATION` in `.env`, and point a webhook endpoint at
`https://<host>/webhooks/stripe/` with the events `checkout.session.completed`,
`customer.subscription.created|updated|deleted|paused|resumed|pending_update_applied|pending_update_expired`,
`invoice.payment_failed`, `invoice.paid`, `invoice.payment_succeeded`
(`python ops/stripe_sandbox/sandbox.py events` prints the list); its signing secret goes
into `STRIPE_WEBHOOK_SECRET`. Keys that are not test keys are refused unless
`ACCESSGUARD_ALLOW_LIVE_PAYMENTS=true` (needs explicit approval).

The portal's proration behaviour matters: with `create_prorations` the portal switches to
the higher plan at once and bills the difference only at renewal, so the higher plan would
be active unpaid. With `always_invoice` the portal charges at once and, when the payment
fails, keeps the old price until the invoice is paid (a pending update). The webhook needs
Stripe's API at runtime: it fetches the subscription for every event and answers 503 when
Stripe cannot be reached, so Stripe retries.

Locally (verified 2026-10-30 against a claimable sandbox; see docs/HANDOFF.md): the
opt-in drivers and step-by-step instructions are in
[ops/stripe_sandbox/README.md](../ops/stripe_sandbox/README.md). Keep the values in a
git-ignored file such as `.local/stripe/stripe.env`, forward webhooks with
`stripe listen --events <the list above> --forward-to http://127.0.0.1:8000/webhooks/stripe/`
(`stripe listen --print-secret` prints the signing secret for `STRIPE_WEBHOOK_SECRET`), and
start the stack with `run_stack.py --env-file .local/stripe/stripe.env`. The stack prints
the names, never the values, of the variables it loaded.

## 3. Runner in the customer's environment

**We build the package** (from a clean checkout of the release commit):

```bash
.venv/Scripts/python -m build --no-isolation --outdir dist .      # Linux/macOS: .venv/bin/python
.venv/Scripts/python ops/ci/check_wheel.py dist                   # content check: engine only
```

This writes `dist/accessguard-<version>-py3-none-any.whl` (the runner has no third-party
dependencies) and `dist/accessguard-<version>.tar.gz`. After the acceptance check below,
copy the wheel to the server's `dist/` folder and set `ACCESSGUARD_RUNNER_WHEEL` to it:
signed-in members then download it from the Runners page and the help page, which show
its SHA-256. Without that setting, send the customer the wheel and its SHA-256
(`sha256sum dist/*.whl`, PowerShell: `Get-FileHash`).

**The customer installs it** into its own virtual environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install ./accessguard-<version>-py3-none-any.whl
# Windows: .venv\Scripts\python -m pip install .\accessguard-<version>-py3-none-any.whl
export ACCESSGUARD_RUNNER_TOKEN=agr_...                      # from the Runners page
.venv/bin/accessguard runner --config runner.toml
```

See the in-app help page "Installing the runner" (`/help/runner/`) for `runner.toml`, network
zones and what the runner refuses. Offline/CI: `accessguard scan --spec spec.json --config
runner.toml --out reports/accessguard` (exit codes on `/help/ci/`).

**Acceptance check of a build** (what was run on 2026-10-30): install the wheel into a clean
venv made from a base interpreter (not the repository's `.venv`, no `PYTHONPATH`), then

```bash
.venv/Scripts/python ops/dev/customer_acceptance.py --runner-venv <clean venv> --out <new dir>
```

It starts a control plane on a fresh SQLite file in `<new dir>`, runs the separate sample
app as the customer's API, and drives the real forms over HTTP: sign-up, project setup
(including the empty extra rows a browser submits), runner token, approval, a scan by the
installed runner (10 confirmed findings, no credential in the report), triage, a redeploy of
the fixed variant on the same origin and a retest to 10/10 resolved and verified. Results:
PASS with the wheel on CPython 3.12.7 and with the source archive on CPython 3.13.13, about
15 s each. With `--external http://127.0.0.1:8000` it targets a running stack instead.

## Configuration reference

All settings come from environment variables; `.env.example` lists them with placeholders.
Production is the default: without `ACCESSGUARD_DEBUG=1` the app refuses to start without
`ACCESSGUARD_SECRET_KEY`. `python ops/ci/deploy_check.py` runs Django's deployment checks
with production settings. For plain-http testing on 127.0.0.1 only, set
`ACCESSGUARD_SECURE_COOKIES=false`, `ACCESSGUARD_SSL_REDIRECT=false` and
`ACCESSGUARD_HSTS_SECONDS=0`; never on a public host.
