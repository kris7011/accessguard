"""Builders for web tests: organisations, members, projects and real engine reports.

Reports are produced by the real engine scanning the separate sample app in-process, so
ingestion and findings are tested against genuine output, not hand-written fixtures.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from django.test import Client

from accessguard.adapters.runner_config import RunnerConfig, parse_runner_config
from accessguard.adapters.scan_spec import parse_scan_spec
from accessguard.entrypoints.runner_service import execute_spec
from accessguard_sampleapp.app import Variant
from accessguard_sampleapp.server import RunningSampleApp, run_sample_app
from accessguard_web.accounts.models import Membership, Organization, Role, User
from accessguard_web.accounts.views import create_organization
from accessguard_web.projects.demo import demo_config
from accessguard_web.projects.models import Project
from accessguard_web.scans import services
from accessguard_web.scans.models import Finding, Runner, Scan

PASSWORD = "correct horse battery 42"
SAMPLE_DIR = Path(__file__).resolve().parents[3] / "sampleapp" / "accessguard_sampleapp" / "config"


def expected_failures(variant: str) -> set[str]:
    key = json.loads((SAMPLE_DIR / "answer_key.json").read_text("utf-8"))
    return {check for checks in key[variant].values() for check in checks}


def make_user(email: str) -> User:
    return User.objects.create_user(email, PASSWORD)


def make_org(name: str, email: str | None = None) -> tuple[Organization, User]:
    owner = make_user(email or f"owner@{name.lower()}.test")
    return create_organization(owner, name), owner


def add_member(organization: Organization, email: str, role: str) -> User:
    user = make_user(email)
    Membership.objects.create(user=user, organization=organization, role=role)
    return user


def login(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def sample_project(organization: Organization, origin: str, name: str = "Staging API") -> Project:
    """A non-demo project whose configuration is the sample app's spec at ``origin``."""
    config = demo_config()
    config["target"]["origin"] = origin
    config["label"] = name
    return Project.objects.create(organization=organization, name=name, config=config)


@contextmanager
def sample_runtime(
    variant: Variant = Variant.FLAWED,
) -> Iterator[tuple[RunningSampleApp, RunnerConfig]]:
    with run_sample_app(variant) as app, tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "credentials.json").write_text(json.dumps(dict(app.api_keys)), "utf-8")
        config = parse_runner_config(
            {"targets": [{"origin": app.origin, "zone": "loopback", "credentials_file": "credentials.json"}]},
            base_dir=Path(tmp),
        )  # fmt: skip
        yield app, config


def run_scan_for(scan: Scan, config: RunnerConfig, **spec_overrides: Any) -> dict[str, Any]:
    """Execute a scan's frozen spec with the real engine, as a runner would."""
    spec = parse_scan_spec({**scan.spec, **spec_overrides})
    return execute_spec(spec, config, environ={}).report


def org_runner(organization: Organization) -> tuple[Runner, str]:
    return Runner.issue(name="runner", organization=organization)


def complete_scan(scan: Scan, runner: Runner, report: dict[str, Any]) -> Scan:
    claimed = services.claim_next(runner)
    assert claimed is not None and claimed.pk == scan.pk
    return services.accept_report(claimed, report)


def scan_and_ingest(
    project: Project,
    owner: User,
    runner: Runner,
    config: RunnerConfig,
    findings: list[Finding] | None = None,
) -> Scan:
    scan = (
        services.create_retest(project, owner, findings)
        if findings
        else services.approve_scan(project, owner)
    )
    return complete_scan(scan, runner, run_scan_for(scan, config))


ROLE_ADMIN, ROLE_VIEWER, ROLE_OWNER = Role.ADMIN, Role.VIEWER, Role.OWNER
