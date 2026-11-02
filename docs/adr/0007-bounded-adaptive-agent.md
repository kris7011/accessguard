# ADR 0007: A bounded adaptive agent with one tool and deterministic verdicts

- Status: accepted (milestone 2)
- Date: 2026-10-26

## Problem

Milestone 2 requires an agent that chooses the next security check from earlier results
(requirements §8, §13 M2). A language model is untrusted: its output can be malformed, it
can be steered by prompt injection, it can be slow or unavailable, and it must never widen
the scope, see credentials or decide whether something is secure.

## Options

1. **Free-form agent**: the model writes requests (paths, methods, headers) that the engine
   executes after filtering.
2. **Plan once**: the model returns a complete order up front (the M1 reorder planner).
3. **One tool, one approved check per turn**: the model may only name a pending check from
   the approved matrix, or stop. The engine executes it and judges it deterministically.

## Decision

Option 3.

- **Port:** `ModelClient` in `application/ports.py` (`descriptor`, `identify`, `complete`),
  provider-neutral types only. Adapters: `adapters/ollama_client.py` (loopback,
  `POST /api/chat`) and `adapters/scripted_model.py` (scripted test adapter,
  `is_real_model=False`). The M1 reorder planner now uses the same Ollama client.
- **What the model sees** (`application/agent_view.py`): opaque aliases (`c001`, `a1`, `t1`,
  `p1`, `r1`, `k1`, `l1`) and engine enumerations (kind, relation, expectation, outcome,
  finding type, session state) plus the integer HTTP status and budget counters. No policy
  string is ever sent: identity, tenant, role, resource, category and collection ids, paths,
  field names and descriptions are customer input and can carry injected instructions. No
  credentials, protected values, response bodies or verdict reasons are sent either.
- **What the model can do** (`application/agent_contract.py`): exactly
  `{"action":"run_check","check_id":"<pending alias>"}` or `{"action":"stop"}`. Validation is
  strict and never repairs a reply: size limit before parsing, standard JSON only, no duplicate
  keys, no missing or extra fields, known actions only, known pending aliases only, no repeats.
  Rejections are fixed categories; model text is never echoed into prompts or reports.
- **Loop** (`application/agent.py`): limits on model calls, invalid replies (the run ends at
  the N-th rejection), output size and output tokens. Every model call gets
  `RequestGate.call_limit`, which bounds it by the run deadline and the stop signal; a provider
  error caused by either is an interruption, not a model failure. Session probes, owner
  baselines, checks and retests all pass the same `RequestGate`, so they share one HTTP budget.
- **Execution and verdicts:** the existing `CheckRunner` (now exposing
  `preflight`/`execute`/`finish`). The model cannot influence a verdict or a retest.
- **Failure:** the loop ends with an explicit `AgentStatus` (`model_failed`,
  `invalid_replies_exhausted`, `model_calls_exhausted`, `stopped_early`, `interrupted`).
  Completed results are kept, retests still run after a model-side end (not after a run
  limit), and every check that was not executed is SKIPPED, so coverage is never full. There is
  no fallback to another model or to the deterministic order. The CLI exits with 5 for
  `model_failed` and `invalid_replies_exhausted` after writing the partial report.
- **Provenance:** reports (schema 1.2, `run.agent`) record adapter, settings, prompt version
  (`agent-v1`), tool contract version (`agent-tools-v1`), limits, every step with latency and
  provider metadata, and model version/digest only when the provider returns them
  (`GET /api/version`, `GET /api/tags`; the chat response has no digest).
  `run.planner.uses_ai` is true only for a real model adapter; scripted runs are never AI.

## Consequences

- The model cannot express a scope change, a new endpoint, a credential, a policy change or a
  verdict; this is enforced by validation and tested, not just requested in the prompt.
- Aliases remove semantics a real model might use (for example that `invoice` is more
  sensitive than `document`). A closed vocabulary of category labels could be added later.
- One model call per check is slow with a real model; latency is recorded per step.
- The state is resent every turn (no conversation memory), so the model's own text never
  re-enters the prompt; prompt size grows linearly with the number of checks.
- `uses_ai` describes the configured adapter: an agent run whose real model failed on the
  first call is still labelled AI, and its `agent.status` says it failed.

## Revisit when

A real model has been evaluated (see ADR 0008), when policies become large enough that the
per-turn state exceeds a model's context window, or when write checks or new tools are
introduced.
