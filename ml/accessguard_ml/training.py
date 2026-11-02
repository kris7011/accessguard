"""Seeded logistic regression, grouped evaluation and the simple baselines it must beat.

Evaluation is leave-one-application-out: for each application, a model is trained on all
other applications and ranks the held-out application's checks. Ranking quality is
``recall@k``: the share of that application's confirmed violations found among the first
``k`` percent of checks. This is an offline measure over verified labels; it does not count
the session probes and owner baselines a real scan also spends (docs/evaluation.md).
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from accessguard.application.ranking import FEATURE_NAMES, FEATURE_VERSION, LinearRanker
from accessguard_ml.data import Dataset, Example

TRAINING_CODE_VERSION = "train-v1"
BUDGETS = (0.25, 0.5)
RANDOM_SEEDS = tuple(range(1, 21))


@dataclass(frozen=True)
class TrainingConfig:
    epochs: int = 400
    learning_rate: float = 0.3
    l2: float = 0.01
    seed: int = 0
    """Only used to shuffle example order per epoch; full-batch gradients make it moot, but
    it is recorded so a future stochastic variant stays reproducible."""


def train(examples: Sequence[Example], config: TrainingConfig, version: str) -> LinearRanker:
    """Full-batch gradient descent on the logistic loss with L2 (bias not regularised)."""
    if not examples:
        raise ValueError("cannot train on an empty dataset")
    weights = dict.fromkeys(FEATURE_NAMES, 0.0)
    rows = [(e.features, e.label) for e in examples]
    random.Random(config.seed).shuffle(rows)  # noqa: S311 - reproducible ordering only
    positives = sum(label for _, label in rows)
    # Class weights keep rare violations from being ignored.
    weight_pos = len(rows) / (2 * max(positives, 1))
    weight_neg = len(rows) / (2 * max(len(rows) - positives, 1))
    for _ in range(config.epochs):
        gradient = dict.fromkeys(FEATURE_NAMES, 0.0)
        for features, label in rows:
            z = sum(weights[n] * features[n] for n in FEATURE_NAMES)
            prediction = 1.0 / (1.0 + math.exp(-max(min(z, 50.0), -50.0)))
            scale = (prediction - label) * (weight_pos if label else weight_neg)
            for name in FEATURE_NAMES:
                gradient[name] += scale * features[name]
        for name in FEATURE_NAMES:
            penalty = 0.0 if name == "bias" else config.l2 * weights[name]
            weights[name] -= config.learning_rate * (gradient[name] / len(rows) + penalty)
    return LinearRanker(version, weights, FEATURE_VERSION)


Ranking = Callable[[Sequence[Example]], list[int]]
"""Returns indexes of the examples in the order they would be checked."""


def policy_order(examples: Sequence[Example]) -> list[int]:
    return list(range(len(examples)))


def list_first(examples: Sequence[Example]) -> list[int]:
    return sorted(range(len(examples)), key=lambda i: (examples[i].check_kind != "list", i))


def deny_first(examples: Sequence[Example]) -> list[int]:
    return sorted(range(len(examples)), key=lambda i: (examples[i].features["expect_deny"] == 0, i))


def model_order(model: LinearRanker) -> Ranking:
    def rank(examples: Sequence[Example]) -> list[int]:
        return sorted(range(len(examples)), key=lambda i: (-model.score(examples[i].features), i))

    return rank


def recall_at(examples: Sequence[Example], order: list[int], budget: float) -> float | None:
    """Share of positives within the first ``budget`` of checks; ``None`` without positives."""
    positives = sum(e.label for e in examples)
    if positives == 0:
        return None
    k = max(1, math.floor(len(examples) * budget))
    return sum(examples[i].label for i in order[:k]) / positives


def random_recall(examples: Sequence[Example], budget: float) -> float | None:
    values = []
    for seed in RANDOM_SEEDS:
        order = list(range(len(examples)))
        random.Random(seed).shuffle(order)  # noqa: S311
        value = recall_at(examples, order, budget)
        if value is None:
            return None
        values.append(value)
    return statistics.median(values)


@dataclass(frozen=True)
class Evaluation:
    per_application: dict[str, dict[str, dict[str, float | None]]]
    """application -> strategy -> ``recall@25%`` / ``recall@50%``."""
    mean: dict[str, dict[str, float]]
    """strategy -> budget label -> mean recall over applications with positives."""

    def best_baseline(self, budget: str) -> tuple[str, float]:
        baselines = {k: v[budget] for k, v in self.mean.items() if k != "model"}
        name = max(baselines, key=lambda k: baselines[k])
        return name, baselines[name]


def _label(budget: float) -> str:
    return f"recall@{int(budget * 100)}%"


def leave_one_application_out(dataset: Dataset, config: TrainingConfig) -> Evaluation:
    per_app: dict[str, dict[str, dict[str, float | None]]] = {}
    for held_out in dataset.applications:
        train_rows = [e for e in dataset.examples if e.application != held_out]
        test_rows = [e for e in dataset.examples if e.application == held_out]
        model = train(train_rows, config, f"loao-{held_out}")
        strategies: dict[str, Ranking] = {
            "model": model_order(model),
            "policy_order": policy_order,
            "list_first": list_first,
            "deny_first": deny_first,
        }
        results: dict[str, dict[str, float | None]] = {}
        for name, rank in strategies.items():
            order = rank(test_rows)
            results[name] = {_label(b): recall_at(test_rows, order, b) for b in BUDGETS}
        results["random_median"] = {_label(b): random_recall(test_rows, b) for b in BUDGETS}
        per_app[held_out] = results
    mean: dict[str, dict[str, float]] = {}
    for strategy in ("model", "policy_order", "list_first", "deny_first", "random_median"):
        mean[strategy] = {}
        for budget in BUDGETS:
            values = [
                v for app in per_app.values() if (v := app[strategy][_label(budget)]) is not None
            ]
            mean[strategy][_label(budget)] = round(statistics.fmean(values), 4) if values else 0.0
    return Evaluation(per_app, mean)
