"""Train a candidate AI4I failure-risk model on AI4I 2020 plus SYNTHETIC DIGITAL TWIN DATA, and gate it.

What the candidate learns
- Inputs: AI4I's five process columns (air temp, process temp, rpm, torque, tool wear), as before.
- AI4I rows: unchanged, with their Machine failure label.
- Synthetic rows (ml/synthetic.py windows): the window means of the same five signals. The positive label is
  ground truth from the simulator: the machine's true process temperature is at or over its configured limit
  (config/machines.yaml) at the end of the window. AI4I has no notion of this machine's limit; this is what
  the synthetic data adds. Windows with bad sensor data (dropout or stuck) are left out.
- The failure-mode models (TWF, HDF, PWF, OSF, RNF) have no synthetic labels, so they are retrained on AI4I
  only, exactly as in ml/train_ai4i.py.

Evaluation (both the candidate and the current production model, on the same held-out rows)
- AI4I test: the 20 % split of ml/train_ai4i.py (seed 42), never used for training.
- Synthetic test: whole episodes held out (20 %, seeded), so no window of a test episode is trained on.

Gate (GATE below, fixed before training): the candidate must not be worse on AI4I and must be better on the
twin's own (synthetic) data. Passing the gate only makes the candidate eligible; an operator deploys it.
Nothing here is evidence about real machines: every training row is synthetic (AI4I 2020 is synthetic too).
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split

from config import get_config
from ml import synthetic
from ml.ai4i import FEATURES, MODES, TARGETS, load_dataset, sha256, DATA_PATH
from ml.model_registry import ModelRegistry
from ml.train_ai4i import RF_PARAMS, SEED, _scores

TRAIN_N_JOBS = 2                 # leaves cores for the live engine when training inside the server
SYNTH_TEST_SHARE = 0.2
MIN_SYNTH_POSITIVES = {"train": 20, "test": 10}
MIN_SYNTH_TEST_NEGATIVES = 10    # a one-class test set cannot show false alarms (and has no ROC-AUC)
LABEL_TEST_SHARE = 0.2
MIN_CONFIRMED_TEST = 5           # held-out confirmed positives AND negatives needed to gate on confirmed labels

# Fixed before any candidate is trained. Deltas are candidate minus production on the same rows.
GATE = {
    "ai4i_f1_min_delta": -0.02,          # AI4I failure F1 may drop by at most 0.02
    "ai4i_recall_min_delta": -0.03,      # recall may drop by at most 0.03 (missed failures matter most)
    "ai4i_roc_auc_min_delta": -0.01,
    "ai4i_fpr_max_delta": 0.01,          # false-positive rate may rise by at most 1 point
    "synthetic_f1_min_delta": 0.05,      # must improve F1 on the twin's own data by at least 0.05
    "confirmed_f1_min_delta": -0.02,     # on held-out operator-confirmed labels (when there are enough)
}


class CandidateError(ValueError):
    pass


def _synthetic_frame(dataset_ids: list[str], store: synthetic.SyntheticStore) -> tuple[pd.DataFrame, list[dict]]:
    """Synthetic windows mapped to AI4I inputs, with label and episode group. Refuses anything not labelled synthetic."""
    frames, used = [], []
    for ds in dataset_ids:
        try:
            meta = store.metadata(ds)
        except (FileNotFoundError, synthetic.GeneratorError):
            raise CandidateError(f"synthetic dataset {ds} not found")
        if meta.get("data_label") != synthetic.DATA_LABEL or meta.get("synthetic") is not True:
            raise CandidateError(f"{ds} is not labelled {synthetic.DATA_LABEL}; refusing to train on it")
        path = store.path(ds) / "windows.csv"
        if sha256(path) != meta["files"]["windows.csv"]["sha256"]:
            raise CandidateError(f"{ds}/windows.csv does not match the checksum in its metadata")
        df = pd.read_csv(path)
        if (df["data_label"] != synthetic.DATA_LABEL).any():
            raise CandidateError(f"{ds} has rows not labelled {synthetic.DATA_LABEL}")
        mid = meta["config"]["machine_id"]
        limit = float(get_config().thresholds(mid, synthetic.MACHINES[mid])["process_temp_limit_k"])
        good = (df["dq_bad_frac"] <= 0.0) & (df["temp_mean"] > 250.0)
        df = df[good]
        frames.append(pd.DataFrame({
            "air_temp_k": df["air_temp_mean"], "process_temp_k": df["temp_mean"], "rpm": df["rpm_mean"],
            "torque_nm": df["torque_mean"], "tool_wear_min": df["wear_mean"],
            "machine_failure": (df["true_process_temp_k"] >= limit).astype(int),
            "group": ds + ":" + df["episode"].astype(str),
        }))
        used.append({"dataset_id": ds, "name": meta.get("name"), "machine_id": mid, "data_label": meta["data_label"],
                     "windows_used": int(good.sum()), "windows_dropped_bad_sensor": int((~good).sum()),
                     "temperature_limit_k": limit, "sha256": meta["files"]["windows.csv"]["sha256"]})
    if not frames:
        raise CandidateError("choose at least one synthetic dataset")
    return pd.concat(frames, ignore_index=True), used


def _split_groups(groups: pd.Series, seed: int) -> tuple[np.ndarray, np.ndarray]:
    uniq = sorted(groups.unique())
    rng = random.Random(seed)
    rng.shuffle(uniq)
    n_test = max(1, round(len(uniq) * SYNTH_TEST_SHARE))
    test = set(uniq[:n_test])
    mask = groups.isin(test).to_numpy()
    return ~mask, mask


def _eval(model, X: pd.DataFrame, y: np.ndarray) -> dict[str, Any]:
    prob = model.predict_proba(X[FEATURES])[:, 1]
    return _scores(y, (prob >= 0.5).astype(int), prob)


def evaluate_gate(prod: dict[str, Any], cand: dict[str, Any], counts: dict[str, int]) -> dict[str, Any]:
    def chk(name, value, op, limit, detail):
        ok = value >= limit if op == ">=" else value <= limit
        return {"check": name, "passed": bool(ok), "value": round(float(value), 4), "rule": f"{op} {limit:+g}",
                "detail": detail}

    pa, ca, ps, cs = prod["ai4i_test"], cand["ai4i_test"], prod["synthetic_test"], cand["synthetic_test"]
    checks = [
        {"check": "synthetic_positives", "passed": counts["train_pos"] >= MIN_SYNTH_POSITIVES["train"]
         and counts["test_pos"] >= MIN_SYNTH_POSITIVES["test"] and counts.get("test_neg", 0) >= MIN_SYNTH_TEST_NEGATIVES,
         "value": f"train {counts['train_pos']} over-limit; test {counts['test_pos']} over-limit, {counts.get('test_neg', 0)} under",
         "rule": f">= {MIN_SYNTH_POSITIVES['train']} train and {MIN_SYNTH_POSITIVES['test']} test over-limit windows, "
                 f">= {MIN_SYNTH_TEST_NEGATIVES} test windows under the limit",
         "detail": "Enough synthetic data of both kinds to train and to measure"},
        chk("ai4i_f1", ca["f1"] - pa["f1"], ">=", GATE["ai4i_f1_min_delta"], "AI4I test F1, candidate − production"),
        chk("ai4i_recall", ca["recall"] - pa["recall"], ">=", GATE["ai4i_recall_min_delta"], "AI4I test recall, candidate − production"),
        chk("ai4i_roc_auc", ca.get("roc_auc", 0) - pa.get("roc_auc", 0), ">=", GATE["ai4i_roc_auc_min_delta"], "AI4I test ROC-AUC, candidate − production"),
        chk("ai4i_false_positive_rate", ca["false_positive_rate"] - pa["false_positive_rate"], "<=", GATE["ai4i_fpr_max_delta"],
            "AI4I test false-positive rate, candidate − production"),
        chk("synthetic_f1", cs["f1"] - ps["f1"], ">=", GATE["synthetic_f1_min_delta"], "Held-out synthetic episodes F1, candidate − production"),
    ]
    if "confirmed_test" in cand:
        pc, cc = prod["confirmed_test"], cand["confirmed_test"]
        cm = cc["confusion_matrix"] if cc else {"true_positives": 0, "false_negatives": 0, "true_negatives": 0, "false_positives": 0}
        pos, neg = cm["true_positives"] + cm["false_negatives"], cm["true_negatives"] + cm["false_positives"]
        if pos >= MIN_CONFIRMED_TEST and neg >= MIN_CONFIRMED_TEST:
            checks.append(chk("confirmed_f1", cc["f1"] - pc["f1"], ">=", GATE["confirmed_f1_min_delta"],
                              "Held-out operator-confirmed labels F1, candidate − production"))
        else:  # reported, never counted as a pass that measured something
            checks.append({"check": "confirmed_f1", "passed": True, "skipped": True, "value": f"{pos} positive, {neg} negative",
                           "rule": f"needs >= {MIN_CONFIRMED_TEST} of each held out", "detail":
                           "Not measured: too few held-out operator-confirmed samples"})
    return {"passed": all(c["passed"] for c in checks), "criteria": GATE, "checks": checks}


def _confirmed_frame(label_store, seed: int) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Operator-confirmed samples split by label record (a label's samples stay together)."""
    rows = label_store.training_rows()
    if not rows:
        raise CandidateError("there are no operator-confirmed labels to train on")
    df = pd.DataFrame(rows)
    ids = sorted(df["label_id"].unique())
    random.Random(seed).shuffle(ids)
    n_test = round(len(ids) * LABEL_TEST_SHARE) if len(ids) >= 5 else 0
    test = df["label_id"].isin(ids[:n_test])
    info = {"labels": len(ids), "samples": int(len(df)), "held_out_labels": n_test,
            "samples_from_real_machines": int((df["source"] == "LIVE").sum()),
            "samples_from_simulation": int((df["source"] != "LIVE").sum()),
            "positives": int(df["machine_failure"].sum())}
    return df[~test], df[test], info


def train_candidate(registry: ModelRegistry, store: synthetic.SyntheticStore, dataset_ids: list[str],
                    synthetic_weight: float = 1.0, by: str = "operator", seed: int = SEED,
                    progress: Callable[[str], None] | None = None, label_store=None,
                    use_labels: bool = False) -> dict[str, Any]:
    if not (0.1 <= synthetic_weight <= 5.0):
        raise CandidateError("synthetic_weight must be 0.1–5")
    say = progress or (lambda _m: None)
    t0 = time.time()
    say("loading data")
    ai4i = load_dataset()
    idx_train, idx_test = train_test_split(ai4i.index, test_size=0.2, random_state=seed, stratify=ai4i["machine_failure"])
    syn, used = _synthetic_frame(dataset_ids, store)
    s_train, s_test = _split_groups(syn["group"], seed)
    syn_train, syn_test = syn[s_train], syn[s_test]
    assert not set(syn_train["group"]) & set(syn_test["group"])
    counts = {"train_pos": int(syn_train["machine_failure"].sum()), "test_pos": int(syn_test["machine_failure"].sum()),
              "test_neg": int((syn_test["machine_failure"] == 0).sum())}

    lab_train = lab_test = None
    lab_info: dict[str, Any] | None = None
    if use_labels:
        lab_train, lab_test, lab_info = _confirmed_frame(label_store, seed)

    say("training")
    params = {**RF_PARAMS, "n_jobs": TRAIN_N_JOBS}
    parts = [ai4i.loc[idx_train, FEATURES], syn_train[FEATURES]] + ([lab_train[FEATURES]] if lab_info else [])
    X = pd.concat(parts, ignore_index=True)
    y = np.concatenate([ai4i.loc[idx_train, "machine_failure"].to_numpy(), syn_train["machine_failure"].to_numpy()]
                       + ([lab_train["machine_failure"].to_numpy()] if lab_info else []))
    w = np.concatenate([np.ones(len(idx_train)), np.full(len(syn_train), synthetic_weight)]
                       + ([np.ones(len(lab_train))] if lab_info else []))
    models = {"machine_failure": RandomForestClassifier(**params).fit(X, y, sample_weight=w)}
    for target in MODES:  # no synthetic labels for modes: AI4I only, as in ml/train_ai4i.py
        models[target] = RandomForestClassifier(**params).fit(ai4i.loc[idx_train, FEATURES], ai4i.loc[idx_train, target])

    say("evaluating")
    prod_version = registry.production()
    prod = joblib.load(registry.model_path(prod_version))["models"]["machine_failure"]
    X_ai, y_ai = ai4i.loc[idx_test], ai4i.loc[idx_test, "machine_failure"].to_numpy()
    y_syn = syn_test["machine_failure"].to_numpy()
    results = {
        "production": {"version": prod_version, "ai4i_test": _eval(prod, X_ai, y_ai), "synthetic_test": _eval(prod, syn_test, y_syn)},
        "candidate": {"ai4i_test": _eval(models["machine_failure"], X_ai, y_ai),
                      "synthetic_test": _eval(models["machine_failure"], syn_test, y_syn)},
    }
    if lab_info:  # None when too few labels to hold any out (fewer than 5): reported as not measured
        y_lab = lab_test["machine_failure"].to_numpy()
        for who, model in (("production", prod), ("candidate", models["machine_failure"])):
            results[who]["confirmed_test"] = _eval(model, lab_test, y_lab) if len(lab_test) else None
    gate = evaluate_gate(results["production"], results["candidate"], counts)
    real_rows = int((lab_train["source"] == "LIVE").sum()) if lab_info else 0

    version = registry.next_version()
    vdir = registry.root / version
    vdir.mkdir(parents=True, exist_ok=True)
    trained_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ranges = {f: [float(X[f].min()), float(X[f].max())] for f in FEATURES}
    medians = {f: float(X[f].median()) for f in FEATURES}
    desc = (f"RandomForestClassifier per target ({', '.join(f'{k}={v}' for k, v in RF_PARAMS.items() if k != 'n_jobs')}); "
            f"machine_failure on AI4I + {synthetic.DATA_LABEL} ({version})")
    from ml.adaptive import save_reference  # the inputs this version has seen, for drift monitoring
    save_reference(vdir / "reference.npz", X[FEATURES].to_numpy(dtype=float))
    joblib.dump({"models": models, "meta": {
        "model": desc, "features": FEATURES, "targets": TARGETS, "trained_at": trained_at,
        "dataset": {"name": "AI4I 2020 + " + synthetic.DATA_LABEL, "source": "ml/candidate.py",
                    "sha256": sha256(DATA_PATH), "synthetic_source": True, "synthetic_datasets": [u["dataset_id"] for u in used]},
        "train_ranges": ranges, "train_medians": medians, "registry_version": version,
    }}, vdir / "model.joblib", compress=3)

    # metrics.json in the shape /api/metrics already serves for the production model
    cand_ai = results["candidate"]["ai4i_test"]
    metrics = {
        "registry_version": version,
        "trained_at": trained_at,
        "model": desc,
        "features": FEATURES, "targets": TARGETS, "threshold": 0.5,
        "dataset": {"name": "AI4I 2020 + " + synthetic.DATA_LABEL, "rows": int(len(X)),
                    "ai4i_train_rows": int(len(idx_train)), "synthetic_train_rows": int(len(syn_train)),
                    "synthetic_datasets": used, "synthetic_source": True, "real_machine_data": real_rows > 0,
                    "confirmed_labels": lab_info, "confirmed_train_samples_from_real_machines": real_rows},
        "per_class": {"machine_failure": {"label": "Machine failure", **cand_ai}},
        "random_forest": {**cand_ai, "feature_importances": {f: round(float(v), 4) for f, v in
                                                             zip(FEATURES, models["machine_failure"].feature_importances_)}},
        "comparison": results, "gate": gate,
        "split": {"ai4i": "stratified 80/20, seed 42 (as ml/train_ai4i.py)",
                  "synthetic": f"{int(SYNTH_TEST_SHARE * 100)} % of episodes held out, seed {seed}",
                  "synthetic_test_rows": int(len(syn_test))},
        "probability_calibrated": False,
        "honest_disclaimer": (
            "Trained and tested only on synthetic data: AI4I 2020 (a synthetic dataset modelled on a real milling process) "
            "and UDYAM-TWIN simulator data labelled SYNTHETIC DIGITAL TWIN DATA"
            + (", plus operator labels on simulated readings" if lab_info else "")
            + ". It has not learned from real machines and is not validated on them." if not real_rows else
            f"Trained on AI4I 2020 and SYNTHETIC DIGITAL TWIN DATA plus {real_rows} operator-confirmed samples from real "
            f"machines ({lab_info['labels']} labels in total). That is a small amount of real data: the model is mostly "
            "synthetic and is not validated on real machines beyond the held-out confirmed labels reported here."),
    }
    # same AI4I test rows as the baseline, so its rule baseline still applies
    metrics["rule_baseline"] = registry.metrics("v001").get("rule_baseline")
    (vdir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    card = registry.add_candidate(version, {
        "trained_at": trained_at, "trained_by": by,
        "trained_on": ["AI4I 2020 (UCI; synthetic)", *[f"{u['dataset_id']} ({synthetic.DATA_LABEL})" for u in used],
                       *([f"{lab_info['labels']} operator-confirmed label(s): {real_rows} training samples from real "
                          f"machines, {len(lab_train) - real_rows} from the simulation"] if lab_info else [])],
        "synthetic_datasets": [u["dataset_id"] for u in used], "synthetic_weight": synthetic_weight,
        "confirmed_labels": lab_info, "real_machine_data": real_rows > 0,
        "summary": "Candidate: AI4I 2020 plus synthetic digital-twin windows (label: over the machine's temperature limit).",
        "gate": gate, "ai4i_test": cand_ai, "comparison": results, "train_seconds": round(time.time() - t0, 1),
    })
    return card


class TrainingJob:
    """One candidate training at a time, in a background thread."""

    def __init__(self, registry: ModelRegistry, store: synthetic.SyntheticStore, label_store=None):
        self.registry, self.store, self.label_store = registry, store, label_store
        self.lock = threading.Lock()
        self.job: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        return dict(self.job) if self.job else {"state": "idle"}

    def start(self, dataset_ids: list[str], synthetic_weight: float, by: str, use_labels: bool = False) -> dict[str, Any]:
        if not dataset_ids:
            raise CandidateError("choose at least one synthetic dataset")
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise CandidateError("a candidate is already being trained")
            self.job = {"state": "running", "step": "starting", "datasets": dataset_ids, "started_at": time.time(),
                        "version": None, "error": None}
        threading.Thread(target=self._run, args=(dataset_ids, synthetic_weight, by, use_labels), daemon=True,
                         name="candidate-training").start()
        return self.status()

    def _run(self, dataset_ids, synthetic_weight, by, use_labels) -> None:
        try:
            card = train_candidate(self.registry, self.store, dataset_ids, synthetic_weight, by,
                                   progress=lambda s: self.job.update(step=s), label_store=self.label_store,
                                   use_labels=use_labels)
            self.job.update(state="done", version=card["version"], gate_passed=card["gate"]["passed"], finished_at=time.time())
        except Exception as err:  # noqa: BLE001 - reported, never shown as success
            self.job.update(state="failed", error=str(err), finished_at=time.time())
