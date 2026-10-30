"""The runner's local configuration: which origins it may scan and where credentials live.

This file is the customer-side half of the authorization: a job from the control plane
can only target an origin listed here, in the network zone given here. Credentials are
read from a local JSON file or from environment variables and never leave the runner.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from accessguard.domain.credentials import Secret
from accessguard.domain.errors import PolicyError, ScopeViolationError
from accessguard.domain.scope import NetworkZone, TargetScope

MAX_CONFIG_BYTES = 262_144
MAX_TARGETS = 50
MAX_CREDENTIALS_BYTES = 1_048_576
DEFAULT_TOKEN_ENV = "ACCESSGUARD_RUNNER_TOKEN"  # noqa: S105 - a variable name
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class RunnerConfigError(PolicyError):
    """The runner configuration or a credential source is invalid."""


@dataclass(frozen=True)
class TrustedTarget:
    origin: str
    zone: NetworkZone
    credentials_file: Path | None = None
    credentials_env_prefix: str | None = None
    max_requests: int | None = None
    """Optional local cap; the runner uses the lower of this and the job's budget."""

    def scope(self, path_prefixes: Collection[str]) -> TargetScope:
        return TargetScope.authorized(
            self.origin, zone=self.zone, path_prefixes=tuple(path_prefixes)
        )


@dataclass(frozen=True)
class RunnerConfig:
    control_plane_url: str | None
    token_env: str
    targets: tuple[TrustedTarget, ...]
    poll_seconds: float = 5.0

    def trusted_target(self, origin: str) -> TrustedTarget:
        """Return the local entry for ``origin`` or refuse the job before any request."""
        wanted = _normalized_origin(origin)
        for target in self.targets:
            if target.origin == wanted:
                return target
        raise ScopeViolationError(
            f"Origin {wanted} is not in this runner's local allowlist; add it to the runner "
            "configuration if you are authorized to test it"
        )


def _normalized_origin(origin: str) -> str:
    # One normalization for both sides of the comparison; PRIVATE accepts http and https.
    return TargetScope.authorized(origin, zone=NetworkZone.PRIVATE).origin


def load_runner_config(path: Path) -> RunnerConfig:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise RunnerConfigError(f"Cannot read runner config {path}: {error.strerror}") from error
    if len(data) > MAX_CONFIG_BYTES:
        raise RunnerConfigError(f"Runner config exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise RunnerConfigError(f"Runner config is not valid TOML: {error}") from error
    return parse_runner_config(document, base_dir=path.resolve().parent)


def parse_runner_config(document: Mapping[str, object], *, base_dir: Path) -> RunnerConfig:
    _keys(document, "runner config", {"control_plane", "targets", "poll_seconds"})
    url: str | None = None
    token_env = DEFAULT_TOKEN_ENV
    plane = document.get("control_plane")
    if plane is not None:
        plane = _table(plane, "control_plane")
        _keys(plane, "control_plane", {"url", "token_env", "allow_plain_http"})
        allow_plain = plane.get("allow_plain_http", False)
        if not isinstance(allow_plain, bool):
            raise RunnerConfigError("control_plane.allow_plain_http must be true or false")
        url = _control_plane_url(plane.get("url"), allow_plain_http=allow_plain)
        token_env = _env_name(plane.get("token_env", DEFAULT_TOKEN_ENV), "control_plane.token_env")
    raw_targets = document.get("targets", [])
    if not isinstance(raw_targets, list) or not raw_targets or len(raw_targets) > MAX_TARGETS:
        raise RunnerConfigError(f"targets must list 1-{MAX_TARGETS} trusted origins")
    targets = tuple(_trusted(item, i, base_dir) for i, item in enumerate(raw_targets))
    if len({t.origin for t in targets}) != len(targets):
        raise RunnerConfigError("targets lists the same origin more than once")
    poll = document.get("poll_seconds", 5.0)
    if not isinstance(poll, int | float) or isinstance(poll, bool) or not 1 <= poll <= 300:
        raise RunnerConfigError("poll_seconds must be between 1 and 300")
    return RunnerConfig(url, token_env, targets, float(poll))


def _control_plane_url(value: object, *, allow_plain_http: bool = False) -> str:
    """https, or http on loopback; plain http elsewhere only when explicitly allowed.

    ``allow_plain_http`` exists for a runner and control plane on the same private
    network (for example the demo container next to the web container); the runner token
    then travels unencrypted on that network, so it must never be used across the internet.
    """
    if not isinstance(value, str):
        raise RunnerConfigError("control_plane.url must be a string")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise RunnerConfigError("control_plane.url must be an http(s) URL")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS and not allow_plain_http:
        raise RunnerConfigError("control_plane.url must use https unless it is on loopback")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise RunnerConfigError("control_plane.url must not contain credentials or a query")
    return value.rstrip("/")


def _trusted(value: object, index: int, base_dir: Path) -> TrustedTarget:
    where = f"targets[{index}]"
    data = _table(value, where)
    _keys(
        data,
        where,
        {"origin", "zone", "credentials_file", "credentials_env_prefix", "max_requests"},
        required={"origin", "zone"},
    )
    try:
        zone = NetworkZone(str(data["zone"]))
    except ValueError:
        raise RunnerConfigError(f"{where}.zone must be loopback, private or public") from None
    origin = data["origin"]
    if not isinstance(origin, str):
        raise RunnerConfigError(f"{where}.origin must be a string")
    try:
        TargetScope.authorized(origin, zone=zone)
        normalized = _normalized_origin(origin)
    except ScopeViolationError as error:
        raise RunnerConfigError(f"{where}.origin: {error}") from error
    file_value = data.get("credentials_file")
    prefix_value = data.get("credentials_env_prefix")
    if file_value is None and prefix_value is None:
        raise RunnerConfigError(f"{where} needs credentials_file or credentials_env_prefix")
    credentials_file: Path | None = None
    if file_value is not None:
        if not isinstance(file_value, str):
            raise RunnerConfigError(f"{where}.credentials_file must be a string")
        credentials_file = (base_dir / file_value).resolve()
    prefix = (
        None if prefix_value is None else _env_name(prefix_value, f"{where}.credentials_env_prefix")
    )
    cap = data.get("max_requests")
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 1):
        raise RunnerConfigError(f"{where}.max_requests must be a positive integer")
    return TrustedTarget(normalized, zone, credentials_file, prefix, cap)


def credential_env_name(prefix: str, identity_id: str) -> str:
    """``ACCESSGUARD_CRED_`` + identity id upper-cased, other characters as ``_``."""
    return prefix + re.sub(r"[^A-Z0-9]", "_", identity_id.upper())


def load_credentials(
    target: TrustedTarget, identity_ids: Collection[str], environ: Mapping[str, str]
) -> dict[str, Secret]:
    """Collect credentials for ``identity_ids``. Missing ones are simply absent.

    The engine then marks those identities' sessions invalid and their checks
    INCONCLUSIVE; a missing credential can never produce a PASS.
    """
    found: dict[str, Secret] = {}
    if target.credentials_file is not None:
        found.update(_credentials_file(target.credentials_file, identity_ids))
    if target.credentials_env_prefix is not None:
        for identity in identity_ids:
            value = environ.get(credential_env_name(target.credentials_env_prefix, identity))
            if value:
                found[identity] = Secret(value)
    return found


def _credentials_file(path: Path, identity_ids: Collection[str]) -> dict[str, Secret]:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise RunnerConfigError(f"Cannot read credentials file {path}: {error.strerror}") from error
    if len(data) > MAX_CREDENTIALS_BYTES:
        raise RunnerConfigError("Credentials file is too large")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # Never include the content: it holds credentials.
        raise RunnerConfigError(f"Credentials file {path} is not valid JSON") from None
    if not isinstance(document, dict):
        raise RunnerConfigError("Credentials file must be a JSON object {identity: credential}")
    return {
        identity: Secret(value)
        for identity in identity_ids
        if isinstance(value := document.get(identity), str) and value
    }


def _table(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise RunnerConfigError(f"{where} must be a table")
    return value


def _keys(
    data: Mapping[str, object],
    where: str,
    allowed: set[str],
    *,
    required: frozenset[str] | set[str] = frozenset(),
) -> None:
    missing = set(required) - set(data)
    if missing:
        raise RunnerConfigError(f"{where} is missing: {', '.join(sorted(missing))}")
    unknown = set(data) - allowed
    if unknown:
        raise RunnerConfigError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")


def _env_name(value: object, where: str) -> str:
    if not isinstance(value, str) or not _ENV_NAME.fullmatch(value):
        raise RunnerConfigError(f"{where} must be an upper-case environment variable name")
    return value
