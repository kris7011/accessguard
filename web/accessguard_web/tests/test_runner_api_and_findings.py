"""Runner API contract and the findings lifecycle, with reports produced by the real engine."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from typing import Any

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accessguard_sampleapp.app import Variant
from accessguard_web.scans import services
from accessguard_web.scans.models import (
    Finding,
    FindingEvent,
    FindingStatus,
    Label,
    Scan,
    ScanStatus,
)
from accessguard_web.tests.support import (
    expected_failures,
    make_org,
    org_runner,
    run_scan_for,
    sample_project,
    sample_runtime,
    scan_and_ingest,
)


def last_event(finding: Finding) -> FindingEvent:
    event = finding.events.last()
    assert event is not None
    return event


def runner_post(client: Any, url: str, token: str, body: object) -> Any:
    return client.post(
        url,
        data=json.dumps(body),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )


class RunnerApiTests(TestCase):
    def setUp(self) -> None:
        self.org, self.owner = make_org("Runner Co")
        self.project = sample_project(self.org, "http://127.0.0.1:9")
        self.runner, self.token = org_runner(self.org)

    def test_authentication_is_required_and_cookies_are_not_enough(self) -> None:
        url = reverse("runner_api:claim")
        self.assertEqual(self.client.post(url).status_code, 401)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(url).status_code, 401)
        self.assertEqual(runner_post(self.client, url, "agr_wrong", {}).status_code, 401)
        self.assertEqual(
            self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {self.token}").status_code, 405
        )

    def test_revoked_runner_is_rejected(self) -> None:
        self.runner.revoked_at = timezone.now()
        self.runner.save()
        self.assertEqual(
            runner_post(self.client, reverse("runner_api:claim"), self.token, {}).status_code, 401
        )

    def test_claim_returns_the_frozen_spec_once(self) -> None:
        scan = services.approve_scan(self.project, self.owner)
        response = runner_post(self.client, reverse("runner_api:claim"), self.token, {})
        self.assertEqual(response.status_code, 200)
        job = response.json()["job"]
        self.assertEqual(job["id"], str(scan.pk))
        self.assertEqual(job["spec_sha256"], scan.spec_sha256)
        self.assertNotIn("token", json.dumps(job).lower().replace("token_env", ""))
        _, other_token = org_runner(self.org)
        self.assertEqual(
            runner_post(self.client, reverse("runner_api:claim"), other_token, {}).status_code, 204
        )
        scan.refresh_from_db()
        self.assertEqual((scan.status, scan.runner_id), (ScanStatus.RUNNING, self.runner.pk))

    def test_project_bound_runner_is_respected(self) -> None:
        bound, bound_token = org_runner(self.org)
        self.project.runner = bound
        self.project.save()
        services.approve_scan(self.project, self.owner)
        self.assertEqual(
            runner_post(self.client, reverse("runner_api:claim"), self.token, {}).status_code, 204
        )
        self.assertEqual(
            runner_post(self.client, reverse("runner_api:claim"), bound_token, {}).status_code, 200
        )

    def test_heartbeat_reports_cancellation_and_rejects_other_runners(self) -> None:
        scan = services.approve_scan(self.project, self.owner)
        services.claim_next(self.runner)
        url = reverse("runner_api:heartbeat", args=[scan.pk])
        response = runner_post(
            self.client, url, self.token, {"phase": "checks", "completed": 3, "planned": 30}
        )
        self.assertEqual(response.json(), {"cancel": False})
        services.cancel_scan(scan, self.owner)
        self.assertEqual(runner_post(self.client, url, self.token, {}).json(), {"cancel": True})
        _, other_token = org_runner(self.org)
        self.assertEqual(runner_post(self.client, url, other_token, {}).status_code, 404)
        scan.refresh_from_db()
        self.assertEqual(scan.progress["completed"], 3)

    def test_cancelling_a_queued_scan_ends_it_without_results(self) -> None:
        scan = services.approve_scan(self.project, self.owner)
        services.cancel_scan(scan, self.owner)
        scan.refresh_from_db()
        self.assertEqual(scan.status, ScanStatus.CANCELLED)
        self.assertIsNone(services.claim_next(self.runner))

    def test_only_one_active_scan_per_project(self) -> None:
        services.approve_scan(self.project, self.owner)
        with self.assertRaises(services.ScanError):
            services.approve_scan(self.project, self.owner)

    def test_silent_runner_marks_the_scan_lost_never_passed(self) -> None:
        scan = services.approve_scan(self.project, self.owner)
        services.claim_next(self.runner)
        Scan.objects.filter(pk=scan.pk).update(
            last_heartbeat_at=timezone.now() - timedelta(minutes=5)
        )
        self.assertEqual(services.sweep_lost_scans(), 1)
        scan.refresh_from_db()
        self.assertEqual(scan.status, ScanStatus.LOST)
        self.assertFalse(scan.shows_as_passed)

    def test_runner_failure_is_recorded(self) -> None:
        scan = services.approve_scan(self.project, self.owner)
        services.claim_next(self.runner)
        runner_post(self.client, reverse("runner_api:fail", args=[scan.pk]), self.token,
                    {"category": "scope_rejected", "message": "not in allowlist"})  # fmt: skip
        scan.refresh_from_db()
        self.assertEqual(
            (scan.status, scan.failure_category), (ScanStatus.FAILED, "scope_rejected")
        )


class ReportValidationTests(TestCase):
    def test_reports_that_do_not_match_the_approved_scan_are_rejected(self) -> None:
        org, owner = make_org("Validate")
        with sample_runtime() as (app, config):
            project = sample_project(org, app.origin)
            runner, token = org_runner(org)
            scan = services.approve_scan(project, owner)
            services.claim_next(runner)
            good = run_scan_for(scan, config)
        tampered: list[tuple[str, dict[str, Any]]] = []
        other_spec = copy.deepcopy(good)
        other_spec["run"]["spec_sha256"] = "0" * 64
        tampered.append(("spec hash", other_spec))
        extra = copy.deepcopy(good)
        extra["results"].append({**extra["results"][0], "check_id": "invented@someone"})
        tampered.append(("extra check", extra))
        hidden = copy.deepcopy(good)
        hidden["findings"] = []
        tampered.append(("hidden findings", hidden))
        bad_status = copy.deepcopy(good)
        bad_status["status"]["overall"] = "secure"
        tampered.append(("status", bad_status))
        for name, report in tampered:
            with self.subTest(case=name), self.assertRaises(services.InvalidReport):
                services.validate_report(scan, report)
        response = runner_post(
            self.client, reverse("runner_api:report", args=[scan.pk]), token, {"report": other_spec}
        )
        self.assertEqual(response.status_code, 422)
        scan.refresh_from_db()
        self.assertEqual(
            (scan.status, scan.failure_category), (ScanStatus.FAILED, "invalid_report")
        )
        self.assertFalse(Finding.objects.exists())


class FindingsLifecycleTests(TestCase):
    def setUp(self) -> None:
        self.org, self.owner = make_org("Lifecycle")
        self.runner, self.token = org_runner(self.org)

    def test_scan_fix_retest_resolve_and_regression(self) -> None:
        with (
            sample_runtime(Variant.FLAWED) as (flawed, flawed_config),
            sample_runtime(Variant.FIXED) as (fixed, fixed_config),
        ):
            project = sample_project(self.org, flawed.origin)
            scan = scan_and_ingest(project, self.owner, self.runner, flawed_config)
            self.assertEqual(scan.status, ScanStatus.COMPLETED)
            self.assertEqual(
                set(Finding.objects.values_list("check_id", flat=True)), expected_failures("flawed")
            )
            self.assertTrue(all(f.status == FindingStatus.OPEN for f in Finding.objects.all()))

            # The team "deploys the fix": the same configuration now points at the fixed app.
            project.config["target"]["origin"] = fixed.origin
            project.save()
            open_findings = list(Finding.objects.filter(project=project))
            retest = scan_and_ingest(project, self.owner, self.runner, fixed_config, open_findings)
            self.assertEqual(retest.kind, "retest")
            self.assertGreater(
                retest.planned_checks, len(open_findings), "positive controls are included"
            )
            self.assertFalse(
                retest.shows_as_passed, "a retest subset is never shown as a full pass"
            )
            for finding in Finding.objects.all():
                self.assertEqual(
                    (finding.status, finding.resolution_verified), (FindingStatus.RESOLVED, True)
                )
                self.assertEqual(last_event(finding).label, Label.FIXED_VERIFIED)

            # The bug comes back: the next full scan reopens the same findings.
            project.config["target"]["origin"] = flawed.origin
            project.save()
            scan_and_ingest(project, self.owner, self.runner, flawed_config)
            self.assertEqual(Finding.objects.count(), len(expected_failures("flawed")))
            for finding in Finding.objects.all():
                self.assertEqual(finding.status, FindingStatus.OPEN)
                self.assertIn("Regression", last_event(finding).note)

    def test_inconclusive_retest_never_resolves(self) -> None:
        with sample_runtime(Variant.FLAWED) as (app, config):
            project = sample_project(self.org, app.origin)
            scan_and_ingest(project, self.owner, self.runner, config)
            finding = Finding.objects.get(check_id="orbit-billing@ben")
            retest = services.create_retest(project, self.owner, [finding])
            services.claim_next(self.runner)
            # The runner lost ben's credential: his checks are inconclusive.
            assert config.targets[0].credentials_file is not None
            creds = json.loads(config.targets[0].credentials_file.read_text("utf-8"))
            del creds["ben"]
            config.targets[0].credentials_file.write_text(json.dumps(creds), "utf-8")
            report = run_scan_for(retest, config)
            services.accept_report(retest, report)
        finding.refresh_from_db()
        self.assertEqual(finding.status, FindingStatus.OPEN)
        self.assertIn("not evidence of a fix", last_event(finding).note)
        retest.refresh_from_db()
        self.assertEqual(retest.report_status, "incomplete")

    def test_triage_records_labels_and_requires_reasons_for_rejection(self) -> None:
        with sample_runtime(Variant.FLAWED) as (app, config):
            project = sample_project(self.org, app.origin)
            scan_and_ingest(project, self.owner, self.runner, config)
        finding = Finding.objects.get(check_id="list:projects@ana")
        with self.assertRaises(services.ScanError):
            services.triage(finding, self.owner, FindingStatus.REJECTED, "")
        services.triage(finding, self.owner, FindingStatus.CONFIRMED, "real leak")
        self.assertEqual(last_event(finding).label, Label.TRUE_POSITIVE)
        services.triage(finding, self.owner, FindingStatus.RESOLVED, "deployed")
        finding.refresh_from_db()
        self.assertFalse(finding.resolution_verified)
        self.assertIn("not yet verified", last_event(finding).note)
        other = Finding.objects.get(check_id="list:projects@ben")
        services.triage(other, self.owner, FindingStatus.REJECTED, "intended for viewers")
        self.assertEqual(last_event(other).label, Label.FALSE_POSITIVE)

    def test_retest_of_a_check_removed_from_the_configuration_is_refused(self) -> None:
        with sample_runtime(Variant.FLAWED) as (app, config):
            project = sample_project(self.org, app.origin)
            scan_and_ingest(project, self.owner, self.runner, config)
        finding = Finding.objects.get(check_id="orbit-billing@ben")
        project.config["policy"]["resources"] = [
            r for r in project.config["policy"]["resources"] if r["id"] != "orbit-billing"
        ]
        project.save()
        with self.assertRaises(services.ScanError):
            services.create_retest(project, self.owner, [finding])

    def test_retest_page_with_an_invalid_configuration_explains_instead_of_failing(self) -> None:
        with sample_runtime(Variant.FLAWED) as (app, config):
            project = sample_project(self.org, app.origin)
            scan_and_ingest(project, self.owner, self.runner, config)
        finding = Finding.objects.first()
        assert finding is not None
        project.config["policy"]["identities"] = []
        project.save()
        client = self.client
        client.force_login(self.owner)
        response = client.get(
            reverse("scans:review", args=[project.pk]) + f"?finding={finding.pk}", follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not complete yet")
