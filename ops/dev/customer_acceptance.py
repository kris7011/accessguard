"""Customer acceptance run: the whole journey over real HTTP with an installed runner.

    python ops/dev/customer_acceptance.py --runner-venv <venv> --out <new dir>
    python ops/dev/customer_acceptance.py --runner-venv <venv> --out <new dir> --external http://127.0.0.1:8000

The runner comes from ``--runner-venv``: a virtual environment where only the built wheel is
installed (see docs/install.md), so the run proves the distribution works without this
repository. The script drives the real server-rendered forms like a browser (GET the page,
read the form's fields including empty extra rows and default select options, fill them,
POST) and runs:

sign-up → project through the forms → runner token → approve → the installed runner scans
the separate sample app (the "customer's API") → 10 confirmed findings → triage → redeploy
the fixed variant on the same origin → retest all → every finding resolved and verified.

Without ``--external`` it starts its own control plane (this interpreter, a new SQLite file in
``--out``). ``--out`` must not exist: every run uses fresh test data. The runner token and the
sample API keys are never printed; the report is checked for leaked keys.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import IO, Any

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "Customer-flow-passphrase-42"  # noqa: S105  # pragma: allowlist secret
USERS = [
    ("ana", "orbit", "owner", "ana@orbit.example"),
    ("ben", "orbit", "viewer", "ben@orbit.example"),
    ("cleo", "quasar", "owner", "cleo@quasar.example"),
    ("dev", "quasar", "viewer", "dev@quasar.example"),
]
RECORDS = [
    (
        "orbit-apollo",
        "project",
        "/api/v1/projects/prj-orbit-apollo",
        "ana",
        "data.name, data.budget_code",
        "projects",
        ["orbit:owner", "orbit:viewer"],
    ),
    (
        "quasar-nova",
        "project",
        "/api/v1/projects/prj-quasar-nova",
        "cleo",
        "data.name, data.budget_code",
        "projects",
        ["quasar:owner", "quasar:viewer"],
    ),
    (
        "quasar-roadmap",
        "shared-project",
        "/api/v1/projects/prj-quasar-roadmap",
        "cleo",
        "data.name, data.budget_code",
        "projects",
        ["quasar:owner", "quasar:viewer", "orbit:owner", "orbit:viewer"],
    ),
    (
        "orbit-billing",
        "billing",
        "/api/v1/orgs/orbit/billing",
        "ana",
        "data.account_ref, data.plan_note",
        "",
        ["orbit:owner"],
    ),
    (
        "quasar-billing",
        "billing",
        "/api/v1/orgs/quasar/billing",
        "cleo",
        "data.account_ref, data.plan_note",
        "",
        ["quasar:owner"],
    ),
]

LIST = {"id": "projects", "path": "/api/v1/projects"}
ANSWER_KEY = ROOT / "sampleapp" / "accessguard_sampleapp" / "config" / "answer_key.json"

Form = dict[str, Any]
FormPick = Callable[[Form], bool]


class FormParser(HTMLParser):
    """Collects every form with its fields as a browser sees them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self._form: Form | None = None
        self._select: dict[str, Any] | None = None
        self._textarea: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self._form = {
                "action": a.get("action") or "",
                "method": (a.get("method") or "get").lower(),
                "fields": [],
            }
            self.forms.append(self._form)
        elif self._form is None:
            return
        elif tag == "input":
            kind = (a.get("type") or "text").lower()
            if a.get("name") and kind not in ("submit", "button", "file", "reset"):
                value = a.get("value")
                default = "on" if kind in ("checkbox", "radio") else ""
                self._form["fields"].append(
                    {
                        "name": a["name"],
                        "type": kind,
                        "value": default if value is None else value,
                        "checked": "checked" in a,
                    }
                )
        elif tag == "select":
            self._select = {
                "name": a.get("name"),
                "type": "select",
                "options": [],
                "multiple": "multiple" in a,
            }
            self._form["fields"].append(self._select)
        elif tag == "option" and self._select is not None:
            self._select["options"].append((a.get("value") or "", "selected" in a))
        elif tag == "textarea":
            self._textarea = {"name": a.get("name"), "type": "textarea", "value": ""}
            self._form["fields"].append(self._textarea)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._textarea["value"] += data


def form_defaults(form: Form) -> list[tuple[str, str]]:
    """What a browser submits untouched: checked boxes, the selected (or first) option, values."""
    pairs: list[tuple[str, str]] = []
    for field in form["fields"]:
        if field["type"] in ("checkbox", "radio"):
            if field["checked"]:
                pairs.append((field["name"], field["value"]))
        elif field["type"] == "select":
            chosen = [value for value, selected in field["options"] if selected]
            if not chosen and field["options"] and not field["multiple"]:
                chosen = [field["options"][0][0]]
            pairs.extend((field["name"], value) for value in chosen)
        elif field["type"] == "textarea":
            pairs.append((field["name"], field["value"].removeprefix("\n")))
        else:
            pairs.append((field["name"], field["value"]))
    return pairs


def find_form(html: str, pick: FormPick) -> Form:
    parser = FormParser()
    parser.feed(html)
    matches = [form for form in parser.forms if pick(form)]
    if not matches:
        raise AssertionError("no matching form on the page")
    return matches[0]


def has_field(name: str) -> FormPick:
    return lambda form: any(field["name"] == name for field in form["fields"])


def action_contains(fragment: str) -> FormPick:
    return lambda form: fragment in form["action"]


def errors_in(html: str) -> list[str]:
    return re.findall(r'<ul class="errorlist[^"]*"[^>]*>(.*?)</ul>', html, re.S)


def number(pattern: str, text: str) -> int:
    match = re.search(pattern, text)
    if match is None:
        raise AssertionError(f"{pattern!r} not found")
    return int(match.group(1))


class Browser:
    """A cookie-keeping HTTP client that submits forms the way a browser does."""

    def __init__(self, base: str, log: list[str]) -> None:
        self.base = base.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.log = log

    def _open(self, request: urllib.request.Request) -> tuple[str, str]:
        try:
            with self.opener.open(request, timeout=60) as response:
                final = str(response.geturl())
                self.log.append(f"{request.get_method()} {request.full_url} -> {final}")
                return final, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:800]
            raise AssertionError(f"HTTP {error.code} for {request.full_url}: {body}") from None

    def get(self, path: str) -> tuple[str, str]:
        return self._open(urllib.request.Request(self.base + path))  # noqa: S310 - http(s) only

    def submit(
        self, page_url: str, html: str, pick: FormPick, changes: dict[str, Any] | None = None
    ) -> tuple[str, str]:
        form = find_form(html, pick)
        pairs = form_defaults(form)
        for name, value in (changes or {}).items():
            pairs = [(n, v) for n, v in pairs if n != name]
            values = value if isinstance(value, list) else [value]
            pairs.extend((name, str(v)) for v in values)
        target = urllib.parse.urljoin(page_url, form["action"] or page_url)
        data = urllib.parse.urlencode(pairs)
        if form["method"] == "get":
            request = urllib.request.Request(target.split("?")[0] + "?" + data)  # noqa: S310
        else:
            headers = {"Referer": page_url}
            request = urllib.request.Request(target, data.encode(), headers)  # noqa: S310
        final, body = self._open(request)
        if errors_in(body):
            raise AssertionError(f"form errors after submitting to {target}: {errors_in(body)}")
        return final, body


def fill_rows(browser: Browser, path: str, prefix: str, rows: list[dict[str, Any]]) -> int:
    """Put rows into the empty rows the page shows, save, and repeat. Returns the saves."""
    pending, saves = list(rows), 0
    while pending:
        url, html = browser.get(path)
        values = dict(form_defaults(find_form(html, has_field(f"{prefix}-TOTAL_FORMS"))))
        total = int(values[f"{prefix}-TOTAL_FORMS"])
        empty = [i for i in range(total) if not values.get(f"{prefix}-{i}-id")]
        if not empty:
            raise AssertionError(f"no empty {prefix} rows on {path}")
        changes: dict[str, Any] = {}
        for index in empty[: len(pending)]:
            for name, value in pending.pop(0).items():
                changes[f"{prefix}-{index}-{name}"] = value
        browser.submit(url, html, has_field(f"{prefix}-TOTAL_FORMS"), changes)
        saves += 1
    return saves


def wait_port(port: int, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.3)
    raise AssertionError(f"port {port} did not open")


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def clean_env(**extra: str) -> dict[str, str]:
    """The caller's environment without Python path overrides, so nothing leaks into the runner."""
    drop = {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    return {**{k: v for k, v in os.environ.items() if k not in drop}, **extra}


def venv_file(venv: Path, name: str) -> Path:
    folder = venv / ("Scripts" if os.name == "nt" else "bin")
    return folder / (f"{name}.exe" if os.name == "nt" else name)


class Processes:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.running: dict[str, subprocess.Popen[bytes]] = {}
        self.logs: list[IO[bytes]] = []

    def start(self, name: str, command: list[str], env: dict[str, str], cwd: Path) -> None:
        log = (self.out / f"{name}.log").open("wb")
        self.logs.append(log)
        # The commands are fixed by this script: this interpreter or the runner venv's executable.
        self.running[name] = subprocess.Popen(  # noqa: S603
            command, cwd=cwd, env=env, stdout=log, stderr=log
        )

    def stop(self, name: str) -> None:
        process = self.running.pop(name, None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    def stop_all(self) -> None:
        for name in list(self.running):
            self.stop(name)
        for log in self.logs:
            log.close()


class Journey:
    """One customer's path through the product; each step records what it observed."""

    def __init__(self, args: argparse.Namespace, out: Path, log: list[str]) -> None:
        self.args = args
        self.out = out
        self.customer = out / "customer-env"
        self.credentials = self.customer / "api-keys.json"
        self.origin = f"http://127.0.0.1:{args.app_port}"
        self.base = args.external or f"http://127.0.0.1:{args.web_port}"
        self.processes = Processes(out)
        self.browser = Browser(self.base, log)
        self.summary: dict[str, Any] = {}
        self.project = 0
        self.app = [sys.executable, "-m", "accessguard_sampleapp", "serve"]
        self.app += ["--port", str(args.app_port), "--credentials-out", str(self.credentials)]
        self.app_env = clean_env(PYTHONPATH=str(ROOT / "sampleapp"))

    def run(self) -> None:
        try:
            self.start_services()
            self.sign_up()
            self.configure()
            self.start_runner()
            findings = self.scan()
            self.triage(findings)
            self.fix_and_retest(len(findings))
        finally:
            self.processes.stop_all()

    def start_services(self) -> None:
        self.customer.mkdir()
        ports = [self.args.app_port] + ([] if self.args.external else [self.args.web_port])
        if busy := [port for port in ports if not port_free(port)]:
            raise AssertionError(f"ports in use: {busy}")
        if not self.args.external:
            env = clean_env(
                PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT / "web")]),
                ACCESSGUARD_DEBUG="1",
                DATABASE_URL=f"sqlite:///{(self.out / 'control-plane.sqlite3').as_posix()}",
            )
            manage = [sys.executable, str(ROOT / "web" / "manage.py")]
            migrate = [*manage, "migrate", "--no-input"]
            subprocess.run(migrate, cwd=self.out, env=env, check=True, capture_output=True)  # noqa: S603
            address = f"127.0.0.1:{self.args.web_port}"
            self.processes.start(
                "web", [*manage, "runserver", address, "--noreload"], env, self.out
            )
            wait_port(self.args.web_port)
        flawed = [*self.app, "--variant", "flawed"]
        self.processes.start("app-flawed", flawed, self.app_env, self.customer)
        wait_port(self.args.app_port)

    def sign_up(self) -> None:
        b = self.browser
        url, html = b.get("/accounts/signup/")
        account = {"email": self.args.email, "password": PASSWORD}
        url, html = b.submit(
            url,
            html,
            has_field("organization"),
            {**account, "organization": "Customer Flow ApS", "accept_terms": "on"},
        )
        if "Get started" not in html:
            raise AssertionError("sign-up did not land on the onboarding page")

    def configure(self) -> None:
        """A project configured only through the forms, row by row as the pages offer them."""
        b = self.browser
        url, html = b.get("/app/projects/new/")
        url, html = b.submit(url, html, has_field("name"), {"name": "Staging API"})
        self.project = number(r"/app/projects/(\d+)/", url)
        setup = f"/app/projects/{self.project}/setup"
        target = {
            "origin": self.origin,
            "path_prefixes": "/api/v1",
            "auth_kind": "header",
            "auth_name": "X-Api-Key",
            "session_path": "/api/v1/whoami",
            "subject_field": "data.email",
        }
        b.submit(*b.get(f"{setup}/target/"), has_field("origin"), target)
        users = [{"id": i, "tenant": t, "role": r, "subject": s} for i, t, r, s in USERS]
        fields = ("id", "category", "path", "owner", "protected_fields", "collection", "access")
        records = [dict(zip(fields, record, strict=True)) for record in RECORDS]
        saves = {
            "users": fill_rows(b, f"{setup}/users/", "users", users),
            "lists": fill_rows(b, f"{setup}/rules/", "lists", [LIST]),
            "records": fill_rows(b, f"{setup}/rules/", "records", records),
        }
        limits = {"max_requests": 300, "min_interval_seconds": 0, "max_duration_seconds": 120}
        b.submit(*b.get(f"{setup}/limits/"), has_field("max_requests"), limits)
        checks = number(r"(\d+) checks will run", b.get(f"/app/projects/{self.project}/")[1])
        self.summary["configuration"] = {"project": self.project, "form_saves": saves}
        self.summary["configuration"]["checks"] = checks

    def start_runner(self) -> None:
        """A runner token from the Runners page; the installed runner keeps it in its env."""
        url, html = self.browser.get("/app/runners/")
        name = {"name": "staging-runner"}
        url, html = self.browser.submit(url, html, action_contains("runners/new"), name)
        token = re.search(r'<pre id="runner-token">(agr_[^<]+)</pre>', html)
        if token is None:
            raise AssertionError("no runner token shown")
        config = [
            "poll_seconds = 1",
            "[control_plane]",
            f'url = "{self.base}"',
            'token_env = "ACCESSGUARD_RUNNER_TOKEN"',
            "[[targets]]",
            f'origin = "{self.origin}"',
            'zone = "loopback"',
            f'credentials_file = "{self.credentials.name}"',
        ]
        (self.customer / "runner.toml").write_text("\n".join(config) + "\n", "utf-8")
        runner = venv_file(self.args.runner_venv, "accessguard")
        command = [str(runner), "runner", "--config", "runner.toml"]
        env = clean_env(ACCESSGUARD_RUNNER_TOKEN=token.group(1))
        self.processes.start("runner", command, env, self.customer)
        python = str(venv_file(self.args.runner_venv, "python"))
        probe = "import accessguard as a, sys; print(a.__version__, a.__file__, sys.version)"
        installed = subprocess.run(  # noqa: S603 - the venv's own interpreter, a fixed probe
            [python, "-I", "-c", probe],
            cwd=self.customer,
            env=clean_env(),
            capture_output=True,
            text=True,
            check=True,
        )
        self.summary["runner"] = {"executable": str(runner), "module": installed.stdout.strip()}

    def approve(self, page: tuple[str, str]) -> int:
        url, _ = self.browser.submit(*page, has_field("authorised"), {"authorised": "yes"})
        scan = number(r"/app/scans/(\d+)/", url)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            state = json.loads(self.browser.get(f"/app/scans/{scan}/status.json")[1])
            if not state["active"]:
                return scan
            time.sleep(1.0)
        raise AssertionError(f"scan {scan} did not finish in time")

    def scan(self) -> list[int]:
        page = self.browser.get(f"/app/projects/{self.project}/scan/")
        if self.origin not in page[1] or "GET only" not in page[1]:
            raise AssertionError("the review page does not show the scope")
        scan = self.approve(page)
        html = self.browser.get(f"/app/scans/{scan}/")[1]
        report = self.browser.get(f"/app/scans/{scan}/report.json")[1]
        keys: dict[str, str] = json.loads(self.credentials.read_text("utf-8"))
        leaked = [name for name, key in keys.items() if key in report or key in html]
        violations = number(r"(\d+) confirmed access-control violations", html)
        run = json.loads(report)["run"]
        self.summary["scan"] = {"id": scan, "violations": violations, "credential_leaks": leaked}
        self.summary["scan"].update(requests=run["requests_used"], runner_python=run["python"])
        answer_key = json.loads(ANSWER_KEY.read_text("utf-8"))["flawed"]
        expected = len({check for checks in answer_key.values() for check in checks})
        if violations != expected or leaked:
            raise AssertionError(f"expected the answer key's findings, no leaks: {self.summary}")
        listing = self.browser.get(f"/app/findings/?project={self.project}")[1]
        return sorted({int(i) for i in re.findall(r'href="/app/findings/(\d+)/"', listing)})

    def triage(self, findings: list[int]) -> None:
        url, html = self.browser.get(f"/app/findings/{findings[0]}/")
        note = {"status": "confirmed", "note": "Reproduced by hand in staging."}
        url, html = self.browser.submit(url, html, action_contains("/triage/"), note)
        self.summary["findings"] = {"count": len(findings), "triaged": "Status updated." in html}

    def fix_and_retest(self, findings: int) -> None:
        """Deploy the fixed variant on the same origin, then retest every open finding."""
        self.processes.stop("app-flawed")
        fixed = [*self.app, "--variant", "fixed"]
        self.processes.start("app-fixed", fixed, self.app_env, self.customer)
        wait_port(self.args.app_port)
        detail = self.browser.get(f"/app/projects/{self.project}/")
        retest_form: FormPick = lambda form: form["method"] == "get" and "/scan/" in form["action"]  # noqa: E731
        scan = self.approve(self.browser.submit(*detail, retest_form))
        passed = "Retest passed" in self.browser.get(f"/app/scans/{scan}/")[1]
        listing = self.browser.get(f"/app/findings/?project={self.project}&status=")[1]
        verified = listing.count("Resolved · verified")
        self.summary["retest"] = {"id": scan, "passed": passed, "resolved_verified": verified}
        if not passed or verified != findings:
            raise AssertionError(f"the retest did not resolve every finding: {self.summary}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, required=True, help="a new directory for the run")
    parser.add_argument("--runner-venv", type=Path, required=True, help="venv with the wheel")
    parser.add_argument("--web-port", type=int, default=8100)
    parser.add_argument("--app-port", type=int, default=8910)
    parser.add_argument("--external", help="base URL of a control plane that is already running")
    parser.add_argument("--email", default="lead@customer-flow.test")
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=False)
    log: list[str] = []
    journey = Journey(args, out, log)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        journey.run()
        result = "PASS"
    except (AssertionError, OSError, subprocess.CalledProcessError) as error:
        result = f"FAIL: {error}"
    summary = {"started": started, **journey.summary, "result": result}
    summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (out / "summary.json").write_text(json.dumps(summary, indent=2), "utf-8")
    (out / "http.log").write_text("\n".join(log), "utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
