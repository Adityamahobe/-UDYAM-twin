"""Generate UDYAM-SIM: a labelled training dataset from our own virtual factory.

Each *episode* is one randomised run of the full 4-machine line (sim/factory.py):
a slightly different machine (cooling capacity, load, ambient, sensor noise,
starting tool wear), optionally one injected fault with random onset and severity,
and supervised self-healing on or off.

Why this is not circular: the LABEL is the fault the simulated plant was actually
given (ground truth the twin never sees). The FEATURES are only what sensors and the
twin can observe. A model must learn the fault from the signals.

Outputs (in --out, default ml/data/):
  udyam_sim_v1_windows.csv   one row per 10 s window of VMC-01 (features + labels)
  udyam_sim_v1_episodes.csv  one row per episode (settings, fault, outcome)
  udyam_sim_v1_meta.json     generator version, ranges, seed, counts

Usage:
  python -m tools.generate_dataset                    # 400 episodes x 30 min (~a few minutes)
  python -m tools.generate_dataset --episodes 40 --minutes 10   # quick test
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import twin.model as twin_model
from config import get_config
from sim.factory import FactoryEngine, TICK_S

DATASET_VERSION = "udyam_sim_v1"
ROOT = Path(__file__).resolve().parent.parent
HERO = "VMC-01"
WINDOW_S = 60.0     # features look back 60 s
STRIDE_S = 10.0     # one row every 10 s
HORIZON_MIN = 30.0  # time-to-overtemp is capped (censored) at 30 min

# Fault classes for the hero machine. pressure_drop is injected on the compressor
# (common cause) but labelled from the VMC's point of view.
FAULT_WEIGHTS = {
    "normal": 0.22,
    "coolant_degradation": 0.16,
    "bearing_defect": 0.12,
    "overload": 0.10,
    "tool_wear_accel": 0.10,
    "sensor_stuck": 0.08,
    "pressure_drop": 0.10,
    "order_surge": 0.12,
}

RANGES = {
    "h_true": (0.92, 1.10),
    "ambient_offset_k": (-3.0, 1.0),
    "load_scale": (0.85, 1.12),
    "noise_scale": (0.7, 1.6),
    "fault_severity": (0.6, 1.6),
    "tool_wear_start": (0.0, 200.0),
    "onset_s": (60.0, 600.0),
}

# Out-of-distribution test machines: heavier load than anything in training.
OOD_LOAD = (1.12, 1.22)


class _NoAI4I:
    """Skip the AI4I Random Forest during generation (it is ~96 % of tick time and not a feature)."""

    def predict(self, **_kw):
        return {"status": "SKIPPED", "failure_risk_pct": None, "detail": "skipped in dataset generation"}


_NO_AI4I = _NoAI4I()
SIM_START = datetime(2026, 10, 26, 10, 0, 0, tzinfo=timezone.utc)  # simulated clock: reproducible


def hero_limits() -> tuple[float, float]:
    """(process temperature limit K, ISO-referenced vibration zone D boundary mm/s) for VMC-01."""
    cfg = get_config()
    temp = float(cfg.thresholds(HERO, "cnc_mill")["process_temp_limit_k"])
    zones = cfg.risk_zones(HERO, "cnc_mill")
    return temp, float(zones[1]) if zones else 4.5


def _u(rng: random.Random, key: str) -> float:
    lo, hi = RANGES[key]
    return rng.uniform(lo, hi)


def _slope(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2.0
    my = statistics.fmean(xs)
    num = sum((i - mx) * (x - my) for i, x in enumerate(xs))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den / TICK_S  # per second


def _stats(prefix: str, xs: list[float]) -> dict:
    if not xs:
        return {f"{prefix}_mean": 0.0, f"{prefix}_std": 0.0, f"{prefix}_slope": 0.0, f"{prefix}_max": 0.0}
    return {
        f"{prefix}_mean": statistics.fmean(xs),
        f"{prefix}_std": statistics.pstdev(xs) if len(xs) > 1 else 0.0,
        f"{prefix}_slope": _slope(xs),
        f"{prefix}_max": max(xs),
    }


def window_features(buf: list[dict]) -> dict:
    """Features from the last WINDOW_S of observed + twin frames. Used for training AND live."""
    obs = [b["obs"] for b in buf]
    tw = [b["twin"] for b in buf]
    temps = [o["process_temp_k"] for o in obs if o.get("process_temp_k") is not None]
    f: dict = {}
    f.update(_stats("temp", temps))
    f.update(_stats("vib", [float(o.get("vibration_rms_mm_s") or 0.0) for o in obs]))
    f.update(_stats("cur", [float(o.get("current_a") or 0.0) for o in obs]))
    f.update(_stats("torque", [float(o.get("torque_nm") or 0.0) for o in obs]))
    f.update(_stats("rpm", [float(o.get("rpm") or 0.0) for o in obs]))
    f.update(_stats("wear", [float(o.get("tool_wear_min") or 0.0) for o in obs]))
    f["air_temp_mean"] = statistics.fmean(float(o.get("air_temp_k") or 300.0) for o in obs)
    f["delta_t"] = (f["temp_mean"] - f["air_temp_mean"]) if temps else 0.0
    f["dq_bad_frac"] = sum(1 for o in obs if o.get("dq_flag") != "OK") / len(obs)
    f["derate_pct"] = float(obs[-1].get("derate_pct") or 0.0)
    f["air_pressure_bar"] = statistics.fmean(float(b.get("pressure_bar", 7.0)) for b in buf)
    res = [float(t["residual"]["value"]) for t in tw]
    f.update(_stats("resid", res))
    f["resid_ewma"] = float(tw[-1]["residual"]["ewma"])
    f["resid_cusum"] = float(tw[-1]["residual"]["cusum"])
    vals = tw[-1]["params"]["values"]
    f["h_cool_est"] = float(vals.get("h_cool", 1.0))
    f["h_cool_drift"] = float(vals.get("h_cool", 1.0)) - float(tw[0]["params"]["values"].get("h_cool", 1.0))
    f["k_wear_est"] = float(vals.get("k_wear", 1.0))
    f["calib_frozen"] = 1.0 if tw[-1]["params"].get("frozen") else 0.0
    m = tw[-1].get("forecast", {}).get("minutes_to_warning")
    f["twin_forecast_min"] = min(HORIZON_MIN, float(m)) if m is not None else HORIZON_MIN
    return {k: round(v, 5) for k, v in f.items()}


FEATURES = list(window_features([{
    "obs": {"process_temp_k": 311.0, "vibration_rms_mm_s": 2.0, "dq_flag": "OK"},
    "twin": {"residual": {"value": 0, "ewma": 0, "cusum": 0}, "params": {"values": {}}, "forecast": {}},
}] * 2).keys())


def run_episode(ep: int, minutes: float, base_seed: int, ood: bool = False) -> tuple[list[dict], dict]:
    """One randomised episode. AI4I scoring is switched off only for its duration."""
    original = twin_model.ai4i_predictor
    twin_model.ai4i_predictor = lambda: _NO_AI4I
    try:
        return _run_episode(ep, minutes, base_seed, ood)
    finally:
        twin_model.ai4i_predictor = original


def _run_episode(ep: int, minutes: float, base_seed: int, ood: bool) -> tuple[list[dict], dict]:
    rng = random.Random(base_seed * 100_003 + ep)
    eng = FactoryEngine(seed=base_seed * 7_919 + ep, start=SIM_START, machines=4)
    temp_limit, vib_zone_d = hero_limits()
    mill = eng.plants[HERO]
    comp = eng.plants["COMP-01"]

    settings = {
        "h_true": _u(rng, "h_true"),
        "ambient_offset_k": _u(rng, "ambient_offset_k"),
        "load_scale": rng.uniform(*OOD_LOAD) if ood else _u(rng, "load_scale"),
        "noise_scale": _u(rng, "noise_scale"),
        "fault_severity": _u(rng, "fault_severity"),
        "tool_wear_start": _u(rng, "tool_wear_start"),
        "onset_s": _u(rng, "onset_s"),
    }
    fault = rng.choices(list(FAULT_WEIGHTS), weights=list(FAULT_WEIGHTS.values()))[0]
    auto_derate = rng.random() < 0.5
    eng.auto_derate = auto_derate

    mill.h_true = settings["h_true"]
    mill.ambient_offset_k = settings["ambient_offset_k"]
    mill.load_scale = settings["load_scale"]
    mill.noise_scale = settings["noise_scale"]
    mill.fault_severity = settings["fault_severity"]
    mill.tool_wear_min = settings["tool_wear_start"]
    eng.twins[HERO].pred_wear = settings["tool_wear_start"]
    # Start near thermal steady state for this machine so the first minutes are "normal".
    rpm_nom = float(mill.nameplate["rpm_nominal"])
    p_kw = 40.0 * settings["load_scale"] * rpm_nom * 2.0 * math.pi / 60.0 / 1000.0
    mill.process_temp_k = 300.0 + settings["ambient_offset_k"] + 8.0 + 0.7 * p_kw / settings["h_true"]
    eng.twins[HERO].pred_temp = mill.process_temp_k

    n_ticks = int(minutes * 60 / TICK_S)
    onset_tick = int(settings["onset_s"] / TICK_S) if fault != "normal" else None
    buf: list[dict] = []
    ticks: list[dict] = []  # per-tick ground truth for labels
    win_n = int(WINDOW_S / TICK_S)
    stride_n = int(STRIDE_S / TICK_S)
    rows: list[dict] = []
    first_overtemp_s = None

    for k in range(n_ticks):
        if onset_tick is not None and k == onset_tick:
            target = comp if fault == "pressure_drop" else mill
            target.faults.inject(fault, eng.t)
        msgs = eng.step()
        obs = next(m for m in msgs if m.get("type") == "observed" and m["machine_id"] == HERO)
        tw = next(m for m in msgs if m.get("type") == "twin" and m["machine_id"] == HERO)
        cobs = next((m for m in msgs if m.get("type") == "observed" and m["machine_id"] == "COMP-01"), {})
        buf.append({"obs": obs, "twin": tw, "pressure_bar": cobs.get("pressure_bar", 7.0)})
        if len(buf) > win_n:
            buf.pop(0)
        t_s = (k + 1) * TICK_S
        true_temp = mill.process_temp_k
        over = true_temp >= temp_limit or mill.vibration >= vib_zone_d
        if over and first_overtemp_s is None:
            first_overtemp_s = t_s
        ticks.append({"t": t_s, "over": over})

        if len(buf) == win_n and (k + 1) % stride_n == 0:
            active = onset_tick is not None and k >= onset_tick
            row = {
                "episode": ep,
                "t_s": t_s,
                "ood": int(ood),
                **window_features(buf),
                "label_fault": fault if active else "normal",
                "since_onset_s": round(t_s - settings["onset_s"], 1) if active else -1.0,
                "true_h_cool": round(mill.h_true, 4),
                "true_process_temp_k": round(true_temp, 3),
            }
            rows.append(row)

    # Time-to-limit label: minutes until the first tick at/over the limit (temp or zone-D vibration)
    over_times = [x["t"] for x in ticks if x["over"]]
    for row in rows:
        nxt = next((t for t in over_times if t >= row["t_s"]), None)
        mins = HORIZON_MIN if nxt is None else min(HORIZON_MIN, (nxt - row["t_s"]) / 60.0)
        row["minutes_to_limit"] = round(mins, 3)
        row["limit_within_10min"] = int(mins < 10.0)

    meta = {
        "episode": ep,
        "ood": int(ood),
        "fault": fault,
        "auto_derate": int(auto_derate),
        **{k: round(v, 4) for k, v in settings.items()},
        "minutes": minutes,
        "reached_limit": int(first_overtemp_s is not None),
        "limit_at_s": first_overtemp_s,
        "minutes_onset_to_limit": (
            round((first_overtemp_s - settings["onset_s"]) / 60.0, 2)
            if first_overtemp_s is not None and fault != "normal" and first_overtemp_s >= settings["onset_s"]
            else None
        ),
        "parts_done": mill.parts_done,
        "final_derate_pct": mill.derate_pct,
    }
    return rows, meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episodes", type=int, default=400)
    ap.add_argument("--ood-episodes", type=int, default=40, help="extra test-only episodes on heavier machines")
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out", type=Path, default=ROOT / "ml" / "data")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    all_rows: list[dict] = []
    metas: list[dict] = []
    total = args.episodes + args.ood_episodes
    for i in range(total):
        ood = i >= args.episodes
        rows, meta = run_episode(i, args.minutes, args.seed, ood=ood)
        all_rows.extend(rows)
        metas.append(meta)
        if (i + 1) % 20 == 0 or i + 1 == total:
            print(f"  episode {i + 1}/{total}  rows={len(all_rows)}  {time.time() - t0:.0f}s", flush=True)

    win_path = args.out / f"{DATASET_VERSION}_windows.csv"
    with open(win_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)
    ep_path = args.out / f"{DATASET_VERSION}_episodes.csv"
    with open(ep_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(metas[0].keys()))
        w.writeheader()
        w.writerows(metas)
    meta = {
        "dataset": DATASET_VERSION,
        "description": "Labelled windows from randomised runs of the UDYAM-TWIN virtual CNC line. "
                       "Synthetic (simulation) data: labels are injected ground-truth faults.",
        "seed": args.seed,
        "episodes": args.episodes,
        "ood_episodes": args.ood_episodes,
        "minutes_per_episode": args.minutes,
        "window_s": WINDOW_S,
        "stride_s": STRIDE_S,
        "horizon_min": HORIZON_MIN,
        "fault_weights": FAULT_WEIGHTS,
        "ranges": RANGES,
        "ood_load_range": OOD_LOAD,
        "features": FEATURES,
        "rows": len(all_rows),
        "generated_in_s": round(time.time() - t0, 1),
    }
    (args.out / f"{DATASET_VERSION}_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"Wrote {len(all_rows)} rows -> {win_path}\n      {len(metas)} episodes -> {ep_path}")


if __name__ == "__main__":
    main()
