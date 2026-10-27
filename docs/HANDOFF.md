# Handoff status

Purpose: lets work resume after an interrupted session. Never put credentials or secrets here.

## Current state (2026-10-27, third M2 session)

- Branch: `feature/m2-adaptive-agent`. M2 itself is committed as `c0abc42`.
- The real-model readiness work (previously staged) is committed as `735d118`.
- The retest budget reserve (this session) is committed as a separate commit (see git log).
- Nothing is pushed or merged.
- Milestone 2 status: **implementation with mock and contract verification; real-model
  verification open.** No language model is installed on the development machine, so no real
  model has been run.
- M3 has not been started.

## Done in this session (2026-10-27)

- **Committed the staged real-model readiness work** as `735d118` (unchanged; it had only
  been staged last session).
- **Fixed: budget-limited runs never confirmed findings.** The scan spent the whole request
  budget before the separate retest phase, so retests were refused and every finding stayed
  `not_retested`. The runner now holds back one request per finding (a finding's retest costs
  exactly one request, since its baseline and session are cached) and releases the reserve for
  the existing retest phase. The policy lives in the shared `CheckRunner`/`RequestGate`, so it
  is identical for the deterministic order, seeded random and the agent. Model input and model
  choice are unchanged, so the budget effect is isolated. ADR 0008 (2026-10-27 amendment)
  records the decision and the rejected alternatives (immediate inline retest; fixed up-front
  reservation). Files: `application/gate.py`, `application/runner.py`, `application/agent.py`
  (one comment).
- **Tests** for a limited-but-sufficient budget (confirmed), an insufficient budget (honest
  `not_retested`, no overrun), a finding that does not reproduce on retest, cancellation during
  the retest, the gate reserve primitive, and identical reserve rules across strategies. Files:
  `tests/unit/test_runner.py`, `tests/unit/test_gate.py`, `tests/unit/test_agent_loop.py`,
  `tests/integration/test_agent_lab.py` (updated to assert confirmation within budget).
- **Docs:** ADR 0008 amendment, `docs/evaluation.md` before/after section, this handoff.

### Previous session (2026-10-26), now in `735d118`

| Area | Files |
| --- | --- |
| Opt-in real-model tests: now fail on `invalid_replies_exhausted` and require at least one accepted `run_check`; new bounded smoke test (3 calls, 120 s per call, 600 s per run) | `tests/e2e/test_real_model.py` |
| Evaluation: `--agent-repeats N` (every repetition scored), `--max-run-seconds` (the same for every strategy), per-run `confirmed_true_positives`, `model_seconds`, `selection_digest`; per-row `agent_statuses`, `distinct_selection_orders` | `evaluations/accessguard_eval/harness.py`, `cli.py` |
| Provenance: `ModelDescriptor.provenance_keys`; the evaluation writes `"unknown"` for keys the provider did not report, plus metadata errors | `application/ports.py`, `adapters/ollama_client.py`, `harness.py` |
| Scripted `deny-first` reference adapter (not AI) | `adapters/scripted_model.py`, `entrypoints/agent_options.py` |
| Tests for the above | `tests/integration/test_evaluation.py`, `tests/unit/test_scripted_model_and_planning.py`, `tests/e2e/test_agent_cli.py` |
| Docs | `docs/milestones/M2.md` (readiness review, machine capacity, model recommendation), `docs/evaluation.md` (new results, information basis, real-model protocol), ADR 0008 amendment, README, walkthrough, interview prep |

## Not done

- **Real-model verification.** It needs Ollama and a model. The recommendation, with its
  sources and uncertainty, is in `docs/milestones/M2.md` → "Local capacity and model
  recommendation": `qwen3:4b-instruct-2507-q4_K_M`, Apache 2.0, a 2.5 GB download plus at least
  4 GB for the Ollama install. Installing it is the owner's decision.
- `agent-v2` (more anonymous structure in the model state) is proposed, not implemented. Measure
  `agent-v1` with a real model first.
- GitHub Actions not run (nothing pushed); Python 3.11, Linux and macOS not verified.

## Commands

```bash
export PYTHONPATH="src;lab;evaluations"          # Git Bash on Windows (use ':' on Linux)
PYTHON=.venv/Scripts/python bash ops/verify.sh    # all quality gates
.venv/Scripts/python -m accessguard_eval run --model scripted:deny-first --agent-repeats 3 --out reports/evaluation-deny-first
```

For the real-model sequence (smoke test → full test → subset evaluation → full evaluation),
see `docs/evaluation.md` → "Real-model protocol".

## Latest actual results (2026-10-27, after the retest reserve)

- `bash ops/verify.sh` (CPython 3.13.13 venv): exit 0.
  - Lint, format and types: mypy strict, 91 files, no issues.
  - Tests: **280 run, 278 passed, 0 failed, 2 skipped**. Both skipped tests are the real-model
    tests, and a skipped test is not a verification. (273 → 280: +7 targeted tests for the
    reserve; one existing budget-limited agent test was updated to assert confirmation.)
  - pip-audit: no known vulnerabilities. detect-secrets: clean. Wheel: 40 files, 0 problems.
    Demo: exact match.
  - One earlier run failed on a mypy error in a new test's annotations; fixed before the rerun.
- Evaluation before/after (`scripted:follow-up`, `--agent-repeats 3`, 108 runs), full table in
  `docs/evaluation.md`:
  - **Confirmed findings at partial budgets rose from 0 to the number confirmable in budget**
    for every strategy; full-budget rows and the flaw-free `library` fixture are unchanged.
  - **No run exceeded its budget** (max `requests_used - budget` over 108 runs is 0).
  - Drawback: a small coverage/candidate drop at partial budgets, because confirming a finding
    spends a request that would otherwise probe another check (e.g. `helpdesk` 50 %
    deterministic: 5 unconfirmed candidates became 4 confirmed findings).
  - 0 false positives and 0 invalid replies throughout. Scripted adapters still show no
    advantage over seeded random (unchanged conclusion).
- Previous session (`735d118`): 273 run, 271 passed, 0 failed, 2 skipped.

## Known issues and limitations

- Python `Path.write_text` on Windows writes CRLF; write files with LF (`.ps1` stays CRLF per
  `.gitattributes`).
- `detect-secrets` (`ops/ci/secret_scan.py`) scans only tracked files: stage new files before
  trusting the secrets step.
- Long Markdown heredocs in Git Bash can fail to parse; use the editor tools for documents.
- See `docs/milestones/M2.md` "Not done or not verified".

## Next step

The owner decides whether to install Ollama and the recommended model.

- **If yes:** run the real-model protocol and record provenance (including `unknown` values),
  settings and every run in `docs/milestones/M2.md` and `docs/evaluation.md`. Then conclude
  whether there is an advantage, no advantage or an insufficient basis.
- **If no:** M2 stays "mock and contract verification". M3 (a separately authorised test
  application through configuration and an explicit policy) needs a separate go-ahead.
