"""The pre-deployment check of a production .env: right combination, names only, no values."""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import REPO_ROOT

_spec = importlib.util.spec_from_file_location(
    "check_env", REPO_ROOT / "ops" / "deploy" / "check_env.py"
)
assert _spec and _spec.loader
check_env = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check_env  # dataclasses look up their module while it loads
_spec.loader.exec_module(check_env)

# Made-up values in the right shape (not credentials).
VALID = {
    "ACCESSGUARD_SECRET_KEY": "k" * 60,
    "ACCESSGUARD_ENVIRONMENT": "production",
    "ACCESSGUARD_DOMAIN": "app.accessguard.invalid",
    "ACCESSGUARD_ALLOWED_HOSTS": "app.accessguard.invalid,127.0.0.1,web",
    "ACCESSGUARD_CSRF_TRUSTED_ORIGINS": "https://app.accessguard.invalid",
    "ACCESSGUARD_PUBLIC_URL": "https://app.accessguard.invalid",
    "ACCESSGUARD_BEHIND_PROXY": "true",
    "ACCESSGUARD_TRUSTED_PROXY_HOPS": "1",
    "ACCESSGUARD_SSL_REDIRECT": "false",
    "ACCESSGUARD_SECURE_COOKIES": "true",
    "ACCESSGUARD_HSTS_SECONDS": "31536000",
    "ACCESSGUARD_ADMIN_PATH": "ops-7f3k/",
    "ACME_EMAIL": "ops@accessguard.invalid",
    "POSTGRES_PASSWORD": "p" * 32,
    "ACCESSGUARD_EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
    "ACCESSGUARD_EMAIL_HOST": "smtp.mail.invalid",
    "ACCESSGUARD_EMAIL_PORT": "587",
    "ACCESSGUARD_EMAIL_USER": "mailer",
    "ACCESSGUARD_EMAIL_PASSWORD": "smtp-secret-value-91",  # pragma: allowlist secret
    "ACCESSGUARD_FROM_EMAIL": "AccessGuard <no-reply@accessguard.invalid>",
    "ACCESSGUARD_ADMIN_EMAILS": "ops@accessguard.invalid",
    "STRIPE_SECRET_KEY": "sk_test_value",  # pragma: allowlist secret
    "STRIPE_WEBHOOK_SECRET": "whsec_value",  # pragma: allowlist secret
    "STRIPE_PRICE_TEAM": "price_team",
    "STRIPE_PRICE_BUSINESS": "price_business",
    "STRIPE_PORTAL_CONFIGURATION": "bpc_value",
    "ACCESSGUARD_ALLOW_LIVE_PAYMENTS": "false",
    "ACCESSGUARD_AI_PROVIDER": "none",
    "RESTIC_REPOSITORY": "s3:https://s3.storage.invalid/bucket/accessguard",
    "RESTIC_PASSWORD": "r" * 40,
    "RESTIC_S3_ACCESS_KEY_ID": "access-id",
    "RESTIC_S3_SECRET_ACCESS_KEY": "s3-secret-value-17",  # pragma: allowlist secret
    "ACCESSGUARD_BACKUP_HEARTBEAT_URL": "https://heartbeat.invalid/ping/1",
    "SENTRY_DSN": "https://key@errors.invalid/1",
    "ACCESSGUARD_RUNNER_WHEEL": "/dist/accessguard-0.3.0-py3-none-any.whl",
}


def errors(**changes: str) -> list[str]:
    found: list[str] = check_env.check({**VALID, **changes}).errors
    return found


class CheckTests(unittest.TestCase):
    def test_a_complete_production_file_passes_without_warnings(self) -> None:
        report = check_env.check(VALID)
        self.assertEqual((report.errors, report.warnings), ([], []))

    def test_placeholders_from_the_example_file_are_errors(self) -> None:
        example = check_env.read_env(REPO_ROOT / ".env.example")
        found = check_env.check(example).errors
        for name in ("ACCESSGUARD_SECRET_KEY", "POSTGRES_PASSWORD", "RESTIC_PASSWORD"):
            self.assertIn(f"{name}: still a placeholder from .env.example", found)

    def test_missing_and_short_values(self) -> None:
        values = {k: v for k, v in VALID.items() if k != "RESTIC_REPOSITORY"}
        self.assertIn("RESTIC_REPOSITORY: missing or empty", check_env.check(values).errors)
        self.assertIn(
            "ACCESSGUARD_SECRET_KEY: shorter than 50 characters",
            errors(ACCESSGUARD_SECRET_KEY="short"),  # pragma: allowlist secret
        )

    def test_the_public_url_hosts_and_origins_must_match_the_domain(self) -> None:
        found = errors(
            ACCESSGUARD_PUBLIC_URL="http://app.accessguard.invalid",
            ACCESSGUARD_ALLOWED_HOSTS="app.accessguard.invalid",
            ACCESSGUARD_CSRF_TRUSTED_ORIGINS="https://other.invalid",
        )
        self.assertEqual(
            [line.split(":")[0] for line in found],
            [
                "ACCESSGUARD_PUBLIC_URL",
                "ACCESSGUARD_ALLOWED_HOSTS",
                "ACCESSGUARD_CSRF_TRUSTED_ORIGINS",
            ],
        )

    def test_the_proxy_settings_must_match_the_overlay(self) -> None:
        self.assertTrue(errors(ACCESSGUARD_TRUSTED_PROXY_HOPS="0"))
        self.assertTrue(errors(ACCESSGUARD_BEHIND_PROXY="false"))
        self.assertTrue(errors(ACCESSGUARD_DEBUG="1"))

    def test_smtp_must_use_starttls_on_587(self) -> None:
        self.assertIn(
            "ACCESSGUARD_EMAIL_PORT: 465 (implicit TLS) is not supported; use 587",
            errors(ACCESSGUARD_EMAIL_PORT="465"),
        )
        self.assertTrue(
            errors(ACCESSGUARD_EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
        )

    def test_live_payments_need_the_explicit_flag(self) -> None:
        live = {
            **VALID,
            "STRIPE_SECRET_KEY": "sk_live_value",  # pragma: allowlist secret
            "ACCESSGUARD_ALLOW_LIVE_PAYMENTS": "true",
        }
        self.assertEqual(len(check_env.check(live).errors), 2)
        self.assertEqual(check_env.check(live, allow_live_payments=True).errors, [])
        sandbox_key = "rkcs_test_value"  # pragma: allowlist secret
        self.assertEqual(errors(STRIPE_SECRET_KEY=sandbox_key), [], "sandbox keys are test keys")

    def test_billing_is_all_or_nothing(self) -> None:
        without = {k: v for k, v in VALID.items() if not k.startswith("STRIPE_")}
        report = check_env.check(without)
        self.assertEqual(report.errors, [], "a free beta runs without Stripe")
        self.assertTrue(any(w.startswith("STRIPE_SECRET_KEY:") for w in report.warnings))
        self.assertEqual(
            errors(STRIPE_PORTAL_CONFIGURATION=""),
            ["STRIPE_PORTAL_CONFIGURATION: missing; Stripe needs all five settings or none"],
        )

    def test_backup_repository_kinds(self) -> None:
        self.assertIn(
            "RESTIC_S3_SECRET_ACCESS_KEY: required for an S3 repository",
            errors(RESTIC_S3_SECRET_ACCESS_KEY=""),
        )
        self.assertEqual(errors(RESTIC_REPOSITORY="sftp:backup-host:/accessguard"), [])
        self.assertTrue(errors(RESTIC_REPOSITORY="/var/backups"), "a local path is not off-site")

    def test_a_third_party_ai_provider_needs_a_key_and_warns_about_the_dpa(self) -> None:
        report = check_env.check({**VALID, "ACCESSGUARD_AI_PROVIDER": "anthropic"})
        self.assertEqual(report.errors, ["ANTHROPIC_API_KEY: required for the anthropic provider"])
        self.assertTrue(any("DPA" in line for line in report.warnings))


class CommandTests(unittest.TestCase):
    def write(self, values: dict[str, str]) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / ".env"
        path.write_text("# production\n" + "".join(f"{k}={v}\n" for k, v in values.items()))
        path.chmod(0o600)
        return path

    def run_check(self, path: Path) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = check_env.main([str(path)])
        return code, out.getvalue()

    def test_names_are_printed_never_values(self) -> None:
        secrets = {**VALID, "ACCESSGUARD_EMAIL_PORT": "465"}
        secrets["STRIPE_SECRET_KEY"] = "sk_live_value"  # pragma: allowlist secret
        code, output = self.run_check(self.write(secrets))
        self.assertEqual(code, 1)
        self.assertIn("ERROR STRIPE_SECRET_KEY", output)
        for value in ("sk_live_value", "smtp-secret-value-91", "s3-secret-value-17", "k" * 60):
            self.assertNotIn(value, output)

    def test_a_valid_file_exits_zero_and_a_malformed_one_two(self) -> None:
        self.assertEqual(self.run_check(self.write(VALID))[0], 0)
        path = self.write(VALID)
        path.write_text("A=1\nnot a pair\n")
        code, output = self.run_check(path)
        self.assertEqual((code, "line 2" in output), (2, True))

    @unittest.skipUnless(os.name == "posix", "file modes are POSIX-only")
    def test_a_group_readable_file_is_an_error(self) -> None:
        path = self.write(VALID)
        path.chmod(0o644)
        code, output = self.run_check(path)
        self.assertEqual(code, 1)
        self.assertIn("chmod 600", output)


if __name__ == "__main__":
    unittest.main()
