"""Runners, scan jobs, findings and the finding history (which doubles as labelled data)."""

from __future__ import annotations

import secrets
from typing import Any, ClassVar

from django.db import models
from django.utils import timezone

from accessguard_web.accounts.models import Organization, User, hash_token
from accessguard_web.projects.models import Project

RUNNER_TOKEN_PREFIX = "agr_"  # noqa: S105 - a public prefix, not a credential


class Runner(models.Model):
    """A runner installation in a customer's environment, authenticated by a bearer token.

    ``organization`` is empty only for the platform demo runner, which may claim demo
    scans and nothing else. Only the token's SHA-256 is stored; it is shown once.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, null=True, blank=True, related_name="runners"
    )
    name = models.CharField(max_length=80)
    is_platform_demo = models.BooleanField(default=False)
    token_hash = models.CharField(max_length=64, unique=True)
    token_hint = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    version = models.CharField(max_length=60, blank=True)

    class Meta:
        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(is_platform_demo=True, organization__isnull=True)
                | models.Q(is_platform_demo=False, organization__isnull=False),
                name="runner_owner_matches_kind",
            )
        ]

    def __str__(self) -> str:
        return self.name

    @classmethod
    def issue(
        cls, *, name: str, organization: Organization | None, is_platform_demo: bool = False
    ) -> tuple[Runner, str]:
        token = RUNNER_TOKEN_PREFIX + secrets.token_urlsafe(32)
        runner = cls.objects.create(
            organization=organization,
            name=name,
            is_platform_demo=is_platform_demo,
            token_hash=hash_token(token),
            token_hint=token[:8],
        )
        return runner, token

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @property
    def online(self) -> bool:
        return bool(self.last_seen_at and (timezone.now() - self.last_seen_at).total_seconds() < 60)


class ScanStatus(models.TextChoices):
    QUEUED = "queued", "Waiting for a runner"
    RUNNING = "running", "Running"
    CANCEL_REQUESTED = "cancel_requested", "Stopping"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"
    LOST = "lost", "Runner stopped responding"


ACTIVE_STATUSES = (ScanStatus.QUEUED, ScanStatus.RUNNING, ScanStatus.CANCEL_REQUESTED)


class ScanKind(models.TextChoices):
    FULL = "full", "Full scan"
    RETEST = "retest", "Retest"


class Scan(models.Model):
    """An approved scan job: a frozen spec, its approval, live progress and the report."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="scans")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="scans")
    kind = models.CharField(max_length=10, choices=ScanKind.choices, default=ScanKind.FULL)
    status = models.CharField(
        max_length=20, choices=ScanStatus.choices, default=ScanStatus.QUEUED, db_index=True
    )
    is_demo = models.BooleanField(default=False)
    spec: models.JSONField[dict[str, Any], dict[str, Any]] = models.JSONField()
    spec_sha256 = models.CharField(max_length=64)
    planned_checks = models.PositiveIntegerField(default=0)
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    approved_at = models.DateTimeField()
    attestation = models.TextField()
    runner = models.ForeignKey(Runner, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    progress: models.JSONField[dict[str, Any], dict[str, Any]] = models.JSONField(default=dict)
    failure_category = models.CharField(max_length=40, blank=True)
    failure_message = models.CharField(max_length=500, blank=True)
    report: models.JSONField[dict[str, Any] | None, dict[str, Any] | None] = models.JSONField(
        null=True, blank=True
    )
    report_status = models.CharField(max_length=20, blank=True)
    report_complete = models.BooleanField(default=False)
    retest_findings: models.ManyToManyField[Finding, Any] = models.ManyToManyField(
        "Finding", blank=True, related_name="retests"
    )

    class Meta:
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"Scan #{self.pk} of {self.project}"

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def shows_as_passed(self) -> bool:
        """Green only for a complete, full-matrix run in which every check passed."""
        return (
            self.status == ScanStatus.COMPLETED
            and self.report_status == "checks_passed"
            and self.report_complete
            and self.kind == ScanKind.FULL
        )


class FindingStatus(models.TextChoices):
    OPEN = "open", "Open"
    CONFIRMED = "confirmed", "Confirmed"
    REJECTED = "rejected", "Rejected (not a problem)"
    RESOLVED = "resolved", "Resolved"


class Finding(models.Model):
    """One policy violation of a project, tracked across scans by a stable fingerprint.

    The fingerprint is the check id plus the finding type: the same actor and resource
    and the same kind of violation are the same finding in every scan.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="findings"
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="findings")
    fingerprint = models.CharField(max_length=200)
    check_id = models.CharField(max_length=160, db_index=True)
    finding_type = models.CharField(max_length=40)
    outcome = models.CharField(max_length=12)
    status = models.CharField(
        max_length=12, choices=FindingStatus.choices, default=FindingStatus.OPEN, db_index=True
    )
    resolution_verified = models.BooleanField(
        default=False, help_text="True only when a conclusive PASS on retest resolved it."
    )
    details: models.JSONField[dict[str, Any], dict[str, Any]] = models.JSONField(default=dict)
    """The latest finding entry from a report (redacted evidence, no response values)."""
    first_seen_scan = models.ForeignKey(
        Scan, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    last_seen_scan = models.ForeignKey(Scan, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar = ["status", "-updated_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["project", "fingerprint"], name="unique_finding")
        ]

    def __str__(self) -> str:
        return self.check_id

    @property
    def is_open(self) -> bool:
        return self.status in (FindingStatus.OPEN, FindingStatus.CONFIRMED)


class EventSource(models.TextChoices):
    SCAN = "scan", "Scan result"
    RETEST = "retest", "Retest result"
    USER = "user", "Person"


class Label(models.TextChoices):
    """Verified labels for learning. Only people and conclusive retests produce them."""

    TRUE_POSITIVE = "true_positive", "Confirmed by a person"
    FALSE_POSITIVE = "false_positive", "Rejected by a person"
    FIXED_VERIFIED = "fixed_verified", "Fix verified by a conclusive retest"


class FindingEvent(models.Model):
    """The history of a finding. Events with a label are the ML training signal."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    finding = models.ForeignKey(Finding, on_delete=models.CASCADE, related_name="events")
    scan = models.ForeignKey(Scan, on_delete=models.SET_NULL, null=True, blank=True)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    source = models.CharField(max_length=10, choices=EventSource.choices)
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12)
    note = models.CharField(max_length=1000, blank=True)
    label = models.CharField(max_length=20, choices=Label.choices, blank=True)
    engine_version = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering: ClassVar = ["created_at", "id"]
