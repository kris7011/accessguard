"""Model client for a locally installed Ollama server (loopback only).

API reference (checked 2026-10-26): https://docs.ollama.com/api/chat,
https://docs.ollama.com/api/tags and https://docs.ollama.com/api-reference/get-version.md

* ``POST /api/chat`` with ``stream: false``; ``format`` is ``"json"`` or a JSON schema;
  ``options`` carries ``temperature``, ``seed``, ``num_ctx`` and ``num_predict``.
* The chat response does not contain a model digest. The digest is only available from
  ``GET /api/tags`` and the server version from ``GET /api/version``; :meth:`identify`
  records them only when the server actually returns them.

Every call is bounded by the caller's :class:`RequestLimit` (remaining run time and stop
signal) and by a response size limit. Failures raise :class:`ModelError`; there is no
retry and no fallback. The client can only reach ``127.0.0.1``, never the scan target.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from accessguard.adapters.http_transport import RawResponse, send
from accessguard.application.ports import (
    ModelDescriptor,
    ModelReply,
    ModelRequest,
    ProviderValue,
    RequestLimit,
)
from accessguard.domain.errors import ModelError, TransportError

OLLAMA_PORT = 11434
LOOPBACK = "127.0.0.1"
MAX_RESPONSE_BYTES = 262_144
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_MAX_ERROR_TEXT = 200
_CHAT_METADATA = {
    "model": str,
    "done_reason": str,
    "prompt_eval_count": int,
    "eval_count": int,
    "total_duration": int,
    "load_duration": int,
}
_TAG_DETAILS = ("format", "family", "parameter_size", "quantization_level")
PROVENANCE_KEYS = ("ollama_version", "digest", "modified_at", *_TAG_DETAILS)
"""Everything :meth:`OllamaModelClient.identify` can report."""

Transport = Callable[..., RawResponse]


class OllamaModelClient:
    """Sends provider-neutral :class:`ModelRequest` objects to Ollama's chat API."""

    def __init__(
        self,
        model: str,
        *,
        port: int = OLLAMA_PORT,
        temperature: float = 0.0,
        seed: int = 0,
        num_ctx: int = 8192,
        transport: Transport = send,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _MODEL_NAME.fullmatch(model):
            raise ModelError(f"Invalid Ollama model name {model!r}")
        if not 1 <= port <= 65_535:
            raise ModelError("Ollama port must be between 1 and 65535")
        if not 0 <= temperature <= 2:
            raise ModelError("temperature must be between 0 and 2")
        if not 256 <= num_ctx <= 1_048_576:
            raise ModelError("num_ctx must be between 256 and 1048576")
        self._model = model
        self._port = port
        self._options: dict[str, ProviderValue] = {
            "temperature": temperature,
            "seed": seed,
            "num_ctx": num_ctx,
        }
        self._transport = transport
        self._clock = clock

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor(
            provider="ollama",
            model=self._model,
            is_real_model=True,
            label=f"Ollama model {self._model} (local, {LOOPBACK}:{self._port})",
            settings={**self._options, "endpoint": f"http://{LOOPBACK}:{self._port}"},
            provenance_keys=PROVENANCE_KEYS,
        )

    def identify(self, limit: RequestLimit) -> Mapping[str, ProviderValue]:
        deadline = self._clock() + limit.seconds
        reported: dict[str, ProviderValue] = {}
        version = self._json(self._call("GET", "/api/version", None, limit, deadline))
        if isinstance(version, dict) and isinstance(version.get("version"), str):
            reported["ollama_version"] = version["version"]
        tags = self._json(self._call("GET", "/api/tags", None, limit, deadline))
        entry = _find_model(tags, self._model)
        if entry is not None:
            for key in ("digest", "modified_at"):
                if isinstance(entry.get(key), str):
                    reported[key] = entry[key]
            details = entry.get("details")
            if isinstance(details, dict):
                for key in _TAG_DETAILS:
                    if isinstance(details.get(key), str):
                        reported[key] = details[key]
        return reported

    def complete(self, request: ModelRequest, limit: RequestLimit) -> ModelReply:
        options = dict(self._options)
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens
        payload = {
            "model": self._model,
            "stream": False,
            "format": dict(request.response_schema)
            if request.response_schema is not None
            else "json",
            "options": options,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        started = self._clock()
        raw = self._call("POST", "/api/chat", payload, limit, started + limit.seconds)
        latency_ms = round((self._clock() - started) * 1000, 1)
        envelope = self._json(raw)
        if not isinstance(envelope, dict):
            raise ModelError("Ollama response is not a JSON object")
        message = envelope.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ModelError("Ollama response does not contain message.content as a string")
        metadata: dict[str, ProviderValue] = {
            key: envelope[key]
            for key, kind in _CHAT_METADATA.items()
            if isinstance(envelope.get(key), kind) and not isinstance(envelope.get(key), bool)
        }
        return ModelReply(content=content, latency_ms=latency_ms, provider_metadata=metadata)

    def _call(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
        limit: RequestLimit,
        deadline: float,
    ) -> RawResponse:
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        try:
            raw = self._transport(
                address=LOOPBACK,
                port=self._port,
                host_header=f"{LOOPBACK}:{self._port}",
                method=method,
                path=path,
                headers=headers,
                body=body,
                timeout_seconds=deadline - self._clock(),
                max_response_bytes=MAX_RESPONSE_BYTES,
                cancel=limit.cancel,
            )
        except TransportError as error:
            raise ModelError(f"Ollama request {method} {path} failed: {error}") from error
        if raw.status != 200:
            raise ModelError(f"Ollama returned HTTP {raw.status} for {path}{_error_text(raw)}")
        return raw

    @staticmethod
    def _json(raw: RawResponse) -> object:
        try:
            return json.loads(raw.body)
        except (ValueError, RecursionError) as error:
            raise ModelError(f"Ollama response is not valid JSON: {error}") from error


def _find_model(tags: object, model: str) -> dict[str, Any] | None:
    """Find ``model`` in ``/api/tags``; Ollama lists untagged names as ``name:latest``."""
    if not isinstance(tags, dict) or not isinstance(tags.get("models"), list):
        return None
    wanted = {model} if ":" in model else {model, f"{model}:latest"}
    for entry in tags["models"]:
        if isinstance(entry, dict) and (
            entry.get("name") in wanted or entry.get("model") in wanted
        ):
            return entry
    return None


def _error_text(raw: RawResponse) -> str:
    """The provider's ``error`` field (e.g. "model not found"), shortened and printable."""
    try:
        document = json.loads(raw.body)
    except (ValueError, RecursionError):
        return ""
    error = document.get("error") if isinstance(document, dict) else None
    if not isinstance(error, str):
        return ""
    printable = "".join(ch for ch in error if ch.isprintable())[:_MAX_ERROR_TEXT]
    return f": {printable}"
