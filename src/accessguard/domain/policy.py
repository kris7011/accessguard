"""The customer's explicit, declared access policy.

AccessGuard never infers intent from tenant membership alone: a resource may be
read only by identities matched by one of its grants. Cross-tenant sharing is
therefore always an explicit grant, which lets shared resources act as negative
controls (legitimate access that must not be reported as a leak).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property

from accessguard.domain.errors import PolicyError
from accessguard.domain.scope import is_safe_request_path

ANONYMOUS = "anonymous"
"""Reserved actor id for requests sent without any credential."""

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_FIELD_SEGMENT = r"[A-Za-z_][A-Za-z0-9_-]{0,63}"
# A JSON object key, or a dotted path of up to eight keys into nested objects ("data.email").
_FIELD_PATH = re.compile(rf"{_FIELD_SEGMENT}(?:\.{_FIELD_SEGMENT}){{0,7}}")
MAX_SUBJECT_LENGTH = 256


def _require_identifier(value: str, what: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise PolicyError(
            f"{what} {value!r} must be 1-64 characters of lowercase letters, "
            "digits, '.', '_' or '-'"
        )


def _require_path(value: str, what: str) -> None:
    if not is_safe_request_path(value):
        raise PolicyError(
            f"{what} {value!r} must be an absolute path without query, fragment or dot segments"
        )


def is_field_path(value: str) -> bool:
    """True for a JSON key or a dotted path of keys (see :func:`lookup_field`)."""
    return bool(_FIELD_PATH.fullmatch(value))


def lookup_field(document: object, path: str) -> object:
    """Follow a dotted field path through nested JSON objects; ``None`` when absent."""
    value = document
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _require_unique(ids: Iterable[str], what: str) -> None:
    seen: set[str] = set()
    for item in ids:
        if item in seen:
            raise PolicyError(f"Duplicate {what} id {item!r}")
        seen.add(item)


@dataclass(frozen=True)
class Identity:
    """A test identity supplied by the customer. Credentials are kept outside the policy.

    ``subject`` is the value the session probe must return for this identity (for example
    an e-mail address or user id); it defaults to the identity id.
    """

    id: str
    tenant: str
    role: str
    subject: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.id, "Identity id")
        if self.id == ANONYMOUS:
            raise PolicyError(f"Identity id {ANONYMOUS!r} is reserved")
        _require_identifier(self.tenant, "Tenant")
        _require_identifier(self.role, "Role")
        if self.subject is not None and not (
            0 < len(self.subject) <= MAX_SUBJECT_LENGTH and self.subject.isprintable()
        ):
            raise PolicyError(
                f"Subject of identity {self.id!r} must be 1-{MAX_SUBJECT_LENGTH} printable "
                "characters"
            )

    @property
    def expected_subject(self) -> str:
        return self.id if self.subject is None else self.subject


@dataclass(frozen=True)
class Grant:
    """Read access for the listed roles of one tenant."""

    tenant: str
    roles: frozenset[str]

    def __post_init__(self) -> None:
        _require_identifier(self.tenant, "Grant tenant")
        if not self.roles:
            raise PolicyError(f"Grant for tenant {self.tenant!r} must list at least one role")
        for role in self.roles:
            _require_identifier(role, "Grant role")

    def permits(self, identity: Identity) -> bool:
        return identity.tenant == self.tenant and identity.role in self.roles


@dataclass(frozen=True)
class Resource:
    """A protected resource and the fields whose disclosure proves access.

    ``owner`` names the identity used to establish the baseline: AccessGuard first
    confirms that this identity can read the resource before judging other actors.
    """

    id: str
    category: str
    path: str
    owner: str
    protected_fields: tuple[str, ...]
    grants: tuple[Grant, ...]
    collection: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.id, "Resource id")
        _require_identifier(self.category, "Resource category")
        _require_path(self.path, f"Path of resource {self.id!r}")
        _require_identifier(self.owner, f"Owner of resource {self.id!r}")
        if not self.protected_fields:
            raise PolicyError(f"Resource {self.id!r} must declare at least one protected field")
        _require_unique(self.protected_fields, f"protected field in resource {self.id!r}")
        for field in self.protected_fields:
            if not is_field_path(field):
                raise PolicyError(f"Protected field {field!r} of resource {self.id!r} is invalid")
        if not self.grants:
            raise PolicyError(f"Resource {self.id!r} must declare at least one grant")
        if self.collection is not None:
            _require_identifier(self.collection, f"Collection of resource {self.id!r}")


@dataclass(frozen=True)
class Collection:
    """A listing endpoint whose items are resources declared in the policy."""

    id: str
    path: str

    def __post_init__(self) -> None:
        _require_identifier(self.id, "Collection id")
        _require_path(self.path, f"Path of collection {self.id!r}")


@dataclass(frozen=True)
class SessionProbe:
    """An endpoint that echoes the authenticated subject, used to detect invalid sessions."""

    path: str
    subject_field: str

    def __post_init__(self) -> None:
        _require_path(self.path, "Session probe path")
        if not is_field_path(self.subject_field):
            raise PolicyError(f"Session probe subject field {self.subject_field!r} is invalid")


@dataclass(frozen=True)
class AccessPolicy:
    """The complete declared policy. Construction enforces all cross-reference invariants."""

    id: str
    identities: tuple[Identity, ...]
    resources: tuple[Resource, ...]
    session_probe: SessionProbe
    collections: tuple[Collection, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        _require_identifier(self.id, "Policy id")
        if not self.identities:
            raise PolicyError("A policy must declare at least one identity")
        if not self.resources:
            raise PolicyError("A policy must declare at least one resource")
        _require_unique((i.id for i in self.identities), "identity")
        _require_unique((r.id for r in self.resources), "resource")
        _require_unique((c.id for c in self.collections), "collection")
        for resource in self.resources:
            owner = self._identities_by_id.get(resource.owner)
            if owner is None:
                raise PolicyError(
                    f"Owner {resource.owner!r} of resource {resource.id!r} "
                    "is not a declared identity"
                )
            if not self.is_allowed(owner.id, resource):
                raise PolicyError(
                    f"Owner {owner.id!r} must be granted access to resource {resource.id!r}"
                )
            if (
                resource.collection is not None
                and resource.collection not in self._collections_by_id
            ):
                raise PolicyError(
                    f"Resource {resource.id!r} references unknown collection "
                    f"{resource.collection!r}"
                )

    @cached_property
    def _identities_by_id(self) -> dict[str, Identity]:
        return {identity.id: identity for identity in self.identities}

    @cached_property
    def _resources_by_id(self) -> dict[str, Resource]:
        return {resource.id: resource for resource in self.resources}

    @cached_property
    def _collections_by_id(self) -> dict[str, Collection]:
        return {collection.id: collection for collection in self.collections}

    def identity(self, identity_id: str) -> Identity:
        try:
            return self._identities_by_id[identity_id]
        except KeyError:
            raise PolicyError(f"Unknown identity {identity_id!r}") from None

    def resource(self, resource_id: str) -> Resource:
        try:
            return self._resources_by_id[resource_id]
        except KeyError:
            raise PolicyError(f"Unknown resource {resource_id!r}") from None

    def collection(self, collection_id: str) -> Collection:
        try:
            return self._collections_by_id[collection_id]
        except KeyError:
            raise PolicyError(f"Unknown collection {collection_id!r}") from None

    def owner_tenant(self, resource: Resource) -> str:
        return self.identity(resource.owner).tenant

    def is_allowed(self, actor_id: str, resource: Resource) -> bool:
        """Return True when the declared policy grants ``actor_id`` read access to ``resource``."""
        if actor_id == ANONYMOUS:
            return False
        identity = self.identity(actor_id)
        return any(grant.permits(identity) for grant in resource.grants)

    def resources_in(self, collection_id: str) -> tuple[Resource, ...]:
        return tuple(r for r in self.resources if r.collection == collection_id)

    def describe_rule(self, resource: Resource) -> str:
        """Human-readable statement of the declared rule, used in findings."""
        grants = "; ".join(
            f"tenant {grant.tenant} (roles: {', '.join(sorted(grant.roles))})"
            for grant in resource.grants
        )
        return (
            f"Read access to {resource.category} {resource.id!r} is granted only to: {grants}. "
            "Anonymous access is not granted."
        )
