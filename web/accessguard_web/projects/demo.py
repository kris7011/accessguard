"""The demo project: a ready configuration for the synthetic sample API.

``demo_spec.json`` is a copy of the sample app's shipped scan spec (a test keeps them
equal). The control plane never imports the sample app; the demo target runs as a separate
process next to the platform demo runner, which trusts only the two demo origins.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from accessguard_web import conf
from accessguard_web.accounts.models import Organization
from accessguard_web.projects.models import Project

DEMO_NAME = "Demo: sample projects API (synthetic)"
_SPEC_PATH = Path(__file__).with_name("demo_spec.json")


def demo_config(*, fixed: bool = False) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(_SPEC_PATH.read_text("utf-8"))
    origins = conf.product()
    document["target"]["origin"] = origins["DEMO_FIXED_ORIGIN" if fixed else "DEMO_FLAWED_ORIGIN"]
    document["label"] = DEMO_NAME
    return copy.deepcopy(document)


def demo_origins() -> frozenset[str]:
    origins = conf.product()
    return frozenset({origins["DEMO_FLAWED_ORIGIN"], origins["DEMO_FIXED_ORIGIN"]})


def create_demo_project(organization: Organization) -> Project:
    return Project.objects.create(
        organization=organization, name=DEMO_NAME, is_demo=True, config=demo_config()
    )


def demo_is_fixed(project: Project) -> bool:
    origin = project.config.get("target", {}).get("origin")
    return bool(origin == conf.product()["DEMO_FIXED_ORIGIN"])


def set_demo_variant(project: Project, *, fixed: bool) -> None:
    """Simulates deploying (or reverting) the fix by pointing at the other demo instance."""
    project.config = demo_config(fixed=fixed)
    project.save(update_fields=["config", "updated_at"])
