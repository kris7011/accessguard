"""The runner API (``/api/runner/v1``): claim, heartbeat, report, fail.

Authenticated by a runner bearer token only; session cookies are not accepted, so CSRF
does not apply. A runner sees only scans it claimed: any other id answers 404.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accessguard_web import conf
from accessguard_web.scans import services
from accessguard_web.scans.models import Runner

RunnerView = Callable[..., HttpResponse]


def _error(status: int, message: str) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def runner_endpoint(view: Callable[..., HttpResponse]) -> RunnerView:
    @csrf_exempt
    @require_POST
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme != "Bearer" or not token:
            return _error(401, "runner token required")
        runner = services.authenticate_runner(token)
        if runner is None:
            return _error(401, "invalid or revoked runner token")
        if len(request.body) > conf.product()["MAX_REPORT_BYTES"]:
            return _error(413, "request body too large")
        try:
            payload = json.loads(request.body or b"{}")
        except ValueError:
            return _error(400, "body must be JSON")
        if not isinstance(payload, dict):
            return _error(400, "body must be a JSON object")
        return view(request, runner, payload, *args, **kwargs)

    return wrapper


@runner_endpoint
def claim(request: HttpRequest, runner: Runner, payload: dict[str, Any]) -> HttpResponse:
    scan = services.claim_next(runner)
    if scan is None:
        return HttpResponse(status=204)
    return JsonResponse(
        {"job": {"id": str(scan.pk), "spec": scan.spec, "spec_sha256": scan.spec_sha256}}
    )


@runner_endpoint
def heartbeat(
    request: HttpRequest, runner: Runner, payload: dict[str, Any], scan_id: int
) -> HttpResponse:
    scan = services.runner_scan(runner, scan_id)
    if scan is None:
        return _error(404, "unknown job")
    return JsonResponse({"cancel": services.heartbeat(scan, payload)})


@runner_endpoint
def report(
    request: HttpRequest, runner: Runner, payload: dict[str, Any], scan_id: int
) -> HttpResponse:
    scan = services.runner_scan(runner, scan_id)
    if scan is None:
        return _error(404, "unknown job")
    try:
        services.accept_report(scan, payload.get("report"))
    except services.InvalidReport as error:
        services.fail_scan(scan, "invalid_report", f"The runner's report was rejected: {error}")
        return _error(422, str(error))
    return JsonResponse({"ok": True})


@runner_endpoint
def fail(
    request: HttpRequest, runner: Runner, payload: dict[str, Any], scan_id: int
) -> HttpResponse:
    scan = services.runner_scan(runner, scan_id)
    if scan is None:
        return _error(404, "unknown job")
    category = payload.get("category")
    message = payload.get("message")
    services.fail_scan(
        scan,
        category if isinstance(category, str) else "",
        message if isinstance(message, str) else "",
    )
    return JsonResponse({"ok": True})
