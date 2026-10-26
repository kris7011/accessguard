"""Defense-in-depth scrubbing of credentials from anything that is persisted or displayed.

The engine is designed never to put credentials into results; this module is the
last line of defense before a report leaves the process.
"""

from __future__ import annotations

import re
from collections.abc import Collection

REDACTED = "[REDACTED]"

_AUTH_SCHEME_VALUE = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{16,}")


def scrub_text(text: str, secrets: Collection[str]) -> str:
    """Replace every known secret and any ``Bearer <value>``-style credential in ``text``."""
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, REDACTED)
    return _AUTH_SCHEME_VALUE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)


def scrub(value: object, secrets: Collection[str]) -> object:
    """Recursively scrub strings (including mapping keys) in JSON-compatible data."""
    if isinstance(value, str):
        return scrub_text(value, secrets)
    if isinstance(value, dict):
        return {scrub_text(str(k), secrets): scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(item, secrets) for item in value]
    return value
