import unittest

from accessguard.domain.credentials import Secret
from accessguard.domain.redaction import REDACTED, scrub, scrub_text


class RedactionTests(unittest.TestCase):
    def test_known_secrets_are_removed(self) -> None:
        text = scrub_text("token=abc123secretvalue; again abc123secretvalue", ["abc123secretvalue"])
        self.assertNotIn("abc123secretvalue", text)
        self.assertEqual(text.count(REDACTED), 2)

    def test_longer_secret_wins_over_its_prefix(self) -> None:
        text = scrub_text("abcdefgh-long", ["abcdefgh", "abcdefgh-long"])
        self.assertEqual(text, REDACTED)

    def test_auth_scheme_values_are_removed_even_if_unknown(self) -> None:
        for header in (
            "Bearer eyJhbGciOiJIUzI1NiJ9.payload",
            "basic dXNlcjpwYXNzd29yZA==",
            "Token abcdefgh12345678",
        ):
            with self.subTest(header=header):
                self.assertTrue(scrub_text(f"Authorization: {header}", []).endswith(REDACTED))

    def test_ordinary_prose_is_preserved(self) -> None:
        text = "Basic information about token handling"
        self.assertEqual(scrub_text(text, []), text)

    def test_nested_structures_including_keys(self) -> None:
        data = {"a": ["x", {"s3cr3t-value": "s3cr3t-value"}], "n": 1, "b": None}
        self.assertEqual(
            scrub(data, ["s3cr3t-value"]), {"a": ["x", {REDACTED: REDACTED}], "n": 1, "b": None}
        )

    def test_secret_never_renders(self) -> None:
        secret = Secret("super-secret-token")
        self.assertNotIn("super-secret-token", repr(secret))
        self.assertNotIn("super-secret-token", str(secret))
        self.assertNotIn("super-secret-token", f"{[secret]}")
        self.assertEqual(secret.reveal(), "super-secret-token")
        with self.assertRaises(ValueError):
            Secret("")


if __name__ == "__main__":
    unittest.main()
