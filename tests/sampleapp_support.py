"""Helpers for tests that scan the separate sample app through the real runner path."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from accessguard.adapters.runner_config import RunnerConfig, parse_runner_config
from accessguard_sampleapp.server import RunningSampleApp
from tests.helpers import REPO_ROOT

CONFIG_DIR = REPO_ROOT / "sampleapp" / "accessguard_sampleapp" / "config"


def sample_spec(origin: str, **changes: object) -> dict[str, Any]:
    """The shipped sample scan spec pointed at ``origin``."""
    document: dict[str, Any] = json.loads((CONFIG_DIR / "scan-spec.json").read_text("utf-8"))
    document = copy.deepcopy(document)
    document["target"]["origin"] = origin
    document.update(changes)
    return document


def expected_failures(variant: str) -> set[str]:
    key = json.loads((CONFIG_DIR / "answer_key.json").read_text("utf-8"))
    return {check for checks in key[variant].values() for check in checks}


def runner_config(app: RunningSampleApp, directory: Path, **target: object) -> RunnerConfig:
    """A runner config that trusts ``app``'s origin, with its keys in a local credentials file."""
    credentials = directory / "credentials.json"
    credentials.write_text(json.dumps(dict(app.api_keys)), "utf-8")
    entry: dict[str, object] = {
        "origin": app.origin,
        "zone": "loopback",
        "credentials_file": credentials.name,
        **target,
    }
    return parse_runner_config({"targets": [entry], "poll_seconds": 1}, base_dir=directory)
