"""Target client that enforces the authorized scope on every request."""

from __future__ import annotations

import re
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from accessguard import __version__
from accessguard.adapters.http_transport import (
    DEFAULT_MAX_RESPONSE_BYTES,
    TlsSettings,
    decode_observation,
    guarded_tls_context,
    send,
)
from accessguard.application.ports import RequestLimit
from accessguard.domain.credentials import Secret
from accessguard.domain.errors import ScopeViolationError, TransportError
from accessguard.domain.observation import HttpObservation
from accessguard.domain.scope import TargetScope

Resolver = Callable[[str, int], list[str]]

USER_AGENT = f"AccessGuard/{__version__} (authorized access-control test)"

# RFC 9110 token characters, used for header and cookie names.
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")
_RESERVED_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "cookie", "user-agent"}
)
_UNSENDABLE = frozenset("\r\n\0")


class AuthKind(StrEnum):
    BEARER = "bearer"
    """``Authorization: Bearer <credential>``."""
    HEADER = "header"
    """``<name>: <credential>``, for example an API key header."""
    COOKIE = "cookie"
    """``Cookie: <name>=<credential>``, for session cookies."""


@dataclass(frozen=True)
class AuthScheme:
    """How a test identity's credential is placed on the request. The name is not secret."""

    kind: AuthKind = AuthKind.BEARER
    name: str = "Authorization"

    def __post_init__(self) -> None:
        if self.kind is AuthKind.BEARER:
            if self.name != "Authorization":
                raise ValueError("Bearer credentials always use the Authorization header")
            return
        if not _TOKEN.fullmatch(self.name):
            raise ValueError(f"{self.kind.value} name {self.name!r} is not a valid token")
        if self.kind is AuthKind.HEADER and self.name.lower() in _RESERVED_HEADERS:
            raise ValueError(f"Header {self.name!r} cannot carry a credential")

    def headers(self, credential: Secret) -> dict[str, str]:
        value = credential.reveal()
        if _UNSENDABLE.intersection(value):
            # The message must never contain the value itself.
            raise TransportError("A credential contains characters that cannot be sent")
        if self.kind is AuthKind.BEARER:
            return {"Authorization": f"Bearer {value}"}
        if self.kind is AuthKind.HEADER:
            return {self.name: value}
        return {"Cookie": f"{self.name}={value}"}


def resolve_addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise TransportError(f"Could not resolve {host!r}: {error}") from error
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class ScopedHttpClient:
    """Read-only HTTP(S) client bound to one :class:`TargetScope`.

    For every request the path must lie under an allowed prefix, the host is resolved
    again, *all* resolved addresses must be inside the scope's network zone, and the
    connection is pinned to a validated address. This blocks DNS rebinding and redirects
    (which are never followed) out of scope. For ``https`` the server certificate is
    verified for the scope's host name, so pinning never weakens TLS.
    """

    def __init__(
        self,
        scope: TargetScope,
        *,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        resolver: Resolver = resolve_addresses,
        auth: AuthScheme | None = None,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be greater than 0 and at most 60")
        self._scope = scope
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._resolver = resolver
        self._auth = auth or AuthScheme()
        self._tls = (
            TlsSettings(scope.host, guarded_tls_context(tls_context)) if scope.uses_tls else None
        )

    def get(
        self, path: str, credential: Secret | None, limit: RequestLimit | None = None
    ) -> HttpObservation:
        if not self._scope.allows_path(path):
            raise ScopeViolationError(
                f"Request path {path!r} is not a safe absolute path under the allowed prefixes"
            )
        address = self._validated_address()
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if credential is not None:
            headers.update(self._auth.headers(credential))
        raw = send(
            address=address,
            port=self._scope.port,
            host_header=self._scope.host_header,
            method="GET",
            path=path,
            headers=headers,
            timeout_seconds=self._timeout if limit is None else min(self._timeout, limit.seconds),
            max_response_bytes=self._max_response_bytes,
            cancel=None if limit is None else limit.cancel,
            tls=self._tls,
        )
        return decode_observation(raw)

    def _validated_address(self) -> str:
        addresses = self._resolver(self._scope.host, self._scope.port)
        if not addresses:
            raise ScopeViolationError(f"{self._scope.host!r} did not resolve to any address")
        for address in addresses:
            self._scope.check_destination(address)
        return addresses[0]
