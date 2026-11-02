from __future__ import annotations

from typing import Any

from django import forms
from django.contrib.auth import password_validation

from accessguard_web.accounts.models import Role, User


class SignupForm(forms.Form):
    email = forms.EmailField(label="Work e-mail")
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="At least 12 characters; not a common password.",
    )
    organization = forms.CharField(max_length=120, label="Company or team name")
    accept_terms = forms.BooleanField(
        label="I will only test systems that my organisation owns or is authorised to test."
    )

    def clean_email(self) -> str:
        email = str(self.cleaned_data["email"]).lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError(
                "An account with this e-mail already exists. Sign in instead."
            )
        return email

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("password") and data.get("email"):
            try:
                password_validation.validate_password(data["password"], User(email=data["email"]))
            except forms.ValidationError as error:
                self.add_error("password", error)
        return data


class OrganizationForm(forms.Form):
    name = forms.CharField(max_length=120, label="Organisation name")


class OrganizationSettingsForm(forms.Form):
    name = forms.CharField(max_length=120)
    ai_assistance_enabled = forms.BooleanField(
        required=False,
        label="Enable AI assistance",
        help_text="Sends the facts of a finding (rule, roles, endpoint path, status codes, field "
        "names) or OpenAPI endpoint names to the configured AI provider when someone asks for "
        "help. Never credentials or response content.",
    )
    ml_data_consent = forms.BooleanField(
        required=False,
        label="Allow de-identified triage labels to improve check ranking",
        help_text="Only engine categories and counts are exported (no names, paths or ids). Off "
        "by default; you can withdraw it at any time.",
    )


class InviteForm(forms.Form):
    email = forms.EmailField()
    role = forms.ChoiceField(choices=Role.choices, initial=Role.VIEWER)


class DeleteOrganizationForm(forms.Form):
    confirm = forms.CharField(label="Type the organisation name to confirm")
