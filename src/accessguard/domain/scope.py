"""Target scope: the single origin a run is authorized to contact.

A scope names one origin (scheme, host, port), the network zone its resolved addresses
must belong to, and the path prefixes requests may use. The destination check is applied
to every resolved address immediately before connecting, so a hostname that later
resolves elsewhere (DNS rebinding) is rejected instead of trusted.

Link-local addresses (which include the cloud metadata endpoint 169.254.169.254),
known metadata addresses, multicast, unspecified and reserved addresses are rejected in
every zone. The lab and evaluation fixtures use :meth:`TargetScope.from_origin`, which
keeps the original milestone 1 rule: plain http on loopback only.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from accessguard.domain.errors import ScopeViolationError

MAX_PATH_LENGTH = 2048
MAX_PATH_PREFIXES = 50
# Absolute path made of RFC 3986 path characters only: no query, fragment, backslash or whitespace.
_PATH_PATTERN = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*")
_DEFAULT_PORTS = {"http": 80, "https": 443}

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_ALWAYS_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",  # "this network"; 0.0.0.0 can reach local services on some systems
        "100.100.100.200/32",  # Alibaba Cloud metadata
        "fd00:ec2::254/128",  # AWS IPv6 metadata
    )
)


def is_safe_request_path(path: str) -> bool:
    """Return True when ``path`` is an absolute, normalized path that cannot change the origin."""
    if len(path) > MAX_PATH_LENGTH or not _PATH_PATTERN.fullmatch(path):
        return False
    if path.startswith("//"):
        return False
    segments = path.split("/")[1:]
    return not any(segment in {".", ".."} for segment in segments)


class NetworkZone(StrEnum):
    """Which resolved addresses a scope may connect to."""

    LOOPBACK = "loopback"
    """Loopback addresses only (127.0.0.0/8, ::1): a target on the runner's own machine."""
    PRIVATE = "private"
    """Loopback and private networks (RFC 1918, unique local IPv6, shared address space),
    for a staging host inside the customer's own network."""
    PUBLIC = "public"
    """Globally routable addresses only: a staging host reachable over the internet."""


def _normalize(address: str) -> IPAddress:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as error:
        raise ScopeViolationError(
            f"Resolved destination {address!r} is not an IP address"
        ) from error
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _always_blocked(ip: IPAddress) -> bool:
    if ip.is_loopback:
        return False  # ::1 lies inside the reserved block ::/8 but is ordinary loopback
    return (
        ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or any(ip in network for network in _ALWAYS_BLOCKED_NETWORKS)
    )


def _in_zone(ip: IPAddress, zone: NetworkZone) -> bool:
    if zone is NetworkZone.LOOPBACK:
        return ip.is_loopback
    if zone is NetworkZone.PRIVATE:
        shared = isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network(
            "100.64.0.0/10"
        )
        return ip.is_loopback or ip.is_private or shared
    return ip.is_global


def normalize_path_prefix(prefix: str) -> str:
    """Validate an allowed path prefix and strip a trailing slash (except for ``/``)."""
    if not is_safe_request_path(prefix):
        raise ScopeViolationError(
            f"Path prefix {prefix!r} must be an absolute path without query, fragment or "
            "dot segments"
        )
    return prefix if prefix == "/" else prefix.rstrip("/") or "/"


@dataclass(frozen=True)
class TargetScope:
    """An authorized HTTP origin. Construct with :meth:`from_origin` or :meth:`authorized`."""

    host: str
    port: int
    scheme: str = "http"
    zone: NetworkZone = NetworkZone.LOOPBACK
    path_prefixes: tuple[str, ...] = ("/",)

    @classmethod
    def from_origin(cls, origin: str) -> TargetScope:
        """The lab rule: a plain ``http`` origin with an explicit port, loopback only."""
        parts = urlsplit(origin)
        if parts.scheme != "http":
            raise ScopeViolationError("Milestone 1 supports plain http origins on loopback only")
        host, port = _host_and_port(origin, require_port=True)
        return cls(host=host, port=port)

    @classmethod
    def authorized(
        cls,
        origin: str,
        *,
        zone: NetworkZone,
        path_prefixes: Sequence[str] = ("/",),
    ) -> TargetScope:
        """An origin the customer authorized explicitly, with its network zone and prefixes.

        ``https`` origins are verified against the system trust store by the HTTP client.
        Plain ``http`` is accepted only for the loopback and private zones, so credentials
        are never sent unencrypted over the internet.
        """
        scheme = urlsplit(origin).scheme
        if scheme not in _DEFAULT_PORTS:
            raise ScopeViolationError("An origin must use http or https")
        if scheme == "http" and zone is NetworkZone.PUBLIC:
            raise ScopeViolationError(
                "Plain http is not allowed for public targets; use https so test "
                "credentials are never sent unencrypted over the internet"
            )
        host, port = _host_and_port(origin, require_port=False)
        if not path_prefixes:
            raise ScopeViolationError("A scope must allow at least one path prefix")
        if len(path_prefixes) > MAX_PATH_PREFIXES:
            raise ScopeViolationError(f"A scope allows at most {MAX_PATH_PREFIXES} path prefixes")
        prefixes = tuple(dict.fromkeys(normalize_path_prefix(p) for p in path_prefixes))
        return cls(host=host, port=port, scheme=scheme, zone=zone, path_prefixes=prefixes)

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host_header}"

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        # Plain-http loopback scopes (the lab rule) always show their port, as in milestone 1.
        lab_rule = self.scheme == "http" and self.zone is NetworkZone.LOOPBACK
        if lab_rule or self.port != _DEFAULT_PORTS[self.scheme]:
            return f"{host}:{self.port}"
        return host

    @property
    def uses_tls(self) -> bool:
        return self.scheme == "https"

    def allows_path(self, path: str) -> bool:
        """True when ``path`` is a safe path equal to or below one of the allowed prefixes."""
        if not is_safe_request_path(path):
            return False
        return any(
            prefix in ("/", path) or path.startswith(prefix + "/") for prefix in self.path_prefixes
        )

    def constraints(self) -> list[str]:
        """Human-readable scope constraints, recorded in every report."""
        constraints = [f"{self.zone.value} destinations only", "GET requests only"]
        if self.path_prefixes != ("/",):
            constraints.append(f"paths under: {', '.join(self.path_prefixes)}")
        return constraints

    def check_destination(self, address: str) -> None:
        """Raise :class:`ScopeViolationError` unless ``address`` belongs to the scope's zone."""
        ip = _normalize(address)
        if _always_blocked(ip):
            raise ScopeViolationError(
                f"Destination {ip} is a link-local, metadata, multicast, unspecified or "
                "reserved address and is never contacted"
            )
        if not _in_zone(ip, self.zone):
            if self.zone is NetworkZone.LOOPBACK:
                raise ScopeViolationError(
                    f"Destination {ip} is not a loopback address; "
                    "this scope only permits local targets"
                )
            raise ScopeViolationError(
                f"Destination {ip} is outside the authorized {self.zone.value} network zone"
            )


def _host_and_port(origin: str, *, require_port: bool) -> tuple[str, int]:
    parts = urlsplit(origin)
    if parts.username is not None or parts.password is not None:
        raise ScopeViolationError("Origins must not contain user information")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ScopeViolationError("An origin must not contain a path, query or fragment")
    try:
        port = parts.port
    except ValueError as error:
        raise ScopeViolationError(f"Invalid origin port: {error}") from error
    if port is None and not require_port:
        port = _DEFAULT_PORTS[parts.scheme]
    if not parts.hostname or port is None:
        raise ScopeViolationError("An origin must name a host and an explicit port")
    return parts.hostname.lower(), port
