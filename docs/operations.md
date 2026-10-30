# Operations guide

For the person running the AccessGuard control plane. Items marked *(tested)* were run on the
development machine; the rest are written procedures not yet exercised in production.

## Architecture in production

```text
browser ──https──► reverse proxy ──► web (gunicorn, Django) ──► PostgreSQL
customer runner ──https──► reverse proxy ──► web /api/runner/v1 (bearer token)
Stripe ──https──► web /webhooks/stripe/ (signature verified)
scheduler: sweep_scans (1 min), purge_expired_data + backup_database (daily)
demo container: sample API + platform demo runner (private network to web)
```

The control plane never connects to customer targets (ADR 0009).

## Health and monitoring

| Signal | Where |
| --- | --- |
| Liveness | `GET /healthz` → `{"status": "ok"}` (no dependencies) *(tested)* |
| Readiness | `GET /readyz` → 200 with `database: ok`, 503 if the database fails *(tested)* |
| Logs | JSON lines on stdout with `request_id`; credentials redacted by a logging filter *(tested)* |
| Errors | Set `SENTRY_DSN` (Sentry or self-hosted GlitchTip); PII off, events scrubbed. Without it, `ADMINS` get error e-mails |
| Runner health | Runners page shows online/offline; queued scans show a warning after 60 s |

Every response carries `X-Request-Id`; ask customers for it in support cases.

## Routine jobs

| Job | Command | Frequency |
| --- | --- | --- |
| Mark lost scans | `python web/manage.py sweep_scans` | every minute (also done lazily on access) |
| Retention | `python web/manage.py purge_expired_data [--dry-run]` | daily |
| Backup | `python web/manage.py backup_database --out /backups` | daily, keep 14 days |
| ML export (only with consent, research) | `python web/manage.py export_ml_labels --out <outside repo>` | on demand |

Retention follows each organisation's plan history (30 / 365 / 730 days): finished scans and
reports, resolved or rejected findings, and assistant usage records. Open findings are never
purged. Processed Stripe event ids are kept 90 days.

## Backup and restore

- **SQLite** (local, small installs): `backup_database` makes an online copy and runs an
  integrity check. *(tested: dev database restored into a fresh file, counts matched)*
- **PostgreSQL**: `backup_database` runs `pg_dump --format=custom`; the password goes through
  `PGPASSWORD`, never the command line. *(not tested locally: no PostgreSQL installed)*
- Copy backups off the server (object storage or another machine); keep at least 14 daily
  copies; encrypt them at rest.

Restore drill (do it once before launch and then quarterly):

1. Stop `web`, `scheduler` and `demo` (`docker compose stop web scheduler demo`).
2. Take a fresh backup of the current state, even if it is broken.
3. `python web/manage.py restore_database <backup> [--replace]` (SQLite refuses to overwrite
   an existing database without `--replace`; PostgreSQL uses `pg_restore --clean`).
4. `python web/manage.py migrate --check`, start the services, check `/readyz`, sign in, open
   a recent report.
5. Record date, backup used, duration and result.

## Deploying a new version

1. Run `bash ops/verify.sh` (all gates) on the release commit; CI must be green.
2. Back up the database.
3. Build and start: `docker compose up -d --build web scheduler demo`. The web container runs
   `migrate` before starting gunicorn. Migrations are additive in V1; review any migration
   that drops or rewrites data before deploying it.
4. Check `/readyz`, sign in, run the demo scan.
5. Roll back by redeploying the previous image tag and, only if a migration requires it,
   restoring the backup taken in step 2.

## Security operations

- Rotate `ACCESSGUARD_SECRET_KEY` only with a plan: it signs sessions and password-reset links
  and keys the ML export HMAC (rotating logs everyone out).
- Runner tokens are stored as SHA-256 hashes; a customer revokes them on the Runners page.
- The platform demo runner token: `bootstrap_demo_runner --token-file` revokes the old token.
- Stripe: rotate the webhook secret in Stripe and `.env` together.
- Incident with suspected data exposure: preserve logs, revoke affected runner tokens, inform
  affected customers without undue delay (GDPR Art. 33/34 timelines apply to personal data).

## Known operational limits (V1)

- Login throttling uses the database cache; it is per installation, not global.
- One web container is enough for the beta; scale gunicorn workers first, then containers.
- No email verification of new accounts.
