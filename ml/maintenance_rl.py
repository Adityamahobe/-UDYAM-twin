"""Reinforcement-learning maintenance policy for the CNC mill (VMC). Simulation only.

What it learns: every 20 s of machine time, which of five actions pays off best for the VMC —
continue at full speed, derate 20 %, derate 40 %, inspect (a short stop that shows whether the VMC
really has a fault), or stop for maintenance (a simulated repair).

- Environment: the same VMC and air-compressor physics as the live simulator (sim/plant.py). The
  compressor feeds the VMC's air interlock: below the minimum pressure the VMC is derated 25 %
  whatever the policy does. Scenarios: no fault, the four VMC fault-lab scenarios, or a compressor
  pressure drop, injected at random times and severities.
- State (shared by training and the live loop, see `features_from_snapshot`): temperature margin to
  the limit, temperature trend, vibration zone, spindle load band, tool-wear band, current derate,
  compressed-air pressure band, and the result of a recent inspection (none, fault found, all clear).
- Reward (₹ per 20 s decision): parts made × ₹/part minus penalties for over-temperature, vibration
  zone C/D, overload, a worn tool, a trip (unplanned breakdown), maintenance (₹ + lost output) and
  inspection (₹ + a quarter of the interval's output). Part value and downtime cost come from
  config/machines.yaml `site`; the penalty and inspection rates are illustrative assumptions (REWARD).
- Algorithm: tabular Q-learning (ε-greedy, decaying learning rate), trained by `python -m ml.train_rl`.
- Live feedback loop (`FeedbackAgent`): each decision records the predicted risk and the selected
  action, then the simulated outcome in the virtual factory is evaluated (prediction correct or not,
  reward), and the Q-table is updated online, in SIM mode only.

The policy only RECOMMENDS. Nothing here changes a machine; an operator applies a recommendation
through the approval workflow. The policy is trained and updated in simulation: it is not a
validated maintenance policy for a real plant.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config import get_config

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "models" / "rl_policy.json"
METRICS_PATH = ROOT / "models" / "rl_metrics.json"

ACTIONS = ("continue", "derate_20", "derate_40", "inspect", "maintain")
ACTION_LABEL = {"continue": "Continue at full speed", "derate_20": "Derate 20 %", "derate_40": "Derate 40 %",
                "inspect": "Inspect (short stop)", "maintain": "Stop for maintenance"}
# derate each action sets; inspect leaves the current derate as it is
ACTION_DERATE: dict[str, float | None] = {"continue": 0.0, "derate_20": 20.0, "derate_40": 40.0, "inspect": None,
                                          "maintain": 0.0}
TICK_S = 0.5
DECISION_TICKS = 40                  # one decision every 20 s of machine time
DECISION_S = DECISION_TICKS * TICK_S
VMC_CYCLE_S = 30.0                   # sim/line.py: the VMC makes one part per 30 s at full speed
INSPECTION_VALID_DECISIONS = 3       # an inspection result is part of the state for 60 s
GAMMA = 0.9
LIVE_ALPHA = 0.1                     # learning rate of the online (live) updates

_SITE = get_config().site
_LIMITS = get_config().thresholds("VMC-01", "cnc_mill")
_AIR = get_config().thresholds("COMP-01", "air_compressor")
_ZONES = get_config().vibration_profile("VMC-01", "cnc_mill")["zone_boundaries"]
TEMP_LIMIT_C = _LIMITS["process_temp_limit_k"] - 273.15
OVERLOAD_PCT = _LIMITS["overload_load_pct"]
TOOL_WORN_MIN = _LIMITS["tool_wear_forecast_min"]
TOOL_WARN_MIN = _LIMITS["tool_wear_warn_min"]
AIR_MIN_BAR = _AIR["pressure_min_bar"]
AIR_WARN_BAR = _AIR["pressure_warn_bar"]

# ₹ figures behind the reward. Part value and downtime cost: config/machines.yaml `site`.
# The penalty and inspection rates are illustrative assumptions for the simulation, not measured costs.
REWARD = {
    "inr_per_part": float(_SITE["inr_per_part"]),
    "over_temp_inr_per_s": 40.0,
    "zone_c_inr_per_s": 5.0,
    "zone_d_inr_per_s": 60.0,
    "overload_inr_per_s": 10.0,
    "worn_tool_inr_per_s": 15.0,
    "maintenance_inr": 1500.0,
    "inspection_inr": 400.0,
    "inspection_output_loss": 0.25,
    "trip_downtime_h": 4.0,
    "downtime_cost_inr_per_hr": float(_SITE["downtime_cost_inr_per_hr"]),
}
REWARD_BASIS = {
    "inr_per_part": "config/machines.yaml site.inr_per_part",
    "downtime_cost_inr_per_hr": "config/machines.yaml site.downtime_cost_inr_per_hr",
    "trip": "trip_downtime_h × downtime cost (unplanned breakdown)",
    "maintenance": "maintenance_inr + no parts during the 20 s decision (planned stop)",
    "inspection": "inspection_inr + a quarter of the 20 s decision's parts (short stop)",
    "penalty_rates": "illustrative assumptions for the simulation, not measured plant costs",
}


# ----------------------------------------------------------------------------- state
def zone_of(vib_mm_s: float | None) -> str:
    if vib_mm_s is None:
        return "A"
    if vib_mm_s >= _ZONES["C_D"]:
        return "D"
    if vib_mm_s >= _ZONES["B_C"]:
        return "C"
    return "B" if vib_mm_s >= _ZONES["A_B"] else "A"


def air_band(bar: float | None) -> str:
    if bar is None:
        return "a?"
    return "a2" if bar < AIR_MIN_BAR else "a1" if bar < AIR_WARN_BAR else "a0"


def discretize(f: dict[str, Any]) -> str:
    """Features → state key, e.g. 'm2|up|AB|ok|t0|d0|a0|i-'. Used identically in training and live."""
    temp, trend = f.get("temp_c"), f.get("temp_trend_c") or 0.0
    if temp is None:
        margin = "m?"
    else:
        m = TEMP_LIMIT_C - temp
        margin = "m0" if m < 0 else "m1" if m < 1.0 else "m2" if m < 3.0 else "m3"
    tr = "up" if trend > 0.5 else "dn" if trend < -0.5 else "st"
    zone = f.get("zone") or "A"
    vz = "D" if zone == "D" else "C" if zone == "C" else "AB"
    load = "hi" if (f.get("load_pct") or 0.0) >= OVERLOAD_PCT else "ok"
    wear = f.get("tool_wear_min")
    tw = "t?" if wear is None else "t2" if wear > TOOL_WORN_MIN else "t1" if wear > TOOL_WARN_MIN else "t0"
    der = f.get("derate_pct") or 0.0
    d = "d40" if der >= 30 else "d20" if der >= 10 else "d0"
    insp = {"fault": "iF", "ok": "iO"}.get(f.get("inspection") or "", "i-")
    return f"{margin}|{tr}|{vz}|{load}|{tw}|{d}|{air_band(f.get('air_bar'))}|{insp}"


def features_from_snapshot(snap: dict[str, Any], prev_temp_c: float | None, air_bar: float | None = None,
                           inspection: str | None = None) -> dict[str, Any]:
    temp = snap.get("temp_c")
    return {
        "temp_c": temp,
        "temp_trend_c": (temp - prev_temp_c) if isinstance(temp, (int, float)) and prev_temp_c is not None else 0.0,
        "zone": (snap.get("severity") or {}).get("zone") or zone_of(snap.get("vibration_rms_mm_s")),
        "load_pct": snap.get("load_pct"),
        "tool_wear_min": (snap.get("signals") or {}).get("tool_wear_min"),
        "derate_pct": snap.get("derate_pct") or 0.0,
        "air_bar": air_bar,
        "inspection": inspection,
    }


# ----------------------------------------------------------------------------- reward
@dataclass
class Outcome:
    """What happened during one 20 s decision interval."""

    ticks: int = 0
    parts: float = 0.0
    over_temp_s: float = 0.0
    zone_c_s: float = 0.0
    zone_d_s: float = 0.0
    overload_s: float = 0.0
    worn_tool_s: float = 0.0
    trip: bool = False
    max_temp_c: float | None = None
    max_vib_mm_s: float | None = None
    min_air_bar: float | None = None

    def add_tick(self, *, running: bool, derate_pct: float, temp_c: float | None, vib: float | None,
                 load_pct: float | None, tool_wear_min: float | None, tripped: bool, air_bar: float | None = None) -> None:
        self.ticks += 1
        if tripped:
            self.trip = True
        if running and not tripped:
            self.parts += max(0.0, 1.0 - derate_pct / 100.0) * TICK_S / VMC_CYCLE_S
        if temp_c is not None:
            self.max_temp_c = temp_c if self.max_temp_c is None else max(self.max_temp_c, temp_c)
            if temp_c > TEMP_LIMIT_C:
                self.over_temp_s += TICK_S
        if vib is not None and running:
            self.max_vib_mm_s = vib if self.max_vib_mm_s is None else max(self.max_vib_mm_s, vib)
            z = zone_of(vib)
            self.zone_d_s += TICK_S if z == "D" else 0.0
            self.zone_c_s += TICK_S if z == "C" else 0.0
        if running and (load_pct or 0.0) >= OVERLOAD_PCT:
            self.overload_s += TICK_S
        if running and tool_wear_min is not None and tool_wear_min > TOOL_WORN_MIN:
            self.worn_tool_s += TICK_S
        if air_bar is not None:
            self.min_air_bar = air_bar if self.min_air_bar is None else min(self.min_air_bar, air_bar)

    @property
    def adverse(self) -> bool:
        """An outcome the operator would not want: over-temperature, zone D, overload or a trip."""
        return self.trip or self.over_temp_s > 0 or self.zone_d_s > 0 or self.overload_s > 0

    def public(self) -> dict[str, Any]:
        return {"parts": round(self.parts, 3), "over_temp_s": self.over_temp_s, "zone_c_s": self.zone_c_s,
                "zone_d_s": self.zone_d_s, "overload_s": self.overload_s, "worn_tool_s": self.worn_tool_s,
                "trip": self.trip, "max_temp_c": self.max_temp_c, "max_vib_mm_s": self.max_vib_mm_s,
                "min_air_bar": self.min_air_bar, "adverse": self.adverse}


def reward(o: Outcome, action: str) -> float:
    """₹ for one decision. Maintenance makes no parts that interval; an inspection costs a quarter of them."""
    r = REWARD
    output = {"maintain": 0.0, "inspect": 1.0 - r["inspection_output_loss"]}.get(action, 1.0)
    value = o.parts * output * r["inr_per_part"]
    value -= (o.over_temp_s * r["over_temp_inr_per_s"] + o.zone_c_s * r["zone_c_inr_per_s"]
              + o.zone_d_s * r["zone_d_inr_per_s"] + o.overload_s * r["overload_inr_per_s"]
              + o.worn_tool_s * r["worn_tool_inr_per_s"])
    if action == "maintain":
        value -= r["maintenance_inr"]
    if action == "inspect":
        value -= r["inspection_inr"]
    if o.trip:
        value -= r["trip_downtime_h"] * r["downtime_cost_inr_per_hr"]
    return round(value, 2)


# ----------------------------------------------------------------------------- environment
SCENARIO_FAULTS = {"none": (), "vibration_fault": ("bearing_defect",), "over_temperature": ("coolant_degradation",),
                   "tool_wear": ("tool_wear_accel",), "overload": ("overload",), "air_pressure_drop": ("pressure_drop",)}
COMPRESSOR_FAULTS = {"pressure_drop"}


class VmcEnv:
    """The VMC and the compressor that feeds its air interlock, stepped in 20 s decisions."""

    def __init__(self, seed: int, scenario: str | None = None, decisions: int = 36):
        from sim.plant import PlantMachine

        self.rng = random.Random(seed)
        self.scenario = scenario or self.rng.choice(list(SCENARIO_FAULTS))
        self.decisions, self.k, self.t = decisions, 0, 0.0
        self.inject_at = self.rng.randint(0, 8)
        p = self.plant = PlantMachine(machine_id="VMC-01", template="cnc_mill", name="VMC (training)")
        self.comp = PlantMachine(machine_id="COMP-01", template="air_compressor", name="Compressor (training)")
        p.ambient_offset_k = self.rng.uniform(-1.5, 1.5)
        p.load_scale = self.rng.uniform(0.92, 1.08)
        p.noise_scale = self.rng.uniform(0.8, 1.2)
        p.fault_severity = self.rng.uniform(0.7, 1.3)
        p.tool_wear_min = self.rng.uniform(20.0, 190.0)
        self.rated_kw = float(p.nameplate["rated_kw"])
        self.done = False
        self.inspection: str | None = None
        self.inspection_age = 0
        self._last: dict[str, Any] = {}
        self._prev_temp: float | None = None
        for _ in range(DECISION_TICKS):  # warm-up
            self._tick()
        self._prev_temp = self._last.get("temp_c")

    def _tick(self) -> dict[str, Any]:
        self.t += TICK_S
        comp = self.comp.step_compressor(self.rng, "")
        air = comp["pressure_bar"]
        obs, _ = self.plant.step_cnc(self.t, self.rng, air >= AIR_MIN_BAR, "")
        temp = obs.get("process_temp_k")
        self._last = {
            "temp_c": None if temp is None else temp - 273.15,
            "vib": obs["vibration_rms_mm_s"],
            "load_pct": 100.0 * obs["power_kw"] / self.rated_kw,
            "tool_wear_min": obs["tool_wear_min"],
            "derate_pct": obs["derate_pct"],
            "running": obs["plc_state"] in ("RUN", "DERATE"),
            "tripped": obs["plc_state"] == "TRIP",
            "air_bar": air,
        }
        return self._last

    def features(self) -> dict[str, Any]:
        last = self._last
        temp = last.get("temp_c")
        return {"temp_c": temp, "temp_trend_c": (temp - self._prev_temp) if temp is not None and self._prev_temp is not None else 0.0,
                "zone": zone_of(last.get("vib")) if last.get("running") else "A", "load_pct": last.get("load_pct"),
                "tool_wear_min": last.get("tool_wear_min"), "derate_pct": last.get("derate_pct"),
                "air_bar": last.get("air_bar"), "inspection": self.inspection}

    def state(self) -> str:
        return discretize(self.features())

    def step(self, action: str) -> tuple[str, float, bool, Outcome]:
        if self.k == self.inject_at:
            for fault in SCENARIO_FAULTS[self.scenario]:
                (self.comp if fault in COMPRESSOR_FAULTS else self.plant).faults.inject(fault, self.t)
        self.inspection_age += 1
        if self.inspection_age >= INSPECTION_VALID_DECISIONS:  # in the state for 3 decisions, as in the live loop
            self.inspection = None
        if action == "inspect":  # the inspection sees the VMC's real condition (not the compressor's)
            self.inspection, self.inspection_age = ("fault" if self.plant.faults.active_faults else "ok"), 0
        elif action == "maintain":
            self.plant.resolve("")
            self.inspection = None
        if ACTION_DERATE[action] is not None:
            self.plant.derate_pct = ACTION_DERATE[action]
        self._prev_temp = self._last.get("temp_c")
        o = Outcome()
        for _ in range(DECISION_TICKS):
            x = self._tick()
            o.add_tick(running=x["running"], derate_pct=x["derate_pct"], temp_c=x["temp_c"], vib=x["vib"],
                       load_pct=x["load_pct"], tool_wear_min=x["tool_wear_min"], tripped=x["tripped"], air_bar=x["air_bar"])
        r = reward(o, action)
        self.k += 1
        self.done = o.trip or self.k >= self.decisions  # a trip ends the shift (unplanned breakdown)
        return self.state(), r, self.done, o


# ----------------------------------------------------------------------------- policies
def rule_policy(state: str) -> str:
    """Baseline: the twin's behaviour today as rules — derate on heat or load, maintain on vibration zone D.

    A worn tool is left to the end-of-life tool change (as the simulator does), not a maintenance stop. With
    low air pressure the VMC is already derated by the interlock, so the rules do not derate it further.
    """
    margin, trend, vz, load, _, _, air, _ = state.split("|")
    if vz == "D":
        return "maintain"
    if margin == "m0":
        return "derate_40"
    if air == "a2":
        return "continue"
    if margin == "m1" or (margin == "m2" and trend == "up") or load == "hi" or vz == "C":
        return "derate_20"
    return "continue"


def rule_inspect_policy(state: str) -> str:
    """Baseline with inspection: inspect on early warning signs, maintain if the inspection found a fault."""
    margin, trend, vz, load, _, _, air, insp = state.split("|")
    if insp == "iF":
        return "maintain"
    early = margin == "m1" or (margin == "m2" and trend == "up") or load == "hi" or vz == "C"
    if insp == "i-" and early and air != "a2" and vz != "D":
        return "inspect"
    return rule_policy(state)


@dataclass
class QPolicy:
    q: dict[str, list[float]] = field(default_factory=dict)
    visits: dict[str, list[int]] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def known(self, state: str) -> bool:
        return state in self.q and sum(self.visits.get(state, [0])) > 0

    def values(self, state: str) -> list[float]:
        return self.q.setdefault(state, [0.0] * len(ACTIONS))

    def best(self, state: str) -> str:
        """Greedy action; states never seen in training fall back to the rule baseline."""
        if not self.known(state):
            return rule_policy(state)
        vals, seen = self.q[state], self.visits[state]
        candidates = [i for i in range(len(ACTIONS)) if seen[i] > 0] or list(range(len(ACTIONS)))
        return ACTIONS[max(candidates, key=lambda i: vals[i])]

    def update(self, s: str, a: str, r: float, s2: str, done: bool, alpha: float) -> tuple[float, float]:
        i = ACTIONS.index(a)
        vals = self.values(s)
        self.visits.setdefault(s, [0] * len(ACTIONS))[i] += 1
        target = r if done else r + GAMMA * max(self.values(s2))
        before = vals[i]
        vals[i] = before + alpha * (target - before)
        return before, vals[i]

    def to_json(self) -> dict[str, Any]:
        return {"actions": list(ACTIONS), "q": {s: [round(v, 2) for v in vs] for s, vs in sorted(self.q.items())},
                "visits": dict(sorted(self.visits.items())), "meta": self.meta}

    @classmethod
    def load(cls, path: Path = POLICY_PATH) -> "QPolicy | None":
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if tuple(raw.get("actions", ())) != ACTIONS:
            return None  # a policy trained for another action set: retrain with python -m ml.train_rl
        return cls(q={s: list(v) for s, v in raw["q"].items()}, visits={s: list(v) for s, v in raw["visits"].items()},
                   meta=raw.get("meta", {}))


def train(episodes: int = 25000, seed: int = 7, log: Callable[[str], None] | None = None) -> tuple[QPolicy, list[float]]:
    """Tabular Q-learning over random scenarios. Returns the policy and a per-episode return curve."""
    rng = random.Random(seed)
    pol = QPolicy()
    returns: list[float] = []
    for ep in range(episodes):
        eps = max(0.05, 1.0 - ep / (0.7 * episodes))
        env = VmcEnv(seed=1_000_000 + ep)  # training seeds; evaluation uses a different range
        s, total = env.state(), 0.0
        while not env.done:
            a = rng.choice(ACTIONS) if rng.random() < eps else pol.best(s) if pol.known(s) else rng.choice(ACTIONS)
            s2, r, done, _ = env.step(a)
            n = pol.visits.get(s, [0] * len(ACTIONS))[ACTIONS.index(a)]
            pol.update(s, a, r, s2, done, alpha=max(0.02, 1.0 / (1.0 + n) ** 0.6))
            s, total = s2, total + r
        returns.append(total)
        if log and (ep + 1) % max(1, episodes // 10) == 0:
            log(f"  episode {ep + 1}/{episodes}  ε={eps:.2f}  mean return (last 500) ₹{sum(returns[-500:]) / len(returns[-500:]):,.0f}")
    return pol, returns


def evaluate(policy: Callable[[str], str], episodes_per_scenario: int = 100, seed0: int = 5_000_000) -> dict[str, Any]:
    """Run a policy on held-out seeds for each scenario (no learning) and summarise the outcomes."""
    out: dict[str, Any] = {}
    for sc in SCENARIO_FAULTS:
        tot = {"return_inr": 0.0, "parts": 0.0, "trips": 0, "over_temp_s": 0.0, "zone_d_s": 0.0,
               "maintenance_stops": 0, "inspections": 0}
        for i in range(episodes_per_scenario):
            env = VmcEnv(seed=seed0 + i, scenario=sc)
            s = env.state()
            while not env.done:
                a = policy(s)
                s, r, _, o = env.step(a)
                tot["return_inr"] += r
                tot["parts"] += {"maintain": 0.0, "inspect": 1.0 - REWARD["inspection_output_loss"]}.get(a, 1.0) * o.parts
                tot["trips"] += int(o.trip)
                tot["over_temp_s"] += o.over_temp_s
                tot["zone_d_s"] += o.zone_d_s
                tot["maintenance_stops"] += int(a == "maintain")
                tot["inspections"] += int(a == "inspect")
        n = episodes_per_scenario
        out[sc] = {k: round(v / n, 3) for k, v in tot.items()}
    keys = list(next(iter(out.values())))
    out["all"] = {k: round(sum(out[sc][k] for sc in SCENARIO_FAULTS) / len(SCENARIO_FAULTS), 3) for k in keys}
    return out


# ----------------------------------------------------------------------------- live feedback loop
def predicted_risk(snap: dict[str, Any]) -> dict[str, Any]:
    """The twin's own risk call at decision time (rules, forecast and the AI4I model), before the outcome."""
    risk = snap.get("risk") or {}
    ml = risk.get("ml") or {}
    pct = ml.get("failure_risk_pct")
    warning = (snap.get("forecast") or {}).get("warning")
    adverse = risk.get("rules") in ("MEDIUM", "HIGH") or bool(warning) or (isinstance(pct, (int, float)) and pct >= 50)
    return {"rules": risk.get("rules"), "failure_risk_pct": pct, "forecast_warning": warning, "adverse_expected": adverse}


INSPECTION_WO = re.compile(r"\binspect", re.IGNORECASE)


@dataclass
class _Open:
    """A decision waiting for its outcome."""

    ts: str
    state: str
    features: dict[str, Any]
    prediction: dict[str, Any]
    recommended: str
    q_values: list[float]
    mode: str
    parts_done_start: int | None
    outcome: Outcome = field(default_factory=Outcome)
    derate_ticks: list[float] = field(default_factory=list)
    maintained: bool = False
    inspected: bool = False


class FeedbackAgent:
    """Predict → act → simulated outcome → evaluate → update, on the live VMC stream.

    Fed every broadcast batch. Every 20 s per CNC mill it closes the previous decision (evaluates
    the outcome, updates the Q-table in SIM mode) and opens a new one with a fresh recommendation.
    It never sends a command: the recommendation is advice until an operator approves it.

    Actions it recognises in the stream: a derate (the machine's derate during the interval), maintenance
    (a RESOLUTION event) and an inspection (a work order created for the machine whose title says
    "inspect"). A simulated inspection result is the simulator's own fault state for that machine.
    """

    def __init__(self, policy: QPolicy | None = None, sink: Callable[[dict[str, Any]], None] | None = None,
                 history: int = 200):
        self.policy = policy if policy is not None else QPolicy.load()
        self.trained = self.policy is not None
        if self.policy is None:
            self.policy = QPolicy(meta={"note": "no trained policy: run python -m ml.train_rl"})
        self.sink = sink
        self.open: dict[str, _Open] = {}
        self.ticks: dict[str, int] = {}
        self.prev_temp: dict[str, float | None] = {}
        self.air_bar: float | None = None
        self.inspection: dict[str, tuple[str, int]] = {}  # machine → (fault | ok, decisions left)
        self.true_fault: dict[str, str | None] = {}
        self.recent: deque[dict[str, Any]] = deque(maxlen=history)
        self.stats = {"decisions": 0, "online_updates": 0, "predictions_correct": 0, "followed": 0,
                      "reward_inr": 0.0}

    def observe(self, msgs: list[dict[str, Any]]) -> None:
        if any(m.get("type") == "event" and m.get("kind") == "RESET" for m in msgs):
            self.open.clear()
            self.ticks.clear()
            self.prev_temp.clear()
            self.inspection.clear()
        for m in msgs:
            if m.get("type") == "observed":
                self.true_fault[m["machine_id"]] = m.get("active_fault")
            elif m.get("type") == "snapshot" and m.get("template") == "air_compressor":
                self.air_bar = (m.get("signals") or {}).get("pressure_bar")
        for m in msgs:  # a repair or an inspection can arrive in a batch without snapshots
            if m.get("type") != "event" or m.get("machine_id") not in self.open:
                continue
            mid = m["machine_id"]
            if m.get("kind") == "RESOLUTION":
                self.open[mid].maintained = True
                self.inspection.pop(mid, None)
            elif m.get("kind") == "WORK_ORDER" and " created" in (m.get("title") or "") and INSPECTION_WO.search(m.get("title") or ""):
                self.open[mid].inspected = True
                self.inspection[mid] = ("fault" if self.true_fault.get(mid) else "ok", INSPECTION_VALID_DECISIONS)
        parts = {m["machine_id"]: m.get("parts_done") for m in msgs if m.get("type") == "observed"}
        for s in msgs:
            if s.get("type") != "snapshot" or s.get("template") != "cnc_mill":
                continue
            mid = s["machine_id"]
            dec = self.open.get(mid)
            if dec is not None:
                dec.derate_ticks.append(s.get("derate_pct") or 0.0)
                state = s.get("state")
                dec.outcome.add_tick(running=state in ("RUN", "DERATE"), derate_pct=s.get("derate_pct") or 0.0,
                                     temp_c=s.get("temp_c"), vib=s.get("vibration_rms_mm_s"), load_pct=s.get("load_pct"),
                                     tool_wear_min=(s.get("signals") or {}).get("tool_wear_min"), tripped=state == "TRIP",
                                     air_bar=self.air_bar)
            n = self.ticks[mid] = self.ticks.get(mid, 0) + 1
            if dec is None or n >= DECISION_TICKS:
                self._decide(mid, s, parts.get(mid))

    def _decide(self, mid: str, snap: dict[str, Any], parts_done: int | None) -> None:
        prev = self.open.pop(mid, None)
        if prev is not None and not prev.inspected and mid in self.inspection:  # results expire after 60 s
            result, left = self.inspection[mid]
            if left <= 1:
                self.inspection.pop(mid)
            else:
                self.inspection[mid] = (result, left - 1)
        feats = features_from_snapshot(snap, self.prev_temp.get(mid), self.air_bar,
                                       (self.inspection.get(mid) or (None,))[0])
        state = discretize(feats)
        if prev is not None:
            self._close(mid, prev, state, snap, parts_done)
        rec = self.policy.best(state)
        self.open[mid] = _Open(ts=snap["ts"], state=state, features=feats, prediction=predicted_risk(snap),
                               recommended=rec, q_values=[round(v, 1) for v in self.policy.values(state)],
                               mode=snap.get("mode") or "SIM", parts_done_start=parts_done)
        self.ticks[mid] = 0
        self.prev_temp[mid] = snap.get("temp_c")

    def _close(self, mid: str, d: _Open, next_state: str, snap: dict[str, Any], parts_done: int | None) -> None:
        o = d.outcome
        if d.maintained:
            taken = "maintain"
        elif d.inspected:
            taken = "inspect"
        else:
            mean_derate = sum(d.derate_ticks) / len(d.derate_ticks) if d.derate_ticks else 0.0
            taken = min(("continue", "derate_20", "derate_40"), key=lambda a: abs(ACTION_DERATE[a] - mean_derate))
        r = reward(o, taken)
        learn = d.mode == "SIM" and (snap.get("mode") or "SIM") == "SIM"
        q_before = q_after = None
        if learn:
            q_before, q_after = self.policy.update(d.state, taken, r, next_state, done=False, alpha=LIVE_ALPHA)
        correct = d.prediction["adverse_expected"] == o.adverse
        row = {
            "ts_start": d.ts, "ts_end": snap["ts"], "machine_id": mid, "mode": d.mode, "state": d.state,
            "risk_rules": d.prediction["rules"], "failure_risk_pct": d.prediction["failure_risk_pct"],
            "adverse_expected": d.prediction["adverse_expected"], "recommended_action": d.recommended,
            "action_taken": taken, "followed": taken == d.recommended, **{f"outcome_{k}": v for k, v in o.public().items()},
            "inspection_result": (self.inspection.get(mid) or (None,))[0] if taken == "inspect" else None,
            "parts_done_delta": (parts_done - d.parts_done_start) if parts_done is not None and d.parts_done_start is not None else None,
            "reward_inr": r, "prediction_correct": correct, "policy_updated": learn,
            "q_before": None if q_before is None else round(q_before, 2),
            "q_after": None if q_after is None else round(q_after, 2),
        }
        st = self.stats
        st["decisions"] += 1
        st["online_updates"] += int(learn)
        st["predictions_correct"] += int(correct)
        st["followed"] += int(row["followed"])
        st["reward_inr"] = round(st["reward_inr"] + r, 2)
        self.recent.appendleft(row)
        if self.sink:
            try:
                self.sink(row)
            except Exception as err:  # logging must never break telemetry
                print(f"[rl sink error]: {err}")

    # --------------------------------------------------------------------- outputs
    def recommendation(self, mid: str) -> dict[str, Any] | None:
        d = self.open.get(mid)
        if d is None:
            return None
        return {"machine_id": mid, "since": d.ts, "state": d.state, "features": d.features, "prediction": d.prediction,
                "action": d.recommended, "label": ACTION_LABEL[d.recommended], "derate_pct": ACTION_DERATE[d.recommended],
                "q_values": dict(zip(ACTIONS, d.q_values)), "state_seen_in_training": self.policy.known(d.state),
                "seconds_to_next_decision": round((DECISION_TICKS - self.ticks.get(mid, 0)) * TICK_S, 1),
                "mode": d.mode, "requires_approval": True}

    def status(self, recent: int = 20) -> dict[str, Any]:
        st = self.stats
        n = st["decisions"]
        return {
            "label": "Simulation-trained policy (tabular Q-learning) — recommendations only; an operator approves any action",
            "trained": self.trained,
            "policy": {**{k: v for k, v in self.policy.meta.items() if k != "returns_curve"},
                       "states_learned": len(self.policy.q), "actions": list(ACTIONS),
                       "action_labels": ACTION_LABEL},
            "decision_interval_s": DECISION_S,
            "air_pressure_bar": self.air_bar,
            "reward": {"inr": REWARD, "basis": REWARD_BASIS},
            "recommendations": {mid: self.recommendation(mid) for mid in sorted(self.open)},
            "feedback": {**st, "prediction_accuracy": round(st["predictions_correct"] / n, 3) if n else None,
                         "followed_rate": round(st["followed"] / n, 3) if n else None,
                         "mean_reward_inr": round(st["reward_inr"] / n, 1) if n else None},
            "recent": list(self.recent)[:recent],
        }


def returns_curve(returns: list[float], points: int = 50) -> list[dict[str, float]]:
    size = max(1, math.ceil(len(returns) / points))
    return [{"episode": i + len(returns[i:i + size]), "mean_return_inr": round(sum(returns[i:i + size]) / len(returns[i:i + size]), 1)}
            for i in range(0, len(returns), size)]
