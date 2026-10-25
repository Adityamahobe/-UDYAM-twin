"""Train the AI4I 2020 failure-risk Random Forests and write honest test metrics.

    python -m ml.train_ai4i

- Data: the UCI AI4I 2020 CSV in ml/data/ai4i2020.csv (SHA-256 checked; a synthetic stand-in is refused).
- Features: air temp, process temp, rpm, torque, tool wear (no vibration/current, no engineered features).
- Targets: Machine failure plus the TWF / HDF / PWF / OSF / RNF modes, one binary Random Forest each.
- Split: one stratified 80/20 split on Machine failure (seed 42); 5-fold stratified CV F1 for Machine failure.
- class_weight="balanced". Metrics at the 0.5 vote threshold, per target, on the held-out 20 %.

Writes models/ai4i_rf.joblib (models + feature list + training ranges/medians) and models/metrics.json.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split

from ml.ai4i import (
    DATA_PATH,
    FEATURES,
    METRICS_PATH,
    MODE_INFO,
    MODEL_DIR,
    MODEL_PATH,
    MODES,
    SOURCE_URL,
    TARGETS,
    load_dataset,
    sha256,
)
from ml.rules import evaluate_ai4i_rules

SEED = 42
RF_PARAMS = {"n_estimators": 150, "min_samples_leaf": 2, "class_weight": "balanced", "random_state": SEED, "n_jobs": -1}


def _scores(y: np.ndarray, pred: np.ndarray, prob: np.ndarray | None = None) -> dict[str, Any]:
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    out = {
        "support_positive": int(y.sum()),
        "precision": round(float(precision_score(y, pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y, pred, zero_division=0)), 4),
        "f1": round(float(f1_score(y, pred, zero_division=0)), 4),
        "false_positive_rate": round(float(fp / (fp + tn)), 4) if fp + tn else 0.0,
        "confusion_matrix": {"true_negatives": int(tn), "false_positives": int(fp),
                             "false_negatives": int(fn), "true_positives": int(tp)},
    }
    if prob is not None and 0 < y.sum() < len(y):
        out["roc_auc"] = round(float(roc_auc_score(y, prob)), 4)
        out["pr_auc"] = round(float(average_precision_score(y, prob)), 4)
    return out


def train(seed: int = SEED, write: bool = True) -> dict[str, Any]:
    df = load_dataset()
    X = df[FEATURES]
    idx_train, idx_test = train_test_split(df.index, test_size=0.2, random_state=seed, stratify=df["machine_failure"])
    X_train, X_test = X.loc[idx_train], X.loc[idx_test]

    models: dict[str, RandomForestClassifier] = {}
    per_class: dict[str, Any] = {}
    for target in TARGETS:
        y_train, y_test = df.loc[idx_train, target].to_numpy(), df.loc[idx_test, target].to_numpy()
        clf = RandomForestClassifier(**RF_PARAMS).fit(X_train, y_train)
        models[target] = clf
        prob = clf.predict_proba(X_test)[:, 1]
        per_class[target] = {
            "label": "Machine failure" if target == "machine_failure" else MODE_INFO[target],
            **_scores(y_test, (prob >= 0.5).astype(int), prob),
        }

    failure = models["machine_failure"]
    importances = {f: round(float(v), 4) for f, v in zip(FEATURES, failure.feature_importances_)}
    cv = cross_val_score(RandomForestClassifier(**RF_PARAMS), X, df["machine_failure"], scoring="f1",
                         cv=StratifiedKFold(5, shuffle=True, random_state=seed))

    # Rule baseline: AI4I's published failure-mode rules (ml/rules.py) on the same test rows.
    test = df.loc[idx_test]
    rules = [evaluate_ai4i_rules(air_temp_k=r.air_temp_k, process_temp_k=r.process_temp_k, rpm=r.rpm,
                                 torque_nm=r.torque_nm, tool_wear_min=r.tool_wear_min,
                                 variant_type=r.variant_type) for r in test.itertuples()]
    rule_any = np.array([int(r.hdf or r.pwf or r.osf or r.twf) for r in rules])
    rule_baseline = {
        **_scores(test["machine_failure"].to_numpy(), rule_any),
        "per_mode": {m: _scores(test[m].to_numpy(), np.array([int(getattr(r, m.lower())) for r in rules]))
                     for m in ("TWF", "HDF", "PWF", "OSF")},
        "note": "AI4I labels were generated from these rules (plus randomness), so the rule baseline is "
                "expected to score well; it shows what the dataset encodes, not field performance.",
    }

    ranges = {f: [float(X_train[f].min()), float(X_train[f].max())] for f in FEATURES}
    medians = {f: float(X_train[f].median()) for f in FEATURES}
    trained_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dataset = {
        "name": "AI4I 2020 Predictive Maintenance Dataset",
        "source": SOURCE_URL,
        "license": "CC BY 4.0",
        "citation": "Matzka, S. (2020). Explainable Artificial Intelligence for Predictive Maintenance "
                    "Applications. AI4I 2020. UCI Machine Learning Repository.",
        "sha256": sha256(DATA_PATH),
        "rows": int(len(df)),
        "failure_rate": round(float(df["machine_failure"].mean()), 4),
        "positives": {t: int(df[t].sum()) for t in TARGETS},
        "synthetic_source": True,
        "about": "AI4I 2020 is itself a synthetic dataset that reflects a real milling process; it is not "
                 "data from this shop's machines.",
    }
    model_desc = f"RandomForestClassifier per target ({', '.join(f'{k}={v}' for k, v in RF_PARAMS.items() if k != 'n_jobs')})"

    report = {
        "dataset": dataset,
        "trained_at": trained_at,
        "model": model_desc,
        "features": FEATURES,
        "targets": TARGETS,
        "split": {"strategy": "stratified on Machine failure", "test_size": 0.2, "seed": seed,
                  "train_rows": int(len(idx_train)), "test_rows": int(len(idx_test))},
        "threshold": 0.5,
        "per_class": per_class,
        # Machine-failure model in the shape the dashboard already reads.
        "random_forest": {**{k: v for k, v in per_class["machine_failure"].items() if k != "label"},
                          "feature_importances": importances},
        "cv_machine_failure_f1": {"folds": 5, "mean": round(float(cv.mean()), 4), "std": round(float(cv.std()), 4)},
        "rule_baseline": rule_baseline,
        "vibration_and_current_excluded_from_ml": True,
        "probability_calibrated": False,
        "honest_disclaimer": (
            "Metrics are on a held-out 20 % of AI4I 2020, a synthetic dataset modelled on a real milling "
            "process. They show the model learned that dataset, not that it predicts failures on this shop's "
            "machines; that needs validation on recorded field data. Risk % is a Random Forest vote share, "
            "not a calibrated probability. RNF failures are random by construction and are not predictable."
        ),
    }
    if write:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump({"models": models, "meta": {
            "model": model_desc, "features": FEATURES, "targets": TARGETS, "trained_at": trained_at,
            "dataset": {k: dataset[k] for k in ("name", "source", "sha256", "synthetic_source")},
            "train_ranges": ranges, "train_medians": medians,
        }}, MODEL_PATH, compress=3)
        METRICS_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    rep = train()
    print(f"Trained on {rep['split']['train_rows']} rows, tested on {rep['split']['test_rows']}.")
    print(f"{'target':16s} {'pos':>4s} {'prec':>6s} {'recall':>6s} {'F1':>6s} {'ROC-AUC':>8s} {'PR-AUC':>7s}")
    for t, m in rep["per_class"].items():
        print(f"{t:16s} {m['support_positive']:4d} {m['precision']:6.3f} {m['recall']:6.3f} {m['f1']:6.3f} "
              f"{m.get('roc_auc', float('nan')):8.3f} {m.get('pr_auc', float('nan')):7.3f}")
    cv = rep["cv_machine_failure_f1"]
    print(f"5-fold CV F1 (machine failure): {cv['mean']:.3f} ± {cv['std']:.3f}")
    print(f"Rule baseline F1 (machine failure): {rep['rule_baseline']['f1']:.3f}")
    print(f"Wrote {MODEL_PATH} and {METRICS_PATH}")
