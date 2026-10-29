"""Sign-up and onboarding, invitations, sign-in throttling, deletion, forms, headers, logs."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accessguard_web.accounts.models import Invitation, Membership, Organization, User
from accessguard_web.billing.models import Subscription
from accessguard_web.observability import JsonFormatter, RedactionFilter, redact
from accessguard_web.projects.forms import IdentityForm, TargetForm
from accessguard_web.scans.models import Runner
from accessguard_web.tests.support import PASSWORD, login, make_org, make_user

WEB = Path(__file__).resolve().parents[1]


class SignupTests(TestCase):
    def test_signup_creates_organisation_owner_demo_and_free_plan(self) -> None:
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "Founder@Example.test",
                "password": PASSWORD,
                "organization": "Acme",
                "accept_terms": "on",
            },
            follow=True,
        )
        self.assertContains(response, "Run the demo scan")
        user = User.objects.get(email="founder@example.test")
        organization = Organization.objects.get(name="Acme")
        self.assertEqual(Membership.objects.get(user=user, organization=organization).role, "owner")
        self.assertTrue(organization.projects.filter(is_demo=True).exists())
        self.assertEqual(Subscription.objects.get(organization=organization).plan, "free")
        self.assertFalse(organization.ai_assistance_enabled, "AI assistance is opt-in")
        self.assertFalse(organization.ml_data_consent, "learning consent is opt-in")

    def test_weak_passwords_and_missing_authorisation_are_refused(self) -> None:
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "email": "a@b.test",
                "password": "password",  # pragma: allowlist secret
                "organization": "X",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())


class LoginThrottleTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        make_user("user@example.test")

    def test_repeated_failures_lock_out_even_the_right_password(self) -> None:
        for _ in range(10):
            self.client.post(
                reverse("accounts:login"),
                {"username": "user@example.test", "password": "wrong"},  # pragma: allowlist secret
            )
        response = self.client.post(
            reverse("accounts:login"), {"username": "user@example.test", "password": PASSWORD}
        )
        self.assertContains(response, "Too many failed sign-in attempts")
        self.assertNotIn("_auth_user_id", self.client.session)


class InvitationTests(TestCase):
    def test_invitation_link_is_single_use_and_bound_to_its_address(self) -> None:
        organization, owner = make_org("Team")
        _, token = Invitation.issue(organization, "new@team.test", "admin", owner)
        url = reverse("accounts:accept_invite", args=[token])
        stranger = make_user("stranger@team.test")
        login(stranger).post(url)
        self.assertFalse(Membership.objects.filter(user=stranger).exists())
        invited = make_user("new@team.test")
        client = login(invited)
        client.post(url)
        self.assertEqual(Membership.objects.get(user=invited).role, "admin")
        self.assertEqual(client.get(url).status_code, 404, "a used invitation is invalid")
        self.assertNotIn(token, Invitation.objects.get().token_hash)


class DeletionTests(TestCase):
    def test_owner_deletes_the_organisation_and_all_its_data(self) -> None:
        organization, owner = make_org("Gone")
        Runner.issue(name="r", organization=organization)
        client = login(owner)
        client.post(reverse("accounts:delete_organization"), {"confirm": "wrong"})
        self.assertTrue(Organization.objects.filter(pk=organization.pk).exists())
        client.post(reverse("accounts:delete_organization"), {"confirm": "Gone"})
        self.assertFalse(Organization.objects.filter(pk=organization.pk).exists())
        self.assertFalse(Runner.objects.exists())


class FormTests(TestCase):
    def target(self, **data: str) -> TargetForm:
        base = {"origin": "https://staging.example.com", "path_prefixes": "/api", "auth_kind": "bearer",
                "session_path": "/api/me", "subject_field": "email"}  # fmt: skip
        return TargetForm({**base, **data})

    def test_target_form_accepts_valid_and_rejects_unsafe_targets(self) -> None:
        self.assertTrue(self.target().is_valid())
        for change in (
            {"origin": "http://staging.example.com"},
            {"origin": "https://staging.example.com/api"},
            {"origin": "ftp://x.test"},
            {"path_prefixes": "api"},
            {"session_path": "/other/me"},
            {"auth_kind": "header", "auth_name": ""},
            {"auth_kind": "header", "auth_name": "Host"},
            {"subject_field": "a b"},
        ):
            with self.subTest(change=change):
                self.assertFalse(self.target(**change).is_valid())
        self.assertTrue(self.target(origin="http://localhost:8080").is_valid())

    def test_identity_form_uses_the_engine_rules(self) -> None:
        self.assertTrue(IdentityForm({"id": "alice", "tenant": "acme", "role": "admin"}).is_valid())
        for bad in ({"id": "anonymous"}, {"id": "Alice"}, {"tenant": "a b"}):
            with self.subTest(bad=bad):
                data = {"id": "alice", "tenant": "acme", "role": "admin", **bad}
                self.assertFalse(IdentityForm(data).is_valid())


class HeaderTests(TestCase):
    def test_security_headers_are_set(self) -> None:
        response = self.client.get(reverse("pages:landing"))
        csp = response["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("X-Request-Id", response)

    def test_no_template_uses_inline_scripts_or_styles(self) -> None:
        for path in (WEB / "templates").rglob("*.html"):
            text = path.read_text("utf-8")
            with self.subTest(template=str(path.relative_to(WEB))):
                self.assertNotIn(" style=", text)
                self.assertNotIn("<script>", text)
                self.assertNotIn(" onclick=", text)

    def test_stylesheet_and_script_are_found_and_linked(self) -> None:
        from django.contrib.staticfiles import finders

        for asset in ("accessguard/app.css", "accessguard/app.js"):
            with self.subTest(asset=asset):
                self.assertIsNotNone(finders.find(asset))
        page = self.client.get(reverse("pages:landing")).content.decode()
        self.assertIn("accessguard/app.css", page)
        self.assertIn("accessguard/app.js", page)

    def test_health_endpoints(self) -> None:
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/readyz").json()["database"], "ok")


class LoggingTests(TestCase):
    def test_credentials_are_redacted_from_log_lines(self) -> None:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "token %s and %s",
                                   ("agr_abcdefghijklmnopqrstuvwxyz", "Bearer eyJhbGciOiJIUzI1NiJ9.payload"), None)  # fmt: skip  # pragma: allowlist secret
        RedactionFilter().filter(record)
        line = JsonFormatter().format(record)
        self.assertNotIn("agr_abcdefgh", line)
        self.assertNotIn("eyJhbGci", line)
        self.assertIn("[REDACTED]", line)
        self.assertEqual(redact("whsec_abcdefghijk sk_live_abcdefghijk password=hunter22"),
                         "[REDACTED] [REDACTED] password=[REDACTED]")  # fmt: skip


class ArchitectureTests(TestCase):
    def test_product_code_never_imports_lab_sample_or_evaluation_code(self) -> None:
        forbidden = ("accessguard_lab", "accessguard_eval", "accessguard_sampleapp")
        for path in WEB.rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                )
                for name in names:
                    with self.subTest(file=path.name, module=name):
                        self.assertFalse(name.startswith(forbidden))
