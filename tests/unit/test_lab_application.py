"""The lab's request handling without sockets: the fixture must behave as documented."""

import unittest

from accessguard_lab.dataset import USERS
from accessguard_lab.server import Flaw, LabApplication

TOKENS = {f"tok-{user.id}": user for user in USERS}


def bearer(user_id: str) -> str:
    return f"Bearer tok-{user_id}"


class LabApplicationTests(unittest.TestCase):
    def test_unknown_or_missing_token_is_unauthorized(self) -> None:
        """Ported from v0.1 ``test_unknown_account_denied``."""
        app = LabApplication(frozenset(), TOKENS)
        for header in (None, "Bearer invalid", "Basic abc", bearer("acme-expired")):
            with self.subTest(header=header):
                self.assertEqual(app.handle("GET", "/documents/acme-doc", header)[0], 401)

    def test_missing_document_is_not_found(self) -> None:
        """Ported from v0.1 ``test_missing_document``."""
        app = LabApplication(frozenset(), TOKENS)
        self.assertEqual(app.handle("GET", "/documents/missing", bearer("acme-member"))[0], 404)

    def test_only_get_is_supported(self) -> None:
        app = LabApplication(frozenset(), TOKENS)
        self.assertEqual(app.handle("POST", "/documents", bearer("acme-admin"))[0], 405)

    def test_fixed_rules(self) -> None:
        app = LabApplication(frozenset(), TOKENS)
        cases = {
            ("/documents/acme-doc", "acme-member"): 200,
            ("/documents/acme-doc", "globex-admin"): 403,
            ("/documents/acme-shared-brief", "globex-member"): 200,
            ("/documents/acme-shared-brief", "initech-admin"): 403,
            ("/invoices/acme-invoice", "acme-admin"): 200,
            ("/invoices/acme-invoice", "acme-member"): 403,
            ("/invoices/acme-invoice", "globex-admin"): 403,
        }
        for (path, user), status in cases.items():
            with self.subTest(path=path, user=user):
                self.assertEqual(app.handle("GET", path, bearer(user))[0], status)

    def test_each_flaw_changes_only_its_rule(self) -> None:
        idor = LabApplication(frozenset({Flaw.DOCUMENT_IDOR}), TOKENS)
        self.assertEqual(idor.handle("GET", "/documents/acme-doc", bearer("globex-admin"))[0], 200)
        self.assertEqual(
            idor.handle("GET", "/invoices/acme-invoice", bearer("acme-member"))[0], 403
        )
        ignored = LabApplication(frozenset({Flaw.SHARE_IGNORED}), TOKENS)
        self.assertEqual(
            ignored.handle("GET", "/documents/acme-shared-brief", bearer("globex-member"))[0], 403
        )

    def test_list_leak_returns_other_tenants(self) -> None:
        leak = LabApplication(frozenset({Flaw.LIST_LEAK}), TOKENS)
        _, body, _ = leak.handle("GET", "/documents", bearer("initech-member"))
        assert isinstance(body, dict)
        tenants = {item["tenant"] for item in body["items"]}  # type: ignore[attr-defined]
        self.assertEqual(tenants, {"acme", "globex", "initech"})


if __name__ == "__main__":
    unittest.main()
