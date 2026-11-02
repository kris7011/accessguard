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

## Amendment 2026-10-26: limits also bound in-flight requests

Problem found in the pre-M2 hardening: the deadline and the stop signal were only checked before
a request, and `http.client` timeouts apply per socket operation, so a slow-drip response or a
slow server could exceed both.

Options: (1) a watchdog thread shutting the socket down: tried first, it did not interrupt a
blocked `recv` on Windows; (2) running requests in killable subprocesses: heavy; (3) reading in
short timeout slices that re-check the limits.

Decision: option 3. The transport uses a socket subclass with 50 ms timeout slices; before each
read it checks the request's total deadline (`min(request timeout, remaining run time)`) and the
stop event, and raises a timeout when either is reached. The gate hands both to the client as a
`RequestLimit`. The runner turns an abort caused by the run deadline or the stop signal into an
interruption (SKIPPED, completed results kept) and a plain request timeout into ERROR. The lab
CLI maps SIGINT, SIGTERM and Ctrl+Break to the stop event, writes the partial report and exits
with 130. The previous consequence "the CLI does not yet wire Ctrl+C" no longer applies.

Remaining limits: 50 ms granularity; TCP connect and sending are bounded by time but not
cancellable mid-call.

## Revisit when

External targets are authorized (milestone 3): add HTTPS with certificate validation and SNI
while pinning, an explicit allowlist of authorized origins and private ranges, per-target rate
limits, and authorization metadata (owner, window, methods) checked before every run.
