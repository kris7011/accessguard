"""ML foundation: private features, versioned datasets, grouped evaluation, gate and rollback."""

import json
import tempfile
import unittest
from pathlib import Path

from accessguard.application.ranking import (
    FEATURE_NAMES,
    LearnedRankerPlanner,
    LinearRanker,
    check_features,
)
from accessguard.domain.checks import generate_checks
from accessguard.domain.errors import PlannerError
from accessguard_ml import training
from accessguard_ml.data import build_dataset, load_dataset, synthetic_examples, write_dataset
from accessguard_ml.registry import Registry, promotion_gate
from accessguard_ml.training import (
    Evaluation,
    TrainingConfig,
    leave_one_application_out,
    recall_at,
    train,
)
from tests.helpers import two_tenant_policy


class FeatureTests(unittest.TestCase):
    def test_features_are_numbers_from_engine_enumerations_only(self) -> None:
        policy = two_tenant_policy(with_collection=True)
        for check in generate_checks(policy):
            features = check_features(policy, check)
            self.assertEqual(tuple(features), FEATURE_NAMES)
            self.assertTrue(all(isinstance(v, float) for v in features.values()))

    def test_learned_ranker_only_reorders(self) -> None:
        policy = two_tenant_policy(with_collection=True)
        checks = generate_checks(policy)
        ranker = LinearRanker("t", {"kind_list": 5.0, "rel_cross_tenant": 2.0})
        plan = LearnedRankerPlanner(policy, ranker).plan(checks)
        self.assertEqual(sorted(c.id for c in plan.checks), sorted(c.id for c in checks))
        self.assertEqual(plan.checks[0].kind, "list")
        self.assertEqual((plan.info.kind, plan.info.model), ("learned-ranker", "ranker:t"))

    def test_incompatible_artifacts_are_refused(self) -> None:
        with self.assertRaises(PlannerError):
            LinearRanker("t", {"unknown_feature": 1.0})
        with self.assertRaises(PlannerError):
            LinearRanker("t", {}, feature_version="features-v0")
        with self.assertRaises(PlannerError):
            LinearRanker.from_artifact({"version": "t", "weights": {"bias": "x"}})


class DatasetTests(unittest.TestCase):
    def test_synthetic_dataset_is_versioned_by_content(self) -> None:
        first = build_dataset(synthetic_examples())
        second = build_dataset(list(reversed(synthetic_examples())))
        self.assertEqual(first.version, second.version)
        manifest = first.manifest()
        self.assertEqual(manifest["customer_applications"], 0)
        self.assertEqual(manifest["sources"], ["synthetic-answer-key"])
        self.assertEqual(
            manifest["applications"]["sampleapp:flawed"], {"examples": 30, "positives": 10}
        )
        self.assertEqual(manifest["applications"]["eval:library"]["positives"], 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_dataset(first, Path(tmp))
            self.assertEqual(load_dataset(path).version, first.version)
            rows = (path / "examples.jsonl").read_text("utf-8").splitlines()
            (path / "examples.jsonl").write_text("\n".join(rows[1:]), "utf-8")
            with self.assertRaises(ValueError):
                load_dataset(path)

    def test_lab_profiles_are_one_application(self) -> None:
        applications = {e.application for e in synthetic_examples()}
        self.assertEqual(sum(a.startswith("lab:") for a in applications), 1)

    def test_committed_dataset_matches_the_sources(self) -> None:
        committed = sorted(Path("ml/datasets").glob("ds-*"))
        self.assertTrue(committed, "a dataset version is committed")
        self.assertEqual(committed[-1].name, build_dataset(synthetic_examples()).version)


class TrainingTests(unittest.TestCase):
    def test_training_is_reproducible(self) -> None:
        examples = synthetic_examples()
        config = TrainingConfig(epochs=50)
        self.assertEqual(train(examples, config, "a").weights, train(examples, config, "a").weights)

    def test_evaluation_never_trains_on_the_held_out_application(self) -> None:
        dataset = build_dataset(synthetic_examples())
        seen: list[set[str]] = []
        original = training.train

        def spy(rows, config, version):  # type: ignore[no-untyped-def]
            seen.append({r.application for r in rows})
            held_out = version.removeprefix("loao-")
            assert held_out not in seen[-1]
            return original(rows, config, version)

        training.train = spy
        try:
            evaluation = leave_one_application_out(dataset, TrainingConfig(epochs=20))
        finally:
            training.train = original
        self.assertEqual(len(seen), len(dataset.applications))
        self.assertEqual(
            set(evaluation.mean),
            {"model", "policy_order", "list_first", "deny_first", "random_median"},
        )

    def test_recall_ignores_applications_without_positives(self) -> None:
        examples = [e for e in synthetic_examples() if e.application == "eval:library"]
        self.assertIsNone(recall_at(examples, list(range(len(examples))), 0.25))


def evaluation(model: float, baseline: float) -> Evaluation:
    per_app: dict[str, dict[str, dict[str, float | None]]] = {
        "a": {
            "model": {"recall@25%": model, "recall@50%": model},
            "list_first": {"recall@25%": baseline, "recall@50%": baseline},
        }
    }
    mean = {
        k: {"recall@25%": v["recall@25%"] or 0.0, "recall@50%": v["recall@50%"] or 0.0}
        for k, v in per_app["a"].items()
    }
    return Evaluation(per_app, mean)


class RegistryTests(unittest.TestCase):
    def manifest(self, customers: int) -> dict[str, object]:
        return {"feature_version": "features-v1", "customer_applications": customers}

    def test_gate_requires_customer_data_and_a_clear_gain(self) -> None:
        self.assertTrue(promotion_gate(evaluation(0.8, 0.5), self.manifest(6)).passed)
        no_data = promotion_gate(evaluation(0.8, 0.5), self.manifest(0))
        self.assertFalse(no_data.passed)
        self.assertIn("synthetic fixtures are not evidence", no_data.reasons[0])
        self.assertFalse(promotion_gate(evaluation(0.52, 0.5), self.manifest(6)).passed)

    def test_promote_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Registry(Path(tmp))

            def artifact(version: str, passed: bool) -> dict[str, object]:
                return {
                    "version": version,
                    "dataset": {"version": "ds-x"},
                    "created_at": "now",
                    "gate": {"passed": passed, "reasons": [] if passed else ["no"]},
                }

            registry.register(artifact("m1", True))
            registry.register(artifact("m2", True))
            registry.register(artifact("bad", False))
            with self.assertRaisesRegex(ValueError, "gate did not pass"):
                registry.promote("bad", reviewer="kris")
            with self.assertRaisesRegex(ValueError, "reviewer"):
                registry.promote("m1", reviewer=" ")
            registry.promote("m1", reviewer="kris")
            registry.promote("m2", reviewer="kris")
            self.assertEqual(registry.load()["models"]["m1"]["status"], "retired")
            self.assertEqual(registry.rollback(reviewer="kris"), "m1")
            self.assertEqual(registry.load()["active"], "m1")
            self.assertIsNone(registry.rollback(reviewer="kris"))
            self.assertIsNone(registry.load()["active"])

    def test_committed_registry_has_no_active_model(self) -> None:
        data = json.loads(Path("ml/registry/registry.json").read_text("utf-8"))
        self.assertIsNone(data["active"], "no model may be active without passing the gate")
        for entry in data["models"].values():
            self.assertFalse(entry["gate"]["passed"])


if __name__ == "__main__":
    unittest.main()
