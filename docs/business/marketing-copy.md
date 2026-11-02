# Product texts and ad drafts

> **Paused (2026-11-02):** the commercial launch is on hold; none of these drafts was
> published, and none should be without a new decision.

Rules for every text: describe only implemented features; no customer names, testimonials,
statistics about customers, certifications or security guarantees; no claim that AI finds
vulnerabilities. Publishing any of this needs the owner's approval.

## Positioning

**One line:** AccessGuard tests whether one customer of your SaaS can read another
customer's data, with evidence, fix guidance and retests.

**For:** small and mid-sized software teams with a multi-tenant API and a test or staging
environment.

**Problem:** missing tenant and role checks (broken object level authorization, listed first in
the OWASP API Security Top 10 2023) are easy to introduce and tedious to test by hand for
every user, record and role after every change.

**What it does (implemented):** you declare test users, records and who may read them; a
runner in your environment sends read-only requests for every combination; a violation is
reported only when another tenant's protected values appear in a response; reports show
evidence, coverage and limits; findings are triaged and retested, including the legitimate
access a fix must keep.

**What it is not:** not a penetration test, not a crawler, not a certification.

## Product texts

**Short (app stores, directories, 160 characters):**
Test your multi-tenant API for cross-customer data leaks. Evidence for every finding, fix
guidance and one-click retests. Credentials stay with you.

**Medium (about 60 words):**
AccessGuard checks every test user against every record and list in your API and tells you,
with evidence, when one customer can read another's data. A small runner in your environment
does the requests, so credentials and responses stay with you. Each finding comes with fix
guidance and a retest that also checks the access that should keep working.

**Feature bullets:**
- Every user × every record × every list, derived from the rules you declare.
- Evidence, not guesses: a violation needs the other tenant's protected values in the response.
- Honest status: partial scans, expired test sessions and unreadable responses are never green.
- Runner in your environment: GET only, local allowlist, budget and deadline, stop any time.
- Retests that catch over-broad fixes; finding history and triage.
- Optional AI drafts for setup from OpenAPI and plain-language explanations. Verdicts stay deterministic.
- JSON reports and an offline CLI for CI.

## Ad drafts (English)

**Search ad 1**
- Headline 1: Test Cross-Tenant Data Leaks
- Headline 2: Evidence For Every Finding
- Headline 3: Free Live Demo, No Card
- Description: Check every user against every record of your multi-tenant API. Read-only requests from a runner in your environment.

**Search ad 2**
- Headline 1: Broken Object Level Authorization?
- Headline 2: Retest Fixes In One Click
- Headline 3: Built For SaaS Teams
- Description: Declare who may read what; AccessGuard checks every combination and shows the evidence. See a real example report.

**Social post (developer audience)**
> Can user A of customer X read customer Y's invoice? We built AccessGuard to answer that for
> every user, record and role in your API, with evidence instead of guesses. Try the live demo
> against a deliberately broken sample API, then retest the fix: [link]

**LinkedIn (founder voice, only when true)**
> I'm building AccessGuard, a small tool that tests multi-tenant APIs for cross-customer data
> access. It runs in your own environment, keeps test credentials local, and never shows a
> partial scan as "secure". Looking for 5-10 SaaS teams to try the beta and tell me what is
> missing.

## Ad drafts (dansk)

**Søgeannonce**
- Overskrift 1: Test datalæk mellem kunder
- Overskrift 2: Evidens for hvert fund
- Overskrift 3: Gratis demo, uden kort
- Beskrivelse: Tjek alle testbrugere mod alle poster i jeres multi-tenant API. Kun læsende kald fra en runner i jeres eget miljø.

**Opslag**
> Kan en bruger hos kunde A læse kunde B's data? AccessGuard tester hver testbruger mod hver
> post og liste i jeres API og viser evidensen. Prøv demoen mod et bevidst fejlbehæftet
> eksempel-API, og gentest rettelsen: [link]

## Claims checklist before publishing

- [ ] Every feature named exists in the deployed version.
- [ ] No numbers about customers, detection rates or time saved unless measured and sourced.
- [ ] "OWASP API Security Top 10 (2023)" is the only external reference, and it is quoted correctly.
- [ ] The link goes to the landing page or the example report, both public.
