# Report schema 1.0

`report.json` is the machine-readable result of one run. Keys are stable within a
`schema_version`; additive changes bump the minor version, breaking changes the major version.
Produced by `application/reporting.py`; written by `adapters/report_files.py` after credential
scrubbing.

## Top level

| Key | Type | Meaning |
| --- | --- | --- |
| `schema_version` | string | `"1.0"` |
| `tool` | object | `name`, `version` |
| `run` | object | Provenance, scope, planner, limits (below) |
| `status` | object | `overall` (`findings`, `needs_review`, `incomplete`, `checks_passed`) and `meaning` |
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
(`kind`, `uses_ai`, `model`, `prompt_version`, `latency_ms`), `limits` (`max_requests`,
`min_interval_seconds`, `max_duration_seconds`, `retest_findings`, `concurrency`),
`requests_used`, `interrupted` (reason or `null`), `python`.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `PASS` | The declared expectation was verified with a valid baseline and session |
| `FAIL` | Evidence contradicts the policy (all protected values leaked, or granted access denied) |
| `SUSPECTED` | Partial evidence, or a FAIL that did not reproduce on retest |
| `INCONCLUSIVE` | Insufficient basis: no baseline, invalid session, redirect, 429, 5xx, soft error |
| `SKIPPED` | Not executed because of the budget, deadline or stop signal |
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
`not_reproduced`, `not_retested` or `null`), `evidence`.

`evidence`: `request` (method and path), `status`, `content_type`, `size_bytes`, `body_sha256`,
`matched_protected_fields` (`resource`, `fields`, `complete`). It never contains headers,
credentials or response values.

## `findings[]`

`id` (check id), `outcome`, `type`, `rule` (the declared grant in words), `actor` (`id`, `tenant`,
`role`; `null` tenant/role for anonymous), `subject` (`type`, `id`, `category`, `owner_tenant`, and
for listings `leaked_resources`), `expected`, `actual`, `evidence`, `reproduction` (ordered steps
without credentials), `scope`, `observed_at`, `versions` (`tool`, `policy_sha256`, `target`),
`verification`.
