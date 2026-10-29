"""Organisation isolation and role checks on every product route."""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from accessguard_web.accounts.models import AuditEvent, Organization, User
from accessguard_web.projects.models import Project
from accessguard_web.scans import services
from accessguard_web.scans.models import Finding, Runner, Scan
from accessguard_web.tests.support import (
    ROLE_ADMIN,
    ROLE_VIEWER,
    add_member,
    login,
    make_org,
    org_runner,
    sample_project,
)


class IsolationTests(TestCase):
    org_a: Organization
    owner_a: User
    org_b: Organization
    owner_b: User
    project_b: Project
    scan_b: Scan
    runner_b: Runner
    finding_b: Finding

    @classmethod
    def setUpTestData(cls) -> None:
        cls.org_a, cls.owner_a = make_org("Alpha")
        cls.org_b, cls.owner_b = make_org("Beta")
        cls.project_b = sample_project(cls.org_b, "http://127.0.0.1:9")
        cls.scan_b = services.approve_scan(cls.project_b, cls.owner_b)
        cls.runner_b, _ = org_runner(cls.org_b)
        cls.finding_b = Finding.objects.create(
            organization=cls.org_b, project=cls.project_b, fingerprint="x|y", check_id="x",
            finding_type="unauthorized_access", outcome="FAIL",
        )  # fmt: skip

    def test_other_organisations_records_are_not_found(self) -> None:
        client = login(self.owner_a)
        project, scan, finding = self.project_b.pk, self.scan_b.pk, self.finding_b.pk
        gets = [
            reverse("projects:detail", args=[project]),
            reverse("projects:setup_target", args=[project]),
            reverse("projects:setup_rules", args=[project]),
            reverse("projects:export_spec", args=[project]),
            reverse("scans:review", args=[project]),
            reverse("scans:detail", args=[scan]),
            reverse("scans:status", args=[scan]),
            reverse("scans:report_json", args=[scan]),
            reverse("scans:finding", args=[finding]),
        ]
        for url in gets:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 404)
        posts = [
            reverse("scans:cancel", args=[scan]),
            reverse("scans:triage", args=[finding]),
            reverse("scans:explain", args=[finding]),
            reverse("scans:runner_revoke", args=[self.runner_b.pk]),
            reverse("projects:delete", args=[project]),
            reverse("projects:demo_variant", args=[project]),
        ]
        for url in posts:
            with self.subTest(url=url):
                self.assertEqual(
                    client.post(url, {"status": "rejected", "note": "x"}).status_code, 404
                )
        self.scan_b.refresh_from_db()
        self.assertEqual(self.scan_b.status, "queued")
        self.finding_b.refresh_from_db()
        self.assertEqual(self.finding_b.status, "open")

    def test_lists_only_show_the_current_organisation(self) -> None:
        client = login(self.owner_a)
        self.assertNotContains(client.get(reverse("dashboard")), "Staging API")
        self.assertNotContains(
            client.get(reverse("scans:findings") + "?status=all"), "unauthorized"
        )

    def test_anonymous_users_are_sent_to_sign_in(self) -> None:
        response = self.client.get(reverse("projects:detail", args=[self.project_b.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_switching_to_an_organisation_you_do_not_belong_to_is_ignored(self) -> None:
        client = login(self.owner_a)
        client.post(reverse("accounts:switch", args=[self.org_b.pk]))
        self.assertEqual(
            client.get(reverse("projects:detail", args=[self.project_b.pk])).status_code, 404
        )


class RoleTests(TestCase):
    org: Organization
    owner: User
    viewer: User
    admin: User
    project: Project

    @classmethod
    def setUpTestData(cls) -> None:
        cls.org, cls.owner = make_org("Gamma")
        cls.viewer = add_member(cls.org, "viewer@gamma.test", ROLE_VIEWER)
        cls.admin = add_member(cls.org, "admin@gamma.test", ROLE_ADMIN)
        cls.project = sample_project(cls.org, "http://127.0.0.1:9")

    def test_viewers_can_read_but_not_change(self) -> None:
        client = login(self.viewer)
        self.assertEqual(
            client.get(reverse("projects:detail", args=[self.project.pk])).status_code, 200
        )
        self.assertEqual(
            client.get(reverse("projects:setup_target", args=[self.project.pk])).status_code, 200
        )
        forbidden = [
            ("post", reverse("projects:setup_target", args=[self.project.pk])),
            ("get", reverse("scans:review", args=[self.project.pk])),
            ("post", reverse("scans:review", args=[self.project.pk])),
            ("get", reverse("scans:runners")),
            ("post", reverse("scans:runner_create")),
            ("get", reverse("projects:new")),
            ("post", reverse("accounts:invite")),
        ]
        for method, url in forbidden:
            with self.subTest(url=url, method=method):
                response = getattr(client, method)(url, {"authorised": "yes", "name": "x"})
                self.assertEqual(response.status_code, 403)
        self.assertFalse(Scan.objects.exists())

    def test_admins_approve_scans_but_owners_manage_billing_and_members(self) -> None:
        client = login(self.admin)
        response = client.post(
            reverse("scans:review", args=[self.project.pk]), {"authorised": "yes"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Scan.objects.get().approved_by, self.admin)
        self.assertEqual(client.post(reverse("billing:checkout", args=["team"])).status_code, 403)
        self.assertEqual(
            client.post(reverse("accounts:invite"), {"email": "x@y.test"}).status_code, 403
        )

    def test_approval_requires_the_authorisation_confirmation(self) -> None:
        client = login(self.admin)
        response = client.post(reverse("scans:review", args=[self.project.pk]), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirm that you are authorised")
        self.assertFalse(Scan.objects.exists())

    def test_approval_is_audited_with_the_spec_hash(self) -> None:
        scan = services.approve_scan(self.project, self.admin)
        event = AuditEvent.objects.get(action="scan.approved")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.detail["spec_sha256"], scan.spec_sha256)

    def test_last_owner_cannot_be_demoted_or_removed(self) -> None:
        client = login(self.owner)
        membership = self.owner.memberships.get()
        client.post(reverse("accounts:change_role", args=[membership.pk]), {"role": "viewer"})
        client.post(reverse("accounts:change_role", args=[membership.pk]), {"remove": "yes"})
        membership.refresh_from_db()
        self.assertEqual(membership.role, "owner")


class DemoProjectTests(TestCase):
    def test_demo_configuration_cannot_be_edited_and_only_scans_demo_origins(self) -> None:
        org, owner = make_org("Delta")
        demo = org.projects.get(is_demo=True)
        client = login(owner)
        before = dict(demo.config)
        client.post(
            reverse("projects:setup_target", args=[demo.pk]), {"origin": "https://evil.test"}
        )
        demo.refresh_from_db()
        self.assertEqual(demo.config, before)
        demo.config["target"]["origin"] = "http://127.0.0.1:9999"
        demo.save()
        with self.assertRaises(services.ScanError):
            services.approve_scan(demo, owner)

    def test_demo_scans_are_claimed_only_by_the_platform_demo_runner(self) -> None:
        org, owner = make_org("Epsilon")
        demo = org.projects.get(is_demo=True)
        scan = services.approve_scan(demo, owner)
        own_runner, _ = org_runner(org)
        self.assertIsNone(services.claim_next(own_runner))
        demo_runner, _ = Runner.issue(name="demo", organization=None, is_platform_demo=True)
        claimed = services.claim_next(demo_runner)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.pk, scan.pk)


class CrossOrgRunnerTests(TestCase):
    def test_a_runner_never_claims_or_reports_for_another_organisation(self) -> None:
        org_a, owner_a = make_org("Zeta")
        org_b, _ = make_org("Eta")
        project = sample_project(org_a, "http://127.0.0.1:9")
        runner_b, _ = org_runner(org_b)
        scan = services.approve_scan(project, owner_a)
        self.assertIsNone(services.claim_next(runner_b))
        runner_a, _ = org_runner(org_a)
        claimed = services.claim_next(runner_a)
        assert claimed is not None
        self.assertIsNone(services.runner_scan(runner_b, scan.pk))
        self.assertIsNotNone(services.runner_scan(runner_a, scan.pk))
