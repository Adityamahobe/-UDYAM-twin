"""Synthetic dataset generator: labelled data from the digital twin, clearly marked SYNTHETIC DIGITAL TWIN DATA.

Every episode runs on its own, freshly built FactoryEngine with a simulated clock. The live engine (dashboard,
Testing mode) and any real telemetry are never touched: nothing here reads from or writes to them.

An episode is one run of the virtual line where one CNC (VMC-01 or LATHE-01) gets parameters sampled from the
requested ranges (the Testing-mode parameters of sim/controls.py, within the same engineering limits) and,
optionally, one fault injected at a random time. Labels are the ground truth the generator injected; features
are only what sensors and the twin observe (tools/generate_dataset.py `window_features`, the same features the
UDYAM-SIM v1 dataset and the live predictor use).

Output, per dataset in ml/data/synthetic/<dataset_id>/:
  windows.csv    one row per 10 s window: features + labels; every row says data_label = SYNTHETIC DIGITAL TWIN DATA
  episodes.csv   one row per episode: sampled settings, fault, outcome
  metadata.json  label, generator version, full config, seed, counts, label balance, columns, file checksums
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import random
import re
import shutil
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import get_config
from sim import controls
from sim.factory import FactoryEngine, TICK_S
from tools.generate_dataset import FEATURES, HORIZON_MIN, SIM_START, STRIDE_S, WINDOW_S, window_features

DATA_LABEL = "SYNTHETIC DIGITAL TWIN DATA"
GENERATOR_VERSION = "udyam_synth_v1"
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(os.environ.get("UDYAM_SYNTHETIC_DIR", ROOT / "ml" / "data" / "synthetic"))

MACHINES = {"VMC-01": "cnc_mill", "LATHE-01": "cnc_lathe"}
FAULTS = ("normal", "coolant_degradation", "bearing_defect", "overload", "tool_wear_accel", "sensor_stuck",
          "sensor_dropout", "pressure_drop", "order_surge")
# Generator-only knobs (not dashboard parameters): sensor noise and how aggressive an injected fault is.
EXTRA_RANGES = {"noise_scale": (0.5, 2.0), "fault_severity": (0.5, 2.0)}
DEFAULT_EXTRA = {"noise_scale": (0.8, 1.3), "fault_severity": (0.7, 1.4)}
MAX_EPISODES = 200
MAX_MINUTES = 60.0
MAX_EPISODE_MINUTES = 1200.0     # episodes × minutes per request: keeps one job to a few minutes of CPU
ONSET_RANGE_S = (60.0, 600.0)
SAMPLE_TRIES = 50


class GeneratorError(ValueError):
    pass


class _SkipRiskModel:
    """The AI4I Random Forest is most of the tick time and not a feature; skipped per twin, never globally."""

    def predict(self, **_kw):
        return {"status": "SKIPPED", "failure_risk_pct": None, "detail": "skipped in synthetic dataset generation"}


_SKIP = _SkipRiskModel()


@dataclass
class GeneratorConfig:
    machine_id: str = "VMC-01"
    episodes: int = 20
    minutes: float = 10.0
    seed: int = 2026
    faults: list[str] = field(default_factory=lambda: list(FAULTS))
    # {param key: [lo, hi]} for the Testing-mode parameters; missing keys stay at their default value
    param_ranges: dict[str, list[float]] = field(default_factory=dict)
    extra_ranges: dict[str, list[float]] = field(default_factory=dict)
    auto_derate_share: float = 0.5   # share of episodes with the twin's SIM auto-derate on
    name: str = ""

    def validate(self) -> "GeneratorConfig":
        """Check every value against its limits; raise GeneratorError naming the first problem."""
        if self.machine_id not in MACHINES:
            raise GeneratorError(f"machine_id must be one of {', '.join(MACHINES)}")
        template = MACHINES[self.machine_id]
        if not (1 <= int(self.episodes) <= MAX_EPISODES):
            raise GeneratorError(f"episodes must be 1–{MAX_EPISODES}")
        if not (1.0 <= float(self.minutes) <= MAX_MINUTES):
            raise GeneratorError(f"minutes per episode must be 1–{MAX_MINUTES:g}")
        if self.episodes * self.minutes > MAX_EPISODE_MINUTES:
            raise GeneratorError(f"episodes × minutes is {self.episodes * self.minutes:g}; the limit per dataset is "
                                 f"{MAX_EPISODE_MINUTES:g} simulated minutes")
        if not self.faults or any(f not in FAULTS for f in self.faults):
            raise GeneratorError(f"faults must be a non-empty subset of {', '.join(FAULTS)}")
        if not (0.0 <= float(self.auto_derate_share) <= 1.0):
            raise GeneratorError("auto_derate_share must be 0–1")
        nameplate = get_config().nameplate(template)
        specs = {s["key"]: s for s in controls.specs_for(template, nameplate)}
        for key, rng in self.param_ranges.items():
            if key not in specs:
                raise GeneratorError(f"'{key}' is not a parameter of a {template.replace('_', ' ')}")
            lo, hi = _pair(key, rng)
            s = specs[key]
            if lo < s["min"] or hi > s["max"]:
                raise GeneratorError(f"{s['label']} range {lo:g}–{hi:g} is outside the limit {s['min']:g}–{s['max']:g} {s['unit']}")
        for key, rng in self.extra_ranges.items():
            if key not in EXTRA_RANGES:
                raise GeneratorError(f"'{key}' is not a generator setting ({', '.join(EXTRA_RANGES)})")
            lo, hi = _pair(key, rng)
            if lo < EXTRA_RANGES[key][0] or hi > EXTRA_RANGES[key][1]:
                raise GeneratorError(f"{key} range {lo:g}–{hi:g} is outside {EXTRA_RANGES[key][0]:g}–{EXTRA_RANGES[key][1]:g}")
        # the worst corner of speed × load must be possible within the rated power, or sampling would stall
        lows = {k: _pair(k, v)[0] for k, v in self.param_ranges.items()}
        try:
            controls.validate(template, nameplate, controls.defaults_for(template), lows)
        except controls.ParamError as err:
            raise GeneratorError(f"Even the lowest values of the ranges break a limit: {err}")
        self.name = re.sub(r"[^A-Za-z0-9 _.-]", "", self.name or "")[:60]
        return self

    def public(self) -> dict[str, Any]:
        return {"machine_id": self.machine_id, "episodes": int(self.episodes), "minutes": float(self.minutes),
                "seed": int(self.seed), "faults": list(self.faults),
                "param_ranges": {k: list(_pair(k, v)) for k, v in self.param_ranges.items()},
                "extra_ranges": {k: list(_pair(k, v)) for k, v in self.extra_ranges.items()},
                "auto_derate_share": float(self.auto_derate_share), "name": self.name}


def _pair(key: str, rng: Any) -> tuple[float, float]:
    try:
        lo, hi = (float(rng[0]), float(rng[1])) if isinstance(rng, (list, tuple)) else (float(rng), float(rng))
    except (TypeError, ValueError, IndexError):
        raise GeneratorError(f"{key} must be [min, max] numbers")
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo > hi:
        raise GeneratorError(f"{key} must be [min, max] with min ≤ max")
    return lo, hi


def _sample_params(cfg: GeneratorConfig, template: str, nameplate: dict, rng: random.Random) -> dict[str, float]:
    """Uniform sample inside the ranges, re-drawn until speed × load fits the rated power."""
    for _ in range(SAMPLE_TRIES):
        changes = {k: rng.uniform(*_pair(k, v)) for k, v in cfg.param_ranges.items()}
        try:
            return controls.validate(template, nameplate, controls.defaults_for(template), changes)
        except controls.ParamError:
            continue
    raise GeneratorError("Could not sample speed and load within the rated power; narrow the speed or load range")


def run_episode(cfg: GeneratorConfig, ep: int) -> tuple[list[dict], dict]:
    """One episode on its own engine. Deterministic for (config, episode)."""
    template = MACHINES[cfg.machine_id]
    nameplate = get_config().nameplate(template)
    rng = random.Random(f"{cfg.seed}:{cfg.machine_id}:{ep}")
    eng = FactoryEngine(seed=cfg.seed * 7_919 + ep, start=SIM_START, machines=4)
    for tw in eng.twins.values():
        tw.risk_model = _SKIP
    plant, comp = eng.plants[cfg.machine_id], eng.plants["COMP-01"]
    temp_limit = float(get_config().thresholds(cfg.machine_id, template)["process_temp_limit_k"])
    zones = get_config().risk_zones(cfg.machine_id, template)
    vib_zone_d = float(zones[1]) if zones else 4.5

    params = _sample_params(cfg, template, nameplate, rng)
    extra = {k: rng.uniform(*_pair(k, cfg.extra_ranges.get(k, DEFAULT_EXTRA[k]))) for k in EXTRA_RANGES}
    fault = rng.choice(list(cfg.faults))
    auto_derate = rng.random() < cfg.auto_derate_share
    duration_s = cfg.minutes * 60.0
    onset_s = rng.uniform(min(ONSET_RANGE_S[0], duration_s * 0.2), min(ONSET_RANGE_S[1], duration_s * 0.6))
    wear_start = rng.uniform(0.0, 0.85 * plant.limits["tool_wear_limit_min"])

    eng.auto_derate = auto_derate
    eng.set_params(cfg.machine_id, params)  # the same path as the dashboard's Simulation Controls
    eng.flush_events()
    plant.noise_scale, plant.fault_severity = extra["noise_scale"], extra["fault_severity"]
    plant.tool_wear_min = wear_start
    eng.twins[cfg.machine_id].pred_wear = wear_start
    # start near thermal steady state for these settings, so the first minutes are "normal"
    p_kw = controls.estimated_power_kw(template, nameplate, params)
    plant.process_temp_k = (300.0 + plant.ambient_offset_k + 8.0 + 0.7 * p_kw / (plant.h_true * plant.cooling_scale))
    eng.twins[cfg.machine_id].pred_temp = plant.process_temp_k
    over_from_settings = plant.process_temp_k >= temp_limit  # the sampled settings alone exceed the limit

    n_ticks = int(duration_s / TICK_S)
    onset_tick = int(onset_s / TICK_S) if fault != "normal" else None
    win_n, stride_n = int(WINDOW_S / TICK_S), int(STRIDE_S / TICK_S)
    buf: list[dict] = []
    over_times: list[float] = []
    rows: list[dict] = []
    for k in range(n_ticks):
        if onset_tick is not None and k == onset_tick:
            (comp if fault == "pressure_drop" else plant).faults.inject(fault, eng.t)
        msgs = eng.step()
        obs = next(m for m in msgs if m.get("type") == "observed" and m["machine_id"] == cfg.machine_id)
        tw = next(m for m in msgs if m.get("type") == "twin" and m["machine_id"] == cfg.machine_id)
        cobs = next((m for m in msgs if m.get("type") == "observed" and m["machine_id"] == "COMP-01"), {})
        buf.append({"obs": obs, "twin": tw, "pressure_bar": cobs.get("pressure_bar", 7.0)})
        if len(buf) > win_n:
            buf.pop(0)
        t_s = (k + 1) * TICK_S
        if plant.process_temp_k >= temp_limit or plant.vibration >= vib_zone_d:
            over_times.append(t_s)
        if len(buf) == win_n and (k + 1) % stride_n == 0:
            active = onset_tick is not None and k >= onset_tick
            rows.append({
                "data_label": DATA_LABEL, "episode": ep, "machine_id": cfg.machine_id, "t_s": t_s,
                **window_features(buf),
                "label_fault": fault if active else "normal",
                "since_onset_s": round(t_s - onset_s, 1) if active else -1.0,
                "true_cooling_capacity": round(plant.h_true * plant.cooling_scale, 4),
                "true_process_temp_k": round(plant.process_temp_k, 3),
            })
    for row in rows:
        nxt = next((t for t in over_times if t >= row["t_s"]), None)
        mins = HORIZON_MIN if nxt is None else min(HORIZON_MIN, (nxt - row["t_s"]) / 60.0)
        row["minutes_to_limit"] = round(mins, 3)
        row["limit_within_10min"] = int(mins < 10.0)
    meta = {
        "data_label": DATA_LABEL, "episode": ep, "machine_id": cfg.machine_id, "fault": fault,
        "onset_s": round(onset_s, 1) if fault != "normal" else None, "auto_derate": int(auto_derate),
        **{k: round(v, 4) for k, v in params.items()}, **{k: round(v, 4) for k, v in extra.items()},
        "tool_wear_start_min": round(wear_start, 2), "minutes": cfg.minutes,
        "reached_limit": int(bool(over_times)), "limit_at_s": over_times[0] if over_times else None,
        "over_limit_from_settings": int(over_from_settings),
        "final_derate_pct": plant.derate_pct, "parts_done": plant.parts_done,
    }
    return rows, meta


WINDOW_COLUMNS = (["data_label", "episode", "machine_id", "t_s"] + FEATURES +
                  ["label_fault", "since_onset_s", "true_cooling_capacity", "true_process_temp_k",
                   "minutes_to_limit", "limit_within_10min"])

COLUMN_NOTES = {
    "data_label": f"Always '{DATA_LABEL}'. Not measured on a real machine.",
    "label_fault": "Ground truth: the fault the generator injected (normal before onset). Never inferred.",
    "since_onset_s": "Seconds since the injected fault started (-1 before onset or with no fault).",
    "true_cooling_capacity": "Hidden plant cooling capacity (fault and parameter), not observable by sensors.",
    "true_process_temp_k": "Plant temperature without sensor noise or sensor faults.",
    "minutes_to_limit": f"Minutes until the temperature limit or vibration zone D is reached, capped at {HORIZON_MIN:g}.",
    "limit_within_10min": "1 if minutes_to_limit < 10.",
    "over_limit_from_settings": "Episode file: 1 if the sampled operating settings alone put the machine over its "
                                "temperature limit from the start (not caused by the fault).",
    "features": "Window statistics over the last 60 s of observed and twin frames (tools/generate_dataset.py).",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def generate(cfg: GeneratorConfig, out_dir: Path, dataset_id: str,
             progress: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Run every episode and write windows.csv, episodes.csv and metadata.json. Returns the metadata."""
    cfg.validate()
    t0 = time.time()
    rows: list[dict] = []
    episodes: list[dict] = []
    for ep in range(cfg.episodes):
        if cancelled and cancelled():
            raise GeneratorError("cancelled")
        r, m = run_episode(cfg, ep)
        rows.extend(r)
        episodes.append(m)
        if progress:
            progress(ep + 1, cfg.episodes)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "windows.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=WINDOW_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    ep_cols = list(episodes[0].keys())
    with open(out_dir / "episodes.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ep_cols)
        w.writeheader()
        w.writerows(episodes)
    meta = {
        "data_label": DATA_LABEL,
        "synthetic": True,
        "real_machine_data": False,
        "notice": ("SYNTHETIC DIGITAL TWIN DATA generated by the UDYAM-TWIN simulator. It contains no readings from "
                   "real machines. A model trained only on it has not learned from real machines."),
        "dataset_id": dataset_id,
        "name": cfg.name,
        "generator": GENERATOR_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "simulated_clock_start": SIM_START.isoformat().replace("+00:00", "Z"),
        "config": cfg.public(),
        "reproducible": "The same config and seed give identical rows.",
        "machine_template": MACHINES[cfg.machine_id],
        "parameter_limits": controls.specs_for(MACHINES[cfg.machine_id], get_config().nameplate(MACHINES[cfg.machine_id])),
        "window_s": WINDOW_S, "stride_s": STRIDE_S, "horizon_min": HORIZON_MIN,
        "counts": {"episodes": len(episodes), "windows": len(rows),
                   "episodes_by_fault": dict(Counter(e["fault"] for e in episodes)),
                   "windows_by_label": dict(Counter(r["label_fault"] for r in rows)),
                   "episodes_reaching_limit": sum(e["reached_limit"] for e in episodes),
                   "episodes_over_limit_from_settings": sum(e["over_limit_from_settings"] for e in episodes)},
        "features": FEATURES,
        "columns": {"windows": WINDOW_COLUMNS, "episodes": ep_cols},
        "column_notes": COLUMN_NOTES,
        "generated_in_s": round(time.time() - t0, 1),
    }
    meta["files"] = {name: {"bytes": (out_dir / name).stat().st_size, "sha256": _sha256(out_dir / name)}
                     for name in ("windows.csv", "episodes.csv")}
    (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2))
    return meta


# -----------------------------------------------------------------------------
# Jobs: one generation at a time, in a background thread, with progress
# -----------------------------------------------------------------------------
ID_RE = re.compile(r"^synth-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


class SyntheticStore:
    def __init__(self, root: Path = OUT_DIR):
        self.root = root
        self.lock = threading.Lock()
        self.job: dict[str, Any] | None = None
        self._cancel = False

    def path(self, dataset_id: str) -> Path:
        if not ID_RE.match(dataset_id or ""):
            raise GeneratorError("invalid dataset id")
        return self.root / dataset_id

    def list(self) -> list[dict[str, Any]]:
        out = []
        if self.root.is_dir():
            for d in sorted(self.root.iterdir(), reverse=True):
                mp = d / "metadata.json"
                if ID_RE.match(d.name) and mp.is_file():
                    m = json.loads(mp.read_text())
                    out.append({k: m.get(k) for k in ("dataset_id", "name", "data_label", "created_at", "config", "counts", "files")})
        return out

    def metadata(self, dataset_id: str) -> dict[str, Any]:
        mp = self.path(dataset_id) / "metadata.json"
        if not mp.is_file():
            raise FileNotFoundError(dataset_id)
        return json.loads(mp.read_text())

    def delete(self, dataset_id: str) -> None:
        p = self.path(dataset_id)
        if self.job and self.job.get("dataset_id") == dataset_id and self.job["state"] == "running":
            raise GeneratorError("this dataset is still being generated")
        if not p.is_dir():
            raise FileNotFoundError(dataset_id)
        shutil.rmtree(p)

    def status(self) -> dict[str, Any] | None:
        return dict(self.job) if self.job else None

    def start(self, cfg: GeneratorConfig) -> dict[str, Any]:
        cfg.validate()
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise GeneratorError("a dataset is already being generated")
            ds_id = f"synth-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
            self._cancel = False
            self.job = {"dataset_id": ds_id, "state": "running", "done": 0, "total": cfg.episodes,
                        "started_at": time.time(), "error": None, "config": cfg.public()}
        threading.Thread(target=self._run, args=(cfg, ds_id), daemon=True, name="synthetic-generator").start()
        return self.status()

    def cancel(self) -> None:
        self._cancel = True

    def _run(self, cfg: GeneratorConfig, ds_id: str) -> None:
        tmp = self.root / f".{ds_id}.partial"
        try:
            def progress(done, total):
                self.job["done"] = done
            generate(cfg, tmp, ds_id, progress=progress, cancelled=lambda: self._cancel)
            tmp.rename(self.root / ds_id)
            self.job.update(state="done", finished_at=time.time())
        except Exception as err:  # noqa: BLE001 - reported to the operator, never fabricated as success
            shutil.rmtree(tmp, ignore_errors=True)
            self.job.update(state="cancelled" if str(err) == "cancelled" else "failed", error=str(err),
                            finished_at=time.time())
