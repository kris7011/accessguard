# Report schema 1.2

`report.json` is the machine-readable result of one run. Keys are stable within a
`schema_version`; additive changes bump the minor version, breaking changes the major version.
Produced by `application/reporting.py`; written by `adapters/report_files.py` after credential
scrubbing.

## Top level

| Key | Type | Meaning |
| --- | --- | --- |
| `schema_version` | string | `"1.2"` (1.1 added `run.completed` and `status.complete`; 1.2 added `run.agent`, `run.planner.seed`, `results[].requests_used_after` and the agent interruption reasons; older readers can ignore new keys) |
| `tool` | object | `name`, `version` |
| `run` | object | Provenance, scope, planner, limits (below) |
| `status` | object | `overall` (`findings`, `needs_review`, `incomplete`, `checks_passed`), `complete` (false if the run was interrupted) and `meaning` (includes an interruption note) |
| `summary` | object | Count per outcome; every outcome key is always present |
| `coverage` | object | See "Coverage" |
| `sessions` | object | Per identity: `state` (`valid`/`invalid`) and `reason` from the session probe |
| `findings` | array | One entry per FAIL or SUSPECTED result |
| `results` | array | One entry per planned check, in execution order |
| `not_tested` | array of strings | Areas outside the run, plus identities whose session could not be verified |
| `disclaimer` | string | Scope statement |

## `run`

`id`, `started_at`, `finished_at` (ISO 8601, UTC), `target` (label), `scope.origin`,
`scope.constraints`, `policy.id`, `policy.sha256` (of the exact policy bytes), `planner`
(`kind`: `deterministic`, `seeded-random`, `ollama` (M1 reorder-only) or `agent`; `uses_ai`,
true only when a real model adapter was configured; `model`, `prompt_version`, `latency_ms`
(for the agent: total model latency), `seed`), `agent` (see below; `null` unless the agent
selected the checks), `limits` (`max_requests`, `min_interval_seconds`, `max_duration_seconds`,
`retest_findings`, `concurrency`), `requests_used`, `interrupted` (`request budget exhausted`,
`run deadline exceeded`, `run cancelled by stop signal`, `agent stopped before every check was
executed`, `model failure ended the agent run`, `too many invalid model replies`, `model call
budget exhausted` or `null`), `completed` (`true` only if the run was not interrupted), `python`.

## `run.agent`

| Key | Meaning |
| --- | --- |
| `adapter` | `provider` (`ollama`, `scripted`), `model`, `is_real_model` (false for the scripted test adapter), `label`, `settings` (generation settings sent, e.g. `temperature`, `seed`, `num_ctx`, `endpoint`; or the script name) |
| `provider_metadata` | Version/digest metadata exactly as the provider reported it (Ollama: `ollama_version`, `digest`, `modified_at`, `format`, `family`, `parameter_size`, `quantization_level`); empty when not reported |
| `provider_metadata_error` | Why the metadata could not be read, or `null` |
| `prompt_version`, `tool_contract_version`, `allowed_actions` | `agent-v1`, `agent-tools-v1`, `["run_check", "stop"]` |
| `verdicts_by` | Always the deterministic engine |
| `limits` | `max_model_calls`, `max_invalid_replies`, `max_output_chars`, `model_timeout_seconds`, `max_output_tokens` |
| `status` | `completed`, `stopped_early`, `model_failed`, `invalid_replies_exhausted`, `model_calls_exhausted`, `interrupted` |
| `failure` | AccessGuard-generated failure text, or `null` (never model output) |
| `model_calls`, `invalid_replies`, `model_latency_ms_total` | Counters |
| `steps[]` | Per model call: `call`, `action` (`run_check`, `stop`, `rejected`, `failed`, `interrupted`), `check_id` (only for an accepted `run_check`), `rejection` (fixed category such as `unknown_check_id`), `latency_ms`, `provider_metadata` (e.g. `eval_count`, `prompt_eval_count`, `done_reason`) |

Prompts and model replies are not stored in the report.

An interrupted run still produces a complete report document: results gathered before the
interruption are kept, the check that was in flight is SKIPPED with the reason
`Interrupted while in progress, no verdict: ...`, and later checks are SKIPPED with
`Not executed: ...`. The coverage numbers show the partial coverage. The same applies when the
agent ends the run (stop, model failure, invalid replies, model call budget): checks it did not
execute are SKIPPED, `completed` is `false` and the overall status is never `checks_passed`.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `PASS` | The declared expectation was verified with a valid baseline and session; for a denied actor, the response body was empty or parsed JSON and contained none of the protected values |
| `FAIL` | Evidence contradicts the policy: all protected values leaked (with **any** status code, including 401/403/404/5xx), or granted access denied without content |
| `SUSPECTED` | Partial evidence, or a FAIL that did not reproduce on retest |
| `INCONCLUSIVE` | Insufficient basis: no baseline, invalid session, redirect, 429, 5xx, soft error, or a body the engine cannot analyse (non-JSON, invalid JSON, misleading Content-Type) |
| `SKIPPED` | Not executed or not completed because of the budget, deadline, stop signal or an agent-side end of the run (no verdict) |
| `ERROR` | Technical failure: timeout, connection error, oversized response, scope violation |

## Coverage

`planned_checks`, `executed_checks`, `conclusive_checks` (PASS + FAIL), `conclusive_ratio`
(= conclusive / planned), `by_kind` (`item`, `list` → outcome counts) and `by_relation`
(`owner`, `same_tenant`, `shared_grant`, `cross_tenant`, `anonymous` → outcome counts; item checks
only).

## `results[]`

`check_id`, `kind`, `actor`, either `resource` + `relation` (item) or `collection` + `visible` +
`hidden` (list), `expectation`, `outcome`, `reason`, `executed`, `observed_at`, `finding_type`
(`unauthorized_access`, `expected_access_denied` or `null`), `verification` (`reproduced`,
`not_reproduced`, `not_retested` or `null`), `evidence`, `requests_used_after` (target requests
the run had used when the check got its first verdict, including probes and baselines; `null`
when not executed).

`evidence`: `request` (method and path), `status`, `content_type`, `size_bytes`, `body_sha256`,
`matched_protected_fields` (`resource`, `fields`, `complete`). It never contains headers,
credentials or response values.

## `findings[]`

`id` (check id), `outcome`, `type`, `rule` (the declared grant in words), `actor` (`id`, `tenant`,
`role`; `null` tenant/role for anonymous), `subject` (`type`, `id`, `category`, `owner_tenant`, and
for listings `leaked_resources`), `expected`, `actual`, `evidence`, `reproduction` (ordered steps
without credentials), `scope`, `observed_at`, `versions` (`tool`, `policy_sha256`, `target`),
`verification`.
