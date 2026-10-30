# ADR 0013: ML foundation for check ranking, gated behind evaluation and consent

- Status: accepted (V1; no model active)
- Date: 2026-10-29

## Problem

A learned ranking of checks could find violations earlier when a budget is small. Learning
needs verified labels, separated evaluation and a way to roll back, and must not be presented
as self-improvement while the data is too small. Customer data must not be used without
explicit agreement.

## Decision

- **Features** `features-v1` in the engine (`application/ranking.py`): engine enumerations
  and counts only (kind, expectation, relation, sharing, field and grant counts, path depth,
  listing sizes). No ids, names, paths or field names. Used for training and inference.
- **Labels with provenance:** synthetic applications labelled by hand-written answer keys;
  customer labels only from people's confirm/reject decisions, fixes verified by a retest, and
  engine PASS results of complete full scans. Exported only for organisations with
  `ml_data_consent` (off by default), grouped by an HMAC of the project id
  (`scans/learning.py`, `manage.py export_ml_labels`).
- **Datasets** are content-addressed (`ml/accessguard_ml/data.py`): the version is the hash
  of the rows; a manifest records sources, counts and feature version.
- **Training** (`training.py`, `train-v1`): seeded, class-weighted logistic regression.
- **Evaluation:** leave-one-application-out, recall of confirmed violations within the
  first 25 % and 50 % of checks, against policy order, list-first, deny-first and the median of
  20 seeded random orders. The six lab profiles count as one application.
- **Registry** (`registry.py`): candidates with their evaluation and gate result; `promote`
  requires the gate and a named reviewer; `rollback` restores the previous model.
- **Gate:** at least 5 consented customer applications; mean recall@25 % at least 0.05
  above the best baseline; recall@50 % not below it; no application more than 0.2 below the
  best baseline.

## Result (2026-10-29)

Dataset `ds-b82628ce4c20c8e3`: 240 checks, 50 violations, 6 synthetic applications,
0 customer applications. Candidate `ranker-b82628ce-s0`: mean recall@25 % 0.262 (best
baseline list-first 0.275), recall@50 % 0.703 (best baseline random median 0.529); it
regresses by more than 0.2 on three applications. **Gate: refused.** No model is active,
and nothing in the product loads one.

## Consequences

- The pipeline is reproducible end to end and ready for real labels.
- Synthetic results say nothing about real systems, and six applications are far too few.
- The offline metric ignores session probes and baselines a real scan also spends.

## Revisit when

At least five consenting customers have completed and triaged scans; then rerun the same
gate. Re-evaluate the features if candidates keep regressing on individual applications.
