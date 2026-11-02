"""Named lab profiles: which flaws are enabled."""

from __future__ import annotations

from collections.abc import Mapping

from accessguard_lab.server import Flaw

PROFILES: Mapping[str, frozenset[Flaw]] = {
    "fixed": frozenset(),
    "vulnerable": frozenset(
        {Flaw.DOCUMENT_IDOR, Flaw.SHARE_OVERBROAD, Flaw.INVOICE_ROLE_BYPASS, Flaw.LIST_LEAK}
    ),
    **{flaw.value: frozenset({flaw}) for flaw in Flaw},
}
