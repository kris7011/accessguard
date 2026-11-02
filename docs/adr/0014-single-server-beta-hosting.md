# ADR 0014: Beta hosting on one server with the compose stack

- Status: deferred (proposed 2026-11-02; the same day the commercial launch was paused, so no
  hosting decision is pending; nothing ordered or deployed)
- Date: 2026-11-02

## Problem

The self-serve beta needs a public, TLS-protected control plane with PostgreSQL, password
reset e-mail, off-site backups and alerts, at the lowest cost that is still safe, without
tying the product to one provider. The only operating environment verified so far is the
compose stack on the development machine.

## Options

1. **One VPS (Hetzner Cloud CX23, EU) running the existing compose stack plus a production
   overlay: Caddy for TLS, PostgreSQL in compose, restic backups to a storage box in another
   location.** About €10 a month.
2. A smaller VPS for the app and a managed PostgreSQL database (Scaleway DB-DEV-S, EU), with
   object storage for backups. About €25 a month.
3. A platform-as-a-service (container platform plus managed database). Not priced: more
   provider-specific configuration than the beta needs.

## Decision (proposed)

Option 1. It runs exactly the stack that was verified locally, including the backup and
restore drill, and it is the cheapest. The production overlay (`docker-compose.prod.yml`)
adds what a public server needs and nothing provider-specific: any Linux host with Docker
can run it, and the data moves as a `pg_dump` and a restic repository.

## Consequences

- We operate PostgreSQL: daily dumps, encrypted off-site copies with retention, a heartbeat
  alert when a copy is missing, and a restore drill before launch and quarterly.
- One server is a single point of failure; restore time depends on the drill being kept up.
- The off-site copy is with the same provider in another location; losing the provider
  account is not covered until a second copy elsewhere is added.
- The app trusts exactly one proxy hop for client addresses
  (`ACCESSGUARD_TRUSTED_PROXY_HOPS=1`), which the overlay's Caddy guarantees.

## When to revisit

Customer data or uptime expectations that need a managed database or a second server;
someone other than the owner operating the service; a provider availability problem when
ordering (Hetzner's plan pages showed the plans as unavailable on 2026-11-02).

Details and prices: docs/beta-operations-plan.md. Runbook: docs/deployment.md.
