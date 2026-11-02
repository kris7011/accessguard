# ADR 0001: Python standard library engine as a layered modular monolith

- Status: accepted (milestone 1)
- Date: 2026-10-26

## Problem

The v0.1 prototype was a single 200-line script. Milestone 1 needs a testable structure that
keeps the local CLI runnable without cloud services or subscriptions, and that can later gain an
API, a database and an agent without rewriting the core.

## Options

1. Keep one script: simplest, but mixes HTTP, verdicts and reporting; hard to test in isolation.
2. Adopt a web framework and ORM now (FastAPI, PostgreSQL): useful later, but no current need;
   adds dependencies and operational weight before the engine is proven.
3. Layered modular monolith in Python, standard library only at runtime.

## Decision

Option 3. Package `src/accessguard` with four layers and inward-pointing dependencies:

- `domain`: policy, checks, observations, verdicts, results, scope. No I/O.
- `application`: use cases (planning, runner, gate, reporting) through narrow ports
  (`TargetClient`, `Planner`).
- `adapters`: HTTP transport and client, JSON policy loader, report files, Ollama planner.
- `entrypoints`: CLI and exit codes.

The direction is enforced by `tests/security/test_architecture.py`. Python remains the language
for engine, agent and ML work (requirements §4). `src` layout follows PyPA guidance so tests run
against the package, not against stray files in the repository root.

## Consequences

- Zero runtime dependencies: `PYTHONPATH=src;lab` is enough to run the demo and tests offline.
- Hand-written validation (no pydantic) is more code, but every rule is explicit and tested.
- `http.client` gives precise control (no redirects, no proxies, IP pinning) at the cost of
  writing a small transport.

## Revisit when

An HTTP API is introduced (milestone 4): evaluate FastAPI/pydantic for the service boundary, keeping
the domain free of framework imports. Revisit "no runtime dependencies" if HTTPS pinning or
schema validation becomes cheaper and safer with a maintained library.
