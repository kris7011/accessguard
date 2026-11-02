"""Synthetic lab data: three tenants, two roles each, documents, invoices and one share.

All names and values are fictitious. Canary values make disclosure unambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LabUser:
    id: str
    tenant: str
    role: str
    expired: bool = False


@dataclass(frozen=True)
class LabDocument:
    id: str
    tenant: str
    title: str
    canary: str
    shared_with: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LabInvoice:
    id: str
    tenant: str
    reference: str
    canary: str
    amount_cents: int


TENANTS = ("acme", "globex", "initech")

USERS = (
    *(
        LabUser(f"{tenant}-{role}", tenant, role)
        for tenant in TENANTS
        for role in ("admin", "member")
    ),
    # Valid-looking credential whose session has expired: every request returns 401.
    LabUser("acme-expired", "acme", "member", expired=True),
)

DOCUMENTS = (
    LabDocument("acme-doc", "acme", "Acme roadmap (synthetic)", "SYNTHETIC-CANARY-ACME-DOC-7F3A"),
    LabDocument(
        "globex-doc",
        "globex",
        "Globex pricing notes (synthetic)",
        "SYNTHETIC-CANARY-GLOBEX-DOC-91C2",
    ),
    LabDocument(
        "initech-doc",
        "initech",
        "Initech audit memo (synthetic)",
        "SYNTHETIC-CANARY-INITECH-DOC-4B8E",
    ),
    LabDocument(
        "acme-shared-brief",
        "acme",
        "Acme and Globex joint project brief (synthetic)",
        "SYNTHETIC-CANARY-ACME-SHARED-D05E",
        shared_with=frozenset({"globex"}),
    ),
)

INVOICES = (
    LabInvoice(
        "acme-invoice", "acme", "INV-ACME-2026-0001", "SYNTHETIC-CANARY-ACME-INV-2A61", 120_00
    ),
    LabInvoice(
        "globex-invoice",
        "globex",
        "INV-GLOBEX-2026-0001",
        "SYNTHETIC-CANARY-GLOBEX-INV-6D17",
        89_50,
    ),
    LabInvoice(
        "initech-invoice",
        "initech",
        "INV-INITECH-2026-0001",
        "SYNTHETIC-CANARY-INITECH-INV-C3F9",
        42_00,
    ),
)
