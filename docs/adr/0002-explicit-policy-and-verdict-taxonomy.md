# ADR 0002: Explicit policy, deterministic check matrix and evidence-based verdicts

- Status: accepted (milestone 1)
- Date: 2026-10-26

## Problem

The prototype hard-coded five cases and a lab-specific `marker` field. The requirements (§7) demand
an explicit customer policy, support for legitimate sharing, an owner baseline before judging
cross-access, and a clear distinction between confirmed failure, suspicion, pass, insufficient
basis, skipped and technical error. Test failures and uncertainty must never look like approval.

## Options

1. Infer intent from tenant membership (anything cross-tenant is a leak): breaks on legitimate
   sharing and produces false positives.
2. Hand-written case lists per target: does not scale; coverage depends on the author.
3. Declarative grants per resource; checks generated as the full identity × resource matrix;
   verdicts by comparing protected values with the owner's baseline.

## Decision

Option 3.

- **Policy** (`domain/policy.py`): identities (tenant, role); resources with `protected_fields`,
  an `owner` identity for the baseline and explicit `grants`; collections; a session probe.
  Construction validates all invariants (owner must be granted, references must exist).
- **Checks** (`domain/checks.py`): every identity plus anonymous against every resource and
  collection. Each item check carries its relation (owner, same tenant, shared grant, cross
  tenant, anonymous) for coverage reporting.
- **Verdicts** (`domain/evaluation.py`):
  - Owner baseline must be HTTP 200 JSON containing every protected field (string ≥ 4 chars).
  - Deny expected: 403/404 (or 401 with a verified session) is PASS; a 2xx response with *all*
    protected values verbatim is FAIL; some values is SUSPECTED; none is INCONCLUSIVE.
  - Allow expected: protected content present is PASS; 403/404 is FAIL of type
    `expected_access_denied` (over-restriction, e.g. a fix that blocks everything).
  - 3xx, 429, 5xx, other codes and 401 with an invalid session are INCONCLUSIVE; transport
    failures are ERROR; budget/deadline/stop leads to SKIPPED.
  - Listings: hidden resources present is FAIL; granted resources missing is INCONCLUSIVE
    (positive control), because an empty list proves nothing.
- **Verification:** every FAIL is re-executed separately; if it does not reproduce it becomes
  SUSPECTED with `not_reproduced`.
- **Overall status:** `findings` > `needs_review` > `incomplete` > `checks_passed`; green requires
  at least one check and every check to PASS.

Behaviour change from v0.1: a granted actor receiving 403/404 was INCONCLUSIVE; it is now FAIL
(`expected_access_denied`) because the owner baseline and session probe make the denial definite.

## Consequences

- No lab-specific field names in the engine; the protected fields come from the policy.
- Exact string matching keeps false positives low but misses transformed or embedded values.
- Baselines are fetched once per resource per run; retests reuse them.
- Policies require a session-probe endpoint that echoes the subject.

## Amendment 2026-10-26: leak evidence takes precedence over the status code

The original rules matched protected values only in 2xx responses, so a 401/403/404 whose body
echoed the resource was PASS (reproduced with the `denial-body-leak` lab flaw: the run was
reported as `checks_passed`). Revised rules:

- Protected content is matched for every status code, before the status is interpreted.
- Actor not granted: complete match → FAIL (`unauthorized_access`), partial → SUSPECTED, both
  regardless of status and session state. Without content: 403/404 (or 401 with a verified
  session or the anonymous actor) → PASS; 2xx, 3xx, 429, 5xx and other codes → INCONCLUSIVE.
- Actor granted: 2xx with all values → PASS; a denial status whose body contains granted content
  → INCONCLUSIVE (contradictory response); a denial without content → FAIL
  (`expected_access_denied`).
- Listings follow the same precedence for hidden resources.

Alternative considered: treat any non-2xx body as untrusted and ignore it. Rejected, because it
recreates the false PASS. Consequence: error bodies are parsed and compared for every request,
which is cheap within the 1 MB response limit.

## Revisit when

Real targets (milestone 3) need other authentication schemes, non-JSON responses, pagination,
field-level rules or a probe-less session check; or when evidence from real APIs shows that exact
matching misses relevant leaks.
