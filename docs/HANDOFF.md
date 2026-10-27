# Handoff status

Purpose: lets work resume after an interrupted session. Never put credentials or secrets here.

## Current state (2026-10-27, fourth M2 session)

- Branch: `feature/m2-adaptive-agent`, HEAD `9bd4846` (retest reserve). M2 itself is
  `c0abc42`, the real-model readiness work `735d118`.
- The smoke-test session changed only documentation; no source or test code changed. Its
  record is committed together with the status update in `docs/milestones/M2.md` and
  `docs/evaluation.md` (smoke test run; full test and comparative evaluation open; no
  documented AI advantage).
- Nothing is pushed or merged.
- Milestone 2 status: **mock and contract verification, plus one passed real-model smoke test
  (3 model calls).** The full real-model test and the real-model evaluation have not been run.
- M3 has not been started.

## Done in this session (2026-10-27): first real-model smoke test

**Resources, measured before installing.**

| Resource | Value |
| --- | --- |
| RAM | 7.35 GB visible, 0.67-0.79 GB free. Commit 4.6 GB free of 24.6 GB; the page file is system-managed |
| VRAM | 3962 MiB free of 4096 MiB (`nvidia-smi`). Ollama itself reported 3.2 GiB available |
| Disk C: | 20.4 GB free before, 15.9 GB after install and model |

The biggest RAM users were VS Code (about 4.6 GB private) and Chrome (about 4.5 GB). No
program was closed. The first `nvidia-smi` call hung (killed after 20 s), and a later one
answered in 0.4 s. Probably the dGPU was waking from a power-saving state; this is not verified.

**Sources checked (2026-10-27).** These are documented facts:

- [Ollama model page](https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M): tag exists,
  2.5 GB, ID `0edcdef34593`, Apache 2.0, 4.02B parameters, Q4_K_M. Capabilities "tools" and
  "thinking". The page states no context length.
- [Model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507): Apache 2.0, 36 layers,
  GQA 32 Q / 8 KV heads, native context 262,144. Non-thinking only, no `<think>` blocks.
- [Ollama Windows docs](https://docs.ollama.com/windows): Windows 10 22H2+, NVIDIA driver
  551.61+ (installed: 610.62), at least 4 GB for the binary install. A standalone
  `ollama-windows-amd64.zip` exists.
- [Ollama FAQ](https://docs.ollama.com/faq): binds 127.0.0.1:11434 by default. Default context
  4096; `num_ctx` in the API options overrides it.
- The agent's required context of 8192 is the project's own setting. Ollama documents no RAM or
  VRAM requirement for this model. Those numbers were my estimates (see `docs/milestones/M2.md`)
  and were checked against the measured run below.

**Install (no installer, no autostart, no tray app, no persistent environment variables).**

- Source: the official GitHub release `ollama/ollama` **v0.40.11** (published 2026-10-24),
  asset `ollama-windows-amd64.zip`, 1,586,356,606 bytes.
- The sha256 `bda4a493ba8a7c24d57263cd843cd68cee84f44aed4299eeed532acad7eda12a` matched both
  `sha256sum.txt` and the GitHub API digest.
- Extracted to `%LOCALAPPDATA%\Programs\ollama-standalone\v0.40.11` (2.08 GB); the zip was
  deleted afterwards.
- Model store: the default `%USERPROFILE%\.ollama\models`.
- The server was started by hand for the run only:
  `OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve`.
- `netstat` showed it listening on `127.0.0.1:11434` only. It was stopped after the run, and no
  Ollama process is left running.

**Model.**

- Pulled `qwen3:4b-instruct-2507-q4_K_M`; Ollama verified the sha256 during the pull.
- Digest `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`, 2,497,293,803
  bytes, gguf, qwen3, 4.0B, Q4_K_M, Apache 2.0.
- `ollama show` reports a context length of 262144 and capabilities tools/thinking/completion.
- The model defaults (temperature 0.7, top_p 0.8, top_k 20) are overridden by the client's
  temperature 0.

**Smoke test.**

- Command: the existing
  `tests.e2e.test_real_model.RealModelAgentTests.test_smoke_a_few_bounded_model_calls`,
  unchanged, run in the CPython 3.13.13 venv with `ACCESSGUARD_REAL_MODEL` set.
- A scratchpad wrapper (not in the repo) only saved the report the test produced.
- Result: **`Ran 1 test in 80.961s ... OK`**.

| Item | Result |
| --- | --- |
| Runtime | Ollama 0.40.11, CUDA backend (`cuda_v13`), RTX 3050 Laptop, compute 8.6 |
| Settings | temperature 0, seed 0, `num_ctx` 8192, `num_predict` 64, endpoint `http://127.0.0.1:11434`; prompt `agent-v1`, tool contract `agent-tools-v1` |
| Limits | 3 model calls, 3 invalid replies, 120 s per call, 600 s per run, 512 output chars; HTTP budget 500 requests (the test's default) |
| Provenance | all 7 keys reported (`ollama_version`, `digest`, `modified_at`, `format`, `family`, `parameter_size`, `quantization_level`); no metadata error |
| Valid `run_check` | **3 of 3**: `list:documents@initech-admin`, `list:documents@initech-member`, `list:documents@anonymous` |
| Invalid replies | **0** (each reply was 15 output tokens, `done_reason: stop`; no `<think>` output) |
| End status | `model_calls_exhausted` (expected at 3 calls; not a failure status). The run is `interrupted: model call budget exhausted`, so 53 of 56 checks were skipped |
| Verdicts | 2 FAIL (both `reproduced` on retest), 1 PASS; overall `findings`, coverage 3/56 |
| HTTP requests | **15** of 500. Per the report's counting rule this includes session probes, owner baselines and retests; the breakdown was not inspected |
| Model time | **80.65 s** of 80.95 s wall time. Call 1: 68.6 s, including 41.1 s model load; calls 2 and 3: 5.9 s and 6.2 s |
| Context | `prompt_eval_count` 2965, 2992, 3018 of 8192. The server log shows `truncated = 0` for every call |
| Timeouts | none (the slowest call took 68.6 s of the 120 s limit) |

**Memory during the run.** There was no out-of-memory error and no WARN or ERROR line in the
server log; the "llm server error" status during load is the normal start-up wait.

- Ollama placed **21 of 37 layers on the GPU**: 1455 MiB of weights on CUDA and 1225 MiB in host
  RAM. The KV cache is 1152 MiB (640 MiB on GPU, 512 MiB on CPU), which matches the earlier
  144 KiB/token estimate exactly.
- VRAM in use peaked at 2291 MiB. Ollama's fit step reported 1112 MiB left free on the GPU;
  why it placed no more layers there was not investigated.
- Free RAM fell to 435 MB and free commit to **421 MB at the lowest**. Commit is the tightest
  resource: a larger context or other memory-hungry programs could cause allocation failures.

**What it shows:**

- The real integration works end to end: the loopback-only adapter, a JSON schema-constrained
  reply, alias validation, the deterministic verdicts, retests and provenance.
- This model, at these settings, produced schema-valid choices without thinking tokens.

**What it does not show:**

- Nothing about prioritisation quality: three choices are no sample, and there is no comparison
  with the deterministic or seeded-random baselines.
- Nothing about behaviour later in a run, with larger states (up to about 14k characters).
- Nothing about repeatability across repetitions or the full-run duration.
- Not whether the two FAIL findings match the lab's answer key. The smoke test does not score
  that, though both reproduced on retest.

`bash ops/verify.sh` was not rerun because no code changed (last run: exit 0, see below).

### Earlier session (2026-10-27), retest reserve, in `9bd4846`

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

- **Real-model verification beyond the smoke test.** The full opt-in test, the subset
  evaluation and the full evaluation are not run. Ollama and the model are now installed (see
  above).
- The smoke-test report itself was saved only in the session scratchpad and is not kept; the
  numbers above (and in `docs/milestones/M2.md` → "Real-model smoke test") are its record.
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
see `docs/evaluation.md` → "Real-model protocol". Start the local server first, loopback
only. It is not autostarted. Stop it with Ctrl+C when done; it holds about 2.3 GB VRAM and
1.2 GB RAM while the model is loaded.

```bash
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 \
  "$LOCALAPPDATA/Programs/ollama-standalone/v0.40.11/ollama.exe" serve
```

## Latest actual results

- Real-model smoke test (2026-10-27, this session): **passed**, 3/3 valid `run_check`,
  0 invalid replies, 15 HTTP requests, 80.65 s model time. Details are above.

### Quality gates (2026-10-27, after the retest reserve)

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

The owner decides whether to continue the real-model protocol. The smoke test shows that it
is feasible on this machine.

1. **Full opt-in test** (`py -3 -m unittest tests.e2e.test_real_model -v`). The model selects
   every lab check, up to 1800 s.
   - Expect roughly 56 calls × 6 s plus about 40 s of load time, so probably 6-10 minutes.
     That is an extrapolation from 3 calls, and later states are larger.
   - Watch `prompt_eval_count` against 8192 and free commit memory (it fell to 421 MB in the
     smoke test). Keep other programs as they are, or close memory-hungry ones yourself
     beforehand.
2. Then the subset evaluation and the full evaluation, as in `docs/evaluation.md`. The full
   evaluation is about 800 model calls, roughly 1.5-2 hours at the measured rate.
3. Record provenance, settings and every run (including this smoke test) in
   `docs/milestones/M2.md` and `docs/evaluation.md`. Only then conclude whether there is an
   advantage, no advantage or an insufficient basis compared with the baselines.

M3 (a separately authorised test application through configuration and an explicit policy)
needs a separate go-ahead.
