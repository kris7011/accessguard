"""Minimal HTTP/1.1 transport that connects to an already validated IP address.

Uses ``http.client`` directly: it never follows redirects, never consults proxy
environment variables, and lets the caller pin the connection to the address
that passed the scope check (the ``Host`` header carries the original name).
"""

from __future__ import annotations

import hashlib
import http.client
import json
from collections.abc import Mapping
from dataclasses import dataclass

from accessguard.domain.errors import TransportError
from accessguard.domain.observation import HttpObservation

DEFAULT_MAX_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


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
) -> RawResponse:
    """Send one request. Raises :class:`TransportError` for every failure mode."""
    connection = http.client.HTTPConnection(address, port, timeout=timeout_seconds)
    try:
        connection.request(method, path, body=body, headers={"Host": host_header, **headers})
        response = connection.getresponse()
        data = response.read(max_response_bytes + 1)
        if len(data) > max_response_bytes:
            raise TransportError(f"Response exceeds the {max_response_bytes}-byte limit")
        return RawResponse(
            status=response.status,
            headers={name.lower(): value for name, value in response.getheaders()},
            body=data,
        )
    except TimeoutError as error:
        raise TransportError(f"Request timed out after {timeout_seconds} s") from error
    except (OSError, http.client.HTTPException) as error:
        raise TransportError(f"{type(error).__name__}: {error}") from error
    finally:
        connection.close()


def decode_observation(raw: RawResponse) -> HttpObservation:
    """Decode a response into an observation. Malformed JSON is treated as non-JSON."""
    content_type = raw.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    is_json_type = media_type == "application/json" or media_type.endswith("+json")
    body: object = None
    is_json = False
    if is_json_type:
        try:
            body = json.loads(raw.body)
            is_json = True
        except (ValueError, RecursionError):
            body = None
    return HttpObservation(
        status=raw.status,
        content_type=media_type,
        size=len(raw.body),
        body_sha256=hashlib.sha256(raw.body).hexdigest(),
        is_json=is_json,
        json_body=body,
    )
