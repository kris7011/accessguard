# ADR 0004: Outbound request guard (loopback scope, IP pinning, GET only, run limits)

- Status: accepted (milestone 1)
- Date: 2026-10-26

## Problem

The engine sends authenticated requests. A wrong URL, a redirect, a DNS change or a model
suggestion must not turn it into an SSRF tool or a load generator. Requirements §6 require
protection against SSRF, DNS rebinding and out-of-scope redirects, read-only defaults, and
timeouts, budgets, rate limits and cancellation.

## Options

1. `urllib.request` with a custom redirect handler (prototype): follows proxy environment
   variables by default, and resolves DNS inside the library, so the checked address and the
   connected address can differ.
2. A third-party client (e.g. httpx) with custom transports: capable, but adds a runtime
   dependency for a small amount of code.
3. `http.client` connecting to an address the scope has just validated.

## Decision

Option 3, split into two layers:

- `adapters/http_target.py` (`ScopedHttpClient`): validates the path, re-resolves the host on
  every request, requires **every** resolved address to be loopback (milestone 1), connects to
  the first validated address and sends the original `Host` header. Only `get()` exists.
- `application/gate.py` (`RequestGate`): hard request budget, minimum interval, run deadline and
  a stop signal (`threading.Event`), checked before every request. Concurrency is 1.

The transport never follows redirects, ignores proxy variables, enforces a timeout and a 1 MB
response limit. Scope violations are raised before any network I/O.

## Consequences

- Only plain `http` on loopback is supported in milestone 1.
- Retries are not automatic; a FAIL retest is a deliberate, separately reported read.
- The CLI does not yet wire Ctrl+C to the stop signal; the engine API supports cancellation.

## Revisit when

External targets are authorized (milestone 3): add HTTPS with certificate validation and SNI
while pinning, an explicit allowlist of authorized origins and private ranges, per-target rate
limits, and authorization metadata (owner, window, methods) checked before every run.
