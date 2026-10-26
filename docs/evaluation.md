# Selection-strategy evaluation (milestone 2)

Method and rationale: [ADR 0008](adr/0008-selection-strategy-evaluation.md). Code:
`evaluations/accessguard_eval/`. Fixtures: `evaluations/accessguard_eval/fixtures/`.

## Commands

```powershell
$env:PYTHONPATH = "src;lab;evaluations"
py -3 -m accessguard_eval fixtures                       # list fixtures
py -3 -m accessguard_eval run --out reports\evaluation   # scripted test adapter (default)
# Later, with a locally installed Ollama model (opt-in; not run in milestone 2):
py -3 -m accessguard_eval run --model ollama:llama3.2 --model-timeout 120 --out reports\eval-real
```

Options: `--fixtures a,b`, `--fractions 0.25,0.5,1.0`, `--seeds 1,2,3,4,5`,
`--max-model-calls`, `--max-invalid-replies`, `--model-timeout`, `--ollama-port`.
Outputs: `results.json` (every run, summary and provenance) and `summary.md`.
Exit codes:

- 0: completed.
- 1: a fixture's complete deterministic run does not match its answer key.
- 2: invalid arguments.
- 5: at least one agent run had a failed model (its partial results are still written).

## Fixtures

| Fixture | Checks | Full budget (requests) | Expected findings | What it contains |
| --- | ---: | ---: | ---: | --- |
| `helpdesk` | 49 | 68 | 7 | Role bypass on payroll within each tenant; one ticket that leaks in its 403 body |
| `clinic` | 45 | 61 | 3 | A single record readable across tenants (needle); a legitimate cross-tenant referral |
| `projects` | 30 | 46 | 7 | Plan listing leaks across workspaces; budgets readable anonymously; an auditor tenant with explicit grants |
| `library` | 30 | 39 | 0 | No flaws (false-positive control) |

"Full budget" is the request count of a complete deterministic run (session probes, owner
baselines, checks and retests). In the recorded run, each fixture's complete deterministic run
reported exactly its hand-written answer key (`deterministic_full_budget_exact: true`).

## Recorded result (scripted adapter, 2026-10-26)

Command: `python -m accessguard_eval run --out reports/evaluation` (defaults: seeds 1-5,
fractions 25/50/100 %), CPython 3.13.13, Windows 11. Agent = **scripted `follow-up` test
adapter, not an AI model**.

The table shows true positives found out of the expected findings. Random rows are the mean
over 5 seeds, with the min-max range in brackets.

| Fixture | Budget | Deterministic | Seeded random | Agent (scripted) |
| --- | ---: | ---: | ---: | ---: |
| helpdesk | 17 (25 %) | 0 / 7 | 1.0 [0-2] | 0 |
| helpdesk | 34 (50 %) | 5 / 7 | 3.4 [2-5] | 5 |
| helpdesk | 68 (100 %) | 7 / 7 | 7.0 | 7 |
| clinic | 16 (26 %) | 0 / 3 | 0.4 [0-1] | 0 |
| clinic | 31 (51 %) | 0 / 3 | 1.6 [1-2] | 0 |
| clinic | 61 (100 %) | 3 / 3 | 3.0 | 3 |
| projects | 12 (26 %) | 0 / 7 | 1.0 [1-1] | 0 |
| projects | 23 (50 %) | 0 / 7 | 3.4 [3-4] | 0 |
| projects | 46 (100 %) | 7 / 7 | 7.0 | 7 |
| library | all | 0 / 0 | 0 | 0 |

The next table gives the requests used until the first true positive, at full budget.
Random is the mean over 5 seeds.

| Fixture | Deterministic | Seeded random | Agent (scripted) |
| --- | ---: | ---: | ---: |
| helpdesk | 24 | 15.4 | 24 |
| clinic | 40 | 19.8 | 40 |
| projects | 26 | 9.2 | 26 |

Across all 36 summary rows:

- There were no false positives.
- The agent made 0 invalid replies.
- Agent model calls equal the number of executed checks at full budget, where the status is
  `completed`. In budget-limited runs (status `interrupted`) there is exactly one more model call
  than executed checks: the last chosen check could not start because the HTTP budget was
  exhausted, and it is reported as SKIPPED "in progress".

A second run with the same command gave identical request-based metrics; only the
wall-clock seconds differ.

## Interpretation

- **The scripted agent gives no advantage here.** It matches the deterministic baseline at
  every budget. Its heuristic only adapts after a first finding: before that it follows the
  listed (policy) order. On these fixtures, the follow-up checks it then prefers are also next
  in policy order.
- **Seeded random beats the policy order at small budgets on these fixtures.** The flawed
  resources happen to be declared late in each policy, and a random order does not care about
  declaration order. This shows that results depend strongly on fixture layout. It is not a
  general property of random ordering.
- At full budget, every strategy finds every expected finding. Order only changes what is
  found before a budget runs out.
- These results validate the evaluation mechanics: equal budgets, isolation from the answer
  key, scoring, reproducibility and agent accounting. They say **nothing about the quality of
  any AI model**.

## Not yet measured

- **A real model.** The run uses `--model ollama:<name>` and `tests/e2e/test_real_model.py`
  (enabled with `ACCESSGUARD_REAL_MODEL`). When run, the results file records the model
  digest and Ollama version as reported by `/api/tags` and `/api/version`. A missing value is
  left empty.
- Variance across real-model runs. With temperature 0 and a fixed seed, Ollama output is
  expected to be stable, but this has not been verified.
- Larger or more realistic fixtures, policies where flaws appear early, and costs (the local
  model has no monetary cost; token counts are recorded per step when Ollama returns them).
