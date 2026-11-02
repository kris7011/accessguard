"""Check a production ``.env`` before deploying with docker-compose.prod.yml.

    python3 ops/deploy/check_env.py .env [--allow-live-payments]

Prints problems by variable **name** only; values are never printed. Exit 0: no errors
(warnings may remain), 1: errors, 2: the file cannot be read. Standard library only, so it
runs on a fresh server before anything is installed.

What it enforces is what the overlay and the settings expect: placeholders replaced, an
https public URL on ``ACCESSGUARD_DOMAIN``, exactly one trusted proxy hop, SMTP with STARTTLS
on 587, Stripe in test mode unless live payments were approved, and an off-site backup
repository with its password.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER = re.compile(r"replace|changeme|example\.(?:com|org|net|test)", re.IGNORECASE)
# The same rule as accessguard_web.billing.gateway.is_test_key (sk_test_, rk_test_, rkcs_test_).
TEST_KEY = re.compile(r"^[a-z]+_test_")
TRUE = {"1", "true", "yes", "on"}
SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
# Needed by the container health check (127.0.0.1) and the demo runner (web).
INTERNAL_HOSTS = ("127.0.0.1", "web")

REQUIRED = (
    "ACCESSGUARD_SECRET_KEY", "ACCESSGUARD_ALLOWED_HOSTS", "ACCESSGUARD_CSRF_TRUSTED_ORIGINS",
    "ACCESSGUARD_PUBLIC_URL", "ACCESSGUARD_ADMIN_PATH", "ACCESSGUARD_DOMAIN", "ACME_EMAIL",
    "POSTGRES_PASSWORD", "ACCESSGUARD_EMAIL_HOST", "ACCESSGUARD_EMAIL_USER",
    "ACCESSGUARD_EMAIL_PASSWORD", "ACCESSGUARD_FROM_EMAIL", "RESTIC_REPOSITORY",
    "RESTIC_PASSWORD",
)  # fmt: skip
# Billing is all or nothing: without any of these the app runs with payments switched off.
STRIPE = (
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_TEAM", "STRIPE_PRICE_BUSINESS",
    "STRIPE_PORTAL_CONFIGURATION",
)  # fmt: skip
MIN_LENGTH = {"ACCESSGUARD_SECRET_KEY": 50, "POSTGRES_PASSWORD": 20, "RESTIC_PASSWORD": 20}
PREFIXES = {
    "STRIPE_WEBHOOK_SECRET": "whsec_",  # pragma: allowlist secret
    "STRIPE_PRICE_TEAM": "price_",
    "STRIPE_PRICE_BUSINESS": "price_",
    "STRIPE_PORTAL_CONFIGURATION": "bpc_",
}


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, name: str, message: str) -> None:
        self.errors.append(f"{name}: {message}")

    def warn(self, name: str, message: str) -> None:
        self.warnings.append(f"{name}: {message}")


def read_env(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines as docker compose reads them; blank lines and ``#`` comments skipped."""
    values = {}
    for number, raw in enumerate(path.read_text("utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"line {number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def _listed(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def check(values: dict[str, str], *, allow_live_payments: bool = False) -> Report:
    report = Report()
    get = values.get
    for name in REQUIRED:
        if not get(name):
            report.error(name, "missing or empty")
    for name, value in values.items():
        if value and PLACEHOLDER.search(value):
            report.error(name, "still a placeholder from .env.example")
    for name, minimum in MIN_LENGTH.items():
        if get(name) and len(values[name]) < minimum:
            report.error(name, f"shorter than {minimum} characters")
    for name in ("ACCESSGUARD_DEBUG", "ACCESSGUARD_TESTING"):
        if get(name, "").lower() in TRUE:
            report.error(name, "must not be enabled in production")
    if get("ACCESSGUARD_ENVIRONMENT", "") != "production":
        report.warn("ACCESSGUARD_ENVIRONMENT", "not 'production' (used to label error reports)")
    _check_domain(values, report)
    _check_transport_security(values, report)
    _check_email(values, report)
    _check_payments(values, report, allow_live_payments)
    _check_ai(values, report)
    _check_backups_and_monitoring(values, report)
    admin = get("ACCESSGUARD_ADMIN_PATH", "")
    if admin and (admin.strip("/") == "admin" or not admin.endswith("/")):
        report.error("ACCESSGUARD_ADMIN_PATH", "use a non-default path that ends with '/'")
    return report


def _check_domain(values: dict[str, str], report: Report) -> None:
    domain = values.get("ACCESSGUARD_DOMAIN", "")
    if not domain:
        return
    if values.get("ACCESSGUARD_PUBLIC_URL", "").rstrip("/") != f"https://{domain}":
        report.error("ACCESSGUARD_PUBLIC_URL", "must be https://<ACCESSGUARD_DOMAIN>")
    hosts = _listed(values.get("ACCESSGUARD_ALLOWED_HOSTS", ""))
    for host in (domain, *INTERNAL_HOSTS):
        if host not in hosts:
            report.error(
                "ACCESSGUARD_ALLOWED_HOSTS", f"must include the domain, {', '.join(INTERNAL_HOSTS)}"
            )
            break
    if f"https://{domain}" not in _listed(values.get("ACCESSGUARD_CSRF_TRUSTED_ORIGINS", "")):
        report.error(
            "ACCESSGUARD_CSRF_TRUSTED_ORIGINS", "must include https://<ACCESSGUARD_DOMAIN>"
        )


def _check_transport_security(values: dict[str, str], report: Report) -> None:
    if values.get("ACCESSGUARD_BEHIND_PROXY", "").lower() not in TRUE:
        report.error("ACCESSGUARD_BEHIND_PROXY", "must be true behind the TLS proxy")
    if values.get("ACCESSGUARD_TRUSTED_PROXY_HOPS", "") != "1":
        report.error("ACCESSGUARD_TRUSTED_PROXY_HOPS", "must be 1 (the proxy in the overlay)")
    if values.get("ACCESSGUARD_SECURE_COOKIES", "true").lower() not in TRUE:
        report.error("ACCESSGUARD_SECURE_COOKIES", "must be true on a public host")
    hsts = values.get("ACCESSGUARD_HSTS_SECONDS", "31536000")
    if not hsts.isdigit() or int(hsts) <= 0:
        report.error("ACCESSGUARD_HSTS_SECONDS", "must be a positive number of seconds")
    if values.get("ACCESSGUARD_HSTS_PRELOAD", "").lower() in TRUE:
        report.warn(
            "ACCESSGUARD_HSTS_PRELOAD", "preloading is hard to undo; only on the final domain"
        )


def _check_email(values: dict[str, str], report: Report) -> None:
    if values.get("ACCESSGUARD_EMAIL_BACKEND", "") != SMTP_BACKEND:
        report.error("ACCESSGUARD_EMAIL_BACKEND", f"must be {SMTP_BACKEND} (password reset)")
    port = values.get("ACCESSGUARD_EMAIL_PORT", "587")
    if port == "465":
        report.error("ACCESSGUARD_EMAIL_PORT", "465 (implicit TLS) is not supported; use 587")
    elif port != "587":
        report.warn("ACCESSGUARD_EMAIL_PORT", "not 587; the connection always uses STARTTLS")
    if not values.get("ACCESSGUARD_ADMIN_EMAILS"):
        report.warn("ACCESSGUARD_ADMIN_EMAILS", "empty: nobody gets server-error e-mails")


def _check_payments(values: dict[str, str], report: Report, allow_live: bool) -> None:
    configured = [name for name in STRIPE if values.get(name)]
    if not configured:
        report.warn("STRIPE_SECRET_KEY", "no Stripe settings: payments are off, every plan is Free")
    elif len(configured) < len(STRIPE):
        for name in STRIPE:
            if not values.get(name):
                report.error(name, "missing; Stripe needs all five settings or none")
    key = values.get("STRIPE_SECRET_KEY", "")
    if key and not TEST_KEY.match(key) and not allow_live:
        report.error("STRIPE_SECRET_KEY", "not a test-mode key; live payments need approval")
    if values.get("ACCESSGUARD_ALLOW_LIVE_PAYMENTS", "").lower() in TRUE and not allow_live:
        report.error("ACCESSGUARD_ALLOW_LIVE_PAYMENTS", "true without --allow-live-payments")
    for name, prefix in PREFIXES.items():
        if values.get(name) and not values[name].startswith(prefix):
            report.error(name, f"expected a value starting with {prefix}")


def _check_ai(values: dict[str, str], report: Report) -> None:
    provider = values.get("ACCESSGUARD_AI_PROVIDER", "none") or "none"
    if provider not in {"none", "anthropic", "ollama"}:
        report.error("ACCESSGUARD_AI_PROVIDER", "must be none, anthropic or ollama")
    if provider == "anthropic":
        if not values.get("ANTHROPIC_API_KEY"):
            report.error("ANTHROPIC_API_KEY", "required for the anthropic provider")
        report.warn(
            "ACCESSGUARD_AI_PROVIDER",
            "a third-party provider: have a DPA in place before customers enable AI assistance",
        )


def _check_backups_and_monitoring(values: dict[str, str], report: Report) -> None:
    repository = values.get("RESTIC_REPOSITORY", "")
    if repository.startswith("s3:"):
        for name in ("RESTIC_S3_ACCESS_KEY_ID", "RESTIC_S3_SECRET_ACCESS_KEY"):
            if not values.get(name):
                report.error(name, "required for an S3 repository")
    elif repository.startswith("sftp:"):
        report.warn("RESTIC_REPOSITORY", "SFTP: provide .secrets/ssh (key, known_hosts, config)")
    elif repository:
        report.error("RESTIC_REPOSITORY", "expected an sftp: or s3: repository")
    if not values.get("ACCESSGUARD_BACKUP_HEARTBEAT_URL"):
        report.warn("ACCESSGUARD_BACKUP_HEARTBEAT_URL", "empty: a stopped backup raises no alert")
    if not values.get("ACCESSGUARD_RUNNER_WHEEL"):
        report.warn("ACCESSGUARD_RUNNER_WHEEL", "empty: customers cannot download the runner")
    if not values.get("SENTRY_DSN"):
        report.warn("SENTRY_DSN", "empty: errors are only e-mailed to ACCESSGUARD_ADMIN_EMAILS")


def check_permissions(path: Path) -> list[str]:
    """On POSIX, the file must not be readable by the group or others."""
    if os.name != "posix":
        return []
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        return [f"{path.name}: readable by group or others; run chmod 600"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("env_file", type=Path)
    parser.add_argument(
        "--allow-live-payments",
        action="store_true",
        help="accept live Stripe keys (only after the owner's explicit approval)",
    )
    args = parser.parse_args(argv)
    try:
        values = read_env(args.env_file)
    except (OSError, ValueError) as error:
        print(f"cannot read {args.env_file.name}: {error}", file=sys.stderr)
        return 2
    report = check(values, allow_live_payments=args.allow_live_payments)
    report.errors.extend(check_permissions(args.env_file))
    for line in report.errors:
        print(f"ERROR {line}")
    for line in report.warnings:
        print(f"WARN  {line}")
    print(f"{len(report.errors)} errors, {len(report.warnings)} warnings")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
