# Verifying the AI assistants with a real model (opt-in)

Status: **real-model run pending.** Everything below except the paid run has been executed
(see docs/HANDOFF.md). Nothing here runs in `ops/verify.sh` or CI unless you opt in.

## What is tested

`web/accessguard_web/tests/test_assistant_flows.py` drives the two existing flows through
the real pages (Django test client), the provider port and the stored `AssistantRun`:

1. **Setup assistant:** a small synthetic OpenAPI document is imported with "Let the setup
   assistant suggest…" ticked.
2. **Finding explainer:** the finding `orbit-billing@ben` from a real scan of the synthetic
   sample app (in-process, loopback) is explained, with a short handler snippet.

| Requirement | How the test checks it |
| --- | --- |
| A real call through the app's adapter | `AnthropicProvider` is called by the views via `run_task`; the SDK call is wrapped to count and capture it |
| Answer validated and shown | outcome `ok`, validated result stored; the page shows the model, prompt version, summary, every fix step, the AI reasons |
| Suggestions need acceptance; scope unchanged | after the draft the project's config is byte-identical; applying one chosen template adds only that template, `target` and `policy` stay unchanged; every path in the draft exists in the document |
| No credentials or protected data sent | everything sent to the port and to the SDK is searched for the sample app's API keys, every protected value (account refs, plan notes, budget codes, project names), canaries placed in the OpenAPI document (`servers`, `securitySchemes`, `example`, `description`) and the Anthropic key itself |
| Provider, model, tokens, latency, outcome recorded | asserted on `AssistantRun`, also for a cut-off answer; the live run appends one line per call to `.local/ai-live/runs.jsonl` (git-ignored) with every model attempt and an estimated cost |
| Invalid answers, timeout and refusal shown honestly | invalid answer and refusal through the pages with a fake provider; a timeout through the real SDK against a local server that never answers; a real `401` from the Claude API with an invalid key; a real `max_tokens` cut-off (paid). None is replaced by canned text |

Opt-in levels (`ACCESSGUARD_AI_LIVE`):

| Value | Runs | Cost |
| --- | --- | --- |
| unset | fake-provider flows, failure pages, timeout via the real SDK | none, offline |
| `free` | plus one request to the Claude API with an invalid key (expects `401`) | none |
| `paid` | plus three real model calls (setup, explanation, cut-off); needs `ANTHROPIC_API_KEY` | see budget |

A guard fails the run before a fourth logical SDK call (`MAX_PAID_CALLS`). It counts calls
to `messages.create`; the SDK's retries and a refusal fallback happen inside one call and
are not counted by it (see Budget).

Not covered by the real-model run: a classifier **refusal** cannot be provoked on purpose
with acceptable content, so it is tested with a fake provider and the adapter's mocked
SDK response. Invalid structured output cannot be forced from a real model either (the API
enforces the schema); length and count rules are tested with the fake.

## Model and price (checked 2026-10-30)

Proposed model: **`claude-opus-5-5`**, the model the app uses by default
(`assistant/providers.py: ANTHROPIC_MODEL`), so the run verifies the shipped configuration.
Checked on the official documentation the same day:

- Price: USD 4 per million input tokens, USD 20 per million output tokens
  (platform.claude.com/docs/en/about-claude/pricing). Thinking is always on for this model
  and is billed as output.
- Structured outputs (`output_config.format`, JSON schema): generally available for
  `claude-opus-5-5`, no beta header. Our schemas use only supported keywords
  (`additionalProperties: false`, no length or numeric constraints).
- Server-side refusal fallback (`fallbacks: "default"` with beta
  `server-side-fallback-2026-07-01`): supported for `claude-opus-5-5` on the Claude API.
- `output_config.effort: "medium"` is a valid level (and this model's default).

`ACCESSGUARD_AI_MODEL=claude-sonnet-5-5` (USD 2 / USD 10) also works with the same request
shape; it would verify a configuration the app does not use by default.

## Budget

Three different things, kept apart:

- **Estimated usage:** what the run should cost (the table below) and, afterwards, what
  the evidence file estimates from the reported tokens at list price. An estimate, not a
  bill; the Console's cost page is authoritative.
- **Credited funds:** the balance the calls are paid from (API credits included in a
  Claude plan, or purchased credits). A balance is not a limit: every key in the
  Console organization draws from it.
- **Configured usage limits:** the workspace spend limit (and the organization's monthly
  spend cap). Only these stop requests, so only these enforce the approved budget.

Worst case per run, from the request limits and the prices above:

| Call | Input (estimate) | Output limit | Opus 5.5 only | With a fallback attempt |
| --- | --- | --- | --- | --- |
| Setup draft | ≤ 2,000 tokens | 8,000 (`max_tokens` in the view) | USD 0.17 | USD 0.38 |
| Explanation | ≤ 2,000 tokens | 6,000 | USD 0.13 | USD 0.29 |
| Cut-off check | ≤ 2,000 tokens | 16 | USD 0.01 | USD 0.02 |
| **One run** | | | **≤ USD 0.31** | **≤ USD 0.69** |

- **Fallback:** a refusal can add one attempt on another model inside the same call. The
  table prices it at Claude Opus 4.8 (USD 5 / 25 per million tokens), where `"default"`
  routes cyber-category refusals, and assumes the declined attempt was billed in full
  (one that declines before any output is billed only in some refusal categories). Other
  categories may route elsewhere; the evidence file then shows the cost as `"unknown"`.
- **Retries:** each logical call can send up to three HTTP requests, because the SDK
  retries timeouts, connection errors, 408/409/429 and 5xx twice (`max_retries=2`). A
  timed-out request may still have been processed and billed. If every call hit that,
  the theoretical ceiling would be three times that, just over USD 2 per run.
  `MAX_PAID_CALLS` does not prevent this; the workspace spend limit does.

Proposed approval: **USD 2 in total**, with the workspace spend limit set to that amount.
A normal run stays at or below USD 0.31; the limit stops anything beyond the approval.

## API access (manual steps for the owner)

Each step is yours to take in your own accounts. Choosing an organization and accepting
terms are your decisions; the assistant does neither.

1. **Check for included API credits first.** Claude Max (5x, 20x) and Team plans include
   monthly Claude API credits: USD 100 (Max 5x), USD 200 (Max 20x), or USD 20 per
   Standard and USD 100 per Premium Team seat, pooled and capped at USD 500. Free, Pro and
   Enterprise plans have none, and new subscribers can claim them after 7 days on the plan.
   On claude.ai look for an **API credits** section under **Settings > Billing** (Max) or
   **Organization settings > Billing** (Team). Source:
   [API credits for Max and Team plans](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)
   (checked 2026-10-30).
2. **Activating them is a manual decision.** Claiming links one Claude Console
   organization to the plan and accepts Anthropic's credit program terms. Each plan links
   to one organization and you cannot change the link yourself afterwards (only support
   can), so choose the organization deliberately and read the terms before linking. The
   credits then appear under **Promotional credits** on the Console's Billing page with
   their expiry (end of each billing cycle, no rollover). They cover the Claude API, not
   Claude Code, and they do not change the spend limits.
3. **If no included credits are available,** the paid run stays pending. How to fund it,
   if at all, is your decision; this guide does not require buying credits.
4. Create a workspace for this in that organization, for example
   `accessguard-verification`, and set its spend limit to the approved budget.
5. Create an API key **in that workspace**. Copy it only into the prompt below; never into
   the chat, a file in the repository, or a commit.
6. After the run, disable or delete the key if it is not needed again.

The run sends synthetic data only: the sample app's demo finding and the test's own
OpenAPI document.

## Running it (Windows PowerShell, repository root)

```powershell
$secure = Read-Host "Anthropic API key" -AsSecureString
$env:ANTHROPIC_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
$env:ACCESSGUARD_AI_LIVE = "paid"
$env:ACCESSGUARD_TESTING = "1"
$env:PYTHONPATH = "src;lab;evaluations;sampleapp;web;ml"
.venv\Scripts\python web\manage.py test accessguard_web.tests.test_assistant_flows -v 2
Remove-Item Env:ANTHROPIC_API_KEY, Env:ACCESSGUARD_AI_LIVE
```

Run only this module with the key set. The key is typed without echo and stays in this
PowerShell session. Optional: `$env:ACCESSGUARD_AI_TIMEOUT_SECONDS` (default 120).

Expected: 10 tests, 0 skipped, `OK`; three lines in `.local/ai-live/runs.jsonl`
(provider `anthropic`, the model and tokens of the returned message, every model attempt
with its own model and tokens, latency, outcome, estimated USD or `"unknown"`). A line is
written before the checks, so a failed call leaves its evidence too. Compare the estimate
with the Console's cost page. Then record the result in docs/HANDOFF.md and change the
status at the top of this file.

## Known limits of the record

- `AssistantRun` stores the model and tokens of the returned message, for a refusal or a
  cut-off answer as well. With a refusal fallback that is the serving attempt only (the
  API's top-level `usage`). The adapter reads every attempt from `usage.iterations`
  (`Attempt` in `assistant/providers.py`), but only the live test's evidence file keeps
  them; the database does not.
- The cost estimate is list price times reported tokens. It can overstate (a refusal
  before any output is not billed in every category) and cannot see requests the SDK
  retried, because only the last response's usage comes back.
- Latency of a failed call is measured around the provider call and includes the SDK's two
  retries.
