"""A scripted stand-in for a language model. It is NOT an AI model.

Used for tests, dry runs of the agent loop and validating the evaluation mechanics
without a model installation or paid API. Its descriptor always says
``is_real_model=False``, so reports produced with it are labelled
"scripted test adapter" and ``uses_ai: false``. Results obtained with it say nothing
about the quality of any AI model.

Two kinds of scripts exist:

* a fixed sequence of replies (raw strings, consumed in order), for testing the
  contract with exact, possibly malformed, output;
* a *responder* function that computes the reply from the model-visible state; the
  built-in responders are documented, deterministic heuristics.

A responder sees exactly what a real model would see (the minimised state) and nothing
else; in particular no ground truth and no lab or fixture configuration.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from accessguard.application.ports import (
    ModelDescriptor,
    ModelReply,
    ModelRequest,
    ProviderValue,
    RequestLimit,
)
from accessguard.domain.errors import ModelError

SCRIPTED_LABEL = "Scripted test adapter (not an AI model)"

Responder = Callable[[ModelRequest], str]
ScriptItem = str | ModelError


class ScriptedModelClient:
    """Replies from a script; records every request so tests can inspect what was sent."""

    def __init__(self, script: Responder | Iterable[ScriptItem], *, name: str) -> None:
        self._responder = script if callable(script) else None
        self._replies = [] if callable(script) else list(script)
        self._name = name
        self.requests: list[ModelRequest] = []

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor(
            provider="scripted",
            model=None,
            is_real_model=False,
            label=SCRIPTED_LABEL,
            settings={"script": self._name},
        )

    def identify(self, limit: RequestLimit) -> Mapping[str, ProviderValue]:
        return {}

    def complete(self, request: ModelRequest, limit: RequestLimit) -> ModelReply:
        if limit.cancel.is_set():
            raise ModelError("Scripted model call cancelled")
        if limit.seconds <= 0:
            raise ModelError("Scripted model call exceeded its time limit")
        self.requests.append(request)
        if self._responder is not None:
            return ModelReply(content=self._responder(request), latency_ms=0.0)
        if not self._replies:
            raise ModelError("Scripted replies exhausted")
        reply = self._replies.pop(0)
        if isinstance(reply, ModelError):
            raise reply
        return ModelReply(content=reply, latency_ms=0.0)


def _state(request: ModelRequest) -> dict[str, Any]:
    state = json.loads(request.messages[-1].content)
    if not isinstance(state, dict):
        raise ModelError("Scripted responder received an unexpected state")
    return state


def _run(check: Mapping[str, Any]) -> str:
    return json.dumps({"action": "run_check", "check_id": check["id"]})


_STOP = json.dumps({"action": "stop"})


def in_order_responder(request: ModelRequest) -> str:
    """Always the first pending check in the listed order (equals the deterministic order)."""
    pending = [c for c in _state(request)["checks"] if c["status"] == "pending"]
    return _run(pending[0]) if pending else _STOP


def _follow_up_score(check: Mapping[str, Any], findings: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for finding in findings:
        if "resource" in check and check["resource"] == finding.get("resource"):
            total += 4
        if "category" in check and (check["category"], check["relation"]) == (
            finding.get("category"),
            finding.get("relation"),
        ):
            total += 2
        if "collection" in check and check["collection"] == finding.get("collection"):
            total += 2
        if check["actor"] == finding["actor"]:
            total += 1
    return total


def _choose(request: ModelRequest, *, deny_first: bool) -> str:
    checks = _state(request)["checks"]
    findings = [c for c in checks if c.get("outcome") in ("FAIL", "SUSPECTED")]
    pending = [c for c in checks if c["status"] == "pending"]
    if not pending:
        return _STOP

    def rank(index: int) -> tuple[int, bool, int]:
        check = pending[index]
        denied = deny_first and check["expectation"] == "deny"
        return (_follow_up_score(check, findings), denied, -index)

    return _run(pending[max(range(len(pending)), key=rank)])


def follow_up_responder(request: ModelRequest) -> str:
    """Deterministic follow-up heuristic: concentrate on what earlier findings point to.

    Each pending check scores against every finding (FAIL or SUSPECTED) so far:
    +4 same resource, +2 same category and relation (or same collection), +1 same actor.
    The highest score wins; ties keep the listed order. Without findings this is the
    listed (deterministic) order. It never stops early.
    """
    return _choose(request, deny_first=False)


def deny_first_responder(request: ModelRequest) -> str:
    """The follow-up heuristic, but among equal scores checks expected to be denied go first.

    A non-AI reference for what the structural state alone allows: before any finding,
    every check expected to be denied is tried before any check expected to be allowed,
    in listed order. It never stops early.
    """
    return _choose(request, deny_first=True)


RESPONDERS: Mapping[str, Responder] = {
    "in-order": in_order_responder,
    "follow-up": follow_up_responder,
    "deny-first": deny_first_responder,
}
