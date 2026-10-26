"""Command-line options that select a model client and agent limits (composition only).

``--model`` takes ``scripted:<script>`` (a scripted test adapter, not an AI model) or
``ollama:<model name>`` (a locally installed Ollama model on 127.0.0.1).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from accessguard.adapters.ollama_client import OLLAMA_PORT, OllamaModelClient
from accessguard.adapters.scripted_model import RESPONDERS, ScriptedModelClient
from accessguard.application.agent import AgentLimits
from accessguard.application.ports import ModelClient
from accessguard.application.scan import AgentStrategy

SCRIPTED_PREFIX = "scripted:"
OLLAMA_PREFIX = "ollama:"


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def model_spec(value: str) -> str:
    if value.startswith(SCRIPTED_PREFIX) and value.removeprefix(SCRIPTED_PREFIX) in RESPONDERS:
        return value
    if value.startswith(OLLAMA_PREFIX) and value.removeprefix(OLLAMA_PREFIX):
        return value
    scripts = ", ".join(f"{SCRIPTED_PREFIX}{name}" for name in sorted(RESPONDERS))
    raise argparse.ArgumentTypeError(f"use one of {scripts} or {OLLAMA_PREFIX}<model name>")


def add_agent_arguments(parser: argparse.ArgumentParser, *, model_required: bool) -> None:
    parser.add_argument(
        "--model",
        type=model_spec,
        required=model_required,
        help="scripted:follow-up | scripted:in-order (scripted test adapters, not AI) or "
        "ollama:<name> (a locally installed Ollama model)",
    )
    parser.add_argument("--ollama-port", type=int, default=OLLAMA_PORT, metavar="PORT")
    parser.add_argument("--max-model-calls", type=_positive_int, default=200)
    parser.add_argument("--max-invalid-replies", type=_positive_int, default=3)
    parser.add_argument(
        "--model-timeout", type=float, default=60.0, metavar="SECONDS", help="Per model call"
    )


def model_factory(spec: str, ollama_port: int) -> Callable[[], ModelClient]:
    """Return a factory: scripted clients keep per-run state, so each run needs a new one."""
    if spec.startswith(SCRIPTED_PREFIX):
        name = spec.removeprefix(SCRIPTED_PREFIX)
        return lambda: ScriptedModelClient(RESPONDERS[name], name=name)
    model = spec.removeprefix(OLLAMA_PREFIX)
    OllamaModelClient(model, port=ollama_port)  # validate the name and port up front
    return lambda: OllamaModelClient(model, port=ollama_port)


def agent_limits(args: argparse.Namespace) -> AgentLimits:
    return AgentLimits(
        max_model_calls=args.max_model_calls,
        max_invalid_replies=args.max_invalid_replies,
        model_timeout_seconds=args.model_timeout,
    )


def agent_strategy(args: argparse.Namespace) -> AgentStrategy:
    """Build the strategy for ``args``; raises ``ValueError`` or ``ModelError`` if invalid."""
    return AgentStrategy(model_factory(args.model, args.ollama_port)(), agent_limits(args))
