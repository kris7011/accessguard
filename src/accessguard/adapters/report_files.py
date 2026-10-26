"""Writes reports to disk: JSON (machine-readable, English) and HTML (human-readable, Danish).

Every report is scrubbed for credentials before it is serialized.
"""

from __future__ import annotations

import html
import json
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from accessguard.domain.redaction import scrub

OUTCOME_LABELS_DA = {
    "PASS": "Bestået",
    "FAIL": "Fejl (bekræftet)",
    "SUSPECTED": "Mistanke",
    "INCONCLUSIVE": "Utilstrækkeligt grundlag",
    "SKIPPED": "Sprunget over",
    "ERROR": "Teknisk fejl",
}

OVERALL_LABELS_DA = {
    "findings": (
        "Fund: adgangskontrollen afviger fra den angivne politik",
        "Mindst én kontrol fejlede. Se fund og evidens nedenfor.",
    ),
    "needs_review": (
        "Kræver manuel gennemgang",
        "Ingen bekræftede fejl, men mindst én mistanke skal vurderes af et menneske.",
    ),
    "incomplete": (
        "Ufuldstændig: ikke en sikkerhedsgodkendelse",
        "Ikke alle planlagte kontroller gav et entydigt resultat.",
    ),
    "checks_passed": (
        "Alle planlagte kontroller bestod",
        "Gælder kun den angivne politik og de viste kontroller. Det er ikke en "
        "sikkerhedscertificering.",
    ),
}


def write_report(report: Mapping[str, Any], directory: Path, secrets: Collection[str]) -> Path:
    """Write ``report.json`` and ``report.html`` to ``directory``; return the JSON path."""
    scrubbed = scrub(dict(report), secrets)
    if not isinstance(scrubbed, dict):
        raise TypeError("A report must be a JSON object")
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "report.json"
    json_path.write_text(json.dumps(scrubbed, indent=2, ensure_ascii=False) + "\n", "utf-8")
    (directory / "report.html").write_text(render_html(scrubbed), "utf-8")
    return json_path


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


AGENT_STATUS_DA = {
    "completed": "alle godkendte kontroller blev valgt og udført",
    "stopped_early": "agenten stoppede, før alle kontroller var udført",
    "model_failed": "modelfejl: kørslen blev afbrudt uden fallback",
    "invalid_replies_exhausted": "for mange ugyldige modelsvar: kørslen blev afbrudt",
    "model_calls_exhausted": "budgettet for modelkald blev opbrugt",
    "interrupted": "kørslen blev afbrudt af en grænse (budget, tidsfrist eller stop)",
}


def _agent_text(agent: Mapping[str, Any]) -> str:
    adapter = agent["adapter"]
    if adapter["is_real_model"]:
        who = f"Adaptiv agent med {_e(adapter['label'])}"
    else:
        who = "Adaptiv agentløkke med scriptet testadapter (ikke en AI-model)"
    failure = f" Fejl: {_e(agent['failure'])}." if agent.get("failure") else ""
    return (
        f"{who}. Prompt {_e(agent['prompt_version'])}, {_e(agent['model_calls'])} modelkald, "
        f"{_e(agent['invalid_replies'])} ugyldige svar. Status: "
        f"{_e(AGENT_STATUS_DA.get(agent['status'], agent['status']))}.{failure} Modellen vælger "
        "kun blandt godkendte kontroller; resultaterne afgøres deterministisk."
    )


def _planner_text(planner: Mapping[str, Any], agent: Mapping[str, Any] | None = None) -> str:
    if agent is not None:
        return _agent_text(agent)
    if planner.get("kind") == "seeded-random":
        return (
            f"Seedet tilfældig rækkefølge (seed {_e(planner.get('seed'))}, ingen AI). "
            "Bruges som sammenligningsgrundlag."
        )
    if not planner.get("uses_ai"):
        return "Deterministisk baseline (ingen AI). Rækkefølgen følger politikken."
    return (
        f"AI-planlægning via {_e(planner.get('kind'))}: model {_e(planner.get('model'))}, "
        f"prompt {_e(planner.get('prompt_version'))}, {_e(planner.get('latency_ms'))} ms. "
        "Modellen må kun ændre rækkefølgen af godkendte kontroller."
    )


def _finding_html(finding: Mapping[str, Any]) -> str:
    actor = finding["actor"]
    actor_text = (
        "anonym (uden legitimation)"
        if actor["role"] is None
        else f"{_e(actor['id'])}: rolle {_e(actor['role'])}, kunde {_e(actor['tenant'])}"
    )
    subject = finding["subject"]
    evidence = finding.get("evidence") or {}
    matched = "; ".join(
        f"{_e(m['resource'])}: {_e(', '.join(m['fields']))}"
        for m in evidence.get("matched_protected_fields", [])
    )
    steps = "".join(f"<li>{_e(step)}</li>" for step in finding["reproduction"])
    outcome = finding["outcome"]
    return f"""
<article class="finding" aria-labelledby="f-{_e(finding["id"])}">
  <h3 id="f-{_e(finding["id"])}"><span class="badge {outcome.lower()}">
    {_e(OUTCOME_LABELS_DA[outcome])}</span> {_e(finding["id"])}</h3>
  <dl>
    <dt>Regel</dt><dd>{_e(finding["rule"])}</dd>
    <dt>Testidentitet</dt><dd>{actor_text}</dd>
    <dt>Ressource</dt><dd>{_e(subject["id"])} ({_e(subject["category"])})</dd>
    <dt>Forventet</dt><dd>{_e(finding["expected"])}</dd>
    <dt>Faktisk</dt><dd>{_e(finding["actual"])}</dd>
    <dt>Evidens (redigeret)</dt><dd><code>{_e(evidence.get("request"))}</code>,
      HTTP {_e(evidence.get("status"))}, {_e(evidence.get("size_bytes"))} bytes,
      sha256 <code>{_e(str(evidence.get("body_sha256", ""))[:16])}…</code><br>
      Beskyttede felter fundet i svaret: {matched or "ingen"}</dd>
    <dt>Verifikation</dt><dd>{_e(finding["verification"])}</dd>
    <dt>Tidspunkt</dt><dd>{_e(finding["observed_at"])}</dd>
  </dl>
  <details><summary>Reproduktion</summary><ol>{steps}</ol></details>
</article>"""


def render_html(report: Mapping[str, Any]) -> str:
    run = report["run"]
    overall = report["status"]["overall"]
    title, explanation = OVERALL_LABELS_DA[overall]
    coverage = report["coverage"]
    counts = "".join(
        f"<tr><th scope='row'><span class='badge {k.lower()}'>{_e(OUTCOME_LABELS_DA[k])}</span>"
        f"</th><td>{_e(k)}</td><td>{v}</td></tr>"
        for k, v in report["summary"].items()
    )
    relations = "".join(
        f"<tr><th scope='row'>{_e(relation)}</th><td>"
        + ", ".join(f"{_e(k)}: {v}" for k, v in sorted(counts_by.items()))
        + "</td></tr>"
        for relation, counts_by in sorted(coverage["by_relation"].items())
    )
    findings = "".join(_finding_html(f) for f in report["findings"]) or "<p>Ingen fund.</p>"
    rows = "".join(
        f"<tr><td><code>{_e(r['check_id'])}</code></td><td>{_e(r['actor'])}</td>"
        f"<td>{_e(r['expectation'])}</td>"
        f"<td>{_e((r.get('evidence') or {}).get('status'))}</td>"
        f"<td><span class='badge {r['outcome'].lower()}'>{_e(OUTCOME_LABELS_DA[r['outcome']])}"
        f"</span></td><td>{_e(r['reason'])}</td></tr>"
        for r in report["results"]
    )
    not_tested = "".join(f"<li>{_e(item)}</li>" for item in report["not_tested"])
    return f"""<!doctype html>
<html lang="da">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AccessGuard rapport</title>
<style>
:root {{ --bg:#f7f8fa; --fg:#14181f; --muted:#4a5565; --card:#fff; --line:#d5dbe3;
  --pass:#0f6b3a; --fail:#a3161b; --warn:#8a4b00; --info:#2d4f86; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#10151d; --fg:#e8edf4; --muted:#a9b4c3;
  --card:#18202b; --line:#2e3a4a; --pass:#6fd59b; --fail:#ff8c8f; --warn:#f2b866;
  --info:#9cc0ff; }} }}
body {{ font: 16px/1.55 system-ui, sans-serif; background: var(--bg); color: var(--fg);
  max-width: 1100px; margin: 0 auto; padding: 24px 16px; }}
h1 {{ font-size: 1.9rem; margin-bottom: .2rem; }}
.muted {{ color: var(--muted); }}
.status {{ border: 2px solid var(--line); background: var(--card); border-radius: 10px;
  padding: 16px 20px; margin: 20px 0; }}
.status.findings {{ border-color: var(--fail); }}
.status.checks_passed {{ border-color: var(--pass); }}
.status.incomplete, .status.needs_review {{ border-color: var(--warn); }}
table {{ width: 100%; border-collapse: collapse; background: var(--card); }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);
  vertical-align: top; }}
.table-wrap {{ overflow-x: auto; }}
.badge {{ font-weight: 600; white-space: nowrap; }}
.badge.pass {{ color: var(--pass); }} .badge.fail {{ color: var(--fail); }}
.badge.suspected, .badge.inconclusive, .badge.skipped {{ color: var(--warn); }}
.badge.error {{ color: var(--info); }}
.finding {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 4px 18px 14px; margin: 14px 0; }}
dl {{ display: grid; grid-template-columns: minmax(120px, 190px) 1fr; gap: 6px 14px; }}
dt {{ font-weight: 600; }} dd {{ margin: 0; overflow-wrap: anywhere; }}
code {{ font-size: .92em; }}
@media (max-width: 640px) {{ dl {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<header>
  <h1>AccessGuard {_e(report["tool"]["version"])}: adgangskontrol-rapport</h1>
  <p class="muted">Mål: {_e(run["target"])} ({_e(run["scope"]["origin"])}) · Politik
    {_e(run["policy"]["id"])} · Kørsel {_e(run["id"])} · {_e(run["started_at"])}</p>
  <p>{_planner_text(run["planner"], run.get("agent"))}</p>
</header>
<main>
<section class="status {_e(overall)}" aria-live="polite">
  <h2>{_e(title)}</h2>
  <p>{_e(explanation)}</p>
  <p class="muted">Requests brugt: {_e(run["requests_used"])} af
    {_e(run["limits"]["max_requests"])}.
    {"Kørslen blev afbrudt: " + _e(run["interrupted"]) + "." if run["interrupted"] else ""}</p>
</section>
<section>
  <h2>Resultater</h2>
  <div class="table-wrap"><table>
    <thead><tr><th scope="col">Status</th><th scope="col">Kode</th>
      <th scope="col">Antal</th></tr></thead>
    <tbody>{counts}</tbody></table></div>
  <h3>Dækning</h3>
  <p>{coverage["conclusive_checks"]} af {coverage["planned_checks"]} planlagte kontroller gav et
    entydigt resultat (PASS eller FAIL): {coverage["conclusive_ratio"] * 100:.1f} %.
    {coverage["executed_checks"]} kontroller blev udført.</p>
  <div class="table-wrap"><table>
    <thead><tr><th scope="col">Relation til ressourcen</th><th scope="col">Udfald</th></tr>
    </thead><tbody>{relations}</tbody></table></div>
</section>
<section>
  <h2>Fund ({len(report["findings"])})</h2>
  {findings}
</section>
<section>
  <h2>Ikke testet</h2>
  <ul>{not_tested}</ul>
</section>
<section>
  <h2>Alle kontroller</h2>
  <div class="table-wrap"><table>
    <thead><tr><th scope="col">Kontrol</th><th scope="col">Aktør</th>
      <th scope="col">Forventning</th><th scope="col">HTTP</th><th scope="col">Resultat</th>
      <th scope="col">Begrundelse</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
</section>
</main>
<footer><p class="muted">{_e(report["disclaimer"])}</p></footer>
</body>
</html>
"""
