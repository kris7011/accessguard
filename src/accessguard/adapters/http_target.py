"""Target client that enforces the authorized scope on every request."""

from __future__ import annotations

import socket
from collections.abc import Callable

from accessguard import __version__
from accessguard.adapters.http_transport import (
    DEFAULT_MAX_RESPONSE_BYTES,
    decode_observation,
    send,
)
from accessguard.domain.credentials import Secret
from accessguard.domain.errors import ScopeViolationError, TransportError
from accessguard.domain.observation import HttpObservation
from accessguard.domain.scope import TargetScope, is_safe_request_path

Resolver = Callable[[str, int], list[str]]

USER_AGENT = f"AccessGuard/{__version__} (authorized local test)"


def resolve_addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise TransportError(f"Could not resolve {host!r}: {error}") from error
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class ScopedHttpClient:
    """Read-only HTTP client bound to one :class:`TargetScope`.

    For every request the host is resolved again, *all* resolved addresses must be
    inside the scope, and the connection is pinned to a validated address. This
    blocks DNS rebinding and redirects (which are never followed) out of scope.
    """

    def __init__(
        self,
        scope: TargetScope,
        *,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        resolver: Resolver = resolve_addresses,
    ) -> None:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be greater than 0 and at most 60")
        self._scope = scope
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._resolver = resolver

    def get(self, path: str, credential: Secret | None) -> HttpObservation:
        if not is_safe_request_path(path):
            raise ScopeViolationError(f"Request path {path!r} is not a safe absolute path")
        address = self._validated_address()
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential.reveal()}"
        raw = send(
            address=address,
            port=self._scope.port,
            host_header=self._scope.host_header,
            method="GET",
            path=path,
            headers=headers,
            timeout_seconds=self._timeout,
            max_response_bytes=self._max_response_bytes,
        )
        return decode_observation(raw)

    def _validated_address(self) -> str:
        addresses = self._resolver(self._scope.host, self._scope.port)
        if not addresses:
            raise ScopeViolationError(f"{self._scope.host!r} did not resolve to any address")
        for address in addresses:
            self._scope.check_destination(address)
        return addresses[0]
