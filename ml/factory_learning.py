"""Factory learning loop: three models trained on factory datasets (ml/factory_dataset.py), each gated and versioned.

  fault   Fault diagnosis. Which fault a machine has, from its 60 s feature window, including the compressor's
          pressure drop seen on the CNCs (a common cause). The cause machine follows from the label: COMP-01 for
          pressure_drop on a CNC, otherwise the machine itself.
  ttf     Time to limit. Minutes until a machine's true temperature reaches its limit or its vibration reaches zone D
          (capped at 30 min), and whether that happens within 10 min.
  rl      RL maintenance policy. The current Q-learning policy (ml/maintenance_rl.py) fine-tuned on the fault
          scenarios of the dataset's training runs, in the same VMC + compressor environment.

Held-out evaluation. Every dataset is already split by whole run (train / test). Models learn only from train runs and
are measured only on test runs. The RL policy trains on the training runs' scenarios and is evaluated on the test runs'
scenarios with environment seeds never used in training.

Gate. Criteria are fixed in GATES before anything is trained. A candidate that fails is registered as
rejected_by_gate and can never be deployed. A candidate that passes is only eligible: an operator promotes it
(api/app.py, Testing mode only). Rollback to an earlier production version is always available.

Registries (ml/model_registry.py): models/registry/factory_fault, factory_ttf, factory_rl. The fault and ttf
registries start empty; the RL registry registers the shipped policy (models/rl_policy.json) as v001, the
current policy.

Everything here is trained and tested on SYNTHETIC DIGITAL TWIN DATA. None of it has learned from real machines.
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, precision_recall_fscore_support

from ml import factory_dataset, maintenance_rl as mrl
from ml.model_registry import ModelRegistry
from tools.generate_dataset import FEATURES, HORIZON_MIN, WINDOW_S, window_features

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_ROOT = ROOT / "models" / "registry"
DATA_LABEL = factory_dataset.DATA_LABEL
TEMPLATES = ("cnc_mill", "cnc_lathe", "bar_saw", "air_compressor")
INPUTS = FEATURES + [f"is_{t}" for t in TEMPLATES]
TRAIN_N_JOBS = 2                 # leaves cores for the live engine when training inside the server
SEED = 42
KINDS = {"fault": "Fault diagnosis", "ttf": "Time to limit", "rl": "RL maintenance policy"}
COMMON_CAUSE = {"pressure_drop": "COMP-01"}
# dataset fault on a CNC (or the compressor) -> the RL environment's scenario
RL_SCENARIO = {"bearing_defect": "vibration_fault", "coolant_degradation": "over_temperature",
               "tool_wear_accel": "tool_wear", "overload": "overload", "pressure_drop": "air_pressure_drop"}
RL_SHIFT_DECISIONS = 36          # a 12-minute training shift of 20 s decisions (as ml/train_rl.py)
RL_LAST_ONSET = 24               # a fault starts by decision 24 so its effects fall inside the shift
RL_EVAL_SEEDS = 5                # held-out environment seeds per test scenario
RL_EVAL_SEED0 = 7_000_000        # training uses 3_000_000+ (and ml/train_rl.py 1_000_000+); evaluation never overlaps

# Fixed before any candidate is trained.
GATES: dict[str, dict[str, float]] = {
    "fault": {
        "min_test_windows_per_fault": 10,     # a fault is gated on only when the test runs hold enough of it
        "macro_f1_min": 0.6,                  # macro F1 over the labels in the test runs
        "macro_f1_vs_majority_min_delta": 0.2,  # clearly better than always answering "normal"
        "fault_recall_min": 0.5,              # every gated fault: at least half of its windows recognised
        "macro_f1_vs_production_min_delta": -0.02,  # not worse than the production model on the same rows
    },
    "ttf": {
        "min_test_positives": 10,             # test windows within 10 min of a limit
        "min_train_positives": 20,
        "within_10min_recall_min": 0.5,       # half of the windows really within 10 min of a limit are flagged
        "within_10min_precision_min": 0.3,
        "mae_vs_twin_forecast_max_ratio": 1.0,  # MAE (rows that reach a limit) no worse than the twin's own forecast
        "within_10min_f1_vs_production_min_delta": -0.02,
    },
    "rl": {
        "min_train_scenarios": 10,
        "min_test_fault_scenarios": 3,
        "return_vs_production_min_delta_inr": 0.0,   # mean ₹ per shift on held-out scenarios: at least the current policy
        "return_vs_rules_min_delta_inr": 0.0,        # and at least the rule baseline
        "trips_vs_production_max_delta": 0.02,       # trips (unplanned breakdowns) per shift may not rise
    },
}


class LearningError(ValueError):
    pass


def registries(root: Path = REGISTRY_ROOT) -> dict[str, ModelRegistry]:
    return {
        "fault": ModelRegistry(root / "factory_fault", None, None, name="factory_fault"),
        "ttf": ModelRegistry(root / "factory_ttf", None, None, name="factory_ttf"),
        "rl": ModelRegistry(root / "factory_rl", mrl.POLICY_PATH, mrl.METRICS_PATH, artifact="policy.json",
                            name="factory_rl", baseline_card={
                                "trained_on": ["Random VMC and compressor fault scenarios (ml/train_rl.py), simulation only"],
                                "summary": "The current RL maintenance policy shipped with UDYAM-TWIN (models/rl_policy.json)."}),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_windows(store: factory_dataset.FactoryDatasetStore, dataset_ids: list[str]) -> tuple[pd.DataFrame, list[dict]]:
    """Windows of the chosen datasets with their run-level split. Refuses anything not labelled synthetic or altered."""
    if not dataset_ids:
        raise LearningError("choose at least one factory dataset")
    frames, used = [], []
    for ds in dict.fromkeys(dataset_ids):
        try:
            meta = store.metadata(ds)
        except (FileNotFoundError, factory_dataset.DatasetError):
            raise LearningError(f"factory dataset {ds} not found")
        if meta.get("data_label") != DATA_LABEL or meta.get("synthetic") is not True:
            raise LearningError(f"{ds} is not labelled {DATA_LABEL}; refusing to train on it")
        path = store.file(ds, "windows")
        if _sha256(path) != meta["files"]["windows.csv"]["sha256"]:
            raise LearningError(f"{ds}/windows.csv does not match the checksum in its metadata")
        df = pd.read_csv(path, low_memory=False, dtype={"label_cause_machine": str, "label_severity": float})
        if (df["data_label"] != DATA_LABEL).any():
            raise LearningError(f"{ds} has rows not labelled {DATA_LABEL}")
        overlap = set(meta["split"]["train"]) & set(meta["split"]["test"])
        if overlap:
            raise LearningError(f"{ds}: runs {sorted(overlap)} are in both train and test")
        df["group"] = ds + ":" + df["run_id"]
        frames.append(df)
        used.append({"dataset_id": ds, "name": meta.get("name"), "data_label": meta["data_label"],
                     "train_runs": len(meta["split"]["train"]), "test_runs": len(meta["split"]["test"]),
                     "windows": int(len(df)), "sha256": meta["files"]["windows.csv"]["sha256"]})
    df = pd.concat(frames, ignore_index=True)
    for t in TEMPLATES:
        df[f"is_{t}"] = (df["template"] == t).astype(float)
    df["label_fault"] = df["label_fault"].fillna("normal")
    return df, used


def _split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    train, test = df[df["split"] == "train"], df[df["split"] == "test"]
    if set(train["group"]) & set(test["group"]):
        raise LearningError("a run is in both train and test")
    if train.empty or test.empty:
        raise LearningError("the datasets need both train and test runs (set a test share above 0 when collecting)")
    return train, test


def _check(name: str, value: float, op: str, limit: float, detail: str) -> dict[str, Any]:
    ok = value >= limit if op == ">=" else value <= limit
    return {"check": name, "passed": bool(ok), "value": round(float(value), 4), "rule": f"{op} {limit:g}", "detail": detail}


# ----------------------------------------------------------------------------------------------- fault diagnosis
def _fault_scores(y: np.ndarray, pred: np.ndarray, labels: list[str], machines: np.ndarray) -> dict[str, Any]:
    p, r, f, s = precision_recall_fscore_support(y, pred, labels=labels, zero_division=0)
    present = [i for i, lab in enumerate(labels) if s[i] > 0]
    faulty = y != "normal"
    true_cause = np.where(np.isin(y, list(COMMON_CAUSE)), [COMMON_CAUSE.get(v, "") for v in y], machines)
    pred_cause = np.where(np.isin(pred, list(COMMON_CAUSE)), [COMMON_CAUSE.get(v, "") for v in pred], machines)
    return {
        "rows": int(len(y)), "accuracy": round(float(accuracy_score(y, pred)), 4),
        "macro_f1": round(float(np.mean([f[i] for i in present])), 4) if present else 0.0,
        "per_label": {labels[i]: {"precision": round(float(p[i]), 4), "recall": round(float(r[i]), 4),
                                  "f1": round(float(f[i]), 4), "support": int(s[i])} for i in range(len(labels))},
        "confusion_matrix": {"labels": labels, "rows_true_cols_predicted":
                             confusion_matrix(y, pred, labels=labels).tolist()},
        # among windows that really have a fault: is the right fault named, and the right machine blamed?
        "fault_windows": int(faulty.sum()),
        "fault_named_correctly": round(float((pred[faulty] == y[faulty]).mean()), 4) if faulty.any() else None,
        "cause_machine_correct": round(float(((pred_cause == true_cause) & (pred != "normal"))[faulty].mean()), 4)
        if faulty.any() else None,
    }


def train_fault(train: pd.DataFrame, test: pd.DataFrame, prod: dict | None, seed: int) -> tuple[Any, dict, dict]:
    labels = sorted(set(train["label_fault"]) | set(test["label_fault"]))
    model = RandomForestClassifier(n_estimators=150, min_samples_leaf=2, class_weight="balanced_subsample",
                                   n_jobs=TRAIN_N_JOBS, random_state=seed).fit(train[INPUTS], train["label_fault"])
    y, machines = test["label_fault"].to_numpy(), test["machine_id"].to_numpy()
    results = {"candidate": _fault_scores(y, model.predict(test[INPUTS]), labels, machines),
               "majority_baseline": _fault_scores(y, np.full(len(y), "normal", dtype=object), labels, machines)}
    if prod is not None:
        results["production"] = {"version": prod["version"],
                                 **_fault_scores(y, prod["model"].predict(test[prod["inputs"]]), labels, machines)}
    g, c = GATES["fault"], results["candidate"]
    test_counts = test["label_fault"].value_counts()
    gated = [f for f in labels if f != "normal" and test_counts.get(f, 0) >= g["min_test_windows_per_fault"]]
    unseen = [f for f in labels if f != "normal" and f not in set(train["label_fault"])]
    checks = [
        {"check": "faults_in_test", "passed": bool(gated), "value": ", ".join(gated) or "none",
         "rule": f">= 1 fault with >= {g['min_test_windows_per_fault']:g} held-out windows",
         "detail": "Enough held-out windows of at least one fault to measure diagnosis"},
        _check("macro_f1", c["macro_f1"], ">=", g["macro_f1_min"], "Macro F1 on the held-out runs"),
        _check("macro_f1_vs_majority", c["macro_f1"] - results["majority_baseline"]["macro_f1"], ">=",
               g["macro_f1_vs_majority_min_delta"], "Macro F1, candidate − always 'normal'"),
    ]
    for f in gated:
        checks.append(_check(f"recall_{f}", c["per_label"][f]["recall"], ">=", g["fault_recall_min"],
                             f"Share of held-out '{f.replace('_', ' ')}' windows recognised"))
    if unseen:
        checks.append({"check": "faults_never_trained", "passed": True, "skipped": True, "value": ", ".join(unseen),
                       "rule": "reported", "detail": "In the test runs only: the model cannot know them (not gated)"})
    if prod is not None:
        checks.append(_check("macro_f1_vs_production", c["macro_f1"] - results["production"]["macro_f1"], ">=",
                             g["macro_f1_vs_production_min_delta"], f"Macro F1, candidate − production {prod['version']} on the same rows"))
    else:
        checks.append({"check": "macro_f1_vs_production", "passed": True, "skipped": True, "value": "no production model",
                       "rule": "reported", "detail": "First version: nothing in production to compare with"})
    summary = {"macro_f1": c["macro_f1"], "accuracy": c["accuracy"], "cause_machine_correct": c["cause_machine_correct"]}
    return model, results | {"labels": labels}, {"checks": checks, "summary": summary}


# ----------------------------------------------------------------------------------------------- time to limit
def _ttf_scores(true_min: np.ndarray, pred_min: np.ndarray) -> dict[str, Any]:
    reach = true_min < HORIZON_MIN
    pos, flag = true_min < 10.0, pred_min < 10.0
    tp, fp, fn = int((pos & flag).sum()), int((~pos & flag).sum()), int((pos & ~flag).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {"rows": int(len(true_min)), "mae_min": round(float(np.abs(pred_min - true_min).mean()), 3),
            "rows_reaching_limit": int(reach.sum()),
            "mae_reaching_limit_min": round(float(np.abs(pred_min - true_min)[reach].mean()), 3) if reach.any() else None,
            "within_10min": {"positives": int(pos.sum()), "precision": round(prec, 4), "recall": round(rec, 4),
                             "f1": round(2 * prec * rec / (prec + rec), 4) if prec + rec else 0.0,
                             "true_positives": tp, "false_positives": fp, "false_negatives": fn}}


def train_ttf(train: pd.DataFrame, test: pd.DataFrame, prod: dict | None, seed: int) -> tuple[Any, dict, dict]:
    model = RandomForestRegressor(n_estimators=150, min_samples_leaf=3, n_jobs=TRAIN_N_JOBS,
                                  random_state=seed).fit(train[INPUTS], train["minutes_to_limit"])
    y = test["minutes_to_limit"].to_numpy(dtype=float)
    results = {"candidate": _ttf_scores(y, np.clip(model.predict(test[INPUTS]), 0, HORIZON_MIN)),
               # the twin's own physics forecast: minutes until its temperature warning (feature twin_forecast_min)
               "twin_forecast_baseline": _ttf_scores(y, test["twin_forecast_min"].to_numpy(dtype=float)),
               "always_censored_baseline": _ttf_scores(y, np.full(len(y), HORIZON_MIN))}
    if prod is not None:
        results["production"] = {"version": prod["version"], **_ttf_scores(
            y, np.clip(prod["model"].predict(test[prod["inputs"]]), 0, HORIZON_MIN))}
    g, c, tw = GATES["ttf"], results["candidate"], results["twin_forecast_baseline"]
    n_train_pos = int((train["minutes_to_limit"] < 10).sum())
    checks = [
        {"check": "positives", "passed": c["within_10min"]["positives"] >= g["min_test_positives"]
         and n_train_pos >= g["min_train_positives"],
         "value": f"train {n_train_pos}, test {c['within_10min']['positives']} windows within 10 min of a limit",
         "rule": f">= {g['min_train_positives']:g} train and {g['min_test_positives']:g} test",
         "detail": "Enough windows near a limit to learn from and to measure (add runs with faults that heat or shake a machine)"},
        _check("within_10min_recall", c["within_10min"]["recall"], ">=", g["within_10min_recall_min"],
               "Held-out windows within 10 min of a limit that are flagged"),
        _check("within_10min_precision", c["within_10min"]["precision"], ">=", g["within_10min_precision_min"],
               "Flags that are right"),
    ]
    if c["mae_reaching_limit_min"] is not None and tw["mae_reaching_limit_min"]:
        checks.append(_check("mae_vs_twin_forecast", c["mae_reaching_limit_min"] / tw["mae_reaching_limit_min"], "<=",
                             g["mae_vs_twin_forecast_max_ratio"],
                             "Error in minutes (windows that reach a limit), candidate ÷ the twin's physics forecast"))
    if prod is not None:
        checks.append(_check("within_10min_f1_vs_production", c["within_10min"]["f1"] - results["production"]["within_10min"]["f1"],
                             ">=", g["within_10min_f1_vs_production_min_delta"],
                             f"Within-10-min F1, candidate − production {prod['version']} on the same rows"))
    else:
        checks.append({"check": "within_10min_f1_vs_production", "passed": True, "skipped": True,
                       "value": "no production model", "rule": "reported",
                       "detail": "First version: nothing in production to compare with"})
    summary = {"mae_reaching_limit_min": c["mae_reaching_limit_min"], "within_10min_f1": c["within_10min"]["f1"],
               "within_10min_recall": c["within_10min"]["recall"]}
    return model, results, {"checks": checks, "summary": summary}


# ----------------------------------------------------------------------------------------------- RL policy
def rl_scenarios(store: factory_dataset.FactoryDatasetStore, dataset_ids: list[str]) -> dict[str, list[dict]]:
    """One scenario per run and CNC: the fault that run gave that CNC (or the compressor's pressure drop), its
    severity and onset (scaled to the 12-minute training shift). Split as the runs are."""
    out: dict[str, list[dict]] = {"train": [], "test": []}
    for ds in dict.fromkeys(dataset_ids):
        meta = store.metadata(ds)
        cncs = [m for m, info in meta["machines"].items() if info["template"] in ("cnc_mill", "cnc_lathe")]
        labels = pd.read_csv(store.file(ds, "labels"))
        runs = pd.read_csv(store.file(ds, "runs"))
        for _, run in runs.iterrows():
            lab = labels[labels["run_id"] == run["run_id"]]
            for mid in cncs:
                mine = lab[(lab["machine_id"] == mid) | ((lab["machine_id"] == "COMP-01") & (lab["fault"] == "pressure_drop"))]
                mine = mine[mine["fault"].isin(list(RL_SCENARIO))].sort_values("start_min")
                sc = {"dataset": ds, "run_id": run["run_id"], "machine_id": mid, "scenario": "none",
                      "severity": None, "inject_at": 0}
                if len(mine):
                    first = mine.iloc[0]
                    sc.update(scenario=RL_SCENARIO[first["fault"]], severity=float(first["severity"]),
                              inject_at=int(round(min(1.0, float(first["start_min"]) / float(run["minutes"])) * RL_LAST_ONSET)))
                out[run["split"]].append(sc)
    return out


def _rl_run(policy: Callable[[str], str], sc: dict, seed: int) -> dict[str, float]:
    env = mrl.VmcEnv(seed=seed, scenario=sc["scenario"], decisions=RL_SHIFT_DECISIONS, severity=sc["severity"],
                     inject_at=sc["inject_at"])
    s, tot = env.state(), {"return_inr": 0.0, "parts": 0.0, "trips": 0.0, "over_temp_s": 0.0, "zone_d_s": 0.0,
                           "maintenance_stops": 0.0, "inspections": 0.0}
    while not env.done:
        a = policy(s)
        s, r, _, o = env.step(a)
        tot["return_inr"] += r
        tot["parts"] += {"maintain": 0.0, "inspect": 1.0 - mrl.REWARD["inspection_output_loss"]}.get(a, 1.0) * o.parts
        tot["trips"] += int(o.trip)
        tot["over_temp_s"] += o.over_temp_s
        tot["zone_d_s"] += o.zone_d_s
        tot["maintenance_stops"] += int(a == "maintain")
        tot["inspections"] += int(a == "inspect")
    return tot


def rl_evaluate(policy: Callable[[str], str], scenarios: list[dict]) -> dict[str, Any]:
    """Mean outcome per shift over the scenarios, each on RL_EVAL_SEEDS held-out environment seeds."""
    by: dict[str, list[dict]] = {}
    for j, sc in enumerate(scenarios):
        for k in range(RL_EVAL_SEEDS):
            by.setdefault(sc["scenario"], []).append(_rl_run(policy, sc, RL_EVAL_SEED0 + 97 * j + k))
    mean = lambda rows: {key: round(sum(r[key] for r in rows) / len(rows), 3) for key in rows[0]}
    out = {sc: {**mean(rows), "shifts": len(rows)} for sc, rows in sorted(by.items())}
    every = [r for rows in by.values() for r in rows]
    out["all"] = {**mean(every), "shifts": len(every)}
    return out


def train_rl(scen: dict[str, list[dict]], prod: dict, seed: int, episodes: int,
             progress: Callable[[str], None]) -> tuple[mrl.QPolicy, dict, dict]:
    """Fine-tune the production policy on the training runs' scenarios (ε 0.3 → 0.05), then evaluate on the test runs'."""
    g = GATES["rl"]
    train_sc, test_sc = scen["train"], scen["test"]
    if not train_sc or not test_sc:
        raise LearningError("the datasets need train and test runs with CNC scenarios")
    pol = mrl.QPolicy(q={s: list(v) for s, v in prod["policy"].q.items()},
                      visits={s: list(v) for s, v in prod["policy"].visits.items()})
    rng = random.Random(seed)
    returns = []
    for ep in range(episodes):
        if ep % max(1, episodes // 20) == 0:
            progress(f"training {ep}/{episodes} shifts")
        eps = max(0.05, 0.3 - 0.25 * ep / max(1, 0.7 * episodes))
        sc = train_sc[rng.randrange(len(train_sc))]
        env = mrl.VmcEnv(seed=3_000_000 + ep, scenario=sc["scenario"], decisions=RL_SHIFT_DECISIONS,
                         severity=sc["severity"], inject_at=sc["inject_at"])
        s, total = env.state(), 0.0
        while not env.done:
            a = rng.choice(mrl.ACTIONS) if rng.random() < eps else pol.best(s)
            s2, r, done, _ = env.step(a)
            n = pol.visits.get(s, [0] * len(mrl.ACTIONS))[mrl.ACTIONS.index(a)]
            pol.update(s, a, r, s2, done, alpha=max(0.02, 1.0 / (1.0 + n) ** 0.6))
            s, total = s2, total + r
        returns.append(total)
    progress("evaluating on held-out runs")
    results = {"candidate": rl_evaluate(pol.best, test_sc),
               "production": {"version": prod["version"], **rl_evaluate(prod["policy"].best, test_sc)},
               "rule_based": rl_evaluate(mrl.rule_policy, test_sc),
               "rule_with_inspection": rl_evaluate(mrl.rule_inspect_policy, test_sc),
               "always_continue": rl_evaluate(lambda s: "continue", test_sc)}
    c, p, rb = results["candidate"]["all"], results["production"]["all"], results["rule_based"]["all"]
    n_fault_test = sum(sc["scenario"] != "none" for sc in test_sc)
    checks = [
        {"check": "scenarios", "passed": len(train_sc) >= g["min_train_scenarios"] and n_fault_test >= g["min_test_fault_scenarios"],
         "value": f"{len(train_sc)} train scenarios; {n_fault_test} held-out with a fault",
         "rule": f">= {g['min_train_scenarios']:g} train, >= {g['min_test_fault_scenarios']:g} held-out fault scenarios",
         "detail": "Enough factory scenarios to learn from and to measure"},
        _check("return_vs_production", c["return_inr"] - p["return_inr"], ">=", g["return_vs_production_min_delta_inr"],
               f"₹ per shift on held-out scenarios, candidate − current policy {prod['version']}"),
        _check("return_vs_rules", c["return_inr"] - rb["return_inr"], ">=", g["return_vs_rules_min_delta_inr"],
               "₹ per shift on held-out scenarios, candidate − rule baseline"),
        _check("trips_vs_production", c["trips"] - p["trips"], "<=", g["trips_vs_production_max_delta"],
               "Trips (unplanned breakdowns) per shift, candidate − current policy"),
    ]
    curve = mrl.returns_curve(returns, points=40) if returns else []
    summary = {"return_inr": c["return_inr"], "production_return_inr": p["return_inr"], "trips": c["trips"]}
    return pol, {**results, "returns_curve": curve, "scenarios": {"train": len(train_sc), "test": len(test_sc),
                                                                  "test_list": test_sc}}, {"checks": checks, "summary": summary}


# ----------------------------------------------------------------------------------------------- training job
def load_production(kind: str, reg: ModelRegistry) -> dict | None:
    version = reg.production()
    if version is None:
        return None
    path = reg.model_path(version)
    if kind == "rl":
        pol = mrl.QPolicy.load(path)
        if pol is None:
            raise LearningError(f"production policy {version} cannot be loaded")
        return {"version": version, "policy": pol}
    blob = joblib.load(path)
    return {"version": version, "model": blob["model"], "inputs": blob["inputs"]}


def train_candidate(kind: str, reg: ModelRegistry, store: factory_dataset.FactoryDatasetStore, dataset_ids: list[str],
                    by: str = "operator", seed: int = SEED, episodes: int = 1500,
                    progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise LearningError(f"kind must be one of {', '.join(KINDS)}")
    say = progress or (lambda _m: None)
    t0 = time.time()
    say("loading data")
    df, used = load_windows(store, dataset_ids)
    train, test = _split(df)
    prod = load_production(kind, reg)
    say("training")
    version = reg.next_version()
    vdir = reg.root / version
    trained_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if kind == "rl":
        if not (200 <= episodes <= 20000):
            raise LearningError("episodes must be 200–20000")
        pol, results, gate = train_rl(rl_scenarios(store, dataset_ids), prod, seed, episodes, say)
        pol.meta = {"algorithm": "tabular Q-learning, fine-tuned from the current policy (ε 0.3 → 0.05, γ = 0.9)",
                    "environment": "VMC and air-compressor physics from sim/plant.py with the fault scenarios of the "
                                   "datasets' training runs (every CNC's faults; onset scaled to a 12-min shift)",
                    "episodes": episodes, "seed": seed, "trained_at": trained_at, "from_version": prod["version"],
                    "decision_interval_s": mrl.DECISION_S, "registry_version": version}
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "policy.json").write_text(json.dumps(pol.to_json(), indent=1))
        model_desc = f"Q-learning policy fine-tuned from {prod['version']} on factory scenarios ({episodes} shifts)"
        rows = {"train_scenarios": results["scenarios"]["train"], "test_scenarios": results["scenarios"]["test"]}
    else:
        fn = train_fault if kind == "fault" else train_ttf
        model, results, gate = fn(train, test, prod, seed)
        vdir.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": model, "inputs": INPUTS, "kind": kind, "trained_at": trained_at, "registry_version": version,
                     "data_label": DATA_LABEL, "datasets": [u["dataset_id"] for u in used]}, vdir / "model.joblib", compress=3)
        model_desc = ("RandomForestClassifier (150 trees, balanced classes) on 60 s feature windows" if kind == "fault"
                      else "RandomForestRegressor (150 trees) on 60 s feature windows, target capped at 30 min")
        rows = {"train_windows": int(len(train)), "test_windows": int(len(test))}
        if kind == "fault":
            results["feature_importances"] = dict(sorted(((f, round(float(v), 4)) for f, v in zip(INPUTS, model.feature_importances_)),
                                                         key=lambda kv: -kv[1])[:12])
    gate = {"passed": all(c["passed"] for c in gate["checks"]), "criteria": GATES[kind], **gate}
    split = {"by": "whole run (from each dataset's split)", "train_runs": int(train["group"].nunique()),
             "test_runs": int(test["group"].nunique()), "test_run_ids": sorted(test["group"].unique().tolist()), **rows}
    metrics = {"registry_version": version, "kind": kind, "title": KINDS[kind], "trained_at": trained_at, "model": model_desc,
               "data_label": DATA_LABEL, "datasets": used, "split": split, "results": results, "gate": gate,
               "real_machine_data": False,
               "honest_disclaimer": "Trained and tested only on SYNTHETIC DIGITAL TWIN DATA from the UDYAM-TWIN factory "
                                    "simulator, evaluated on held-out simulated runs. It has not learned from real machines "
                                    "and is not validated on them."}
    (vdir / "metrics.json").write_text(json.dumps(metrics, indent=2, default=float))
    return reg.add_candidate(version, {
        "trained_at": trained_at, "trained_by": by, "title": KINDS[kind], "model": model_desc,
        "trained_on": [f"{u['dataset_id']} ({DATA_LABEL})" for u in used], "datasets": [u["dataset_id"] for u in used],
        "real_machine_data": False, "summary": gate["summary"], "gate": gate, "split": split,
        "train_seconds": round(time.time() - t0, 1)})


class FactoryTrainingJob:
    """One factory model training at a time, in a background thread."""

    def __init__(self, regs: dict[str, ModelRegistry], store: factory_dataset.FactoryDatasetStore):
        self.regs, self.store = regs, store
        self.lock = threading.Lock()
        self.job: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        return dict(self.job) if self.job else {"state": "idle"}

    def start(self, kind: str, dataset_ids: list[str], by: str, episodes: int = 1500) -> dict[str, Any]:
        if kind not in KINDS:
            raise LearningError(f"kind must be one of {', '.join(KINDS)}")
        if not dataset_ids:
            raise LearningError("choose at least one factory dataset")
        if kind == "rl" and not (200 <= episodes <= 20000):
            raise LearningError("episodes must be 200–20000")
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise LearningError("a factory model is already being trained")
            self.job = {"state": "running", "kind": kind, "step": "starting", "datasets": dataset_ids,
                        "started_at": time.time(), "version": None, "error": None}
        threading.Thread(target=self._run, args=(kind, dataset_ids, by, episodes), daemon=True,
                         name="factory-training").start()
        return self.status()

    def _run(self, kind, dataset_ids, by, episodes) -> None:
        try:
            card = train_candidate(kind, self.regs[kind], self.store, dataset_ids, by, episodes=episodes,
                                   progress=lambda s: self.job.update(step=s))
            self.job.update(state="done", version=card["version"], gate_passed=card["gate"]["passed"], finished_at=time.time())
        except Exception as err:  # noqa: BLE001 - reported, never shown as success
            self.job.update(state="failed", error=str(err), finished_at=time.time())


# ----------------------------------------------------------------------------------------------- live readout
class LiveFactoryModels:
    """The production fault and time-to-limit models applied to the live simulation's last 60 s, per machine.

    Fed every broadcast batch (api/app.py). Read-only: it reports what the models say and never acts."""

    def __init__(self, regs: dict[str, ModelRegistry]):
        self.regs = regs
        self.n = int(WINDOW_S / mrl.TICK_S)
        self.bufs: dict[str, deque] = {}
        self.templates: dict[str, str] = {}
        self.pressure = 7.0
        self._cache: dict[str, tuple[str, Any]] = {}

    def observe(self, messages: list[dict]) -> None:
        if any(m.get("type") == "event" and m.get("kind") == "RESET" for m in messages):
            self.clear()  # frames from before a reset describe another plant state
        twins = {m["machine_id"]: m for m in messages if m.get("type") == "twin"}
        for m in messages:
            if m.get("type") == "observed" and m.get("machine_id") == "COMP-01" and m.get("pressure_bar") is not None:
                self.pressure = float(m["pressure_bar"])
        for m in messages:
            if m.get("type") == "observed" and m.get("source_mode", "SIM") == "SIM":
                mid = m["machine_id"]
                self.templates[mid] = m.get("template", "")
                self.bufs.setdefault(mid, deque(maxlen=self.n)).append(
                    {"obs": m, "twin": twins.get(mid) or factory_dataset._EMPTY_TWIN, "pressure_bar": self.pressure})

    def clear(self) -> None:
        self.bufs.clear()

    def _model(self, kind: str) -> tuple[str, Any] | None:
        reg = self.regs[kind]
        if not reg.index_path.is_file() or reg.production() is None:
            return None
        v = reg.production()
        if self._cache.get(kind, ("",))[0] != v:
            self._cache[kind] = (v, joblib.load(reg.model_path(v)))
        return self._cache[kind]

    def predict(self) -> dict[str, Any]:
        fault, ttf = self._model("fault"), self._model("ttf")
        out = []
        for mid, buf in sorted(self.bufs.items()):
            row: dict[str, Any] = {"machine_id": mid, "template": self.templates.get(mid), "window_s": len(buf) * mrl.TICK_S}
            if len(buf) < self.n:
                row["status"] = "warming up (needs 60 s of live data)"
                out.append(row)
                continue
            f = window_features(list(buf))
            x = pd.DataFrame([{**f, **{f"is_{t}": float(self.templates.get(mid) == t) for t in TEMPLATES}}])
            if fault:
                blob = fault[1]
                probs = blob["model"].predict_proba(x[blob["inputs"]])[0]
                classes = list(blob["model"].classes_)
                best = int(np.argmax(probs))
                label = classes[best]
                row["fault"] = {"version": fault[0], "label": label, "probability": round(float(probs[best]), 3),
                                "cause_machine": COMMON_CAUSE.get(label, mid) if label != "normal" else None,
                                "top": sorted(({"label": c, "p": round(float(p), 3)} for c, p in zip(classes, probs)),
                                              key=lambda d: -d["p"])[:3]}
            if ttf:
                blob = ttf[1]
                mins = float(np.clip(blob["model"].predict(x[blob["inputs"]])[0], 0, HORIZON_MIN))
                row["ttf"] = {"version": ttf[0], "minutes_to_limit": round(mins, 1), "within_10min": mins < 10.0,
                              "capped": mins >= HORIZON_MIN - 0.5}
            out.append(row)
        return {"data_label": "Live simulation (SIM)", "machines": out,
                "models": {k: (self._model(k) or (None,))[0] for k in ("fault", "ttf")},
                "note": "Read-only: what the production factory models say about the last 60 s of the live simulation. "
                        "They were trained on SYNTHETIC DIGITAL TWIN DATA; nothing is acted on."}
