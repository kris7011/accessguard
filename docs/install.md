# Installation

Three ways to run AccessGuard, from quickest to production-like. Status per path:
**local stack: tested** (Windows 11, CPython 3.13); **Docker compose: written, not built**
on the development machine (Docker daemon not running); **runner on a customer machine:
tested** against the local stack and the sample app.

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

Services: `db` (PostgreSQL 17), `web` (gunicorn on 127.0.0.1:8000), `scheduler` (lost-scan
sweep every minute; retention purge and a database backup daily into the `backups`
volume), `demo` (sample API and demo runner). Put a TLS-terminating reverse proxy in front of
`web` (for example Caddy: `reverse_proxy 127.0.0.1:8000`) and keep
`ACCESSGUARD_BEHIND_PROXY=true`. The admin console is at `ACCESSGUARD_ADMIN_PATH`.

Stripe (test mode): create the Team and Business prices, set `STRIPE_*` in `.env`, and point a
webhook endpoint at `https://<host>/webhooks/stripe/` with the events
`checkout.session.completed`, `customer.subscription.created|updated|deleted`,
`invoice.payment_failed`, `invoice.paid`. Enable the Customer Portal (cancellation, payment
method, invoices) in the Stripe dashboard.

## 3. Runner in the customer's environment

```bash
python -m venv .venv && .venv/bin/pip install accessguard   # or the source archive during the beta
export ACCESSGUARD_RUNNER_TOKEN=agr_...                      # from the Runners page
accessguard runner --config runner.toml
```

See the in-app help page "Installing the runner" (`/help/runner/`) for `runner.toml`, network
zones and what the runner refuses. Offline/CI: `accessguard scan --spec spec.json --config
runner.toml --out reports/accessguard` (exit codes on `/help/ci/`).

## Configuration reference

All settings come from environment variables; `.env.example` lists them with placeholders.
Production is the default: without `ACCESSGUARD_DEBUG=1` the app refuses to start without
`ACCESSGUARD_SECRET_KEY`. `python ops/ci/deploy_check.py` runs Django's deployment checks
with production settings.
