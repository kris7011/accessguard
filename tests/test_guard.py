import unittest
from unittest.mock import patch
from guard import CASES, ai_plan, demo, fixture, http_json, validate_plan, verdict


class GuardTests(unittest.TestCase):
    def test_real_http_before_after(self):
        report = demo()
        self.assertEqual({r['id'] for r in report['runs']['vulnerable'] if r['result'] == 'FAIL'}, {'a-cross', 'b-cross'})
        self.assertTrue(all(r['result'] == 'PASS' for r in report['runs']['fixed']))

    def test_unknown_account_denied(self):
        with fixture(True) as base:
            self.assertEqual(http_json(base + '/documents/101', 'invalid')[0], 401)

    def test_missing_document(self):
        with fixture(True) as base:
            self.assertEqual(http_json(base + '/documents/missing', 'demo-a')[0], 404)

    def test_baseline_failure_is_not_pass(self):
        self.assertEqual(verdict(False, (401, {}), (403, {})), 'INCONCLUSIVE')

    def test_server_error_is_not_pass(self):
        self.assertEqual(verdict(False, (200, {'marker': 'x'}), (500, {})), 'INCONCLUSIVE')

    def test_login_page_is_not_leak(self):
        self.assertEqual(verdict(False, (200, {'marker': 'x'}), (200, {'login': True})), 'INCONCLUSIVE')

    def test_positive_access_failure(self):
        self.assertEqual(verdict(True, (200, {'marker': 'x'}), (403, {})), 'INCONCLUSIVE')

    def test_planner_cannot_drop_duplicate_or_add_tests(self):
        ids = [c.id for c in CASES]
        for invalid in (ids[:-1], ids + ['evil'], ids[:-1] + [ids[0]], 'invalid', [None]):
            with self.assertRaises(ValueError):
                validate_plan(invalid)

    def test_optional_ai_adapter_contract(self):
        import json
        ids = [c.id for c in reversed(CASES)]
        with patch('guard.http_json', return_value=(200, {'message': {'content': json.dumps({'order': ids})}})):
            self.assertEqual([c.id for c in ai_plan('test-model')], ids)

    def test_ai_errors_are_not_hidden(self):
        with patch('guard.http_json', return_value=(500, {})):
            with self.assertRaises(ValueError):
                ai_plan('test-model')


if __name__ == '__main__':
    unittest.main()
