# Handoff status

Purpose: lets work resume after an interrupted session. Never put credentials or secrets here.

## Current state (2026-10-26, end of the second M2 session)

- Branch: `feature/m2-adaptive-agent`. M2 itself is committed as `c0abc42`.
- The real-model readiness work from this session is **staged but not committed** (the owner
  had not asked for a commit). Nothing is pushed or merged.
- Milestone 2 status: **implementation with mock and contract verification; real-model
  verification open.** No language model is installed on the development machine, so no real
  model has been run.
- M3 has not been started.

## Done in this session

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
- Reserving HTTP budget for retests is proposed, not implemented. It would affect every
  strategy.
- GitHub Actions not run (nothing pushed); Python 3.11, Linux and macOS not verified.

## Commands

```bash
export PYTHONPATH="src;lab;evaluations"          # Git Bash on Windows (use ':' on Linux)
PYTHON=.venv/Scripts/python bash ops/verify.sh    # all quality gates
.venv/Scripts/python -m accessguard_eval run --model scripted:deny-first --agent-repeats 3 --out reports/evaluation-deny-first
```

For the real-model sequence (smoke test → full test → subset evaluation → full evaluation),
see `docs/evaluation.md` → "Real-model protocol".

## Latest actual results (this session, staged tree)

- `bash ops/verify.sh` (CPython 3.13.13 venv): exit 0.
  - Lint, format and types: mypy strict, 91 files.
  - Tests: **273 run, 271 passed, 0 failed, 2 skipped**. Both skipped tests are the real-model
    tests, and a skipped test is not a verification.
  - pip-audit: no known vulnerabilities.
  - detect-secrets: 136 tracked files clean.
  - Wheel: 40 files, 0 problems.
  - Demo: exact match.
  - The first run of this session exited 1 (lint, format and type issues in new code). They
    were fixed before the rerun.
- Before this session (`c0abc42`): 266 run, 265 passed, 0 failed, 1 skipped. The earlier
  "266 OK, 1 skipped" meant this.
- Real-model test logic against the **fake** Ollama server (not a model):
  - valid replies: 2 passed;
  - always-invalid replies: 2 failed, as intended.
- Evaluation (`--agent-repeats 3`) with `scripted:follow-up` and with `scripted:deny-first`:
  - Both exited 0, with 0 false positives and 0 invalid replies.
  - Both scripted adapters show no advantage over seeded random.
  - No strategy confirms a finding in a budget-limited run.

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
