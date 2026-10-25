"""Inference interface for the vibration fault model.

Returns an explicit status instead of a prediction whenever one would be unjustified:
no trained model, a model trained on synthetic data, or too few input features.
The class vote share of a Random Forest is reported as such: it is not a calibrated
probability unless the model metadata says it was calibrated.

Domain checks (from the model's `input_spec`): a waveform sampled at a rate the model
was not trained on gets no prediction (full-bandwidth features such as kurtosis are not
comparable across rates). A machine type outside the training data gets a prediction
marked OK_UNVALIDATED.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from ml.vibration.train import ARTIFACT_DIR, MODEL_FILE

MIN_FEATURE_FRACTION = 0.8


class VibrationPredictor:
    def __init__(self, model_path: Path = ARTIFACT_DIR / MODEL_FILE):
        self.model_path = model_path
        self.pipeline = None
        self.metadata: dict[str, Any] = {}
        self.load_error: str | None = None
        if model_path.exists():
            try:
                art = joblib.load(model_path)
                self.pipeline, self.metadata = art["pipeline"], art["metadata"]
            except Exception as err:  # noqa: BLE001 - surfaced in status
                self.load_error = f"model artifact unreadable: {err}"

    @property
    def synthetic(self) -> bool:
        return bool(self.metadata.get("dataset", {}).get("synthetic"))

    def info(self) -> dict[str, Any]:
        if self.pipeline is None:
            return {"status": "TRAINING_NOT_CONFIGURED", "detail": self.load_error or "No vibration model has been trained."}
        md = self.metadata
        return {
            "status": "SYNTHETIC_MODEL" if self.synthetic else "READY",
            "dataset": md.get("dataset"),
            "trained_at": md.get("trained_at"),
            "selected_model": md.get("selected_model"),
            "labels": md.get("labels"),
            "feature_columns": md.get("feature_columns"),
            "split": (md.get("split") or {}).get("strategy"),
            "probability_calibrated": md.get("probability_calibrated", False),
            "input_spec": md.get("input_spec"),
        }

    def predict(
        self,
        features: dict[str, float | None],
        *,
        allow_synthetic: bool = False,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """`context`: {"sampling_rate_hz", "machine_type"} of the input waveform, for domain checks."""
        base = {"kind": "ml_prediction", "label": None, "class_vote_share": None, "calibrated": False}
        if self.pipeline is None:
            return {**base, "status": "TRAINING_NOT_CONFIGURED",
                    "detail": self.load_error or "No vibration model has been trained; no labelled vibration dataset is available."}
        if self.synthetic and not allow_synthetic:
            return {**base, "status": "SYNTHETIC_MODEL",
                    "detail": "Model was trained on synthetic data; predictions are not published."}
        spec = self.metadata.get("input_spec") or {}
        ctx = context or {}
        fs, train_fs = ctx.get("sampling_rate_hz"), spec.get("sampling_rate_hz") or []
        if fs is not None and train_fs and not any(abs(float(fs) - f) < 1e-6 for f in train_fs):
            return {**base, "status": "OUT_OF_DOMAIN",
                    "detail": f"Waveform sampled at {float(fs):g} Hz; model trained at {', '.join(f'{f:g}' for f in train_fs)} Hz. "
                              "Full-bandwidth features are not comparable, so no prediction is made."}
        cols = self.metadata["feature_columns"]
        present = [c for c in cols if features.get(c) is not None and np.isfinite(features[c])]
        if len(present) < MIN_FEATURE_FRACTION * len(cols):
            return {**base, "status": "INSUFFICIENT_FEATURES",
                    "detail": f"{len(present)}/{len(cols)} model features available; waveform features are required.",
                    "missing_features": [c for c in cols if c not in present]}
        X = pd.DataFrame([{c: (features.get(c) if c in present else np.nan) for c in cols}], dtype=float)
        label = str(self.pipeline.predict(X)[0])
        share = None
        if hasattr(self.pipeline, "predict_proba"):
            proba = self.pipeline.predict_proba(X)[0]
            classes = [str(c) for c in self.pipeline.classes_]
            share = {c: round(float(p), 4) for c, p in zip(classes, proba)}
        calibrated = bool(self.metadata.get("probability_calibrated"))
        notes = [] if calibrated else ["class_vote_share is the model's vote fraction, not a calibrated probability."]
        status = "OK"
        mtype, train_types = ctx.get("machine_type"), spec.get("machine_types") or []
        if mtype is not None and train_types and mtype not in train_types:
            status = "OK_UNVALIDATED"
            notes.insert(0, f"Model trained on {', '.join(train_types)} data ({(self.metadata.get('dataset') or {}).get('name')}); "
                            f"not validated for machine type '{mtype}'.")
        return {**base, "status": status, "label": label, "class_vote_share": share, "calibrated": calibrated,
                "model": self.metadata.get("selected_model"), "detail": " ".join(notes) or None}
