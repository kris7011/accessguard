# ADR 0008: Evaluate check selection on separate fixtures under equal HTTP budgets

- Status: accepted (milestone 2)
- Date: 2026-10-26

## Problem

Milestone 2 must show whether the agent helps, without assuming that AI is better
(requirements §8, §9, §13 M2). Every strategy that runs to completion executes the same checks
and reaches the same verdicts. Order only matters when the budget runs out, so comparisons
must use limited and equal budgets. Results must be reproducible, and the agent must have no
access to the expected answers.

## Options

1. Compare on the development lab.
2. Compare on separate synthetic fixtures with hand-written answer keys.
3. Wait for real applications and human-adjudicated labels.

## Decision

Option 2, implemented in `evaluations/accessguard_eval/` (not part of the product wheel).

- **Fixtures:** four fixtures (`helpdesk`, `clinic`, `projects`, `library`) served by a
  data-driven server (`fixture_server.py`). The routes, session endpoint, data and flaw
  placements differ from the lab. `library` has no flaws (a false-positive control).
- **Three files per fixture:**
  - `target.json` is the served behaviour, including the flaws.
  - `policy.json` is the declared policy, the only input to the engine and the agent.
  - `answer_key.json` holds the hand-written expected findings.
- **Isolation:** a run (`harness.run_once`) gets the policy, origin and credentials only.
  `load_answer_key` is called once per fixture after all its runs; a static test asserts
  this. The agent additionally sees only aliases, so it cannot even name a real check id.
- **Strategies:** the deterministic policy order, seeded random orders (`SeededRandomPlanner`,
  seeds 1-5) and the agent (scripted `follow-up` adapter by default, `--model ollama:NAME` as
  opt-in). All use the same engine.
- **Budgets:** a complete deterministic reference run defines the full budget of each fixture
  (probes, baselines, checks and retests). Every strategy then runs at 25 %, 50 % and 100 % of
  it.
- **Metrics:** true and false positives, recall, findings per 100 requests, requests and
  seconds to the first true positive, conclusive ratio, executed checks, model calls, invalid
  replies, total seconds and agent status.
- **Sanity check:** the reference run must reproduce the answer key exactly. Otherwise the CLI
  exits with 1.
- **Provenance:** fixture SHA-256, tool, prompt and contract versions, seeds, limits, adapter
  settings and provider-reported metadata, exactly as reported.

## Consequences

- Request-based metrics are deterministic and reproduce exactly; wall-clock times do not.
- Scripted results validate the mechanics only. The output states this in its
  `interpretation` field and in `summary.md`.
- Four small synthetic fixtures are not evidence of real-world performance, and the results
  are sensitive to where flaws sit in the policy order (see `docs/evaluation.md`).

## Revisit when

A real model is run (record its digest and settings), more or larger fixtures are added, or
milestone 5 introduces trained rankers that need train/test splits by application and flaw
type.

## Amendment (2026-10-26, real-model readiness)

Before a first real-model run, the harness was extended. These changes leave the request-based
results of the scripted runs unchanged.

- **Repetitions.** `agent_repeats` runs the agent several times per fixture and budget, with a
  fresh client each time. Every repetition is scored and averaged, never selected. A
  selection digest per run shows whether the repetitions chose identically.
- **One deadline for all.** `max_run_seconds` applies to every strategy. Only the agent spends
  time on a model, so a deadline that is too short would penalise it silently. The value is
  explicit, validated and recorded.
- **Confirmed findings.** `confirmed_true_positives` counts a FAIL only when its retest
  reproduced it. This exposed the fact that budget-limited runs never confirm findings,
  because retests run last.
- **Unknown provenance.** Adapters declare the metadata keys they can report. A key that was
  not reported is recorded as `"unknown"` instead of being left out.
- **Non-AI reference.** The scripted `deny-first` heuristic shows what the structural state
  alone achieves. A real model should beat it, not only the policy order.

## Amendment (2026-10-27, retest budget reserve)

The readiness amendment above exposed a defect: under a limited budget, the scan spent every
request before the separate retest phase, so findings were found but never confirmed
(`confirmed_true_positives` was 0 at the 25 % and 50 % budgets for every strategy). Retesting a
finding is deterministic engine work, so the AI model must not decide confirmation; the fix is
pure run-logic and the model input and model choice are unchanged.

**Options for giving retests budget:**

1. **Retest a finding immediately**, inline, right after it is observed. No reservation to
   size, but the retest logic would move into both the planned loop and the model-driven agent
   loop, interleaving retest requests with the scan and changing request ordering.
2. **Reserve budget up front** for retests. Simple to place, but it holds requests back on
   *every* run — including findings-free runs — which lowers coverage and risks starving small
   runs, exactly what the requirements warn against.
3. **Reserve lazily, one request per finding** (chosen). When a check first fails and retests
   are enabled, the runner tells the shared `RequestGate` to hold back one request from the
   scan phase; `finish()` releases the reserve before the existing separate retest phase.

**Decision:** option 3. A retest of a finding costs exactly one request (its baseline and
session are already cached), so one held-back request per finding is the precise amount. The
change touches only the gate and the single `execute()`/`finish()` path that the planned order
and the agent already share, so the budget and confirmation policy are identical for the
deterministic order, seeded random and the agent. Nothing is reserved when there are no
findings, so findings-free and generous-budget runs keep full coverage. The reserve only shifts
budget between the scan and retest phases and never raises the total above `max_requests`. A
finding that still cannot be retested (the finding consumed the last request, or a deadline or
cancellation intervened) keeps its FAIL verdict and is reported `not_retested`; it never
disappears and is never shown as reproduced.

**Measured effect (scripted `follow-up` adapter, synthetic fixtures; mechanics only, not AI
quality).** See `docs/evaluation.md` → "Retest budget reserve (before/after)" for the table.
Confirmed true positives at partial budgets rose from 0 to the number of findings the run made,
total requests stayed within budget, and coverage at partial budgets is equal or slightly lower
because some later checks are skipped in order to confirm a finding already made.
