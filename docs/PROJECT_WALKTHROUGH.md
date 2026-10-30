# AccessGuard project walkthrough

Status as of V1 (2026-10-30, branch `feature/v1-commercial-beta`): a self-serve product with a
web control plane, a customer-side runner, a separate integration app, billing in Stripe test
mode, two bounded AI assistants and an ML foundation without an active model. Labels used
below: **implemented** (code exists), **tested** (an automated test exists and was run),
**not verified** (written but not exercised, with the reason).

Part A describes the V1 product. Part B (sections 1-12, from milestones 1 and 2) describes
the engine, the lab and the agent evaluation in detail; it remains accurate where V1 did not
change the engine (V1 engine changes are listed in A.3).

## A.1 System overview

```text
                 browser (forms, no SPA)                  Stripe (Checkout, Portal, webhooks)
                        │ https                                     │ signed webhooks
                        ▼                                           ▼
   ┌─────────────── web control plane (Django 6.1, web/accessguard_web) ───────────────┐
   │ accounts/ tenancy · projects/ config+forms+OpenAPI · scans/ jobs, findings,       │
   │ runner API, guidance, presenters · billing/ plans, entitlements, webhooks ·       │
   │ assistant/ providers, tasks · pages/ landing, help, health, backup                │
   └──────────────┬───────────────────────────────────────────────▲────────────────────┘
                  │ PostgreSQL (SQLite locally)                   │ https: claim, heartbeat,
                  ▼                                               │ report, fail (bearer token)
                                                         customer runner (src/accessguard,
                                                         stdlib only): local allowlist +
                                                         credentials → engine → target API
```

The control plane never connects to customer systems; only the runner does (ADR 0009).

## A.2 Structure and technologies

| Path | Responsibility | Status |
| --- | --- | --- |
| `src/accessguard/` | Engine and runner CLI (`accessguard scan`, `accessguard runner`); Python ≥ 3.11, no third-party dependencies | implemented, tested |
| `web/accessguard_web/` | Control plane: Django 6.1, server-rendered templates, one small script, whitenoise, gunicorn, psycopg, stripe, anthropic (optional), sentry-sdk (optional) | implemented, tested on SQLite and (locally, in compose) PostgreSQL 17 |
| `sampleapp/accessguard_sampleapp/` | Synthetic projects API with its own auth, flawed and fixed variants, answer key | implemented, tested |
| `ml/accessguard_ml/` | Datasets, training, evaluation, registry | implemented, tested |
| `ops/` | verify scripts, CI helpers, dev stack, browser check, docker entry scripts, economics model | implemented; Docker images built and run locally |

Dependencies are hash-locked (`web/requirements.txt`, `requirements-dev.txt`,
`requirements-e2e.txt`), audited with pip-audit; mypy strict with django-stubs.

## A.3 Engine changes in V1

- `domain/scope.py`: `NetworkZone` (loopback / private / public), always-blocked link-local,
  metadata, multicast, reserved and unspecified addresses; `TargetScope.authorized` for http/https
  with path prefixes; the lab rule `from_origin` is unchanged. *Tested:* `test_scope_zones.py`.
- `adapters/http_transport.py` + `http_target.py`: TLS with certificate and host-name
  verification while the connection stays pinned to the validated IP; `AuthScheme` (Bearer,
  header, cookie). *Tested with a real local TLS server:* `test_https_and_auth.py`.
- `domain/policy.py`: optional identity `subject`, dotted field paths (`data.email`).
- `application/scan.py`: `select_checks` (retests run a subset; the report records it);
  `application/runner.py`: `Progress` snapshots for live status.
- `adapters/scan_spec.py`, `adapters/runner_config.py`, `adapters/control_plane.py`,
  `entrypoints/runner_service.py`: the runner.
- `application/ranking.py`: `features-v1` and an (inactive) learned ranker planner.

## A.4 Authentication and authorization

- **People:** Django auth with e-mail login (`accounts/models.py: User`), password validators
  (min 12 characters), throttled sign-in (`ThrottledLoginView`), password reset by e-mail.
- **Tenancy:** every product record has an `organization`; `accounts/tenancy.py` resolves the
  current membership per request and `get_owned` loads records only within it (other tenants'
  ids answer 404). Roles: viewer (read), admin (configure, approve, triage, runners), owner
  (billing, members, deletion). *Tested:* `test_tenancy_and_roles.py`.
- **Runners:** bearer tokens (`agr_…`, 256-bit, stored as SHA-256, shown once, revocable);
  the platform demo runner may claim demo scans only (`scans/services.py: claim_next`).
- **Stripe:** webhook signatures verified with the SDK; no session or CSRF involved.

## A.5 Persistence

Models: `accounts` (User, Organization, Membership, Invitation, AuditEvent), `projects`
(Project with the scan-spec draft as JSON and imported endpoint templates), `scans` (Runner,
Scan with frozen spec, hash, approval, progress, report; Finding with fingerprint
`check_id|type`; FindingEvent as history and labels), `billing` (Subscription, StripeEvent),
`assistant` (AssistantRun). Migrations in each app's `migrations/` (checked in CI by
`makemigrations --check`). Backups: `manage.py backup_database` / `restore_database`.

## A.6 Core workflows

1. **Sign-up** (`accounts/views.py: signup` → `create_organization`): user, organisation,
   owner membership, Free subscription and the demo project, in one transaction.
2. **Configure** (`projects/views.py`): target, test users, records and lists, limits. Forms
   validate with the engine's domain types; `projects/config.py: evaluate` validates the whole
   document with the engine's `parse_scan_spec` and computes the budget that guarantees full
   coverage. Optional OpenAPI import (`projects/openapi.py`) and setup assistant.
3. **Approve** (`scans/views.py: review` → `services.approve_scan`): scope summary,
   attestation, plan limits, one active scan per project; the spec is frozen with its SHA-256.
4. **Run** (`entrypoints/runner_service.py`): claim → refuse untrusted origins or hash
   mismatch → scan with local credentials → heartbeats every 3 s (progress, cancel flag) →
   upload the scrubbed report. Cancellation stops the scan and keeps a partial report.
5. **Ingest** (`services.accept_report` → `validate_report` → `apply_report`): the report must
   match the approved checks; FAIL/SUSPECTED create or update findings (a resolved one that
   reappears is reopened); a PASS of the same check resolves a finding as verified.
6. **Act** (`scans/views.py`): report page (`presenters.py` decides the banner: green only for
   a complete full pass), finding page with evidence, reproduction, rule-based guidance
   (`guidance.py`), optional AI explanation, triage (confirm / reject with reason / resolve
   unverified), retest (`create_retest` adds the legitimate-access checks of the same records).
7. **Pay** (`billing/`): Checkout and Portal redirects; webhooks update the subscription;
   `entitlements.py` turns status into limits with a grace period for failed payments.

## A.7 Testing

| Suite | Command | Result (latest local run) |
| --- | --- | --- |
| Engine, runner, sample app, ML (unittest) | `bash ops/verify.sh tests` | 351 run, 2 skipped (opt-in real-model tests) |
| Web (Django) incl. two end-to-end journeys over real HTTP with a real runner, and browser-faithful setup-form tests | `bash ops/verify.sh web-tests` | 78 run, 0 failed (SQLite); the same 78 also passed on PostgreSQL 17.11 in the compose stack (2026-10-30) |
| Deployment checks with production settings | `bash ops/verify.sh deploy-check` | passed |
| Browser (Chrome via Playwright), desktop + phone | `ops/dev/browser_check.py` | 0 problems |
| Customer acceptance: runner installed from the wheel / source archive in a clean venv, forms driven over HTTP, scan → triage → fix → retest | `ops/dev/customer_acceptance.py` | PASS (CPython 3.12.7 wheel, 3.13.13 sdist) |
| Compose stack: build, migrate, readiness, static files, demo runner, scheduler backup, restore into a separate database | manual run, recorded in [install.md](install.md) and [operations.md](operations.md) | passed locally; not on a server |

Mocks are used only at external boundaries: Stripe Checkout/Portal calls, the Anthropic SDK
call, and fake assistant providers. No real Stripe payment and no real AI call exists yet.

The customer acceptance run found a real defect the end-to-end test had missed: the
"Endpoints and rules" page could not be saved in a browser unless its empty extra record row
was filled, because the owner `<select>` had no blank option and browsers submit the first
option. The e2e test posted hand-built rows without the empty one. Fixed in `054abc8` with
tests that build the POST data from the rendered page (`test_project_setup.py`).

## A.8 Local development, deployment, backup

See [install.md](install.md), [operations.md](operations.md) and
[launch-checklist.md](launch-checklist.md).

## A.9 Design decisions

ADR 0009 (local runner), 0010 (Django), 0011 (Stripe), 0012 (assistants), 0013 (ML). The
central trade-offs: credentials stay with the customer at the price of an installed runner;
server-rendered pages keep the UI simple and testable at the price of rich interactivity;
deterministic verdicts and full coverage over AI prioritisation, because the evaluation found
no advantage.

## A.10 Known limitations (V1)

- Not deployed to a server. Docker and PostgreSQL ran locally only; GitHub Actions, Stripe
  and AI providers have not been exercised for real.
- No e-mail verification; login throttling per installation; English UI only.
- Assistant quality is unmeasured (no eval set); no real model call made for V1 tasks.
- Legal texts are not written; VAT handling not decided.
- The engine's limits (GET only, declared endpoints, exact JSON matching) apply unchanged.

# Part B: engine, lab and agent (milestones 1 and 2)

## 1. System overview

AccessGuard checks whether a multi-tenant API enforces its *declared* read-access policy: can a
user of customer A read customer B's data, can a member read an admin-only resource, does a
listing leak other tenants' items, and is legitimately shared data still reachable?

Milestone 1 delivered the engine as a local CLI plus a deliberately vulnerable local lab.
Milestone 2 adds a bounded adaptive agent that chooses the next approved check from earlier
results (**implemented, tested with a scripted adapter and a fake Ollama server; with a real
model the 3-call smoke test and one full opt-in test run passed; not evaluated against the
baselines**), and an evaluation harness with separate fixtures. There is no hosted service, UI,
database or ML (all future).

```text
policy.json ──► domain.checks.generate_checks ──► Planner (deterministic | Ollama reorder-only)
                                                        │
                                                        ▼
lab / target ◄── ScopedHttpClient ◄── RequestGate ◄── CheckRunner ──► RunOutcome
 (127.0.0.1)     (scope, pinning)     (budget, stop)   (probe, baseline,      │
                                                        verdict, retest)      ▼
                                                     reporting.build_report ──► report_files
                                                                               (JSON + HTML)
```

## 2. Architecture and structure

Layered modular monolith; dependencies point inwards (ADR 0001), enforced by
`tests/security/test_architecture.py`.

| Path | Responsibility |
| --- | --- |
| `src/accessguard/domain/` | `policy.py` (identities, grants, resources, invariants), `checks.py` (matrix generation, plan validation), `evaluation.py` (baselines and verdicts), `results.py` (results, overall status, coverage), `scope.py` (origin and destination rules, path validation), `redaction.py`, `credentials.py` (`Secret`), `errors.py` |
| `src/accessguard/application/` | `ports.py` (`TargetClient`, `Planner`, `ModelClient`), `planning.py` (`DeterministicPlanner`, `SeededRandomPlanner`), `gate.py` (`RequestGate`, `RunLimits`, `call_limit`), `runner.py` (`CheckRunner`: `run`, `preflight`, `execute`, `finish`), `agent_view.py` (`ModelView`: aliases, minimised state), `agent_contract.py` (`parse_action`, tool contract, prompt), `agent.py` (`AgentLoop`, `AgentLimits`, `AgentStatus`), `reporting.py` (`build_report`), `scan.py` (`run_scan`, `run_agent_scan`, `AgentStrategy`) |
| `src/accessguard/adapters/` | `http_transport.py` (`send`, `decode_observation`), `http_target.py` (`ScopedHttpClient`), `policy_json.py`, `ollama_client.py` (`OllamaModelClient`), `ollama_planner.py` (M1 reorder-only), `scripted_model.py` (`ScriptedModelClient`, not AI), `report_files.py` |
| `src/accessguard/entrypoints/` | `cli.py` (`validate-policy`, `plan`), `agent_options.py` (`--model` and agent limits), `exit_codes.py`, `interrupt.py` (signals → stop event, worker thread) |
| `evaluations/accessguard_eval/` | Evaluation harness (`harness.py`), data-driven fixture server (`fixture_server.py`), fixture loader with a separate answer-key loader (`fixtures.py`), CLI (`cli.py`), fixtures with hand-written answer keys |
| `lab/accessguard_lab/` | Vulnerable synthetic API (`server.py`), data (`dataset.py`), profiles, hand-written `ground_truth.py`, lab composition root (`scan.py`), lab CLI (`cli.py`), policies |
| `tests/{unit,integration,security,e2e}/` | 266 tests (1 opt-in real-model test skipped by default) |
| `ops/verify.sh`, `ops/verify.ps1` | One verification entry point per shell; preserve exit codes |
| `ops/ci/` | `check_wheel.py` (no lab or evaluation code in the wheel), `build_check.py`, `secret_scan.py` |
| `docs/` | This walkthrough, threat model, ADRs, report schema, milestone report |

## 3. Technologies

Python ≥ 3.11 with the standard library only at runtime (`http.client`, `http.server`, `json`,
`ipaddress`, `socket`, `secrets`). Development: Ruff, mypy, pip-tools (lock generation),
pip-audit, detect-secrets, build, setuptools; GitHub Actions. Rationale in ADR 0001 and 0005.

## 4. Authentication and authorization

- **Of AccessGuard itself:** none yet. It is a local CLI run by the developer; there are no users,
  tenants or API of AccessGuard (future, milestone 4).
- **Towards the target (implemented, tested):** each identity's credential is a `Secret` supplied
  at run time (by the lab in milestone 1, never in the policy file). It is sent as
  `Authorization: Bearer …` by `ScopedHttpClient.get`. A session probe (`/me` in the lab) must echo
  the identity id before the run and after any 401; otherwise that identity's checks are
  INCONCLUSIVE.
- **Authorization being tested:** the declared grants in the policy. `AccessPolicy.is_allowed`
  is the single source of the expected outcome.

## 5. Validation and security controls

- Policy input: strict JSON schema with unknown-key rejection, size and count caps, identifier and
  path validation (`adapters/policy_json.py`, `domain/policy.py`). Tested.
- Outbound requests: loopback-only scope, DNS re-validation and IP pinning, no redirects or
  proxies, GET only, 1 MB response cap, total request timeout, budget, interruptible pacing, and a
  run deadline and stop signal that also abort in-flight requests (ADR 0004). Tested.
- Reports: no credentials or protected values; final scrub of known secrets and auth-scheme values.
  Tested.
- Model output: reorder planner, schema-validated permutation only (ADR 0006). Agent: strict
  tool contract (only `run_check` with a pending approved alias, or `stop`), limits on calls,
  invalid replies and output size, every model call bounded by the run deadline and stop
  signal (ADR 0007). Tested with a scripted adapter, mocks and a fake Ollama server.
- Data sent to a model: opaque aliases, engine enumerations and integers only; no
  credentials, protected values, response bodies, verdict reasons, paths or policy strings.
  Tested.
- Full list with test references: `docs/threat-model.md`.

## 6. Persistence

No database. Inputs are policy JSON files (versioned in git). Outputs are report files in a
directory chosen with `--out` (default `reports/`, git-ignored). Each report records the policy's
SHA-256 so results can be tied to the exact policy bytes.

## 7. Core workflow (request and data flow)

1. `accessguard_lab.cli.main` → `_demo`/`_scan` loads the policy (`scan.load_lab_policy` →
   `policy_json.load_policy_bytes` → `parse_policy` → `AccessPolicy`).
2. `scan_lab` starts the lab (`server.run_lab`: random tokens, ephemeral port), builds
   `TargetScope.from_origin` and `ScopedHttpClient`, and calls `application.scan.run_scan`.
3. `run_scan` generates the check matrix (`generate_checks`), asks the planner for an order
   (deterministic, or Ollama with `order_checks` validation), creates a `RequestGate` and a
   `CheckRunner`.
4. `CheckRunner.run`:
   1. probes every identity's session (`_probe_session`);
   2. for each check: skips with INCONCLUSIVE if the actor's session is invalid; fetches and caches
      the owner baseline (`_baseline` → `establish_baseline`); sends the actor's request through
      the gate (`_send`, which passes the gate's `RequestLimit` to the client); on 401 re-probes
      the session; evaluates with `evaluate_item` or `evaluate_list`, which match protected
      content for every status code before interpreting the status, and accept a denial as PASS
      only when the body was read completely (`BodyFormat` `json` or `empty`, classified by
      `http_transport.decode_observation`); records minimal `Evidence`;
   3. retests every FAIL (`_verify_failures`); converts non-reproduced failures to SUSPECTED;
   4. on budget/deadline/stop marks the in-flight check and all remaining checks SKIPPED and
      keeps completed results; the CLI maps Ctrl+C/SIGTERM/Ctrl+Break to the stop event and exits
      with 130 after writing the partial report.
5. `build_report` computes coverage and overall status and builds findings with rule, actor,
   subject, expected/actual, evidence, reproduction steps, scope, time, versions, verification.
6. `write_report` scrubs secrets and writes `report.json` and the Danish `report.html`.
7. The demo compares results with `ground_truth.py` and writes `demo-summary.json`.

### 7a. Agent workflow (milestone 2; implemented, tested with a scripted adapter)

1. `accessguard_lab.cli._agent` builds an `AgentStrategy` (`entrypoints/agent_options.py`:
   `scripted:*` → `ScriptedModelClient`, `ollama:NAME` → `OllamaModelClient`) and calls
   `scan_lab`, which calls `application.scan.run_agent_scan`.
2. `AgentLoop.run` asks the model for provider metadata (`identify`, recorded only as reported),
   then runs `CheckRunner.preflight` (session probes).
3. On each turn:
   1. `ModelView.state` renders the aliased state.
   2. `ModelClient.complete` is called with `RequestGate.call_limit` (per-call timeout,
      remaining run time and the stop signal).
   3. `parse_action` validates the reply. A rejection is counted, and its category is shown
      in the next state. `stop` ends the loop. `run_check` maps the alias back to the approved
      check and calls `CheckRunner.execute`, which judges it exactly as in a baseline run.
4. The loop ends when every check ran, or on stop, a model failure, too many invalid replies,
   the model call budget, the HTTP budget, the deadline or the stop signal. Then:
   - checks that were not executed become SKIPPED;
   - `CheckRunner.finish` retests failures (from a per-finding reserve even after the HTTP
     budget is spent; still skipped after a deadline or cancellation);
   - `build_report` adds `run.agent` (schema 1.2).
5. `accessguard_eval` runs the same engine with the deterministic, seeded random and agent
   strategies under equal budgets on the fixtures. It scores the reports against the answer
   keys afterwards (ADR 0008, `docs/evaluation.md`).

## 8. Testing

| Suite | What it covers |
| --- | --- |
| `tests/unit` | Policy invariants, JSON parsing, check generation and plan validation, every verdict branch, overall status and coverage, gate limits, scope rules, redaction, runner behaviour with a scripted client, Ollama contract (mocked), report fields and HTML escaping, lab request handling |
| `tests/integration` | Real HTTP against the lab: every profile against ground truth, prototype parity, edge cases (soft error, login page, redirect, 429, 5xx, partial, flaky, timeout, missing resource, expired session, budget, stop, 403 bodies in HTML, plain text, invalid JSON, misleading Content-Type and empty), HTTP client behaviour against a real socket server |
| `tests/security` | SSRF/DNS rebinding/path injection, credential and data leakage into reports and console, architecture rules |
| `tests/e2e` | CLI subprocess runs: demo, exit codes, policy validation, agent runs (scripted, budget cut, model failure via a fake Ollama), seeded random order, evaluation CLI; opt-in real-model test (`ACCESSGUARD_REAL_MODEL`) |
| Agent and model | `tests/unit/test_agent_contract.py`, `test_agent_loop.py`, `test_scripted_model_and_planning.py`; `tests/security/test_agent_boundaries.py` (data minimisation, prompt injection via policy strings and responses, scope-expansion attempts); `tests/integration/test_ollama_contract.py` (fake Ollama HTTP server), `test_agent_lab.py` (verdict equivalence with the baseline over real HTTP), `test_evaluation.py` |

Run: `py -3 -m unittest discover -s tests -t .` (with `PYTHONPATH=src;lab` or an editable install),
or all checks with `ops/verify.ps1` / `ops/verify.sh`. Manual mutation checks (five injected bugs
in M1 and five in the pre-M2 hardening) were all detected by the suite; they are not automated.

## 9. Local development and deployment

See README. There is no deployment: AccessGuard runs locally. CI is defined in
`.github/workflows/test.yml` (not yet run on GitHub).

## 10. Backup and restore

Nothing stateful exists beyond files in the repository and generated reports. Back up the
repository (git) and any report directories you want to keep. A database backup/restore plan is
required when persistence is introduced (future).

## 11. Design decisions

ADR 0001 (stdlib layered monolith), 0002 (explicit policy and verdict taxonomy), 0003 (lab
separated from product), 0004 (outbound request guard), 0005 (tooling and CI), 0006 (reorder-only
model planner), 0007 (bounded adaptive agent), 0008 (selection-strategy evaluation). Notable trade-offs:

- Exact JSON string matching: few false positives, misses transformed leaks. Bodies the engine
  cannot analyse are INCONCLUSIVE rather than scanned, which keeps the matching semantics simple
  at the cost of conclusive coverage on targets with HTML error pages.
- Owner baseline cached per run: fewer requests, but a resource that changes mid-run is not
  re-baselined.
- Over-restriction (granted access denied) is a FAIL, so a "fix" that blocks everything is visible.

## 12. Known limitations

- Synthetic lab only; results are not evidence of real-world detection rates.
- Plain HTTP on loopback only; no HTTPS, no external targets (milestone 3).
- Bearer tokens only; session probe endpoint required.
- Evidence requires exact string values (≥ 4 characters) in JSON responses; substrings, HTML,
  encodings and non-JSON responses are not matched. A response whose body cannot be analysed
  (HTML, plain text, invalid JSON, a Content-Type that is not JSON) is INCONCLUSIVE, never PASS;
  a denial counts as PASS only with an empty body or parsed JSON without protected values.
- Listings: no pagination or query parameters; a granted item missing from a listing gives
  INCONCLUSIVE.
- Read-only: write operations, privilege escalation via writes, side channels and undeclared
  endpoints are not tested.
- Time limits are enforced with 50 ms granularity during reads; TCP connect and request sending
  are bounded by time but not cancellable mid-call.
- Leaked data is only recognized as exact JSON string values; a leak inside an HTML or plain-text
  error page is not reported as a finding. Such a check is INCONCLUSIVE (the run is `incomplete`),
  so the leak is not hidden behind a PASS, but it must be investigated manually. Content sniffing
  and raw-byte matching are not implemented.
- The Ollama planner only reorders; with every check executed, the order does not affect results.
  Verified with mocks only.
- The adaptive agent has been compared with a real model only in **bounded** evaluations
  (2026-10-27; `docs/evaluation.md`), at 25 % and 50 % budgets:
  - `projects`: it confirmed 2 and 5 findings at 12 and 23 requests, against at most a median
    of 1 and 2 for the first simple strategies. A scripted `list-first` rule, added afterwards
    as a post-hoc control, matches it exactly, so the gain is explained by "listings first".
  - `helpdesk` and `clinic` (pre-registered, three separate server sessions with identical
    choices): no demonstrated advantage. It confirmed 0, 1, 0 and 0 findings, against best
    simple baselines of 1, 5, 0 and 2.
  - It had 0 false positives everywhere and took 12-87 s per run against hundredths of a
    second. Its guarantees are verified with a scripted adapter, mocks and a fake
  Ollama server. Against a real local model
  (`qwen3:4b-instruct-2507-q4_K_M`, Ollama 0.40.11), the 3-call smoke test passed. The first
  full-test attempt was stopped after 12 valid choices, because the runtime's host prompt cache
  exhausted the commit memory on this 8 GB machine. With the cache disabled
  (`LLAMA_ARG_CACHE_RAM=0` in the server's environment), the full test passed: 56 valid
  choices, 56/56 checks, and an exact match with the answer key. One run says nothing about
  prioritisation quality or repeatability (`docs/milestones/M2.md`). The scripted evaluation shows no
  advantage over the deterministic baseline on 4 small synthetic fixtures; those results
  describe the mechanics, not AI quality.
- The agent makes one model call per check and resends the whole aliased state each turn;
  very large policies may exceed a model's context window. Aliases hide semantics (such as
  resource categories) that a real model might use.
- The agent's state has structural gaps (no names are needed to close them):
  - list checks are not linked to tenants or categories;
  - roles carry no privilege information;
  - aliases follow policy order.

  A structural non-AI heuristic (`scripted:deny-first`) beats the policy order slightly, but
  not seeded random, on the fixtures. A second one, `scripted:list-first` (every list check
  first, then policy order), was added as a control for the real model's behaviour. See `docs/evaluation.md` → "Information basis of the
  agent".
- Retests run in a separate phase after the selected checks, but the runner holds back one
  request per finding so they are confirmed within the same HTTP budget (2026-10-27 reserve;
  ADR 0008). A finding that still cannot be retested (the finding spent the last request, or a
  deadline or cancellation intervened) stays `not_retested`. The evaluation reports
  `confirmed_true_positives` separately.
- The dev lock file was resolved on Windows/CPython 3.13 without environment markers.
- CI has not been run on GitHub; Python 3.11, Linux and macOS are not verified locally.

## 13. Ownership and AI assistance

Milestone 1 code, tests, configuration and documentation were written by an AI coding assistant
(Claude Code) following the owner's requirements document (`AccessGuard-Claude-Code.md`). The
owner defined the product goals, constraints and milestones. The assistant verified the results by
running the commands listed in `docs/milestones/M1.md`. Decisions the owner should review and
explicitly accept or change: the outcome taxonomy, the changed v0.1 behaviour for denied granted
access and the leak-precedence rule (ADR 0002), exit code 130 for cancelled runs (ADR 0004), unittest over pytest (ADR 0005), and the lab packaging (ADR 0003).

Milestone 2 was written the same way: the code, tests, fixtures, answer keys, configuration and
documentation are by Claude Code, and the owner specified the requirements. The assistant
verified the results by running the commands listed in `docs/milestones/M2.md`, including a
mutation check of the new guards. Decisions the owner should review and explicitly accept or
change:

- opaque aliases instead of policy names in prompts (ADR 0007);
- ending the run at the N-th invalid reply;
- running retests after a model-side stop;
- `uses_ai` describing the configured adapter, even when the model fails;
- exit code 5 for agent model failures;
- the scripted `follow-up` heuristic;
- the fixture design and the budget fractions (ADR 0008).

The real-model readiness review (same day, second session) was also implemented by Claude
Code. It covered the stricter opt-in tests, agent repetitions, one shared run deadline,
confirmed findings, `unknown` provenance and the `deny-first` reference. It also included the
measurement of the development machine and the model recommendation. Decisions the owner
should review:

- whether to install Ollama and `qwen3:4b-instruct-2507-q4_K_M`;
- keeping the model state at `agent-v1` until a real model has been measured.

The retest budget reserve (2026-10-27, third session) was implemented by Claude Code: the
runner holds back one request per finding so budget-limited runs confirm their findings, with
the same policy for every strategy and the model input unchanged (ADR 0008 amendment). The
owner should review the trade-off that confirming a finding slightly lowers coverage at tight
budgets.

The `scripted:list-first` control and the bounded `helpdesk`/`clinic` evaluation (2026-10-27,
eighth session) were implemented and run by Claude Code at the owner's request; the owner set
the design (fixtures, budgets, strategies, three server sessions), and the decision rule was
committed before the model runs. The owner should review the conclusion that the measured
advantage on `projects` is explained by a simple rule.

Interview preparation: `docs/interview-prep/M1.md`, `docs/interview-prep/M2.md`.
