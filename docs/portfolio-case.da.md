# Porteføljecase: AccessGuard

*Automatiske test af dataadskillelse mellem kunder i SaaS-API'er · oktober–november 2026 ·
status: porteføljeprojekt; den kommercielle lancering er sat på pause*

## Formål

I et SaaS-produkt ligger mange kunders data i samme system. Mangler én adgangskontrol, kan
kunde A læse kunde B's data. Den slags fejl er svære at fange med almindelige test, fordi
antallet af kombinationer (brugere × poster × endpoints) vokser hurtigt.

AccessGuard lader et udviklingsteam beskrive, hvem der må læse hvad. Derefter tester en
runner i teamets eget netværk alle kombinationer med læse-forespørgsler (GET). En fejl
rapporteres kun, når en anden kundes beskyttede værdier faktisk står i svaret. Hver fejl
kommer med bevis, rettevejledning og en gentest, der dokumenterer, at rettelsen virker, og at
lovlig adgang stadig fungerer.

Målet var et produkt, der kunne sælges som selvbetjent beta. Det er nu afsluttet som et
præsentabelt porteføljeprojekt: koden, testene og historikken er bevaret, og repositoryet er
offentligt på GitHub. Ingen hostet tjeneste er lanceret eller sat til salg, og der er ikke
modtaget livebetalinger. Den engelske [README](../README.md) er projektets primære
præsentation; denne danske case er et supplement.

## Mit arbejde med projektet

- **Krav og retning:** Jeg skrev den oprindelige kravspecifikation
  ([AccessGuard-Claude-Code.md](../AccessGuard-Claude-Code.md)) og fastlagde milepælene
  (M1 motor, M2 begrænset AI-agent og evaluering, V1 kommerciel beta) med
  acceptkriterier for hver arbejdspakke.
- **Beslutninger:** Jeg traf de ejerbeslutninger, der er noteret i
  [HANDOFF.md](HANDOFF.md), fx reglen om at annullere en ubetalt fornyelsesfaktura, når
  Stripe opsiger abonnementet, og at beholde 7 dages henstand. Jeg fastlagde designet for
  den forhåndsregistrerede AI-evaluering (fixtures, budgetter, strategier, tre separate
  serversessioner).
- **Verificering med rigtige systemer:** Jeg kørte selv den betalte test mod Claude API'et
  og overtog og kontrollerede Stripe-sandkassen (Dashboard-indstillinger for betalingsforsøg).
- **AI-assisteret udvikling:** Koden, testene og det meste af dokumentationen er skrevet med
  AI-kodeassistenten Claude Code under min styring; de AI-assisterede sessioner kørte også
  de fleste verificeringer. Jeg præsenterer det ikke som håndskrevet kode. Min rolle var at
  specificere, vælge, gennemgå og kræve dokumentation for hvert resultat. Gennemgangen i
  [PROJECT_WALKTHROUGH.md](PROJECT_WALKTHROUGH.md) og interviewnoterne i
  [interview-prep/](interview-prep/) er mit værktøj til at kunne forklare og forsvare hver del.

## Teknologier

| Område | Valg |
| --- | --- |
| Testmotor og runner | Python 3.11–3.13, kun standardbiblioteket; lagdelt modulær monolit |
| Webplatform | Django 6.1, server-renderede skabeloner, PostgreSQL 17 (SQLite lokalt) |
| Betaling | Stripe i testtilstand: Checkout, Customer Portal, signerede webhooks, test clocks |
| AI | To begrænsede assistenter bag en port (Claude API, lokal Ollama eller ingen) |
| ML | Datasæt, evaluering mod baselines og modelregister med en promoveringsport |
| Drift | Docker Compose, Caddy (TLS), restic (krypterede backups uden for serveren) – forberedt, ikke deployet |
| Kvalitet | unittest, Ruff, mypy strict, hash-låste afhængigheder, pip-audit, detect-secrets, Playwright, GitHub Actions |

## Dokumenterede resultater

| Resultat | Dokumentation |
| --- | --- |
| Alle 11 lokale kontroller grønne; 376 test af motor/runner/ML og 150 webtest (2026-11-02) | [HANDOFF.md](HANDOFF.md) |
| CI med seks job grøn på Ubuntu (Python 3.11 og 3.13), Windows og PostgreSQL 17 | [README](../README.md#what-works-and-how-it-is-verified) |
| Demoen finder præcis de 10 forventede fejl (3 bevidste fejl), og efter rettelsen løses alle 10 og verificeres ved gentest; gennemgået i browser 2026-11-02 | [demo.md](demo.md) |
| Runneren installeret fra den byggede pakke i rene miljøer; hele kunderejsen bestået | [milestones/V1.md](milestones/V1.md) |
| Betaling kørt mod en rigtig Stripe-sandkasse, inkl. fornyelser og 8 betalingsforsøg over 14 simulerede dage; syv fejl fundet og rettet i én hærdningsrunde | [ops/stripe_sandbox/README.md](../ops/stripe_sandbox/README.md), [HANDOFF.md](HANDOFF.md) |
| AI-assistenterne bestod én test mod den rigtige Claude-model (3 kald, ca. 0,03 USD) for to syntetiske scenarier | [ai-live-verification.md](ai-live-verification.md) |
| Evalueringen viste ingen dokumenteret fordel ved AI-prioritering frem for simple regler; den trænede ML-kandidat blev afvist af porten, så **ingen lærende model er aktiv** | [evaluation.md](evaluation.md), `ml/registry/registry.json` |

Begrænsninger: kun GET-forespørgsler og deklarerede endpoints; resultater fra syntetiske
apps siger intet om fundprocenten på rigtige systemer; aldrig kørt på en server; kun
Stripe-testtilstand; betalingsvilje er ikke valideret.

## Læringspunkter

1. **Mål mod simple baselines, før AI bliver en del af produktet.** Agentens målte fordel
   blev forklaret af en simpel regel ("lister først"). Derfor bruger produktet fuld,
   deterministisk dækning, og AI hjælper kun med opsætning og forklaringer.
2. **Mocks skjuler integrationsfejl.** Stripe-sandkassen afslørede fejl, som testene med
   mocks ikke kunne se: en portalindstilling gav en dyrere plan uden betaling, og
   sikkerhedspolitikken (CSP) blokerede viderestillingen til Stripes fakturaside. Det førte
   til designet, hvor hver webhook kun er et signal, og abonnementet læses igen fra Stripe
   under en rækkelås.
3. **Test det, brugeren faktisk sender.** En kundetest fandt en fejl, som end-to-end-testen
   havde overset: en formular kunne ikke gemmes i en browser, fordi testen ikke sendte den
   tomme ekstra række, som en browser sender. Testene bygger nu data ud fra den renderede side.
4. **CI på flere platforme betaler sig.** Linux- og Windows-jobbene fandt forskelle, der ikke
   viste sig lokalt: C-bibliotekets `log1p` afrunder ikke ens overalt, så Linux byggede et
   andet ML-datasæt end Windows (rettet med en korrekt afrundet logaritme); et job manglede
   en afhængighed; en test hang på Windows' CI-maskine.
5. **Teknisk klar er ikke det samme som klar til lancering.** Server, domæne, e-mail,
   juridiske tekster, databehandleraftaler, moms og validering af betalingsvilje er stadig
   åbne punkter ([launch-checklist.md](launch-checklist.md)). At skrive dem op ærligt var en
   del af arbejdet.
6. **Dokumentér tre ting hver for sig:** hvad der er implementeret, hvad der er testet, og
   hvad der er blokeret af noget udefra. Det gør det muligt at stå inde for hver påstand.
