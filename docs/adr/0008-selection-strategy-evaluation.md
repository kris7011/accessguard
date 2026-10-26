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
