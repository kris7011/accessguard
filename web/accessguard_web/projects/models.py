"""A project is one customer test environment: its target, test users and access rules."""

from __future__ import annotations

from typing import Any

from django.db import models

from accessguard_web.accounts.models import Organization


class Project(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="projects"
    )
    name = models.CharField(max_length=120)
    is_demo = models.BooleanField(
        default=False, help_text="Synthetic demo project; scanned by the platform demo runner."
    )
    config: models.JSONField[dict[str, Any], dict[str, Any]] = models.JSONField(default=dict)
    """The editable scan spec draft (see projects/config.py). Never contains credentials."""
    endpoint_templates: models.JSONField[list[dict[str, Any]], list[dict[str, Any]]] = (
        models.JSONField(default=list, blank=True)
    )
    """Endpoint suggestions from an OpenAPI import, waiting for concrete test-data ids."""
    runner = models.ForeignKey(
        "scans.Runner",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="projects",
        help_text="Only this runner may claim the project's scans. Empty: any of your runners.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-is_demo", "name")

    def __str__(self) -> str:
        return self.name
