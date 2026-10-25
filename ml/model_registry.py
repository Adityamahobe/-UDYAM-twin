"""Versioned registry for the AI4I failure-risk model (the one the digital twin uses live).

models/registry/ai4i_failure/
  registry.json         production version, every version's card, and the promotion / rollback history
  v001/model.joblib     the original models/ai4i_rf.joblib, registered as the baseline (trained on AI4I 2020 only)
  v002/model.joblib     candidates trained by ml/candidate.py, each with metrics.json and its evaluation gate

Rules:
- A candidate is never deployed automatically. It becomes production only through promote(), which refuses a
  version whose evaluation gate did not pass. The API also requires an operator (api/app.py).
- rollback() makes an earlier version production again; only versions that were production before, or the
  baseline, are eligible.
- Every change is appended to the history with who did it and when.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ml.ai4i import METRICS_PATH, MODEL_PATH

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = Path(os.environ.get("UDYAM_REGISTRY_DIR", ROOT / "models" / "registry" / "ai4i_failure"))


class RegistryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ModelRegistry:
    def __init__(self, root: Path = REGISTRY_DIR, baseline_model: Path = MODEL_PATH, baseline_metrics: Path = METRICS_PATH):
        self.root = root
        self.baseline_model = baseline_model
        self.baseline_metrics = baseline_metrics
        self.lock = threading.RLock()

    # -- storage ---------------------------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.root / "registry.json"

    def _load(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return self._init()
        return json.loads(self.index_path.read_text())

    def _save(self, reg: dict[str, Any]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, indent=2))
        tmp.replace(self.index_path)

    def _init(self) -> dict[str, Any]:
        """Register the shipped model as v001 (baseline) the first time the registry is used."""
        if not self.baseline_model.is_file():
            raise RegistryError(f"{self.baseline_model} not found; run `python -m ml.train_ai4i` first")
        vdir = self.root / "v001"
        vdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.baseline_model, vdir / "model.joblib")
        metrics = json.loads(self.baseline_metrics.read_text()) if self.baseline_metrics.is_file() else {}
        (vdir / "metrics.json").write_text(json.dumps(metrics, indent=2))
        card = {
            "version": "v001", "kind": "baseline", "status": "production", "created_at": _now(),
            "trained_at": metrics.get("trained_at"),
            "trained_on": ["AI4I 2020 (UCI; itself a synthetic dataset modelled on a real milling process)"],
            "synthetic_datasets": [], "real_machine_data": False,
            "summary": "The model shipped with UDYAM-TWIN (models/ai4i_rf.joblib).",
            "gate": {"passed": True, "baseline": True, "checks": []},
            "ai4i_test": (metrics.get("per_class") or {}).get("machine_failure"),
        }
        reg = {"model": "ai4i_failure", "production": "v001", "versions": [card],
               "history": [{"at": card["created_at"], "action": "register_baseline", "version": "v001", "by": "system"}]}
        self.root.mkdir(parents=True, exist_ok=True)
        self._save(reg)
        return reg

    # -- reads -----------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        with self.lock:
            return self._load()

    def card(self, version: str) -> dict[str, Any]:
        reg = self.state()
        for v in reg["versions"]:
            if v["version"] == version:
                return v
        raise RegistryError(f"unknown version {version}")

    def model_path(self, version: str) -> Path:
        self.card(version)
        return self.root / version / "model.joblib"

    def metrics(self, version: str) -> dict[str, Any]:
        p = self.root / self.card(version)["version"] / "metrics.json"
        return json.loads(p.read_text()) if p.is_file() else {}

    def production(self) -> str:
        return self.state()["production"]

    def production_model_path(self) -> Path:
        return self.model_path(self.production())

    # -- writes ----------------------------------------------------------------
    def next_version(self) -> str:
        reg = self.state()
        return f"v{max(int(v['version'][1:]) for v in reg['versions']) + 1:03d}"

    def add_candidate(self, version: str, card: dict[str, Any]) -> dict[str, Any]:
        """Record a trained candidate (its files are already in root/version)."""
        with self.lock:
            reg = self._load()
            if any(v["version"] == version for v in reg["versions"]):
                raise RegistryError(f"{version} already exists")
            card = {**card, "version": version, "kind": "candidate",
                    "status": "candidate" if card["gate"]["passed"] else "rejected_by_gate", "created_at": _now()}
            reg["versions"].append(card)
            reg["history"].append({"at": card["created_at"], "action": "train_candidate", "version": version,
                                   "by": card.get("trained_by", "operator"), "gate_passed": card["gate"]["passed"]})
            self._save(reg)
            return card

    def promote(self, version: str, by: str) -> dict[str, Any]:
        with self.lock:
            reg = self._load()
            card = next((v for v in reg["versions"] if v["version"] == version), None)
            if card is None:
                raise RegistryError(f"unknown version {version}")
            if version == reg["production"]:
                raise RegistryError(f"{version} is already in production")
            if not card["gate"]["passed"]:
                raise RegistryError(f"{version} did not pass the evaluation gate and cannot be deployed")
            if card["status"] not in ("candidate", "archived"):
                raise RegistryError(f"{version} has status {card['status']} and cannot be promoted")
            return self._switch(reg, version, by, "promote")

    def rollback(self, by: str, to_version: str | None = None) -> dict[str, Any]:
        """Back to `to_version`, or to the version that was production before the current one."""
        with self.lock:
            reg = self._load()
            was_prod = [h["version"] for h in reg["history"] if h["action"] in ("register_baseline", "promote", "rollback")]
            if to_version is None:
                earlier = [v for v in was_prod if v != reg["production"]]
                if not earlier:
                    raise RegistryError("there is no earlier production version to roll back to")
                to_version = earlier[-1]
            if to_version == reg["production"]:
                raise RegistryError(f"{to_version} is already in production")
            if to_version not in was_prod:
                raise RegistryError(f"{to_version} was never in production; promote it instead (it must pass the gate)")
            return self._switch(reg, to_version, by, "rollback")

    def _switch(self, reg: dict[str, Any], version: str, by: str, action: str) -> dict[str, Any]:
        old = reg["production"]
        for v in reg["versions"]:
            if v["version"] == old:
                v["status"] = "archived"
            if v["version"] == version:
                v["status"] = "production"
        reg["production"] = version
        reg["history"].append({"at": _now(), "action": action, "version": version, "from": old, "by": by})
        self._save(reg)
        return reg
