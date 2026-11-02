"""SSRF, DNS rebinding and scope-expansion protections of AccessGuard itself."""

import unittest
from unittest import mock

from accessguard.adapters.http_target import ScopedHttpClient
from accessguard.domain.errors import ScopeViolationError
from accessguard.domain.scope import TargetScope


class ScopeEnforcementTests(unittest.TestCase):
    def client(self, *answers: list[str]) -> tuple[ScopedHttpClient, mock.Mock]:
        resolver = mock.Mock(side_effect=list(answers))
        scope = TargetScope.from_origin("http://lab.internal.test:8080")
        return ScopedHttpClient(scope, resolver=resolver), resolver

    def test_non_loopback_resolution_is_blocked_before_connecting(self) -> None:
        for addresses in (["169.254.169.254"], ["10.0.0.5"], ["127.0.0.1", "10.0.0.5"], []):
            client, _ = self.client(addresses)
            with (
                self.subTest(addresses=addresses),
                mock.patch("accessguard.adapters.http_target.send") as send,
                self.assertRaises(ScopeViolationError),
            ):
                client.get("/documents/x", None)
            send.assert_not_called()

    def test_dns_rebinding_is_caught_on_every_request(self) -> None:
        client, resolver = self.client(["127.0.0.1"], ["169.254.169.254"])
        with (
            mock.patch("accessguard.adapters.http_target.send") as send,
            mock.patch("accessguard.adapters.http_target.decode_observation"),
        ):
            client.get("/first", None)
            with self.assertRaises(ScopeViolationError):
                client.get("/second", None)
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(send.call_count, 1)

    def test_connection_is_pinned_to_the_validated_address(self) -> None:
        client, _ = self.client(["127.0.0.2"])
        with (
            mock.patch("accessguard.adapters.http_target.send") as send,
            mock.patch("accessguard.adapters.http_target.decode_observation"),
        ):
            client.get("/documents/x", None)
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["address"], "127.0.0.2")
        self.assertEqual(kwargs["host_header"], "lab.internal.test:8080")
        self.assertEqual(kwargs["method"], "GET")

    def test_unsafe_paths_never_reach_the_network(self) -> None:
        client, resolver = self.client(["127.0.0.1"])
        for path in ("//169.254.169.254/x", "http://evil.example/", "/a/../../etc", "/a\r\nX: y"):
            with self.subTest(path=path), self.assertRaises(ScopeViolationError):
                client.get(path, None)
        resolver.assert_not_called()

    def test_only_get_is_ever_sent(self) -> None:
        self.assertFalse(hasattr(ScopedHttpClient, "post"))
        self.assertFalse(hasattr(ScopedHttpClient, "request"))


if __name__ == "__main__":
    unittest.main()
