"""Factory Run Designer: whole-factory test runs from a simple event list (Testing mode only).

A run is: a length, a seed, whether the twin's self-healing (SIM auto-derate) is on, and a list of events
"at minute X, on machine Y, do Z". Z is one of:
  set_params      Testing-mode machine parameters (sim/controls.py limits)
  inject_fault    a fault the machine can have, with a severity (0.5–2.0)
  repair          the simulated repair of that machine (clears faults, releases derate)
  derate          a supervised derate of N % (0–50)
  release_derate  back to 0 % derate
  self_healing    turn the twin's SIM auto-derate on or off (whole factory)

Fast runs execute on a separate, freshly built FactoryEngine (the live dashboard is untouched) and return per-machine
results, the alerts raised, and ground-truth labels: which fault was active on which machine, when, at what severity,
and how long the twin took to raise an alert after each fault started. Batch runs repeat a run with fault times,
severities and parameters varied within chosen ranges. The same event list can also be played on the live
simulation in real time (api/app.py, FactoryPlayback), so a run can be watched in the dashboard and 3D view.

Everything here is simulation: SYNTHETIC DIGITAL TWIN DATA, not real machines. The AI4I failure-risk model is not
evaluated in fast runs (it is most of the tick time and not needed for these results).
"""

from __future__ import annotations

import copy
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sim import controls
from sim.analytics import Analytics
from sim.factory import FactoryEngine, TICK_S
from sim.line import LINE_FLOW

DATA_LABEL = "SYNTHETIC DIGITAL TWIN DATA"
SIM_START = datetime(2026, 10, 26, 8, 0, 0, tzinfo=timezone.utc)
MAX_MINUTES, MAX_EVENTS, MAX_BATCH = 120.0, 60, 100
ACTIONS = ("set_params", "inject_fault", "repair", "derate", "release_derate", "self_healing")
CNC_FAULTS = ("coolant_degradation", "bearing_defect", "overload", "tool_wear_accel", "sensor_stuck", "sensor_dropout",
              "order_surge")
FAULTS_BY_TEMPLATE = {"cnc_mill": CNC_FAULTS, "cnc_lathe": CNC_FAULTS, "bar_saw": ("blade_wear",),
                      "air_compressor": ("pressure_drop",)}
SEVERITY_RANGE = (0.5, 2.0)
ALERT_KINDS = {"ALERT", "ANOMALY", "COMMON_CAUSE"}
TREND_EVERY_S = 10.0
KEEP_RESULTS = 10


class RunError(ValueError):
    pass


class _SkipRiskModel:
    def predict(self, **_kw):
        return {"status": "SKIPPED", "failure_risk_pct": None, "detail": "not evaluated in factory test runs"}


_SKIP = _SkipRiskModel()


@dataclass
class RunSpec:
    name: str = "Factory run"
    minutes: float = 30.0
    seed: int = 7
    self_healing: bool = True
    events: list[dict[str, Any]] = field(default_factory=list)


def machines_of(custom: list[dict] | None = None) -> dict[str, dict[str, Any]]:
    """machine_id -> {template, name, nameplate} for a factory built like the live one (standard + added machines)."""
    eng = _engine(0, custom)
    return {mid: {"template": p.template, "name": p.name, "nameplate": p.nameplate} for mid, p in eng.plants.items()}


def _engine(seed: int, custom: list[dict] | None) -> FactoryEngine:
    eng = FactoryEngine(seed=seed, start=SIM_START, machines=4)
    for spec in custom or []:
        eng.add_custom_machine(dict(spec), announce=False)
    eng.flush_events()
    return eng


def validate(spec: RunSpec, machines: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Checked, normalised and time-sorted events; RunError names the first problem."""
    if not (1.0 <= float(spec.minutes) <= MAX_MINUTES):
        raise RunError(f"run length must be 1–{MAX_MINUTES:g} minutes")
    if len(spec.events) > MAX_EVENTS:
        raise RunError(f"at most {MAX_EVENTS} events")
    out = []
    for i, raw in enumerate(spec.events, 1):
        ev = dict(raw)
        where = f"event {i}"
        try:
            at = float(ev.get("at_min"))
        except (TypeError, ValueError):
            raise RunError(f"{where}: 'at minute' must be a number")
        if not (0 <= at < spec.minutes):
            raise RunError(f"{where}: must happen within the run (0–{spec.minutes:g} min)")
        act = ev.get("action")
        if act not in ACTIONS:
            raise RunError(f"{where}: action must be one of {', '.join(ACTIONS)}")
        norm: dict[str, Any] = {"at_min": round(at, 3), "action": act}
        if act == "self_healing":
            norm["on"] = bool(ev.get("on", True))
        else:
            mid = ev.get("machine_id")
            if mid not in machines:
                raise RunError(f"{where}: unknown machine {mid!r}")
            norm["machine_id"] = mid
            tpl = machines[mid]["template"]
            if act == "set_params":
                params = ev.get("params") or {}
                if not params:
                    raise RunError(f"{where}: choose at least one parameter")
                try:
                    controls.validate(tpl, machines[mid]["nameplate"], controls.defaults_for(tpl), params)
                except controls.ParamError as err:
                    raise RunError(f"{where}: {mid}: {err}")
                norm["params"] = {k: float(v) for k, v in params.items()}
            elif act == "inject_fault":
                fault = ev.get("fault")
                if fault not in FAULTS_BY_TEMPLATE.get(tpl, ()):
                    raise RunError(f"{where}: {mid} ({tpl.replace('_', ' ')}) can have: "
                                   f"{', '.join(FAULTS_BY_TEMPLATE.get(tpl, ())) or 'no faults'}")
                sev = float(ev.get("severity", 1.0))
                if not (SEVERITY_RANGE[0] <= sev <= SEVERITY_RANGE[1]):
                    raise RunError(f"{where}: severity must be {SEVERITY_RANGE[0]:g}–{SEVERITY_RANGE[1]:g}")
                norm.update(fault=fault, severity=round(sev, 3))
            elif act == "derate":
                pct = float(ev.get("pct", 20))
                if not (0 < pct <= 50):
                    raise RunError(f"{where}: derate must be 1–50 %")
                norm["pct"] = pct
        out.append(norm)
    return sorted(out, key=lambda e: e["at_min"])


def describe(ev: dict[str, Any]) -> str:
    a = ev["action"]
    who = ev.get("machine_id", "factory")
    if a == "set_params":
        return f"{who}: " + ", ".join(f"{k.replace('_', ' ')} {v:g}" for k, v in ev["params"].items())
    if a == "inject_fault":
        return f"{who}: fault {ev['fault'].replace('_', ' ')} (severity {ev['severity']:g})"
    if a == "repair":
        return f"{who}: simulated repair"
    if a == "derate":
        return f"{who}: derate {ev['pct']:g} %"
    if a == "release_derate":
        return f"{who}: release derate"
    return f"self-healing {'on' if ev['on'] else 'off'}"


def apply_event(eng: FactoryEngine, ev: dict[str, Any]) -> list[dict]:
    """Apply one event to an engine (fast run or the live simulation); returns the engine's messages."""
    a, mid = ev["action"], ev.get("machine_id")
    if a == "set_params":
        return eng.set_params(mid, ev["params"])
    if a == "inject_fault":
        eng.plants[mid].fault_severity = ev["severity"]
        return eng.command({"cmd": "inject_fault", "machine_id": mid, "fault": ev["fault"]})
    if a == "repair":
        out = eng.command({"cmd": "resolve", "machine_id": mid})
        eng.plants[mid].fault_severity = 1.0
        return out
    if a == "derate":
        return eng.command({"cmd": "set_derate", "machine_id": mid, "pct": ev["pct"]})
    if a == "release_derate":
        return eng.command({"cmd": "set_derate", "machine_id": mid, "pct": 0})
    eng.auto_derate = bool(ev["on"])
    eng._emit_event(None, "SIM_RUN", "INFO", f"Twin self-healing {'on' if ev['on'] else 'off'} (factory run)")
    return eng.flush_events()


def run(spec: RunSpec, economics: dict[str, float], custom: list[dict] | None = None,
        cancelled: Callable[[], bool] | None = None,
        record: Callable[[int, list[dict], FactoryEngine, dict], None] | None = None) -> dict[str, Any]:
    """One fast run on its own engine. Deterministic for (spec, custom machines).

    record(tick, msgs, engine, open_labels) is called after every tick (the dataset collector, ml/factory_dataset.py);
    open_labels maps (machine_id, fault) to the injected fault's label while it is active."""
    machines = machines_of(custom)
    events = validate(spec, machines)
    eng = _engine(spec.seed, custom)
    for tw in eng.twins.values():
        tw.risk_model = _SKIP
    eng.auto_derate = spec.self_healing
    an = Analytics()
    n_ticks = int(spec.minutes * 60 / TICK_S)
    trend_every = int(TREND_EVERY_S / TICK_S)
    by_tick: dict[int, list[dict]] = {}
    for ev in events:
        by_tick.setdefault(int(round(ev["at_min"] * 60 / TICK_S)), []).append(ev)
    trends: dict[str, list[dict]] = {m: [] for m in machines}
    alerts: list[dict] = []
    seen_alert: set[str] = set()
    applied: list[dict] = []
    active: dict[tuple[str, str], dict] = {}      # (machine, fault) -> open label
    labels: list[dict] = []
    for k in range(n_ticks):
        if cancelled and cancelled():
            raise RunError("cancelled")
        t_min = round(k * TICK_S / 60, 3)
        for ev in by_tick.get(k, []):
            apply_event(eng, ev)
            applied.append({**ev, "text": describe(ev)})
            mid = ev.get("machine_id")
            if ev["action"] == "inject_fault":
                key = (mid, ev["fault"])
                if key not in active:
                    active[key] = {"machine_id": mid, "fault": ev["fault"], "severity": ev["severity"],
                                   "start_min": t_min, "end_min": None, "first_alert_min": None, "first_alert": None}
            elif ev["action"] == "repair":
                for key in [x for x in active if x[0] == mid]:
                    lab = active.pop(key)
                    lab["end_min"] = t_min
                    labels.append(lab)
        msgs = eng.step()
        an.observe(msgs)
        if record:
            record(k, msgs, eng, active)
        now_min = round((k + 1) * TICK_S / 60, 3)
        for ev in (m for m in msgs if m.get("type") == "event"):
            if ev.get("kind") in ALERT_KINDS and ev.get("severity") in ("WARN", "CRITICAL"):
                key = f"{ev.get('machine_id')}|{ev['title'].split(':')[0]}"
                if key not in seen_alert:
                    seen_alert.add(key)
                    alerts.append({"at_min": now_min, "machine_id": ev.get("machine_id"), "severity": ev["severity"],
                                   "kind": ev["kind"], "title": ev["title"]})
                for lab in active.values():  # first alert on the faulty machine (or any machine for a common cause)
                    if lab["first_alert_min"] is None and (ev.get("machine_id") == lab["machine_id"]
                                                           or ev.get("kind") == "COMMON_CAUSE"):
                        # the first alert after onset; named, so a human can judge whether it really is about the fault
                        lab["first_alert_min"], lab["first_alert"] = now_min, ev["title"]
        if (k + 1) % trend_every == 0:
            obs = {m["machine_id"]: m for m in msgs if m.get("type") == "observed"}
            snaps = {m["machine_id"]: m for m in msgs if m.get("type") == "snapshot"}
            for mid in machines:
                o, s = obs.get(mid, {}), snaps.get(mid, {})
                plant = eng.plants[mid]
                trends[mid].append({
                    "t_min": now_min, "state": o.get("plc_state"), "rpm": o.get("rpm"), "power_kw": o.get("power_kw"),
                    "vibration_mm_s": o.get("vibration_rms_mm_s"),
                    "temp_c": round(plant.process_temp_k - 273.15, 2) if plant.template in ("cnc_mill", "cnc_lathe") else None,
                    "pressure_bar": o.get("pressure_bar"), "derate_pct": o.get("derate_pct"),
                    "zone": (s.get("severity") or {}).get("zone"),
                    "faults": sorted(plant.faults.active_faults),
                })
    for lab in active.values():
        lab["end_min"] = None  # still active at the end of the run
        labels.append(lab)
    for lab in labels:
        lab["detected"] = lab["first_alert_min"] is not None
        lab["detection_delay_min"] = round(lab["first_alert_min"] - lab["start_min"], 2) if lab["detected"] else None
    labels.sort(key=lambda x: x["start_min"])
    summ = an.summary("session")
    tariff, per_part = economics.get("electricity_tariff_per_kwh", 0.0), economics.get("inr_per_part", 0.0)
    last = LINE_FLOW[-1][0]
    parts = eng.plants[last].parts_done if last in eng.plants else 0
    per_machine = {}
    for mid, info in machines.items():
        m = summ["machines"].get(mid) or {}
        oee = m.get("oee") or {}
        tr = trends[mid]
        temps = [p["temp_c"] for p in tr if p["temp_c"] is not None]
        vibs = [p["vibration_mm_s"] for p in tr if isinstance(p["vibration_mm_s"], (int, float))]
        per_machine[mid] = {"name": info["name"], "template": info["template"], "energy_kwh": round(m.get("energy_kwh") or 0, 3),
                            "oee": oee.get("oee"), "parts": oee.get("parts"),
                            "peak_temp_c": max(temps) if temps else None, "peak_vibration_mm_s": max(vibs) if vibs else None,
                            "alerts": sum(1 for a in alerts if a["machine_id"] == mid)}
    kwh = summ["site_energy_kwh"]
    return {
        "label": DATA_LABEL, "name": spec.name, "minutes": spec.minutes, "seed": spec.seed,
        "self_healing": spec.self_healing, "events": applied, "machines": per_machine, "trends": trends,
        "alerts": alerts, "labels": labels,
        "kpis": {"line_parts": parts, "line_value_inr": round(parts * per_part, 0), "site_energy_kwh": round(kwh, 3),
                 "energy_cost_inr": round(kwh * tariff, 2), "alerts": len(alerts),
                 "faults_injected": len(labels), "faults_detected": sum(l["detected"] for l in labels)},
        "note": "Simulated factory run (SYNTHETIC DIGITAL TWIN DATA). Labels are the faults the run injected; "
                "detection delay is the time until the twin raised an alert. The AI4I failure-risk model is not "
                "evaluated in fast runs.",
    }


@dataclass
class Variation:
    """How a batch varies each repeat (all optional)."""
    fault_time_jitter_min: float = 0.0       # each event time moves by up to ± this
    severity_range: list[float] | None = None  # each fault's severity drawn from [lo, hi]
    param_jitter_pct: float = 0.0             # each set_params value moves by up to ± this % (kept within limits)


def vary(spec: RunSpec, v: Variation, i: int, machines: dict[str, dict[str, Any]]) -> RunSpec:
    """Repeat i of a batch: a reproducible variation of the run (seed + i)."""
    rng = random.Random(f"{spec.seed}:{i}")
    s = copy.deepcopy(spec)
    s.seed, s.name = spec.seed + i, f"{spec.name} #{i + 1}"
    for ev in s.events:
        if v.fault_time_jitter_min:
            ev["at_min"] = round(min(max(0.0, float(ev["at_min"]) + rng.uniform(-v.fault_time_jitter_min, v.fault_time_jitter_min)),
                                     s.minutes - 0.5), 2)
        if ev.get("action") == "inject_fault" and v.severity_range:
            ev["severity"] = round(rng.uniform(*v.severity_range), 2)
        if ev.get("action") == "set_params" and v.param_jitter_pct:
            tpl = machines[ev["machine_id"]]["template"]
            specs = {p["key"]: p for p in controls.specs_for(tpl, machines[ev["machine_id"]]["nameplate"])}
            for key, val in ev["params"].items():
                lo, hi = specs[key]["min"], specs[key]["max"]
                ev["params"][key] = round(min(hi, max(lo, val * (1 + rng.uniform(-1, 1) * v.param_jitter_pct / 100))), 2)
    return s


def batch(spec: RunSpec, v: Variation, n: int, economics: dict[str, float], custom: list[dict] | None = None,
          progress: Callable[[int, int], None] | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    if not (2 <= n <= MAX_BATCH):
        raise RunError(f"a batch is 2–{MAX_BATCH} runs")
    if v.severity_range and not (SEVERITY_RANGE[0] <= v.severity_range[0] <= v.severity_range[1] <= SEVERITY_RANGE[1]):
        raise RunError(f"severity range must be within {SEVERITY_RANGE[0]:g}–{SEVERITY_RANGE[1]:g}")
    if not (0 <= v.fault_time_jitter_min <= 30) or not (0 <= v.param_jitter_pct <= 50):
        raise RunError("time jitter must be 0–30 min and parameter jitter 0–50 %")
    machines = machines_of(custom)
    validate(spec, machines)
    runs = []
    for i in range(n):
        r = run(vary(spec, v, i, machines), economics, custom, cancelled)
        runs.append({k: r[k] for k in ("name", "seed", "events", "machines", "alerts", "labels", "kpis")})
        if progress:
            progress(i + 1, n)
    by_fault: dict[str, dict[str, Any]] = {}
    for r in runs:
        for lab in r["labels"]:
            f = by_fault.setdefault(f"{lab['machine_id']} · {lab['fault']}", {"injected": 0, "detected": 0, "delays": []})
            f["injected"] += 1
            f["detected"] += lab["detected"]
            if lab["detected"]:
                f["delays"].append(lab["detection_delay_min"])
    detection = {k: {"injected": f["injected"], "detected": f["detected"],
                     "detection_rate": round(f["detected"] / f["injected"], 3),
                     "mean_delay_min": round(sum(f["delays"]) / len(f["delays"]), 2) if f["delays"] else None,
                     "max_delay_min": max(f["delays"]) if f["delays"] else None} for k, f in sorted(by_fault.items())}
    mean = lambda key: round(sum(r["kpis"][key] for r in runs) / len(runs), 2)
    return {"label": DATA_LABEL, "name": spec.name, "runs": n, "minutes": spec.minutes, "base_seed": spec.seed,
            "variation": v.__dict__, "detection": detection,
            "kpis_mean": {k: mean(k) for k in ("line_parts", "site_energy_kwh", "energy_cost_inr", "alerts")},
            "per_run": [{"name": r["name"], "seed": r["seed"], **r["kpis"]} for r in runs], "detail": runs,
            "note": "Each repeat is a separate simulated run (seed + i) with the chosen variation. Detection is measured "
                    "against the injected ground truth."}


# -----------------------------------------------------------------------------------------------------------------
PRESETS: dict[str, dict[str, Any]] = {
    "normal_shift": {"name": "Normal shift", "minutes": 30, "events": []},
    "compressor_mid_shift": {"name": "Compressor failure mid-shift", "minutes": 30, "events": [
        {"at_min": 10, "action": "inject_fault", "machine_id": "COMP-01", "fault": "pressure_drop", "severity": 1.0},
        {"at_min": 22, "action": "repair", "machine_id": "COMP-01"}]},
    "hot_day_heavy_jobs": {"name": "Hot day + heavy jobs", "minutes": 30, "events": [
        {"at_min": 0, "action": "set_params", "machine_id": "VMC-01", "params": {"ambient_temp_c": 34, "load_pct": 115}},
        {"at_min": 0, "action": "set_params", "machine_id": "LATHE-01", "params": {"ambient_temp_c": 34, "load_pct": 110}}]},
    "lathe_bearing_wear": {"name": "Bearing wear on the lathe", "minutes": 30, "events": [
        {"at_min": 5, "action": "inject_fault", "machine_id": "LATHE-01", "fault": "bearing_defect", "severity": 1.2},
        {"at_min": 25, "action": "repair", "machine_id": "LATHE-01"}]},
    "coolant_no_healing": {"name": "Coolant loss, self-healing off", "minutes": 30, "self_healing": False, "events": [
        {"at_min": 4, "action": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation", "severity": 1.0}]},
}


class RunJobs:
    """One fast run or batch at a time in a background thread; the last results are kept in memory."""

    def __init__(self):
        self.lock = threading.Lock()
        self.job: dict[str, Any] | None = None
        self.results: dict[str, dict[str, Any]] = {}
        self._cancel = False

    def status(self) -> dict[str, Any]:
        return dict(self.job) if self.job else {"state": "idle"}

    def start(self, kind: str, fn: Callable[..., dict], total: int) -> dict[str, Any]:
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise RunError("a factory run is already running")
            rid = f"frun-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
            self._cancel = False
            self.job = {"id": rid, "kind": kind, "state": "running", "done": 0, "total": total, "error": None,
                        "started": time.time()}
        threading.Thread(target=self._run, args=(rid, fn), daemon=True, name="factory-run").start()
        return self.status()

    def cancel(self) -> None:
        self._cancel = True

    def _run(self, rid: str, fn: Callable[..., dict]) -> None:
        try:
            res = fn(progress=lambda d, t: self.job.update(done=d), cancelled=lambda: self._cancel)
            res.update(id=rid, kind=self.job["kind"], computed_in_s=round(time.time() - self.job["started"], 1),
                       created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
            self.results[rid] = res
            for old in sorted(self.results)[:-KEEP_RESULTS]:
                self.results.pop(old, None)
            self.job.update(state="done", done=self.job["total"])
        except Exception as err:  # noqa: BLE001 - reported to the operator, never shown as a result
            self.job.update(state="cancelled" if str(err) == "cancelled" else "failed", error=str(err))
