"""What-if comparisons: run 2–4 variants of the virtual line side by side and compare their outcomes.

Each scenario runs on its own, freshly built FactoryEngine with a simulated clock, so the live simulation (and
real telemetry) is never touched. All scenarios use the same seed, so they start from the same state and draw
the same random numbers until their paths diverge; differences come from the settings, not from luck at the start.

A scenario is: Testing-mode parameters per machine (sim/controls.py, same engineering limits), optionally one
fault injected at a given minute, and whether the twin's SIM auto-derate (self-healing) is on.

Outcomes come from the same models the dashboard uses: OEE and energy from sim/analytics.py, alerts from the
twin's events, line output from sim/line.py, money from the economics settings. The AI4I failure-risk model is
not evaluated in what-if runs (it is most of the tick time and not needed for these outcomes).

Results are SIMULATED WHAT-IF outcomes for the virtual line: comparisons between settings, not predictions
for real machines.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from config import get_config
from sim import controls
from sim.analytics import Analytics
from sim.factory import FactoryEngine, TICK_S
from sim.faults import FaultType
from sim.line import LINE_FLOW

RESULT_LABEL = "SIMULATED WHAT-IF (virtual line; not a prediction for real machines)"
SIM_START = datetime(2026, 10, 26, 8, 0, 0, tzinfo=timezone.utc)
FAULTS = [f.value for f in FaultType]
MAX_SCENARIOS, MIN_SCENARIOS = 4, 2
MAX_MINUTES = 60.0
TREND_EVERY_S = 10.0
ALERT_KINDS = {"ALERT", "ANOMALY", "COMMON_CAUSE"}
KEEP_RESULTS = 10


class WhatIfError(ValueError):
    pass


class _SkipRiskModel:
    def predict(self, **_kw):
        return {"status": "SKIPPED", "failure_risk_pct": None, "detail": "not evaluated in what-if runs"}


_SKIP = _SkipRiskModel()


@dataclass
class Scenario:
    name: str
    params: dict[str, dict[str, float]] = field(default_factory=dict)
    fault: dict[str, Any] | None = None       # {"machine_id", "fault", "at_min"}
    auto_derate: bool = True


def validate(scenarios: list[Scenario], minutes: float, focus: str) -> None:
    if not (MIN_SCENARIOS <= len(scenarios) <= MAX_SCENARIOS):
        raise WhatIfError(f"compare {MIN_SCENARIOS}–{MAX_SCENARIOS} scenarios")
    if not (1.0 <= minutes <= MAX_MINUTES):
        raise WhatIfError(f"run time must be 1–{MAX_MINUTES:g} minutes")
    probe = FactoryEngine(machines=4)
    if focus not in probe.plants:
        raise WhatIfError(f"unknown machine {focus}")
    names = [s.name.strip() for s in scenarios]
    if any(not n for n in names) or len(set(names)) != len(names):
        raise WhatIfError("every scenario needs a distinct name")
    for sc in scenarios:
        for mid, params in sc.params.items():
            plant = probe.plants.get(mid)
            if plant is None:
                raise WhatIfError(f"{sc.name}: unknown machine {mid}")
            try:
                controls.validate(plant.template, plant.nameplate, controls.defaults_for(plant.template), params)
            except controls.ParamError as err:
                raise WhatIfError(f"{sc.name}: {mid}: {err}")
        if sc.fault:
            f = sc.fault
            if f.get("machine_id") not in probe.plants or f.get("fault") not in FAULTS:
                raise WhatIfError(f"{sc.name}: fault needs a known machine and one of {', '.join(FAULTS)}")
            at = f.get("at_min", 0)
            if not isinstance(at, (int, float)) or not (0 <= at < minutes):
                raise WhatIfError(f"{sc.name}: the fault must start within the run (0–{minutes:g} min)")


def run_scenario(sc: Scenario, minutes: float, seed: int, focus: str, economics: dict[str, float]) -> dict[str, Any]:
    eng = FactoryEngine(seed=seed, start=SIM_START, machines=4)
    for tw in eng.twins.values():
        tw.risk_model = _SKIP
    eng.auto_derate = sc.auto_derate
    for mid, params in sc.params.items():
        eng.set_params(mid, params)
    eng.flush_events()
    an = Analytics()
    plant = eng.plants[focus]
    limits = plant.limits
    temp_limit_k = limits.get("process_temp_limit_k")
    n_ticks = int(minutes * 60 / TICK_S)
    fault_tick = int(sc.fault["at_min"] * 60 / TICK_S) if sc.fault else None
    trend_every = int(TREND_EVERY_S / TICK_S)

    temps, vibs, trend = [], [], []
    over_temp = zone_cd = derated = tripped = 0
    alerts: dict[str, dict[str, Any]] = {}
    tool_changes = 0
    for k in range(n_ticks):
        if k == fault_tick:
            eng.plants[sc.fault["machine_id"]].faults.inject(sc.fault["fault"], eng.t)
        msgs = eng.step()
        an.observe(msgs)
        snap = next(m for m in msgs if m.get("type") == "snapshot" and m["machine_id"] == focus)
        obs = next(m for m in msgs if m.get("type") == "observed" and m["machine_id"] == focus)
        temp_k = plant.process_temp_k if plant.template in ("cnc_mill", "cnc_lathe") else None
        if temp_k is not None:
            temps.append(temp_k)
            over_temp += bool(temp_limit_k and temp_k >= temp_limit_k)
        vib = obs.get("vibration_rms_mm_s")
        if isinstance(vib, (int, float)):
            vibs.append(vib)
        zone_cd += (snap.get("severity") or {}).get("zone") in ("C", "D")
        derated += (obs.get("derate_pct") or 0) > 0
        tripped += obs.get("plc_state") == "TRIP"
        for ev in (m for m in msgs if m.get("type") == "event"):
            if ev.get("kind") in ALERT_KINDS and ev.get("severity") in ("WARN", "CRITICAL"):
                key = f"{ev.get('machine_id')}|{ev['title'].split(':')[0]}"
                a = alerts.setdefault(key, {"machine_id": ev.get("machine_id"), "title": ev["title"], "severity": ev["severity"],
                                            "first_at_min": round((k + 1) * TICK_S / 60, 2), "count": 0})
                a["count"] += 1
            if ev.get("kind") == "WORK_ORDER" and "Tool changed" in ev.get("title", "") and ev.get("machine_id") == focus:
                tool_changes += 1
        if (k + 1) % trend_every == 0:
            line = next((m for m in msgs if m.get("type") == "line"), {})
            trend.append({"t_min": round((k + 1) * TICK_S / 60, 2),
                          "temp_c": round(temp_k - 273.15, 2) if temp_k is not None else None,
                          "vibration_mm_s": vib, "rpm": obs.get("rpm"), "power_kw": obs.get("power_kw"),
                          "derate_pct": obs.get("derate_pct"), "throughput_parts_per_hr": line.get("throughput_parts_per_hr")})

    summ = an.summary("session")
    kwh = summ["site_energy_kwh"]
    tariff, co2 = economics.get("electricity_tariff_per_kwh", 0.0), economics.get("cea_co2_factor", 0.0)
    per_part = economics.get("inr_per_part", 0.0)
    last = LINE_FLOW[-1][0]
    parts = eng.plants[last].parts_done if last in eng.plants else 0
    oee = (summ["machines"].get(focus) or {}).get("oee") or {}
    to_min = TICK_S / 60.0
    kpis = {
        "line_parts": parts,
        "line_value_inr": round(parts * per_part, 0),
        "site_energy_kwh": round(kwh, 3),
        "energy_cost_inr": round(kwh * tariff, 2),
        "co2_kg": round(kwh * co2, 3),
        "energy_kwh_per_part": round(kwh / parts, 4) if parts else None,
        "focus_oee": oee.get("oee"),
        "focus_availability": oee.get("availability"),
        "focus_performance": oee.get("performance"),
        "focus_quality": oee.get("quality"),
        "focus_peak_temp_c": round(max(temps) - 273.15, 2) if temps else None,
        "focus_mean_temp_c": round(sum(temps) / len(temps) - 273.15, 2) if temps else None,
        "focus_min_over_temp_limit": round(over_temp * to_min, 2),
        "focus_peak_vibration_mm_s": round(max(vibs), 2) if vibs else None,
        "focus_min_in_zone_c_or_d": round(zone_cd * to_min, 2),
        "focus_min_derated": round(derated * to_min, 2),
        "focus_min_tripped": round(tripped * to_min, 2),
        "focus_tool_changes": tool_changes,
        "alerts_raised": len(alerts),
    }
    return {"name": sc.name, "settings": {"params": sc.params, "fault": sc.fault, "auto_derate": sc.auto_derate},
            "kpis": kpis, "alerts": sorted(alerts.values(), key=lambda a: a["first_at_min"]), "trend": trend}


# what "better" means for each KPI (for the comparison colours); None = no direction
BETTER = {"line_parts": "higher", "line_value_inr": "higher", "site_energy_kwh": "lower", "energy_cost_inr": "lower",
          "co2_kg": "lower", "energy_kwh_per_part": "lower", "focus_oee": "higher", "focus_availability": "higher",
          "focus_performance": "higher", "focus_quality": "higher", "focus_peak_temp_c": "lower",
          "focus_mean_temp_c": "lower", "focus_min_over_temp_limit": "lower", "focus_peak_vibration_mm_s": "lower",
          "focus_min_in_zone_c_or_d": "lower", "focus_min_derated": None, "focus_min_tripped": "lower",
          "focus_tool_changes": None, "alerts_raised": "lower"}


def compare(scenarios: list[Scenario], minutes: float, seed: int, focus: str, economics: dict[str, float],
            progress: Callable[[int, int], None] | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    validate(scenarios, minutes, focus)
    t0 = time.time()
    results = []
    for i, sc in enumerate(scenarios):
        if cancelled and cancelled():
            raise WhatIfError("cancelled")
        results.append(run_scenario(sc, minutes, seed, focus, economics))
        if progress:
            progress(i + 1, len(scenarios))
    base = results[0]["kpis"]
    for r in results[1:]:
        r["delta_vs_baseline"] = {k: (round(v - base[k], 4) if isinstance(v, (int, float)) and isinstance(base[k], (int, float)) else None)
                                  for k, v in r["kpis"].items()}
    return {
        "label": RESULT_LABEL, "minutes": minutes, "seed": seed, "focus_machine": focus,
        "baseline": results[0]["name"], "scenarios": results, "better": BETTER,
        "economics": {k: economics.get(k) for k in ("inr_per_part", "electricity_tariff_per_kwh", "cea_co2_factor")},
        "temperature_limit_c": round(get_config().thresholds(focus).get("process_temp_limit_k", 0) - 273.15, 2)
        if get_config().thresholds(focus).get("process_temp_limit_k") else None,
        "method": ("Each scenario: a fresh virtual line, same seed, simulated clock; outcomes from the dashboard's own "
                   "models. The AI4I failure-risk model is not evaluated in what-if runs."),
        "computed_in_s": round(time.time() - t0, 1),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


class WhatIfJobs:
    """One comparison at a time in a background thread; the last few results are kept in memory."""

    def __init__(self):
        self.lock = threading.Lock()
        self.job: dict[str, Any] | None = None
        self.results: dict[str, dict[str, Any]] = {}
        self._cancel = False

    def status(self) -> dict[str, Any]:
        return dict(self.job) if self.job else {"state": "idle"}

    def start(self, scenarios: list[Scenario], minutes: float, seed: int, focus: str, economics: dict[str, float]) -> dict[str, Any]:
        validate(scenarios, minutes, focus)
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise WhatIfError("a comparison is already running")
            rid = f"whatif-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
            self._cancel = False
            self.job = {"id": rid, "state": "running", "done": 0, "total": len(scenarios), "error": None}
        threading.Thread(target=self._run, args=(rid, scenarios, minutes, seed, focus, economics), daemon=True,
                         name="what-if").start()
        return self.status()

    def cancel(self) -> None:
        self._cancel = True

    def _run(self, rid, scenarios, minutes, seed, focus, economics) -> None:
        try:
            res = compare(scenarios, minutes, seed, focus, economics,
                          progress=lambda d, t: self.job.update(done=d), cancelled=lambda: self._cancel)
            res["id"] = rid
            self.results[rid] = res
            for old in sorted(self.results)[:-KEEP_RESULTS]:
                self.results.pop(old, None)
            self.job.update(state="done")
        except Exception as err:  # noqa: BLE001 - reported, never shown as a result
            self.job.update(state="cancelled" if str(err) == "cancelled" else "failed", error=str(err))
