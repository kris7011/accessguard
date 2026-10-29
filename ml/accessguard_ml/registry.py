"""Model registry: versioned artifacts, a promotion gate and rollback.

``registry.json`` records every trained model (``candidate``, ``active`` or ``retired``) and
the history of activations. A model becomes active only through :func:`promote`, which
refuses unless the gate passes; :func:`rollback` reactivates the previously active model
(or none). Nothing in the product loads a model unless one is active.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from accessguard.application.ranking import FEATURE_VERSION
from accessguard_ml.training import Evaluation

MIN_CUSTOMER_APPLICATIONS = 5
MIN_GAIN_AT_25 = 0.05
MAX_REGRESSION_PER_APPLICATION = 0.2


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: list[str]


def promotion_gate(evaluation: Evaluation, manifest: dict[str, Any]) -> GateResult:
    """Fixed, pre-registered criteria (docs/adr/0013). Every failed criterion is listed."""
    reasons = []
    if manifest["feature_version"] != FEATURE_VERSION:
        reasons.append(f"feature version {manifest['feature_version']} != engine {FEATURE_VERSION}")
    customers = manifest.get("customer_applications", 0)
    if customers < MIN_CUSTOMER_APPLICATIONS:
        reasons.append(
            f"only {customers} consented customer applications (need {MIN_CUSTOMER_APPLICATIONS}); "
            "synthetic fixtures are not evidence of real-world performance"
        )
    model25 = evaluation.mean["model"]["recall@25%"]
    model50 = evaluation.mean["model"]["recall@50%"]
    name25, best25 = evaluation.best_baseline("recall@25%")
    name50, best50 = evaluation.best_baseline("recall@50%")
    if model25 < best25 + MIN_GAIN_AT_25:
        reasons.append(
            f"mean recall@25% {model25:.3f} is not at least {MIN_GAIN_AT_25} above the best "
            f"baseline ({name25}: {best25:.3f})"
        )
    if model50 < best50:
        reasons.append(f"mean recall@50% {model50:.3f} is below {name50} ({best50:.3f})")
    for application, results in evaluation.per_application.items():
        model = results["model"]["recall@25%"]
        if model is None:
            continue
        baseline = max(
            v for k, r in results.items() if k != "model" and (v := r["recall@25%"]) is not None
        )
        if baseline - model > MAX_REGRESSION_PER_APPLICATION:
            reasons.append(
                f"{application}: recall@25% {model:.3f} is more than "
                f"{MAX_REGRESSION_PER_APPLICATION} below the best baseline ({baseline:.3f})"
            )
    return GateResult(not reasons, reasons)


class Registry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "registry.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"active": None, "models": {}, "history": []}
        data: dict[str, Any] = json.loads(self.path.read_text("utf-8"))
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8", newline="\n"
        )

    def register(self, artifact: dict[str, Any]) -> None:
        data = self.load()
        version = artifact["version"]
        if version in data["models"]:
            raise ValueError(f"model {version} is already registered")
        models_dir = self.root / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        (models_dir / f"{version}.json").write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n", "utf-8", newline="\n"
        )
        data["models"][version] = {
            "status": "candidate",
            "dataset": artifact["dataset"]["version"],
            "gate": artifact["gate"],
            "created_at": artifact["created_at"],
        }
        self._save(data)

    def artifact(self, version: str) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(
            (self.root / "models" / f"{version}.json").read_text("utf-8")
        )
        return loaded

    def promote(self, version: str, *, reviewer: str) -> None:
        data = self.load()
        entry = data["models"].get(version)
        if entry is None:
            raise ValueError(f"unknown model {version}")
        if not entry["gate"]["passed"]:
            raise ValueError(
                "the promotion gate did not pass: " + "; ".join(entry["gate"]["reasons"])
            )
        if not reviewer.strip():
            raise ValueError("promotion needs a named reviewer")
        previous = data["active"]
        if previous:
            data["models"][previous]["status"] = "retired"
        entry["status"] = "active"
        data["active"] = version
        data["history"].append(
            {
                "action": "promote",
                "version": version,
                "previous": previous,
                "reviewer": reviewer,
                "at": datetime.now(UTC).isoformat(),
            }
        )
        self._save(data)

    def rollback(self, *, reviewer: str) -> str | None:
        """Reactivate the model that was active before the current one (or none)."""
        data = self.load()
        current = data["active"]
        if current is None:
            raise ValueError("no model is active")
        promotions = [h for h in data["history"] if h["action"] == "promote"]
        previous = next(
            (h["previous"] for h in reversed(promotions) if h["version"] == current), None
        )
        data["models"][current]["status"] = "retired"
        if previous:
            data["models"][previous]["status"] = "active"
        data["active"] = previous
        data["history"].append(
            {
                "action": "rollback",
                "version": current,
                "restored": previous,
                "reviewer": reviewer,
                "at": datetime.now(UTC).isoformat(),
            }
        )
        self._save(data)
        return previous
