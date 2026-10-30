# AccessGuard 0.3 (V1 commercial beta, in progress)

AccessGuard tests whether one customer of a multi-tenant API can read another customer's data.
You declare test users, the records they own and who may read what; a runner in your own
environment sends read-only requests for every combination; a deterministic engine reports a
violation only when another tenant's protected values appear in a response. Findings come
with evidence, fix guidance, triage and retests.

> Working name; not a registered trademark. A passing report covers only the declared policy
> and checks; it is **not** a guarantee of security. Nothing is deployed, published or sold yet.

## What is in this repository

| Part | Path | Status |
| --- | --- | --- |
| Engine + runner CLI (stdlib only) | `src/accessguard/` | implemented, tested |
| Web control plane (Django 6.1) | `web/accessguard_web/` | implemented, tested (78 tests incl. end-to-end over HTTP; also run on PostgreSQL 17) |
| Separate integration app (flawed/fixed) | `sampleapp/` | implemented, tested |
| ML foundation (datasets, training, gated registry) | `ml/accessguard_ml/` | implemented, tested; **no model active** |
| Development lab and M2 evaluation fixtures | `lab/`, `evaluations/` | unchanged from M2 |
| Runner distribution (wheel / source archive) | `pyproject.toml`, `ops/dev/customer_acceptance.py` | built and installed in clean venvs; full customer journey passes; **not published to PyPI** |
| Deployment: Dockerfile, compose | `Dockerfile`, `docker-compose.yml` | built and run locally (Docker 29.8, PostgreSQL 17); **not deployed to a server** |
| CI | `.github/workflows/` | written; **never run** (nothing pushed) |
| Billing (Stripe) and AI assistants | `web/accessguard_web/billing/`, `.../assistant/` | implemented, tested with signed fixtures and fake providers only; **no real Stripe payment and no real AI call made** |

## Run the whole product locally

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python ops\dev\run_stack.py
```

Open <http://127.0.0.1:8000/>, sign up, open the demo project and start its scan. You will
see 10 confirmed violations of the synthetic sample API; "deploy" the fixed version on the
project page and retest: the findings are resolved as verified. Details: [docs/install.md](docs/install.md).

## Verify

```bash
PYTHON=.venv/Scripts/python bash ops/verify.sh       # lint, format, mypy strict, engine tests,
                                                    # web tests, migrations, deploy check, audit,
                                                    # secret scan, wheel build, lab demo
```

Latest local result: see [docs/HANDOFF.md](docs/HANDOFF.md) and the acceptance record in
[docs/milestones/V1.md](docs/milestones/V1.md).

## How it works

1. **Configure** a project in the web app with forms: target URL, allowed path prefixes, how
   test users authenticate (Bearer, API-key header or cookie), a session-check endpoint, test
   users, records and who may read them. Optional import from OpenAPI.
2. **Approve** the exact scope (attestation, frozen spec with SHA-256).
3. **Scan**: your runner (`accessguard runner --config runner.toml`) claims the job, refuses
   origins not on its local allowlist, reads credentials locally, sends GET requests within a
   budget and deadline, and uploads a report without credentials or response values.
4. **Act**: findings with evidence, coverage, limitations and rule-based fix guidance;
   optional AI explanation; triage; retest including legitimate-access checks.

Pricing (hypotheses): Free, Team €49, Business €199 per month; Stripe Checkout and Customer
Portal in test mode ([ADR 0011](docs/adr/0011-stripe-billing-and-entitlements.md)).

## AI and ML status (important)

- **Verdicts, scope and budgets are deterministic.** No model decides whether something is a
  violation or fixed.
- **No AI prioritisation:** the M2 evaluation found no demonstrated advantage of a local model
  over simple ordering rules ([docs/evaluation.md](docs/evaluation.md)); scans run every check.
- **Two assistants** ([ADR 0012](docs/adr/0012-bounded-ai-assistants.md)): an OpenAPI setup
  draft and a finding explainer, schema-validated, opt-in per organisation, user-approved.
  Tested with fake providers only; **no real model call has been made for them** (no API key;
  not enough free memory for the local model on the development machine).
- **ML foundation** ([ADR 0013](docs/adr/0013-ml-ranking-foundation.md)): consented labels,
  versioned datasets, grouped evaluation against baselines, registry with rollback. The only
  data is synthetic; the candidate model failed the promotion gate. No model is active.

## Documentation

- [Project walkthrough](docs/PROJECT_WALKTHROUGH.md) · [Handoff](docs/HANDOFF.md) · [V1 plan and acceptance](docs/milestones/V1.md)
- [Install](docs/install.md) · [Operations](docs/operations.md) · [Launch checklist](docs/launch-checklist.md)
- [Threat model](docs/threat-model.md) · [ADRs](docs/adr/) · [Report schema](docs/report-schema.md)
- [Economics model](docs/business/economics.md) · [Product texts and ads](docs/business/marketing-copy.md)
- Earlier milestones: [M1](docs/milestones/M1.md), [M2](docs/milestones/M2.md), [evaluation](docs/evaluation.md)
- [Original requirements (Danish)](AccessGuard-Claude-Code.md)

## Limitations

GET requests only; only declared users, records and endpoints; exact JSON value matching;
no pagination or query-parameter testing; no write operations, side channels or
authentication-strength tests. Results on synthetic apps say nothing about detection rates on
real systems. Willingness to pay and competitive advantage are not validated.
