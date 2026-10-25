"""Adaptive learning: input-drift monitoring for the failure-risk model, and operator-confirmed labels.

Drift monitor (per machine that uses the AI4I model, and per telemetry source; SIM / REPLAY / LIVE are never mixed)
- Model coverage: is the machine operating where the production model has training data? Each sample's
  distance to its nearest training row (standardised inputs) is compared with the 99th percentile of the
  training rows' own nearest-neighbour distances. A distribution test against the whole training set (PSI, KS)
  is not used on purpose: AI4I covers many operating points and one machine in steady state is a narrow slice
  of it, so such tests flag "drift" all the time.
- Machine drift: has the machine moved away from its own baseline (the first 5 minutes after start or reset, or
  a baseline the operator sets)? Shift of the 5-minute mean in units of the baseline spread (with a floor).
  Tool wear is excluded: it grows by design.
Drift is information, not a fault: it raises a DATA_DRIFT event, never an alert, and triggers nothing. It may
recommend labelling events or training a candidate; an operator decides.

Operator-confirmed labels
- An operator labels a time window of one machine: failure, false alarm or normal operation. The model inputs
  buffered for that window are copied into the label record, with their source (LIVE = real device).
- Unlabelled readings are never treated as faults or used as training labels.
- LIVE windows are accepted only if the device itself sent all five model inputs; otherwise some values would
  be the simulation's (see hub/core.py `live_fields`) and the label would describe a mix.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.neighbors import NearestNeighbors

from config import get_config
from ml.ai4i import FEATURES, load_dataset

SAMPLE_EVERY_TICKS = 4          # one sample every 2 s per machine
BUFFER_N = 900                  # 30 min of samples kept per machine and source
BASELINE_N = 150                # 5 min baseline
RECENT_N = 150                  # 5 min evaluation window
MIN_N = 60                      # 2 min of complete samples before any verdict
EVALUATE_EVERY = 15             # re-evaluate every 30 s per machine and source
COVERAGE_WATCH, COVERAGE_OOD = 0.05, 0.25   # share of recent samples outside the training region
DRIFT_WATCH, DRIFT_ALERT = 2.0, 3.0         # mean shift in baseline-spread units
SHIFT_FLOOR = {"air_temp_k": 0.5, "process_temp_k": 0.5, "rpm": 20.0, "torque_nm": 2.0}
DRIFT_FEATURES = [f for f in FEATURES if f != "tool_wear_min"]
MAX_LABEL_WINDOW = timedelta(minutes=30)
LABELS = {"failure": 1, "false_alarm": 0, "normal": 0}
LABEL_TEXT = {"failure": "Confirmed failure", "false_alarm": "False alarm", "normal": "Normal operation"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# -----------------------------------------------------------------------------
# Training reference (what the production model has seen)
# -----------------------------------------------------------------------------
class TrainingReference:
    def __init__(self, X: np.ndarray, version: str, approximate: bool = False, seed: int = 0):
        self.version, self.approximate = version, approximate
        self.mean, self.std = X.mean(axis=0), X.std(axis=0) + 1e-9
        Z = (X - self.mean) / self.std
        self.nn = NearestNeighbors(n_neighbors=2).fit(Z)
        rng = np.random.default_rng(seed)
        probe = Z[rng.choice(len(Z), size=min(2000, len(Z)), replace=False)]
        self_dist = self.nn.kneighbors(probe, n_neighbors=2)[0][:, 1]  # [:, 0] is the row itself
        self.threshold = float(np.percentile(self_dist, 99))
        self.ranges = {f: [float(X[:, i].min()), float(X[:, i].max())] for i, f in enumerate(FEATURES)}
        self.rows = int(len(X))

    def outside(self, X: np.ndarray) -> np.ndarray:
        d = self.nn.kneighbors((X - self.mean) / self.std, n_neighbors=1)[0][:, 0]
        return d > self.threshold

    def public(self) -> dict[str, Any]:
        return {"version": self.version, "rows": self.rows, "approximate": self.approximate,
                "distance_threshold": round(self.threshold, 3), "ranges": self.ranges}


def ai4i_training_inputs(seed: int = 42) -> np.ndarray:
    """AI4I training split inputs, exactly as ml/train_ai4i.py splits them."""
    df = load_dataset()
    idx_train, _ = train_test_split(df.index, test_size=0.2, random_state=seed, stratify=df["machine_failure"])
    return df.loc[idx_train, FEATURES].to_numpy(dtype=float)


def save_reference(path: Path, X: np.ndarray) -> None:
    np.savez_compressed(path, X=X.astype(np.float32))


def load_reference(registry, version: str) -> TrainingReference:
    """The version's training inputs (reference.npz); built from AI4I for versions trained before it existed."""
    path = registry.root / version / "reference.npz"
    if path.is_file():
        return TrainingReference(np.load(path)["X"].astype(float), version)
    X = ai4i_training_inputs()
    baseline = registry.card(version)["kind"] == "baseline"
    if baseline:
        save_reference(path, X)
    return TrainingReference(X, version, approximate=not baseline)


# -----------------------------------------------------------------------------
# Drift monitor
# -----------------------------------------------------------------------------
def uses_failure_model(machine_id: str, template: str | None) -> bool:
    try:
        return (get_config().ml(machine_id, template) or {}).get("failure_model") == "ai4i"
    except Exception:  # noqa: BLE001 - unknown machine: not monitored
        return template == "cnc_mill"


class DriftMonitor:
    def __init__(self, reference: Callable[[], TrainingReference]):
        self.reference = reference
        self.lock = threading.Lock()
        self.buffers: dict[tuple[str, str], deque] = {}
        self.baselines: dict[tuple[str, str], dict[str, Any]] = {}
        self.results: dict[tuple[str, str], dict[str, Any]] = {}
        self.ticks: dict[str, int] = {}
        self.since_eval: dict[tuple[str, str], int] = {}
        self.segment_start: dict[tuple[str, str], str] = {}  # evaluation only looks after the last reset
        self.pending_events: list[dict[str, Any]] = []

    # -- input ------------------------------------------------------------------
    def observe(self, msgs) -> None:
        for m in msgs:
            if m.get("type") == "event" and m.get("kind") == "RESET":
                self.reset_source("SIM", m.get("ts"))
                continue
            if m.get("type") != "observed" or not uses_failure_model(m.get("machine_id"), m.get("template")):
                continue
            mid = m["machine_id"]
            n = self.ticks[mid] = self.ticks.get(mid, 0) + 1
            if n % SAMPLE_EVERY_TICKS:
                continue
            source = m.get("source_mode") or "SIM"
            x = {f: m.get(f) for f in FEATURES}
            have = all(isinstance(v, (int, float)) for v in x.values())
            if source == "LIVE":  # only fields the device sent are real; the rest are the simulation's
                have = have and set(FEATURES) <= set(m.get("live_fields") or [])
            sample = {"ts": m["ts"], "x": x, "complete": have, "plc_state": m.get("plc_state")}
            key = (mid, source)
            with self.lock:
                buf = self.buffers.setdefault(key, deque(maxlen=BUFFER_N))
                buf.append(sample)
                base = self.baselines.get(key)
                if base is None or (base["auto"] and len(base["rows"]) < BASELINE_N):
                    if base is None:
                        base = self.baselines[key] = {"rows": [], "auto": True, "set_at": None, "set_by": "automatic"}
                    if have:
                        base["rows"].append(x)
                        if len(base["rows"]) == BASELINE_N:
                            base["set_at"] = m["ts"]
                k = self.since_eval[key] = self.since_eval.get(key, 0) + 1
            if k >= EVALUATE_EVERY:
                self.since_eval[key] = 0
                self.evaluate(mid, source)

    def reset_source(self, source: str, ts: str | None = None) -> None:
        """A simulation reset starts a fresh baseline and evaluation window for simulated data. The buffer is kept
        (an operator may still label what happened just before the reset); real-device data is untouched."""
        ts = ts or _iso(datetime.now(timezone.utc))
        with self.lock:
            for key in [k for k in self.buffers if k[1] in (source, "REPLAY")]:
                self.baselines.pop(key, None)
                self.results.pop(key, None)
                self.since_eval.pop(key, None)
                self.segment_start[key] = ts

    def set_baseline(self, machine_id: str, source: str, by: str) -> dict[str, Any]:
        key = (machine_id, source)
        with self.lock:
            after = self.segment_start.get(key, "")
            rows = [s["x"] for s in self.buffers.get(key, []) if s["complete"] and s["ts"] > after][-BASELINE_N:]
            if len(rows) < MIN_N:
                raise ValueError(f"need at least {MIN_N} complete samples ({MIN_N * 2 // 60} min) of {source} data "
                                 f"from {machine_id}; have {len(rows)}")
            self.baselines[key] = {"rows": rows, "auto": False, "set_at": _iso(datetime.now(timezone.utc)), "set_by": by}
        return self.evaluate(machine_id, source)

    # -- evaluation -------------------------------------------------------------
    def evaluate(self, machine_id: str, source: str) -> dict[str, Any]:
        key = (machine_id, source)
        with self.lock:
            after = self.segment_start.get(key, "")
            recent = [s["x"] for s in self.buffers.get(key, []) if s["complete"] and s["ts"] > after][-RECENT_N:]
            base = self.baselines.get(key)
            base_rows = list(base["rows"]) if base else []
            prev = self.results.get(key, {})
        res: dict[str, Any] = {"machine_id": machine_id, "source": source, "samples": len(recent),
                               "evaluated_at": _iso(datetime.now(timezone.utc))}
        if len(recent) < MIN_N:
            res.update(coverage={"status": "INSUFFICIENT_DATA"}, drift={"status": "INSUFFICIENT_DATA"},
                       message=f"Collecting data: {len(recent)} of {MIN_N} complete samples")
        else:
            X = np.array([[r[f] for f in FEATURES] for r in recent], dtype=float)
            ref = self.reference()
            out = ref.outside(X)
            share = float(out.mean())
            per_feature = {f: round(float(((X[:, i] < ref.ranges[f][0]) | (X[:, i] > ref.ranges[f][1])).mean()), 3)
                           for i, f in enumerate(FEATURES)}
            cov_status = "OUT_OF_DISTRIBUTION" if share >= COVERAGE_OOD else "WATCH" if share >= COVERAGE_WATCH else "OK"
            res["coverage"] = {"status": cov_status, "outside_share": round(share, 3), "model_version": ref.version,
                               "reference_approximate": ref.approximate, "outside_training_range": per_feature,
                               "recent_means": {f: round(float(X[:, i].mean()), 2) for i, f in enumerate(FEATURES)}}
            if len(base_rows) < MIN_N:
                res["drift"] = {"status": "NO_BASELINE", "baseline_samples": len(base_rows)}
            else:
                B = np.array([[r[f] for f in DRIFT_FEATURES] for r in base_rows], dtype=float)
                R = np.array([[r[f] for f in DRIFT_FEATURES] for r in recent], dtype=float)
                shifts = {}
                for i, f in enumerate(DRIFT_FEATURES):
                    scale = max(float(B[:, i].std()), SHIFT_FLOOR[f])
                    shifts[f] = {"baseline_mean": round(float(B[:, i].mean()), 2), "recent_mean": round(float(R[:, i].mean()), 2),
                                 "shift": round((float(R[:, i].mean()) - float(B[:, i].mean())) / scale, 2)}
                worst = max(shifts, key=lambda f: abs(shifts[f]["shift"]))
                z = abs(shifts[worst]["shift"])
                res["drift"] = {"status": "DRIFT" if z >= DRIFT_ALERT else "WATCH" if z >= DRIFT_WATCH else "OK",
                                "largest": worst, "features": shifts,
                                "baseline": {"samples": len(base_rows), "set_at": base["set_at"], "set_by": base["set_by"]}}
            res["message"] = self._message(res)
            res["recommendation"] = self._recommend(res)
        with self.lock:
            self.results[key] = res
        self._transition(prev, res)
        return res

    @staticmethod
    def _message(res: dict[str, Any]) -> str:
        cov, dr = res["coverage"], res["drift"]
        parts = [f"{100 * cov['outside_share']:.0f} % of the last {res['samples'] * 2 // 60} min is outside the "
                 f"{cov['model_version']} model's training data"]
        if dr["status"] in ("OK", "WATCH", "DRIFT"):
            f = dr["largest"]
            parts.append(f"largest change vs baseline: {f} {dr['features'][f]['shift']:+.1f}×")
        return "; ".join(parts)

    @staticmethod
    def _recommend(res: dict[str, Any]) -> str | None:
        if res["coverage"]["status"] == "OUT_OF_DISTRIBUTION":
            return ("The failure-risk model has little training data for these conditions: treat its risk % with "
                    "caution. Label events from this period, or generate synthetic data covering these conditions, "
                    "then train a candidate. Nothing is retrained automatically.")
        if res["drift"]["status"] == "DRIFT":
            return ("The machine is operating differently from its baseline. Check whether this is intended (new job, "
                    "new settings) and set a new baseline, or inspect it. Drift alone is not a fault.")
        return None

    def _transition(self, prev: dict[str, Any], res: dict[str, Any]) -> None:
        def bad(r):
            return bool(r) and (r.get("coverage", {}).get("status") == "OUT_OF_DISTRIBUTION"
                                or r.get("drift", {}).get("status") == "DRIFT")
        if bad(res) and not bad(prev):
            self.pending_events.append({"machine_id": res["machine_id"], "severity": "WARN",
                                        "title": f"Input drift on {res['machine_id']} ({res['source']}): {res['message']}",
                                        "data": {"source": res["source"], "coverage": res["coverage"]["status"],
                                                 "drift": res["drift"]["status"], "not_a_fault": True}})
        elif bad(prev) and not bad(res) and res["coverage"]["status"] != "INSUFFICIENT_DATA":
            self.pending_events.append({"machine_id": res["machine_id"], "severity": "INFO",
                                        "title": f"Input drift cleared on {res['machine_id']} ({res['source']})",
                                        "data": {"source": res["source"]}})

    def take_events(self) -> list[dict[str, Any]]:
        out, self.pending_events = self.pending_events, []
        return out

    def status(self) -> list[dict[str, Any]]:
        with self.lock:
            keys = sorted(self.buffers)
            have = {k: sum(1 for s in self.buffers[k] if s["complete"] and s["ts"] > self.segment_start.get(k, ""))
                    for k in keys}
            results = {k: self.results.get(k) for k in keys}
        return [results[k] or {"machine_id": k[0], "source": k[1], "samples": have[k],
                               "coverage": {"status": "INSUFFICIENT_DATA"}, "drift": {"status": "INSUFFICIENT_DATA"},
                               "message": f"Collecting data: {have[k]} complete samples"} for k in keys]

    def window(self, machine_id: str, source: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        with self.lock:
            return [s for s in self.buffers.get((machine_id, source), []) if start <= _parse(s["ts"]) <= end]

    def buffered_since(self, machine_id: str, source: str) -> str | None:
        with self.lock:
            buf = self.buffers.get((machine_id, source))
            return buf[0]["ts"] if buf else None


# -----------------------------------------------------------------------------
# Operator-confirmed labels
# -----------------------------------------------------------------------------
class LabelError(ValueError):
    pass


class LabelStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.lock:
            conn = self._conn()
            with conn:
                conn.execute("""
                CREATE TABLE IF NOT EXISTS confirmed_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL, machine_id TEXT NOT NULL, source TEXT NOT NULL,
                    start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, label TEXT NOT NULL, fault_type TEXT,
                    alert_id TEXT, operator TEXT NOT NULL, note TEXT, n_samples INTEGER NOT NULL,
                    samples_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE',
                    retracted_by TEXT, retracted_at TEXT
                );""")
            conn.close()  # sqlite3's context manager commits but does not close

    def create(self, *, machine_id: str, source: str, start: datetime, end: datetime, label: str,
               samples: list[dict[str, Any]], operator: str, fault_type: str | None = None,
               alert_id: str | None = None, note: str | None = None) -> dict[str, Any]:
        if label not in LABELS:
            raise LabelError(f"label must be one of {', '.join(LABELS)}")
        if end <= start or end - start > MAX_LABEL_WINDOW:
            raise LabelError("the window must end after it starts and be at most 30 minutes long")
        if label != "failure" and fault_type:
            raise LabelError("a fault type only goes with a confirmed failure")
        complete = [s for s in samples if s["complete"]]
        if not samples:
            raise LabelError(f"no {source} readings from {machine_id} are buffered for that window "
                             "(the last 30 minutes are kept)")
        if not complete:
            raise LabelError(f"the {source} readings in that window lack some of the model's inputs "
                             "(air temp, process temp, rpm, torque, tool wear)"
                             + (": the device does not send all of them, so the rest would be simulated values" if source == "LIVE" else ""))
        rows = [{"ts": s["ts"], **s["x"]} for s in complete]
        now = _iso(datetime.now(timezone.utc))
        with self.lock:
            conn = self._conn()
            with conn:
                cur = conn.execute(
                    "INSERT INTO confirmed_labels (created_at, machine_id, source, start_ts, end_ts, label, fault_type, "
                    "alert_id, operator, note, n_samples, samples_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?);",
                    (now, machine_id, source, _iso(start), _iso(end), label, fault_type, alert_id, operator, note,
                     len(rows), json.dumps(rows)))
                lid = cur.lastrowid
            conn.close()
        return self.get(lid)

    def _row(self, r: sqlite3.Row, with_samples: bool = False) -> dict[str, Any]:
        d = dict(r)
        samples = json.loads(d.pop("samples_json"))
        if with_samples:
            d["samples"] = samples
        d["label_text"] = LABEL_TEXT.get(d["label"], d["label"])
        d["data_origin"] = "real machine" if d["source"] == "LIVE" else "simulation"
        return d

    def get(self, label_id: int, with_samples: bool = False) -> dict[str, Any]:
        conn = self._conn()
        r = conn.execute("SELECT * FROM confirmed_labels WHERE id = ?;", (label_id,)).fetchone()
        conn.close()
        if r is None:
            raise LabelError(f"no label {label_id}")
        return self._row(r, with_samples)

    def list(self, include_retracted: bool = False, with_samples: bool = False) -> list[dict[str, Any]]:
        conn = self._conn()
        q = "SELECT * FROM confirmed_labels" + ("" if include_retracted else " WHERE status = 'ACTIVE'") + " ORDER BY id DESC;"
        rows = [self._row(r, with_samples) for r in conn.execute(q).fetchall()]
        conn.close()
        return rows

    def retract(self, label_id: int, operator: str) -> dict[str, Any]:
        """Labels are never deleted: a retracted label stays in the audit trail but is not used for training."""
        cur = self.get(label_id)
        if cur["status"] != "ACTIVE":
            raise LabelError(f"label {label_id} is already retracted")
        with self.lock:
            conn = self._conn()
            with conn:
                conn.execute("UPDATE confirmed_labels SET status = 'RETRACTED', retracted_by = ?, retracted_at = ? WHERE id = ?;",
                             (operator, _iso(datetime.now(timezone.utc)), label_id))
            conn.close()
        return self.get(label_id)

    def training_rows(self) -> list[dict[str, Any]]:
        """One row per labelled sample: model inputs, 0/1 target, source and the label it came from."""
        out = []
        for lab in self.list(with_samples=True):
            for s in lab["samples"]:
                out.append({**{f: s[f] for f in FEATURES}, "machine_failure": LABELS[lab["label"]],
                            "label_id": lab["id"], "source": lab["source"], "ts": s["ts"]})
        return out
