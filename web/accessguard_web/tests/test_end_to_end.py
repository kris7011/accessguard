"""The whole customer journey over real HTTP, with a real runner and the separate sample app.

Sign up → configure a project only through the forms → create a runner token → approve →
the runner (the product CLI's loop) claims the job over HTTP, scans the flawed sample app
and uploads its report → findings → "deploy" the fix → retest → resolved. A second flow
cancels a running scan. Nothing here is mocked except time between polls.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

from django.test import Client, LiveServerTestCase
from django.urls import reverse

from accessguard.adapters.control_plane import ControlPlaneClient
from accessguard.adapters.runner_config import parse_runner_config
from accessguard.domain.credentials import Secret
from accessguard.entrypoints import runner_service
from accessguard.entrypoints.runner_service import run_forever
from accessguard_sampleapp.app import Variant
from accessguard_sampleapp.server import new_api_keys, run_sample_app
from accessguard_web.projects.models import Project
from accessguard_web.scans.models import Finding, FindingStatus, Scan, ScanStatus
from accessguard_web.tests.support import PASSWORD, expected_failures

USERS = [
    ("ana", "orbit", "owner", "ana@orbit.example"),
    ("ben", "orbit", "viewer", "ben@orbit.example"),
    ("cleo", "quasar", "owner", "cleo@quasar.example"),
    ("dev", "quasar", "viewer", "dev@quasar.example"),
]
RECORDS = [
    ("orbit-apollo", "project", "/api/v1/projects/prj-orbit-apollo", "ana", "data.name, data.budget_code", "projects", ["orbit:owner", "orbit:viewer"]),
    ("quasar-nova", "project", "/api/v1/projects/prj-quasar-nova", "cleo", "data.name, data.budget_code", "projects", ["quasar:owner", "quasar:viewer"]),
    ("quasar-roadmap", "shared-project", "/api/v1/projects/prj-quasar-roadmap", "cleo", "data.name, data.budget_code", "projects",
     ["quasar:owner", "quasar:viewer", "orbit:owner", "orbit:viewer"]),
    ("orbit-billing", "billing", "/api/v1/orgs/orbit/billing", "ana", "data.account_ref, data.plan_note", "", ["orbit:owner"]),
    ("quasar-billing", "billing", "/api/v1/orgs/quasar/billing", "cleo", "data.account_ref, data.plan_note", "", ["quasar:owner"]),
]  # fmt: skip


def formset(prefix: str, rows: list[dict[str, Any]], initial: int = 0) -> dict[str, Any]:
    """POST data as the browser sends it; ``initial`` is the number of rows already saved."""
    data: dict[str, Any] = {
        f"{prefix}-TOTAL_FORMS": str(max(len(rows), initial)),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for index, row in enumerate(rows):
        for key, value in row.items():
            data[f"{prefix}-{index}-{key}"] = value
    return data


class CustomerJourneyTests(LiveServerTestCase):
    def setUp(self) -> None:
        self.web = Client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys = new_api_keys()
        Path(self.tmp.name, "credentials.json").write_text(json.dumps(self.keys), "utf-8")

    def runner(self, token: str, origins: list[str]) -> tuple[ControlPlaneClient, Any]:
        config = parse_runner_config(
            {
                "control_plane": {"url": self.live_server_url},
                "targets": [
                    {"origin": o, "zone": "loopback", "credentials_file": "credentials.json"}
                    for o in origins
                ],
                "poll_seconds": 1,
            },
            base_dir=Path(self.tmp.name),
        )
        return ControlPlaneClient(self.live_server_url, Secret(token)), config

    def run_runner_once(self, token: str, origins: list[str]) -> None:
        client, config = self.runner(token, origins)
        self.assertEqual(
            run_forever(client, config, stop=threading.Event(), once=True, log=lambda m: None), 0
        )

    def ok(self, response: Any) -> Any:
        self.assertLess(response.status_code, 400, response.content[:2000])
        return response

    def configure(self, project: int, origin: str) -> None:
        self.ok(self.web.post(reverse("projects:setup_target", args=[project]), {
            "origin": origin, "path_prefixes": "/api/v1", "auth_kind": "header", "auth_name": "X-Api-Key",
            "session_path": "/api/v1/whoami", "subject_field": "data.email",
        }))  # fmt: skip
        policy = Project.objects.get(pk=project).config["policy"]
        users = [{"id": i, "tenant": t, "role": r, "subject": s} for i, t, r, s in USERS]
        self.ok(self.web.post(reverse("projects:setup_users", args=[project]),
                              formset("users", users, len(policy["identities"]))))  # fmt: skip
        lists = formset(
            "lists", [{"id": "projects", "path": "/api/v1/projects"}], len(policy["collections"])
        )
        records = formset("records", [
            {"id": i, "category": c, "path": p, "owner": o, "protected_fields": f, "collection": col, "access": a}
            for i, c, p, o, f, col, a in RECORDS
        ], len(policy["resources"]))  # fmt: skip
        self.ok(
            self.web.post(reverse("projects:setup_rules", args=[project]), {**lists, **records})
        )
        self.ok(self.web.post(reverse("projects:setup_limits", args=[project]),
                              {"max_requests": 300, "min_interval_seconds": 0, "max_duration_seconds": 120}))  # fmt: skip

    def test_sign_up_configure_scan_fix_retest(self) -> None:
        with (
            run_sample_app(Variant.FLAWED, api_keys=self.keys) as flawed,
            run_sample_app(Variant.FIXED, api_keys=self.keys) as fixed,
        ):
            response = self.ok(self.web.post(reverse("accounts:signup"), {
                "email": "lead@customer.test", "password": PASSWORD, "organization": "Customer",
                "accept_terms": "on",
            }, follow=True))  # fmt: skip
            self.assertContains(response, "Get started")
            response = self.ok(self.web.post(reverse("projects:new"), {"name": "Staging API"}))
            project = int(re.search(r"/app/projects/(\d+)/", response["Location"]).group(1))  # type: ignore[union-attr]
            self.configure(project, flawed.origin)
            detail = self.ok(self.web.get(reverse("projects:detail", args=[project])))
            self.assertContains(detail, "30 checks will run")

            page = self.ok(self.web.post(reverse("scans:runner_create"), {"name": "staging"}))
            token = re.search(
                r'<pre id="runner-token">(agr_[^<]+)</pre>', page.content.decode()
            ).group(1)  # type: ignore[union-attr]
            self.assertEqual(page["Cache-Control"], "no-store")
            self.assertNotContains(self.web.get(reverse("scans:runners")), token, status_code=200)

            review = self.ok(self.web.get(reverse("scans:review", args=[project])))
            self.assertContains(review, flawed.origin)
            self.assertContains(review, "GET only")
            approved = self.ok(
                self.web.post(reverse("scans:review", args=[project]), {"authorised": "yes"})
            )
            scan = Scan.objects.get(pk=int(approved["Location"].rstrip("/").rsplit("/", 1)[-1]))
            self.run_runner_once(token, [flawed.origin, fixed.origin])

            scan.refresh_from_db()
            self.assertEqual((scan.status, scan.report_status), (ScanStatus.COMPLETED, "findings"))
            page = self.ok(self.web.get(reverse("scans:detail", args=[scan.pk])))
            self.assertContains(page, "10 confirmed access-control violations")
            self.assertEqual(
                set(Finding.objects.values_list("check_id", flat=True)), expected_failures("flawed")
            )
            stored = json.dumps(scan.report)
            for key in self.keys.values():
                self.assertNotIn(key, stored)
            self.assertNotIn("SAMPLE-BUDGET-ORBIT-7731", stored)

            # Deploy the fix: the team points the project at the fixed build and retests.
            self.configure(project, fixed.origin)
            ids = [str(pk) for pk in Finding.objects.values_list("pk", flat=True)]
            self.ok(
                self.web.post(
                    reverse("scans:review", args=[project]), {"authorised": "yes", "finding": ids}
                )
            )
            self.run_runner_once(token, [flawed.origin, fixed.origin])
            retest = Scan.objects.latest("pk")
            self.assertEqual((retest.kind, retest.report_status), ("retest", "checks_passed"))
            self.assertContains(
                self.web.get(reverse("scans:detail", args=[retest.pk])), "Retest passed"
            )
            self.assertEqual(
                set(Finding.objects.values_list("status", "resolution_verified")),
                {(FindingStatus.RESOLVED, True)},
            )

    def test_cancelling_a_running_scan_keeps_a_partial_report(self) -> None:
        # Faster heartbeats keep the test short; the production interval is 3 seconds.
        with (
            run_sample_app(Variant.FLAWED, api_keys=self.keys) as flawed,
            mock.patch.object(runner_service, "HEARTBEAT_SECONDS", 0.2),
        ):
            self.ok(self.web.post(reverse("accounts:signup"), {
                "email": "ops@customer.test", "password": PASSWORD, "organization": "Customer",
                "accept_terms": "on",
            }))  # fmt: skip
            response = self.ok(self.web.post(reverse("projects:new"), {"name": "Slow API"}))
            project = int(re.search(r"/app/projects/(\d+)/", response["Location"]).group(1))  # type: ignore[union-attr]
            self.configure(project, flawed.origin)
            self.ok(self.web.post(reverse("projects:setup_limits", args=[project]),
                                  {"max_requests": 300, "min_interval_seconds": 0.3, "max_duration_seconds": 120}))  # fmt: skip
            page = self.ok(self.web.post(reverse("scans:runner_create"), {"name": "slow"}))
            token = re.search(
                r'<pre id="runner-token">(agr_[^<]+)</pre>', page.content.decode()
            ).group(1)  # type: ignore[union-attr]
            self.ok(self.web.post(reverse("scans:review", args=[project]), {"authorised": "yes"}))
            scan = Scan.objects.latest("pk")
            worker = threading.Thread(target=self.run_runner_once, args=(token, [flawed.origin]))
            worker.start()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                scan.refresh_from_db()
                if scan.status == ScanStatus.RUNNING and scan.progress.get("completed", 0) >= 2:
                    break
                time.sleep(0.2)
            self.ok(self.web.post(reverse("scans:cancel", args=[scan.pk])))
            worker.join(timeout=60)
            scan.refresh_from_db()
            self.assertEqual(scan.status, ScanStatus.CANCELLED)
            assert scan.report is not None
            self.assertFalse(scan.report_complete)
            self.assertEqual(scan.report["run"]["interrupted"], "run cancelled by stop signal")
            self.assertNotEqual(scan.report_status, "checks_passed")
            page = self.ok(self.web.get(reverse("scans:detail", args=[scan.pk])))
            self.assertNotContains(page, "declared checks passed")
