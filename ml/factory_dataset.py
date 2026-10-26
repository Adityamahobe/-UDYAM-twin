"""Factory datasets: labelled whole-factory data collected from Factory Run Designer runs (SYNTHETIC DIGITAL TWIN DATA).

A dataset is built from a recipe: one or more parts, each a run design (sim/factory_run.py RunSpec) repeated N times
with a batch variation. Every run executes on its own, freshly built FactoryEngine (the live simulation is never
touched) and is recorded while it runs:

  readings.csv  every machine every 2 s: what its sensors report, plus the hidden plant truth and the labels
  windows.csv   every machine every 10 s: 60-second feature windows (tools/generate_dataset.py window_features, the
                same features as the UDYAM-SIM v1 dataset and the live predictor) + labels + time-to-limit
  line.csv      the line every 10 s: throughput, bottleneck, air supply, each station's state and input buffer
  events.csv    the run's events and the twin's alerts
  labels.csv    ground truth per injected fault: machine, fault, severity, start/end, detection by the twin
  runs.csv      one row per run: part, seed, split, faults, KPIs
  metadata.json label, recipe, split, label balance, quality checks, columns, file checksums

Labels are only what the run injected, never inferred. A compressor pressure drop is a common cause: while it is
active, every CNC's rows also carry label_cause_machine = COMP-01 and label_knock_on = 1 (unless the CNC has a
fault of its own, which then is its label_fault).

Train/test is split by whole run, never by row, so no test run is ever seen in training. Runs are grouped by the
faults they contain and each group is split separately, so fault types appear on both sides where possible.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
import re
import shutil
import threading
import time
import uuid
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import get_config
from sim import factory_run
from sim.factory import TICK_S
from tools.generate_dataset import FEATURES, HORIZON_MIN, STRIDE_S, WINDOW_S, window_features

DATA_LABEL = factory_run.DATA_LABEL
GENERATOR_VERSION = "udyam_factory_v1"
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(os.environ.get("UDYAM_FACTORY_DATA_DIR", ROOT / "ml" / "data" / "factory"))
READING_EVERY_S = 2.0
LINE_EVERY_S = 10.0
MAX_RUNS = factory_run.MAX_BATCH          # runs per dataset
MAX_RUN_MINUTES = 3000.0                  # total simulated minutes per dataset (100 runs × 30 min)
MAX_PARTS = 10
COMMON_CAUSE = {"COMP-01": "pressure_drop"}
AIR_USERS = ("cnc_mill", "cnc_lathe")
MIN_CLASS_SHARE = 0.03                    # a fault label below 3 % of windows is flagged as rare
ID_RE = re.compile(r"^fds-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
PARTS = ("readings", "windows", "line", "events", "labels", "runs")


class DatasetError(ValueError):
    pass


@dataclass
class Part:
    spec: factory_run.RunSpec
    runs: int = 1
    variation: factory_run.Variation | None = None


READING_COLUMNS = ["data_label", "run_id", "split", "t_s", "machine_id", "template", "plc_state", "dq_flag", "rpm",
                   "power_kw", "current_a", "torque_nm", "vibration_mm_s", "temp_c", "tool_wear_min", "blade_wear_pct",
                   "pressure_bar", "discharge_temp_c", "duty_pct", "derate_pct", "parts_done", "zone", "twin_residual",
                   "true_temp_c", "true_vibration_mm_s", "label_fault", "label_severity", "label_cause_machine",
                   "label_knock_on"]
WINDOW_COLUMNS = (["data_label", "run_id", "split", "t_s", "machine_id", "template"] + FEATURES +
                  ["label_fault", "label_severity", "label_cause_machine", "label_knock_on", "since_onset_s",
                   "minutes_to_limit", "limit_within_10min"])
LINE_BASE = ["data_label", "run_id", "split", "t_s", "throughput_parts_per_hr", "line_loss_inr_per_hr", "bottleneck",
             "air_supply_ok", "line_parts", "active_faults"]
EVENT_COLUMNS = ["data_label", "run_id", "split", "t_min", "type", "machine_id", "severity", "kind", "text"]
LABEL_COLUMNS = ["data_label", "run_id", "split", "machine_id", "fault", "severity", "start_min", "end_min", "detected",
                 "detection_delay_min", "first_alert"]
RUN_COLUMNS = ["data_label", "run_id", "split", "part", "scenario", "seed", "minutes", "self_healing", "faults",
               "line_parts", "site_energy_kwh", "energy_cost_inr", "alerts", "faults_injected", "faults_detected"]

COLUMN_NOTES = {
    "data_label": f"Always '{DATA_LABEL}'. Not measured on a real machine.",
    "split": "train or test. Decided per whole run, so a test run is never seen in training.",
    "label_fault": "Ground truth: the fault the run injected on this machine, the common-cause fault (pressure_drop) "
                   "for a CNC while the compressor has it, or 'normal'. Never inferred.",
    "label_severity": "Severity of that injected fault (0.5–2.0), empty when normal.",
    "label_cause_machine": "The machine that has the fault (COMP-01 for a knock-on effect), empty when normal.",
    "label_knock_on": "1 when the label comes from a fault on another machine (the compressor), not this one.",
    "since_onset_s": "Seconds since that fault started (-1 when normal).",
    "minutes_to_limit": f"Minutes until the machine's true temperature reaches its limit or its vibration reaches "
                        f"zone D, capped (censored) at {HORIZON_MIN:g}. For time-to-failure models.",
    "limit_within_10min": "1 if minutes_to_limit < 10.",
    "true_temp_c": "Plant temperature without sensor noise or sensor faults (CNCs only); not observable by sensors.",
    "true_vibration_mm_s": "Plant vibration without sensor effects; not observable by sensors.",
    "twin_residual": "The twin's temperature residual (measured minus predicted) at that moment.",
    "features": "Window statistics over the last 60 s of observed and twin frames (tools/generate_dataset.py).",
    "line.buffer_<machine>": "Parts waiting in front of that station.",
}


def _num(v: Any) -> Any:
    return round(v, 4) if isinstance(v, float) else v


class _Recorder:
    """Collects one run's rows from the engine's messages (called after every tick by factory_run.run)."""

    def __init__(self, run_id: str, machines: dict[str, dict[str, Any]]):
        self.run_id, self.machines = run_id, machines
        self.read_every = int(READING_EVERY_S / TICK_S)
        self.stride, self.win_n = int(STRIDE_S / TICK_S), int(WINDOW_S / TICK_S)
        self.line_every = int(LINE_EVERY_S / TICK_S)
        self.bufs: dict[str, list[dict]] = {m: [] for m in machines}
        self.over: dict[str, list[float]] = {m: [] for m in machines}
        self.readings: list[dict] = []
        self.windows: list[dict] = []
        self.line: list[dict] = []
        cfg = get_config()
        self.limits = {}
        for mid, info in machines.items():
            th = cfg.thresholds(mid, info["template"]) or {}
            zones = cfg.risk_zones(mid, info["template"])
            self.limits[mid] = (th.get("process_temp_limit_k"), float(zones[1]) if zones else None)

    def label(self, mid: str, eng, open_labels: dict, t_s: float) -> dict[str, Any]:
        own = sorted(eng.plants[mid].faults.active_faults)
        if own:
            lab = open_labels.get((mid, own[0])) or {}
            return {"label_fault": own[0], "label_severity": lab.get("severity"), "label_cause_machine": mid,
                    "label_knock_on": 0, "_start": lab.get("start_min")}
        if self.machines[mid]["template"] in AIR_USERS:
            for cm, cf in COMMON_CAUSE.items():
                if cm in eng.plants and cf in eng.plants[cm].faults.active_faults:
                    lab = open_labels.get((cm, cf)) or {}
                    return {"label_fault": cf, "label_severity": lab.get("severity"), "label_cause_machine": cm,
                            "label_knock_on": 1, "_start": lab.get("start_min")}
        return {"label_fault": "normal", "label_severity": None, "label_cause_machine": None, "label_knock_on": 0,
                "_start": None}

    def __call__(self, k: int, msgs: list[dict], eng, open_labels: dict) -> None:
        t_s = round((k + 1) * TICK_S, 1)
        obs = {m["machine_id"]: m for m in msgs if m.get("type") == "observed"}
        twin = {m["machine_id"]: m for m in msgs if m.get("type") == "twin"}
        pressure = (obs.get("COMP-01") or {}).get("pressure_bar", 7.0)
        snaps = None
        for mid in self.machines:
            o, plant = obs.get(mid), eng.plants[mid]
            if o is None:
                continue
            temp_lim, vib_d = self.limits[mid]
            if (temp_lim and plant.template in AIR_USERS and plant.process_temp_k >= temp_lim) or \
                    (vib_d and plant.vibration >= vib_d):
                self.over[mid].append(t_s)
            buf = self.bufs[mid]
            buf.append({"obs": o, "twin": twin.get(mid) or _EMPTY_TWIN, "pressure_bar": pressure})
            if len(buf) > self.win_n:
                buf.pop(0)
            lab = None
            if (k + 1) % self.read_every == 0:
                if snaps is None:
                    snaps = {m["machine_id"]: m for m in msgs if m.get("type") == "snapshot"}
                lab = self.label(mid, eng, open_labels, t_s)
                pt = o.get("process_temp_k")
                self.readings.append({
                    "data_label": DATA_LABEL, "run_id": self.run_id, "t_s": t_s, "machine_id": mid,
                    "template": plant.template, "plc_state": o.get("plc_state"), "dq_flag": o.get("dq_flag"),
                    **{c: _num(o.get(src)) for c, src in (
                        ("rpm", "rpm"), ("power_kw", "power_kw"), ("current_a", "current_a"), ("torque_nm", "torque_nm"),
                        ("vibration_mm_s", "vibration_rms_mm_s"), ("tool_wear_min", "tool_wear_min"),
                        ("blade_wear_pct", "blade_wear_pct"), ("pressure_bar", "pressure_bar"),
                        ("discharge_temp_c", "discharge_temp_c"), ("duty_pct", "duty_pct"),
                        ("derate_pct", "derate_pct"), ("parts_done", "parts_done"))},
                    "temp_c": round(pt - 273.15, 3) if isinstance(pt, (int, float)) else None,
                    "zone": ((snaps.get(mid) or {}).get("severity") or {}).get("zone"),
                    "twin_residual": _num(((twin.get(mid) or {}).get("residual") or {}).get("value")),
                    "true_temp_c": round(plant.process_temp_k - 273.15, 3) if plant.template in AIR_USERS else None,
                    "true_vibration_mm_s": round(plant.vibration, 4),
                    **{c: lab[c] for c in ("label_fault", "label_severity", "label_cause_machine", "label_knock_on")},
                })
            if len(buf) == self.win_n and (k + 1) % self.stride == 0:
                lab = lab or self.label(mid, eng, open_labels, t_s)
                self.windows.append({
                    "data_label": DATA_LABEL, "run_id": self.run_id, "t_s": t_s, "machine_id": mid,
                    "template": plant.template, **window_features(buf),
                    **{c: lab[c] for c in ("label_fault", "label_severity", "label_cause_machine", "label_knock_on")},
                    "since_onset_s": round(t_s - lab["_start"] * 60, 1) if lab["_start"] is not None else -1.0,
                })
        if (k + 1) % self.line_every == 0:
            ln = next((m for m in msgs if m.get("type") == "line"), None)
            if ln:
                last = factory_run.LINE_FLOW[-1][0]
                faults = sorted(f"{m}:{f}" for m, p in eng.plants.items() for f in p.faults.active_faults)
                row = {"data_label": DATA_LABEL, "run_id": self.run_id, "t_s": t_s,
                       "throughput_parts_per_hr": ln.get("throughput_parts_per_hr"),
                       "line_loss_inr_per_hr": ln.get("line_loss_inr_per_hr"), "bottleneck": ln.get("bottleneck"),
                       "air_supply_ok": int(bool(ln.get("air_supply_ok"))),
                       "line_parts": eng.plants[last].parts_done if last in eng.plants else None,
                       "active_faults": "|".join(faults) or "normal"}
                for st in ln.get("stations") or []:
                    row[f"state_{st['machine_id']}"] = st.get("state")
                    row[f"buffer_{st['machine_id']}"] = st.get("buffer_in")
                self.line.append(row)

    def finish(self) -> None:
        """Backfill time-to-limit from the times each machine was over its limit."""
        for row in self.windows:
            nxt = next((t for t in self.over[row["machine_id"]] if t >= row["t_s"]), None)
            mins = HORIZON_MIN if nxt is None else min(HORIZON_MIN, (nxt - row["t_s"]) / 60.0)
            row["minutes_to_limit"] = round(mins, 3)
            row["limit_within_10min"] = int(mins < 10.0)


_EMPTY_TWIN = {"residual": {"value": 0.0, "ewma": 0.0, "cusum": 0.0}, "params": {"values": {}}, "forecast": {}}


def check_recipe(parts: list[Part], custom: list[dict] | None) -> int:
    """Validate every part; returns the number of runs. DatasetError names the first problem."""
    if not (1 <= len(parts) <= MAX_PARTS):
        raise DatasetError(f"a dataset has 1–{MAX_PARTS} parts")
    machines = factory_run.machines_of(custom)
    total = minutes = 0
    for i, p in enumerate(parts, 1):
        if not (1 <= int(p.runs) <= MAX_RUNS):
            raise DatasetError(f"part {i}: runs must be 1–{MAX_RUNS}")
        try:
            factory_run.validate(p.spec, machines)
        except factory_run.RunError as err:
            raise DatasetError(f"part {i} ({p.spec.name}): {err}")
        v = p.variation
        if v and v.severity_range and not (factory_run.SEVERITY_RANGE[0] <= v.severity_range[0] <= v.severity_range[1]
                                           <= factory_run.SEVERITY_RANGE[1]):
            raise DatasetError(f"part {i}: severity range must be within 0.5–2 and min ≤ max")
        if v and (not (0 <= v.fault_time_jitter_min <= 30) or not (0 <= v.param_jitter_pct <= 50)):
            raise DatasetError(f"part {i}: time variation must be 0–30 min and parameter variation 0–50 %")
        total += int(p.runs)
        minutes += int(p.runs) * float(p.spec.minutes)
    if total > MAX_RUNS:
        raise DatasetError(f"a dataset has at most {MAX_RUNS} runs (this recipe has {total})")
    if minutes > MAX_RUN_MINUTES:
        raise DatasetError(f"this recipe is {minutes:g} simulated minutes; the limit per dataset is {MAX_RUN_MINUTES:g}")
    return total


def split_runs(runs: list[dict], test_share: float, seed: int) -> dict[str, str]:
    """run_id -> train|test. Whole runs only; runs grouped by the faults they contain and each group split."""
    groups: dict[str, list[str]] = defaultdict(list)
    for r in runs:
        groups[r["faults"] or "normal"].append(r["run_id"])
    rng = random.Random(f"split:{seed}")
    out: dict[str, str] = {}
    for key in sorted(groups):
        ids = sorted(groups[key])
        rng.shuffle(ids)
        n_test = round(len(ids) * test_share) if len(ids) > 1 else 0
        if test_share > 0 and len(ids) > 1:
            n_test = min(len(ids) - 1, max(1, n_test))
        for j, rid in enumerate(ids):
            out[rid] = "test" if j < n_test else "train"
    if test_share > 0 and len(out) > 1 and "test" not in out.values():   # every group had a single run
        ids = sorted(out)
        rng.shuffle(ids)
        for rid in ids[:max(1, round(len(ids) * test_share))]:
            out[rid] = "test"
    return out


def quality_checks(meta_counts: dict[str, Any], runs: list[dict], labels: list[dict], windows: list[dict]) -> list[dict]:
    """Checks a person should look at before training on the dataset: pass / warn / fail, each with a reason."""
    out = []

    def add(name: str, status: str, detail: str) -> None:
        out.append({"check": name, "status": status, "detail": detail})

    train = {r["run_id"] for r in runs if r["split"] == "train"}
    test = {r["run_id"] for r in runs if r["split"] == "test"}
    add("Split by whole run", "pass" if not (train & test) else "fail",
        f"{len(train)} train runs, {len(test)} test runs, none in both." if not (train & test)
        else f"{len(train & test)} runs are in both train and test.")
    if not test:
        add("Test set", "warn", "No test runs: models trained on this dataset cannot be evaluated on unseen runs.")
    add("Rows", "pass" if windows else "fail",
        f"{len(windows)} feature windows." if windows else "No feature windows: runs must be at least 1 minute.")
    add("Data label", "pass" if all(w["data_label"] == DATA_LABEL for w in windows) else "fail",
        f"Every row is labelled {DATA_LABEL}.")
    by_label = Counter(w["label_fault"] for w in windows)
    faults = [k for k in by_label if k != "normal"]
    if not faults:
        add("Fault labels", "warn", "Only normal operation: add runs with injected faults to train fault diagnosis.")
    else:
        rare = [f for f in faults if by_label[f] / len(windows) < MIN_CLASS_SHARE]
        add("Label balance", "warn" if rare else "pass",
            f"Rare labels (under {MIN_CLASS_SHARE:.0%} of windows): {', '.join(rare)}. Add runs with these faults."
            if rare else f"{len(faults)} fault labels, each at least {MIN_CLASS_SHARE:.0%} of windows.")
        normal_share = by_label.get("normal", 0) / len(windows)
        if normal_share > 0.9:
            add("Normal share", "warn", f"{normal_share:.0%} of windows are normal; fault labels are scarce.")
        train_lab = Counter(w["label_fault"] for w in windows if w["split"] == "train")
        test_lab = Counter(w["label_fault"] for w in windows if w["split"] == "test")
        only_train = [f for f in faults if f not in test_lab]
        only_test = [f for f in faults if f not in train_lab]
        if test and (only_train or only_test):
            msg = []
            if only_test:
                msg.append(f"only in test (never learned): {', '.join(only_test)}")
            if only_train:
                msg.append(f"only in train (never evaluated): {', '.join(only_train)}")
            add("Labels on both sides", "warn", "; ".join(msg).capitalize() + ". Add more runs with these faults.")
        elif test:
            add("Labels on both sides", "pass", "Every fault label appears in both train and test.")
    empty = [l for l in labels if not any(w["run_id"] == l["run_id"] and w["label_cause_machine"] == l["machine_id"]
                                          and w["label_fault"] == l["fault"] for w in windows)]
    add("Injected faults have rows", "warn" if empty else "pass",
        f"{len(empty)} injected faults have no labelled window (injected in the last minute or repaired at once)."
        if empty else f"All {len(labels)} injected faults have labelled windows.")
    ttf = sum(w["limit_within_10min"] for w in windows)
    add("Time-to-limit positives", "pass" if ttf else "warn",
        f"{ttf} windows within 10 min of a temperature or vibration limit." if ttf
        else "No window comes within 10 min of a limit: time-to-failure models will only see censored rows.")
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})


def collect(parts: list[Part], economics: dict[str, float], out_dir: Path, dataset_id: str, name: str = "",
            test_share: float = 0.2, split_seed: int = 0, custom: list[dict] | None = None,
            progress: Callable[[int, int], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Run the recipe and write the dataset files. Returns the metadata."""
    total = check_recipe(parts, custom)
    if not (0.0 <= test_share <= 0.5):
        raise DatasetError("test share must be 0–0.5")
    t0 = time.time()
    machines = factory_run.machines_of(custom)
    readings, windows, line, events, labels, runs = [], [], [], [], [], []
    done = 0
    for pi, part in enumerate(parts):
        v = part.variation or factory_run.Variation()
        for i in range(int(part.runs)):
            spec = factory_run.vary(part.spec, v, i, machines) if part.runs > 1 else part.spec
            run_id = f"p{pi + 1}-r{i + 1:03d}"
            rec = _Recorder(run_id, machines)
            r = factory_run.run(spec, economics, custom, cancelled, record=rec)
            rec.finish()
            readings += rec.readings
            windows += rec.windows
            line += rec.line
            for ev in r["events"]:
                events.append({"data_label": DATA_LABEL, "run_id": run_id, "t_min": ev["at_min"], "type": "run_event",
                               "machine_id": ev.get("machine_id"), "kind": ev["action"], "text": ev["text"]})
            for al in r["alerts"]:
                events.append({"data_label": DATA_LABEL, "run_id": run_id, "t_min": al["at_min"], "type": "alert",
                               "machine_id": al["machine_id"], "severity": al["severity"], "kind": al["kind"],
                               "text": al["title"]})
            for lab in r["labels"]:
                labels.append({"data_label": DATA_LABEL, "run_id": run_id, **lab})
            runs.append({"data_label": DATA_LABEL, "run_id": run_id, "part": pi + 1, "scenario": part.spec.name,
                         "seed": spec.seed, "minutes": spec.minutes, "self_healing": int(spec.self_healing),
                         "faults": "|".join(sorted({f"{l['machine_id']}:{l['fault']}" for l in r["labels"]})),
                         **{k: r["kpis"][k] for k in ("line_parts", "site_energy_kwh", "energy_cost_inr", "alerts",
                                                      "faults_injected", "faults_detected")}})
            done += 1
            if progress:
                progress(done, total)
    split = split_runs(runs, test_share, split_seed)
    for rows in (readings, windows, line, events, labels, runs):
        for row in rows:
            row["split"] = split[row["run_id"]]
    line_cols = LINE_BASE + sorted({k for r in line for k in r} - set(LINE_BASE))
    out_dir.mkdir(parents=True, exist_ok=True)
    for fname, rows, cols in (("readings.csv", readings, READING_COLUMNS), ("windows.csv", windows, WINDOW_COLUMNS),
                              ("line.csv", line, line_cols), ("events.csv", events, EVENT_COLUMNS),
                              ("labels.csv", labels, LABEL_COLUMNS), ("runs.csv", runs, RUN_COLUMNS)):
        _write(out_dir / fname, rows, cols)

    def balance(split_name: str | None) -> dict[str, int]:
        return dict(sorted(Counter(w["label_fault"] for w in windows
                                   if split_name is None or w["split"] == split_name).items()))

    counts = {"runs": len(runs), "train_runs": sum(r["split"] == "train" for r in runs),
              "test_runs": sum(r["split"] == "test" for r in runs), "readings": len(readings),
              "windows": len(windows), "line_rows": len(line), "events": len(events), "injected_faults": len(labels),
              "windows_by_label": balance(None), "train_windows_by_label": balance("train"),
              "test_windows_by_label": balance("test"),
              "windows_by_machine": dict(sorted(Counter(w["machine_id"] for w in windows).items())),
              "knock_on_windows": sum(w["label_knock_on"] for w in windows),
              "windows_within_10min_of_limit": sum(w["limit_within_10min"] for w in windows),
              "simulated_minutes": round(sum(r["minutes"] for r in runs), 1)}
    meta = {
        "data_label": DATA_LABEL, "synthetic": True, "real_machine_data": False,
        "notice": ("SYNTHETIC DIGITAL TWIN DATA generated by the UDYAM-TWIN factory simulator. It contains no readings "
                   "from real machines. A model trained only on it has not learned from real machines."),
        "dataset_id": dataset_id, "name": name, "generator": GENERATOR_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "simulated_clock_start": factory_run.SIM_START.isoformat().replace("+00:00", "Z"),
        "recipe": [{"spec": p.spec.__dict__, "runs": int(p.runs),
                    "variation": (p.variation or factory_run.Variation()).__dict__} for p in parts],
        "custom_machines": [m.get("machine_id") for m in custom or []],
        "machines": machines,
        "split": {"by": "run", "test_share": test_share, "seed": split_seed,
                  "train": sorted(r["run_id"] for r in runs if r["split"] == "train"),
                  "test": sorted(r["run_id"] for r in runs if r["split"] == "test")},
        "reproducible": "The same recipe, machines and split seed give identical rows.",
        "sampling": {"readings_every_s": READING_EVERY_S, "window_s": WINDOW_S, "window_every_s": STRIDE_S,
                     "line_every_s": LINE_EVERY_S, "time_to_limit_cap_min": HORIZON_MIN},
        "counts": counts,
        "checks": quality_checks(counts, runs, labels, windows),
        "features": FEATURES,
        "columns": {"readings": READING_COLUMNS, "windows": WINDOW_COLUMNS, "line": line_cols, "events": EVENT_COLUMNS,
                    "labels": LABEL_COLUMNS, "runs": RUN_COLUMNS},
        "column_notes": COLUMN_NOTES,
        "generated_in_s": round(time.time() - t0, 1),
    }
    meta["files"] = {f"{p}.csv": {"bytes": (out_dir / f"{p}.csv").stat().st_size, "sha256": _sha256(out_dir / f"{p}.csv")}
                     for p in PARTS}
    (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2))
    return meta


# -----------------------------------------------------------------------------
# Store: one collection at a time in a background thread; datasets on disk in ml/data/factory/<id>/
# -----------------------------------------------------------------------------
class FactoryDatasetStore:
    def __init__(self, root: Path = OUT_DIR):
        self.root = root
        self.lock = threading.Lock()
        self.job: dict[str, Any] | None = None
        self._cancel = False

    def path(self, dataset_id: str) -> Path:
        if not ID_RE.match(dataset_id or ""):
            raise DatasetError("invalid dataset id")
        return self.root / dataset_id

    def list(self) -> list[dict[str, Any]]:
        out = []
        if self.root.is_dir():
            for d in sorted(self.root.iterdir(), reverse=True):
                mp = d / "metadata.json"
                if ID_RE.match(d.name) and mp.is_file():
                    m = json.loads(mp.read_text())
                    out.append({k: m.get(k) for k in ("dataset_id", "name", "data_label", "created_at", "counts")}
                               | {"checks_warn": sum(c["status"] != "pass" for c in m.get("checks", [])),
                                  "bytes": sum(f["bytes"] for f in m.get("files", {}).values())})
        return out

    def metadata(self, dataset_id: str) -> dict[str, Any]:
        mp = self.path(dataset_id) / "metadata.json"
        if not mp.is_file():
            raise FileNotFoundError(dataset_id)
        return json.loads(mp.read_text())

    def file(self, dataset_id: str, part: str) -> Path:
        if part not in PARTS:
            raise DatasetError(f"part must be one of {', '.join(PARTS)}")
        p = self.path(dataset_id) / f"{part}.csv"
        if not p.is_file():
            raise FileNotFoundError(dataset_id)
        return p

    def zip_bytes(self, dataset_id: str) -> bytes:
        d = self.path(dataset_id)
        if not (d / "metadata.json").is_file():
            raise FileNotFoundError(dataset_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in ["metadata.json"] + [f"{p}.csv" for p in PARTS]:
                z.write(d / f, f"SYNTHETIC_{dataset_id}/{f}")
        return buf.getvalue()

    def preview(self, dataset_id: str, run_id: str | None, machine_id: str | None) -> dict[str, Any]:
        """One run and machine's readings (temperature, vibration, rpm, pressure, derate) with their labels."""
        meta = self.metadata(dataset_id)
        runs = meta["split"]["train"] + meta["split"]["test"]
        run_id = run_id if run_id in runs else sorted(runs)[0]
        machine_id = machine_id if machine_id in meta["machines"] else next(iter(meta["machines"]))
        pts = []
        with open(self.file(dataset_id, "readings")) as fh:
            for row in csv.DictReader(fh):
                if row["run_id"] == run_id and row["machine_id"] == machine_id:
                    pts.append({k: row[k] for k in ("t_s", "temp_c", "vibration_mm_s", "rpm", "pressure_bar",
                                                    "derate_pct", "label_fault", "label_knock_on")})
        split = "test" if run_id in meta["split"]["test"] else "train"
        return {"data_label": DATA_LABEL, "run_id": run_id, "split": split, "machine_id": machine_id,
                "runs": sorted(runs), "machines": list(meta["machines"]), "points": pts}

    def delete(self, dataset_id: str) -> None:
        p = self.path(dataset_id)
        if self.job and self.job.get("dataset_id") == dataset_id and self.job["state"] == "running":
            raise DatasetError("this dataset is still being collected")
        if not p.is_dir():
            raise FileNotFoundError(dataset_id)
        shutil.rmtree(p)

    def status(self) -> dict[str, Any]:
        return dict(self.job) if self.job else {"state": "idle"}

    def start(self, parts: list[Part], economics: dict[str, float], name: str, test_share: float, split_seed: int,
              custom: list[dict] | None) -> dict[str, Any]:
        total = check_recipe(parts, custom)
        if not (0.0 <= test_share <= 0.5):
            raise DatasetError("test share must be 0–0.5")
        name = re.sub(r"[^A-Za-z0-9 _.+()-]", "", name or "")[:60]
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise DatasetError("a factory dataset is already being collected")
            ds_id = f"fds-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
            self._cancel = False
            self.job = {"dataset_id": ds_id, "name": name, "state": "running", "done": 0, "total": total,
                        "started_at": time.time(), "error": None}
        args = (parts, economics, name, test_share, split_seed, [dict(c) for c in custom or []], ds_id)
        threading.Thread(target=self._run, args=args, daemon=True, name="factory-dataset").start()
        return self.status()

    def cancel(self) -> None:
        self._cancel = True

    def _run(self, parts, economics, name, test_share, split_seed, custom, ds_id) -> None:
        tmp = self.root / f".{ds_id}.partial"
        try:
            collect(parts, economics, tmp, ds_id, name, test_share, split_seed, custom,
                    progress=lambda d, t: self.job.update(done=d), cancelled=lambda: self._cancel)
            tmp.rename(self.root / ds_id)
            self.job.update(state="done", finished_at=time.time())
        except Exception as err:  # noqa: BLE001 - reported to the operator, never fabricated as success
            shutil.rmtree(tmp, ignore_errors=True)
            self.job.update(state="cancelled" if str(err) == "cancelled" else "failed", error=str(err),
                            finished_at=time.time())
