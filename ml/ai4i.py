"""AI4I 2020 failure-risk model: dataset loading and inference.

Training lives in ml/train_ai4i.py (`python -m ml.train_ai4i`), which writes
models/ai4i_rf.joblib and models/metrics.json.

Inputs are the five AI4I process columns only. Vibration and current never go into this
model (AI4I has no such columns); they feed the rule engine.

The output is a Random Forest vote share, reported as "failure risk %". It is not a
calibrated probability, and the model is trained on AI4I 2020, which is itself a synthetic
dataset modelled on a real milling process (Matzka, 2020), not on this shop's machines.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "ml" / "data" / "ai4i2020.csv"
MODEL_DIR = ROOT / "models"
MODEL_PATH = MODEL_DIR / "ai4i_rf.joblib"
METRICS_PATH = MODEL_DIR / "metrics.json"

SOURCE_URL = "https://archive.ics.uci.edu/dataset/601/ai4i+2020+predictive+maintenance+dataset"
EXPECTED_SHA256 = "dc6630cd9b1f0f853922fad78a1b6436570d3f1ec863f1dd5c4340ac56bc8a8e"

COLUMNS = {
    "Air temperature [K]": "air_temp_k",
    "Process temperature [K]": "process_temp_k",
    "Rotational speed [rpm]": "rpm",
    "Torque [Nm]": "torque_nm",
    "Tool wear [min]": "tool_wear_min",
    "Machine failure": "machine_failure",
    "Type": "variant_type",
}
FEATURES = ["air_temp_k", "process_temp_k", "rpm", "torque_nm", "tool_wear_min"]
MODES = ["TWF", "HDF", "PWF", "OSF", "RNF"]
TARGETS = ["machine_failure", *MODES]
FEATURE_INFO = {
    "air_temp_k": ("Air temperature", "°C"),
    "process_temp_k": ("Process temperature", "°C"),
    "rpm": ("Rotational speed", "rpm"),
    "torque_nm": ("Torque", "N·m"),
    "tool_wear_min": ("Tool wear", "min"),
}
MODE_INFO = {
    "TWF": "Tool wear failure",
    "HDF": "Heat dissipation failure",
    "PWF": "Power failure",
    "OSF": "Overstrain failure",
    "RNF": "Random failure",
}
K_TO_C = 273.15


class DatasetError(ValueError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_dataset(path: Path = DATA_PATH, verify_hash: bool = True) -> pd.DataFrame:
    """The UCI AI4I 2020 CSV with snake_case columns. Refuses anything else (e.g. a synthetic stand-in)."""
    if not path.exists():
        raise DatasetError(f"{path} not found. Download it from {SOURCE_URL}.")
    if verify_hash and sha256(path) != EXPECTED_SHA256:
        raise DatasetError(f"{path} is not the UCI AI4I 2020 file (SHA-256 mismatch).")
    df = pd.read_csv(path, encoding="utf-8-sig")
    missing = [c for c in [*COLUMNS, *MODES] if c not in df.columns]
    if missing:
        raise DatasetError(f"{path} lacks AI4I columns: {missing}")
    df = df.rename(columns=COLUMNS)
    if len(df) != 10000:
        raise DatasetError(f"expected 10,000 rows, got {len(df)}")
    return df


def _display(feature: str, value: float) -> float:
    return round(value - K_TO_C, 2) if feature.endswith("_k") else round(float(value), 2)


class AI4IPredictor:
    """Failure risk % and per-mode risk with per-feature contributions, from models/ai4i_rf.joblib."""

    def __init__(self, model_path: Path = MODEL_PATH):
        self.model_path = model_path
        self.models: dict[str, Any] = {}
        self.meta: dict[str, Any] = {}
        self.load_error: str | None = None
        if not model_path.exists():
            self.load_error = f"{model_path.name} not found; run `python -m ml.train_ai4i`."
            return
        try:
            art = joblib.load(model_path)
            self.models, self.meta = art["models"], art["meta"]
            for m in self.models.values():
                m.n_jobs = 1  # single-row predictions: thread start-up costs more than it saves
        except Exception as err:  # noqa: BLE001 - surfaced in status
            self.load_error = f"model artifact unreadable: {err}"

    @property
    def ready(self) -> bool:
        return bool(self.models)

    def info(self) -> dict[str, Any]:
        if not self.ready:
            return {"status": "UNAVAILABLE", "detail": self.load_error}
        return {"status": "READY", **{k: self.meta[k] for k in ("model", "features", "targets", "trained_at", "dataset")}}

    def predict(
        self,
        *,
        air_temp_k: float,
        process_temp_k: float,
        rpm: float,
        torque_nm: float,
        tool_wear_min: float,
        top_n: int = 3,
    ) -> dict[str, Any]:
        if not self.ready:
            return {"status": "UNAVAILABLE", "detail": self.load_error, "failure_risk_pct": None}
        x = {"air_temp_k": air_temp_k, "process_temp_k": process_temp_k, "rpm": rpm,
             "torque_nm": torque_nm, "tool_wear_min": tool_wear_min}
        medians = self.meta["train_medians"]
        # Row 0 is the input; row i+1 has feature i replaced by its training median. The drop in
        # risk is that feature's contribution (a simple, model-agnostic attribution).
        rows = [dict(x)] + [{**x, f: medians[f]} for f in FEATURES]
        frame = pd.DataFrame(rows, columns=FEATURES)
        p_fail = self.models["machine_failure"].predict_proba(frame)[:, 1]
        risk = float(p_fail[0])
        contrib = [
            {"feature": f, "label": FEATURE_INFO[f][0], "value": _display(f, x[f]), "unit": FEATURE_INFO[f][1],
             "training_median": _display(f, medians[f]),
             "contribution_pct": round(100.0 * (risk - float(p_fail[i + 1])), 1)}
            for i, f in enumerate(FEATURES)
        ]
        contrib.sort(key=lambda c: c["contribution_pct"], reverse=True)
        top = [c for c in contrib if c["contribution_pct"] > 0][:top_n]

        modes = {m: round(100.0 * float(self.models[m].predict_proba(frame.iloc[:1])[0, 1]), 1) for m in MODES}
        # RNF is random by construction (unpredictable from these inputs), so it never headlines.
        top_mode = max((m for m in MODES if m != "RNF"), key=modes.get)
        ranges = self.meta["train_ranges"]
        oor = [f for f in FEATURES if not ranges[f][0] <= x[f] <= ranges[f][1]]
        return {
            "status": "OUT_OF_RANGE" if oor else "OK",
            "failure_risk_pct": round(100.0 * risk, 1),
            "top_features": top,
            "modes_pct": modes,
            "top_mode": {"mode": top_mode, "label": MODE_INFO[top_mode], "risk_pct": modes[top_mode]},
            "out_of_range": [{"feature": f, "label": FEATURE_INFO[f][0], "value": _display(f, x[f]),
                              "training_range": [_display(f, ranges[f][0]), _display(f, ranges[f][1])],
                              "unit": FEATURE_INFO[f][1]} for f in oor],
            "model": self.meta["model"],
            "calibrated_probability": False,
            "note": "Random Forest vote share trained on AI4I 2020 (synthetic milling dataset); "
                    "unvalidated on this machine, not a calibrated probability.",
        }


def as_rows(values: np.ndarray) -> list[float]:
    return [float(v) for v in values]
