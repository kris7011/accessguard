# ADR 0009: A local runner pulls approved jobs; the control plane never contacts targets

- Status: accepted (V1)
- Date: 2026-10-29

## Problem

Customers must connect their own test environment with little setup. Scanning needs test
credentials and reads responses that can contain customer data. A hosted service that holds
those credentials and connects to arbitrary hosts is a credential vault and an SSRF platform
at the same time, and internal staging systems are often not reachable from the internet.

## Options

1. **Hosted scanner**: customers upload credentials; our servers scan their targets.
2. **Agent with inbound connection**: a customer-side component that our servers call.
3. **Local runner, pull model**: a customer-side runner polls the control plane over HTTPS,
   claims approved jobs, scans locally and uploads a scrubbed report.

## Decision

Option 3.

- The runner is the existing engine (zero third-party dependencies): `accessguard runner
  --config runner.toml` (`entrypoints/runner_service.py`). The same code runs offline as
  `accessguard scan --spec ... --config ...` for CI.
- **Two-sided authorisation.** A person approves the scope in the web app (attestation,
  frozen spec, SHA-256 of the spec); the runner refuses any origin that is not in its local
  `[[targets]]` allowlist, in the network zone given there, and any spec whose hash does not
  match (`check_spec_against_trust`). A compromised control plane can therefore not redirect
  scans to new hosts.
- **Credentials stay local** (`adapters/runner_config.py`: file or environment variables).
  Reports contain field names, status codes, sizes and hashes, never response values, and
  are scrubbed for the local secrets before upload.
- **Runner API** (`web/accessguard_web/scans/runner_api.py`): bearer token (stored as a
  SHA-256 hash), claim with compare-and-set, heartbeats carry progress and return the cancel
  flag, the report must contain exactly the approved checks, failures are categorised.
  A silent runner's scan is marked lost after a lease (`sweep_lost_scans`), never passed.
- The demo uses the same path: a platform demo runner that trusts only the demo API.

## Consequences

- Customers install one Python package and write a short TOML file; no inbound firewall rule.
- Our servers make no outbound requests to customer systems (no SSRF surface there).
- Scans need the customer's runner to be online; a queued scan says so after a minute.
- A malicious customer runner can upload a false report for its own organisation only; the
  report is validated against the approved checks but its verdicts are taken as given.

## Revisit when

Customers ask for fully hosted scanning of public staging URLs, or for scheduled scans
without a long-running runner (then consider a hosted runner pool with per-tenant
isolation and a separate credential vault, reviewed as a new threat model).
