# Selection-strategy evaluation (milestone 2)

Method and rationale: [ADR 0008](adr/0008-selection-strategy-evaluation.md). Code:
`evaluations/accessguard_eval/`. Fixtures: `evaluations/accessguard_eval/fixtures/`.

**Status: no real language model has been evaluated.** Every agent result below comes from
scripted test adapters (deterministic heuristics, not AI). A real local model passed the
bounded smoke test on 2026-10-27 (3 valid model choices). The full opt-in model test passed
the same day under runtime setup B (prompt cache disabled; 56 valid choices, 56/56 checks,
exact match with the answer key), after a first attempt was stopped for memory. See
[Real-model protocol](#real-model-protocol). The comparative real-model evaluation has not
been run. **There is no documented advantage of AI prioritisation.**

## Commands

```powershell
$env:PYTHONPATH = "src;lab;evaluations"
py -3 -m accessguard_eval fixtures                                   # list fixtures
py -3 -m accessguard_eval run --out reports\evaluation               # scripted follow-up adapter (default)
py -3 -m accessguard_eval run --model scripted:deny-first --agent-repeats 3 --out reports\evaluation-deny-first
```

Options:

- `--fixtures a,b`, `--fractions 0.25,0.5,1.0`, `--seeds 1,2,3,4,5`
- `--agent-repeats N` (1-20, default 1): agent runs per fixture and budget, each with a fresh
  model client. Every repetition is scored and averaged; none is selected or dropped.
- `--max-run-seconds S` (default 300, at most 3600): the same deadline for every run of every
  strategy. For the agent it includes model time.
- `--max-model-calls`, `--max-invalid-replies`, `--model-timeout` (per model call),
  `--ollama-port`

Outputs: `results.json` (every run, summary and provenance) and `summary.md`.

Exit codes:

- 0: completed.
- 1: a fixture's complete deterministic run does not match its answer key.
- 2: invalid arguments.
- 5: at least one agent run had a failed model or ran out of valid replies (its partial results
  are still written).

The real-model protocol is in [Real-model protocol](#real-model-protocol) below.

## What is measured

Per run (`results.json` → `runs`):

- true and false positives against the hand-written answer key, and recall;
- **confirmed true positives**: outcome FAIL whose separate retest reproduced it;
- true positives per 100 requests;
- requests and seconds until the first true positive;
- conclusive ratio and executed checks (coverage);
- total wall-clock seconds, which include model time;
- for the agent only:
  - model calls and invalid replies;
  - model seconds;
  - agent status (`completed`, `stopped_early`, `interrupted`, `model_failed`,
    `invalid_replies_exhausted`, `model_calls_exhausted`);
  - a selection digest: identical digests mean identical choices across repetitions.

Request counts include session probes, owner baselines and retests for every strategy.

Provenance (`meta.agent_adapter`):

- the adapter's settings;
- provider metadata exactly as reported;
- `provenance`, which lists every key the adapter can report (for Ollama: `ollama_version`,
  `digest`, `modified_at`, `format`, `family`, `parameter_size`, `quantization_level`). A key
  that was never reported is the string `"unknown"`. A key with several values means the model
  or server changed during the evaluation;
- `provider_metadata_errors`.

## Fixtures

| Fixture | Checks | Full budget (requests) | Expected findings | What it contains |
| --- | ---: | ---: | ---: | --- |
| `helpdesk` | 49 | 68 | 7 | Role bypass on payroll within each tenant; one ticket that leaks in its 403 body |
| `clinic` | 45 | 61 | 3 | A single record readable across tenants (needle); a legitimate cross-tenant referral |
| `projects` | 30 | 46 | 7 | Plan listing leaks across workspaces; budgets readable anonymously; an auditor tenant with explicit grants |
| `library` | 30 | 39 | 0 | No flaws (false-positive control) |

"Full budget" is the request count of a complete deterministic run (session probes, owner
baselines, checks and retests). In every recorded run, each fixture's complete deterministic
run reported exactly its hand-written answer key (`deterministic_full_budget_exact: true`).

## Recorded results (scripted adapters, 2026-10-26)

Commands (CPython 3.13.13, Windows 11, both exit 0, 12-13 s each):

```bash
python -m accessguard_eval run --model scripted:follow-up  --agent-repeats 3 --out reports/evaluation-follow-up
python -m accessguard_eval run --model scripted:deny-first --agent-repeats 3 --out reports/evaluation-deny-first
```

Defaults: seeds 1-5, fractions 25/50/100 %, 300 s per run.

The two scripted adapters are documented heuristics in `adapters/scripted_model.py`, **not AI**:

- `follow-up` is the policy order until a first finding, then it prefers checks that share
  the finding's resource, category and relation, or actor.
- `deny-first` is the same, but before any finding it tries every check expected to be
  denied before any check expected to be allowed. It is a non-AI reference for what the
  structural state alone allows (see [Information basis](#information-basis-of-the-agent)).

The table shows true positives found out of the expected findings. Random rows are the mean over
5 seeds, with the min-max range in brackets. Each scripted agent ran 3 times per row; the
repetitions were identical (1 distinct selection order, as expected for a deterministic
script).

| Fixture | Budget | Deterministic | Seeded random | follow-up | deny-first |
| --- | ---: | ---: | ---: | ---: | ---: |
| helpdesk | 17 (25 %) | 0 / 7 | 1.0 [0-2] | 0 | 0 |
| helpdesk | 34 (50 %) | 5 / 7 | 3.4 [2-5] | 5 | 5 |
| helpdesk | 68 (100 %) | 7 / 7 | 7.0 | 7 | 7 |
| clinic | 16 (26 %) | 0 / 3 | 0.4 [0-1] | 0 | 0 |
| clinic | 31 (51 %) | 0 / 3 | 1.6 [1-2] | 0 | 0 |
| clinic | 61 (100 %) | 3 / 3 | 3.0 | 3 | 3 |
| projects | 12 (26 %) | 0 / 7 | 1.0 [1-1] | 0 | 0 |
| projects | 23 (50 %) | 0 / 7 | 3.4 [3-4] | 0 | 1 |
| projects | 46 (100 %) | 7 / 7 | 7.0 | 7 | 7 |
| library | all | 0 / 0 | 0 | 0 | 0 |

Requests used until the first true positive, at full budget (random: mean over 5 seeds):

| Fixture | Deterministic | Seeded random | follow-up | deny-first |
| --- | ---: | ---: | ---: | ---: |
| helpdesk | 24 | 15.4 | 24 | 20 |
| clinic | 40 | 19.8 | 40 | 37 |
| projects | 26 | 9.2 | 26 | 20 |

Other observations across all rows:

- There were no false positives.
- The scripted agents made 0 invalid replies.
- Statuses: `completed` at full budget, `interrupted` (HTTP budget) below it.
- **Confirmed true positives are 0 in every budget-limited run, for every strategy.** At full
  budget, every true positive is confirmed.

The deterministic and seeded-random numbers equal the earlier M2 run, so they reproduce.

## Retest budget reserve (before/after, 2026-10-27)

The results above exposed a defect: under a limited budget no strategy confirmed any finding,
because retests ran only after the scan had already spent every request. The runner now holds
back one request per finding (its retest costs exactly one, as the baseline and session are
cached) and releases the reserve for the existing separate retest phase. The policy lives in
the shared engine, so it is identical for the deterministic order, seeded random and the agent;
the model input and model choice are unchanged. See ADR 0008 (2026-10-27 amendment) for the
decision and the alternatives.

Same command and defaults as above (`scripted:follow-up`, `--agent-repeats 3`, seeds 1-5,
fractions 25/50/100 %, 300 s). Columns are `true positives` and `confirmed` (a FAIL whose
separate retest reproduced it), before and after; `cov` is the conclusive ratio. Only the rows
with finding activity at a partial budget are shown; every full-budget (100 %) row and every
`library` row (no flaws) is unchanged. Across all 108 runs, no run exceeded its budget.

| Fixture | Budget | Strategy | TP before | conf before | TP after | conf after | cov before | cov after |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| helpdesk | 17 (25 %) | seeded-random | 1.0 | 0.0 | 1.0 | 1.0 | 0.12 | 0.11 |
| helpdesk | 34 (50 %) | deterministic | 5.0 | 0.0 | 4.0 | 4.0 | 0.49 | 0.43 |
| helpdesk | 34 (50 %) | seeded-random | 3.4 | 0.0 | 3.0 | 2.8 | 0.45 | 0.40 |
| helpdesk | 34 (50 %) | follow-up | 5.0 | 0.0 | 4.0 | 4.0 | 0.49 | 0.43 |
| clinic | 16 (26 %) | seeded-random | 0.4 | 0.0 | 0.4 | 0.4 | 0.10 | 0.09 |
| clinic | 31 (51 %) | seeded-random | 1.6 | 0.0 | 1.6 | 1.6 | 0.40 | 0.36 |
| projects | 12 (26 %) | seeded-random | 1.0 | 0.0 | 1.0 | 1.0 | 0.12 | 0.10 |
| projects | 23 (50 %) | seeded-random | 3.4 | 0.0 | 3.0 | 2.2 | 0.47 | 0.40 |

What the numbers show:

- **Confirmed findings rose from 0 to the number the run can confirm within budget.** At full
  budget nothing changed (every finding was already confirmed); `library` is untouched, so the
  reserve never fires and never invents a confirmation when there are no findings.
- **The drawback is a small coverage and candidate cost at partial budgets.** Confirming a
  finding spends a request that would otherwise probe another check, so the conclusive ratio
  and the raw true-positive count drop slightly (e.g. `helpdesk` 50 % deterministic: 5
  unconfirmed candidates became 4 confirmed findings). This is deliberate: a confirmed finding
  is worth more than an unconfirmed candidate (requirements §7), and the cost is paid only when
  a finding is actually made.
- **When the budget cannot confirm every finding, the report is honest.** `projects` 50 %
  seeded random finds 3.0 but confirms 2.2 on average: a finding made on nearly the last
  affordable request leaves nothing to retest, so it stays FAIL / `not_retested` rather than
  disappearing or being shown as reproduced.

## Interpretation

- **Neither scripted adapter shows an advantage over seeded random order.**
  - `follow-up` ties the deterministic baseline everywhere.
  - `deny-first` beats the deterministic order slightly: the first finding comes earlier on
    three fixtures, and it finds 1 of 7 instead of 0 of 7 on `projects` at 50 %. It still
    stays behind seeded random.
- **Seeded random wins at small budgets on these fixtures** because the flawed resources are
  declared late in each policy. This is a property of the fixture layout, not of random
  ordering in general.
- **Findings under a limited budget are now confirmed within that budget** (since the
  2026-10-27 retest reserve; see the before/after section). The runner holds back one request
  per finding for its retest, so a budget-limited run reports confirmed findings rather than
  unconfirmed candidates. The reserve is enforced identically for every strategy, so the
  comparison stays fair. The recorded 2026-10-26 table above predates the fix and its
  budget-limited columns are unconfirmed candidates.
- At full budget, every strategy finds and confirms every expected finding.
- These results validate the evaluation mechanics: equal budgets, isolation from the answer
  key, scoring, repetitions, reproducibility and agent accounting. They say **nothing about the
  quality of any AI model**.

## Information basis of the agent

What the model receives each turn (`application/agent_view.py`, prompt `agent-v1`):

| Element | Fields |
| --- | --- |
| Run | task name; budget used/max for HTTP requests, model calls and invalid replies; the category of the last rejected reply |
| Actor | opaque alias; `anonymous` flag; tenant alias; role alias; session state (`valid`, `invalid`, `not_applicable`, `unknown`) |
| Item check | alias; actor; **expected access** (`allow`/`deny`); resource alias; category alias; owner-tenant alias; **relation of actor to resource** (`owner`, `same_tenant`, `shared_grant`, `cross_tenant`, `anonymous`); status |
| List check | alias; actor; expected access; collection alias; number of visible and hidden items; status |
| **Previous outcome** of a done check | step number; outcome (`PASS`, `FAIL`, `SUSPECTED`, `INCONCLUSIVE`, `SKIPPED`, `ERROR`); finding type; HTTP status code; executed flag |

Never sent:

- names, paths and field names;
- response bodies, verdict reasons and target error texts;
- credentials;
- the answer key;
- the active lab flaw profile or fixture flaw configuration.

Tests that enforce this: `tests/security/test_agent_boundaries.py` and
`test_agent_never_sees_answer_key_or_flaw_configuration`.

Assessment:

- **Enough for meaningful priors and follow-up.**
  - Expected access, relation and owner tenant let a model prefer checks that can reveal
    unauthorized access: `deny` with `cross_tenant` or `anonymous`, and `deny` with
    `same_tenant`, which marks a role restriction.
  - Shared grants are visible as legitimate cross-tenant access.
  - After a finding, a model can follow the same resource, category, relation or actor.
- **Not enough to tell structurally similar checks apart before the first finding.**
  - Nothing in the state indicates which of several `cross_tenant`/`deny` checks is flawed.
    That is inherent: a policy does not say where the bugs are.
  - The `deny-first` result shows the ceiling of the structure alone on these fixtures: a
    small gain over policy order, but not over random.
- **Missing structure (no names needed):**
  1. List checks are not linked to anything else. The model does not see which tenant or
     categories a collection covers, or how the actor relates to it, so it cannot connect a
     listing leak with item checks.
  2. Role aliases carry no privilege information. A model can only infer it by counting
     `allow` expectations per role.
  3. Category aliases carry no sensitivity. This is deliberate. A future, explicit policy field
     (for example a customer-declared sensitivity level) could add it without names.
  4. Aliases follow policy declaration order. A model with position bias will imitate the
     deterministic order, which these fixtures disadvantage.
- **Not added in this round.** Changing the state now would leave the first real-model run
  without a clean reference.
  - Recommended order: run `agent-v1` unchanged first.
  - Then add anonymous structure (items 1 and 2, possibly a seeded presentation order for 4)
    as `agent-v2`, and measure it as an ablation under the same budgets.
  - Raw customer names, paths or server content are not to be added.

## Real-model protocol

Progress:

| Step | Status |
| --- | --- |
| 1. Smoke test | **Run 2026-10-27, passed**: `qwen3:4b-instruct-2507-q4_K_M` on Ollama 0.40.11, 3 valid model choices, 0 invalid, 15 HTTP requests, 80.65 s model time ([details](milestones/M2.md#real-model-smoke-test-2026-10-27)) |
| 2. Full opt-in test | **Passed 2026-10-27 under runtime setup B** (`LLAMA_ARG_CACHE_RAM=0`; both tests in the module OK, exit 0). The agent made 56 valid choices with 0 invalid and ended `completed`. It executed 56 of 56 checks: 23 FAIL, all reproduced, and 33 PASS. That is an exact match with the answer key (0 false positives, 0 false negatives). It used 92 HTTP requests and 294.12 s of model time (agent scan 294.9 s, module 312.1 s), with no truncation. `llama-server` memory stayed flat at 4637-4801 MB, and free commit was at least 2.73 GB ([details](milestones/M2.md#full-opt-in-real-model-test-rerun-with-the-prompt-cache-disabled-2026-10-27-passed)) |
| 2a. Earlier full-test attempt | **Stopped for memory, not passed** (runtime setup A, default prompt cache): 12 valid choices, 12/56 checks, all matching the answer key; the watchdog stopped the server at 214 MB free commit ([details](milestones/M2.md#full-opt-in-real-model-test-2026-10-27-stopped-for-memory)) |
| 3. Subset and full evaluation | Not yet run. Run them with runtime setup B |

Prerequisites: an installed Ollama and model (installed on the development machine since
2026-10-27; see
[docs/milestones/M2.md](milestones/M2.md#local-capacity-and-model-recommendation-2026-10-26)).

Run the steps in this order and keep every run's output, including failures.

```powershell
$env:PYTHONPATH = "src;lab;evaluations"
$env:ACCESSGUARD_REAL_MODEL = "<model>"
# 1. Small, time-bounded smoke test (3 model calls, 120 s per call, 600 s per run)
py -3 -m unittest tests.e2e.test_real_model.RealModelAgentTests.test_smoke_a_few_bounded_model_calls -v
# 2. Full opt-in test (the model selects every lab check; up to 1800 s)
py -3 -m unittest tests.e2e.test_real_model -v
# 3. Subset evaluation, then the full evaluation (same budgets, seeds and deadline for every strategy)
py -3 -m accessguard_eval run --model ollama:<model> --agent-repeats 3 --model-timeout 120 --max-run-seconds 1800 --fixtures clinic,projects --out reports\eval-real-subset
py -3 -m accessguard_eval run --model ollama:<model> --agent-repeats 3 --model-timeout 120 --max-run-seconds 1800 --out reports\eval-real
```

Record and check:

- `meta.agent_adapter.provenance`, including any `unknown`;
- the settings (temperature 0, seed 0, `num_ctx` 8192);
- invalid replies and agent statuses (early stops, failures, deadline interruptions);
- distinct selection orders across repetitions;
- model seconds next to total seconds;
- confirmed true positives.

Start the server with runtime setup B, which adds `LLAMA_ARG_CACHE_RAM=0` to the server's
own environment. Monitor free commit memory during every real-model run on this machine. With
Ollama 0.40.11 on Windows, the `llama-server` child keeps a host prompt cache by default (up to
8192 MiB, about 0.43 GiB per agent call) on top of about 4.7 GB for the model load. That
exhausted commit memory in the
[first full-test attempt](milestones/M2.md#full-opt-in-real-model-test-2026-10-27-stopped-for-memory).
With setup B the cache is disabled, which the `llama-server` log confirms, and the model
server's memory stays flat. The start command and how it was verified are in
[Runtime setups](milestones/M2.md#runtime-setups). Record the setup (A or B) with every result.

Compare `prompt_eval_count` (in the report's `run.agent.steps`) with `num_ctx`. Use the
first step after the model was loaded, because later calls may reuse a cached prompt prefix
and then count fewer tokens. A value close to `num_ctx` suggests that Ollama truncated the
state.

The 1800 s deadline applies to every strategy. It is chosen so that the HTTP budget, not the
deadline, normally ends a run. An agent run that hits the deadline is reported as
`interrupted` and is kept.

Exit code 5 means at least one agent run failed. Report that, do not rerun selectively.
