# AccessGuard 0.2 (milestone 1)

AccessGuard tests whether one customer of a multi-tenant API can read another customer's data.
You declare the intended access policy explicitly; the engine derives every check from it, sends
real read-only HTTP requests, compares responses with the legitimate owner's baseline, retests
failures, and writes a machine-readable report with coverage.

> Working name; not a registered or researched trademark. Research prototype, not a finished
> security product. A passing report covers only the declared policy and checks: it is **not** a
> guarantee of security.

**Milestone 1 scope:** the engine only runs against the isolated local laboratory
(`127.0.0.1`, synthetic data). Scanning external systems is deliberately not possible.

## Quick start (Windows PowerShell)

Requires Python 3.11 or newer. The engine has no third-party runtime dependencies, so the demo
and the tests run without installing anything:

```powershell
cd C:\AccessGuard
$env:PYTHONPATH = "src;lab"
py -3 -m accessguard_lab demo
Start-Process .\reports\vulnerable\report.html
py -3 -m unittest discover -s tests -t .
```

On Linux/macOS use `export PYTHONPATH=src:lab` and `python3`.

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
.\.venv\Scripts\python -m pip install --no-deps --no-build-isolation -e . -e .\lab
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
| `accessguard-lab scan --profile P [--policy lab\|v0.1-parity] [--no-retest]` | Scan one lab profile | 0 all checks passed, 1 findings, 3 incomplete or needs review, 130 cancelled |
| `accessguard-lab profiles` | List lab profiles and their flaws | 0 |
| `accessguard validate-policy FILE` | Validate a policy file (no network) | 0 valid, 4 invalid |
| `accessguard plan FILE [--json]` | Print the deterministic check matrix (no network) | 0, 4 |

Run limits for `demo` and `scan`: `--max-requests N` (budget), `--min-interval SECONDS` (rate
limit), `--max-duration SECONDS` (hard wall-clock limit that also aborts in-flight requests).
Ctrl+C (or SIGTERM / Ctrl+Break) stops a run in a controlled way: completed results are kept,
unfinished checks are marked SKIPPED, a partial report is written, and the exit code is 130.

Without an editable install, use `py -3 -m accessguard_lab ...` and `py -3 -m accessguard ...` with
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
  SKIPPED, ERROR. Redirects, 429, 5xx, login pages, soft errors and expired sessions are never PASS.
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
`"uses_ai": false` in every report. An optional Ollama planner (`--ollama-model NAME`) may only
reorder the approved checks; its output is schema-validated and must be an exact permutation.
Errors are reported (exit 5) and never replaced by a silent fallback.

The Ollama adapter is tested with mocked responses only. **No real model has been run** in this
milestone (Ollama was not installed on the development machine). There is no ML training, no
adaptive agent, no customer scanning and no automatic code repair. The agent loop is milestone 2.

## Verification status

| Item | Status |
| --- | --- |
| 160 unit, integration, security and e2e tests | Passed locally on Windows 11 (10.0.26300): CPython 3.13.13 (venv) and 3.12.7 (no install), via both `ops/verify.sh` (Git Bash) and `ops/verify.ps1` (Windows PowerShell 5.1) |
| Ruff lint/format, mypy `--strict`, pip-audit, detect-secrets, wheel build and content check | Passed locally on CPython 3.13.13 |
| GitHub Actions workflow (Ubuntu 3.11/3.13, Windows 3.13) | Written, **not yet run** (repository not pushed) |
| Python 3.11, Linux, macOS | **Not verified** (the local 3.11 install is broken; no Linux/macOS host) |
| Real Ollama model | **Not verified** |

## Documentation

- [Project walkthrough](docs/PROJECT_WALKTHROUGH.md): architecture, flows, testing, limitations
- [Milestone 1 report](docs/milestones/M1.md): acceptance criteria and evidence
- [Threat model](docs/threat-model.md)
- [Architecture decision records](docs/adr/)
- [Report schema](docs/report-schema.md)
- [Product requirements (Danish)](AccessGuard-Claude-Code.md)

## Limitations

See [known limitations](docs/PROJECT_WALKTHROUGH.md#known-limitations). The most important ones:
results on the synthetic lab say nothing about detection rates on real systems; only exact JSON
string matches count as evidence (data leaked in an HTML error page is not detected); listings are not paginated; write operations, side channels
and undeclared endpoints are not tested. Competitive advantage and willingness to pay have not been
validated.
