# Selection-strategy evaluation (milestone 2)

Method and rationale: [ADR 0008](adr/0008-selection-strategy-evaluation.md). Code:
`evaluations/accessguard_eval/`. Fixtures: `evaluations/accessguard_eval/fixtures/`.

**Status: no real language model has been evaluated.** Every agent result below comes from
scripted test adapters (deterministic heuristics, not AI). A real local model passed the
bounded smoke test on 2026-10-27 (3 valid model choices). The full opt-in model test passed
the same day under runtime setup B (prompt cache disabled; 56 valid choices, 56/56 checks,
exact match with the answer key), after a first attempt was stopped for memory. See
[Real-model protocol](#real-model-protocol). A **bounded** comparative real-model evaluation
(fixtures `projects` and `library`, 25 % and 50 % budgets) ran on 2026-10-27. By its
pre-registered rule, the model shows an advantage **per HTTP request** on `projects` (5
confirmed findings against at most 2 for the simple strategies' medians at 50 %), at a much
longer waiting time and with important limits. See
[Bounded comparative real-model evaluation](#bounded-comparative-real-model-evaluation-pre-registered-2026-10-27).
A post-hoc control afterwards showed that a simple scripted "listings first" rule matches the
model on `projects`, so that advantage is explained by the rule
([details](#post-hoc-list-first-control-on-projects-and-library-2026-10-27)).
The full evaluation matrix has not been run, so there is no general documented advantage of AI
prioritisation.

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
| 3a. Bounded comparison | **Run 2026-10-27 (setup B), exit 0**: `projects` and `library` at 25 % and 50 %, 3 model repetitions per cell, 99 valid model calls and 0 invalid. Advantage per request on `projects`, limited (see [results](#results-run-2026-10-27-after-the-pre-registration-commit-00f860f)) |
| 3. Subset and full evaluation | Not yet run (needs the owner's go-ahead). Run them with runtime setup B |

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

## Bounded comparative real-model evaluation (pre-registered 2026-10-27)

Written and committed **before** any run of this experiment. The owner asked for a limited
comparison first; the larger evaluation matrix (all four fixtures, 100 % budgets) is not part
of it and was not started.

### Question

On the `projects` fixture (7 expected findings) and the flaw-free `library` fixture, at 25 %
and 50 % of the full HTTP budget, does the real local model (prompt `agent-v1`) find and
confirm more expected findings, or find the first one with fewer requests, than three simple
strategies: the deterministic policy order, seeded random order, and the scripted
`deny-first` reference? What does it cost in waiting time?

### Design

| Item | Setting |
| --- | --- |
| Fixtures | `projects` (30 checks, full budget 46 requests, 7 expected findings) and `library` (30 checks, full budget 39, 0 expected) |
| Budgets | `--fractions 0.25,0.5`; the harness rounds up: `projects` 12 and 23, `library` 10 and 20 requests (the harness re-derives the full budget from a complete deterministic run and records it) |
| Strategies | deterministic (1 run per cell), seeded random (seeds 1-5, the documented defaults), `scripted:deny-first` (3 repetitions), real model (3 repetitions) |
| Equal for every strategy | the same HTTP budget per cell; probes, owner baselines and retests count against it; the same retest reserve in the shared engine (ADR 0008 amendment); the same deadline of 1800 s per run (`--max-run-seconds 1800`), which for the agent includes model time |
| Model | `ollama:qwen3:4b-instruct-2507-q4_K_M`, digest `0edcdef34593…` (checked against `meta.agent_adapter.provenance`), Ollama 0.40.11, **runtime setup B** (`LLAMA_ARG_CACHE_RAM=0`) |
| Model settings | as the passed full test: prompt `agent-v1`, tool contract `agent-tools-v1`, temperature 0, seed 0, `num_ctx` 8192, at most 64 output tokens, at most 200 model calls and 3 invalid replies per run, **300 s per model call** (`--model-timeout 300`; the passed test used 300 s, the protocol's example command above says 120 s) |
| Order | sequential: first the scripted evaluation (no model server), then the real-model evaluation; never in parallel |

Commands (Git Bash, repository root, `PYTHONPATH="src;lab;evaluations"`):

```bash
python save_reports.py <out> run --model scripted:deny-first --fixtures projects,library --fractions 0.25,0.5 --agent-repeats 3 --model-timeout 300 --max-run-seconds 1800 --out <out>
python save_reports.py <out> run --model ollama:qwen3:4b-instruct-2507-q4_K_M --fixtures projects,library --fractions 0.25,0.5 --agent-repeats 3 --model-timeout 300 --max-run-seconds 1800 --out <out>
```

`save_reports.py` (a scratch wrapper, git-ignored under `reports/`) calls the unchanged
`accessguard_eval` CLI and only saves every raw scan report to disk as it returns, so the
per-call model metadata (load time, `prompt_eval_count`) is kept. It changes no input,
strategy, budget or scoring. Deterministic and seeded-random runs happen in both evaluations;
they must give identical results, which doubles as a reproducibility check.

### Model loading and timing

- Before the real-model evaluation, one load request outside the measured runs
  (`POST /api/generate` with an empty prompt and `num_ctx` 8192, which Ollama documents as
  "load the model") puts the model in memory. Measured runs therefore start with a loaded
  model, as a long-running tool would.
- The warm-up time is reported separately and not added to any run.
- Every measured call's `load_duration` comes from the per-step metadata. If a measured run
  contains a reload (for example after Ollama's 5-minute idle unload), it stays in that run's
  time and is reported, not removed.
- Reported times: wall-clock seconds per run (including model time), model seconds per run, and
  the wall-clock time of each whole evaluation.

### Resources and safety

- Check free RAM, commit, VRAM and disk before the start.
- Run the commit-memory watchdog (`reports/real-model-cram0-2026-10-27/watchdog.py`, same
  rule): it stops only the model server if free commit drops below 300 MB.
- Confirm `prompt cache is disabled` in the `llama-server` log after the first model call.
- Loopback only; no downloads; the server is stopped afterwards.
- The model never sees the answer key or the fixture flaw configuration (the harness loads the
  key only after every run of a fixture; enforced by `tests/security/test_agent_boundaries.py`).
- No prompt, fixture, strategy or limit is changed during the experiment. Every run is kept,
  including failed, interrupted or watchdog-stopped runs. Exit code 5 is reported, not
  retried.

### Metrics, per fixture, budget and strategy

- expected findings found (true positives) and confirmed (FAIL with a reproducing retest);
- false positives;
- coverage: executed checks out of 30, and the conclusive ratio;
- actual HTTP requests used;
- requests until the first true positive, shown as **not reached** when a run found nothing (never
  as 0);
- total seconds per run and model seconds;
- invalid replies, agent end status, and distinct selection orders across repetitions.

Per-run values are listed next to the median and the min-max range, so a failed or very
different repetition is not hidden by a mean. Two efficiency views are reported: true positives
per 100 requests, and true positives per minute of wall-clock time.

### Decision rule (fixed before the run)

The primary metric is **confirmed true positives on `projects`**. The comparison value for
seeded random is its median over the five seeds; the deterministic order and `deny-first` give
one value each (`deny-first` is deterministic, so its three repetitions are expected to be
identical).

- **Advantage** in a `projects` cell: every one of the three model repetitions confirms more
  expected findings than the deterministic order, `deny-first` and the seeded-random median,
  with 0 false positives. Equal confirmed counts with fewer requests to the first finding in
  every repetition are reported as a secondary-metric advantage only; they do not count under
  the overall rule below.
- **No demonstrated advantage** in a cell: the model's median confirmed count is not higher
  than the best of the three simple strategies.
- **Insufficient basis** in a cell: a model repetition failed (`model_failed`,
  `invalid_replies_exhausted`), was stopped by the watchdog or the deadline, or the
  repetitions disagree so that the median is higher but not every repetition is.
- `library`: any false positive is a disadvantage. Zero false positives everywhere is the
  expected control result and says nothing about prioritisation.
- **Overall**: "advantage" only if the model has an advantage in at least one `projects` cell,
  is not worse by median in the other, and has no false positive on `library`. Otherwise "no
  demonstrated advantage" or, if a cell is insufficient, "insufficient basis".
- The conclusion is limited to these two synthetic fixtures, these budgets, this model and
  these settings. With three repetitions and one flawed fixture it is not a general statement
  about AI prioritisation.

### Results (run 2026-10-27, after the pre-registration commit `00f860f`)

The design above was followed without changes. The scripted `deny-first` evaluation ran after
the section was written but just before it was committed. It is a deterministic script, and its
numbers equal the earlier recorded ones.

**Execution.**

| Item | Result |
| --- | --- |
| Scripted evaluation | exit 0, 38 scans (2 complete reference runs that set the full budgets, 36 scored runs), 3.0 s in total |
| Real-model evaluation | **exit 0**, 38 scans (2 reference, 36 scored), **415.3 s** in total (of which 409.0 s model time over 12 agent runs) |
| Runtime | setup B. The `llama-server` log shows `prompt cache is disabled`, `n_ctx = 8192` and 21/37 layers on the GPU, as in the passed full test. Listening on `127.0.0.1:11434` only |
| Provenance | `ollama_version` 0.40.11, digest `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`, gguf, qwen3, 4.0B, Q4_K_M. No `unknown` keys, no metadata errors, one distinct value per key |
| Settings and limits | temperature 0, seed 0, `num_ctx` 8192, 64 output tokens; 200 calls, 3 invalid replies and 300 s per call; prompt `agent-v1`, tool contract `agent-tools-v1` |
| Model calls | 99, **all valid**: 0 invalid replies, 0 rejections, 0 `stop` actions. The log shows `truncated = 0` for all 99, with `prompt_eval_count` at most 1997 of 8192 |
| Agent end status | `interrupted` (HTTP budget used up) in all 12 runs; no `model_failed`, `invalid_replies_exhausted` or deadline stop. The deny-first runs also all ended `interrupted` |
| Model loading | warm-up request outside the measured runs: 9.27 s (`llama-server started in 7.58 s`). Inside the measured runs, `load_duration` was at most 0.25 s per call, so no measured run contained a reload |
| Memory | the watchdog did not intervene. Free commit stayed at least 2947 MB; `llama-server` private memory peaked at 4656 MB; the VRAM peak was 2299 MiB. **Free physical RAM was below 100 MB in 139 of 186 samples** (0 MB once). Commit was safe, but the machine was paging; this may explain single slow calls (up to 14.0 s against a typical 3-5 s) |
| Reproducibility | the 24 deterministic and seeded-random runs were identical in both evaluations |
| Answer-key sanity | the complete deterministic run matched the answer key exactly on both fixtures (full budgets 46 and 39, as recorded) |

Every strategy used exactly its budget in every run (12, 23, 10 or 20 requests, including probes,
baselines and retests); no run exceeded it. There were 0 false positives in all 72 scored runs.

**Results table, `projects`** (7 expected findings).

Values are per run where every run gave the same value. Otherwise they are the median with the
min-max range in brackets. "Not reached" means the run found no expected finding. Seconds are
wall-clock per run, including model time.

| Budget | Strategy | Runs | Found | Confirmed | Executed / 30 | First finding (requests) | Seconds per run | Model s per run | Distinct orders |
| --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| 12 (26 %) | deterministic | 1 | 0 | 0 | 6 | not reached | 0.01 | - | - |
| 12 (26 %) | seeded random | 5 | 1 | 1 | 3 | 9 [8-11] | 0.01 | - | - |
| 12 (26 %) | deny-first | 3 | 0 | 0 | 5 | not reached (3 of 3) | 0.01 | - | 1 |
| 12 (26 %) | **model** | 3 | **2** | **2** | 3 | **8** | 13.9 [12.0-15.9] | 13.8 [11.9-15.8] | 1 |
| 23 (50 %) | deterministic | 1 | 0 | 0 | 15 | not reached | 0.02 | - | - |
| 23 (50 %) | seeded random | 5 | 3 [2-4] | 2 [1-4] | 12 [11-13] | 9 [8-11] | 0.02 | - | - |
| 23 (50 %) | deny-first | 3 | 1 | 1 | 14 | 20 | 0.02 | - | 1 |
| 23 (50 %) | **model** | 3 | **5** | **5** | 10 | **8** | 42.7 [42.6-47.1] | 42.4 [42.4-46.9] | 1 |

**`library`** (no flaws, false-positive control). Every strategy found nothing and reported 0
false positives in every run, so "first finding" is not reached for all of them.

| Budget | Strategy | Runs | Executed / 30 | Seconds per run | Model s per run |
| --- | --- | ---: | ---: | ---: | ---: |
| 10 (26 %) | deterministic / seeded random / deny-first | 1 / 5 / 3 | 5 / 3 [2-3] / 4 | 0.01 | - |
| 10 (26 %) | model | 3 | 4 | 21.4 [18.3-22.6] | 21.2 [18.1-22.3] |
| 20 (51 %) | deterministic / seeded random / deny-first | 1 / 5 / 3 | 13 / 11 / 12 | 0.02-0.03 | - |
| 20 (51 %) | model | 3 | 12 | 52.1 [50.0-73.2] | 51.7 [49.7-72.8] |

**Per request versus per waiting time (`projects`).**

| Budget | Strategy | Found per 100 requests | Found per minute of wall-clock time |
| --- | --- | ---: | ---: |
| 12 | seeded random (median) | 8.3 | about 5,700 |
| 12 | model (median) | 16.7 | 8.6 |
| 23 | seeded random (median) | 13.0 | about 6,100 |
| 23 | model (median) | 21.7 | 7.0 |

The simple strategies have no model time, and the local fixture answers in milliseconds, so
their wall-clock rate is very large. The model spent about 3-5 s per choice, or roughly 1.1-1.9 s
per HTTP request on `projects`.

**Variation between repetitions.** In every cell, the three model repetitions chose exactly the
same checks in the same order (one distinct selection digest), with the same results. Only the
time varied, by up to 46 % (`library` 20: 50.0-73.2 s). All repetitions ran in one server
session. Earlier, two smoke runs in different server sessions chose differently (see
`docs/milestones/M2.md`), so repeatability **across** sessions is still not shown. The three
repetitions are therefore effectively one observation per cell, not three independent ones.

**Why the model scored higher (inspection of the saved reports, not a new experiment).**

- At 12 and 23 requests on `projects`, the model always began with the six plan-listing checks
  (`list:plans@…`). It started the same way on `library` at 10 requests.
- In the answer key, **5 of the 7 `projects` findings are those listing checks** (one listing
  leak affects five actors). The model found exactly those five at 23 requests. It never reached
  the two remaining findings, the anonymous budget reads.
- The model's advantage on `projects` thus comes from a consistent "listings first" preference
  that matches this fixture's flaw layout. The policy order, `deny-first` and random order do
  not put listings first.
- The model did not always choose listings first. On `library` at 20 requests, it began with a
  cross-tenant item check, so its order depends on the state it sees (including the budget).
- **A scripted "listings first" baseline was not run** (no new experiments in this round).
  Whether the model adds anything beyond such a simple rule is therefore not shown.

### Conclusion (by the pre-registered rule)

- `projects` 12: **advantage.** Each model repetition confirmed 2 findings, against 0
  (deterministic), 0 (`deny-first`) and a median of 1 (seeded random), with 0 false positives.
  The first finding came at request 8, the lowest value any strategy reached (seeded random:
  8-11).
- `projects` 23: **advantage.** Each repetition confirmed 5, against 0, 1 and a median of 2
  (best seed: 4). First finding at request 8.
- `library`: 0 false positives for every strategy (the expected control result).
- **Overall, by the rule: advantage, limited to the tested scenarios.** On the `projects`
  fixture at 25 % and 50 % budgets, this model with `agent-v1` found and confirmed more
  expected findings **per HTTP request** than the deterministic order, seeded random and
  `deny-first`. It caused no false positives on the flaw-free `library`.

What the result does **not** show:

- **An advantage in waiting time.** Each model run took about 12-73 s, where the simple
  strategies took a few hundredths of a second. The advantage counts only where requests, not
  time, are the scarce resource, for example under rate limits or with a fragile target.
- **That the advantage generalises.** It rests on one flawed fixture, in which most flaws sit in
  listing checks, which the model happens to prefer. `helpdesk` (role bypass) and `clinic`
  (one cross-tenant record) were not part of this experiment.
- **That the model beats a trivial "listings first" rule.** That rule was not tested in this
  experiment. A later, post-hoc control found that it matches the model on `projects` (see
  [the next section](#post-hoc-list-first-control-on-projects-and-library-2026-10-27)).
- **Repeatability across server sessions.** All repetitions were identical, but they ran in one
  server session.

Kept locally (git-ignored, not committed): `reports/eval-real-bounded-2026-10-27/`.

- `scripted/` and `model/`: `results.json`, `summary.md`, every raw report under `raw/`,
  console output;
- `model/`: the server log, the warm-up reply, the memory CSV and the watchdog summary;
- `analysis.md`, with every run listed;
- the scratch scripts `save_reports.py` and `analyze.py`.

Later addendum: the "listings first" rule was tested afterwards; see the next section.

## Post-hoc list-first control on `projects` and `library` (2026-10-27)

**This is an after-the-fact check, not an independent confirmation.** The rule was chosen
*after* the bounded evaluation showed that the model put listings first. It can test whether
that rule alone explains the model's measured advantage on `projects`. It cannot show that the
model or the rule generalises, because the rule was fitted to an observation on these very
fixtures.

**The rule** (`scripted:list-first`, `adapters/scripted_model.py`, not AI): among the pending
checks in the model-visible state, run every list check first, then every other check, in
listed (policy) order within each group. It reads only the check `kind` and `status`, the same
state the model receives. It has no access to the answer key, the flaw configuration, fixture
names or particular check ids, and it does not react to findings (unlike `follow-up` and
`deny-first`). Budget, retest reserve, deadline and safety limits are the shared engine's,
unchanged.

**Run.** The same command and settings as the bounded scripted evaluation, with only the
adapter changed (no model run, no server):

```bash
python save_reports.py <out>/raw run --model scripted:list-first --fixtures projects,library --fractions 0.25,0.5 --agent-repeats 3 --model-timeout 300 --max-run-seconds 1800 --out <out>
```

Exit 0. The 24 deterministic and seeded-random runs were identical to the saved bounded
evaluation, so the baselines reproduced. The model numbers below are the **saved** results of
the bounded evaluation; the model was not rerun.

| Fixture | Budget | Strategy | Found | Confirmed | FP | Executed / 30 | First finding (requests) | Seconds per run |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- | ---: |
| projects | 12 | model (saved, 3 runs) | 2 | **2** | 0 | 3 | 8 | 12.0-15.9 |
| projects | 12 | list-first (3 runs) | 3 | **2** | 0 | 3 | 8 | 0.02 |
| projects | 23 | model (saved, 3 runs) | 5 | **5** | 0 | 10 | 8 | 42.6-47.1 |
| projects | 23 | list-first (3 runs) | 5 | **5** | 0 | 11 | 8 | 0.02-0.04 |
| library | 10 | model / list-first | 0 / 0 | 0 / 0 | 0 / 0 | 4 / 4 | not reached | 18.3-22.6 / 0.01-0.03 |
| library | 20 | model / list-first | 0 / 0 | 0 / 0 | 0 / 0 | 12 / 14 | not reached | 50.0-73.2 / 0.02-0.04 |

Every list-first value was the same in all three repetitions (one selection order).

What it shows:

- **On `projects`, list-first matches the model on the primary metric** (confirmed findings:
  2 and 5) and on requests to the first finding (8). At 12 requests it also found one more
  candidate than the model; that finding came with the last affordable request and stayed
  `not_retested`. At 23 requests it executed one check more (11 against 10).
- **The orders are not identical.** The model began with the auditor's listing and the anonymous
  listing (a PASS), then the other plan listings; list-first takes the listings in policy order.
  On `library` at 10 requests the executed checks were the same; at 20 the model mixed item and
  list checks.
- **Conclusion:** on `projects` the model's measured advantage per request over the earlier
  baselines is fully explained by the simple "listings first" rule. The model added nothing
  measurable beyond it there, at a cost of 12-47 s per run against hundredths of a second.

Kept locally (git-ignored): `reports/eval-list-first-2026-10-27/posthoc/` (results, summary,
raw reports), `posthoc-comparison.txt` and the scratch script `compare_posthoc.py`.
