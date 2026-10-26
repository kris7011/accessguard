# AccessGuard project walkthrough

Status as of milestone 1 (version 0.2.0). Every statement is marked by status where it matters:
**implemented** (code exists), **tested** (an automated test exists and was run),
**proposed/future** (not built).

## 1. System overview

AccessGuard checks whether a multi-tenant API enforces its *declared* read-access policy: can a
user of customer A read customer B's data, can a member read an admin-only resource, does a
listing leak other tenants' items, and is legitimately shared data still reachable?

Milestone 1 delivers the engine as a local CLI plus a deliberately vulnerable local lab. There is
no hosted service, UI, database, agent or ML (all future).

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
| `src/accessguard/application/` | `ports.py` (`TargetClient`, `Planner`), `planning.py` (`DeterministicPlanner`), `gate.py` (`RequestGate`, `RunLimits`), `runner.py` (`CheckRunner`), `reporting.py` (`build_report`), `scan.py` (`run_scan` use case) |
| `src/accessguard/adapters/` | `http_transport.py` (`send`, `decode_observation`), `http_target.py` (`ScopedHttpClient`), `policy_json.py`, `ollama_planner.py`, `report_files.py` |
| `src/accessguard/entrypoints/` | `cli.py` (`validate-policy`, `plan`), `exit_codes.py` |
| `lab/accessguard_lab/` | Vulnerable synthetic API (`server.py`), data (`dataset.py`), profiles, hand-written `ground_truth.py`, lab composition root (`scan.py`), lab CLI (`cli.py`), policies |
| `tests/{unit,integration,security,e2e}/` | 137 tests |
| `ops/ci/check_wheel.py` | Verifies the product wheel contains no lab code |
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
  proxies, GET only, 1 MB response cap, timeout, budget, pacing, deadline, stop signal
  (ADR 0004). Tested.
- Reports: no credentials or protected values; final scrub of known secrets and auth-scheme values.
  Tested.
- Model output: schema-validated permutation only (ADR 0006). Tested with mocks.
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
      the gate (`_send`); on 401 re-probes the session; evaluates with `evaluate_item` or
      `evaluate_list`; records minimal `Evidence`;
   3. retests every FAIL (`_verify_failures`); converts non-reproduced failures to SUSPECTED;
   4. on budget/deadline/stop marks remaining checks SKIPPED.
5. `build_report` computes coverage and overall status and builds findings with rule, actor,
   subject, expected/actual, evidence, reproduction steps, scope, time, versions, verification.
6. `write_report` scrubs secrets and writes `report.json` and the Danish `report.html`.
7. The demo compares results with `ground_truth.py` and writes `demo-summary.json`.

## 8. Testing

| Suite | What it covers |
| --- | --- |
| `tests/unit` | Policy invariants, JSON parsing, check generation and plan validation, every verdict branch, overall status and coverage, gate limits, scope rules, redaction, runner behaviour with a scripted client, Ollama contract (mocked), report fields and HTML escaping, lab request handling |
| `tests/integration` | Real HTTP against the lab: every profile against ground truth, prototype parity, edge cases (soft error, login page, redirect, 429, 5xx, partial, flaky, timeout, missing resource, expired session, budget, stop), HTTP client behaviour against a real socket server |
| `tests/security` | SSRF/DNS rebinding/path injection, credential and data leakage into reports and console, architecture rules |
| `tests/e2e` | CLI subprocess runs: demo, exit codes, policy validation |

Run: `py -3 -m unittest discover -s tests -t .` (with `PYTHONPATH=src;lab` or an editable install).
A manual mutation check (five injected bugs) was detected by the suite; it is not automated.

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
model planner). Notable trade-offs:

- Exact JSON string matching: few false positives, misses transformed leaks.
- Owner baseline cached per run: fewer requests, but a resource that changes mid-run is not
  re-baselined.
- Over-restriction (granted access denied) is a FAIL, so a "fix" that blocks everything is visible.

## 12. Known limitations

- Synthetic lab only; results are not evidence of real-world detection rates.
- Plain HTTP on loopback only; no HTTPS, no external targets (milestone 3).
- Bearer tokens only; session probe endpoint required.
- Evidence requires exact string values (≥ 4 characters) in JSON responses; substrings, HTML,
  encodings and non-JSON responses are not matched.
- Listings: no pagination or query parameters; a granted item missing from a listing gives
  INCONCLUSIVE.
- Read-only: write operations, privilege escalation via writes, side channels and undeclared
  endpoints are not tested.
- Timeout is per socket operation, not per whole request.
- CLI Ctrl+C aborts without a partial report; cancellation exists in the engine API.
- The Ollama planner only reorders; with every check executed, the order does not affect results.
  Verified with mocks only.
- The dev lock file was resolved on Windows/CPython 3.13 without environment markers.
- CI has not been run on GitHub; Python 3.11 is not verified locally.

## 13. Ownership and AI assistance

Milestone 1 code, tests, configuration and documentation were written by an AI coding assistant
(Claude Code) following the owner's requirements document (`AccessGuard-Claude-Code.md`). The
owner defined the product goals, constraints and milestones. The assistant verified the results by
running the commands listed in `docs/milestones/M1.md`. Decisions the owner should review and
explicitly accept or change: the outcome taxonomy and the changed v0.1 behaviour for denied granted
access (ADR 0002), unittest over pytest (ADR 0005), and the lab packaging (ADR 0003).

Interview preparation for this milestone: `docs/interview-prep/M1.md`.
