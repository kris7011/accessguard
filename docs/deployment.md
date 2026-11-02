# Deploying the beta on one server

Runbook for the first public deployment: one Linux server running the existing compose
stack plus `docker-compose.prod.yml` (TLS proxy, off-site backups, log rotation). The choice
of host and its cost are in [beta-operations-plan.md](beta-operations-plan.md).

**Status (2026-11-02): prepared, not run.** No server exists yet. What has been checked
without one is listed at the end; everything else in this file is a procedure to follow and
record, not a result.

Every step that needs an account, a payment, a DNS change or a login is the owner's.

## 0. What you need first

| Item | Used for |
| --- | --- |
| A server: Debian 13 or Ubuntu 24.04, x86-64, at least 2 vCPU / 4 GB RAM / 40 GB disk, public IPv4 | the stack (PostgreSQL in compose) |
| A domain and access to its DNS | `ACCESSGUARD_DOMAIN`, TLS certificate, e-mail sender records |
| An SMTP provider with STARTTLS on port 587 and a verified sender domain | password reset, server-error e-mails |
| Off-site storage reachable by restic (SFTP storage box or S3-compatible bucket), in another data centre than the server | encrypted daily backups |
| A Stripe test environment with the products, portal configuration and a webhook endpoint (below) | billing in test mode |
| Optional: a heartbeat monitor (any service that alerts when a URL is not pinged), an uptime check, a Sentry-compatible DSN | alerts |
| The checked runner wheel from the release commit (`docs/install.md`, section 3) | self-serve runner download |

## 1. Server

1. Create the server with your SSH key only (no password login). Note its IPv4 and IPv6.
2. Firewall (the provider's firewall, or `ufw`): inbound 22/tcp (ideally only from your IP),
   80/tcp, 443/tcp, 443/udp. Nothing else; PostgreSQL and the app are not published.
3. Create a non-root user for deployment, add it to the `docker` group, disable root SSH
   login, enable unattended security updates (`unattended-upgrades` on Debian/Ubuntu).
4. Install Docker Engine and the compose plugin from Docker's own apt repository (Compose
   2.18 or newer is required for `!reset` in the overlay).
5. Outbound mail: some hosts block ports 25 and 465 by default; 587 is what the app uses.

## 2. Code and secrets on the server

The repository is private, so no GitHub credentials go to the server. Copy the release:

```bash
# on your machine, from a clean checkout of the release commit
git archive --format=tar.gz -o accessguard-<commit>.tar.gz <commit>
scp accessguard-<commit>.tar.gz deploy@<server>:
# on the server
mkdir -p ~/accessguard && tar -xzf accessguard-<commit>.tar.gz -C ~/accessguard
```

Then, in `~/accessguard`:

1. `cp .env.example .env && chmod 600 .env` and fill in every value. Generate secrets on the
   server, for example `python3 -c "import secrets; print(secrets.token_urlsafe(50))"` for
   `ACCESSGUARD_SECRET_KEY`, and again for `POSTGRES_PASSWORD` and `RESTIC_PASSWORD`.
2. Store `RESTIC_PASSWORD` **also outside the server** (password manager). Without it the
   off-site backups cannot be decrypted.
3. Runner download: `mkdir dist` and copy the checked wheel into it; set
   `ACCESSGUARD_RUNNER_WHEEL=/dist/<file name>`.
4. SFTP backup repository only: put the private key, `known_hosts` and an SSH `config` with
   the host alias, user and port into `.secrets/ssh/`, then
   `sudo chown -R root:root .secrets/ssh && sudo chmod 600 .secrets/ssh/*` (the restic
   container runs as root and `ssh` refuses group- or world-readable files).
5. Check the file; it prints names, never values, and must end with `0 errors`:

   ```bash
   python3 ops/deploy/check_env.py .env
   ```

   Stripe stays in test mode: the check refuses live keys unless `--allow-live-payments` is
   passed, which needs the owner's explicit approval.

## 3. DNS and e-mail records

- `A` (and `AAAA` if the server has IPv6) for `ACCESSGUARD_DOMAIN` to the server. Wait
  until it resolves before the first start, or the certificate request fails (Let's
  Encrypt rate-limits repeated failures).
- The SMTP provider's sender-domain records: SPF, DKIM and a DMARC policy (start with
  `p=none` and a report address, tighten later).

## 4. First start

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs proxy | grep -i certificate
```

Then:

1. `https://<domain>/readyz` returns 200 with `database: ok`; `http://` redirects to https.
2. Response headers include `Strict-Transport-Security` and the CSP.
3. `docker compose ... exec web python web/manage.py createsuperuser` (the admin console is
   at `ACCESSGUARD_ADMIN_PATH`).
4. `docker compose ... exec web python web/manage.py sendtestemail <your address>`: the
   e-mail arrives and passes SPF/DKIM (check the headers).
5. Sign up as a test customer, run the demo scan, download the runner from the Runners
   page and compare the SHA-256, request a password reset and use the link.
6. Sign-in throttling sees real visitor addresses: the app trusts exactly one proxy hop
   (`ACCESSGUARD_TRUSTED_PROXY_HOPS=1`); Caddy replaces any client-supplied
   `X-Forwarded-For`.

## 5. Stripe (test mode)

In the Stripe test environment the app uses: create a webhook endpoint
`https://<domain>/webhooks/stripe/` with the events from
`python ops/stripe_sandbox/sandbox.py events` (`ACCESSGUARD_STRIPE_SANDBOX=1`), put its
signing secret into `STRIPE_WEBHOOK_SECRET`, restart `web`, and buy a plan with card 4242.
Products, prices and the portal configuration: `docs/install.md` (Stripe section).

## 6. Backups

The `scheduler` writes a `pg_dump` to the `backups` volume every day (kept 14 days). The
`offsite-backup` service copies the volume to the restic repository after 02:00 UTC, keeps
14 daily, 8 weekly and 6 monthly snapshots, runs `restic check` on Sundays and then pings
`ACCESSGUARD_BACKUP_HEARTBEAT_URL`. It refuses to back up (and so skips the ping) when the
newest dump is older than 26 hours, which also catches a stalled scheduler.

Once, before the first start of `offsite-backup`, initialise the repository:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm \
  --entrypoint restic offsite-backup init
```

**Restore drill (before inviting customers, then quarterly).** Restore the newest
off-site snapshot into a separate database and compare it, as in docs/operations.md:

```bash
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$C run --rm --entrypoint restic -v "$PWD/restore:/restore" offsite-backup \
  restore latest --target /restore
ls restore/backups/                                   # pick the newest accessguard-*.dump
$C cp restore/backups/accessguard-<stamp>.dump scheduler:/tmp/drill.dump
$C exec db createdb -U accessguard accessguard_restore_drill
$C exec -e DATABASE_URL=postgres://accessguard:<password>@db:5432/accessguard_restore_drill \
  scheduler python web/manage.py restore_database /tmp/drill.dump
```

Record date, snapshot, duration and the comparison (row counts, sign-in on a copy) in
docs/operations.md. Delete `restore/` and the drill database afterwards. A real restore
replaces the live database instead (docs/operations.md, steps 1-5).

## 7. Monitoring

| Signal | Set up |
| --- | --- |
| Up and ready | An external uptime check on `https://<domain>/readyz` every 1-5 minutes |
| Backups | The heartbeat URL with a 26-hour period and a grace time; alert when a ping is missing |
| Errors | `SENTRY_DSN` (Sentry or self-hosted GlitchTip); without it, `ACCESSGUARD_ADMIN_EMAILS` get error e-mails |
| Certificate | Caddy renews automatically; the uptime check fails if it stops working |
| Disk | Container logs are rotated (5 × 10 MB per service); check `df -h` and the `backups` volume monthly |
| Scheduler | `docker compose ... logs scheduler offsite-backup | grep ERROR` |

## 8. Updates

1. `bash ops/verify.sh` and green CI on the release commit.
2. Copy the new release, keep `.env`, `dist/` and `.secrets/`.
3. Take a manual backup: `$C exec scheduler python web/manage.py backup_database --out /backups`.
4. `$C up -d --build`; check `/readyz`, sign in, run the demo scan.
5. Base images are pinned by digest (Dockerfile, both compose files, the CI PostgreSQL
   service). To update one, look up the new digest, change it everywhere it is used, let CI
   build the images, and note the change in docs/HANDOFF.md. Upgrading PostgreSQL to a new
   major version needs a matching `postgresql-client` in the web image first.

Rollback: redeploy the previous release; restore the backup from step 3 only if a migration
requires it.

## What was checked without a server (2026-11-02)

- `docker compose -f docker-compose.yml -f docker-compose.prod.yml config` (Compose 5.6.1,
  dummy `.env`): only the proxy publishes ports (80, 443, 443/udp); `web` has none; every
  service has log rotation; missing `ACCESSGUARD_DOMAIN`, `ACME_EMAIL`, `RESTIC_REPOSITORY`
  or `RESTIC_PASSWORD` stops the start with a message.
- `ops/docker/offsite_backup.sh`: `sh -n`, and one loop iteration per case with stubbed
  `restic` and `wget` (no dump, old dump, fresh dump, failing backup, no heartbeat URL).
  Since 2026-11-02 also `tests/unit/test_offsite_backup.py` (runs the script under `sh`
  with stubbed `date`, `find`, `restic`, `wget`, `sleep`): weekday, Sunday check passed,
  Sunday check failed (no heartbeat), failed backup, no recent dump, before 02:00 UTC
  (POSIX only: runs in the Ubuntu CI jobs, skipped on Windows).
  The real restic image and repository were not run.
- `ops/deploy/check_env.py`: unit tests (`tests/unit/test_deploy_env_check.py`).
- Client address behind one proxy and the password-reset flow and limits: web tests.
- Image digests were read from Docker Hub; the Python digest is the one built and run
  locally on 2026-10-30. The PostgreSQL 17.11 digest and the Caddy and restic images were
  not pulled or run on this machine (Docker is not started here because of its memory
  limits; see docs/operations.md).

Not checked: the Caddyfile (no Caddy binary here), certificate issuance, real SMTP
delivery, the restic repository, the restore drill from off-site storage, any server.
