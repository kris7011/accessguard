import unittest

from accessguard.domain.errors import ScopeViolationError
from accessguard.domain.scope import TargetScope, is_safe_request_path


class TargetScopeTests(unittest.TestCase):
    def test_parses_loopback_origin(self) -> None:
        scope = TargetScope.from_origin("http://127.0.0.1:8080")
        self.assertEqual((scope.host, scope.port), ("127.0.0.1", 8080))
        self.assertEqual(scope.origin, "http://127.0.0.1:8080")
        self.assertEqual(TargetScope.from_origin("http://[::1]:9/").host_header, "[::1]:9")

    def test_rejects_unsupported_origins(self) -> None:
        for origin in (
            "https://127.0.0.1:443",
            "ftp://127.0.0.1:21",
            "http://user:pass@127.0.0.1:80",  # pragma: allowlist secret
            "http://127.0.0.1:80/path",
            "http://127.0.0.1:80?x=1",
            "http://127.0.0.1",
            "http://127.0.0.1:99999",
            "127.0.0.1:80",
        ):
            with self.subTest(origin=origin), self.assertRaises(ScopeViolationError):
                TargetScope.from_origin(origin)

    def test_only_loopback_destinations_are_allowed(self) -> None:
        scope = TargetScope.from_origin("http://localhost:8080")
        for address in ("127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"):
            with self.subTest(address=address):
                scope.check_destination(address)
        for address in (
            "169.254.169.254",  # cloud metadata
            "::ffff:169.254.169.254",
            "fd00:ec2::254",
            "10.0.0.1",
            "192.168.1.1",
            "0.0.0.0",  # noqa: S104 - asserting that it is rejected
            "8.8.8.8",
            "not-an-ip",
        ):
            with self.subTest(address=address), self.assertRaises(ScopeViolationError):
                scope.check_destination(address)


class RequestPathTests(unittest.TestCase):
    def test_safe_paths(self) -> None:
        for path in ("/", "/documents/acme-doc", "/a/b.c/~d", "/x%20y"):
            with self.subTest(path=path):
                self.assertTrue(is_safe_request_path(path))

    def test_unsafe_paths(self) -> None:
        for path in (
            "",
            "documents",
            "//evil.example/x",
            "/a/../b",
            "/a/./b",
            "/..",
            "/a?b",
            "/a#b",
            "/a b",
            "/a\r\nHost: evil",
            "/a\\b",
            "/" + "a" * 2048,
        ):
            with self.subTest(path=path[:30]):
                self.assertFalse(is_safe_request_path(path))


if __name__ == "__main__":
    unittest.main()
