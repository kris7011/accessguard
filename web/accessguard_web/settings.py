"""Django settings, configured entirely from environment variables (see .env.example).

Production is the default posture: DEBUG is off unless ``ACCESSGUARD_DEBUG=1`` and a
secret key must be provided. Development settings never apply silently in production.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import django_stubs_ext
from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

from accessguard_web.observability import init_error_monitoring, logging_config

django_stubs_ext.monkeypatch()

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


DEBUG = env_bool("ACCESSGUARD_DEBUG")
TESTING = env_bool("ACCESSGUARD_TESTING")

SECRET_KEY = env("ACCESSGUARD_SECRET_KEY")
if not SECRET_KEY:
    if not (DEBUG or TESTING):
        raise ImproperlyConfigured("ACCESSGUARD_SECRET_KEY is required when DEBUG is off")
    SECRET_KEY = "insecure-development-key-never-use-in-production"  # noqa: S105

ALLOWED_HOSTS = env_list("ACCESSGUARD_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
CSRF_TRUSTED_ORIGINS = env_list("ACCESSGUARD_CSRF_TRUSTED_ORIGINS")
PUBLIC_BASE_URL = env("ACCESSGUARD_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accessguard_web.accounts",
    "accessguard_web.projects",
    "accessguard_web.scans",
    "accessguard_web.billing",
    "accessguard_web.assistant",
    "accessguard_web.pages",
]

MIDDLEWARE = [
    "accessguard_web.observability.RequestIdMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "accessguard_web.accounts.middleware.CurrentOrganizationMiddleware",
]

ROOT_URLCONF = "accessguard_web.urls"
WSGI_APPLICATION = "accessguard_web.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "accessguard_web" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "accessguard_web.accounts.context.organization",
            ],
        },
    }
]


def database_from_url(url: str) -> dict[str, object]:
    """Parse ``sqlite:///path`` or ``postgres://user:pass@host:port/name`` into Django settings."""
    parts = urlsplit(url)
    if parts.scheme == "sqlite":
        # sqlite:///relative.db, sqlite:////absolute.db, sqlite:///C:/windows.db
        path = unquote(parts.path)[1:]
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": path or BASE_DIR / "db.sqlite3"}
    if parts.scheme in {"postgres", "postgresql"}:
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parts.path.lstrip("/")),
            "USER": unquote(parts.username or ""),
            "PASSWORD": unquote(parts.password or ""),
            "HOST": parts.hostname or "",
            "PORT": str(parts.port or ""),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {"sslmode": env("ACCESSGUARD_DB_SSLMODE", "prefer")},
        }
    raise ImproperlyConfigured("DATABASE_URL must start with sqlite:/// or postgres://")


DATABASES = {
    "default": database_from_url(
        env("DATABASE_URL", f"sqlite:///{(BASE_DIR / 'db.sqlite3').as_posix()}")
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# Runner reports can be a few MB for large policies; the runner API enforces its own cap too.
DATA_UPLOAD_MAX_MEMORY_SIZE = 5_000_000
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "pages:landing"

LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG or TESTING
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache"
        if not (DEBUG or TESTING)
        else "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "accessguard_cache",
    }
}

# --- Security headers and cookies -------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 7
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF],
    "form-action": [CSP.SELF, "https://checkout.stripe.com", "https://billing.stripe.com"],
    "frame-ancestors": [CSP.NONE],
    "base-uri": [CSP.NONE],
    "object-src": [CSP.NONE],
}
if not DEBUG:
    SESSION_COOKIE_SECURE = env_bool("ACCESSGUARD_SECURE_COOKIES", True)
    CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
    SECURE_SSL_REDIRECT = env_bool("ACCESSGUARD_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(env("ACCESSGUARD_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = False
    SECURE_REDIRECT_EXEMPT = [r"^healthz$", r"^readyz$"]
    if env_bool("ACCESSGUARD_BEHIND_PROXY"):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --- E-mail ------------------------------------------------------------------------------
EMAIL_BACKEND = env("ACCESSGUARD_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("ACCESSGUARD_EMAIL_HOST")
EMAIL_PORT = int(env("ACCESSGUARD_EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("ACCESSGUARD_EMAIL_USER")
EMAIL_HOST_PASSWORD = env("ACCESSGUARD_EMAIL_PASSWORD")
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = env("ACCESSGUARD_FROM_EMAIL", "AccessGuard <no-reply@localhost>")
ADMINS = [("Operations", a) for a in env_list("ACCESSGUARD_ADMIN_EMAILS")]

# --- Product settings ----------------------------------------------------------------------
ACCESSGUARD: dict[str, Any] = {
    # The demo target: the synthetic sample API, served next to the control plane.
    "DEMO_FLAWED_ORIGIN": env("ACCESSGUARD_DEMO_FLAWED_ORIGIN", "http://127.0.0.1:8800"),
    "DEMO_FIXED_ORIGIN": env("ACCESSGUARD_DEMO_FIXED_ORIGIN", "http://127.0.0.1:8801"),
    # A running scan whose runner has not sent a heartbeat for this long is marked lost.
    "SCAN_LEASE_SECONDS": int(env("ACCESSGUARD_SCAN_LEASE_SECONDS", "120")),
    # A queued scan nobody claims is flagged after this long (it stays claimable).
    "QUEUE_WARNING_SECONDS": int(env("ACCESSGUARD_QUEUE_WARNING_SECONDS", "60")),
    "MAX_REPORT_BYTES": 4_000_000,
    "ADMIN_PATH": env("ACCESSGUARD_ADMIN_PATH", "ops-admin/"),
}

STRIPE: dict[str, Any] = {
    "SECRET_KEY": env("STRIPE_SECRET_KEY"),
    "WEBHOOK_SECRET": env("STRIPE_WEBHOOK_SECRET"),
    "PRICE_TEAM": env("STRIPE_PRICE_TEAM"),
    "PRICE_BUSINESS": env("STRIPE_PRICE_BUSINESS"),
    # Days a past_due subscription keeps its plan while Stripe retries the payment.
    "GRACE_DAYS": int(env("ACCESSGUARD_PAYMENT_GRACE_DAYS", "7")),
}

ASSISTANT: dict[str, Any] = {
    # anthropic | ollama | none
    "PROVIDER": env("ACCESSGUARD_AI_PROVIDER", "none"),
    "MODEL": env("ACCESSGUARD_AI_MODEL", ""),
    "ANTHROPIC_API_KEY": env("ANTHROPIC_API_KEY"),
    "OLLAMA_PORT": int(env("ACCESSGUARD_OLLAMA_PORT", "11434")),
    "TIMEOUT_SECONDS": float(env("ACCESSGUARD_AI_TIMEOUT_SECONDS", "60")),
}

LOGGING = logging_config(env("ACCESSGUARD_LOG_LEVEL", "INFO"), json_logs=not DEBUG)
init_error_monitoring(env("SENTRY_DSN"), environment=env("ACCESSGUARD_ENVIRONMENT", "local"))
