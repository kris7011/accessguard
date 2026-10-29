"""Browser check of the main screens against a running local stack (ops/dev/run_stack.py).

    python ops/dev/browser_check.py --base http://127.0.0.1:8000 --out reports/browser

Uses Playwright with the locally installed Chrome (``channel="chrome"``; nothing is
downloaded). Walks the customer journey (landing, sign-up, demo scan, report, finding,
setup, billing, pricing) at desktop and phone width, saves screenshots, and fails on any
browser console error (which includes Content-Security-Policy violations), HTTP error or
horizontal overflow at phone width.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import ConsoleMessage, Page, Response, sync_playwright

VIEWPORTS = {"desktop": {"width": 1366, "height": 900}, "phone": {"width": 390, "height": 844}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--out", type=Path, default=Path("reports/browser"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        for name, viewport in VIEWPORTS.items():
            page = browser.new_page(viewport=viewport)  # type: ignore[arg-type]

            def on_console(message: ConsoleMessage, view: str = name, tab: Page = page) -> None:
                if message.type == "error":
                    problems.append(f"[{view}] console error on {tab.url}: {message.text}")

            page.on("console", on_console)

            def on_response(response: Response, view: str = name) -> None:
                if response.status >= 400 and not response.url.endswith("favicon.ico"):
                    problems.append(f"[{view}] HTTP {response.status} {response.url}")

            page.on("response", on_response)
            try:
                journey(page, args.base, args.out, name, problems)
            except Exception as error:  # report what was collected, then fail
                page.screenshot(path=str(args.out / f"{name}-failure.png"), full_page=True)
                problems.append(f"[{name}] journey stopped at {page.url}: {error}")
            page.close()
        browser.close()

    for problem in problems:
        print(problem)
    print(f"{len(problems)} problems; screenshots in {args.out}")
    return 1 if problems else 0


def shot(page: Page, out: Path, view: str, label: str, problems: list[str]) -> None:
    page.screenshot(path=str(out / f"{view}-{label}.png"), full_page=True)
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    if view == "phone" and overflow > 2:
        problems.append(f"[{view}] {label}: page is {overflow}px wider than the screen")


def journey(page: Page, base: str, out: Path, view: str, problems: list[str]) -> None:
    page.goto(base + "/")
    shot(page, out, view, "01-landing", problems)
    page.goto(base + "/example-report/")
    shot(page, out, view, "02-example-report", problems)
    page.goto(base + "/pricing/")
    shot(page, out, view, "03-pricing", problems)
    page.goto(base + "/accounts/signup/")
    page.fill("#id_email", f"browser-{view}-{int(time.time())}@example.test")
    page.fill("#id_password", "correct horse battery 42")
    page.fill("#id_organization", f"Browser {view}")
    page.check("#id_accept_terms")
    page.click("button[type=submit]")
    page.wait_for_url("**/app/")
    shot(page, out, view, "04-dashboard", problems)
    page.click("text=Demo: sample projects API (synthetic)")
    shot(page, out, view, "05-demo-project", problems)
    page.click("text=Review and start scan")
    shot(page, out, view, "06-scan-review", problems)
    page.check("#authorised")
    page.click("text=Approve and start")
    page.wait_for_url("**/app/scans/*/")
    shot(page, out, view, "07-scan-running", problems)
    page.wait_for_selector("text=confirmed access-control violations", timeout=60_000)
    shot(page, out, view, "08-scan-report", problems)
    page.click("text=Triage and retest findings")
    shot(page, out, view, "09-findings", problems)
    page.locator("table a").first.click()
    shot(page, out, view, "10-finding-detail", problems)
    page.goto(base + "/app/runners/")
    shot(page, out, view, "11-runners", problems)
    page.goto(base + "/app/billing/")
    shot(page, out, view, "12-billing", problems)
    page.goto(base + "/app/projects/new/")
    page.fill("#id_name", "Staging API")
    page.click("button[type=submit]")
    shot(page, out, view, "13-setup-target", problems)
    page.goto(base + "/help/runner/")
    shot(page, out, view, "14-help-runner", problems)


if __name__ == "__main__":
    sys.exit(main())
