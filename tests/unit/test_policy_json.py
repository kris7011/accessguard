import copy
import json
import unittest
from typing import Any

from accessguard.adapters.policy_json import (
    MAX_POLICY_BYTES,
    load_policy_bytes,
    load_policy_file,
    parse_policy,
)
from accessguard.domain.errors import PolicyError
from tests.helpers import LAB_POLICY_PATH


def lab_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(LAB_POLICY_PATH.read_text("utf-8"))
    return document


class PolicyJsonTests(unittest.TestCase):
    def test_lab_policy_loads(self) -> None:
        loaded = load_policy_file(LAB_POLICY_PATH)
        self.assertEqual(loaded.policy.id, "accessguard-lab")
        self.assertEqual(len(loaded.policy.identities), 6)
        self.assertEqual(len(loaded.policy.resources), 7)
        self.assertEqual(len(loaded.sha256), 64)

    def test_hash_identifies_exact_bytes(self) -> None:
        data = LAB_POLICY_PATH.read_bytes()
        self.assertEqual(load_policy_bytes(data).sha256, load_policy_bytes(data).sha256)
        self.assertNotEqual(load_policy_bytes(data).sha256, load_policy_bytes(data + b" ").sha256)

    def test_unknown_keys_are_rejected(self) -> None:
        document = lab_document()
        document["resources"][0]["grant"] = []
        with self.assertRaisesRegex(PolicyError, "unknown keys: grant"):
            parse_policy(document)

    def test_credentials_cannot_be_embedded(self) -> None:
        document = lab_document()
        document["identities"][0]["token"] = "secret-value"
        with self.assertRaisesRegex(PolicyError, "unknown keys: token"):
            parse_policy(document)

    def test_missing_required_keys(self) -> None:
        document = lab_document()
        del document["resources"][0]["owner"]
        with self.assertRaisesRegex(PolicyError, r"resources\[0\] is missing required keys: owner"):
            parse_policy(document)

    def test_wrong_types(self) -> None:
        cases: list[tuple[str, Any]] = [
            ("identities", {}),
            ("id", 42),
            ("session_probe", []),
        ]
        for key, value in cases:
            document = lab_document()
            document[key] = value
            with self.subTest(key=key), self.assertRaises(PolicyError):
                parse_policy(document)

    def test_duplicate_roles_are_rejected(self) -> None:
        document = lab_document()
        document["resources"][0]["grants"][0]["roles"] = ["admin", "admin"]
        with self.assertRaisesRegex(PolicyError, "duplicates"):
            parse_policy(document)

    def test_unsupported_schema_version(self) -> None:
        document = lab_document()
        document["schema_version"] = "2"
        with self.assertRaisesRegex(PolicyError, "schema_version"):
            parse_policy(document)

    def test_malformed_and_oversized_input(self) -> None:
        for data in (b"{", b"\xff\xfe", b"[]", b"null"):
            with self.subTest(data=data), self.assertRaises(PolicyError):
                load_policy_bytes(data)
        with self.assertRaisesRegex(PolicyError, "exceeds"):
            load_policy_bytes(b" " * (MAX_POLICY_BYTES + 1))

    def test_domain_invariants_apply_to_parsed_policies(self) -> None:
        document = copy.deepcopy(lab_document())
        document["resources"][0]["owner"] = "globex-member"
        with self.assertRaisesRegex(PolicyError, "must be granted"):
            parse_policy(document)


if __name__ == "__main__":
    unittest.main()
