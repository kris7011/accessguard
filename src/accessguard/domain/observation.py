"""What the engine observed for one HTTP request, independent of the HTTP library used."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BodyFormat(StrEnum):
    """Whether the engine could read a response body. Only JSON bodies are analysed."""

    JSON = "json"
    """Declared as JSON (``application/json`` or ``+json``) and parsed successfully."""
    EMPTY = "empty"
    """Zero bytes: nothing to analyse, so the body cannot contain protected data."""
    UNSUPPORTED = "unsupported"
    """Non-empty and not declared as JSON (HTML, plain text, missing Content-Type, ...)."""
    INVALID_JSON = "invalid_json"
    """Non-empty and declared as JSON, but not parseable."""


@dataclass(frozen=True)
class HttpObservation:
    """A decoded HTTP response. ``json_body`` is untrusted data and typed as ``object``."""

    status: int
    content_type: str
    size: int
    body_sha256: str
    body_format: BodyFormat
    json_body: object = None

    @property
    def is_success(self) -> bool:
        return 200 <= self.status < 300

    @property
    def is_json(self) -> bool:
        return self.body_format is BodyFormat.JSON

    @property
    def is_analysable(self) -> bool:
        """True when the body was fully read: parsed JSON, or no body at all."""
        return self.body_format in (BodyFormat.JSON, BodyFormat.EMPTY)

    def string_leaves(self) -> frozenset[str]:
        """Return every string value in the JSON body (object keys excluded)."""
        if not self.is_json:
            return frozenset()
        leaves: set[str] = set()
        pending: list[object] = [self.json_body]
        while pending:
            value = pending.pop()
            if isinstance(value, str):
                leaves.add(value)
            elif isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return frozenset(leaves)
