"""Runs an assistant task with consent, quota, validation and a usage record. No fallback.

A provider failure or an invalid answer is shown to the user as such; the deterministic
guidance and import stay available, and nothing is silently replaced by canned text.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from accessguard_web.accounts.models import Organization, User
from accessguard_web.assistant.models import AssistantKind, AssistantOutcome, AssistantRun
from accessguard_web.assistant.providers import (
    AssistantProvider,
    ProviderError,
    configured_provider,
)
from accessguard_web.assistant.tasks import InvalidAnswer
from accessguard_web.billing.entitlements import LimitExceeded, require_assistant_quota


class AssistantUnavailable(Exception):  # noqa: N818 - a user-facing state
    """The assistant cannot run for this organisation right now; the message says why."""


def availability(organization: Organization, provider: AssistantProvider | None) -> str | None:
    """Why the assistant cannot run, or ``None`` when it can."""
    if provider is None:
        return "No AI provider is configured in this environment."
    if provider.sends_data_to_third_party and not organization.ai_assistance_enabled:
        return (
            "AI assistance is off for your organisation. An owner can enable it in Settings; "
            "it sends finding metadata (never credentials or response content) to the AI provider."
        )
    return None


def run_task(
    *,
    kind: AssistantKind,
    organization: Organization,
    user: User,
    system: str,
    message: str,
    schema: dict[str, Any],
    validate: Callable[[dict[str, Any]], dict[str, Any]],
    prompt_version: str,
    max_tokens: int,
    provider: AssistantProvider | None = None,
    **links: Any,
) -> AssistantRun:
    provider = provider if provider is not None else configured_provider()
    reason = availability(organization, provider)
    if reason is not None or provider is None:
        raise AssistantUnavailable(reason or "No AI provider is configured.")
    try:
        require_assistant_quota(organization)
    except LimitExceeded as error:
        raise AssistantUnavailable(str(error)) from error
    run = AssistantRun(
        organization=organization,
        user=user,
        kind=kind,
        provider=provider.name,
        model=provider.model,
        prompt_version=prompt_version,
        outcome=AssistantOutcome.FAILED,
        **links,
    )
    try:
        completion = provider.complete_json(
            system=system, user=message, schema=schema, max_tokens=max_tokens
        )
        run.model = completion.model
        run.latency_ms = completion.latency_ms
        run.input_tokens = completion.input_tokens
        run.output_tokens = completion.output_tokens
        run.result = validate(completion.data)
        run.outcome = AssistantOutcome.OK
    except ProviderError as error:
        run.error = str(error)[:300]
    except InvalidAnswer as error:
        run.outcome = AssistantOutcome.INVALID
        run.error = f"The answer was rejected: {error}"[:300]
    run.save()
    return run
