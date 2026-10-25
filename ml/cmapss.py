"""NASA C-MAPSS turbofan remaining-useful-life (RUL) experiment. A separate research module.

    python -m ml.cmapss            (reads ml/data/cmapss/raw; writes models/cmapss_metrics.json, ~1 min)

Data: NASA Prognostics Data Repository, "CMAPSS Jet Engine Simulated Data" (data.nasa.gov CMAPSSData.zip,
12,425,978 bytes). Simulated turbofan engines run to failure (A. Saxena, K. Goebel, D. Simon, N. Eklund,
"Damage Propagation Modeling for Aircraft Engine Run-to-Failure Simulation", PHM08). Four subsets:
FD001 (1 condition, 1 fault mode), FD002 (6 conditions, 1), FD003 (1, 2), FD004 (6, 2).

Method (fixed before looking at the test results; nothing is tuned on the test engines):
- Target: cycles to failure, capped at 125 (the usual piecewise-linear target: early cycles count as healthy).
- Operating conditions: the three settings are rounded and grouped; each sensor is z-scored per condition with
  training statistics. Sensors with no variance in training are dropped.
- Features per cycle (causal, past cycles only): normalised sensors, 10-cycle rolling mean, 20-cycle slope,
  and the cycle count.
- Models: gradient boosting (HistGradientBoostingRegressor) against two baselines: predict the training mean,
  and linear regression on the same features.
- Test: the official test engines, predicting at their last recorded cycle against RUL_FD00x.txt.
  RMSE against the true RUL as given and capped at 125 (both conventions are used in the literature), and the
  PHM08 score (late predictions cost more than early ones). Plus 5-fold cross-validation grouped by engine.

This is a turbofan simulation, not one of the CNC machines: nothing here feeds the machines' RUL.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.model_selection import GroupKFold

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "ml" / "data" / "cmapss" / "raw"
OUT = ROOT / "models" / "cmapss_metrics.json"
SUBSETS = ("FD001", "FD002", "FD003", "FD004")
DESCRIPTION = {"FD001": "1 condition, HPC degradation", "FD002": "6 conditions, HPC degradation",
               "FD003": "1 condition, HPC + fan degradation", "FD004": "6 conditions, HPC + fan degradation"}
RUL_CAP = 125
ROLL, SLOPE = 10, 20
COLUMNS = ["unit", "cycle", "op1", "op2", "op3"] + [f"s{i}" for i in range(1, 22)]
SENSORS = [f"s{i}" for i in range(1, 22)]


def read(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, sep=r"\s+", header=None, engine="python")
    df = df.iloc[:, :26]
    df.columns = COLUMNS
    return df


def read_rul(path: Path) -> np.ndarray:
    return pd.read_csv(path, sep=r"\s+", header=None).iloc[:, 0].to_numpy(dtype=float)


def nasa_score(pred: np.ndarray, true: np.ndarray) -> float:
    """PHM08 scoring function: d = predicted − true; late (d > 0) costs exp(d/10) − 1, early exp(−d/13) − 1."""
    d = np.asarray(pred, dtype=float) - np.asarray(true, dtype=float)
    return float(np.sum(np.where(d < 0, np.exp(-d / 13.0) - 1.0, np.exp(d / 10.0) - 1.0)))


def rmse(pred: np.ndarray, true: np.ndarray) -> float:
    return float(np.sqrt(np.mean((np.asarray(pred, dtype=float) - np.asarray(true, dtype=float)) ** 2)))


def add_target(train: pd.DataFrame) -> pd.DataFrame:
    last = train.groupby("unit")["cycle"].transform("max")
    return train.assign(rul=(last - train["cycle"]).clip(upper=RUL_CAP))


class Normaliser:
    """Per-operating-condition z-score of the sensors, fitted on training data."""

    def fit(self, train: pd.DataFrame) -> "Normaliser":
        cond = self.condition(train)
        self.n_conditions = int(cond.nunique())
        stats = train[SENSORS].groupby(cond)
        self.mean, self.std = stats.mean(), stats.std().replace(0, np.nan)
        z = self._z(train, cond)
        self.sensors = [s for s in SENSORS if z[s].std(skipna=True) > 1e-6 and z[s].notna().mean() > 0.99]
        return self

    @staticmethod
    def condition(df: pd.DataFrame) -> pd.Series:
        # the six FD002/FD004 flight conditions sit on distinct setting values; rounding groups them
        # (+ 0.0 turns -0.0 into 0.0, so the single sea-level condition is not split in two)
        return (df["op1"].round(0).astype(int).astype(str) + "|" + (df["op2"].round(2) + 0.0).astype(str) + "|"
                + df["op3"].round(0).astype(int).astype(str))

    def _z(self, df: pd.DataFrame, cond: pd.Series) -> pd.DataFrame:
        m, s = self.mean.reindex(cond).to_numpy(), self.std.reindex(cond).to_numpy()
        return pd.DataFrame((df[SENSORS].to_numpy() - m) / s, columns=SENSORS, index=df.index)

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        z = self._z(df, self.condition(df))[self.sensors].fillna(0.0)
        return pd.concat([df[["unit", "cycle"]], z], axis=1)


def features(z: pd.DataFrame, sensors: list[str]) -> pd.DataFrame:
    """Causal per-unit features: value, rolling mean, rolling slope, cycle."""
    parts = [z[["unit", "cycle"]]]
    g = z.groupby("unit", sort=False)
    parts.append(z[sensors])
    parts.append(g[sensors].transform(lambda s: s.rolling(ROLL, min_periods=1).mean()).add_suffix("_mean"))
    c = z["cycle"].astype(float)

    def slope(s: pd.Series) -> pd.Series:
        x = c.loc[s.index]
        return (s.rolling(SLOPE, min_periods=3).cov(x) / x.rolling(SLOPE, min_periods=3).var()).fillna(0.0)

    parts.append(g[sensors].transform(slope).add_suffix("_slope"))
    return pd.concat(parts, axis=1)


def model() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=40,
                                         l2_regularization=1.0, random_state=42)


def run_subset(name: str, raw: Path = RAW) -> dict:
    t0 = time.time()
    train = add_target(read(raw / f"train_{name}.txt"))
    test = read(raw / f"test_{name}.txt")
    true_rul = read_rul(raw / f"RUL_{name}.txt")
    norm = Normaliser().fit(train)
    Xtr = features(norm.transform(train), norm.sensors)
    Xte = features(norm.transform(test), norm.sensors)
    cols = [c for c in Xtr.columns if c != "unit"]
    y = train["rul"].to_numpy(dtype=float)
    last = Xte.groupby("unit").tail(1)  # predict at each test engine's last recorded cycle
    if len(last) != len(true_rul):
        raise ValueError(f"{name}: {len(last)} test engines but {len(true_rul)} RUL values")

    gbr = model().fit(Xtr[cols], y)
    lin = Ridge(alpha=1.0).fit(Xtr[cols], y)
    preds = {"gradient_boosting": np.clip(gbr.predict(last[cols]), 0, None),
             "linear_regression": np.clip(lin.predict(last[cols]), 0, None),
             "predict_training_mean": np.full(len(last), float(y.mean()))}
    capped = np.minimum(true_rul, RUL_CAP)
    test_scores = {k: {"rmse": round(rmse(p, true_rul), 2), "rmse_true_capped_125": round(rmse(p, capped), 2),
                       "nasa_score": round(nasa_score(p, true_rul), 1),
                       "mae": round(float(np.mean(np.abs(p - true_rul))), 2)} for k, p in preds.items()}

    cv = []
    for tr, va in GroupKFold(n_splits=5).split(Xtr, y, groups=Xtr["unit"]):
        m = model().fit(Xtr.iloc[tr][cols], y[tr])
        cv.append(rmse(m.predict(Xtr.iloc[va][cols]), y[va]))

    order = np.argsort(true_rul)
    return {
        "subset": name, "description": DESCRIPTION[name],
        "train_engines": int(train["unit"].nunique()), "test_engines": int(len(true_rul)),
        "operating_conditions": norm.n_conditions, "sensors_used": norm.sensors, "features": len(cols),
        "test": test_scores,
        "cv_rmse_capped_target": {"mean": round(float(np.mean(cv)), 2), "std": round(float(np.std(cv)), 2),
                                  "folds": 5, "note": "all training cycles, capped target, grouped by engine"},
        "predictions": {"true_rul": true_rul[order].astype(int).tolist(),
                        "predicted_rul": np.round(preds["gradient_boosting"][order], 1).tolist()},
        "seconds": round(time.time() - t0, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", type=Path, default=RAW)
    ap.add_argument("--subsets", default=",".join(SUBSETS))
    args = ap.parse_args()
    results = []
    for name in args.subsets.split(","):
        r = run_subset(name, args.raw)
        results.append(r)
        t = r["test"]
        print(f"{name} ({r['description']}): {r['train_engines']} train / {r['test_engines']} test engines, "
              f"{r['operating_conditions']} conditions, {len(r['sensors_used'])} sensors, {r['seconds']} s")
        for k, v in t.items():
            print(f"   {k:22s} RMSE {v['rmse']:6.2f}   RMSE (true capped 125) {v['rmse_true_capped_125']:6.2f}   "
                  f"NASA score {v['nasa_score']:>10,.1f}")
        print(f"   cross-validation RMSE (capped target) {r['cv_rmse_capped_target']['mean']} ± {r['cv_rmse_capped_target']['std']}")
    metrics = {
        "label": "NASA C-MAPSS turbofan simulation: a separate research experiment; it does not feed the CNC machines' RUL",
        "source": "NASA Prognostics Data Repository, CMAPSS Jet Engine Simulated Data (data.nasa.gov CMAPSSData.zip, 12,425,978 bytes)",
        "reference": "A. Saxena, K. Goebel, D. Simon, N. Eklund, Damage Propagation Modeling for Aircraft Engine Run-to-Failure Simulation, PHM08",
        "method": {"target": f"cycles to failure capped at {RUL_CAP}", "normalisation": "z-score per operating condition (training stats)",
                   "features": f"normalised sensors, {ROLL}-cycle rolling mean, {SLOPE}-cycle slope, cycle",
                   "model": "HistGradientBoostingRegressor (fixed settings, not tuned on test)",
                   "baselines": ["predict the training mean", "linear (ridge) regression on the same features"],
                   "evaluation": "official test engines at their last cycle vs RUL_FD00x.txt; PHM08 score; 5-fold grouped CV"},
        "subsets": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(metrics, indent=1), encoding="utf-8")
    print(f"\nWrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
