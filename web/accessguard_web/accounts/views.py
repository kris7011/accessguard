"""Sign-up, sign-in, organisations, members, invitations and organisation settings."""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accessguard_web.accounts.forms import (
    DeleteOrganizationForm,
    InviteForm,
    OrganizationForm,
    OrganizationSettingsForm,
    SignupForm,
)
from accessguard_web.accounts.models import (
    AuditEvent,
    Invitation,
    Membership,
    Organization,
    Role,
    User,
    hash_token,
    record,
)
from accessguard_web.accounts.tenancy import (
    current_membership,
    current_org,
    current_user,
    get_owned,
    membership_required,
    memberships,
    switch_organization,
)
from accessguard_web.billing.entitlements import LimitExceeded, require_capacity, subscription_for
from accessguard_web.projects.demo import create_demo_project

LOGIN_FAILURE_LIMIT = 10
LOGIN_WINDOW_SECONDS = 15 * 60


@transaction.atomic
def create_organization(user: User, name: str) -> Organization:
    """A new tenant with its owner, a Free subscription and the synthetic demo project."""
    organization = Organization.objects.create(name=name)
    Membership.objects.create(user=user, organization=organization, role=Role.OWNER)
    subscription_for(organization)
    create_demo_project(organization)
    record(organization, user, "organization.created", name)
    return organization


def signup(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = User.objects.create_user(
                form.cleaned_data["email"], form.cleaned_data["password"]
            )
            organization = create_organization(user, form.cleaned_data["organization"])
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        switch_organization(request, organization)
        messages.success(request, "Welcome! Start with the demo scan below to see a full report.")
        return redirect("dashboard")
    return render(request, "accounts/signup.html", {"form": form})


def _client_ip(request: HttpRequest) -> str:
    return str(request.META.get("REMOTE_ADDR", ""))


class ThrottledLoginView(LoginView):
    """Django's login view plus a per-IP and per-account failure limit (credential stuffing)."""

    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def _keys(self) -> list[str]:
        email = str(self.request.POST.get("username", "")).lower()[:254]
        return [f"login-fail:ip:{_client_ip(self.request)}", f"login-fail:user:{hash_token(email)}"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if any((cache.get(key) or 0) >= LOGIN_FAILURE_LIMIT for key in self._keys()):
            form = self.get_form()
            form.errors.clear()
            form.add_error(
                None, "Too many failed sign-in attempts. Wait 15 minutes or reset your password."
            )
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form: AuthenticationForm) -> HttpResponse:
        if self.request.method == "POST":
            for key in self._keys():
                cache.add(key, 0, LOGIN_WINDOW_SECONDS)
                cache.incr(key)
        return super().form_invalid(form)

    def form_valid(self, form: AuthenticationForm) -> HttpResponse:
        cache.delete_many(self._keys())
        return super().form_valid(form)


def create_organization_view(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    form = OrganizationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        organization = create_organization(current_user(request), form.cleaned_data["name"])
        switch_organization(request, organization)
        return redirect("dashboard")
    return render(request, "accounts/create_organization.html", {"form": form})


@require_POST
@membership_required()
def switch(request: HttpRequest, organization_id: int) -> HttpResponse:
    membership = memberships(current_user(request)).filter(organization_id=organization_id).first()
    if membership is not None:
        switch_organization(request, membership.organization)
    return redirect("dashboard")


@membership_required()
def settings_view(request: HttpRequest) -> HttpResponse:
    membership = current_membership(request)
    organization = membership.organization
    is_owner = membership.has_role(Role.OWNER)
    form = OrganizationSettingsForm(
        request.POST or None,
        initial={
            "name": organization.name,
            "ai_assistance_enabled": organization.ai_assistance_enabled,
            "ml_data_consent": organization.ml_data_consent,
        },
    )
    if request.method == "POST" and is_owner and form.is_valid():
        before = (organization.ai_assistance_enabled, organization.ml_data_consent)
        organization.name = form.cleaned_data["name"]
        organization.ai_assistance_enabled = form.cleaned_data["ai_assistance_enabled"]
        organization.ml_data_consent = form.cleaned_data["ml_data_consent"]
        organization.save()
        record(organization, current_user(request), "organization.settings_updated", "",
               ai_before=before[0], ai_after=organization.ai_assistance_enabled,
               ml_before=before[1], ml_after=organization.ml_data_consent)  # fmt: skip
        messages.success(request, "Settings saved.")
        return redirect("accounts:settings")
    return render(
        request,
        "accounts/settings.html",
        {
            "form": form,
            "is_owner": is_owner,
            "members": Membership.objects.filter(organization=organization).select_related("user"),
            "invitations": Invitation.objects.filter(organization=organization).usable(),
            "invite_form": InviteForm(),
            "delete_form": DeleteOrganizationForm(),
            "roles": Role.choices,
            "audit": AuditEvent.objects.filter(organization=organization).select_related("actor")[
                :30
            ]
            if membership.has_role(Role.ADMIN)
            else [],
            "invite_link": request.session.pop("invite_link", None),
        },
    )


@require_POST
@membership_required(Role.OWNER)
def invite(request: HttpRequest) -> HttpResponse:
    organization = current_org(request)
    form = InviteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a valid e-mail address and role.")
        return redirect("accounts:settings")
    try:
        require_capacity(
            organization, "members", Membership.objects.filter(organization=organization).count()
        )
    except LimitExceeded as error:
        messages.error(request, str(error))
        return redirect("accounts:settings")
    invitation, token = Invitation.issue(
        organization, form.cleaned_data["email"], form.cleaned_data["role"], current_user(request)
    )
    record(
        organization,
        current_user(request),
        "member.invited",
        invitation.email,
        role=invitation.role,
    )
    request.session["invite_link"] = request.build_absolute_uri(
        reverse("accounts:accept_invite", args=[token])
    )
    messages.success(
        request, "Invitation created. Send the link below to the person; it is shown once."
    )
    return redirect("accounts:settings")


def accept_invite(request: HttpRequest, token: str) -> HttpResponse:
    invitation = Invitation.objects.usable().filter(token_hash=hash_token(token)).first()
    if invitation is None:
        return render(request, "accounts/invite_invalid.html", status=404)
    if not request.user.is_authenticated:
        messages.info(request, f"Sign in or create an account as {invitation.email} to join.")
        return redirect(f"{reverse('accounts:login')}?next={request.path}")
    user = current_user(request)
    if user.email.lower() != invitation.email:
        messages.error(request, f"This invitation is for {invitation.email}.")
        return redirect("dashboard")
    if request.method == "POST":
        with transaction.atomic():
            Membership.objects.get_or_create(
                user=user, organization=invitation.organization, defaults={"role": invitation.role}
            )
            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=["accepted_at"])
        record(invitation.organization, user, "member.joined", user.email)
        switch_organization(request, invitation.organization)
        return redirect("dashboard")
    return render(request, "accounts/accept_invite.html", {"invitation": invitation})


@require_POST
@membership_required(Role.OWNER)
def change_role(request: HttpRequest, membership_id: int) -> HttpResponse:
    organization = current_org(request)
    target = get_owned(Membership.objects.all(), organization, pk=membership_id)
    role = request.POST.get("role", "")
    remove = request.POST.get("remove") == "yes"
    owners = Membership.objects.filter(organization=organization, role=Role.OWNER)
    if target.role == Role.OWNER and owners.count() == 1 and (remove or role != Role.OWNER):
        messages.error(request, "An organisation needs at least one owner.")
        return redirect("accounts:settings")
    if remove:
        target.delete()
        record(organization, current_user(request), "member.removed", target.user.email)
    elif role in Role.values:
        target.role = role
        target.save(update_fields=["role"])
        record(
            organization, current_user(request), "member.role_changed", target.user.email, role=role
        )
    return redirect("accounts:settings")


@require_POST
@membership_required(Role.OWNER)
def delete_organization(request: HttpRequest) -> HttpResponse:
    """Deletes the organisation and every project, scan, report and finding it owns."""
    organization = current_org(request)
    form = DeleteOrganizationForm(request.POST)
    if not form.is_valid() or form.cleaned_data["confirm"] != organization.name:
        messages.error(request, "Type the exact organisation name to confirm.")
        return redirect("accounts:settings")
    subscription = subscription_for(organization)
    if subscription.status in ("active", "trialing", "past_due") and subscription.plan != "free":
        messages.error(
            request, "Cancel the paid subscription in Billing before deleting the organisation."
        )
        return redirect("accounts:settings")
    organization.delete()
    messages.success(request, "The organisation and all of its data were deleted.")
    return redirect("dashboard")
