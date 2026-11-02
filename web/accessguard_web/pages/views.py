"""Public pages (landing, pricing, help, example report) and health endpoints."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from django.db import connection
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static

from accessguard_web.billing.plans import PLANS
from accessguard_web.scans.distribution import runner_wheel
from accessguard_web.scans.models import Scan, ScanKind, ScanStatus
from accessguard_web.scans.presenters import report_view

EXAMPLE_REPORT = Path(__file__).with_name("example_report.json")
HELP_PAGES = {
    "getting-started": "Getting started",
    "runner": "Installing the runner",
    "configuration": "Configuring users and access rules",
    "results": "Reading results and retesting",
    "ci": "Running scans in CI",
    "data-handling": "Security and data handling",
    "limitations": "What AccessGuard does not test",
    "faq": "FAQ",
}


@cache
def _example() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(EXAMPLE_REPORT.read_text("utf-8"))
    return data


def landing(request: HttpRequest) -> HttpResponse:
    return render(request, "pages/landing.html", {"plans": list(PLANS.values())})


def pricing(request: HttpRequest) -> HttpResponse:
    return render(request, "pages/pricing.html", {"plans": list(PLANS.values())})


def example_report(request: HttpRequest) -> HttpResponse:
    """A real report of the synthetic sample API (see ops/dev/make_example_report.py)."""
    report = _example()
    scan = Scan(
        status=ScanStatus.COMPLETED,
        kind=ScanKind.FULL,
        report=report,
        report_status=report["status"]["overall"],
        report_complete=report["status"]["complete"],
        planned_checks=report["coverage"]["planned_checks"],
    )
    return render(
        request, "pages/example_report.html", {"report": report_view(scan), "raw": report}
    )


def help_page(request: HttpRequest, slug: str = "getting-started") -> HttpResponse:
    if slug not in HELP_PAGES:
        raise Http404("No such help page")
    return render(
        request,
        f"pages/help/{slug}.html",
        {
            "help_pages": HELP_PAGES,
            "slug": slug,
            "title": HELP_PAGES[slug],
            "runner_wheel": runner_wheel() if slug == "runner" else None,
        },
    )


def terms(request: HttpRequest) -> HttpResponse:
    return render(request, "pages/terms.html")


def favicon(request: HttpRequest) -> HttpResponse:
    return redirect(static("accessguard/favicon.svg"), permanent=True)


def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness: the process answers. No dependencies are checked."""
    return JsonResponse({"status": "ok"})


def readyz(request: HttpRequest) -> JsonResponse:
    """Readiness: the database answers a trivial query."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # any database failure means not ready
        return JsonResponse({"status": "unavailable", "database": "error"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})
