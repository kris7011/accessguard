"""Target scope: the single origin a run is authorized to contact.

Milestone 1 permits loopback destinations only. The check is applied to every
resolved address immediately before connecting, so a hostname that later
resolves elsewhere (DNS rebinding) is rejected instead of trusted.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from accessguard.domain.errors import ScopeViolationError

MAX_PATH_LENGTH = 2048
# Absolute path made of RFC 3986 path characters only: no query, fragment, backslash or whitespace.
_PATH_PATTERN = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*")


def is_safe_request_path(path: str) -> bool:
    """Return True when ``path`` is an absolute, normalized path that cannot change the origin."""
    if len(path) > MAX_PATH_LENGTH or not _PATH_PATTERN.fullmatch(path):
        return False
    if path.startswith("//"):
        return False
    segments = path.split("/")[1:]
    return not any(segment in {".", ".."} for segment in segments)


@dataclass(frozen=True)
class TargetScope:
    """An authorized HTTP origin. Construct with :meth:`from_origin` to validate input."""

    host: str
    port: int

    @classmethod
    def from_origin(cls, origin: str) -> TargetScope:
        parts = urlsplit(origin)
        if parts.scheme != "http":
            raise ScopeViolationError("Milestone 1 supports plain http origins on loopback only")
        if parts.username is not None or parts.password is not None:
            raise ScopeViolationError("Origins must not contain user information")
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise ScopeViolationError("An origin must not contain a path, query or fragment")
        try:
            port = parts.port
        except ValueError as error:
            raise ScopeViolationError(f"Invalid origin port: {error}") from error
        if not parts.hostname or port is None:
            raise ScopeViolationError("An origin must name a host and an explicit port")
        return cls(host=parts.hostname.lower(), port=port)

    @property
    def origin(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def host_header(self) -> str:
        return self.origin.removeprefix("http://")

    def check_destination(self, address: str) -> None:
        """Raise :class:`ScopeViolationError` unless ``address`` is a loopback IP address."""
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as error:
            raise ScopeViolationError(
                f"Resolved destination {address!r} is not an IP address"
            ) from error
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_loopback:
            raise ScopeViolationError(
                f"Destination {ip} is not a loopback address; "
                "milestone 1 only permits local laboratory targets"
            )
