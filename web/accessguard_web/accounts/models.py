"""Users, organisations (our tenants), memberships with roles, invitations and the audit log."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any, ClassVar

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        if not email:
            raise ValueError("An e-mail address is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.update(is_staff=True, is_superuser=True)
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    """A person. Signs in with e-mail; belongs to organisations through memberships."""

    username = None  # type: ignore[assignment]
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []
    objects: ClassVar[UserManager] = UserManager()  # type: ignore[assignment]

    def __str__(self) -> str:
        return self.email


class Organization(models.Model):
    """A customer account. Every product record belongs to exactly one organisation."""

    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    ai_assistance_enabled = models.BooleanField(
        default=False,
        help_text="Allows sending finding metadata (never credentials or response content) "
        "to the configured AI provider.",
    )
    ml_data_consent = models.BooleanField(
        default=False,
        help_text="Allows using de-identified triage labels to improve check ranking.",
    )

    def __str__(self) -> str:
        return self.name


class Role(models.TextChoices):
    VIEWER = "viewer", "Viewer"
    ADMIN = "admin", "Admin"
    OWNER = "owner", "Owner"


ROLE_RANK = {Role.VIEWER: 1, Role.ADMIN: 2, Role.OWNER: 3}
ROLE_ABILITIES = {
    Role.VIEWER: "View projects, scans, reports and findings.",
    Role.ADMIN: "Everything a viewer can, plus configure projects, approve and cancel scans, "
    "triage findings and manage runners.",
    Role.OWNER: "Everything an admin can, plus billing, members and deleting the organisation.",
}


class Membership(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.VIEWER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["user", "organization"], name="unique_membership")
        ]

    def has_role(self, minimum: str) -> bool:
        return ROLE_RANK[Role(self.role)] >= ROLE_RANK[Role(minimum)]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class InvitationQuerySet(models.QuerySet["Invitation"]):
    def usable(self) -> InvitationQuerySet:
        return self.filter(accepted_at__isnull=True, expires_at__gt=timezone.now())


class Invitation(models.Model):
    """A link that lets someone join an organisation. Only the token's hash is stored."""

    VALID_DAYS = 7

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="invitations"
    )
    email = models.EmailField()
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.VIEWER)
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    objects = InvitationQuerySet.as_manager()

    @classmethod
    def issue(
        cls, organization: Organization, email: str, role: str, created_by: User
    ) -> tuple[Invitation, str]:
        token = secrets.token_urlsafe(32)
        invitation = cls.objects.create(
            organization=organization,
            email=email.lower(),
            role=role,
            token_hash=hash_token(token),
            created_by=created_by,
            expires_at=timezone.now() + timedelta(days=cls.VALID_DAYS),
        )
        return invitation, token


class AuditEvent(models.Model):
    """Who did what in an organisation. Never stores credentials or report content."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="audit_events"
    )
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=60)
    target = models.CharField(max_length=200, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering: ClassVar = ["-created_at"]


def record(
    organization: Organization,
    actor: User | None,
    action: str,
    target: str = "",
    **detail: object,
) -> AuditEvent:
    return AuditEvent.objects.create(
        organization=organization, actor=actor, action=action, target=target[:200], detail=detail
    )
