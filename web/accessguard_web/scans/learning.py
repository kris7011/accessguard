"""De-identified, consent-gated export of verified labels for the ranking research (ml/).

Only organisations with ``ml_data_consent`` are exported. A row contains the engine's
``features-v1`` vector (enumerations and counts only), a label, and provenance; never
names, ids, paths, field names or report text. The application group is an HMAC of the
project id with the server's secret, so rows of one project stay together without
revealing which project they came from.

Labels:

* 1: a finding a person confirmed, or whose fix a conclusive retest verified.
* 0: a finding a person rejected, or a check that passed in a complete full scan.
* Everything else (unreviewed findings, inconclusive, skipped, errors) is not exported.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from typing import Any

from django.conf import settings

from accessguard.adapters.scan_spec import parse_scan_spec
from accessguard.application.ranking import FEATURE_VERSION, check_features
from accessguard.application.scan import select_checks
from accessguard_web.scans.models import FindingEvent, Label, Scan, ScanKind, ScanStatus


def application_group(project_id: int) -> str:
    key = str(settings.SECRET_KEY).encode()
    return "app-" + hmac.new(key, f"project:{project_id}".encode(), hashlib.sha256).hexdigest()[:16]


def _human_labels(project_id: int) -> dict[str, tuple[int, str]]:
    """check id -> (label, provenance) from the latest labelled event of each finding."""
    labels: dict[str, tuple[int, str]] = {}
    events = FindingEvent.objects.filter(finding__project_id=project_id).exclude(label="")
    for event in events.select_related("finding").order_by("created_at", "id"):
        value = 0 if event.label == Label.FALSE_POSITIVE else 1
        labels[event.finding.check_id] = (value, str(event.label))
    return labels


def export_rows() -> Iterator[dict[str, Any]]:
    scans = (
        Scan.objects.filter(
            organization__ml_data_consent=True,
            status=ScanStatus.COMPLETED,
            kind=ScanKind.FULL,
            report_complete=True,
            is_demo=False,
        )
        .select_related("project")
        .order_by("project_id", "-finished_at")
    )
    seen_projects: set[int] = set()
    for scan in scans:
        if scan.project_id in seen_projects or scan.report is None:
            continue  # the latest complete full scan per project describes its checks
        seen_projects.add(scan.project_id)
        spec = parse_scan_spec(scan.spec)
        policy = spec.policy.policy
        checks, _ = select_checks(policy, None)
        outcomes = {r["check_id"]: r["outcome"] for r in scan.report["results"]}
        human = _human_labels(scan.project_id)
        group = application_group(scan.project_id)
        for check in checks:
            if check.id in human:
                label, source = human[check.id]
            elif outcomes.get(check.id) == "PASS":
                label, source = 0, "engine_pass"
            else:
                continue
            yield {
                "application": group,
                "check_kind": check.kind,
                "feature_version": FEATURE_VERSION,
                "features": check_features(policy, check),
                "label": label,
                "provenance": {
                    "label_source": source,
                    "engine_version": str(scan.report["tool"]["version"]),
                },
            }
