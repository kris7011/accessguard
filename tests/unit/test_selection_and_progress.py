"""Identity subjects, nested fields, check selection for retests and progress snapshots."""

import unittest
from datetime import UTC, datetime

from accessguard.adapters.policy_json import parse_policy
from accessguard.application.gate import RunLimits
from accessguard.application.planning import DeterministicPlanner
from accessguard.application.runner import Progress
from accessguard.application.scan import run_scan, select_checks
from accessguard.domain.errors import PolicyError
from accessguard.domain.policy import Identity, lookup_field
from accessguard.domain.scope import TargetScope
from tests.helpers import (
    TOKENS,
    ScriptedClient,
    correct_routes,
    credentials,
    flawed_routes,
    observation,
    two_tenant_policy,
)


def scan(client: ScriptedClient, **kwargs: object) -> dict[str, object]:
    return run_scan(
        policy=two_tenant_policy(with_collection=True),
        policy_sha256="0" * 64,
        client=client,
        credentials=credentials(),
        scope=TargetScope.from_origin("http://127.0.0.1:9"),
        target_label="test",
        planner=DeterministicPlanner(),
        limits=RunLimits(),
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        **kwargs,  # type: ignore[arg-type]
    )


class SubjectAndFieldPathTests(unittest.TestCase):
    def test_lookup_follows_nested_objects(self) -> None:
        document = {"data": {"user": {"email": "ana@orbit.test"}}, "x": 1}
        self.assertEqual(lookup_field(document, "data.user.email"), "ana@orbit.test")
        self.assertIsNone(lookup_field(document, "data.missing"))
        self.assertIsNone(lookup_field(document, "x.y"))
        self.assertIsNone(lookup_field([1, 2], "data"))

    def test_subject_defaults_to_the_identity_id(self) -> None:
        self.assertEqual(Identity("ana", "orbit", "owner").expected_subject, "ana")
        self.assertEqual(
            Identity("ana", "orbit", "owner", subject="ana@orbit.test").expected_subject,
            "ana@orbit.test",
        )
        for subject in ("", "a\nb", "x" * 257):
            with self.subTest(subject=subject[:5]), self.assertRaises(PolicyError):
                Identity("ana", "orbit", "owner", subject=subject)

    def test_policy_json_accepts_subjects_and_dotted_fields(self) -> None:
        policy = parse_policy(
            {
                "schema_version": "1",
                "id": "p",
                "session_probe": {"path": "/api/me", "subject_field": "data.email"},
                "identities": [
                    {"id": "ana", "tenant": "orbit", "role": "owner", "subject": "ana@orbit.test"}
                ],
                "resources": [
                    {
                        "id": "r1",
                        "category": "project",
                        "path": "/api/projects/r1",
                        "owner": "ana",
                        "protected_fields": ["data.name", "data.budget_code"],
                        "grants": [{"tenant": "orbit", "roles": ["owner"]}],
                    }
                ],
            }
        )
        self.assertEqual(policy.session_probe.subject_field, "data.email")
        self.assertEqual(policy.identity("ana").subject, "ana@orbit.test")
        self.assertEqual(policy.resource("r1").protected_fields, ("data.name", "data.budget_code"))

    def test_session_probe_compares_the_configured_subject(self) -> None:
        routes = correct_routes()
        # The probe now answers with a nested e-mail instead of the identity id.
        routes[("/me", TOKENS["acme-admin"])] = observation(200, {"data": {"email": "x@y"}})
        report = scan(ScriptedClient(routes))
        sessions = report["sessions"]
        assert isinstance(sessions, dict)
        self.assertEqual(sessions["acme-admin"]["state"], "invalid")
        self.assertEqual(sessions["acme-member"]["state"], "valid")


class SelectionTests(unittest.TestCase):
    def test_unknown_or_empty_selections_are_configuration_errors(self) -> None:
        policy = two_tenant_policy()
        with self.assertRaises(PolicyError):
            select_checks(policy, ["acme-doc@nobody"])
        with self.assertRaises(PolicyError):
            select_checks(policy, [])
        checks, full = select_checks(policy, ["globex-doc@acme-admin", "acme-doc@globex-member"])
        self.assertEqual(
            [c.id for c in checks], ["acme-doc@globex-member", "globex-doc@acme-admin"]
        )
        self.assertEqual(full, 12)

    def test_subset_report_says_it_is_a_subset(self) -> None:
        report = scan(ScriptedClient(flawed_routes()), only_checks=["acme-doc@globex-member"])
        run = report["run"]
        status = report["status"]
        assert isinstance(run, dict) and isinstance(status, dict)
        self.assertEqual(run["selection"]["mode"], "subset")
        self.assertEqual(run["selection"]["selected_checks"], 1)
        self.assertEqual(report["coverage"]["planned_checks"], 1)  # type: ignore[index]
        self.assertEqual(report["findings"][0]["verification"], "reproduced")  # type: ignore[index]
        self.assertIn("Only 1 of", status["meaning"])

    def test_passing_retest_of_a_fixed_check_is_marked_as_a_subset(self) -> None:
        report = scan(ScriptedClient(correct_routes()), only_checks=["acme-doc@globex-member"])
        self.assertEqual(report["status"]["overall"], "checks_passed")  # type: ignore[index]
        self.assertIn("says nothing about the checks that were not selected",
                      report["status"]["meaning"])  # type: ignore[index]  # fmt: skip

    def test_full_run_reports_mode_all(self) -> None:
        report = scan(ScriptedClient(correct_routes()))
        self.assertEqual(report["run"]["selection"]["mode"], "all")  # type: ignore[index]


class ProgressTests(unittest.TestCase):
    def test_progress_reaches_done_with_retests(self) -> None:
        events: list[Progress] = []
        report = scan(ScriptedClient(flawed_routes()), on_progress=events.append)
        planned = report["coverage"]["planned_checks"]  # type: ignore[index]
        self.assertEqual(events[0], Progress("checks", 0, planned, 0))
        checks = [e for e in events if e.phase == "checks"]
        self.assertEqual([e.completed for e in checks], list(range(planned + 1)))
        self.assertTrue(any(e.phase == "retests" for e in events))
        self.assertEqual(events[-1].phase, "done")
        self.assertEqual(events[-1].requests_used, report["run"]["requests_used"])  # type: ignore[index]
        used = [e.requests_used for e in events]
        self.assertEqual(used, sorted(used))


if __name__ == "__main__":
    unittest.main()
