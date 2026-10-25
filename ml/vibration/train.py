"""Reproducible training pipeline for vibration fault classification.

No vibration dataset ships with this repository, so no vibration model has been
trained. This pipeline is ready for one. Expected layout:

    <dataset>/dataset.json   {"name", "source", "license", "synthetic": bool, "label_definition"}
    <dataset>/manifest.csv   one row per recording:
        recording_id, machine_id, label, sampling_rate_hz, unit, signal_path   (required)
        recorded_at (ISO 8601), rpm, load_pct, machine_type, sensor_range,      (optional)
        group_id  (independence unit for the grouped split; defaults to machine_id)
    <dataset>/<signal_path>  .npy or single-column .csv waveform

    python -m ml.vibration.train --data path/to/dataset [--split auto|machine|time]

Splits keep every recording of one machine (or `group_id`) in one split ("machine"), or order by
`recorded_at` ("time"); random row-level splits are not offered because windows from
the same recording/machine leak between train and test. Preprocessing is fitted on the
training split only; class imbalance is handled with class weights on training data,
so validation and test keep their natural distribution.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ml.vibration.evaluation import class_distribution, classification_metrics
from ml.vibration.features import DEFAULT_BAND_HZ, FEATURE_COLUMNS, extract_features

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
MODEL_FILE = "model.joblib"
METRICS_FILE = "metrics.json"
REQUIRED_COLUMNS = ["recording_id", "machine_id", "label", "sampling_rate_hz", "unit", "signal_path"]
LEAKAGE_COLUMNS = {"recording_id", "machine_id", "group_id", "recorded_at", "signal_path", "label"}
MIN_PER_CLASS = 5
MIN_MACHINES_FOR_GROUP_SPLIT = 3
MAX_MISSING_FRACTION = 0.5
MAX_GROUPS_FOR_LOGO = 12


class DatasetError(Exception):
    pass


def _load_signal(path: Path) -> np.ndarray:
    if path.suffix == ".npy":
        return np.load(path, allow_pickle=False)
    if path.suffix == ".csv":
        return pd.read_csv(path, header=None).iloc[:, 0].to_numpy(dtype=float)
    raise DatasetError(f"Unsupported signal file type: {path.name} (use .npy or .csv)")


def load_dataset(data_dir: Path) -> tuple[dict[str, Any], pd.DataFrame]:
    card_path, manifest_path = data_dir / "dataset.json", data_dir / "manifest.csv"
    if not card_path.exists() or not manifest_path.exists():
        raise DatasetError(f"{data_dir} must contain dataset.json and manifest.csv")
    card = json.loads(card_path.read_text(encoding="utf-8"))
    for key in ("name", "source", "synthetic", "label_definition"):
        if key not in card:
            raise DatasetError(f"dataset.json missing '{key}'")
    if not isinstance(card["synthetic"], bool):
        raise DatasetError("dataset.json 'synthetic' must be true or false")

    df = pd.read_csv(manifest_path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DatasetError(f"manifest.csv missing columns: {missing}")
    if df["recording_id"].duplicated().any():
        raise DatasetError("manifest.csv has duplicate recording_id values")
    if df["label"].isna().any() or (df["label"].astype(str).str.strip() == "").any():
        raise DatasetError("manifest.csv has empty labels; unlabeled recordings cannot be used for supervised training")
    return card, df


def build_feature_table(data_dir: Path, df: pd.DataFrame, band_hz: tuple[float, float]) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    rows, rejected = [], []
    for rec in df.to_dict("records"):
        path = data_dir / str(rec["signal_path"])
        try:
            if not path.exists():
                raise DatasetError(f"signal file not found: {rec['signal_path']}")
            fx = extract_features(
                _load_signal(path), rec["sampling_rate_hz"], str(rec["unit"]), band_hz=band_hz,
                rpm=rec.get("rpm") if pd.notna(rec.get("rpm")) else None,
                load_pct=rec.get("load_pct") if pd.notna(rec.get("load_pct")) else None,
                sensor_range=rec.get("sensor_range") if pd.notna(rec.get("sensor_range")) else None,
            )
        except (DatasetError, ValueError, OSError) as err:
            rejected.append({"recording_id": rec["recording_id"], "reasons": [str(err)]})
            continue
        if not fx.valid:
            rejected.append({"recording_id": rec["recording_id"],
                             "reasons": [i.message for i in fx.issues if i.severity == "error"]})
            continue
        rows.append({
            "recording_id": rec["recording_id"],
            "machine_id": str(rec["machine_id"]),
            "group_id": str(rec["group_id"]) if pd.notna(rec.get("group_id")) else str(rec["machine_id"]),
            "recorded_at": rec.get("recorded_at"),
            "label": str(rec["label"]),
            **fx.features,
        })
    return pd.DataFrame(rows), rejected


def split_indices(table: pd.DataFrame, strategy: str, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    n_machines = table["group_id"].nunique()
    has_time = "recorded_at" in table and table["recorded_at"].notna().all()
    if strategy == "auto":
        strategy = "machine" if n_machines >= MIN_MACHINES_FOR_GROUP_SPLIT else "time" if has_time else ""
        if not strategy:
            raise DatasetError(
                f"Need ≥{MIN_MACHINES_FOR_GROUP_SPLIT} machines for a machine-level split, or recorded_at on every "
                "recording for a time-based split; a random split would leak recordings between sets."
            )
    idx = np.arange(len(table))
    if strategy == "machine":
        if n_machines < MIN_MACHINES_FOR_GROUP_SPLIT:
            raise DatasetError(f"Machine-level split needs ≥{MIN_MACHINES_FOR_GROUP_SPLIT} machines, found {n_machines}")
        groups = table["group_id"].to_numpy()
        outer = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=seed)
        trval, test = next(outer.split(idx, groups=groups))
        inner = GroupShuffleSplit(n_splits=1, test_size=0.25, random_state=seed)
        tr, va = next(inner.split(trval, groups=groups[trval]))
        return trval[tr], trval[va], test, "machine"
    if strategy == "time":
        if not has_time:
            raise DatasetError("Time-based split needs recorded_at on every recording")
        order = np.argsort(pd.to_datetime(table["recorded_at"], utc=True).to_numpy())
        n = len(order)
        n_test, n_val = max(1, int(round(0.2 * n))), max(1, int(round(0.2 * n)))
        return order[: n - n_test - n_val], order[n - n_test - n_val: n - n_test], order[n - n_test:], "time"
    raise DatasetError(f"Unknown split strategy '{strategy}'")


def _candidates(seed: int) -> dict[str, Any]:
    pre = [("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]
    return {
        "majority_class_baseline": Pipeline(pre + [("clf", DummyClassifier(strategy="most_frequent"))]),
        "logistic_regression_baseline": Pipeline(pre + [("clf", LogisticRegression(class_weight="balanced", max_iter=2000, random_state=seed))]),
        "random_forest": Pipeline(pre + [("clf", RandomForestClassifier(
            n_estimators=300, class_weight="balanced_subsample", min_samples_leaf=2, random_state=seed, n_jobs=-1))]),
    }


def train(
    data_dir: Path,
    out_dir: Path = ARTIFACT_DIR,
    *,
    split: str = "auto",
    seed: int = 42,
    band_hz: tuple[float, float] = DEFAULT_BAND_HZ,
    allow_synthetic: bool = False,
) -> dict[str, Any]:
    card, manifest = load_dataset(data_dir)
    if card["synthetic"] and not allow_synthetic:
        raise DatasetError("Dataset is marked synthetic; refusing to produce a model artifact (use allow_synthetic only for pipeline tests)")

    table, rejected = build_feature_table(data_dir, manifest, band_hz)
    if table.empty:
        raise DatasetError(f"No usable recordings ({len(rejected)} rejected)")
    dist = class_distribution(table["label"])
    if len(dist) < 2:
        raise DatasetError(f"Need ≥2 classes, found {dist}")
    small = {k: v for k, v in dist.items() if v < MIN_PER_CLASS}
    if small:
        raise DatasetError(f"Classes with fewer than {MIN_PER_CLASS} usable recordings: {small}")

    tr, va, te, used_split = split_indices(table, split, seed)
    for name, part in (("train", tr), ("validation", va), ("test", te)):
        missing = set(dist) - set(table["label"].iloc[part])
        if missing:
            raise DatasetError(f"{name} split has no recordings of {sorted(missing)}; collect more machines/recordings or use --split time")

    train_missing = table.iloc[tr][FEATURE_COLUMNS].isna().mean()
    features = [c for c in FEATURE_COLUMNS if train_missing[c] <= MAX_MISSING_FRACTION and c not in LEAKAGE_COLUMNS]
    if not features:
        raise DatasetError("No feature is populated in the training split")
    X, y = table[features].astype(float), table["label"].to_numpy()
    labels = sorted(dist)

    results: dict[str, Any] = {}
    fitted: dict[str, Any] = {}
    for name, pipe in _candidates(seed).items():
        pipe.fit(X.iloc[tr], y[tr])
        fitted[name] = pipe
        results[name] = {
            "validation": classification_metrics(y[va], pipe.predict(X.iloc[va]), labels),
            "test": classification_metrics(y[te], pipe.predict(X.iloc[te]), labels),
        }
    # One grouped split is a noisy estimate with few groups: also report leave-one-group-out.
    logo: dict[str, Any] = {}
    groups = table["group_id"].to_numpy()
    if used_split == "machine" and len(set(groups)) <= MAX_GROUPS_FOR_LOGO:
        for name in results:
            per_group = {}
            for g in sorted(set(groups)):
                tr_g, te_g = groups != g, groups == g
                if len(set(y[tr_g])) < len(labels):
                    continue
                pipe = _candidates(seed)[name].fit(X[tr_g], y[tr_g])
                per_group[g] = classification_metrics(y[te_g], pipe.predict(X[te_g]), labels)
            f1s = [m["macro_f1"] for m in per_group.values()]
            if f1s:
                logo[name] = {"per_group": per_group, "macro_f1_mean": round(float(np.mean(f1s)), 4),
                              "macro_f1_std": round(float(np.std(f1s)), 4), "macro_f1_min": round(float(np.min(f1s)), 4)}

    selectable = [k for k in results if k != "majority_class_baseline"]
    best = max(selectable, key=lambda k: results[k]["validation"]["macro_f1"])

    metadata = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": {k: card.get(k) for k in ("name", "source", "license", "synthetic", "label_definition", "description",
                                             "units_note", "preprocessing", "limitations")},
        "target": "label (multi-class fault / condition per recording)",
        "labels": labels,
        "feature_columns": features,
        "excluded_columns": sorted(LEAKAGE_COLUMNS),
        "training_missing_fraction": {c: round(float(train_missing[c]), 4) for c in features},
        "band_hz": list(band_hz),
        "input_spec": {
            "sampling_rate_hz": sorted({float(v) for v in manifest["sampling_rate_hz"]}),
            "machine_types": sorted({str(v) for v in manifest["machine_type"].dropna()}) if "machine_type" in manifest else [],
        },
        "seed": seed,
        "split": {
            "strategy": used_split,
            "sizes": {"train": len(tr), "validation": len(va), "test": len(te)},
            "group_column": "group_id" if "group_id" in manifest.columns else "machine_id",
            "group_definition": card.get("group_definition", "machine_id"),
            "machines": {
                "train": sorted(table["group_id"].iloc[tr].unique().tolist()),
                "validation": sorted(table["group_id"].iloc[va].unique().tolist()),
                "test": sorted(table["group_id"].iloc[te].unique().tolist()),
            },
            "class_distribution": {
                "train": class_distribution(y[tr]), "validation": class_distribution(y[va]), "test": class_distribution(y[te]),
            },
        },
        "selected_model": best,
        "selection_criterion": "highest validation macro F1 (majority baseline excluded); test set used only for reporting",
        "imbalance_handling": "class weights computed on the training split; validation/test distributions unchanged",
        "probability_calibrated": False,
        "rejected_recordings": rejected,
        "sklearn_version": sklearn.__version__,
    }
    report = {**metadata, "results": results, "leave_one_group_out": logo}

    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump({"pipeline": fitted[best], "metadata": metadata}, out_dir / MODEL_FILE)
    (out_dir / METRICS_FILE).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Train a vibration fault classifier from labelled recordings.")
    ap.add_argument("--data", type=Path, help="dataset directory with dataset.json + manifest.csv")
    ap.add_argument("--out", type=Path, default=ARTIFACT_DIR)
    ap.add_argument("--split", choices=["auto", "machine", "time"], default="auto")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--band", type=float, nargs=2, default=list(DEFAULT_BAND_HZ), metavar=("LOW_HZ", "HIGH_HZ"))
    args = ap.parse_args(argv)
    if args.data is None:
        print("No vibration dataset given. None ships with this repository, so no vibration model is trained.\n"
              "Provide labelled recordings (see the module docstring) with --data.", file=sys.stderr)
        return 2
    try:
        rep = train(args.data, args.out, split=args.split, seed=args.seed, band_hz=tuple(args.band))
    except DatasetError as err:
        print(f"Training refused: {err}", file=sys.stderr)
        return 1
    sel = rep["selected_model"]
    print(f"Selected {sel}: test macro F1 {rep['results'][sel]['test']['macro_f1']} "
          f"(split={rep['split']['strategy']}, sizes={rep['split']['sizes']}) → {args.out / MODEL_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
