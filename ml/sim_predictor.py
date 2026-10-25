"""Live inference with the UDYAM-SIM models (trained by ml/train_sim.py).

Feeds the same 60 s window features used in training, so live and training match.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.generate_dataset import WINDOW_S, window_features
from sim.factory import TICK_S

MODEL_PATH = Path(__file__).resolve().parent / "sim_model.joblib"
WINDOW_TICKS = int(WINDOW_S / TICK_S)


class SimPredictor:
    def __init__(self, path: Path = MODEL_PATH):
        self.path = path
        self._bundle: dict | None = None
        self.error: str | None = None

    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self) -> dict | None:
        if self._bundle is None and self.error is None:
            try:
                import joblib

                self._bundle = joblib.load(self.path)
            except Exception as err:  # missing file or sklearn
                self.error = f"{type(err).__name__}: {err}"
        return self._bundle

    def predict(self, buf: list[dict[str, Any]]) -> dict | None:
        """buf: list of {"obs", "twin", "pressure_bar"} frames, oldest first."""
        b = self._load()
        if b is None or len(buf) < WINDOW_TICKS:
            return None
        feats = window_features(buf[-WINDOW_TICKS:])
        import pandas as pd

        row = pd.DataFrame([[feats[f] for f in b["features"]]], columns=b["features"])
        proba = b["clf"].predict_proba(row)[0]
        ranked = sorted(zip(b["classes"], proba), key=lambda x: -x[1])
        minutes = float(b["reg"].predict(row)[0])
        return {
            "model": b["version"],
            "top_faults": [{"fault": c, "probability": round(float(p), 3)} for c, p in ranked[:3]],
            "minutes_to_limit_ml": round(max(0.0, min(30.0, minutes)), 1),
            "minutes_to_limit_note": "30 means 'no limit expected within 30 min'",
            "key_features": {k: feats[k] for k in (
                "temp_mean", "temp_slope", "vib_max", "torque_mean", "rpm_mean",
                "resid_ewma", "h_cool_est", "h_cool_drift", "dq_bad_frac", "air_pressure_bar")},
        }
