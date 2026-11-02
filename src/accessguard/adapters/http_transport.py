"""Minimal HTTP/1.1 transport that connects to an already validated IP address.

Uses ``http.client`` directly: it never follows redirects, never consults proxy
environment variables, and lets the caller pin the connection to the address
that passed the scope check (the ``Host`` header carries the original name).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from accessguard.domain.errors import TransportError
from accessguard.domain.observation import BodyFormat, HttpObservation

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer, WriteableBuffer

DEFAULT_MAX_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


_SLICE_SECONDS = 0.05
"""How often a blocked read re-checks the time limit and the cancel signal."""


class _GuardedSocket(socket.socket):
    """A socket whose blocking reads re-check a wall-clock deadline and a cancel signal.

    ``http.client`` timeouts apply to each socket operation, so a server that drips bytes
    could keep a request alive indefinitely, and a blocked read cannot be cancelled from
    another thread portably. Reading in short timeout slices bounds the total time and
    makes in-flight requests cancellable without cross-thread socket operations.
    """

    deadline: float
    cancel: threading.Event | None
    abort_reason: str | None

    def _check_limits(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            self.abort_reason = "Request cancelled"
        elif time.monotonic() >= self.deadline:
            self.abort_reason = "Request exceeded its time limit"
        else:
            return
        raise TimeoutError(self.abort_reason)

    def recv_into(self, buffer: WriteableBuffer, nbytes: int = 0, flags: int = 0) -> int:
        while True:
            self._check_limits()
            try:
                return super().recv_into(buffer, nbytes, flags)
            except TimeoutError:
                continue  # one slice elapsed without data; re-check the limits

    def sendall(self, data: ReadableBuffer, flags: int = 0, /) -> None:
        self._check_limits()
        self.settimeout(max(self.deadline - time.monotonic(), 0.001))
        try:
            super().sendall(data, flags)
        finally:
            self.settimeout(_SLICE_SECONDS)


class _GuardedSSLSocket(ssl.SSLSocket):
    """The TLS counterpart of :class:`_GuardedSocket`: the same deadline and cancel checks.

    Retrying a read after a timeout slice is safe: OpenSSL keeps partial record state, as
    for a non-blocking socket that reported "want read".
    """

    deadline: float
    cancel: threading.Event | None
    abort_reason: str | None

    def _check_limits(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            self.abort_reason = "Request cancelled"
        elif time.monotonic() >= self.deadline:
            self.abort_reason = "Request exceeded its time limit"
        else:
            return
        raise TimeoutError(self.abort_reason)

    def recv_into(self, buffer: WriteableBuffer, nbytes: int | None = None, flags: int = 0) -> int:
        while True:
            self._check_limits()
            try:
                return super().recv_into(buffer, nbytes, flags)
            except TimeoutError:
                continue

    def sendall(self, data: ReadableBuffer, flags: int = 0) -> None:
        self._check_limits()
        self.settimeout(max(self.deadline - time.monotonic(), 0.001))
        try:
            super().sendall(data, flags)
        finally:
            self.settimeout(_SLICE_SECONDS)


@dataclass(frozen=True)
class TlsSettings:
    """Verify the server certificate for ``server_hostname`` (also sent as SNI).

    The connection itself goes to the already validated IP address, so certificate
    validation and IP pinning apply together.
    """

    server_hostname: str
    context: ssl.SSLContext


def guarded_tls_context(context: ssl.SSLContext | None = None) -> ssl.SSLContext:
    """Return a verifying TLS context whose sockets honour deadlines and cancellation."""
    context = context or ssl.create_default_context()
    if context.verify_mode is not ssl.CERT_REQUIRED or not context.check_hostname:
        raise ValueError("TLS contexts must verify certificates and host names")
    context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
    context.sslsocket_class = _GuardedSSLSocket
    return context


class _GuardedConnection(http.client.HTTPConnection):
    def __init__(
        self,
        address: str,
        port: int,
        deadline: float,
        cancel: threading.Event | None,
        tls: TlsSettings | None = None,
    ) -> None:
        super().__init__(address, port, timeout=max(deadline - time.monotonic(), 0.001))
        self._deadline = deadline
        self._cancel = cancel
        self._tls = tls
        self.guarded: _GuardedSocket | _GuardedSSLSocket | None = None

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), self.timeout)
        guarded: _GuardedSocket | _GuardedSSLSocket
        if self._tls is None:
            guarded = _GuardedSocket(raw.family, raw.type, raw.proto, fileno=raw.detach())
        else:
            wrapped = self._tls.context.wrap_socket(
                raw, server_hostname=self._tls.server_hostname, do_handshake_on_connect=False
            )
            if not isinstance(wrapped, _GuardedSSLSocket):
                wrapped.close()
                raise TransportError("TLS context is not a guarded AccessGuard context")
            guarded = wrapped
        guarded.deadline = self._deadline
        guarded.cancel = self._cancel
        guarded.abort_reason = None
        self.sock = self.guarded = guarded
        if isinstance(guarded, _GuardedSSLSocket):
            guarded.settimeout(max(self._deadline - time.monotonic(), 0.001))
            guarded.do_handshake()
        guarded.settimeout(_SLICE_SECONDS)

    def abort_reason(self) -> str | None:
        return self.guarded.abort_reason if self.guarded is not None else None


def send(
    *,
    address: str,
    port: int,
    host_header: str,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes | None = None,
    timeout_seconds: float,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    cancel: threading.Event | None = None,
    tls: TlsSettings | None = None,
) -> RawResponse:
    """Send one request within ``timeout_seconds`` of total wall-clock time.

    Raises :class:`TransportError` for every failure mode, including expiry of the time
    limit and cancellation via ``cancel``. The connection is always closed.
    """
    if timeout_seconds <= 0:
        raise TransportError("Request exceeded its time limit")
    connection = _GuardedConnection(address, port, time.monotonic() + timeout_seconds, cancel, tls)
    response: http.client.HTTPResponse | None = None
    try:
        connection.request(method, path, body=body, headers={"Host": host_header, **headers})
        response = connection.getresponse()
        data = response.read(max_response_bytes + 1)
    except TimeoutError as error:
        raise TransportError(
            connection.abort_reason() or f"Request timed out after {timeout_seconds:.2f} s"
        ) from error
    except ssl.SSLCertVerificationError as error:
        raise TransportError(
            f"TLS certificate verification failed: {error.verify_message}"
        ) from error
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise TransportError(
            connection.abort_reason() or f"{type(error).__name__}: {error}"
        ) from error
    finally:
        # The response's file object holds its own reference to the socket; closing only
        # the connection would leave the socket open after a timeout or cancellation.
        if response is not None:
            response.close()
        connection.close()
    if len(data) > max_response_bytes:
        raise TransportError(f"Response exceeds the {max_response_bytes}-byte limit")
    return RawResponse(
        status=response.status,
        headers={name.lower(): value for name, value in response.getheaders()},
        body=data,
    )


def decode_observation(raw: RawResponse) -> HttpObservation:
    """Decode a response into an observation and classify whether its body can be analysed.

    The declared Content-Type decides whether a body is parsed as JSON; bodies are never
    sniffed. A body that cannot be read is marked as such rather than treated as empty.
    """
    content_type = raw.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    body_format, body = _classify_body(raw.body, media_type)
    return HttpObservation(
        status=raw.status,
        content_type=media_type,
        size=len(raw.body),
        body_sha256=hashlib.sha256(raw.body).hexdigest(),
        body_format=body_format,
        json_body=body,
    )


def _classify_body(body: bytes, media_type: str) -> tuple[BodyFormat, object]:
    if not body:
        return BodyFormat.EMPTY, None
    if media_type != "application/json" and not media_type.endswith("+json"):
        return BodyFormat.UNSUPPORTED, None
    try:
        return BodyFormat.JSON, json.loads(body)
    except (ValueError, RecursionError):
        return BodyFormat.INVALID_JSON, None
