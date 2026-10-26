# AccessGuard project walkthrough

Status as of milestone 2 (version 0.3.0): implementation with mock and contract verification;
no real language model has been run. Every statement is marked by status where it matters:
**implemented** (code exists), **tested** (an automated test exists and was run),
**proposed/future** (not built).

## 1. System overview

AccessGuard checks whether a multi-tenant API enforces its *declared* read-access policy: can a
user of customer A read customer B's data, can a member read an admin-only resource, does a
listing leak other tenants' items, and is legitimately shared data still reachable?

Milestone 1 delivered the engine as a local CLI plus a deliberately vulnerable local lab.
Milestone 2 adds a bounded adaptive agent that chooses the next approved check from earlier
results (**implemented, tested with a scripted adapter and a fake Ollama server; not run with a
real model**), and an evaluation harness with separate fixtures. There is no hosted service, UI,
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
   - `CheckRunner.finish` retests failures (except after a run limit);
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
- The adaptive agent has **not been run with a real model**. Its guarantees are verified with
  a scripted adapter, mocks and a fake Ollama server. The scripted evaluation shows no
  advantage over the deterministic baseline on 4 small synthetic fixtures; those results
  describe the mechanics, not AI quality.
- The agent makes one model call per check and resends the whole aliased state each turn;
  very large policies may exceed a model's context window. Aliases hide semantics (such as
  resource categories) that a real model might use.
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

Interview preparation: `docs/interview-prep/M1.md`, `docs/interview-prep/M2.md`.
