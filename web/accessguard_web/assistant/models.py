"""A log of every assistant request: provider, model, prompt version, cost signals, outcome."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import models

from accessguard_web.accounts.models import Organization, User


class AssistantKind(models.TextChoices):
    SETUP_DRAFT = "setup_draft", "Configuration draft from an OpenAPI document"
    EXPLAIN_FINDING = "explain_finding", "Explanation and fix suggestion for a finding"


class AssistantOutcome(models.TextChoices):
    OK = "ok", "Valid answer"
    INVALID = "invalid", "Answer rejected by validation"
    FAILED = "failed", "Provider error"


class AssistantRun(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    kind = models.CharField(max_length=20, choices=AssistantKind.choices)
    provider = models.CharField(max_length=20)
    model = models.CharField(max_length=80, blank=True)
    prompt_version = models.CharField(max_length=30)
    outcome = models.CharField(max_length=10, choices=AssistantOutcome.choices)
    error = models.CharField(max_length=300, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    result: models.JSONField[dict[str, Any] | None, dict[str, Any] | None] = models.JSONField(
        null=True, blank=True
    )
    """The validated answer. Prompts are not stored."""
    finding = models.ForeignKey(
        "scans.Finding",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="explanations",
    )
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering: ClassVar = ["-created_at"]
