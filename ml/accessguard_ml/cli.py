"""ML command line: build datasets, train and evaluate candidates, promote, roll back.

python -m accessguard_ml build-dataset [--export customer.jsonl] --out ml/datasets
python -m accessguard_ml train --dataset ml/datasets/ds-... --registry ml/registry
python -m accessguard_ml status --registry ml/registry
python -m accessguard_ml promote VERSION --reviewer NAME --registry ml/registry
python -m accessguard_ml rollback --reviewer NAME --registry ml/registry
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from accessguard_ml.data import (
    build_dataset,
    load_dataset,
    read_export,
    synthetic_examples,
    write_dataset,
)
from accessguard_ml.registry import Registry, promotion_gate
from accessguard_ml.training import (
    TRAINING_CODE_VERSION,
    TrainingConfig,
    leave_one_application_out,
    train,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="accessguard-ml",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-dataset")
    build.add_argument("--export", type=Path, action="append", default=[])
    build.add_argument("--out", type=Path, required=True)
    trainer = commands.add_parser("train")
    trainer.add_argument("--dataset", type=Path, required=True)
    trainer.add_argument("--registry", type=Path, required=True)
    trainer.add_argument("--seed", type=int, default=0)
    status = commands.add_parser("status")
    status.add_argument("--registry", type=Path, required=True)
    promote = commands.add_parser("promote")
    promote.add_argument("version")
    promote.add_argument("--reviewer", required=True)
    promote.add_argument("--registry", type=Path, required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--reviewer", required=True)
    rollback.add_argument("--registry", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "build-dataset":
        examples = synthetic_examples()
        for export in args.export:
            examples += read_export(export)
        dataset = build_dataset(examples)
        path = write_dataset(dataset, args.out)
        print(json.dumps(dataset.manifest(), indent=2))
        print(f"dataset written to {path}")
        return 0
    registry = Registry(args.registry)
    if args.command == "train":
        dataset = load_dataset(args.dataset)
        config = TrainingConfig(seed=args.seed)
        evaluation = leave_one_application_out(dataset, config)
        manifest = dataset.manifest()
        gate = promotion_gate(evaluation, manifest)
        version = f"ranker-{dataset.version.removeprefix('ds-')[:8]}-s{args.seed}"
        model = train(dataset.examples, config, version)
        registry.register(
            {
                "version": version,
                "feature_version": model.feature_version,
                "weights": dict(model.weights),
                "training": {"code": TRAINING_CODE_VERSION, **asdict(config)},
                "dataset": {"version": dataset.version, "manifest": manifest},
                "evaluation": {
                    "method": "leave-one-application-out",
                    "per_application": evaluation.per_application,
                    "mean": evaluation.mean,
                },
                "gate": {"passed": gate.passed, "reasons": gate.reasons},
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        print(
            json.dumps(
                {
                    "version": version,
                    "mean": evaluation.mean,
                    "gate": {"passed": gate.passed, "reasons": gate.reasons},
                },
                indent=2,
            )
        )
        return 0
    if args.command == "status":
        print(json.dumps(registry.load(), indent=2))
        return 0
    try:
        if args.command == "promote":
            registry.promote(args.version, reviewer=args.reviewer)
            print(f"{args.version} is now active")
        else:
            restored = registry.rollback(reviewer=args.reviewer)
            print(f"rolled back; active model: {restored or 'none'}")
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    return 0
