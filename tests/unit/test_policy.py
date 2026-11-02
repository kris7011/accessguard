import unittest
from dataclasses import replace

from accessguard.domain.errors import PolicyError
from accessguard.domain.policy import ANONYMOUS, Collection, Grant, Identity, Resource
from tests.helpers import two_tenant_policy


class PolicyInvariantTests(unittest.TestCase):
    def test_valid_policy_exposes_lookups(self) -> None:
        policy = two_tenant_policy(with_collection=True)
        self.assertEqual(policy.identity("acme-member").tenant, "acme")
        self.assertEqual(policy.owner_tenant(policy.resource("shared")), "acme")
        self.assertEqual(len(policy.resources_in("docs")), 3)

    def test_anonymous_is_never_allowed(self) -> None:
        policy = two_tenant_policy()
        self.assertFalse(policy.is_allowed(ANONYMOUS, policy.resource("acme-doc")))

    def test_grants_match_tenant_and_role(self) -> None:
        policy = two_tenant_policy()
        self.assertTrue(policy.is_allowed("acme-member", policy.resource("acme-doc")))
        self.assertFalse(policy.is_allowed("globex-member", policy.resource("acme-doc")))
        self.assertTrue(policy.is_allowed("globex-member", policy.resource("shared")))

    def test_same_tenant_different_role_is_not_implied(self) -> None:
        grant = Grant("acme", frozenset({"admin"}))
        self.assertTrue(grant.permits(Identity("a", "acme", "admin")))
        self.assertFalse(grant.permits(Identity("b", "acme", "member")))

    def test_owner_must_be_granted(self) -> None:
        policy = two_tenant_policy()
        bad = replace(policy.resource("acme-doc"), owner="globex-member")
        with self.assertRaisesRegex(PolicyError, "must be granted"):
            replace(policy, resources=(bad,))

    def test_owner_must_exist(self) -> None:
        policy = two_tenant_policy()
        bad = replace(policy.resource("acme-doc"), owner="ghost")
        with self.assertRaisesRegex(PolicyError, "not a declared identity"):
            replace(policy, resources=(bad,))

    def test_duplicate_ids_are_rejected(self) -> None:
        policy = two_tenant_policy()
        with self.assertRaisesRegex(PolicyError, "Duplicate identity"):
            replace(policy, identities=(*policy.identities, policy.identities[0]))
        with self.assertRaisesRegex(PolicyError, "Duplicate resource"):
            replace(policy, resources=(*policy.resources, policy.resources[0]))

    def test_unknown_collection_is_rejected(self) -> None:
        policy = two_tenant_policy()
        bad = replace(policy.resource("acme-doc"), collection="missing")
        with self.assertRaisesRegex(PolicyError, "unknown collection"):
            replace(policy, resources=(bad,))

    def test_reserved_and_malformed_identifiers(self) -> None:
        for value in (ANONYMOUS, "Upper", "", "a b", "x" * 65):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                Identity(value, "acme", "member")

    def test_unsafe_paths_are_rejected(self) -> None:
        for path in (
            "relative",
            "//evil.example/x",
            "/a/../b",
            "/a?x=1",
            "/a#f",
            "/a b",
            "http://evil.example/",
            "/a\\b",
        ):
            with self.subTest(path=path), self.assertRaises(PolicyError):
                Collection("c", path)

    def test_resource_requires_fields_and_grants(self) -> None:
        grant = (Grant("acme", frozenset({"admin"})),)
        with self.assertRaises(PolicyError):
            Resource("r", "doc", "/r", "o", (), grant)
        with self.assertRaises(PolicyError):
            Resource("r", "doc", "/r", "o", ("title",), ())
        with self.assertRaises(PolicyError):
            Resource("r", "doc", "/r", "o", ("title", "title"), grant)
        with self.assertRaises(PolicyError):
            Grant("acme", frozenset())

    def test_rule_description_names_all_grants(self) -> None:
        policy = two_tenant_policy()
        rule = policy.describe_rule(policy.resource("shared"))
        self.assertIn("tenant acme (roles: admin, member)", rule)
        self.assertIn("tenant globex (roles: member)", rule)


if __name__ == "__main__":
    unittest.main()
