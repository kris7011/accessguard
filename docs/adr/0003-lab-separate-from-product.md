# ADR 0003: The vulnerable lab is a separate package with hand-written ground truth

- Status: accepted (milestone 1)
- Date: 2026-10-26

## Problem

The prototype's vulnerable fixture and fixed tokens (`demo-a`, `demo-b`) lived in the same module
as the engine. Requirements §2 forbid built-in flaws and fixed tokens from ever becoming product
functions. Milestone 1 also needs a richer lab (three tenants, roles, a share) and a trustworthy
way to judge the engine's results.

## Options

1. Keep the lab inside `accessguard`: simple imports, but the product ships vulnerable code.
2. Separate top-level package excluded from the product distribution.
3. Separate repository: strongest isolation, but slows development and CI for little gain now.

## Decision

Option 2: `lab/accessguard_lab` with its own `pyproject.toml`.

- Credentials are generated with `secrets.token_urlsafe` at every lab start; no fixed tokens.
- Flaws are individually switchable (`Flaw` enum) and combined into named profiles.
- Ground truth (`ground_truth.py`) lists expected failing check ids **literally** per flaw instead
  of deriving them from the server or engine, so a bug in either cannot redefine "correct".
- Edge-case routes (`/edge/{behaviour}/{id}`) model soft errors, login pages, redirects to a
  metadata address, 429, 5xx, partial disclosure, flaky behaviour and slow responses.
- The product wheel is checked in CI (`ops/ci/check_wheel.py`) and a static test forbids product
  modules from importing the lab.

## Consequences

- The lab composes the product engine (it depends on `accessguard`, never the reverse).
- Ground truth must be updated by hand when the lab policy changes; the tests fail loudly if not.
- Exact-match results on the lab are **synthetic** and are not evidence of real-world detection.

## Revisit when

Evaluation datasets with many applications are introduced (milestone 2/5): move fixtures and
manifests to `evaluations/` with versioned datasets and separate train/test applications.
