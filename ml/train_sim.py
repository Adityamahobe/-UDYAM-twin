"""Train the UDYAM-SIM models on our own simulation dataset.

  1. Fault classifier: which fault is developing on VMC-01 (8 classes incl. normal)
  2. Time-to-limit regressor: minutes until over-temperature or ISO 20816 zone-D vibration
     (the machine-level RUL estimate, capped at 30 min)

Evaluation is honest by construction:
  - split BY EPISODE (no windows from one run in both train and test)
  - an extra out-of-distribution (OOD) test set: heavier machines never seen in training
  - compared against a transparent rule baseline using the same features

Usage:
  python -m tools.generate_dataset      # first, builds ml/data/udyam_sim_v1_*.csv
  python -m ml.train_sim                # writes ml/sim_model.joblib, sim_metrics.json, sim_knowledge.json
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    mean_absolute_error,
    precision_recall_fscore_support,
)
from sklearn.model_selection import GroupShuffleSplit

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "ml" / "data"
VERSION = "udyam_sim_v1"
MODEL_PATH = ROOT / "ml" / "sim_model.joblib"
METRICS_PATH = ROOT / "ml" / "sim_metrics.json"
KNOWLEDGE_PATH = ROOT / "ml" / "sim_knowledge.json"

CLASSES = [
    "normal",
    "coolant_degradation",
    "bearing_defect",
    "overload",
    "tool_wear_accel",
    "sensor_stuck",
    "pressure_drop",
    "order_surge",
]


def rule_baseline(df: pd.DataFrame) -> np.ndarray:
    """Transparent if-then rules on the same window features (what a careful engineer would write)."""
    from config import get_config

    zones = get_config().risk_zones("VMC-01", "cnc_mill")
    vib_zone_c = zones[0] if zones else 2.8
    out = []
    for r in df.itertuples(index=False):
        if r.dq_bad_frac > 0.3:
            out.append("sensor_stuck")
        elif r.vib_max > vib_zone_c:                # zone C of the configured ISO-referenced profile
            out.append("bearing_defect")
        elif r.air_pressure_bar < 6.2:
            out.append("pressure_drop")
        elif r.torque_mean > 50.0:
            out.append("overload")
        elif r.rpm_mean > 1650.0:
            out.append("order_surge")
        elif r.wear_slope > 0.2:                    # min of wear per s, ~2x normal
            out.append("tool_wear_accel")
        elif r.h_cool_drift < -0.02 or r.resid_ewma > 0.6:
            out.append("coolant_degradation")
        else:
            out.append("normal")
    return np.array(out)


def cls_report(y_true, y_pred) -> dict:
    p, r, f, s = precision_recall_fscore_support(y_true, y_pred, labels=CLASSES, zero_division=0)
    return {
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 4),
        # macro over classes actually present in this test set
        "macro_f1": round(float(f1_score(y_true, y_pred, labels=[c for c in CLASSES if c in set(y_true)],
                                         average="macro", zero_division=0)), 4),
        "per_class": {
            c: {"precision": round(float(p[i]), 3), "recall": round(float(r[i]), 3),
                "f1": round(float(f[i]), 3), "support": int(s[i])}
            for i, c in enumerate(CLASSES)
        },
        "confusion_matrix": {"labels": CLASSES,
                             "matrix": confusion_matrix(y_true, y_pred, labels=CLASSES).tolist()},
    }


def detection_delay(df: pd.DataFrame, pred: np.ndarray) -> dict:
    """Seconds from fault onset to the first correct classification, per faulty test episode."""
    d = df.assign(pred=pred)
    delays = {}
    for ep, g in d[d.since_onset_s >= 0].groupby("episode"):
        fault = g.label_fault.iloc[0]
        hit = g[g.pred == fault]
        delays.setdefault(fault, []).append(float(hit.since_onset_s.min()) if len(hit) else None)
    out = {}
    for fault, v in delays.items():
        found = [x for x in v if x is not None]
        out[fault] = {
            "episodes": len(v),
            "detected": len(found),
            "median_delay_s": round(float(np.median(found)), 1) if found else None,
        }
    return out


def knowledge(episodes: pd.DataFrame) -> dict:
    """Facts mined from the simulated episodes, for the advisor chatbot."""
    ind = episodes[episodes.ood == 0]
    facts = {}
    for fault, g in ind.groupby("fault"):
        entry = {"episodes": int(len(g))}
        for flag, name in ((0, "no_action"), (1, "auto_derate")):
            h = g[g.auto_derate == flag]
            if len(h) == 0:
                continue
            reached = h[h.reached_limit == 1]
            entry[name] = {
                "episodes": int(len(h)),
                "pct_reached_limit": round(100.0 * len(reached) / len(h), 1),
                "median_min_onset_to_limit": (
                    round(float(reached.minutes_onset_to_limit.dropna().median()), 2)
                    if reached.minutes_onset_to_limit.notna().any() else None
                ),
                "mean_parts_per_30min": round(float(h.parts_done.mean()), 1),
            }
        facts[fault] = entry
    return facts


def main() -> None:
    t0 = time.time()
    win = pd.read_csv(DATA / f"{VERSION}_windows.csv")
    eps = pd.read_csv(DATA / f"{VERSION}_episodes.csv")
    meta = json.loads((DATA / f"{VERSION}_meta.json").read_text())
    features = meta["features"]

    ind = win[win.ood == 0].reset_index(drop=True)
    ood = win[win.ood == 1].reset_index(drop=True)
    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=7)
    tr_idx, te_idx = next(gss.split(ind, groups=ind.episode))
    train, test = ind.iloc[tr_idx], ind.iloc[te_idx]
    print(f"train {len(train)} rows / {train.episode.nunique()} eps · test {len(test)} / "
          f"{test.episode.nunique()} eps · OOD {len(ood)} / {ood.episode.nunique()} eps")

    clf = RandomForestClassifier(
        n_estimators=150, max_depth=18, min_samples_leaf=3,
        class_weight="balanced_subsample", n_jobs=-1, random_state=7,
    )
    clf.fit(train[features], train.label_fault)

    reg = RandomForestRegressor(n_estimators=120, max_depth=16, min_samples_leaf=4, n_jobs=-1, random_state=7)
    reg.fit(train[features], train.minutes_to_limit)

    results = {}
    for name, part in (("test_in_distribution", test), ("test_ood_heavier_machines", ood)):
        if len(part) == 0:
            continue
        pred = clf.predict(part[features])
        rules = rule_baseline(part)
        m_pred = np.clip(reg.predict(part[features]), 0, 30)
        near = part.minutes_to_limit < 30
        results[name] = {
            "rows": int(len(part)),
            "episodes": int(part.episode.nunique()),
            "random_forest": cls_report(part.label_fault, pred),
            "rule_baseline": cls_report(part.label_fault, rules),
            "detection_delay_rf": detection_delay(part, pred),
            "detection_delay_rules": detection_delay(part, rules),
            "time_to_limit": {
                "mae_min_all": round(float(mean_absolute_error(part.minutes_to_limit, m_pred)), 2),
                "mae_min_when_limit_ahead": (
                    round(float(mean_absolute_error(part.minutes_to_limit[near], m_pred[near])), 2)
                    if near.any() else None
                ),
            },
        }
        r = results[name]
        print(f"[{name}] RF macro-F1 {r['random_forest']['macro_f1']:.3f} vs rules "
              f"{r['rule_baseline']['macro_f1']:.3f} · time-to-limit MAE {r['time_to_limit']['mae_min_all']} min")

    importances = sorted(zip(features, clf.feature_importances_), key=lambda x: -x[1])[:12]
    metrics = {
        "dataset": VERSION,
        "data_kind": "SYNTHETIC (UDYAM-TWIN virtual line). Labels = injected ground-truth faults.",
        "split": "by episode (80/20) + out-of-distribution heavier-machine episodes",
        "features": features,
        "classes": CLASSES,
        "results": results,
        "top_features": [{"feature": f, "importance": round(float(v), 4)} for f, v in importances],
        "caveat": "Scores measure how well the model learns OUR simulator. Real-machine accuracy "
                  "must be measured on a pilot (ESP32 / shop-floor data) before any claim.",
        "trained_in_s": round(time.time() - t0, 1),
    }
    METRICS_PATH.write_text(json.dumps(metrics, indent=2))

    know = {
        "dataset": VERSION,
        "episodes_in_distribution": int((eps.ood == 0).sum()),
        "minutes_per_episode": meta["minutes_per_episode"],
        "faults": knowledge(eps),
        "model_summary": {
            k: {"rf_macro_f1": v["random_forest"]["macro_f1"],
                "rules_macro_f1": v["rule_baseline"]["macro_f1"],
                "detection_delay_rf": v["detection_delay_rf"],
                "time_to_limit_mae_min": v["time_to_limit"]["mae_min_when_limit_ahead"]}
            for k, v in results.items()
        },
        "top_features": metrics["top_features"][:6],
    }
    KNOWLEDGE_PATH.write_text(json.dumps(know, indent=2))

    joblib.dump({"version": VERSION, "features": features, "classes": list(clf.classes_),
                 "clf": clf, "reg": reg}, MODEL_PATH, compress=3)
    print(f"Saved {MODEL_PATH.name} ({MODEL_PATH.stat().st_size / 1e6:.1f} MB), "
          f"{METRICS_PATH.name}, {KNOWLEDGE_PATH.name} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
