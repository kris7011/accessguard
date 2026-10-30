# Threat model (milestones 1, 2 and V1)

Status: covers the implemented milestone 1 and 2 system (local CLI, local lab, adaptive agent,
evaluation fixtures). Must be revised before
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
3. **Engine ↔ model** (`application/agent_view.py` out, `application/agent_contract.py` in;
   adapters `ollama_client.py`, `scripted_model.py`, `ollama_planner.py`): everything sent is
   minimised, everything received is untrusted.
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
| T7 | Request flooding / runaway run / slow target holding the run | Hard request budget, interruptible pacing, run deadline and stop signal that also bound **in-flight** requests, total (not per-read) request timeout, concurrency 1, 1 MB response cap; Ctrl+C/SIGTERM stop the run with a partial report (exit 130) | `tests/unit/test_gate.py`, `tests/integration/test_time_limits.py`, `tests/e2e/test_cancellation.py`, `test_budget_and_stop_signal`, `test_oversized_response_is_rejected` |
| T8 | Credential disclosure in reports/console | Credentials live only in `Secret` (masked repr), never in results; reports scrubbed for known secrets and `Bearer`-style values before writing | `tests/security/test_secret_leakage.py`, `tests/unit/test_redaction.py` |
| T9 | Customer data disclosure in evidence | Evidence contains field *names*, status, size and body hash; never values | `test_report_contains_no_protected_values_or_credentials`, secret-leakage test |
| T10 | Credentials embedded in policy files | Strict schema rejects unknown keys such as `token` | `test_credentials_cannot_be_embedded` |
| T11 | Model expands scope, changes policy/expectations or drops checks (prompt injection) | Reorder planner: output must be an exact permutation. Agent: only `run_check` with a pending approved alias or `stop`; strict validation (size, JSON, nesting depth at most 32 on every platform, duplicate keys, extra/missing fields, unknown actions and ids, repeats); the engine executes the immutable check and decides the verdict; model adapters cannot import the HTTP target client or the runner | `tests/unit/test_agent_contract.py`, `tests/security/test_agent_boundaries.py::ScopeExpansionTests`, `test_model_adapters_cannot_send_target_requests`, `tests/unit/test_ollama_planner.py` |
| T19 | Prompt injection through customer input (identity, tenant, role, resource, category, collection ids, paths, field names, descriptions) or through target responses | The model sees only generated aliases, engine enumerations and integers; no policy string, response body or verdict reason is ever sent; model text is never echoed into later prompts or reports (fixed rejection categories) | `test_injection_in_policy_identifiers_never_reaches_the_model`, `test_target_response_content_never_reaches_the_model`, `test_state_contains_only_aliases_enumerations_and_numbers`, `test_rejections_never_echo_model_text_into_the_trace` |
| T20 | Credentials or protected data disclosed to a model provider | Same minimisation; `Secret` values are only revealed by the HTTP target client; the Ollama client only connects to 127.0.0.1 | `test_model_never_receives_credentials_values_paths_or_policy_strings`, `test_lab_run_sends_no_lab_secret_to_the_model`, `AgentOverOllamaContractTests` |
| T21 | Model-side denial of service: endless or huge output, slow or hanging model, endless invalid replies | Limits on model calls, invalid replies, output characters (checked before parsing) and output tokens; 256 KiB response cap; every model call bounded by the per-call timeout, the run deadline and the stop signal (also in flight) | `StopAndBudgetTests`, `DeadlineAndCancellationTests`, `test_slow_model_is_bounded_by_the_time_limit`, `test_in_flight_model_call_is_cancellable`, `test_oversized_reply_is_rejected` |
| T22 | Hidden model failure or silent fallback that misstates how results were produced | No fallback; explicit `agent.status` and `failure`; partial results kept, remaining checks SKIPPED, `completed: false`; CLI exit 5; scripted adapter is never labelled AI (`uses_ai: false`, "not an AI model") | `ModelFailureTests`, `test_model_failure_exits_5_with_a_partial_report`, `AgentReportTests` |
| T23 | Evaluation leakage: the agent sees the answer key or the active flaws, which inflates results | Runs receive only the policy, origin and credentials; answer keys are loaded once per fixture after its runs; flaws live only in the fixture server's target definition; the product wheel excludes evaluation code and markers | `test_agent_never_sees_answer_key_or_flaw_configuration`, `test_answer_key_is_loaded_only_after_the_runs`, `test_evaluation_strategies_never_load_the_answer_key`, `ops/ci/check_wheel.py` |
| T12 | False PASS (expired session, login page, rate limit, error, missing baseline) | Owner baseline required; session probe before the run and after every 401; strict outcome taxonomy; overall green only if every check passes | `tests/unit/test_evaluation.py`, `tests/integration/test_edge_cases.py` |
| T17 | False PASS when a denial or error response still carries protected data (found in the pre-M2 hardening: e21c8ff reported such a lab as `checks_passed`) | Protected content is matched for **every** status code; a complete match is FAIL and a partial match SUSPECTED before the status is interpreted; only a denial without protected content is PASS | `tests/unit/test_evaluation_status_matrix.py`, `leaky-denial` edge case, `denial-body-leak` lab profile |
| T18 | Verification that reports success although a check failed (e.g. `cmd \| tail` without `pipefail`) | `ops/verify.sh` / `ops/verify.ps1` take exit codes from the commands, aggregate failures and exit with the first failing code; CI uses `shell: bash` (`-eo pipefail`) and the same scripts | `tests/e2e/test_verify_scripts.py` (negative control) |
| T13 | Flaky single observation reported as fact | Every FAIL is retested; non-reproduced → SUSPECTED | `test_failure_that_does_not_reproduce_is_downgraded`, flaky edge case |
| T14 | Lab flaws or fixed tokens shipped as product features | Lab is a separate package; random tokens per start; CI checks the wheel; static test forbids lab imports in product | `tests/security/test_architecture.py`, `ops/ci/check_wheel.py` |
| T15 | Malicious/huge policy or response (DoS of the tool) | Policy ≤ 1 MiB with item caps; response ≤ 1 MB; JSON recursion errors handled | `test_malformed_and_oversized_input` |
| T16 | Supply-chain compromise of tooling | Runtime has zero third-party dependencies; dev tools hash-locked; pip-audit; actions pinned to SHAs; least-privilege CI token | CI workflow (not yet run on GitHub) |

## Residual risks and open items

- **Not yet applicable but required before M3:** documented authorization (owner, targets,
  time window, permitted methods), HTTPS with certificate validation plus pinning, private-range
  policy for authorized internal targets, credential storage with encryption and rotation,
  per-customer isolation, audit log, evidence retention and deletion.
- Request time limits are enforced with 50 ms granularity during reads; TCP connect and request
  sending are bounded by the remaining time but are not cancellable mid-call.
- Leak detection matches exact JSON string values only; protected data in HTML, plain-text,
  invalid-JSON or otherwise encoded bodies is not detected. Such responses are INCONCLUSIVE, also
  for a denial status, so they cannot produce a false PASS, but the leak is not reported as a
  finding either (ADR 0002, second amendment). Only an empty body or parsed JSON can confirm a
  denial.
- Response bodies are held in memory (≤ 1 MB each) for the duration of a check.
- Report files are written unencrypted to a local directory chosen by the user.
- The lab binds to `127.0.0.1` only, but any local process can reach it while it runs.
- A real model has not been run. The agent's guarantees do not depend on model behaviour, but
  real-model latency, invalid-reply rates and prompt-size limits (context window) are unmeasured.
- The Ollama endpoint is assumed to be a trustworthy local process; AccessGuard does not
  authenticate it, and any local process listening on the configured port could answer.
  Its replies are still treated as untrusted and validated.
- The agent state grows linearly with the number of checks; very large policies may exceed a
  model's context window (a provider would then truncate or fail; failures are reported).
- The secret scrubber is defense in depth; the primary control is that results never contain
  credentials. Unknown credential formats that are not `Bearer/Basic/Token` values are only
  scrubbed if they are known secrets.

## V1: control plane, runner, billing and assistants

New assets: customer accounts and organisations, project configurations (target URLs, test
user ids, record paths), reports and findings, runner tokens, Stripe customer ids, assistant
usage records. New trust boundaries: browser → web app; runner → runner API; Stripe →
webhook; web app → AI provider; customer-supplied OpenAPI documents and pasted code.

| # | Threat | Mitigation | Verified by |
| --- | --- | --- | --- |
| T24 | One customer reads or changes another customer's projects, scans, reports, findings or runners | Every lookup goes through `tenancy.get_owned` filtered by the current membership's organisation; other tenants' ids answer 404; organisation switch only to own memberships | `web/.../tests/test_tenancy_and_roles.py::IsolationTests` |
| T25 | Privilege escalation inside an organisation | Roles viewer < admin < owner; `membership_required(role)` on every mutating view; last owner cannot be removed | `RoleTests` |
| T26 | Stolen or guessed runner token claims jobs or uploads fake reports | 256-bit random tokens, stored as SHA-256, shown once, revocable; a runner sees only scans of its organisation (or demo scans for the platform runner) and only scans it claimed; reports must match the approved check list, spec hash and finding list | `test_runner_api_and_findings.py`, `CrossOrgRunnerTests` |
| T27 | Compromised control plane redirects scans to other hosts (SSRF through customers' runners) | Runner-side origin allowlist with network zone, spec hash check, policy paths must lie under allowed prefixes; control plane itself never connects to targets | `tests/integration/test_runner_control_plane.py`, `test_sampleapp_integration.py` |
| T28 | Credentials or response data reach our servers | Credentials only in the runner's local config; reports contain names, statuses, sizes and hashes only; runner scrubs known secrets before upload | `test_claims_scans_and_uploads_a_scrubbed_report`, `test_sign_up_configure_scan_fix_retest` |
| T29 | Partial, cancelled or lost scans, invalid sessions or retest subsets shown as "secure" | One banner rule (`scans/presenters.py`): green only for a completed, complete, full-matrix pass; lost runners → no result | `test_presentation.py::BannerTests`, cancellation e2e test |
| T30 | Forged, replayed or reordered payment webhooks grant plans | Signature verified by the Stripe SDK; event ids unique; older subscription events ignored; plans only from webhooks | `test_billing.py::WebhookTests` |
| T31 | Credential stuffing against sign-in | Failure counter per IP and per account (10 per 15 min); password validators (min 12) | `LoginThrottleTests` |
| T32 | XSS, clickjacking, CSRF | Auto-escaping templates; CSP `default-src 'self'` without inline script/style (static test); `X-Frame-Options: DENY`; CSRF on all forms; runner API and webhook accept no cookies | `HeaderTests`, `test_authentication_is_required_and_cookies_are_not_enough` |
| T33 | Prompt injection through OpenAPI text, finding text or pasted code | Inputs wrapped as untrusted data; outputs schema-validated; setup answers can only reference extracted paths/fields; explanations are labelled drafts and change nothing | `test_assistant.py` |
| T34 | Customer data sent to an AI provider without consent | Third-party providers require the organisation's opt-in (off by default); quotas; prompts not stored | `test_consent_is_required_for_third_party_providers` |
| T35 | Customer data reused for ML without consent or with identifiers | Export only for consenting organisations; features are engine enumerations and counts; HMAC application groups | `test_learning_export.py` |
| T36 | Secrets in logs or error reports | JSON log formatter with a redaction filter (bearer, runner, Stripe, Anthropic keys, `password=`); Sentry with PII off and scrubbing | `LoggingTests` |
| T37 | Data loss | Daily backups with integrity check, restore refuses to overwrite without `--replace`, retention never deletes open findings | `test_backup.py`, restore drill in docs/operations.md |

Residual risks (V1): login throttling is per installation; no e-mail verification of new
accounts; a customer's own runner can upload false verdicts for its own organisation; the
PostgreSQL path, Docker images and real Stripe and AI-provider calls are not verified on this
machine; legal texts (terms, privacy, DPA) are not written.
