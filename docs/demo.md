# Five-minute demo: find, fix and retest an access-control bug

The demo runs the whole product on your machine against a small **synthetic** projects API
with two organisations (`orbit` and `quasar`). Its flawed version has three deliberate
access-control bugs. You will:

**start** the stack → **find** the access errors → **apply the fix** in the demo app →
**retest** and see every finding resolved as verified.

No accounts, API keys, Docker or paid services are needed. The AI assistants stay off
(`ACCESSGUARD_AI_PROVIDER` defaults to `none`), and nothing leaves `127.0.0.1`.

*Last walked through on 2026-11-02 (commit `2fefd69`) in headless Chrome against fresh
databases, using only the buttons named below, three times: 30 checks → 10 confirmed
violations → fixed version deployed → retest of 17 checks passed → 0 open, 10 resolved, in
under 30 seconds from sign-up. The optional regression step below was checked too (10
findings reopened). `ops/dev/browser_check.py` reported 0 problems at desktop and phone width
the same day.*

## 1. Start

Requirements: Python 3.12 or newer (3.13 used), Git, and free ports 8000, 8800 and 8801.

```powershell
cd C:\AccessGuard                       # your clone
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python ops\dev\run_stack.py
```

On Linux/macOS: `python3 -m venv .venv`, then `.venv/bin/python ...` with the same arguments.

`run_stack.py` migrates a SQLite database in `.local/`, then starts four processes:

| Process | Address | Role |
| --- | --- | --- |
| Demo API, **flawed** variant | `http://127.0.0.1:8800` | the "customer's" API before the fix |
| Demo API, **fixed** variant | `http://127.0.0.1:8801` | the same API after the fix |
| Demo runner | — | the scanner, trusting only the two demo origins |
| Web control plane | `http://127.0.0.1:8000` | where you work |

Wait for `AccessGuard is running on http://127.0.0.1:8000/`. Ctrl+C stops everything.

## 2. Find the access errors

1. Open <http://127.0.0.1:8000/>, choose **Sign up**, enter any e-mail address (nothing is
   sent), a password of at least 12 characters and an organisation name, accept the terms.
2. On the overview page open **Demo: sample projects API (synthetic)**. The card "About this
   demo" says *The demo API currently runs the **flawed** version.*
3. Click **Review and start scan**. The review page shows the exact scope the runner may use:
   target, allowed endpoints, GET only, test users, **30 checks** and the request budget.
4. Tick the authorisation box and click **Approve and start**. The scan page updates by
   itself; the demo runner finishes in a few seconds.
5. Result: **10 confirmed access-control violations**, with a coverage table by relationship
   ("who tried to read what") and the list of all 30 checks.
6. Click **Triage and retest findings** and open any finding. Each one has *What happened*,
   *Evidence*, *Reproduce*, *How to fix it*, *Decide* (triage) and *Retest*.

The 10 findings are exactly the hand-written answer key
([`sampleapp/accessguard_sampleapp/config/answer_key.json`](../sampleapp/accessguard_sampleapp/config/answer_key.json)):

| Bug in the flawed variant | What it allows | Findings |
| --- | --- | --- |
| `project-idor` | `GET /api/v1/projects/{id}` returns any project to any signed-in user | 4 (`orbit-apollo@cleo`, `orbit-apollo@dev`, `quasar-nova@ana`, `quasar-nova@ben`) |
| `billing-role-bypass` | `GET /api/v1/orgs/{org}/billing` checks the organisation but not the role, so viewers see billing details | 2 (`orbit-billing@ben`, `quasar-billing@dev`) |
| `project-list-leak` | `GET /api/v1/projects` lists every organisation's projects | 4 (`list:projects@ana` … `@dev`) |

A finding id reads `<record or list>@<test user>`: `orbit-billing@ben` means "Ben, a viewer
in orbit, could read orbit's billing record".

## 3. Apply the fix in the demo app

Back on the demo project, click **Deploy the fixed version**. The card now says *The demo API
currently runs the **fixed** version.*

What that button does: it simulates a deployment by pointing the demo project at the fixed
instance on port 8801 (`web/accessguard_web/projects/demo.py: set_demo_variant`). Both
instances run the same code, [`sampleapp/accessguard_sampleapp/app.py`](../sampleapp/accessguard_sampleapp/app.py);
the variant decides whether the three access rules are enforced. The fix for each bug is
the branch the fixed variant takes:

| Bug | Method in `SampleApplication` | Fix |
| --- | --- | --- |
| `project-list-leak` | `_list_projects` | list only projects the caller may see: `[p for p in PROJECTS.values() if self._can_see(user, p)]` |
| `project-idor` | `_project` | if the caller may not see the project, answer `404 not_found` (not `403`, so other organisations' ids are not confirmed) |
| `billing-role-bypass` | `_billing` | after the organisation check, require `user.role == "owner"`, else `403 forbidden` |

`_can_see` is the intended rule: the project belongs to the caller's organisation or is
shared with it.

## 4. Retest

1. On the demo project, the "Open findings" card offers **Retest all 10 open findings**.
2. The review page is now titled *Review the retest* and shows **17 selected** checks: the 10
   findings plus their *positive controls*, the legitimate reads of the same records. They
   make sure a "fix" that simply blocks everyone does not count as a pass.
3. Tick the box, **Approve and start**. Result: *Retest passed for the 17 selected checks*.
4. The demo project now shows *No open findings* and *10 resolved*; each finding is marked
   **resolved · verified by retest**, and its *History* records the scan that verified it.

A finding is resolved as verified only by a conclusive PASS on retest. Marking it
"resolved" by hand keeps it *not verified*.

## Optional next steps

- Click **Switch back to the flawed version**, then **Review and start scan** for a full
  scan: the 10 findings reopen, and each one's history notes the regression.
- Download the JSON report from a scan page, or the scan spec from the project page, and
  compare it with [report-schema.md](report-schema.md).
- Break a rule yourself: the hands-on exercise in
  [interview-prep/V1.md](interview-prep/V1.md) changes the fixed variant's billing rule and
  shows the retest catching it.
- Set up your own project (**New project**) against a test API you are authorised to scan,
  with your own runner: [install.md](install.md), section 3.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `run_stack.py` stops right away with a socket or port error | Another process holds 8000, 8800 or 8801. Stop it, or use `--port` for the web app (the demo API ports are fixed) |
| The scan waits and a banner says no runner has claimed it | The demo runner process is not running; stop and restart `run_stack.py` |
| You want a clean start without losing your data | Point the stack at a new database file before starting it. PowerShell: `$env:DATABASE_URL = "sqlite:///C:/temp/accessguard-demo.sqlite3"`; bash: `DATABASE_URL=sqlite:////tmp/accessguard-demo.sqlite3 .venv/bin/python ops/dev/run_stack.py`. The default is `.local/dev.sqlite3` |

Automated equivalents of this walkthrough: `web/accessguard_web/tests/test_end_to_end.py`
(over real HTTP with a real runner) and `ops/dev/customer_acceptance.py` (a runner installed
from the wheel, for a customer project instead of the demo).
