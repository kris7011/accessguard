"""Model providers behind one narrow port: one JSON-schema-constrained completion per call.

* ``anthropic``: the Claude API through the official SDK, with structured output
  (``output_config.format``) and server-side refusal fallback enabled.
* ``ollama``: a model on a local Ollama server (loopback only), reusing the engine's client.
* ``none``: no provider; the product then offers only its deterministic features.

Every reply is untrusted text: callers parse and validate it themselves (contracts.py).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from accessguard.adapters.ollama_client import OllamaModelClient
from accessguard.application.ports import ModelMessage, ModelRequest, RequestLimit
from accessguard.domain.errors import ModelError
from accessguard_web import conf

ANTHROPIC_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class Attempt:
    """One model attempt within a call; a declined attempt and its fallback are two."""

    model: str
    input_tokens: int
    output_tokens: int


class ProviderError(Exception):
    """The provider failed or declined. Messages never contain prompt or customer data.

    When the provider did answer (a refusal or a cut-off answer), the usage of that answer
    is kept so the call is still recorded with its model and tokens.
    """

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        attempts: tuple[Attempt, ...] = (),
    ) -> None:
        super().__init__(message)
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.attempts = attempts


@dataclass(frozen=True)
class Completion:
    data: dict[str, Any]
    provider: str
    model: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    attempts: tuple[Attempt, ...] = ()


class AssistantProvider(Protocol):
    name: str
    model: str
    sends_data_to_third_party: bool

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> Completion: ...


def _parse(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        raise ProviderError("the model reply was not valid JSON") from None
    if not isinstance(data, dict):
        raise ProviderError("the model reply was not a JSON object")
    return data


def _usage(response: Any) -> dict[str, Any]:
    """The returned message's model and tokens, and every attempt behind it.

    With a refusal fallback, the top-level ``usage`` covers only the attempt that produced
    the message; ``usage.iterations`` lists each attempt with its own model (a declined
    attempt is a ``message`` entry, the one that served a ``fallback_message`` entry).
    """
    model, usage = str(response.model), response.usage
    attempts = tuple(
        Attempt(str(entry.model or model), entry.input_tokens, entry.output_tokens)
        for entry in usage.iterations or ()
        if entry.type in {"message", "fallback_message"}
    )
    return {
        "model": model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "attempts": attempts or (Attempt(model, usage.input_tokens, usage.output_tokens),),
    }


class AnthropicProvider:
    name = "anthropic"
    sends_data_to_third_party = True

    def __init__(self, api_key: str, model: str = ANTHROPIC_MODEL, timeout: float = 60.0) -> None:
        import anthropic  # noqa: PLC0415 - only needed when this provider is configured

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self._timeout = timeout
        self.model = model

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> Completion:
        errors = self._anthropic
        started = time.monotonic()
        try:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except errors.RateLimitError:
            raise ProviderError(
                "the AI provider is rate limiting requests; try again shortly"
            ) from None
        except errors.AuthenticationError:
            raise ProviderError("the AI provider rejected the API key") from None
        except errors.APIStatusError as error:
            raise ProviderError(f"the AI provider answered HTTP {error.status_code}") from None
        except errors.APITimeoutError:  # a subclass of APIConnectionError: catch it first
            raise ProviderError(
                f"the AI provider did not answer within {self._timeout:g} seconds"
            ) from None
        except errors.APIConnectionError:
            raise ProviderError("the AI provider could not be reached") from None
        latency = int((time.monotonic() - started) * 1000)
        usage = _usage(response)
        if response.stop_reason == "refusal":
            raise ProviderError("the AI provider declined this request", **usage)
        if response.stop_reason == "max_tokens":
            raise ProviderError("the AI answer was cut off before it was complete", **usage)
        text = next((b.text for b in response.content if b.type == "text"), "")
        return Completion(_parse(text), self.name, latency_ms=latency, **usage)


class OllamaProvider:
    """A local model (127.0.0.1 only). Nothing leaves the machine that runs AccessGuard."""

    name = "ollama"
    sends_data_to_third_party = False

    def __init__(self, model: str, port: int, timeout: float) -> None:
        self._client = OllamaModelClient(model, port=port, num_ctx=16384)
        self._timeout = timeout
        self.model = model

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> Completion:
        request = ModelRequest(
            messages=(ModelMessage("system", system), ModelMessage("user", user)),
            response_schema=schema,
            max_output_tokens=max_tokens,
        )
        try:
            reply = self._client.complete(request, RequestLimit(self._timeout))
        except ModelError as error:
            raise ProviderError(str(error)) from None
        metadata = reply.provider_metadata
        prompt, output = metadata.get("prompt_eval_count"), metadata.get("eval_count")
        return Completion(
            _parse(reply.content),
            self.name,
            self.model,
            int(reply.latency_ms),
            prompt if isinstance(prompt, int) else None,
            output if isinstance(output, int) else None,
        )


def configured_provider() -> AssistantProvider | None:
    config = conf.assistant()
    if config["PROVIDER"] == "anthropic" and config["ANTHROPIC_API_KEY"]:
        return AnthropicProvider(
            config["ANTHROPIC_API_KEY"],
            config["MODEL"] or ANTHROPIC_MODEL,
            config["TIMEOUT_SECONDS"],
        )
    if config["PROVIDER"] == "ollama" and config["MODEL"]:
        return OllamaProvider(config["MODEL"], config["OLLAMA_PORT"], config["TIMEOUT_SECONDS"])
    return None
