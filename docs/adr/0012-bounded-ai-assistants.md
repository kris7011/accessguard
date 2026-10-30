# ADR 0012: Two bounded AI assistants; verdicts, scope and budgets stay deterministic

- Status: accepted (V1)
- Date: 2026-10-29

## Problem

The M2 evaluation found no demonstrated advantage of a model choosing the order of checks
(docs/evaluation.md). Customers still spend effort on setup and on understanding findings.
AI should be used where it reduces that work, with clear tasks and limited power.

## Decision

- **Scan strategy:** complete coverage in policy order with a budget sized for every check
  and retest (`projects/config.py: recommended_budget`). No model orders checks in the
  product. A learned ranker exists only behind a promotion gate (ADR 0013).
- **Setup assistant** (`assistant/tasks.py`, prompt `setup-v1`): input is the deterministic
  OpenAPI catalogue (paths, summaries, field names). The model may only choose among
  extracted operations and fields; invented references are dropped (`apply_setup_answer`).
  A person reviews and applies the draft. The rule-based draft works without any model.
- **Finding explainer** (prompt `explain-v1`): input is the finding's facts (rule, roles,
  endpoint, status, matched field names; no response values exist) and optionally code the
  user pastes. Output is schema-constrained and validated (keys, lengths, step count) and
  shown as an "AI draft". It never changes status; only a retest resolves a finding.
- **Providers** behind one port (`assistant/providers.py`): Claude through the official
  `anthropic` SDK (`claude-opus-5-5`, structured output via `output_config.format`,
  server-side refusal fallback), a local Ollama model (loopback, reusing the engine's
  client), or none. No silent fallback to canned text.
- **Controls:** organisation opt-in for third-party providers (off by default), per-plan
  monthly quota, a usage record per request (provider, model, prompt version, tokens,
  latency, outcome; prompts are not stored), untrusted input wrapped and marked.

## Consequences

- The product is fully usable without any AI provider.
- Assistant quality is not measured yet; there is no eval set for the two tasks.
- Sending finding facts to a third party needs consent and a data processing agreement
  with the provider before paid use.

## Revisit when

An eval set for the two tasks exists (then compare models and prompts against it), or
customers ask for code changes (then a patch-plus-regression-test workflow with review).
