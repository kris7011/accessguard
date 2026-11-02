"""Structured logging with redaction, request ids and optional error monitoring.

Logs are JSON lines on stdout in production, so any platform can collect them. A filter
scrubs bearer tokens, runner tokens, Stripe keys and API-key-like values from every
message before it is written. Error monitoring (Sentry-compatible, for example a
self-hosted GlitchTip) is enabled only when ``SENTRY_DSN`` is set, never sends personal
data, and passes events through the same scrubber.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any

from django.http import HttpRequest, HttpResponse

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

REDACTED = "[REDACTED]"
_REDACTION_RULES = (
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {REDACTED}"),
    (re.compile(r"\bagr_[A-Za-z0-9_-]{8,}"), REDACTED),  # AccessGuard runner tokens
    (re.compile(r"\b(?:sk|rk|pk)_(?:test|live)_[A-Za-z0-9]{8,}"), REDACTED),  # Stripe keys
    (re.compile(r"\bwhsec_[A-Za-z0-9]{8,}"), REDACTED),  # Stripe webhook secrets
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), REDACTED),  # Anthropic API keys
    (re.compile(r"(?i)\b(password|secret|api[_-]?key|token)=[^\s&\"']+"), rf"\1={REDACTED}"),
)


def redact(text: str) -> str:
    """Replace credential-like values in ``text``. Defense in depth for logs and errors."""
    for pattern, replacement in _REDACTION_RULES:
        text = pattern.sub(replacement, text)
    return text


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        record.request_id = _request_id.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False)


def logging_config(level: str, *, json_logs: bool) -> dict[str, Any]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"redact": {"()": RedactionFilter}},
        "formatters": {
            "json": {"()": JsonFormatter},
            "plain": {"format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"},
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "filters": ["redact"],
                "formatter": "json" if json_logs else "plain",
            }
        },
        "root": {"handlers": ["stdout"], "level": level},
        "loggers": {
            "django.request": {"handlers": ["stdout"], "level": "WARNING", "propagate": False},
        },
    }


class RequestIdMiddleware:
    """Gives every request an id (or keeps a valid incoming one) for logs and support."""

    _VALID = re.compile(r"[A-Za-z0-9-]{8,64}")

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.headers.get("X-Request-Id", "")
        request_id = incoming if self._VALID.fullmatch(incoming) else uuid.uuid4().hex
        token = _request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)
        response["X-Request-Id"] = request_id
        return response


def _scrub_event(event: dict[str, Any], hint: object) -> dict[str, Any]:
    return json.loads(redact(json.dumps(event, default=str)))  # type: ignore[no-any-return]


def init_error_monitoring(dsn: str, *, environment: str) -> bool:
    """Enable error monitoring when a DSN is configured. Returns whether it was enabled."""
    if not dsn:
        return False
    import sentry_sdk  # noqa: PLC0415 - optional at runtime, only imported when configured

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        send_default_pii=False,
        traces_sample_rate=0.0,
        before_send=_scrub_event,  # type: ignore[arg-type]
    )
    return True
