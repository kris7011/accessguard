import unittest

from accessguard.domain.checks import (
    Expectation,
    ItemCheck,
    ListCheck,
    Relation,
    generate_checks,
    order_checks,
)
from accessguard.domain.errors import PlanValidationError
from tests.helpers import two_tenant_policy


class CheckGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = two_tenant_policy(with_collection=True)
        self.checks = {check.id: check for check in generate_checks(self.policy)}

    def test_full_matrix_including_anonymous(self) -> None:
        # 3 resources x (3 identities + anonymous) + 1 collection x 4 actors
        self.assertEqual(len(self.checks), 16)

    def test_generation_is_deterministic(self) -> None:
        first = [c.id for c in generate_checks(self.policy)]
        second = [c.id for c in generate_checks(self.policy)]
        self.assertEqual(first, second)

    def test_expectations_follow_grants(self) -> None:
        expected = {
            "acme-doc@acme-member": (Expectation.ALLOW, Relation.OWNER),
            "acme-doc@acme-admin": (Expectation.ALLOW, Relation.SAME_TENANT),
            "acme-doc@globex-member": (Expectation.DENY, Relation.CROSS_TENANT),
            "acme-doc@anonymous": (Expectation.DENY, Relation.ANONYMOUS),
            "shared@globex-member": (Expectation.ALLOW, Relation.SHARED_GRANT),
        }
        for check_id, (expectation, relation) in expected.items():
            check = self.checks[check_id]
            assert isinstance(check, ItemCheck)
            with self.subTest(check=check_id):
                self.assertEqual(check.expectation, expectation)
                self.assertEqual(check.relation, relation)

    def test_list_checks_split_visible_and_hidden(self) -> None:
        check = self.checks["list:docs@globex-member"]
        assert isinstance(check, ListCheck)
        self.assertEqual(set(check.visible), {"globex-doc", "shared"})
        self.assertEqual(check.hidden, ("acme-doc",))
        anonymous = self.checks["list:docs@anonymous"]
        self.assertEqual(anonymous.expectation, Expectation.DENY)


class PlanOrderTests(unittest.TestCase):
    """Ported from the v0.1 test ``test_planner_cannot_drop_duplicate_or_add_tests``."""

    def test_valid_permutation_is_applied(self) -> None:
        checks = generate_checks(two_tenant_policy())
        ids = [c.id for c in reversed(checks)]
        self.assertEqual([c.id for c in order_checks(checks, ids)], ids)

    def test_planner_cannot_drop_duplicate_add_or_alter_checks(self) -> None:
        checks = generate_checks(two_tenant_policy())
        ids = [c.id for c in checks]
        invalid: list[object] = [
            ids[:-1],
            [*ids, "evil@anonymous"],
            [*ids[:-1], ids[0]],
            "not-a-list",
            [None],
            [{"id": ids[0]}],
            [],
        ]
        for proposal in invalid:
            with self.subTest(proposal=str(proposal)[:40]), self.assertRaises(PlanValidationError):
                order_checks(checks, proposal)


if __name__ == "__main__":
    unittest.main()
