# ADR 0006: The optional model planner may only reorder approved checks

- Status: accepted (milestone 1, carried over from v0.1)
- Date: 2026-10-26

## Problem

A language model can help prioritize, but it must never decide what is in scope, add targets,
drop checks or judge results (requirements §8). The prototype's Ollama adapter already followed
this; milestone 1 had to keep that behaviour without hiding model failures.

## Options

1. Remove the adapter until milestone 2.
2. Let the model select or generate checks now.
3. Keep a reorder-only planner behind the `Planner` port.

## Decision

Option 3. `adapters/ollama_planner.py` sends minimal, credential-free check metadata (id, kind,
expectation, relation) to a loopback Ollama endpoint. The reply must match
`{"order": [...]}` and be an exact permutation of approved ids (`domain/checks.order_checks`).
Any failure raises `PlannerError`; the CLI exits with code 5 and writes no report. Reports record
planner kind, `uses_ai`, model name, prompt version (`reorder-v1`) and latency.

## Consequences

- Because every check is executed regardless of order, the planner currently cannot change
  results; it only demonstrates the guarded integration point.
- Verified with mocked responses only; no real model has been run.

## Revisit when

Milestone 2 introduces the bounded agent loop with budgets, where ordering matters, and a
comparison with the deterministic baseline under the same budget is required.

## Update (milestone 2)

The reorder-only planner is kept unchanged in behaviour (`--ollama-model` on `demo`/`scan`).
Its HTTP exchange now goes through the shared `OllamaModelClient` (ADR 0007), which also sends
`options` (`temperature` 0, `seed` 0, `num_ctx` 8192) and is bounded by the same transport
limits. The adaptive agent of milestone 2 is described in ADR 0007; the comparison under equal
budgets in ADR 0008.
