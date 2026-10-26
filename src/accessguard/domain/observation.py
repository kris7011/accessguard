"""What the engine observed for one HTTP request, independent of the HTTP library used."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HttpObservation:
    """A decoded HTTP response. ``json_body`` is untrusted data and typed as ``object``."""

    status: int
    content_type: str
    size: int
    body_sha256: str
    is_json: bool
    json_body: object = None

    @property
    def is_success(self) -> bool:
        return 200 <= self.status < 300

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
