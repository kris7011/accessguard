"""Forms for the project configuration. Field rules come from the engine's domain types.

Each form validates its own fields by constructing the engine's value objects (``Identity``,
``Resource``, ``TargetScope``), so the web UI can never accept something the runner rejects.
Cross-references (owners, collections) are validated once more on the whole document.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.forms import formset_factory

from accessguard.adapters.http_target import AuthKind, AuthScheme
from accessguard.application.gate import MAX_REQUEST_BUDGET
from accessguard.domain.errors import PolicyError, ScopeViolationError
from accessguard.domain.policy import Collection, Grant, Identity, Resource, SessionProbe
from accessguard.domain.scope import NetworkZone, TargetScope, normalize_path_prefix
from accessguard_web.projects.config import (
    access_choices,
    grants_from_pairs,
    identity_choices,
    pairs_from_grants,
)

AUTH_CHOICES = [
    (AuthKind.BEARER.value, "Bearer token (Authorization: Bearer ...)"),
    (AuthKind.HEADER.value, "API key in a custom header"),
    (AuthKind.COOKIE.value, "Session cookie"),
]


class ProjectForm(forms.Form):
    name = forms.CharField(max_length=120, help_text="For example: Staging API")


class TargetForm(forms.Form):
    origin = forms.CharField(
        max_length=300,
        label="Base URL of your test environment",
        help_text="Scheme, host and optional port only, e.g. https://staging.example.com. "
        "Only test environments you own or may test.",
        widget=forms.TextInput(attrs={"placeholder": "https://staging.example.com"}),
    )
    path_prefixes = forms.CharField(
        label="Allowed endpoints (path prefixes)",
        help_text="One per line. Requests outside these prefixes are refused by the runner.",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "/api/v1"}),
        initial="/",
    )
    auth_kind = forms.ChoiceField(choices=AUTH_CHOICES, label="How test users authenticate")
    auth_name = forms.CharField(
        max_length=64,
        required=False,
        label="Header or cookie name",
        help_text="Only for API-key headers or cookies, e.g. X-Api-Key or sessionid.",
    )
    session_path = forms.CharField(
        max_length=300,
        label="Session check endpoint",
        help_text="An endpoint that returns the signed-in user, e.g. /api/v1/me. Used to "
        "detect expired or wrong credentials, so they are never mistaken for a denial.",
    )
    subject_field = forms.CharField(
        max_length=200,
        label="Field that identifies the user",
        help_text="Dotted path into the JSON response, e.g. email or data.user.id.",
    )

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        try:
            prefixes = [
                normalize_path_prefix(p.strip())
                for p in data.get("path_prefixes", "").splitlines()
                if p.strip()
            ]
            scope = TargetScope.authorized(
                data.get("origin", "").strip().rstrip("/"),
                zone=NetworkZone.PRIVATE,
                path_prefixes=prefixes or ["/"],
            )
            if data.get("origin", "").startswith("http://") and not _is_local(scope.host):
                self.add_error(
                    "origin",
                    "Use https for anything that is not on your own machine or network; "
                    "runners refuse plain http for public targets.",
                )
            kind = AuthKind(data.get("auth_kind", "bearer"))
            name = data.get("auth_name") or ("Authorization" if kind is AuthKind.BEARER else "")
            if kind is not AuthKind.BEARER and not name:
                self.add_error("auth_name", "Enter the header or cookie name.")
            else:
                AuthScheme(kind, name if kind is not AuthKind.BEARER else "Authorization")
            SessionProbe(data.get("session_path", ""), data.get("subject_field", ""))
            if not scope.allows_path(data.get("session_path", "")):
                self.add_error(
                    "session_path", "The session check must lie under an allowed prefix."
                )
        except (ScopeViolationError, PolicyError, ValueError) as error:
            raise forms.ValidationError(str(error)) from error
        data["origin"] = scope.origin
        data["prefixes"] = list(scope.path_prefixes)
        return data


def _is_local(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(
        (".local", ".internal", ".test")
    )


class IdentityForm(forms.Form):
    id = forms.CharField(max_length=64, label="Test user id", help_text="e.g. alice-acme")
    tenant = forms.CharField(max_length=64, help_text="Customer/organisation in your app")
    role = forms.CharField(max_length=64, help_text="Role in your app, e.g. admin")
    subject = forms.CharField(
        max_length=256,
        required=False,
        label="Value returned by the session check",
        help_text="e.g. alice@acme.test. Empty: the test user id.",
    )

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("id"):
            try:
                Identity(
                    data["id"],
                    data.get("tenant", ""),
                    data.get("role", ""),
                    data.get("subject") or None,
                )
            except PolicyError as error:
                raise forms.ValidationError(str(error)) from error
        return data


class CollectionForm(forms.Form):
    id = forms.CharField(max_length=64, label="List id", help_text="e.g. projects")
    path = forms.CharField(max_length=300, label="List endpoint", help_text="e.g. /api/v1/projects")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("id"):
            try:
                Collection(data["id"], data.get("path", ""))
            except PolicyError as error:
                raise forms.ValidationError(str(error)) from error
        return data


class ResourceForm(forms.Form):
    id = forms.CharField(max_length=64, label="Record id", help_text="e.g. acme-invoice-1")
    category = forms.CharField(max_length=64, help_text="e.g. invoice")
    path = forms.CharField(
        max_length=300, label="Endpoint of this record", help_text="e.g. /api/v1/invoices/123"
    )
    owner = forms.ChoiceField(label="Owner (test user who can read it)")
    protected_fields = forms.CharField(
        max_length=500,
        help_text="Comma-separated fields whose values prove the record was seen, e.g. "
        "data.title, data.reference. Give them distinctive test values.",
    )
    collection = forms.ChoiceField(required=False, label="Appears in list")
    access = forms.MultipleChoiceField(
        required=False,
        label="Who may read it",
        widget=forms.CheckboxSelectMultiple,
        help_text="Every tenant/role combination the policy allows. Others must be denied.",
    )

    def __init__(self, *args: Any, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["owner"].choices = identity_choices(config)  # type: ignore[attr-defined]
        collections = (config.get("policy") or {}).get("collections") or []
        self.fields["collection"].choices = [("", "None")] + [  # type: ignore[attr-defined]
            (c["id"], c["id"]) for c in collections if isinstance(c, dict)
        ]
        self.fields["access"].choices = access_choices(config)  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if not data.get("id"):
            return data
        fields = [f.strip() for f in data.get("protected_fields", "").split(",") if f.strip()]
        grants = grants_from_pairs(data.get("access") or [])
        try:
            Resource(
                data["id"],
                data.get("category", ""),
                data.get("path", ""),
                data.get("owner", "") or "x",
                tuple(fields),
                tuple(Grant(g["tenant"], frozenset(g["roles"])) for g in grants)
                or (Grant("x", frozenset({"x"})),),
                data.get("collection") or None,
            )
        except PolicyError as error:
            raise forms.ValidationError(str(error)) from error
        if not grants:
            self.add_error("access", "Select at least the owner's tenant and role.")
        data["fields_list"] = fields
        data["grants"] = grants
        return data


class LimitsForm(forms.Form):
    max_requests = forms.IntegerField(
        min_value=1,
        max_value=MAX_REQUEST_BUDGET,
        label="Request budget",
        help_text="Total requests a scan may send, including session checks, owner baselines "
        "and retests.",
    )
    min_interval_seconds = forms.FloatField(
        min_value=0,
        max_value=60,
        label="Minimum seconds between requests",
        help_text="Rate limit towards your test environment. 0 sends requests one after another.",
    )
    max_duration_seconds = forms.FloatField(
        min_value=10, max_value=3600, label="Maximum scan duration (seconds)",
        help_text="The runner stops the scan at this deadline and reports partial results.",
    )  # fmt: skip


IdentityFormSet = formset_factory(IdentityForm, extra=2, can_delete=True, max_num=200)
CollectionFormSet = formset_factory(CollectionForm, extra=1, can_delete=True, max_num=100)
ResourceFormSet = formset_factory(ResourceForm, extra=1, can_delete=True, max_num=2000)


def resource_initial(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": r["id"],
            "category": r["category"],
            "path": r["path"],
            "owner": r["owner"],
            "protected_fields": ", ".join(r.get("protected_fields", [])),
            "collection": r.get("collection") or "",
            "access": pairs_from_grants(r.get("grants", [])),
        }
        for r in resources
        if isinstance(r, dict)
    ]


class OpenApiImportForm(forms.Form):
    document = forms.FileField(required=False, label="OpenAPI 3 document (JSON)")
    pasted = forms.CharField(
        required=False, label="…or paste it here", widget=forms.Textarea(attrs={"rows": 6})
    )
    use_assistant = forms.BooleanField(
        required=False,
        label="Let the setup assistant suggest protected fields and categories",
        help_text="Sends endpoint paths, summaries and field names (no data) to the AI provider.",
    )

    def raw(self) -> bytes:
        upload = self.cleaned_data.get("document")
        if upload is not None:
            return bytes(upload.read(2_000_001))
        return str(self.cleaned_data.get("pasted", "")).encode("utf-8")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if not data.get("document") and not str(data.get("pasted", "")).strip():
            raise forms.ValidationError("Upload or paste an OpenAPI document.")
        return data


class TemplateResourceForm(forms.Form):
    """Turns an imported endpoint template into a concrete resource."""

    template = forms.IntegerField(widget=forms.HiddenInput)
    record_path = forms.CharField(
        max_length=300, label="Endpoint of one test record",
        help_text="Replace the {placeholders} with an id from your test data.",
    )  # fmt: skip
