# Operations guide

For the person running the AccessGuard control plane. Items marked *(tested)* were run on the
development machine; *(tested in Docker)* means the compose stack (PostgreSQL 17.11, images on
Debian 13) on Docker 29.8 / WSL2 on 2026-10-30. Nothing here has run on a production server yet.
The step-by-step deployment of a public server (TLS proxy, secrets, e-mail, off-site
backups, alerts) is in [deployment.md](deployment.md); the hosting choice in
[beta-operations-plan.md](beta-operations-plan.md).

## Architecture in production

```text
browser ──https──► proxy (Caddy) ──► web (gunicorn, Django) ──► PostgreSQL
customer runner ──https──► proxy ──► web /api/runner/v1 (bearer token)
Stripe ──https──► proxy ──► web /webhooks/stripe/ (signature verified)
scheduler: sweep_scans (1 min), purge_expired_data + backup_database (daily)
offsite-backup: restic copy of the dumps to off-site storage (daily), heartbeat ping
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
| Backups | `offsite-backup` pings `ACCESSGUARD_BACKUP_HEARTBEAT_URL` after each successful off-site copy; alert when the ping stops (prepared; loop checked with stubs) |

Every response carries `X-Request-Id`; ask customers for it in support cases.

## Routine jobs

| Job | Command | Frequency |
| --- | --- | --- |
| Mark lost scans | `python web/manage.py sweep_scans` | every minute (also done lazily on access) *(tested in Docker)* |
| Retention | `python web/manage.py purge_expired_data [--dry-run]` | daily *(tested in Docker)* |
| Backup | `python web/manage.py backup_database --out /backups` | daily, keep 14 days *(tested in Docker)* |

The `scheduler` container runs these (`ops/docker/scheduler.sh`). It starts only after `web`
is healthy (web migrates first). A failing job does not stop it: it logs a line starting with
`scheduler: ERROR` and retries the daily purge and backup after 15 minutes. **Alert on that
line** (or check that a new `accessguard-*.dump` appears in the `backups` volume every day).
| ML export (only with consent, research) | `python web/manage.py export_ml_labels --out <outside repo>` | on demand |

Retention follows each organisation's plan history (30 / 365 / 730 days): finished scans and
reports, resolved or rejected findings, and assistant usage records. Open findings are never
purged. Processed Stripe event ids are kept 90 days.

## Backup and restore

- **SQLite** (local, small installs): `backup_database` makes an online copy and runs an
  integrity check. *(tested: dev database restored into a fresh file, counts matched)*
- **PostgreSQL**: `backup_database` runs `pg_dump --format=custom`; the password goes through
  `PGPASSWORD`, never the command line. `pg_dump` must be at least the server's major
  version: the images ship `postgresql-client` 17 (Debian 13) for the PostgreSQL 17 server in
  `docker-compose.yml`. Upgrading the server to 18 requires a matching client first.
  *(tested in Docker: scheduled and manual backups, and the restore drill below)*
- **Off-site:** the `offsite-backup` service of `docker-compose.prod.yml` encrypts the
  `backups` volume with restic and copies it to SFTP or S3-compatible storage daily
  (14 daily, 8 weekly, 6 monthly snapshots; `restic check` on Sundays). It refuses to run
  when the newest dump is older than 26 hours. Set-up and the restore drill from off-site
  storage: deployment.md, section 6. *(prepared; not run against a real repository)*

Restore drill (do it once before launch and then quarterly):

1. Stop `web`, `scheduler` and `demo` (`docker compose stop web scheduler demo`).
2. Take a fresh backup of the current state, even if it is broken.
3. `python web/manage.py restore_database <backup> [--replace]` (SQLite refuses to overwrite
   an existing database without `--replace`; PostgreSQL uses `pg_restore --clean`).
4. `python web/manage.py migrate --check`, start the services, check `/readyz`, sign in, open
   a recent report.
5. Record date, backup used, duration and result.

To rehearse without touching the live database, restore into a separate one:

```bash
docker compose exec db createdb -U accessguard accessguard_restore_drill
docker compose exec -e DATABASE_URL=postgres://accessguard:<password>@db:5432/accessguard_restore_drill \
  scheduler python web/manage.py restore_database /backups/accessguard-<stamp>.dump
```

Drill record, 2026-10-30, local compose stack (test data only): backup
`accessguard-20261030T082535Z.dump` (107,049 bytes) restored into
`accessguard_restore_drill` in 1 s; `migrate --check` clean; exact row counts for every model
(2 users, 2 organisations, 3 projects, 3 scans, 20 findings, 21 finding events, 18 audit
events, ...) and a digest over user e-mails, finding fingerprints and statuses and every
scan's report matched the source database. Not yet done on a production server.

## Deploying a new version

1. Run `bash ops/verify.sh` (all gates) on the release commit; CI must be green.
2. Back up the database.
3. Build and start: `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`
   on a public server (`docker compose up -d --build web scheduler demo` locally). The web
   container runs `migrate` before starting gunicorn. Migrations are additive in V1; review any migration
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

- Login throttling uses the database cache; it is per installation, not global. Behind the
  proxy it keys on the visitor's address (`ACCESSGUARD_TRUSTED_PROXY_HOPS=1`, the entry the
  proxy added to `X-Forwarded-For`); with the default 0 every visitor behind a proxy would
  share one limit. *(tested)*
- Password-reset e-mails are limited to 3 per address and 10 per IP address per hour; over
  the limit the same "sent" page appears and nothing is sent. *(tested)*
- One web container is enough for the beta; scale gunicorn workers first, then containers.
- No email verification of new accounts.
- SMTP always uses STARTTLS (`EMAIL_USE_TLS = True`, port 587 by default); providers that only
  offer implicit TLS on port 465 are not supported by the settings yet. Port 587 is also the
  one cloud hosts that block outbound 25 and 465 (Hetzner Cloud, for example) leave open. Real e-mail delivery
  has not been tested (no SMTP account; only the console backend).
- Machine size: the compose stack itself is small (images 293 MB web, 121 MB demo), but the
  development laptop (7.3 GB RAM, about 2 GB free commit memory with an editor and a browser
  open) crashed twice with a GPU-driver bugcheck (`0x116`, `STATUS_INSUFFICIENT_RESOURCES`)
  while Docker Desktop's VM ran. Give a server at least 2 GB RAM for the stack alone and do
  not run it next to memory-heavy desktop software.
