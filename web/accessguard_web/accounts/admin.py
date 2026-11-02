from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from accessguard_web.accounts.models import AuditEvent, Membership, Organization, User


@admin.register(User)
class UserAdmin(BaseUserAdmin[User]):
    ordering = ("email",)
    list_display = ("email", "is_staff", "is_active", "date_joined")
    search_fields = ("email",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Status", {"fields": ("is_active", "is_staff", "is_superuser")}),
    )
    add_fieldsets = ((None, {"fields": ("email", "password1", "password2")}),)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin[Organization]):
    list_display = ("name", "created_at")
    search_fields = ("name",)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin[Membership]):
    list_display = ("user", "organization", "role")


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin[AuditEvent]):
    list_display = ("created_at", "organization", "actor", "action", "target")
    readonly_fields = ("organization", "actor", "action", "target", "detail", "created_at")
