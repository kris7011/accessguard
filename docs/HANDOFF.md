# Handoff status

Purpose: lets work resume after an interrupted session. Never put credentials or secrets here.

## Billing hardening (2026-10-30, fourth session)

Scope: upgrades only after payment, webhook ordering and concurrency, reproducible sandbox
drivers. Test payments only (claimable sandbox `acct_1UWAdYB9QLrE2rSI`, expires
**2026-11-06**); no live key, purchase, deployment, merge or AI call. Branch
`feature/v1-commercial-beta`, draft PR #1. Decision record: ADR 0011, update "current state
instead of timestamps, pending upgrades".

### Defects confirmed

| # | Defect | Evidence |
| --- | --- | --- |
| 1 | "Choose Business" while on Team opened a new Checkout → a **second subscription** | `PlanChangeTests` failed against the old code (Checkout called) |
| 2 | `invoice.payment_failed` / `invoice.paid` had no ordering guard: a late failure after a successful retry restarted the grace period; an old subscription's `paid` ended a running grace period | `test_late_payment_failure_after_a_successful_payment_changes_nothing`, `test_old_payment_success_does_not_end_the_grace_period_of_the_new_one` failed |
| 3 | Customer fallback in `_subscription_by_stripe`: events of an earlier subscription changed the current one; a later `deleted` of the old subscription downgraded an active re-subscription to Free | `test_old_failure_of_a_previous_subscription_does_not_touch_the_new_one`, `test_cancellation_of_a_previous_subscription_keeps_the_new_plan` failed |
| 4 | Same-second events: the later arrival won, whatever was true | `test_events_with_the_same_created_second_follow_stripes_current_state` failed; Stripe produced 3 events with one `created` in the sandbox |
| 5 | An upgrade applied before payment (`allow_incomplete`) granted the higher plan during the grace period | `test_failed_upgrade_keeps_the_previous_plan_during_the_grace_period` failed |
| 6 | Customer Portal configured with `create_prorations`: switching to Business applied at once with **no charge** (difference billed at renewal), with a declining card | sandbox, org pc2: Stripe `active` + Business, latest invoice still the EUR 49 purchase, portal "next estimated payment €349" |
| 7 | CSP `form-action` blocked the redirect to Stripe's hosted invoice page (`net::ERR_ABORTED`) | sandbox, Playwright; unit tests cannot see it; header assertion added |

Baseline before the fix: 13 of the 15 new tests failed (the 2 PostgreSQL concurrency tests
skip on SQLite; the cross-customer guard already held).

### Fixes

- `webhooks.py`: events are signals. Organisation from subscription id, customer id or our
  metadata → `SELECT ... FOR UPDATE` on its row → fetch the subscription from Stripe **inside**
  the lock → mirror it. Events of another subscription apply only when the followed one has
  ended at Stripe; customer and metadata must match (tenant check). Only `active`/`trialing`
  raise the plan; other states keep at most the plan already held. No-op syncs save nothing
  and write no audit event. Duplicate detection is limited to the `StripeEvent` insert.
  Handles `pending_update_applied|expired`. `last_event_created` removed (migration `0003`;
  the sandbox database was copied to `.local/stripe-e2e.before-0003.sqlite3` first).
- `views.webhook`: Stripe API unreachable → nothing stored, HTTP 503, Stripe retries.
- `gateway.py`: `subscription()`, `change_plan()` (`always_invoice` + `pending_if_incomplete`;
  returns the hosted invoice URL when pending), Checkout refused while Stripe lists a live
  subscription for the customer (`BillingRefused`).
- `views.checkout`: with a live subscription → plan change (refused while a payment is
  open), otherwise Checkout. Template: "Switch to …" with a note.
- Sandbox portal configuration `bpc_1UWD5Z…` now uses `always_invoice` (install.md says so).

### Sandbox evidence (stack on `.local/stripe-e2e.sqlite3`, `stripe listen`, drivers in `ops/stripe_sandbox/`)

| Scenario | Org | Result |
| --- | --- | --- |
| App upgrade, card declined | pc1 | redirect to `invoice.stripe.com`; Stripe `active`, price Team, `pending_update` → Business, invoice EUR 150 `open`; app Team, no grace |
| … then paid (`pay-open`) | pc1 | `invoice.paid` (first!), `subscription.updated`, `pending_update_applied` → app Business |
| Replacing a pending update | pc1 | Stripe sent `pending_update_expired` for the old one; app unchanged |
| Portal upgrade, `create_prorations`, card declined | pc2 | defect 6 |
| Portal upgrade, `always_invoice`, card declined | pc3 | portal: "Your payment method was declined"; Stripe pending update, invoice EUR 150 open; app Team |
| … then paid in the portal with the saved 4242 card | pc3 | app Business |
| `allow_incomplete` upgrade declined | pc4 | Stripe `past_due` with Business; app **Team** with the grace warning; after paying: Business |
| Late first delivery: listener stopped, fail + recover, 7 events resent newest-first | pc5 | first event → Business `active`; the other 6, incl. the late `invoice.payment_failed` and the `past_due` snapshot, stored as `unchanged` |
| Resend of already processed events | pc1 | `duplicate`, `updated_at` unchanged |
| Portal cancellation, end via `cancel_at` + 60 s | pc2 | warning with end date → `deleted` → Free |
| Re-subscription via Checkout | pc2 | allowed; same customer, new subscription; app follows it |
| Extra subscriptions | pc1-pc5 | exactly one live subscription per customer after every plan change |

Side effect of defect 6 worth knowing: pc2's deferred EUR 150 proration stayed as an
expected charge in the portal even after the cancellation.

### Reproducible drivers

`ops/stripe_sandbox/` (`README.md`, `sandbox.py`, `sandbox_browser.py`): opt-in via
`ACCESSGUARD_STRIPE_SANDBOX=1`, test keys only (`is_test_key`, `livemode: false` checked),
credentials only from the git-ignored env file, refuses any database but
`.local/stripe-e2e.sqlite3`. Not part of `verify.sh` or CI. The earlier ad-hoc scratchpad
scripts are superseded.

### Still not verified

- A **natural renewal** and its failure, Stripe's **retry schedule** (Smart Retries) and its
  final action: the claimable sandbox has no test clocks. `fail-renewal` is not a
  substitute (with flexible billing the reset invoice can be EUR 0, observed).
- `ConcurrentWebhookTests` (row lock, simultaneous copies) run only on PostgreSQL: CI, not
  this machine.
- No public webhook endpoint (only `stripe listen`).

### Verification

`PYTHON=.venv/Scripts/python bash ops/verify.sh`: exit 0, all 11 steps (2026-10-30): lint;
format; mypy strict (incl. `ops/stripe_sandbox`); 357 engine/runner/ML tests (2 opt-in
real-model tests skipped); 99 web tests on SQLite (2 skipped: the PostgreSQL-only
`ConcurrentWebhookTests`); migrations check; deploy check; pip-audit no known
vulnerabilities; detect-secrets on tracked files (re-run after staging the new files);
wheel 45 files, 0 problems; lab demo exact match. CI result for the pushed commits: see
PR #1.

### Machine state after this session

Stack and listener stopped; ports 8000/8800/8801 free; no `stripe.exe` running. Sandbox
test orgs `owner-pc1…pc5@example.test` in `.local/stripe-e2e.sqlite3`. Logs
`.local/stripe/stack3-5.log`, `listen3-6.log` (the listener logs contain the webhook secret:
git-ignored, do not share). `.local/stripe/stripe.env.bak` is a copy taken before `setup`
(identical ids).

## Stripe sandbox verification (2026-10-30, third session)

Billing (package 6) ran end to end against a real Stripe test environment. Test payments only;
no live key, purchase, AI call, deployment or merge. Fix commit: `710f4fe`.

### Setup (owner-approved; no Stripe account was registered)

- Stripe CLI 1.55.3 installed with `npm install -g @stripe/cli@1.55.3` (the package is published
  by Stripe from `github.com/stripe/stripe-cli`; npm skipped its postinstall script, the binary
  works). Only CLI calls with an explicit `--config` file and `-p accessguard-sandbox` touched
  credentials. The default `%USERPROFILE%\.config\stripe` held no prior login; `--help` created
  an empty docs cache there, which was removed again.
- `stripe sandbox create --from-git --non-interactive` (git identity of this repository)
  created a **claimable sandbox**, account `acct_1UWAdYB9QLrE2rSI`, **expires 2026-11-06**.
  It issues a restricted key with the prefix `rkcs_test_`.
- The command output (keys and claim link), the CLI config and the app's env file are in
  `.local/stripe/` (git-ignored; the folder's ACL grants only the Windows user `krisr`):
  `sandbox-create.out`, `config.toml`, `stripe.env`. Do not print or commit them.
- Created by API, all `livemode: false`: products `prod_VXHeVVyQ0QQDk9` (Team) and
  `prod_VXHecucyp9KtJt` (Business); prices `price_1UWD5XB9QLrE2rSIaX4lxei7` (EUR 49/month,
  lookup key `accessguard_team_monthly_eur`) and `price_1UWD5YB9QLrE2rSIVnnFhrbI` (EUR 199/month,
  `accessguard_business_monthly_eur`); Customer Portal configuration
  `bpc_1UWD5ZB9QLrE2rSICEGY6Xch` (cancel at period end, switch between the two prices, payment
  methods, invoice history). It became the sandbox's default because none existed; the app
  passes it explicitly anyway.
- Webhooks: `stripe listen --events <the nine handled types> --forward-to
  http://127.0.0.1:8000/webhooks/stripe/` (CLI 1.55 requires `--events`); API version
  `2026-09-30.endive`, the same as the pinned SDK. Its signing secret went from
  `stripe listen --print-secret` straight into `stripe.env`.
- App: `ops/dev/run_stack.py --env-file .local/stripe/stripe.env` (new flag) with a **separate
  database** `.local/stripe-e2e.sqlite3`; the dev database `.local/dev.sqlite3` was not used.
  Proof that the running server loaded the configuration: it printed the five variable names,
  the billing page showed the test-mode banner, and it created real `cs_test_` Checkout
  Sessions with the configured prices.

### Results (evidence: Stripe events delivered by `stripe listen`, `StripeEvent` rows, database state and pages of the running app)

| Scenario | How | Result |
| --- | --- | --- |
| Checkout and webhooks | Playwright in headless Chrome: sign-up, "Choose Team" (Org A) and "Choose Business" (Org B), hosted Checkout with card 4242 | Each purchase: `checkout.session.completed`, `customer.subscription.created`, `invoice.paid`, `invoice.payment_succeeded`, all HTTP 200 and stored. Org A `team`/`active`, Org B `business`/`active`, own customer and subscription ids |
| Plan limits | projects created through the running app | Org A (Team) created a 2nd project; an untouched Free organisation was refused its 2nd ("Your Free plan includes 1 projects") |
| Customer Portal | "Manage payment method…" in the app | Stripe's `billing_portal.session.created` shows `configuration = bpc_1UWD5Z…`; portal lists plan, payment method, paid invoice |
| Failed payment | card `pm_card_chargeCustomerFail`, then Team → Business with `always_invoice` and `allow_incomplete` | invoice EUR 150 `open`, `customer.subscription.updated` (`past_due`) and `invoice.payment_failed`: grace started, plan kept, warning "stays active until 2026-11-06" |
| Grace period | 3rd project during grace; then `past_due_since` moved 8 days back **in the test database only**, then restored | during grace: created. Boundary: 7 days → paid plan, 7 days + 1 s → Free. After expiry: page shows Free with the "grace period has ended" banner, 4th project refused, existing projects kept |
| Recovery | card 4242 attached, open invoice paid | `invoice.paid` and `subscription.updated` (`active`): `past_due_since` cleared |
| Cancellation | "Cancel subscription" in the portal UI; later "Don't cancel" and cancel again | **defect** (below); after the fix the app shows "cancelled and ends on 2026-11-30", plan kept; reactivation cleared it |
| Access after the end | `cancel_at` moved to 90 s ahead; Stripe ended the subscription itself | `customer.subscription.deleted`: Org A `free`/`canceled`, page shows Free, new project refused, its 3 projects kept |
| Re-subscription | Org A bought Team again | same customer `cus_VXHmYk…` reused, new subscription, `team`/`active` (ordering guard did not block it) |
| Repeated deliveries | `stripe events resend` of `invoice.payment_failed` (Org A, then active), `checkout.session.completed` and `customer.subscription.created` (Org B) | delivered again with HTTP 200; every field of every organisation unchanged (incl. `updated_at`, audit-event counts), 19 → 19 `StripeEvent` rows; Org A stayed `active` |
| Organisation isolation | all of the above | Org B's row did not change (`updated_at` 10:39:33) through about 20 events for Org A; the Free control organisation never changed; each organisation got its own customer and subscription |

### Defect found and fixed (`710f4fe`)

New subscriptions use `billing_mode: flexible`. The Customer Portal then cancels with
`cancel_at` (= period end) and leaves `cancel_at_period_end` **false**, so the app stored no
cancellation and showed no warning. Access was still correct (it ends on
`customer.subscription.deleted`). Fix: `Subscription.cancel_at` (migration `0002`, nullable,
applied to the populated test database), set by the webhook; the warning uses it.
Regression test `test_flexible_billing_cancellation_uses_cancel_at` failed before the fix.

Same commit: explicit portal configuration (`STRIPE_PORTAL_CONFIGURATION`); key classification
by mode segment (`rkcs_test_` is a test key) with refusal of every non-test key unless
`ACCESSGUARD_ALLOW_LIVE_PAYMENTS=true` (before, a live key only changed a banner); and
`run_stack.py --env-file`. Tests: `GatewaySafetyTests` (3), `tests/unit/test_run_stack_env.py`
(3).

### Limits of this run (not verified, or verified with a substitute)

- **No test clocks:** the claimable sandbox key gets `PermissionError` on test clocks. So the
  failed payment was an upgrade invoice, not a renewal, and the end came from a near
  `cancel_at`, not a natural period end. Stripe's own retry schedule and its final action
  (cancel or `unpaid`) after failed retries were not observed.
- The grace expiry relies on the app's clock; it was checked by moving `past_due_since` in
  the test database, not by waiting 7 days.
- Plan switching in the portal UI is configured but was not clicked.
- No webhook endpoint on a server: only `stripe listen` to localhost.
- The Playwright and SDK driver scripts were ad hoc (session scratchpad), not committed.

### Observations for the owner (no code changed)

1. **Out-of-order delivery happened for real:** `invoice.paid` arrived before
   `checkout.session.completed` and was recorded as "ignored: unknown customer". Harmless,
   because the subscription events carry the status. A possible improvement: resolve the
   organisation from `invoice.parent.subscription_details.metadata`.
2. **Failed upgrade payment:** with `allow_incomplete`, Stripe applies the new price even
   though the payment failed, so the organisation had Business during the grace period.
   Decide whether upgrades should use `pending_if_incomplete` (no upgrade until paid).

### State left behind

- The stack and `stripe listen` are stopped; ports 8000/8800/8801 are free.
- The sandbox keeps its test data until **2026-11-06**. Claiming it (`stripe --config
  .local/stripe/config.toml -p accessguard-sandbox sandboxes claim`) turns it into a Stripe
  account with full keys and test clocks: an owner decision. Otherwise it expires.
- `.local/stripe-e2e.sqlite3` holds the test organisations (`owner-a`, `owner-a2`,
  `owner-b@example.test`); delete it whenever convenient.

### Verification after the fix

`PYTHON=.venv/Scripts/python bash ops/verify.sh`: exit 0, all 11 steps (2026-10-30): lint;
format; mypy strict; 357 engine/runner/ML tests (2 opt-in real-model tests skipped); 82 web
tests; migrations check; deploy check; pip-audit no known vulnerabilities; detect-secrets 344
tracked files clean; wheel 45 files, 0 problems; lab demo exact match.

## Previous state (2026-10-30, V1 commercial beta, second session)

- Branch: `feature/v1-commercial-beta` (from `b94f5b7`), pushed to the **private** repository
  `kris7011/accessguard` (`origin`, SSH). **Draft PR #1** into `main`, open and not merged.
  Nothing is merged, published or deployed; `main` is unchanged.
- V1 plan and acceptance record (three axes: implemented / verified locally / blocked
  externally): `docs/milestones/V1.md`. At the end of this session: 9 of 12 accepted. Since
  the third session (above): **10 of 12**, billing (6) accepted for test mode.
- This session's commits (oldest first): `054abc8` rules-page form fix + browser-faithful
  tests, `5ca94fc` compose/image fixes, `0ccdd87` runner install text, `f223fd0` customer
  acceptance script, then the documentation commit after it.

### GitHub and CI (PR #1)

CI (`.github/workflows/test.yml`) runs on the pull request only for feature branches
(`d6a48a6`). It ran for the first time on this PR and found four defects that local Windows
runs could not show; each is fixed in its own commit:

| Run | Commit | Result | Failure and fix |
| --- | --- | --- | --- |
| 41030339469 | `d6a48a6` | 3 of 6 jobs failed | Ubuntu (3.11 and 3.13) rebuilt the committed ML dataset with another version, because `math.log1p` is not correctly rounded in every C library → correctly rounded `Decimal` ln (`737bc42`). mypy failed on Linux: Windows-only attributes behind `os.name`, and missing Playwright types → `sys.platform` and the locked e2e requirements (`10d6445`). The verify scripts showed only 15 log lines, hiding a second failure → full log for failed steps (`6473fa4`) |
| 41031684030 | `6473fa4` | 5 of 6 passed | Ubuntu/3.13: `test_deep_nesting_within_the_size_limit_is_invalid_json` got `not_an_object`. `json` stops deep nesting only at CPython's C recursion limit, which is above 5000 on Linux with 3.13 and below it on Windows and on 3.11, so the rejection category depended on the platform → explicit `MAX_NESTING_DEPTH` (32) in `parse_action`, boundary and in-string tests added, original test unchanged (`c93242e`) |
| 41033173820 | `c93242e` | **6 of 6 passed** | lint/types/audit/secrets/build; engine tests 354 (2 skipped) on Ubuntu 3.11, Ubuntu 3.13 and Windows 3.13; 78 web tests on PostgreSQL 17; container image builds |

The documentation commit after `c93242e` (this file) gets its own CI run; see the PR for its
result.

### Verified this session

| What | How | Result |
| --- | --- | --- |
| Distribution | `python -m build`; wheel and sdist into clean venvs made from the base interpreters (no repo `.venv`, no `PYTHONPATH`, cwd outside the repo) | wheel 45 files, 0 problems; sdist holds only the package. `accessguard_sampleapp`, `accessguard_web`, `accessguard_lab` not importable in the venvs |
| Customer journey with the installed runner | `ops/dev/customer_acceptance.py` (new control plane on a fresh SQLite file, sample app as the customer's API, forms over HTTP) | PASS with the wheel on CPython 3.12.7 and the sdist on 3.13.13: 30 checks, 10 findings, no key in the report, triage, fixed variant on the same origin, retest 10/10 resolved and verified |
| Docker compose | Docker Desktop 29.8 (WSL2) started for the run; project name `agverify`, throwaway env file in the scratchpad, override only for `env_file` and a loopback db port | build, migrations, `/healthz` `/readyz`, hashed static files, CSP, demo runner in compose, wheel runner on the host against the container (scan with 10 findings), scheduler sweep/purge/backup, restore into a separate database (exact counts and digest), 78 web tests on PostgreSQL 17.11. Test stack and volumes removed afterwards; images kept |
| Anthropic adapter | current API docs (claude-api skill) + the installed SDK 2.3.0 signature | request shape statically valid (`output_config.format`, `effort`, `fallbacks: "default"` with `server-side-fallback-2026-07-01`). **No real call** |

### Defects found and fixed

1. **Rules page unsavable in a browser** (`054abc8`): the empty record row's owner select had
   no blank option, browsers submit the first option, so the row failed validation. Adding a
   list or editing a record failed until the empty row was filled. The e2e test missed it
   because it posts hand-built rows.
2. **No `.dockerignore`** (`5ca94fc`): local SQLite files and caches would be baked into the
   web image. Allowlist added; images verified clean (no `*.sqlite3`, `__pycache__`, tests).
3. **Backups never worked in Docker** (`5ca94fc`): pg_dump 15 (bookworm) vs server 17. Images
   now on `python:3.13-slim-trixie` (postgresql-client 17).
4. **Scheduler died on a fresh install** (`5ca94fc`): started before migrations, `set -eu`,
   no restart policy. Now waits for a healthy web, restarts unless stopped, logs and retries
   failed jobs; its inherited gunicorn health check is disabled.
5. **Install text pointed at an unpublished PyPI name** (`0ccdd87`): replaced by the wheel
   install with a warning. The PyPI names were free on 2026-10-30, so anyone could take them.

### Blocked externally (exact missing item)

- **Stripe**: resolved in the third session with a claimable sandbox (above).
- **AI assistants, real call**: no `ANTHROPIC_API_KEY`. The local Ollama model was not run:
  the assistants use `num_ctx` 16384, free commit memory was 1.2-3.5 GB, and the machine
  crashed twice (below).
- **E-mail**: no SMTP settings. Needs an SMTP provider with STARTTLS on 587.
- **Server, domain, TLS, production backup drill**: owner decisions.

### Machine incidents (not caused by AccessGuard code, but caused during this work)

- Two unexpected reboots, 09:02 and 09:15 local time, both bugcheck `0x116`
  (`VIDEO_TDR_FAILURE`, parameter 3 `0xC000009A` = `STATUS_INSUFFICIENT_RESOURCES`). Dumps:
  `C:\Windows\Minidump\103026-10453-01.dmp` and `103026-15640-01.dmp`. Both happened while
  Docker Desktop's VM ran with about 1-2 GB free commit memory; `nvlddmkm` event 153 preceded
  the first. Older dumps from 26-10 and 29-10 exist, so the machine had this before.
- No repository or development data was lost (`.local/` untouched; all test data lived in the
  scratchpad and the `agverify` volumes).
- Docker Desktop was stopped at the end (`docker desktop stop`, `wsl --shutdown`). The
  `agverify-*` images (about 700 MB) remain; remove them with
  `docker image rm agverify-web agverify-scheduler agverify-demo` if not needed.

### Latest verification

- `PYTHON=.venv/Scripts/python bash ops/verify.sh`: exit 0, all 11 steps (2026-10-30, after the code commits): lint; format (244 files); mypy strict (213 files); 351 engine/runner/ML tests, 2 opt-in real-model tests skipped; 78 web tests; migrations check; deploy check; pip-audit no known vulnerabilities; detect-secrets 342 tracked files clean; wheel 45 files, 0 problems; lab demo exact match
- After the CI fixes (2026-10-30, `c93242e`): `ops/verify.ps1 -Steps lint,format,types,tests`
  passed locally (354 engine/runner/ML tests, 2 skipped); CI on the same commit: see above.
- Customer acceptance runs and the Docker results: see above, `docs/install.md` and
  `docs/operations.md` (drill record).

### Publishing review (done before the push; the repository is private)

- 342 tracked files at the time; 31 commits on this branch not in `main`; other local branches:
  `feature/m1-reproducible-foundation`, `feature/m2-adaptive-agent`, `main`.
- No `.env`, database, dump, key or report file is tracked (`.gitignore` covers `.env*`,
  `.local/`, `reports/`, `web/db.sqlite3`); `.env.example` holds placeholders only; the secret
  scan (detect-secrets on tracked files) is part of `verify.sh`.
- Worth a conscious decision before a **public** repository: `AccessGuard-Claude-Code.md` (the
  original Danish task prompt for Claude Code) and `ml/datasets/ds-b82628ce4c20c8e3/`
  (140 KB, built from the synthetic lab and sample app, no customer data).
- Dependencies unchanged this session (`requirements*.txt`, `web/requirements.txt` untouched;
  pip-audit is part of the gate).

### Next steps (each needs the owner)

See the prioritised owner list in the session report and `docs/launch-checklist.md`. In short:
review draft PR #1 (merge only on the owner's explicit approval); Stripe test
account (done in the third session with a sandbox); an Anthropic API key for the assistant verification; reserve the PyPI name; hosting
and domain; legal texts; design-partner conversations.

### Commands (no secrets)

```bash
# Git Bash on Windows (use ':' as separator on Linux/macOS)
PYTHON=.venv/Scripts/python bash ops/verify.sh          # all gates incl. web-tests, deploy-check
.venv/Scripts/python ops/dev/run_stack.py                # whole product on http://127.0.0.1:8000
PYTHONPATH=src .venv/Scripts/python ops/dev/browser_check.py --out reports/browser   # stack must run
# customer journey with a runner installed from the wheel in a clean venv (see docs/install.md)
.venv/Scripts/python ops/dev/customer_acceptance.py --runner-venv <venv> --out <new dir>
```

The stack writes only to `.local/` (git-ignored). Stop it with Ctrl+C; if it was started in
the background, stop the `python.exe` processes whose command line contains `run_stack.py`,
`runserver`, `accessguard_sampleapp` or `-m accessguard runner`.

### Known issues

- Git Bash heredocs with `\` or `'` in long documents can break; write such files with the
  editor tools or a script file.
- `ops/ci/secret_scan.py` scans tracked files only; `git add` before trusting it.
- `py -3.11` is registered on this machine but its `python.exe` is missing, so Python 3.11 is
  untested locally.
- This laptop cannot safely run Docker Desktop and a local model next to an editor and a
  browser (see the incidents above).

## Previous V1 session (2026-10-30, first): summary

Commits `4fd78c1` … `c37f9e5`: engine for authorised targets, runner, sample app, web control
plane, web tests, ML foundation, static assets, backup/restore, Docker/compose/CI files, ADRs
0009-0013, install/operations/checklist, business material, V1 docs. Gate then: 351 engine
tests (2 skipped), 74 web tests, wheel 45 files. Docker, PostgreSQL, Stripe and real AI calls
were not exercised; Docker and PostgreSQL now are (above).

## Archive: M2 state (2026-10-27, eighth M2 session)

- Branch: `feature/m2-adaptive-agent`.
- This session added four commits on top of `5b2d4b7`: the list-first adapter `f6d8d25`, the
  post-hoc control record `7a15505`, the pre-registration `a047f3c`, and the results commit
  after it (see git log).
- **Code changed this session** (first time since the fourth session): a new non-AI responder
  `scripted:list-first` plus tests. No prompt, fixture, engine, budget or limit changed.
- Nothing is pushed or merged.
- Milestone 2 status: **mock and contract verification, a passed real-model smoke test, a
  passed full opt-in test, and bounded real-model comparisons on all four fixtures at 25 % and
  50 % budgets** (all under runtime setup B).
  - **No demonstrated advantage of AI prioritisation over simple rules.** On `projects` the
    post-hoc `list-first` rule matches the model; on `helpdesk` and `clinic` the model was
    worse than the best simple baseline in three of four cells and equal in one.
  - The model's choices were identical across three separate server sessions.
- The full evaluation at 100 % budgets has not been run with the model.
- No Ollama or `llama-server` process is running: the three servers started this session were
  stopped, and port 11434 is not listening.
- M3 has not been started.

## Done in this session (2026-10-27): list-first control and bounded helpdesk/clinic evaluation

Full record: `docs/evaluation.md` → "Post-hoc list-first control on `projects` and `library`"
and "Bounded evaluation on `helpdesk` and `clinic` (pre-registered 2026-10-27)" (design,
results, conclusion); summary in `docs/milestones/M2.md`.

- **Adapter** (`f6d8d25`): `scripted:list-first` in `adapters/scripted_model.py`, registered in
  `RESPONDERS` and the CLI help (`entrypoints/agent_options.py`). Every pending list check
  first, then every other pending check, in listed order; reads only `kind` and `status` from
  the model-visible state; does not follow findings. Shared budget, retest reserve, deadline
  and safety limits unchanged.
- **Tests:** `tests/unit/test_scripted_model_and_planning.py` (`ListFirstResponderTests`:
  priority, stable order and no reaction to findings, executed checks never chosen again, a
  real `ModelView` state yields an alias only; plus the stop test) and
  `tests/integration/test_agent_lab.py` (full-budget verdicts equal the baseline for every lab
  profile; a 30-request run takes listings first in policy order, then items, nothing twice,
  budget respected, a last-request finding stays `not_retested`). 280 → 285 tests.
- **Post-hoc control** (`7a15505`, marked as chosen after seeing the model; not independent
  confirmation): list-first on `projects`/`library` at the bounded evaluation's budgets
  confirmed 2 and 5 findings on `projects` with the first at request 8, exactly the model's
  saved results; 0 false positives on `library`. The model was not rerun; the 24 baseline runs
  reproduced the saved ones.
- **Pre-registration** (`a047f3c`) before any run on `helpdesk`/`clinic`.
- **Runs:** `deny-first` and `list-first` (3 repetitions each, 3.8 s and 3.7 s), then three
  model sessions (fresh `ollama serve` each, setup B, warm-up 17.9 / 10.6 / 12.2 s, evaluations
  214.9 / 215.2 / 220.8 s, all exit 0). 177 model calls, all valid, `truncated = 0`,
  `prompt cache is disabled` in every log. The watchdog never intervened (free commit at least
  3118 MB; `llama-server` peak 4651 MB; VRAM 2295 MiB; free RAM at least 57 MB).
- **Resources before:** RAM 0.71 of 7.35 GB free; commit 7.85 of 28.52 GB free; VRAM
  3962 MiB free; C: 14.03 GB free. No program was closed.
- **Results** (confirmed findings; `helpdesk` 17/34, `clinic` 16/31): model 0, 1, 0, 0 in every
  session; best simple baseline 1 (random median), 5 (`deny-first`), 0, 2 (random median);
  `list-first` 0, 2, 0, 0. 0 false positives in all 96 scored runs. Model runs took 27-87 s.
- **Conclusion by the pre-registered rule:** no demonstrated advantage; worse in three cells.
  There is no gain, so there is no consistency or cost-benefit case for the model time.
- **Kept locally (git-ignored):** `reports/eval-list-first-2026-10-27/` (post-hoc control,
  `compare_posthoc.py`) and `reports/eval-hc-2026-10-27/` (`deny-first/`, `list-first/`,
  `model-s1..3/` with raw reports, server logs, warm-up, memory CSVs, start/stop records;
  `analysis.md`; scratch scripts `session.sh`, `save_reports.py`, `watchdog.py`, `analyze.py`).
- **Docs:** `evaluation.md`, `M2.md`, `README.md`, `PROJECT_WALKTHROUGH.md`,
  `interview-prep/M2.md` (new addendum), this file.
- No prompt optimisation, no fixture change, no downloads, no paid services, no external
  scans, no push, no merge.

## Earlier session (2026-10-27): bounded comparative real-model evaluation

Full record: `docs/evaluation.md` → "Bounded comparative real-model evaluation
(pre-registered 2026-10-27)". In short:

- Pre-registered (`00f860f`); `projects` and `library` at 25 % and 50 %; deterministic,
  seeds 1-5, `deny-first` ×3, model ×3 in one server session; setup B; exit 0, 415.3 s,
  99 valid model calls.
- `projects`: the model confirmed 2 / 5 at 12 / 23 requests in every repetition, against
  deterministic 0 / 0, `deny-first` 0 / 1 and a random median of 1 / 2. `library`: 0 false
  positives everywhere. By its rule: an advantage per request, limited.
- Explained by a "listings first" preference (5 of 7 `projects` flaws are listing checks), which
  this session's post-hoc control confirmed.
- Kept locally: `reports/eval-real-bounded-2026-10-27/`.

## Earlier session (2026-10-27): prompt-cache fix and full-test rerun

Full record: `docs/milestones/M2.md` → "Runtime setups", "Control series with setup B" and
"Full opt-in real-model test, rerun with the prompt cache disabled". In short:

- **Supported setting found and verified, nothing installed:**
  - The installed `llama-server.exe --help` documents `--cache-ram N` (env
    `LLAMA_ARG_CACHE_RAM`, 0 disables it). The llama.cpp server README says an argument takes
    precedence over its env var.
  - The Ollama v0.40.11 source (`llm/llama_server.go`) copies `os.Environ()` into the child.
    For chat models it passes no `--cache-ram`.
  - `ollama serve --help` does not list the variable.
  - The child's log then confirmed the setting: `prompt cache is disabled` and
    `--cache-idle-slots requires --cache-ram, disabling`, with no prompt saves.
- **Runtime setup B** = the smoke-test settings plus `LLAMA_ARG_CACHE_RAM=0`, set only in the
  environment of the `ollama serve` process (command below). Model, digest, prompt `agent-v1`,
  client settings and every AccessGuard limit are unchanged. Nothing persistent was changed.
- **Control series** (10 calls through the test's own `agent_scan`, 600 s):
  - free commit stayed flat at about 4.1 GB after the load, and `llama-server` private memory
    at 4638-4644 MB;
  - latency was flat at about 3.8 s (setup A rose from 4.3 to 15 s);
  - 10 valid choices, 0 invalid, all matching the answer key.
- **Full opt-in test** (`unittest tests.e2e.test_real_model`, unchanged; same watchdog rule,
  300 MB free commit): **both tests OK, exit 0, 312.1 s.**
  - Agent `completed`; 56 valid choices, 0 invalid; 56/56 checks executed, 0 skipped.
  - 23 FAIL (all reproduced) and 33 PASS: an exact match with the answer key (0 false positives,
    0 false negatives). The deterministic comparison gave the same 56 outcomes.
  - 92 of 500 HTTP requests; model time 294.12 s of the 294.9 s agent scan.
  - `prompt_eval_count` was at most 4381 of 8192, with `truncated = 0`.
  - Memory: `llama-server` at 4637-4801 MB; free commit at least 2.73 GB (an unattributed
    drift of about 1 GB, not from the model server); free RAM at least 3 MB; VRAM peak 2299 MiB.
  - The smoke test also passed again (3 valid choices).
- **Observation:** this session's smoke run chose different checks than the morning's smoke
  run, although the first prompt length was the same (2965 tokens). The cause is not
  established. Repeatability is for the evaluation to measure.
- **Resources before:** RAM 0.81-1.28 GB free; commit 8.15-8.53 of 27.78 GB free; VRAM
  3962 MiB free; C: 13.46 GB free. No program was closed.
- **Kept locally (git-ignored):** `reports/real-model-cram0-2026-10-27/` (`control/`, `full/`,
  scratch scripts). The previous attempt's directory is untouched.
- **Docs:** `M2.md`, `evaluation.md`, `README.md`, `PROJECT_WALKTHROUGH.md` (status lines),
  this file.
- **Permission note:** a PowerShell launch with `-ExecutionPolicy Bypass` was refused by the
  session's permission check. The server was started from Git Bash instead.

## Earlier session (2026-10-27): full opt-in real-model test attempt, stopped for memory

Full record: `docs/milestones/M2.md` → "Full opt-in real-model test (2026-10-27): stopped for
memory". In short:

- **Docs first:** recorded the smoke test in `M2.md` and `evaluation.md` (commit `01fb104`).
- **Resources before the run:** RAM 0.70 GB of 7.35 GB free; commit 4.53 GB free of
  24.63 GB; VRAM 3962 MiB free; C: 15.13 GB free. No program was closed.
- **Run:** protocol step 2 (`unittest tests.e2e.test_real_model -v`, both tests, unchanged).
  It used the same Ollama 0.40.11, model, server settings and client settings as the smoke
  test, on loopback only, with no downloads. A scratch wrapper saved each report. A watchdog
  sampled memory every 2 s and was allowed to stop only the Ollama server below 300 MB free
  commit.
- **Result:** `Ran 2 tests in 121.431s ... FAILED (failures=2)`.
  - The watchdog stopped the server at 110 s (214 MB free commit). The agent ended with
    `model_failed` after 12 valid `run_check` and 0 invalid replies.
  - 12 of 56 checks executed, 44 skipped; 3 FAIL (all reproduced) and 9 PASS. All 12 match the
    hand-written answer key, with 0 false positives; 20 of 23 expected failures were never
    reached.
  - 24 HTTP requests; 108.49 s model time; 117.2 s agent scan.
  - No truncation: `truncated = 0` for every call, at most 3255 tokens of 8192.
  - The smoke test ran second and failed only because the server had been stopped.
- **Cause:** the `llama-server` child of Ollama 0.40.11 keeps a host prompt cache by default
  (limit 8192 MiB). It saved each previous prompt (419-470 MiB) and reached 12 prompts /
  5263.75 MiB at the stop. It reused only about 186 tokens per prompt. Not an AccessGuard
  defect:
  - against the fake Ollama server, the Python side grows about 0.03 MB per call;
  - the other programs did not grow;
  - committed memory returned to 19.97 GB after the stop.
- No code, assertion or limit was changed.
- **Kept locally (git-ignored):** `reports/real-model-full-test-2026-10-27/`, with the partial
  reports, the server log, the memory CSV, the score output and the scratch scripts
  (`run_full.py`, `watchdog.py`, `score.py`, `python_side_memory.py`).
- **Docs:** `M2.md`, `evaluation.md`, `README.md` (status lines; also fixed a broken
  walkthrough anchor), `PROJECT_WALKTHROUGH.md` (status lines), this file.
- **Side effect:** Windows grew the system-managed page file (21 GB allocated, C: free
  15.1 → 12.0 GB).

## Earlier session (2026-10-27): first real-model smoke test

**Resources, measured before installing.**

| Resource | Value |
| --- | --- |
| RAM | 7.35 GB visible, 0.67-0.79 GB free. Commit 4.6 GB free of 24.6 GB; the page file is system-managed |
| VRAM | 3962 MiB free of 4096 MiB (`nvidia-smi`). Ollama itself reported 3.2 GiB available |
| Disk C: | 20.4 GB free before, 15.9 GB after install and model |

The biggest RAM users were VS Code (about 4.6 GB private) and Chrome (about 4.5 GB). No
program was closed. The first `nvidia-smi` call hung (killed after 20 s), and a later one
answered in 0.4 s. Probably the dGPU was waking from a power-saving state; this is not verified.

**Sources checked (2026-10-27).** These are documented facts:

- [Ollama model page](https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M): tag exists,
  2.5 GB, ID `0edcdef34593`, Apache 2.0, 4.02B parameters, Q4_K_M. Capabilities "tools" and
  "thinking". The page states no context length.
- [Model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507): Apache 2.0, 36 layers,
  GQA 32 Q / 8 KV heads, native context 262,144. Non-thinking only, no `<think>` blocks.
- [Ollama Windows docs](https://docs.ollama.com/windows): Windows 10 22H2+, NVIDIA driver
  551.61+ (installed: 610.62), at least 4 GB for the binary install. A standalone
  `ollama-windows-amd64.zip` exists.
- [Ollama FAQ](https://docs.ollama.com/faq): binds 127.0.0.1:11434 by default. Default context
  4096; `num_ctx` in the API options overrides it.
- The agent's required context of 8192 is the project's own setting. Ollama documents no RAM or
  VRAM requirement for this model. Those numbers were my estimates (see `docs/milestones/M2.md`)
  and were checked against the measured run below.

**Install (no installer, no autostart, no tray app, no persistent environment variables).**

- Source: the official GitHub release `ollama/ollama` **v0.40.11** (published 2026-10-24),
  asset `ollama-windows-amd64.zip`, 1,586,356,606 bytes.
- The sha256 `bda4a493ba8a7c24d57263cd843cd68cee84f44aed4299eeed532acad7eda12a` matched both
  `sha256sum.txt` and the GitHub API digest.
- Extracted to `%LOCALAPPDATA%\Programs\ollama-standalone\v0.40.11` (2.08 GB); the zip was
  deleted afterwards.
- Model store: the default `%USERPROFILE%\.ollama\models`.
- The server was started by hand for the run only:
  `OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve`.
- `netstat` showed it listening on `127.0.0.1:11434` only. It was stopped after the run, and no
  Ollama process is left running.

**Model.**

- Pulled `qwen3:4b-instruct-2507-q4_K_M`; Ollama verified the sha256 during the pull.
- Digest `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`, 2,497,293,803
  bytes, gguf, qwen3, 4.0B, Q4_K_M, Apache 2.0.
- `ollama show` reports a context length of 262144 and capabilities tools/thinking/completion.
- The model defaults (temperature 0.7, top_p 0.8, top_k 20) are overridden by the client's
  temperature 0.

**Smoke test.**

- Command: the existing
  `tests.e2e.test_real_model.RealModelAgentTests.test_smoke_a_few_bounded_model_calls`,
  unchanged, run in the CPython 3.13.13 venv with `ACCESSGUARD_REAL_MODEL` set.
- A scratchpad wrapper (not in the repo) only saved the report the test produced.
- Result: **`Ran 1 test in 80.961s ... OK`**.

| Item | Result |
| --- | --- |
| Runtime | Ollama 0.40.11, CUDA backend (`cuda_v13`), RTX 3050 Laptop, compute 8.6 |
| Settings | temperature 0, seed 0, `num_ctx` 8192, `num_predict` 64, endpoint `http://127.0.0.1:11434`; prompt `agent-v1`, tool contract `agent-tools-v1` |
| Limits | 3 model calls, 3 invalid replies, 120 s per call, 600 s per run, 512 output chars; HTTP budget 500 requests (the test's default) |
| Provenance | all 7 keys reported (`ollama_version`, `digest`, `modified_at`, `format`, `family`, `parameter_size`, `quantization_level`); no metadata error |
| Valid `run_check` | **3 of 3**: `list:documents@initech-admin`, `list:documents@initech-member`, `list:documents@anonymous` |
| Invalid replies | **0** (each reply was 15 output tokens, `done_reason: stop`; no `<think>` output) |
| End status | `model_calls_exhausted` (expected at 3 calls; not a failure status). The run is `interrupted: model call budget exhausted`, so 53 of 56 checks were skipped |
| Verdicts | 2 FAIL (both `reproduced` on retest), 1 PASS; overall `findings`, coverage 3/56 |
| HTTP requests | **15** of 500. Per the report's counting rule this includes session probes, owner baselines and retests; the breakdown was not inspected |
| Model time | **80.65 s** of 80.95 s wall time. Call 1: 68.6 s, including 41.1 s model load; calls 2 and 3: 5.9 s and 6.2 s |
| Context | `prompt_eval_count` 2965, 2992, 3018 of 8192. The server log shows `truncated = 0` for every call |
| Timeouts | none (the slowest call took 68.6 s of the 120 s limit) |

**Memory during the run.** There was no out-of-memory error and no WARN or ERROR line in the
server log; the "llm server error" status during load is the normal start-up wait.

- Ollama placed **21 of 37 layers on the GPU**: 1455 MiB of weights on CUDA and 1225 MiB in host
  RAM. The KV cache is 1152 MiB (640 MiB on GPU, 512 MiB on CPU), which matches the earlier
  144 KiB/token estimate exactly.
- VRAM in use peaked at 2291 MiB. Ollama's fit step reported 1112 MiB left free on the GPU;
  why it placed no more layers there was not investigated.
- Free RAM fell to 435 MB and free commit to **421 MB at the lowest**. Commit is the tightest
  resource: a larger context or other memory-hungry programs could cause allocation failures.

**What it shows:**

- The real integration works end to end: the loopback-only adapter, a JSON schema-constrained
  reply, alias validation, the deterministic verdicts, retests and provenance.
- This model, at these settings, produced schema-valid choices without thinking tokens.

**What it does not show:**

- Nothing about prioritisation quality: three choices are no sample, and there is no comparison
  with the deterministic or seeded-random baselines.
- Nothing about behaviour later in a run, with larger states (up to about 14k characters).
- Nothing about repeatability across repetitions or the full-run duration.
- Not whether the two FAIL findings match the lab's answer key. The smoke test does not score
  that, though both reproduced on retest.

`bash ops/verify.sh` was not rerun because no code changed (last run: exit 0, see below).

### Earlier session (2026-10-27), retest reserve, in `9bd4846`

- **Committed the staged real-model readiness work** as `735d118` (unchanged; it had only
  been staged last session).
- **Fixed: budget-limited runs never confirmed findings.** The scan spent the whole request
  budget before the separate retest phase, so retests were refused and every finding stayed
  `not_retested`. The runner now holds back one request per finding (a finding's retest costs
  exactly one request, since its baseline and session are cached) and releases the reserve for
  the existing retest phase. The policy lives in the shared `CheckRunner`/`RequestGate`, so it
  is identical for the deterministic order, seeded random and the agent. Model input and model
  choice are unchanged, so the budget effect is isolated. ADR 0008 (2026-10-27 amendment)
  records the decision and the rejected alternatives (immediate inline retest; fixed up-front
  reservation). Files: `application/gate.py`, `application/runner.py`, `application/agent.py`
  (one comment).
- **Tests** for a limited-but-sufficient budget (confirmed), an insufficient budget (honest
  `not_retested`, no overrun), a finding that does not reproduce on retest, cancellation during
  the retest, the gate reserve primitive, and identical reserve rules across strategies. Files:
  `tests/unit/test_runner.py`, `tests/unit/test_gate.py`, `tests/unit/test_agent_loop.py`,
  `tests/integration/test_agent_lab.py` (updated to assert confirmation within budget).
- **Docs:** ADR 0008 amendment, `docs/evaluation.md` before/after section, this handoff.

### Previous session (2026-10-26), now in `735d118`

| Area | Files |
| --- | --- |
| Opt-in real-model tests: now fail on `invalid_replies_exhausted` and require at least one accepted `run_check`; new bounded smoke test (3 calls, 120 s per call, 600 s per run) | `tests/e2e/test_real_model.py` |
| Evaluation: `--agent-repeats N` (every repetition scored), `--max-run-seconds` (the same for every strategy), per-run `confirmed_true_positives`, `model_seconds`, `selection_digest`; per-row `agent_statuses`, `distinct_selection_orders` | `evaluations/accessguard_eval/harness.py`, `cli.py` |
| Provenance: `ModelDescriptor.provenance_keys`; the evaluation writes `"unknown"` for keys the provider did not report, plus metadata errors | `application/ports.py`, `adapters/ollama_client.py`, `harness.py` |
| Scripted `deny-first` reference adapter (not AI) | `adapters/scripted_model.py`, `entrypoints/agent_options.py` |
| Tests for the above | `tests/integration/test_evaluation.py`, `tests/unit/test_scripted_model_and_planning.py`, `tests/e2e/test_agent_cli.py` |
| Docs | `docs/milestones/M2.md` (readiness review, machine capacity, model recommendation), `docs/evaluation.md` (new results, information basis, real-model protocol), ADR 0008 amendment, README, walkthrough, interview prep |

## Not done

- **Full real-model evaluation.** Bounded comparisons now cover all four fixtures at 25 % and
  50 %. The full evaluation (100 % budgets) has not run with the model. Ollama and the model are
  installed.
- The smoke-test report itself was saved only in the session scratchpad and is not kept; the
  numbers above (and in `docs/milestones/M2.md` → "Real-model smoke test") are its record.
- `agent-v2` (more anonymous structure in the model state) is proposed, not implemented. Measure
  `agent-v1` with a real model first.
- GitHub Actions not run (nothing pushed); Python 3.11, Linux and macOS not verified.

## Commands

```bash
export PYTHONPATH="src;lab;evaluations"          # Git Bash on Windows (use ':' on Linux)
PYTHON=.venv/Scripts/python bash ops/verify.sh    # all quality gates
.venv/Scripts/python -m accessguard_eval run --model scripted:deny-first --agent-repeats 3 --out reports/evaluation-deny-first
.venv/Scripts/python -m accessguard_eval run --model scripted:list-first --agent-repeats 3 --out reports/evaluation-list-first
```

One model-server session (start, watchdog, warm-up, evaluation, stop), as used in the eighth
session: `bash reports/eval-hc-2026-10-27/session.sh <k>` (a git-ignored scratch script; it
starts the server with setup B from Git Bash and stops only the process it started).

For the real-model sequence (smoke test → full test → subset evaluation → full evaluation),
see `docs/evaluation.md` → "Real-model protocol". Start the local server first, loopback
only, with **runtime setup B** (prompt cache disabled). It is not autostarted. Stop it with
Ctrl+C when done. While the model is loaded, it holds about 2.3 GB of VRAM, and `llama-server`
holds about 4.6-4.8 GB of private memory (measured in setup B).

```bash
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 LLAMA_ARG_CACHE_RAM=0 \
  "$LOCALAPPDATA/Programs/ollama-standalone/v0.40.11/ollama.exe" serve
```

After the first model call, check that the server log contains
`prompt cache is disabled - use --cache-ram N to enable it`.

## Latest actual results

- Bounded `helpdesk`/`clinic` evaluation (2026-10-27, eighth session, setup B, three server
  sessions, each exit 0): 177 valid model calls, 0 invalid. Confirmed findings, model vs best
  simple baseline: `helpdesk` 17: 0 vs 1; `helpdesk` 34: 1 vs 5; `clinic` 16: 0 vs 0;
  `clinic` 31: 0 vs 2. **No demonstrated advantage** (worse in 3 of 4 cells). Identical choices
  across sessions. 0 false positives.
- Post-hoc list-first control on `projects` (eighth session, no model run): confirmed 2 / 5 at
  12 / 23 requests, equal to the model's saved results.
- Bounded comparative real-model evaluation (2026-10-27, this session, setup B): exit 0;
  99 valid model calls, 0 invalid.
  - `projects`, confirmed findings at 12 / 23 requests: model 2 / 5 (every repetition);
    deterministic 0 / 0; `deny-first` 0 / 1; random median 1 / 2.
  - `library`: 0 false positives.
  - Model runs took 12-73 s, against hundredths of a second for the others.
  - By the pre-registered rule: advantage per request, limited. Details are above.
- Real-model full opt-in test (2026-10-27, setup B): **passed**, both tests OK.
  56 valid choices, 0 invalid; 56/56 checks; 23 reproduced FAILs; exact match with the answer
  key; 92 HTTP requests; 294.12 s model time. Details are above.
- Real-model full opt-in test, first attempt (2026-10-27, setup A): **stopped for memory, not
  passed.** 12 valid choices; 12/56 checks; all matching the answer key.
- Real-model smoke test (2026-10-27): **passed**, 3/3 valid `run_check`, 0 invalid replies,
  15 HTTP requests, 80.65 s model time. Details are above.

### Quality gates

- Rerun 2026-10-27 at the end of the eighth session (after the code changes and every
  documentation change except this file): `PYTHON=.venv/Scripts/python bash ops/verify.sh` exit 0.
  - **285 tests run, 283 passed, 0 failed, 2 skipped** (the real-model tests; the variable is
    not set in `verify.sh`). 280 → 285: +4 list-first unit tests and +1 list-first lab test;
    two existing tests were extended to include list-first.
  - mypy: no issues in 91 files. detect-secrets: 136 tracked files clean. Wheel: 40 files,
    0 problems. Demo: exact match. pip-audit: no known vulnerabilities.
  - The same gate also passed after the adapter commit, before the documentation changes.
- Rerun 2026-10-27 after the seventh session's documentation changes:
  `PYTHON=.venv/Scripts/python bash ops/verify.sh` exit 0.
  - 280 tests run, 278 passed, 0 failed, 2 skipped (the real-model tests, since the variable
    is not set in `verify.sh`).
  - mypy: no issues in 91 files. detect-secrets: 136 tracked files clean. Wheel: 40 files,
    0 problems. Demo: exact match. pip-audit: no known vulnerabilities.
- Rerun 2026-10-27 after the sixth session's documentation changes:
  `PYTHON=.venv/Scripts/python bash ops/verify.sh` exit 0. 280 tests run, 278 passed,
  0 failed, 2 skipped (the real-model tests, since the variable was not set in `verify.sh`;
  their real-model pass is the separate run above, not this gate). mypy reported no issues in
  91 files, detect-secrets found 136 tracked files clean, the wheel had 40 files and
  0 problems, the demo was an exact match, and pip-audit found no known vulnerabilities.
- Rerun 2026-10-27 after the fifth session's documentation changes:
  `PYTHON=.venv/Scripts/python bash ops/verify.sh` exit 0. 280 tests run, 278 passed,
  0 failed, 2 skipped (the real-model tests; the variable was not set). mypy reported no
  issues in 91 files, detect-secrets found 136 tracked files clean, the wheel had 40 files
  and 0 problems, and the demo was an exact match.

Previous run (2026-10-27, after the retest reserve):

- `bash ops/verify.sh` (CPython 3.13.13 venv): exit 0.
  - Lint, format and types: mypy strict, 91 files, no issues.
  - Tests: **280 run, 278 passed, 0 failed, 2 skipped**. Both skipped tests are the real-model
    tests, and a skipped test is not a verification. (273 → 280: +7 targeted tests for the
    reserve; one existing budget-limited agent test was updated to assert confirmation.)
  - pip-audit: no known vulnerabilities. detect-secrets: clean. Wheel: 40 files, 0 problems.
    Demo: exact match.
  - One earlier run failed on a mypy error in a new test's annotations; fixed before the rerun.
- Evaluation before/after (`scripted:follow-up`, `--agent-repeats 3`, 108 runs), full table in
  `docs/evaluation.md`:
  - **Confirmed findings at partial budgets rose from 0 to the number confirmable in budget**
    for every strategy; full-budget rows and the flaw-free `library` fixture are unchanged.
  - **No run exceeded its budget** (max `requests_used - budget` over 108 runs is 0).
  - Drawback: a small coverage/candidate drop at partial budgets, because confirming a finding
    spends a request that would otherwise probe another check (e.g. `helpdesk` 50 %
    deterministic: 5 unconfirmed candidates became 4 confirmed findings).
  - 0 false positives and 0 invalid replies throughout. Scripted adapters still show no
    advantage over seeded random (unchanged conclusion).
- Previous session (`735d118`): 273 run, 271 passed, 0 failed, 2 skipped.

## Known issues and limitations

- Python `Path.write_text` on Windows writes CRLF; write files with LF (`.ps1` stays CRLF per
  `.gitattributes`).
- `detect-secrets` (`ops/ci/secret_scan.py`) scans only tracked files: stage new files before
  trusting the secrets step.
- Long Markdown heredocs in Git Bash can fail to parse; use the editor tools for documents.
- See `docs/milestones/M2.md` "Not done or not verified".

## Next step

Every further experiment needs the owner's go-ahead. None was started, and no prompt
optimisation was started. The bounded results give no reason to expect an AI advantage at
these settings. Options to decide on:

1. Stop the real-model comparison for M2 here and record M2 as complete with a negative
   prioritisation result: the agent's safety and contract guarantees are verified, and its
   prioritisation adds no measured value with this model.
2. If AI prioritisation is still of interest: `agent-v2` (more anonymous structure in the
   state; see `docs/evaluation.md` → "Information basis") or a larger model, each as a new
   pre-registered comparison against the same simple rules. Ideally use fresh fixtures, since
   the current four have now been seen.
3. The full evaluation at 100 % budgets. It mostly checks that every strategy converges and
   cannot show a prioritisation advantage.

M3 (a separately authorised test application through configuration and an explicit policy)
needs a separate go-ahead.
