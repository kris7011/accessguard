"""Labelled examples with provenance, and versioned datasets.

An example is one check of one application: its ``features-v1`` vector and a verified label
(1 = a confirmed access-control violation, 0 = the check passed or a person rejected it).
Labels come only from hand-written answer keys (synthetic applications) or from consented,
de-identified customer exports (people's triage decisions and conclusive engine results);
never from a model's own claims.

A dataset version is the SHA-256 of its canonical rows, so the same examples always give
the same version id and any change gives a new one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from accessguard.adapters.policy_json import parse_policy
from accessguard.adapters.scan_spec import canonical_json
from accessguard.application.ranking import FEATURE_NAMES, FEATURE_VERSION, check_features
from accessguard.domain.checks import Check, generate_checks
from accessguard.domain.policy import AccessPolicy

DATASET_SCHEMA = "accessguard-ml-dataset-v1"
SYNTHETIC = "synthetic-answer-key"
CUSTOMER = "customer-consented-export"


@dataclass(frozen=True)
class Example:
    application: str
    """Grouping key: every example of one application stays in the same split."""
    check_kind: str
    features: dict[str, float]
    label: int
    provenance: dict[str, str] = field(default_factory=dict)


def examples_for_policy(
    policy: AccessPolicy, positives: Iterable[str], *, application: str, provenance: dict[str, str]
) -> list[Example]:
    wanted = set(positives)
    checks: tuple[Check, ...] = generate_checks(policy)
    unknown = wanted - {c.id for c in checks}
    if unknown:
        raise ValueError(f"{application}: answer key names unknown checks {sorted(unknown)[:3]}")
    return [
        Example(
            application=application,
            check_kind=check.kind,
            features=check_features(policy, check),
            label=int(check.id in wanted),
            provenance=provenance,
        )
        for check in checks
    ]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def synthetic_examples() -> list[Example]:
    """Every synthetic application with a hand-written answer key, one group each.

    The six lab flaw profiles share one policy and one code base, so the lab counts as one
    application (its combined ``vulnerable`` profile): near-identical fixtures never land
    in both training and evaluation.
    """
    from accessguard_eval.fixtures import fixture_ids, load_answer_key, load_fixture  # noqa: PLC0415
    from accessguard_lab import ground_truth  # noqa: PLC0415
    from accessguard_lab.profiles import PROFILES  # noqa: PLC0415
    from accessguard_lab.scan import load_lab_policy  # noqa: PLC0415

    examples: list[Example] = []
    for fixture_id in fixture_ids():
        fixture = load_fixture(fixture_id)
        key = sorted(load_answer_key(fixture_id))
        examples += examples_for_policy(
            fixture.policy.policy,
            key,
            application=f"eval:{fixture_id}",
            provenance={"source": SYNTHETIC, "answer_key_sha256": _sha(canonical_json(key))},
        )
    lab_key = sorted(ground_truth.expected_failures(PROFILES["vulnerable"]))
    examples += examples_for_policy(
        load_lab_policy("lab").policy,
        lab_key,
        application="lab:vulnerable",
        provenance={"source": SYNTHETIC, "answer_key_sha256": _sha(canonical_json(lab_key))},
    )
    config = files("accessguard_sampleapp").joinpath("config")
    spec = json.loads(config.joinpath("scan-spec.json").read_text("utf-8"))
    answer = json.loads(config.joinpath("answer_key.json").read_text("utf-8"))
    sample_key = sorted({c for checks in answer["flawed"].values() for c in checks})
    examples += examples_for_policy(
        parse_policy(spec["policy"]),
        sample_key,
        application="sampleapp:flawed",
        provenance={"source": SYNTHETIC, "answer_key_sha256": _sha(canonical_json(sample_key))},
    )
    return examples


def read_export(path: Path) -> list[Example]:
    """Read a consented customer export (JSON lines written by ``manage.py export_ml_labels``)."""
    examples = []
    for line_number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("feature_version") != FEATURE_VERSION:
            raise ValueError(f"{path}:{line_number}: feature version {row.get('feature_version')}")
        if set(row["features"]) != set(FEATURE_NAMES) or row["label"] not in (0, 1):
            raise ValueError(f"{path}:{line_number}: malformed example")
        examples.append(
            Example(
                row["application"],
                row["check_kind"],
                row["features"],
                row["label"],
                {**row.get("provenance", {}), "source": CUSTOMER},
            )
        )
    return examples


@dataclass(frozen=True)
class Dataset:
    version: str
    examples: tuple[Example, ...]

    @property
    def applications(self) -> list[str]:
        return sorted({e.application for e in self.examples})

    def manifest(self) -> dict[str, Any]:
        by_app: dict[str, dict[str, int]] = {}
        for example in self.examples:
            stats = by_app.setdefault(example.application, {"examples": 0, "positives": 0})
            stats["examples"] += 1
            stats["positives"] += example.label
        sources = sorted({e.provenance.get("source", "") for e in self.examples})
        return {
            "schema": DATASET_SCHEMA,
            "version": self.version,
            "feature_version": FEATURE_VERSION,
            "features": list(FEATURE_NAMES),
            "examples": len(self.examples),
            "positives": sum(e.label for e in self.examples),
            "applications": by_app,
            "sources": sources,
            "customer_applications": len(
                {e.application for e in self.examples if e.provenance.get("source") == CUSTOMER}
            ),
        }


def build_dataset(examples: Sequence[Example]) -> Dataset:
    ordered = sorted(examples, key=lambda e: (e.application, json.dumps(asdict(e), sort_keys=True)))
    digest = _sha(b"\n".join(canonical_json(asdict(e)) for e in ordered))
    return Dataset(f"ds-{digest[:16]}", tuple(ordered))


def write_dataset(dataset: Dataset, directory: Path) -> Path:
    target = directory / dataset.version
    target.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(json.dumps(asdict(e), sort_keys=True) for e in dataset.examples)
    (target / "examples.jsonl").write_text(rows + "\n", "utf-8", newline="\n")
    (target / "manifest.json").write_text(
        json.dumps(dataset.manifest(), indent=2, sort_keys=True) + "\n", "utf-8", newline="\n"
    )
    return target


def load_dataset(directory: Path) -> Dataset:
    rows = (directory / "examples.jsonl").read_text("utf-8").splitlines()
    examples = tuple(Example(**json.loads(row)) for row in rows if row.strip())
    dataset = build_dataset(examples)
    if dataset.version != directory.name:
        raise ValueError(f"{directory}: content hash {dataset.version} does not match its name")
    return dataset
