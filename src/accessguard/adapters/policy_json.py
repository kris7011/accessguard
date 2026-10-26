"""Loads an access policy from JSON. This is a trust boundary: input is validated strictly.

Unknown keys are rejected so that a typo (``"grant"`` instead of ``"grants"``) can
never silently widen or narrow the tested policy. Credentials are never part of a
policy file; identities only name tenant and role.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from accessguard.domain.errors import PolicyError
from accessguard.domain.policy import (
    AccessPolicy,
    Collection,
    Grant,
    Identity,
    Resource,
    SessionProbe,
)

SUPPORTED_SCHEMA_VERSIONS = frozenset({"1"})
MAX_POLICY_BYTES = 1_048_576
MAX_IDENTITIES = 200
MAX_RESOURCES = 2_000
MAX_COLLECTIONS = 100
MAX_GRANTS_PER_RESOURCE = 50
MAX_DESCRIPTION_LENGTH = 2_000


@dataclass(frozen=True)
class LoadedPolicy:
    policy: AccessPolicy
    sha256: str


def load_policy_file(path: Path) -> LoadedPolicy:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise PolicyError(f"Cannot read policy file {path}: {error.strerror}") from error
    if size > MAX_POLICY_BYTES:
        raise PolicyError(f"Policy file exceeds {MAX_POLICY_BYTES} bytes")
    return load_policy_bytes(path.read_bytes())


def load_policy_bytes(data: bytes) -> LoadedPolicy:
    if len(data) > MAX_POLICY_BYTES:
        raise PolicyError(f"Policy exceeds {MAX_POLICY_BYTES} bytes")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise PolicyError(f"Policy is not valid UTF-8 JSON: {error}") from error
    return LoadedPolicy(parse_policy(document), hashlib.sha256(data).hexdigest())


def parse_policy(document: object) -> AccessPolicy:
    root = _object(
        document,
        "policy",
        required={"schema_version", "id", "session_probe", "identities", "resources"},
        optional={"description", "collections"},
    )
    version = _string(root["schema_version"], "schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise PolicyError(f"Unsupported policy schema_version {version!r}")
    description = _string(root.get("description", ""), "description", allow_empty=True)
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise PolicyError(f"description exceeds {MAX_DESCRIPTION_LENGTH} characters")
    return AccessPolicy(
        id=_string(root["id"], "id"),
        description=description,
        session_probe=_session_probe(root["session_probe"]),
        identities=tuple(
            _identity(item, f"identities[{i}]")
            for i, item in enumerate(_list(root["identities"], "identities", MAX_IDENTITIES))
        ),
        collections=tuple(
            _collection(item, f"collections[{i}]")
            for i, item in enumerate(
                _list(root.get("collections", []), "collections", MAX_COLLECTIONS)
            )
        ),
        resources=tuple(
            _resource(item, f"resources[{i}]")
            for i, item in enumerate(_list(root["resources"], "resources", MAX_RESOURCES))
        ),
    )


def _session_probe(value: object) -> SessionProbe:
    data = _object(value, "session_probe", required={"path", "subject_field"})
    return SessionProbe(
        path=_string(data["path"], "session_probe.path"),
        subject_field=_string(data["subject_field"], "session_probe.subject_field"),
    )


def _identity(value: object, where: str) -> Identity:
    data = _object(value, where, required={"id", "tenant", "role"})
    return Identity(
        id=_string(data["id"], f"{where}.id"),
        tenant=_string(data["tenant"], f"{where}.tenant"),
        role=_string(data["role"], f"{where}.role"),
    )


def _collection(value: object, where: str) -> Collection:
    data = _object(value, where, required={"id", "path"})
    return Collection(
        id=_string(data["id"], f"{where}.id"), path=_string(data["path"], f"{where}.path")
    )


def _resource(value: object, where: str) -> Resource:
    data = _object(
        value,
        where,
        required={"id", "category", "path", "owner", "protected_fields", "grants"},
        optional={"collection"},
    )
    collection = data.get("collection")
    return Resource(
        id=_string(data["id"], f"{where}.id"),
        category=_string(data["category"], f"{where}.category"),
        path=_string(data["path"], f"{where}.path"),
        owner=_string(data["owner"], f"{where}.owner"),
        protected_fields=tuple(
            _string(item, f"{where}.protected_fields[{i}]")
            for i, item in enumerate(_list(data["protected_fields"], f"{where}.protected_fields"))
        ),
        grants=tuple(
            _grant(item, f"{where}.grants[{i}]")
            for i, item in enumerate(
                _list(data["grants"], f"{where}.grants", MAX_GRANTS_PER_RESOURCE)
            )
        ),
        collection=None if collection is None else _string(collection, f"{where}.collection"),
    )


def _grant(value: object, where: str) -> Grant:
    data = _object(value, where, required={"tenant", "roles"})
    roles = [
        _string(item, f"{where}.roles[{i}]")
        for i, item in enumerate(_list(data["roles"], f"{where}.roles"))
    ]
    if len(set(roles)) != len(roles):
        raise PolicyError(f"{where}.roles contains duplicates")
    return Grant(tenant=_string(data["tenant"], f"{where}.tenant"), roles=frozenset(roles))


def _object(
    value: object,
    where: str,
    *,
    required: set[str],
    optional: frozenset[str] | set[str] = frozenset(),
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise PolicyError(f"{where} must be a JSON object")
    keys = set(value)
    missing = required - keys
    if missing:
        raise PolicyError(f"{where} is missing required keys: {', '.join(sorted(missing))}")
    unknown = keys - required - set(optional)
    if unknown:
        raise PolicyError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    return value


def _list(value: object, where: str, max_items: int = 100) -> list[object]:
    if not isinstance(value, list):
        raise PolicyError(f"{where} must be a JSON array")
    if len(value) > max_items:
        raise PolicyError(f"{where} has more than {max_items} items")
    return value


def _string(value: object, where: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise PolicyError(f"{where} must be a non-empty string")
    return value
