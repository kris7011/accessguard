# Threat model (milestone 1)

Status: covers the implemented milestone 1 system (local CLI + local lab). Must be revised before
any external target, hosted service or real credentials are supported (milestones 3–4).

## System and assets

AccessGuard sends authenticated requests on behalf of test identities and judges the responses.
The tool therefore legitimately holds sensitive capabilities.

| Asset | Why it matters |
| --- | --- |
| Test identity credentials | Grant real access to the target; misuse equals account takeover |
| Response content (protected data) | Customer data; disclosure in reports or logs is itself a leak |
| Scope (authorized origin) | Requests outside it are unauthorized scanning / SSRF |
| Verdicts and reports | A false "pass" creates false assurance; a false "fail" wastes effort |
| Policy file | Defines what is tested; tampering silently changes conclusions |

## Trust boundaries

1. **Policy file → engine** (`adapters/policy_json.py`): untrusted input.
2. **Engine → target HTTP API** (`adapters/http_target.py`): outbound network; responses are
   untrusted data.
3. **Engine ↔ optional model** (`adapters/ollama_planner.py`): model output is untrusted.
4. **Engine → report files** (`adapters/report_files.py`): output that may be shared.
5. **Product ↔ lab**: deliberately vulnerable code must never ship with the product.

## Threats and mitigations

| # | Threat | Mitigation in M1 | Verified by |
| --- | --- | --- | --- |
| T1 | SSRF: requests to metadata endpoints or internal hosts | Scope is one `http` origin; every resolved address must be loopback, IPv4-mapped IPv6 normalized; checked before connecting | `tests/security/test_scope_enforcement.py`, `tests/unit/test_scope.py` |
| T2 | DNS rebinding | Host is re-resolved for every request, **all** addresses validated, connection pinned to the validated IP with the original `Host` header | `test_dns_rebinding_is_caught_on_every_request`, `test_connection_is_pinned_to_the_validated_address` |
| T3 | Redirect out of scope (e.g. to `169.254.169.254`) | `http.client` never follows redirects; 3xx is INCONCLUSIVE | `test_redirect_to_metadata_endpoint_is_not_followed`, `test_redirects_are_never_followed` |
| T4 | Proxy environment variables redirecting traffic | `http.client` does not read proxy variables | `test_proxy_environment_is_ignored` |
| T5 | Path injection changing host or request line | Paths validated against an RFC 3986 allowlist; no `//`, dot segments, query, CR/LF | `test_unsafe_paths_never_reach_the_network` |
| T6 | Destructive or state-changing tests | Only GET exists in the client API; no write checks | `test_only_get_is_ever_sent` |
| T7 | Request flooding / runaway run | Hard request budget, pacing, deadline, stop signal, concurrency 1, timeout, 1 MB response cap | `tests/unit/test_gate.py`, `test_budget_and_stop_signal`, `test_oversized_response_is_rejected` |
| T8 | Credential disclosure in reports/console | Credentials live only in `Secret` (masked repr), never in results; reports scrubbed for known secrets and `Bearer`-style values before writing | `tests/security/test_secret_leakage.py`, `tests/unit/test_redaction.py` |
| T9 | Customer data disclosure in evidence | Evidence contains field *names*, status, size and body hash; never values | `test_report_contains_no_protected_values_or_credentials`, secret-leakage test |
| T10 | Credentials embedded in policy files | Strict schema rejects unknown keys such as `token` | `test_credentials_cannot_be_embedded` |
| T11 | Model expands scope or drops checks (prompt injection) | Model only reorders; output must be an exact permutation of approved check ids; it never sees credentials or paths and cannot issue requests | `tests/unit/test_ollama_planner.py`, `test_model_planner_cannot_send_target_requests` |
| T12 | False PASS (expired session, login page, rate limit, error, missing baseline) | Owner baseline required; session probe before the run and after every 401; strict outcome taxonomy; overall green only if every check passes | `tests/unit/test_evaluation.py`, `tests/integration/test_edge_cases.py` |
| T13 | Flaky single observation reported as fact | Every FAIL is retested; non-reproduced → SUSPECTED | `test_failure_that_does_not_reproduce_is_downgraded`, flaky edge case |
| T14 | Lab flaws or fixed tokens shipped as product features | Lab is a separate package; random tokens per start; CI checks the wheel; static test forbids lab imports in product | `tests/security/test_architecture.py`, `ops/ci/check_wheel.py` |
| T15 | Malicious/huge policy or response (DoS of the tool) | Policy ≤ 1 MiB with item caps; response ≤ 1 MB; JSON recursion errors handled | `test_malformed_and_oversized_input` |
| T16 | Supply-chain compromise of tooling | Runtime has zero third-party dependencies; dev tools hash-locked; pip-audit; actions pinned to SHAs; least-privilege CI token | CI workflow (not yet run on GitHub) |

## Residual risks and open items

- **Not yet applicable but required before M3:** documented authorization (owner, targets,
  time window, permitted methods), HTTPS with certificate validation plus pinning, private-range
  policy for authorized internal targets, credential storage with encryption and rotation,
  per-customer isolation, audit log, evidence retention and deletion.
- The per-request timeout applies per socket operation; a server that drips bytes slowly can
  exceed it in total until the run deadline is reached.
- Response bodies are held in memory (≤ 1 MB each) for the duration of a check.
- Report files are written unencrypted to a local directory chosen by the user.
- The lab binds to `127.0.0.1` only, but any local process can reach it while it runs.
- The secret scrubber is defense in depth; the primary control is that results never contain
  credentials. Unknown credential formats that are not `Bearer/Basic/Token` values are only
  scrubbed if they are known secrets.
