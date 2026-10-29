"""HTTP client for the runner API of the AccessGuard control plane.

The runner only ever sends job progress, failure categories and scrubbed reports. Redirects
are refused so the runner token can never be forwarded to another host, and https
certificates are verified with the system trust store.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from accessguard import __version__
from accessguard.domain.credentials import Secret
from accessguard.domain.errors import AccessGuardError

API_PREFIX = "/api/runner/v1"
MAX_RESPONSE_BYTES = 4_194_304
DEFAULT_TIMEOUT_SECONDS = 30.0


class ControlPlaneError(AccessGuardError):
    """The control plane could not be reached or answered unexpectedly."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a 3xx then surfaces as an HTTPError instead of being followed


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    spec: Mapping[str, object]
    spec_sha256: str


class ControlPlaneClient:
    def __init__(
        self,
        base_url: str,
        token: Secret,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._base = base_url.rstrip("/") + API_PREFIX
        self._token = token
        self._timeout = timeout_seconds
        self._opener = urllib.request.build_opener(
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl_context or ssl.create_default_context()),
        )

    def claim(self) -> ClaimedJob | None:
        """Claim the next approved job for this runner, or ``None`` when there is none."""
        status, body = self._post("/claim", {})
        if status == 204:
            return None
        job = body.get("job") if isinstance(body, dict) else None
        if not isinstance(job, dict):
            raise ControlPlaneError("claim response has no job object", status)
        job_id, spec, digest = job.get("id"), job.get("spec"), job.get("spec_sha256")
        if not isinstance(job_id, str) or not isinstance(spec, dict) or not isinstance(digest, str):
            raise ControlPlaneError("claim response is malformed", status)
        return ClaimedJob(job_id, spec, digest)

    def heartbeat(self, job_id: str, progress: Mapping[str, object]) -> bool:
        """Report progress; returns True when the user asked to cancel the job."""
        _, body = self._post(f"/jobs/{job_id}/heartbeat", dict(progress))
        return bool(isinstance(body, dict) and body.get("cancel") is True)

    def submit_report(self, job_id: str, report: Mapping[str, object]) -> None:
        self._post(f"/jobs/{job_id}/report", {"report": dict(report)})

    def fail(self, job_id: str, category: str, message: str) -> None:
        self._post(f"/jobs/{job_id}/fail", {"category": category, "message": message[:500]})

    def _post(self, path: str, payload: Mapping[str, object]) -> tuple[int, object]:
        request = urllib.request.Request(  # noqa: S310 - the scheme is validated by the config
            self._base + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token.reveal()}",
                "User-Agent": f"AccessGuard-Runner/{__version__}",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            error.close()  # the error object owns the response and its socket
            raise ControlPlaneError(
                f"control plane answered HTTP {error.code} for {path}", error.code
            ) from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise ControlPlaneError(f"control plane unreachable: {reason}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ControlPlaneError("control plane response is too large", status)
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except ValueError:
            raise ControlPlaneError("control plane response is not JSON", status) from None
