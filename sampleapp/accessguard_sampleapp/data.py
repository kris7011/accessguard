"""Synthetic organisations, users, projects and billing records. Nothing here is real data."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    id: str
    email: str
    org: str
    role: str


@dataclass(frozen=True)
class Project:
    id: str
    org: str
    name: str
    budget_code: str
    shared_with: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Billing:
    account_ref: str
    plan_note: str


USERS = (
    User("ana", "ana@orbit.example", "orbit", "owner"),
    User("ben", "ben@orbit.example", "orbit", "viewer"),
    User("cleo", "cleo@quasar.example", "quasar", "owner"),
    User("dev", "dev@quasar.example", "quasar", "viewer"),
)

PROJECTS = {
    project.id: project
    for project in (
        Project("prj-orbit-apollo", "orbit", "Apollo launch plan", "SAMPLE-BUDGET-ORBIT-7731"),
        Project("prj-quasar-nova", "quasar", "Nova data migration", "SAMPLE-BUDGET-QUASAR-2208"),
        Project(
            "prj-quasar-roadmap",
            "quasar",
            "Joint partner roadmap",
            "SAMPLE-BUDGET-SHARED-5150",
            shared_with=frozenset({"orbit"}),
        ),
    )
}

BILLING = {
    "orbit": Billing("SAMPLE-ACCT-ORBIT-0042", "Orbit annual plan, invoiced quarterly"),
    "quasar": Billing("SAMPLE-ACCT-QUASAR-0917", "Quasar monthly plan, card on file"),
}
