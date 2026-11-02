"""Scan specifications: what to test (policy), where (target) and within which limits.

A scan spec is the unit the control plane approves and a runner executes; the same JSON
file also drives offline scans (``accessguard scan``), for example in CI. This module is a
trust boundary: input is validated strictly and unknown keys are rejected. A spec never
contains credentials; those stay in the runner's own environment.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from accessguard.adapters.http_target import AuthKind, AuthScheme
from accessguard.adapters.policy_json import MAX_POLICY_BYTES, LoadedPolicy, parse_policy
from accessguard.application.gate import MAX_REQUEST_BUDGET, RunLimits
from accessguard.domain.errors import PolicyError, ScopeViolationError
from accessguard.domain.scope import MAX_PATH_PREFIXES, normalize_path_prefix

SPEC_VERSION = "1"
MAX_SPEC_BYTES = MAX_POLICY_BYTES + 65_536
MAX_LABEL_LENGTH = 200
MAX_SELECTED_CHECKS = 5_000


def canonical_json(value: object) -> bytes:
    """Stable bytes for hashing: sorted keys, no insignificant whitespace, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sha256_of(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class TargetSpec:
    origin: str
    path_prefixes: tuple[str, ...]
    auth: AuthScheme


@dataclass(frozen=True)
class ScanSpec:
    label: str
    target: TargetSpec
    limits: RunLimits
    checks: tuple[str, ...] | None
    """``None`` runs the full policy matrix; otherwise only these check ids (a retest)."""
    policy: LoadedPolicy
    sha256: str
    """Hash of the canonical spec; ties a report to the exact spec that was approved."""


def load_scan_spec_file(path: Path) -> ScanSpec:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise PolicyError(f"Cannot read scan spec {path}: {error.strerror}") from error
    if size > MAX_SPEC_BYTES:
        raise PolicyError(f"Scan spec exceeds {MAX_SPEC_BYTES} bytes")
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise PolicyError(f"Scan spec is not valid UTF-8 JSON: {error}") from error
    return parse_scan_spec(document)


def parse_scan_spec(document: object) -> ScanSpec:
    root = _object(
        document,
        "spec",
        required={"spec_version", "target", "policy"},
        optional={"label", "limits", "checks"},
    )
    if root["spec_version"] != SPEC_VERSION:
        raise PolicyError(f"Unsupported spec_version {root['spec_version']!r}")
    label = root.get("label", "")
    if not isinstance(label, str) or len(label) > MAX_LABEL_LENGTH:
        raise PolicyError(f"label must be a string of at most {MAX_LABEL_LENGTH} characters")
    policy_document = root["policy"]
    policy = LoadedPolicy(parse_policy(policy_document), sha256_of(policy_document))
    return ScanSpec(
        label=label,
        target=_target(root["target"]),
        limits=_limits(root.get("limits", {})),
        checks=_checks(root.get("checks")),
        policy=policy,
        sha256=sha256_of(document),
    )


def _target(value: object) -> TargetSpec:
    data = _object(value, "target", required={"origin"}, optional={"path_prefixes", "auth"})
    origin = data["origin"]
    if not isinstance(origin, str) or not origin:
        raise PolicyError("target.origin must be a non-empty string")
    prefixes = data.get("path_prefixes", ["/"])
    if not isinstance(prefixes, list) or not prefixes or len(prefixes) > MAX_PATH_PREFIXES:
        raise PolicyError(f"target.path_prefixes must list 1-{MAX_PATH_PREFIXES} paths")
    normalized: list[str] = []
    for prefix in prefixes:
        if not isinstance(prefix, str):
            raise PolicyError("target.path_prefixes must contain strings")
        try:
            normalized.append(normalize_path_prefix(prefix))
        except ScopeViolationError as error:
            raise PolicyError(f"target.path_prefixes: {error}") from error
    return TargetSpec(origin, tuple(normalized), _auth(data.get("auth", {"kind": "bearer"})))


def _auth(value: object) -> AuthScheme:
    data = _object(value, "target.auth", required={"kind"}, optional={"name"})
    try:
        kind = AuthKind(str(data["kind"]))
    except ValueError:
        raise PolicyError("target.auth.kind must be bearer, header or cookie") from None
    name = data.get("name", "Authorization" if kind is AuthKind.BEARER else None)
    if not isinstance(name, str):
        raise PolicyError(f"target.auth.name is required for {kind.value} credentials")
    try:
        return AuthScheme(kind, name)
    except ValueError as error:
        raise PolicyError(f"target.auth: {error}") from error


def _limits(value: object) -> RunLimits:
    data = _object(
        value,
        "limits",
        required=set(),
        optional={"max_requests", "min_interval_seconds", "max_duration_seconds"},
    )
    defaults = RunLimits()
    max_requests = data.get("max_requests", defaults.max_requests)
    interval = data.get("min_interval_seconds", defaults.min_interval_seconds)
    duration = data.get("max_duration_seconds", defaults.max_duration_seconds)
    if not isinstance(max_requests, int) or isinstance(max_requests, bool):
        raise PolicyError(f"limits.max_requests must be an integer 1-{MAX_REQUEST_BUDGET}")
    if not _is_number(interval) or not _is_number(duration):
        raise PolicyError("limits.min_interval_seconds and max_duration_seconds must be numbers")
    try:
        return RunLimits(
            max_requests=max_requests,
            min_interval_seconds=float(interval),
            max_duration_seconds=float(duration),
        )
    except ValueError as error:
        raise PolicyError(f"limits: {error}") from error


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _checks(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_SELECTED_CHECKS
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise PolicyError(f"checks must be null or a list of 1-{MAX_SELECTED_CHECKS} check ids")
    return tuple(dict.fromkeys(value))


def _object(
    value: object, where: str, *, required: set[str], optional: set[str]
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise PolicyError(f"{where} must be a JSON object")
    missing = required - set(value)
    if missing:
        raise PolicyError(f"{where} is missing required keys: {', '.join(sorted(missing))}")
    unknown = set(value) - required - optional
    if unknown:
        raise PolicyError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    return value
