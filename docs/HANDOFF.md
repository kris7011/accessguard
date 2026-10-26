# Handoff status

Purpose: lets work resume after an interrupted session. Never put credentials or secrets here.

## Current state (2026-10-26)

- Branch: `feature/m2-adaptive-agent`, created from `ab8bdb0` (end of M1 on
  `feature/m1-reproducible-foundation`). Not pushed, not merged.
- Milestone 2 is committed locally as **"M2 implementation with mock and contract
  verification"** (see `git log -1` for the commit id). No real language model has been run, so
  M2's real-model verification is **open**.

## Done

| Area | Files |
| --- | --- |
| Model port (`ModelClient`, `ModelRequest`, `ModelReply`, `ModelDescriptor`), `ModelError` | `src/accessguard/application/ports.py`, `src/accessguard/domain/errors.py` |
| Gate: `call_limit` for model calls, agent interruption reasons | `src/accessguard/application/gate.py` |
| Runner split into `preflight`/`execute`/`finish`; `requests_used_after` | `src/accessguard/application/runner.py` |
| Seeded random planner | `src/accessguard/application/planning.py` |
| Agent: model view (opaque aliases), tool contract (strict validation), loop with limits | `src/accessguard/application/agent_view.py`, `agent_contract.py`, `agent.py` |
| Agent use case, report schema 1.2 | `src/accessguard/application/scan.py`, `reporting.py`, `adapters/report_files.py` |
| Ollama model client; the reorder planner reuses it; scripted test adapter (not AI) | `src/accessguard/adapters/ollama_client.py`, `ollama_planner.py`, `scripted_model.py` |
| CLI: `accessguard-lab agent`, `scan --order random --seed N`, `accessguard-eval` | `lab/accessguard_lab/cli.py`, `src/accessguard/entrypoints/agent_options.py`, `evaluations/accessguard_eval/cli.py` |
| Evaluation: 4 fixtures, hand-written answer keys, harness | `evaluations/accessguard_eval/` |
| Tests (unit, integration incl. fake Ollama server, security, e2e, opt-in real model) | `tests/**/test_agent_*.py`, `test_ollama_contract.py`, `test_evaluation.py`, `test_scripted_model_and_planning.py`, `test_real_model.py` |
| Docs | ADR 0007, 0008 (and an update to 0006), `docs/milestones/M2.md`, `docs/evaluation.md`, threat model T11 and T19-T23, report schema 1.2, walkthrough, README, `docs/interview-prep/M2.md` |

## Not done

- Real-model verification: `tests/e2e/test_real_model.py` (skipped without
  `ACCESSGUARD_REAL_MODEL`) and `accessguard_eval run --model ollama:<name>`. Needs an installed
  Ollama model, which this milestone was not allowed to install.
- GitHub Actions not run (nothing pushed); Python 3.11, Linux and macOS not verified.

## Commands

```bash
export PYTHONPATH="src;lab;evaluations"          # Git Bash on Windows (use ':' on Linux)
PYTHON=.venv/Scripts/python bash ops/verify.sh    # all quality gates
.venv/Scripts/python -m accessguard_lab agent --profile vulnerable --model scripted:follow-up
.venv/Scripts/python -m accessguard_eval run --out reports/evaluation
```

## Latest actual results (final tree, before the commit)

- `bash ops/verify.sh` (CPython 3.13.13 venv): exit 0. The run covered:
  - lint, format and types (mypy strict, 91 files);
  - 266 tests OK, 1 skipped (the real-model test);
  - pip-audit: no known vulnerabilities;
  - detect-secrets: 136 tracked files clean;
  - wheel: 40 files, 0 problems;
  - demo: exact match.
- `ops\verify.ps1` (Windows PowerShell 5.1, 7 steps without audit): exit 0.
- `bash ops/verify.sh tests demo` on CPython 3.12.7 without install: exit 0, 266 tests OK.
- Evaluation: exit 0; request-based metrics identical across reruns; at full budget all
  strategies report exactly each answer key; the scripted agent ties the deterministic baseline
  (no advantage).
- Mutation check of the new guards: 10 injected bugs, 10 killed.

## Known issues and limitations

- Python `Path.write_text` on Windows writes CRLF; write files with LF (`.ps1` stays CRLF per
  `.gitattributes`).
- `detect-secrets` (`ops/ci/secret_scan.py`) scans only tracked files: stage new files before
  trusting the secrets step.
- The `-ExecutionPolicy Bypass` form of the PowerShell command was declined by the assistant's
  tool safety policy in this session; `verify.ps1` was run directly instead.
- See `docs/milestones/M2.md` "Not done or not verified".

## Next step

With an Ollama model available (owner decision), run the opt-in real-model test and the
evaluation with `--model ollama:<name>`. Record the reported digest, version, settings and
results in `docs/milestones/M2.md` and `docs/evaluation.md`. Otherwise, the next milestone is M3
(a separately authorised test application through configuration and an explicit policy).
