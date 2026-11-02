"""Network zones, https origins and allowed path prefixes of authorized scopes."""

import unittest

from accessguard.domain.errors import ScopeViolationError
from accessguard.domain.scope import NetworkZone, TargetScope

ALWAYS_BLOCKED = (
    "169.254.169.254",  # cloud metadata (link-local)
    "::ffff:169.254.169.254",
    "fe80::1",
    "fd00:ec2::254",  # AWS IPv6 metadata
    "100.100.100.200",  # Alibaba metadata
    "0.0.0.0",  # noqa: S104 - asserting that it is rejected
    "0.1.2.3",
    "224.0.0.1",
    "240.0.0.1",
    "not-an-ip",
)


class ZoneTests(unittest.TestCase):
    def scope(self, zone: NetworkZone, origin: str = "https://staging.example.test") -> TargetScope:
        return TargetScope.authorized(origin, zone=zone)

    def test_blocked_addresses_are_rejected_in_every_zone(self) -> None:
        for zone in NetworkZone:
            scope = self.scope(zone)
            for address in ALWAYS_BLOCKED:
                with (
                    self.subTest(zone=zone, address=address),
                    self.assertRaises(ScopeViolationError),
                ):
                    scope.check_destination(address)

    def test_public_zone_accepts_only_globally_routable_addresses(self) -> None:
        scope = self.scope(NetworkZone.PUBLIC)
        for address in ("93.184.215.14", "2606:4700::6810:84e5"):
            with self.subTest(address=address):
                scope.check_destination(address)
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "100.64.0.1", "::1"):
            with self.subTest(address=address), self.assertRaises(ScopeViolationError):
                scope.check_destination(address)

    def test_private_zone_accepts_internal_but_not_public_addresses(self) -> None:
        scope = self.scope(NetworkZone.PRIVATE)
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "fd12::1", "::1"):
            with self.subTest(address=address):
                scope.check_destination(address)
        with self.assertRaises(ScopeViolationError):
            scope.check_destination("93.184.215.14")

    def test_loopback_zone_accepts_only_loopback(self) -> None:
        scope = self.scope(NetworkZone.LOOPBACK, "http://127.0.0.1:8800")
        scope.check_destination("127.0.0.1")
        scope.check_destination("::1")
        for address in ("10.0.0.5", "93.184.215.14"):
            with self.subTest(address=address), self.assertRaises(ScopeViolationError):
                scope.check_destination(address)


class AuthorizedOriginTests(unittest.TestCase):
    def test_https_origin_defaults_to_port_443(self) -> None:
        scope = TargetScope.authorized("https://Staging.Example.test", zone=NetworkZone.PUBLIC)
        self.assertEqual(
            (scope.scheme, scope.host, scope.port), ("https", "staging.example.test", 443)
        )
        self.assertEqual(scope.origin, "https://staging.example.test")
        self.assertTrue(scope.uses_tls)
        custom = TargetScope.authorized(
            "https://staging.example.test:8443", zone=NetworkZone.PUBLIC
        )
        self.assertEqual(custom.host_header, "staging.example.test:8443")

    def test_plain_http_is_refused_for_public_targets(self) -> None:
        with self.assertRaises(ScopeViolationError):
            TargetScope.authorized("http://staging.example.test", zone=NetworkZone.PUBLIC)
        TargetScope.authorized("http://10.0.0.5:8080", zone=NetworkZone.PRIVATE)

    def test_invalid_origins_are_rejected(self) -> None:
        for origin in (
            "ftp://example.test",
            "https://user:pw@example.test",  # pragma: allowlist secret
            "https://example.test/api",
            "https://example.test?x=1",
            "https://:443",
        ):
            with self.subTest(origin=origin), self.assertRaises(ScopeViolationError):
                TargetScope.authorized(origin, zone=NetworkZone.PUBLIC)

    def test_lab_rule_still_rejects_https(self) -> None:
        with self.assertRaises(ScopeViolationError):
            TargetScope.from_origin("https://127.0.0.1:443")


class PathPrefixTests(unittest.TestCase):
    def test_paths_must_lie_under_an_allowed_prefix(self) -> None:
        scope = TargetScope.authorized(
            "https://api.example.test",
            zone=NetworkZone.PUBLIC,
            path_prefixes=("/api/v1/", "/health"),
        )
        self.assertEqual(scope.path_prefixes, ("/api/v1", "/health"))
        for path in ("/api/v1", "/api/v1/projects/p1", "/health"):
            with self.subTest(path=path):
                self.assertTrue(scope.allows_path(path))
        for path in ("/api/v10/x", "/admin", "/api/v1/../admin", "/healthz", "/api/v1?x"):
            with self.subTest(path=path):
                self.assertFalse(scope.allows_path(path))
        self.assertIn("paths under: /api/v1, /health", scope.constraints())

    def test_invalid_prefixes_are_rejected(self) -> None:
        for prefixes in ((), ("api",), ("/a/../b",), ("//evil",)):
            with self.subTest(prefixes=prefixes), self.assertRaises(ScopeViolationError):
                TargetScope.authorized(
                    "https://api.example.test", zone=NetworkZone.PUBLIC, path_prefixes=prefixes
                )


if __name__ == "__main__":
    unittest.main()
