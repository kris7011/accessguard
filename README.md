# AccessGuard 0.3 (milestone 2)

AccessGuard tests whether one customer of a multi-tenant API can read another customer's data.
You declare the intended access policy explicitly; the engine derives every check from it, sends
real read-only HTTP requests, compares responses with the legitimate owner's baseline, retests
failures, and writes a machine-readable report with coverage.

> Working name; not a registered or researched trademark. Research prototype, not a finished
> security product. A passing report covers only the declared policy and checks: it is **not** a
> guarantee of security.

**Scope:** the engine only runs against the isolated local laboratory and evaluation fixtures
(`127.0.0.1`, synthetic data). Scanning external systems is deliberately not possible.

**Milestone 2 status: implementation with mock and contract verification.** A bounded adaptive
agent can choose the next approved check from earlier results. It is verified with a scripted
test adapter and a fake Ollama server. A real local model passed a bounded 3-call smoke test
and the full opt-in real-model test (56/56 checks, exact match with the lab's answer key, with
the runtime's prompt cache disabled). Bounded comparisons against simple strategies (all four
synthetic fixtures, 25 % and 50 % budgets) found **no demonstrated advantage of AI
prioritisation**: on `projects` a simple scripted "listings first" rule matches the model
(a post-hoc check), and on `helpdesk` and `clinic` the model was worse than the best simple
baseline in three of four cells, in each of three server sessions
([status](docs/milestones/M2.md), [evaluation](docs/evaluation.md)).

## Quick start (Windows PowerShell)

Requires Python 3.11 or newer. The engine has no third-party runtime dependencies, so the demo
and the tests run without installing anything:

```powershell
cd C:\AccessGuard
$env:PYTHONPATH = "src;lab;evaluations"
py -3 -m accessguard_lab demo
Start-Process .\reports\vulnerable\report.html
py -3 -m unittest discover -s tests -t .
```

On Linux/macOS use `export PYTHONPATH=src:lab:evaluations` and `python3`.

Expected demo output (verified, see "Verification status"):

```text
vulnerable  findings       PASS 33, FAIL 23
fixed       checks_passed  PASS 56
ground truth: exact match
```

Exit code 0 from `demo` means the before/after demo behaved as expected (exactly the seeded
flaws were found and the fixed lab passed), **not** that any system is secure.

## Development setup (locked tooling)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python -m pip install --no-deps --no-build-isolation -e . -e .\lab -e .\evaluations
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m mypy
.\.venv\Scripts\python -m unittest discover -s tests -t .
.\.venv\Scripts\python -m pip_audit --strict --require-hashes -r requirements-dev.txt
```

After the editable install, the console scripts `accessguard` and `accessguard-lab` are available
in the virtual environment.

### One-command verification

Both scripts run the same steps (lint, format, types, tests, audit, secrets, build, demo), keep
each step's log, never hide a failing exit code behind a pipe, and exit with the first failing
step's original exit code:

```powershell
powershell -ExecutionPolicy Bypass -File ops\verify.ps1 -Python .\.venv\Scripts\python.exe
powershell -ExecutionPolicy Bypass -File ops\verify.ps1 -Steps tests,demo   # no tools needed
```

```bash
PYTHON=.venv/Scripts/python bash ops/verify.sh          # Git Bash / Linux
bash ops/verify.sh selftest-fail; echo $?               # negative control: prints 3
```

## Commands

| Command | Purpose | Exit codes |
| --- | --- | --- |
| `accessguard-lab demo [--out DIR]` | Scan the vulnerable and the fixed lab, compare with ground truth | 0 expected result, 1 mismatch, 5 planner error, 130 cancelled |
| `accessguard-lab scan --profile P [--policy lab\|v0.1-parity] [--no-retest] [--order deterministic\|random --seed N]` | Scan one lab profile (policy order or seeded random order, no AI) | 0 all checks passed, 1 findings, 3 incomplete or needs review, 130 cancelled |
| `accessguard-lab agent --profile P --model scripted:follow-up\|scripted:deny-first\|scripted:list-first\|scripted:in-order\|ollama:NAME` | Adaptive agent selects one approved check at a time (`scripted:*` = test adapter, not AI) | as `scan`, plus 2 invalid configuration, 5 model failure (partial report written) |
| `accessguard-eval run [--model ...] [--fixtures ...] [--seeds ...] [--fractions ...] [--agent-repeats N] [--max-run-seconds S]` | Compare deterministic, seeded random and agent selection under equal HTTP budgets | 0 done, 1 fixture/answer-key mismatch, 2 usage, 5 an agent run failed |
| `accessguard-lab profiles` | List lab profiles and their flaws | 0 |
| `accessguard validate-policy FILE` | Validate a policy file (no network) | 0 valid, 4 invalid |
| `accessguard plan FILE [--json]` | Print the deterministic check matrix (no network) | 0, 4 |

Run limits for `demo`, `scan` and `agent`: `--max-requests N` (budget, also counting session
probes, owner baselines and retests), `--min-interval SECONDS` (rate limit), `--max-duration
SECONDS` (hard wall-clock limit that also aborts in-flight requests and model calls). Agent
limits: `--max-model-calls`, `--max-invalid-replies`, `--model-timeout SECONDS`,
`--ollama-port`.
Ctrl+C (or SIGTERM / Ctrl+Break) stops a run in a controlled way: completed results are kept,
unfinished checks are marked SKIPPED, a partial report is written, and the exit code is 130.

Without an editable install, use `py -3 -m accessguard_lab ...`, `py -3 -m accessguard_eval ...` and `py -3 -m accessguard ...` with
`PYTHONPATH` set as above. Only a complete pass exits with 0: findings, inconclusive results,
skipped checks and technical errors never do.

## What milestone 1 does

- **Explicit policy** ([lab.json](lab/accessguard_lab/policies/lab.json)): identities (tenant,
  role), resources with protected fields and grants, collections, and a session probe. Cross-tenant
  sharing is an explicit grant, so the shared resource acts as a negative control.
- **Deterministic check matrix:** every identity plus the anonymous actor against every resource
  and listing (56 checks for the lab policy).
- **Evidence-based verdicts:** the owner's access is confirmed first; a leak requires the protected
  values in the response, not just HTTP 200. Leaked values count with every status code: a 403
  whose body still contains the data is a FAIL, not a PASS. Outcomes: PASS, FAIL, SUSPECTED, INCONCLUSIVE,
  SKIPPED, ERROR. Redirects, 429, 5xx, login pages, soft errors and expired sessions are never PASS, and neither is a denial whose body cannot be analysed (non-JSON or invalid JSON).
- **Separate retest** of every failure; a failure that does not reproduce becomes SUSPECTED.
- **Safety limits:** loopback-only scope, DNS re-validation and pinning, GET only, no redirects,
  no proxies, response size limit, total request timeout, request budget, pacing, run deadline and
  stop signal (the last two also abort in-flight requests).
- **Reports:** versioned JSON ([schema](docs/report-schema.md)) and an accessible Danish HTML page.
  Credentials and protected values are never written; reports are scrubbed before writing.
- **Lab:** three tenants, two roles each, an expired session, documents, admin-only invoices, one
  explicit share, a listing endpoint, six switchable flaws with hand-written ground truth, and
  edge-case routes for ambiguous responses. The lab is a separate package and is not part of the
  product wheel.

## AI status (important)

The default run is a deterministic baseline **without AI or ML** and is labelled
`"uses_ai": false` in every report.

**Adaptive agent (milestone 2, [ADR 0007](docs/adr/0007-bounded-adaptive-agent.md)).**
`accessguard-lab agent` lets a model choose the next check, one at a time:

- **Allowed actions:** the model may only reply `run_check` with the id of a pending,
  approved check, or `stop`.
- **Validation:** every reply is validated strictly. Repeats, unknown ids, extra fields,
  oversized output and invalid JSON are rejected and counted.
- **Execution and verdicts:** the existing engine executes the check and decides the verdict.
  The model cannot change scope, endpoints, credentials, policy, expectations or verdicts.
- **What the model sees:** opaque aliases and engine enumerations only. It never sees
  credentials, protected values, response bodies, paths or any policy string.
- **Limits:** model calls, invalid replies, output size, the HTTP budget (including probes,
  baselines and retests) and the run deadline and stop signal (also during model calls).
- **Failures:** a model failure ends the run visibly (`run.agent.status`, exit 5). Completed
  results are kept, and every check that was not executed is SKIPPED. There is no fallback.

The `scripted:*` models are a **scripted test adapter, not an AI model**. Their reports say
`"uses_ai": false` and "not an AI model". The `ollama:NAME` adapter talks to a local Ollama
server on 127.0.0.1. It is contract-tested against a fake server and has run against a real
local model in a 3-call smoke test and one passed full opt-in test (2026-10-27).
A real model has been compared with the baselines only in bounded evaluations (all four
fixtures, 25 % and 50 % budgets). It had 0 false positives everywhere and took 12-87 s per run
instead of milliseconds. Its gain per request on `projects` is matched by a simple scripted
"listings first" rule, and on `helpdesk` and `clinic` it confirmed fewer findings than the best
simple baseline. The evaluation with the scripted adapter showed no advantage over seeded
random order ([evaluation](docs/evaluation.md)). There is no ML training, no customer scanning and
no automatic code repair.

The M1 option `--ollama-model NAME` on `demo`/`scan` (reorder-only planner, exit 5 on error)
is unchanged.

## Verification status

| Item | Status |
| --- | --- |
| 266 unit, integration, security and e2e tests (1 opt-in real-model test skipped) | Passed locally on Windows 11 (10.0.26300): CPython 3.13.13 (venv) via `ops/verify.sh` and `ops/verify.ps1` (Windows PowerShell 5.1), and CPython 3.12.7 without installation |
| Ruff lint/format, mypy `--strict`, pip-audit, detect-secrets, wheel build and content check, demo | Passed locally on CPython 3.13.13 |
| Agent loop, limits, failure handling, data minimisation | Verified with the scripted test adapter and mocked model clients |
| Ollama adapter | Contract-tested against a local fake Ollama HTTP server |
| GitHub Actions workflow (Ubuntu 3.11/3.13, Windows 3.13) | Written, **not yet run** (repository not pushed) |
| Python 3.11, Linux, macOS | **Not verified** |
| Real Ollama model | **Not verified** (opt-in test `tests/e2e/test_real_model.py` is skipped) |

## Documentation

- [Project walkthrough](docs/PROJECT_WALKTHROUGH.md): architecture, flows, testing, limitations
- [Milestone 1 report](docs/milestones/M1.md) and [milestone 2 report](docs/milestones/M2.md):
  acceptance criteria and evidence
- [Evaluation of selection strategies](docs/evaluation.md)
- [Handoff status](docs/HANDOFF.md) for resuming interrupted work
- [Threat model](docs/threat-model.md)
- [Architecture decision records](docs/adr/)
- [Report schema](docs/report-schema.md)
- [Product requirements (Danish)](AccessGuard-Claude-Code.md)

## Limitations

See [known limitations](docs/PROJECT_WALKTHROUGH.md#12-known-limitations). The most important ones:
results on the synthetic lab say nothing about detection rates on real systems; only exact JSON
string matches count as evidence (data leaked in an HTML or plain-text error page is not reported as
a finding; such responses are INCONCLUSIVE, never PASS); listings are not paginated; write operations, side channels
and undeclared endpoints are not tested. The agent has been compared with a real model only in
bounded runs on four synthetic fixtures, with one 4B model, and showed no advantage over simple
rules. Competitive advantage and willingness to
pay have not been validated.
