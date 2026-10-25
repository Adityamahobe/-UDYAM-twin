"""Train and evaluate on the IMS run-to-failure features (ml/vibration/datasets/ims.py output).

    python -m ml.vibration.train_ims          (reads ml/vibration/data/ims/features.csv)

What it reports (ml/vibration/artifacts/ims_metrics.json), all from the data:
1. ISO 20816-3 zone progression per bearing: hours spent in each zone, first time each zone is reached,
   and the zone the bearing ends in (demo Group 2 rigid boundaries; not a certified assessment).
2. Early warning "this bearing fails within the next H hours" (default 24 h): a Random Forest on
   features normalised to each bearing's own healthy baseline, evaluated leave-one-test-out (train on two
   tests, test on the third), against two rules: zone ≥ C, and velocity ≥ 2 × the bearing's baseline.
   Reported: precision, recall, F1, false-alarm rate on bearings that did not fail, and lead time
   (hours before the end of the test at which each method's alarm first holds for 3 snapshots).
3. RUL check on the bearings that failed: hours to the end of the test, capped, leave-one-bearing-out.
   With only four failed bearings this is an indication, not a validated RUL model.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestClassifier
from sklearn.metrics import f1_score, mean_absolute_error, precision_score, recall_score

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "ims" / "features.csv"
OUT = ROOT / "artifacts" / "ims_metrics.json"
TRENDS = ROOT / "artifacts" / "ims_trends.json"
BASELINE_FRACTION = 0.10   # first 10 % of each test = the bearing's healthy baseline
ROLL = 6                   # snapshots in the rolling mean
SLOPE = 12                 # snapshots in the trend slope
PERSIST = 3                # alarm must hold for this many snapshots
RUL_CAP_H = 100.0
STOPPED_MM_S = 0.1         # every bearing below this in one snapshot = the rig was stopped, not healthy
ZONES = ("A", "B", "C", "D")
MODEL_FEATURES = ["vel_ratio", "vel_roll_ratio", "vel_log_slope", "accel_ratio", "kurtosis", "kurt_ratio",
                  "crest_factor", "disp_ratio"]


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for (_, _), g in df.sort_values("hours").groupby(["test", "bearing"]):
        g = g.copy()
        base = g[g["hours"] <= g["hours"].max() * BASELINE_FRACTION]
        b = {k: float(base[k].median()) for k in ("velocity_rms_mm_s", "accel_rms_m_s2", "kurtosis", "disp_pp_um")}
        g["vel_ratio"] = g["velocity_rms_mm_s"] / b["velocity_rms_mm_s"]
        g["vel_roll_ratio"] = g["velocity_rms_mm_s"].rolling(ROLL, min_periods=1).mean() / b["velocity_rms_mm_s"]
        logv = np.log(g["velocity_rms_mm_s"].clip(lower=1e-6))
        x = g["hours"]
        g["vel_log_slope"] = (logv.rolling(SLOPE, min_periods=3).cov(x) / x.rolling(SLOPE, min_periods=3).var()).fillna(0.0)
        g["accel_ratio"] = g["accel_rms_m_s2"] / b["accel_rms_m_s2"]
        g["kurt_ratio"] = g["kurtosis"] / b["kurtosis"]
        g["disp_ratio"] = g["disp_pp_um"] / b["disp_pp_um"]
        g["baseline_vel_mm_s"] = b["velocity_rms_mm_s"]
        out.append(g)
    df = pd.concat(out, ignore_index=True)
    num = df.select_dtypes("number").columns
    df[num] = df[num].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    df["zone"] = df["zone"].fillna("")  # invalid reading: sorts below "A", so it never raises a zone alarm
    return df


def zone_progression(df: pd.DataFrame) -> list[dict]:
    rows = []
    for (test, bearing), g in df.sort_values("hours").groupby(["test", "bearing"]):
        dt = g["hours"].diff().fillna(0.0).to_numpy()
        hours_in = {z: round(float(dt[(g["zone"] == z).to_numpy()].sum()), 1) for z in ZONES}
        first = {z: (round(float(g.loc[g["zone"] >= z, "hours"].min()), 1) if (g["zone"] >= z).any() else None) for z in ZONES[1:]}
        rows.append({"test": int(test), "bearing": int(bearing), "failed": bool(g["failed"].iloc[0]),
                     "failure_mode": g["failure_mode"].iloc[0] if g["failed"].iloc[0] else None,
                     "duration_h": round(float(g["hours"].max()), 1),
                     "baseline_vel_mm_s": round(float(g["baseline_vel_mm_s"].iloc[0]), 4),
                     "max_vel_mm_s": round(float(g["velocity_rms_mm_s"].max()), 4),
                     "final_vel_mm_s": round(float(g["velocity_rms_mm_s"].iloc[-1]), 4),
                     "final_zone": g["zone"].iloc[-1], "hours_in_zone": hours_in, "first_reached_h": first})
    return rows


def _persistent(alarm: np.ndarray) -> np.ndarray:
    """True from the snapshot where the alarm has held PERSIST times in a row."""
    run = np.zeros(len(alarm), dtype=int)
    for i, a in enumerate(alarm):
        run[i] = run[i - 1] + 1 if a and i else int(a)
    return run >= PERSIST


def _lead_times(df: pd.DataFrame, alarm: np.ndarray) -> list[dict]:
    out = []
    df = df.assign(_alarm=alarm)
    for (test, bearing), g in df.groupby(["test", "bearing"]):
        p = _persistent(g["_alarm"].to_numpy())
        first = g["hours_to_end"].to_numpy()[p][0] if p.any() else None
        out.append({"test": int(test), "bearing": int(bearing), "failed": bool(g["failed"].iloc[0]),
                    "first_alarm_hours_before_end": None if first is None else round(float(first), 1)})
    return out


def _scores(y: np.ndarray, pred: np.ndarray, df: pd.DataFrame) -> dict:
    healthy = ~df["failed"].to_numpy()
    return {"precision": round(float(precision_score(y, pred, zero_division=0)), 3),
            "recall": round(float(recall_score(y, pred, zero_division=0)), 3),
            "f1": round(float(f1_score(y, pred, zero_division=0)), 3),
            "false_alarm_rate_healthy_bearings": round(float(pred[healthy].mean()), 4) if healthy.any() else None,
            "positives": int(y.sum()), "snapshots": int(len(y))}


def early_warning(df: pd.DataFrame, horizon_h: float) -> dict:
    df = df.sort_values(["test", "bearing", "hours"]).reset_index(drop=True)
    y = (df["failed"] & (df["hours_to_end"] <= horizon_h)).to_numpy()
    rules = {"zone_c_or_worse": (df["zone"] >= "C").to_numpy(),
             "velocity_2x_baseline": (df["vel_roll_ratio"] >= 2.0).to_numpy()}
    model_pred = np.zeros(len(df), dtype=bool)
    folds = []
    for test in sorted(df["test"].unique()):
        tr, te = df["test"] != test, df["test"] == test
        if y[tr.to_numpy()].sum() == 0:
            continue
        clf = RandomForestClassifier(n_estimators=300, min_samples_leaf=3, class_weight="balanced", random_state=42, n_jobs=-1)
        clf.fit(df.loc[tr, MODEL_FEATURES], y[tr.to_numpy()])
        model_pred[te.to_numpy()] = clf.predict(df.loc[te, MODEL_FEATURES]).astype(bool)
        folds.append({"held_out_test": int(test), **_scores(y[te.to_numpy()], model_pred[te.to_numpy()], df[te])})
    final = RandomForestClassifier(n_estimators=300, min_samples_leaf=3, class_weight="balanced", random_state=42, n_jobs=-1)
    final.fit(df[MODEL_FEATURES], y)
    importances = sorted(zip(MODEL_FEATURES, final.feature_importances_), key=lambda kv: -kv[1])
    return {
        "horizon_h": horizon_h, "validation": "leave-one-test-out (train on two tests, evaluate on the third)",
        "random_forest": {"overall": _scores(y, model_pred, df), "folds": folds, "lead_times": _lead_times(df, model_pred),
                          "feature_importance": [{"feature": f, "importance": round(float(v), 4)} for f, v in importances]},
        "rules": {name: {"overall": _scores(y, pred, df), "lead_times": _lead_times(df, pred)} for name, pred in rules.items()},
    }


def rul_check(df: pd.DataFrame) -> dict:
    f = df[df["failed"]].copy()
    f["rul_h"] = f["hours_to_end"].clip(upper=RUL_CAP_H)
    keys = sorted({(int(t), int(b)) for t, b in f[["test", "bearing"]].itertuples(index=False)})
    folds = []
    for t, b in keys:
        te = (f["test"] == t) & (f["bearing"] == b)
        model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=42)
        model.fit(f.loc[~te, MODEL_FEATURES], f.loc[~te, "rul_h"])
        pred = np.clip(model.predict(f.loc[te, MODEL_FEATURES]), 0, RUL_CAP_H)
        mean_pred = np.full(int(te.sum()), float(f.loc[~te, "rul_h"].mean()))
        last = te & (f["hours_to_end"] <= RUL_CAP_H)
        folds.append({"test": t, "bearing": b,
                      "mae_h": round(float(mean_absolute_error(f.loc[te, "rul_h"], pred)), 2),
                      "mae_last_100h": round(float(mean_absolute_error(
                          f.loc[last, "rul_h"], np.clip(model.predict(f.loc[last, MODEL_FEATURES]), 0, RUL_CAP_H))), 2)
                      if last.any() else None,
                      "baseline_mean_mae_h": round(float(mean_absolute_error(f.loc[te, "rul_h"], mean_pred)), 2)})
    return {"target": f"hours to the end of the test, capped at {RUL_CAP_H:g} h", "validation": "leave-one-failed-bearing-out",
            "failed_bearings": len(keys), "folds": folds,
            "note": "Only four run-to-failure bearings: an indication of feasibility, not a validated RUL model."}


def trends(df: pd.DataFrame, points: int = 300) -> dict:
    out = {}
    for (test, bearing), g in df.sort_values("hours").groupby(["test", "bearing"]):
        step = max(1, len(g) // points)
        s = g.iloc[::step]
        out[f"T{int(test)}-B{int(bearing)}"] = {"failed": bool(g["failed"].iloc[0]), "hours": s["hours"].round(2).tolist(),
                                                "velocity_rms_mm_s": s["velocity_rms_mm_s"].round(4).tolist(),
                                                "zone": s["zone"].tolist()}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--horizon-h", type=float, default=24.0)
    args = ap.parse_args()
    raw = pd.read_csv(args.data, low_memory=False)
    stopped = raw.groupby(["test", "file"])["velocity_rms_mm_s"].transform("max") < STOPPED_MM_S
    dropped = raw[stopped].groupby("test")["file"].nunique().to_dict()
    df = add_features(raw[~stopped])
    meta_path = args.data.parent / "features_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    metrics = {
        "label": "IMS bearing run-to-failure (NASA Prognostics Data Repository); test rig, not a CNC machine",
        "dataset": {k: meta.get(k) for k in ("source", "credit", "sampling_rate_hz", "rpm", "band_hz", "zone_profile",
                                              "zone_boundaries_mm_s", "zone_note", "direction_rule", "snapshots_per_test")},
        "zone_progression": zone_progression(df),
        "early_warning": early_warning(df, args.horizon_h),
        "rul_check": rul_check(df),
        "features": MODEL_FEATURES,
        "excluded_snapshots": {"rule": f"every bearing below {STOPPED_MM_S} mm/s (rig stopped)",
                               "per_test": {str(int(k)): int(v) for k, v in dropped.items()}},
        "method": {"baseline": f"median of the first {int(BASELINE_FRACTION * 100)} % of each bearing's test",
                   "rolling_mean_snapshots": ROLL, "slope_snapshots": SLOPE, "alarm_persistence_snapshots": PERSIST},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(metrics, indent=1), encoding="utf-8")
    TRENDS.write_text(json.dumps(trends(df)), encoding="utf-8")
    ew = metrics["early_warning"]
    print("Zone progression (ISO 20816-3 demo Group 2 rigid):")
    for z in metrics["zone_progression"]:
        print(f"  T{z['test']}-B{z['bearing']} {'FAILED ' + str(z['failure_mode']) if z['failed'] else 'ok':24s} "
              f"baseline {z['baseline_vel_mm_s']:.3f} → max {z['max_vel_mm_s']:.3f} mm/s, final zone {z['final_zone']}, first C/D at {z['first_reached_h']['C']}/{z['first_reached_h']['D']} h")
    print(f"\nEarly warning (fails within {args.horizon_h:g} h), leave-one-test-out:")
    for name, r in [("random_forest", ew["random_forest"])] + list(ew["rules"].items()):
        o = r["overall"]
        leads = [lt["first_alarm_hours_before_end"] for lt in r["lead_times"] if lt["failed"]]
        print(f"  {name:22s} P {o['precision']:.2f}  R {o['recall']:.2f}  F1 {o['f1']:.2f}  false alarms {o['false_alarm_rate_healthy_bearings']}  lead h {leads}")
    print("\nRUL check:", [(f["test"], f["bearing"], f["mae_h"], f["baseline_mean_mae_h"]) for f in metrics["rul_check"]["folds"]])
    print(f"\nWrote {OUT} and {TRENDS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
