"""Fake 4-machine factory that emits schema-v1.0 messages (docs/schema.json).

This is a *development stand-in* so the 3D, backend and dashboard work can
start before the real plant/twin core exists. It has simple, plausible
dynamics (first-order thermal model, buffers, a toy calibrator) but it is NOT
the real twin. The real code lives in sim/ and twin/ and must emit the same
messages.

Pure Python, no dependencies, deterministic for a given seed.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = "1.0"
TICK_S = 0.5
TIME_SCALE = 30  # demo time runs 30x faster than plant time (used for "minutes" and "per hour")

# Demo thresholds: configurable demo settings, not engineering limits.
TEMP_LIMIT_K = 315.5  # process-temperature warning (physics rule, "OVERTEMP")
VIB_ZONE_C = 4.5      # mm/s RMS: ISO 20816-3-*referenced* demo zones
VIB_ZONE_D = 7.1
PRESSURE_MIN_BAR = 6.0
INR_PER_PART = 450.0  # user-entered contribution per finished part (assumption)

TEMPLATE_PREFIX = {"cnc_mill": "VMC", "cnc_lathe": "LATHE", "bar_saw": "SAW", "air_compressor": "COMP"}
NAMEPLATE_DEFAULTS = {
    "cnc_mill": {"rated_kw": 11.0, "max_rpm": 3000, "make": "Representative VMC (geometry: TseNC)", "year": 2012},
    "cnc_lathe": {"rated_kw": 7.5, "max_rpm": 3500, "make": "Generic CNC lathe", "year": 2010},
    "bar_saw": {"rated_kw": 2.2, "max_rpm": 1400, "make": "Generic bar saw", "year": 2008},
    "air_compressor": {"rated_kw": 7.5, "rated_pressure_bar": 8.0, "make": "Generic screw compressor", "year": 2015},
}
# Line stations in flow order: (machine_id, cycle seconds of demo time)
LINE = [("SAW-01", 20.0), ("LATHE-01", 25.0), ("VMC-01", 30.0)]
BUFFER_CAP = 10


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@dataclass
class Machine:
    machine_id: str
    template: str
    name: str
    hero: bool
    x: float
    y: float
    order: int
    nameplate: dict
    faults: set = field(default_factory=set)
    fault_t0: dict = field(default_factory=dict)
    derate_pct: float = 0.0
    plc_state: str = "RUN"
    # CNC state
    air_temp_k: float = 300.0
    process_temp_k: float = 311.5
    tool_wear_min: float = 118.0
    h_true: float = 1.0      # true cooling capacity multiplier (hidden from the twin)
    h_est: float = 1.0       # twin's calibrated estimate
    k_wear_est: float = 1.0
    vibration: float = 2.2
    # saw / compressor state
    blade_wear_pct: float = 35.0
    pressure_bar: float = 7.0
    discharge_temp_c: float = 82.0
    # twin bookkeeping
    ewma: float = 0.0
    cusum: float = 0.0
    vib_ewma: float = 2.2
    param_version: list = field(default_factory=lambda: [1, 0])
    h_at_version: float = 1.0
    frozen_reason: str | None = None
    last_temp_readings: list = field(default_factory=list)
    drift_announced: bool = False
    anomaly_announced: bool = False
    alert_announced: bool = False
    parts_done: int = 0
    pred_temp: float | None = None
    y_filt: float | None = None
    dy_filt: float = 0.0

    def registry_entry(self) -> dict:
        return {
            "machine_id": self.machine_id, "template": self.template, "name": self.name, "hero": self.hero,
            "layout": {"x": self.x, "y": self.y, "order": self.order}, "nameplate": self.nameplate,
        }


class FakeFactory:
    def __init__(self, seed: int = 42, start: datetime | None = None, auto_demo: bool = True, machines: int = 4):
        self.seed = seed
        self.auto_demo = auto_demo
        self.n_machines = machines
        self.start = start or datetime(2026, 10, 26, 10, 0, 0, tzinfo=timezone.utc)
        self.reset(announce=False)

    # ------------------------------------------------------------------ setup
    def reset(self, announce: bool = True) -> list[dict]:
        self.rng = random.Random(self.seed)
        self.t = getattr(self, "t", 0.0)  # clock stays monotonic across resets
        self.demo_t = 0.0
        self.event_seq = getattr(self, "event_seq", 0)
        self.machines: dict[str, Machine] = {}
        self._add("cnc_mill", "CNC Mill (VMC)", hero=True, x=8.0, y=0.0, order=2)
        self._add("cnc_lathe", "CNC Lathe", x=4.0, y=0.0, order=1)
        self._add("bar_saw", "Bar Saw", x=0.0, y=0.0, order=0)
        if self.n_machines >= 4:
            self._add("air_compressor", "Air Compressor", x=4.0, y=-4.0, order=-1)
        self.buffers = {mid: 4 for mid, _ in LINE[1:]}  # parts waiting in front of lathe and mill
        self.progress = {mid: 0.0 for mid, _ in LINE}
        self.rate_ewma = 3600.0 / LINE[-1][1] / TIME_SCALE
        self.common_cause_announced = False
        self.pending: list[dict] = []
        self.registry_dirty = announce  # machines added via the wizard are gone after a reset
        if announce:
            self._event(None, "RESET", "INFO", "Simulation reset")
        return self.flush_events()

    def _add(self, template: str, name: str, hero: bool = False, x: float = 0.0, y: float = 0.0,
             order: int | None = None, nameplate: dict | None = None) -> Machine:
        prefix = TEMPLATE_PREFIX[template]
        n = 1 + sum(1 for m in self.machines.values() if m.template == template)
        mid = f"{prefix}-{n:02d}"
        if order is None:
            order = -1 if template == "air_compressor" else len(self.machines)
        m = Machine(mid, template, name, hero, x, y, order, {**NAMEPLATE_DEFAULTS[template], **(nameplate or {})})
        if template == "cnc_lathe":
            m.tool_wear_min = 64.0
            m.process_temp_k = 310.8
        self.machines[mid] = m
        return m

    # --------------------------------------------------------------- commands
    def command(self, cmd: dict) -> list[dict]:
        """Apply a client command. Returns messages to broadcast (events, registry)."""
        kind = cmd.get("cmd")
        out: list[dict] = []
        if kind == "inject_fault":
            m = self.machines.get(cmd.get("machine_id", ""))
            if m:
                self._inject(m, cmd["fault"])
        elif kind == "clear_faults":
            targets = [self.machines[cmd["machine_id"]]] if cmd.get("machine_id") in self.machines else self.machines.values()
            for m in targets:
                m.faults.clear()
                m.fault_t0.clear()
        elif kind == "reset":
            events = self.reset()
            self.registry_dirty = False
            return [self.registry()] + events
        elif kind == "auto_demo":
            self.auto_demo = bool(cmd.get("on", True))
        elif kind == "set_derate":
            m = self.machines.get(cmd.get("machine_id", ""))
            if m:
                m.derate_pct = max(0.0, min(100.0, float(cmd.get("pct", 0))))
        elif kind == "add_machine":
            tpl = cmd.get("template", "air_compressor")
            if tpl in TEMPLATE_PREFIX:
                m = self._add(tpl, cmd.get("name") or tpl.replace("_", " ").title(),
                              x=float(cmd.get("x", 4.0 * len(self.machines))), y=float(cmd.get("y", -4.0)),
                              nameplate=cmd.get("nameplate"))
                self._event(m.machine_id, "MACHINE_ADDED", "INFO", f"{m.name} onboarded from '{tpl}' template",
                            data={"template": tpl, "calibration": "starting from template defaults"})
                out.append(self.registry())
        return out + self.flush_events()

    def _inject(self, m: Machine, fault: str) -> None:
        m.faults.add(fault)
        m.fault_t0[fault] = self.t
        self._event(m.machine_id, "FAULT_INJECTED", "INFO", f"Injected '{fault}' on {m.name} (plant only; the twin is not told)",
                    data={"fault": fault})

    # ------------------------------------------------------------- messages
    def hello(self) -> dict:
        return {"type": "hello", "schema_version": SCHEMA_VERSION, "generator": "fake_stream", "tick_ms": int(TICK_S * 1000)}

    def registry(self) -> dict:
        return {"type": "registry", "schema_version": SCHEMA_VERSION,
                "machines": [m.registry_entry() for m in self.machines.values()]}

    def _event(self, machine_id, kind, severity, title, measured=None, inferred=None, suggested=None, data=None):
        self.event_seq += 1
        ev = {"type": "event", "schema_version": SCHEMA_VERSION, "ts": self._ts(), "event_id": f"E{self.event_seq:05d}",
              "machine_id": machine_id, "kind": kind, "severity": severity, "title": title}
        for k, v in (("measured", measured), ("inferred", inferred), ("suggested", suggested), ("data", data)):
            if v is not None:
                ev[k] = v
        self.pending.append(ev)

    def flush_events(self) -> list[dict]:
        out, self.pending = self.pending, []
        return out

    def _ts(self) -> str:
        return _iso(self.start + timedelta(seconds=self.t))

    # ------------------------------------------------------------------ tick
    def step(self) -> list[dict]:
        self.t += TICK_S
        if self.auto_demo:
            self._auto_demo()
        comp = next((m for m in self.machines.values() if m.template == "air_compressor"), None)
        air_ok = True
        out: list[dict] = []
        if self.registry_dirty:
            self.registry_dirty = False
            out.append(self.registry())
        if comp:
            obs = self._step_compressor(comp)
            air_ok = comp.pressure_bar >= PRESSURE_MIN_BAR
            out += [obs, self._twin_simple(comp, obs)]
        self._common_cause(air_ok)
        for m in self.machines.values():
            if m.template in ("cnc_mill", "cnc_lathe"):
                obs = self._step_cnc(m, air_ok)
                out += [obs, self._twin_cnc(m, obs)]
            elif m.template == "bar_saw":
                obs = self._step_saw(m)
                out += [obs, self._twin_simple(m, obs)]
        out.append(self._step_line(air_ok))
        return out + self.flush_events()

    def _auto_demo(self) -> None:
        """Scripted 150 s loop so frontends see every state without clicking.

        5 s coolant degradation on the mill -> ~35 s drift -> calibration -> ~95 s forecast + SIM auto-derate
        105 s compressor pressure drop (common cause) -> 118 s fixed -> 125 s bearing spike (anomaly, frozen)
        -> 145 s reset and repeat.
        """
        self.demo_t += TICK_S
        t = round(self.demo_t, 1)
        mill = self.machines.get("VMC-01")
        comp = self.machines.get("COMP-01")
        if t == 5 and mill:
            self._inject(mill, "coolant_degradation")
        elif t == 105 and comp:
            self._inject(comp, "pressure_drop")
        elif t == 118 and comp:
            comp.faults.discard("pressure_drop")
            self._event(comp.machine_id, "WORK_ORDER", "INFO", "Compressor filter replaced (work order closed)",
                        data={"status": "Closed"})
        elif t == 125 and mill:
            self._inject(mill, "bearing_defect")
        elif t >= 145:
            self.pending += self.reset()
            self.demo_t = 0.0

    def _noise(self, sigma: float) -> float:
        return self.rng.gauss(0.0, sigma)

    def _step_cnc(self, m: Machine, air_ok: bool) -> dict:
        is_mill = m.template == "cnc_mill"
        # PLC: air-pressure interlock derates both CNCs; self-heal derate on mill is set in _twin_cnc
        air_derate = 0.0 if air_ok else 25.0
        derate = max(m.derate_pct, air_derate)
        if "bearing_defect" in m.faults and m.vibration > VIB_ZONE_D * 1.4:
            m.plc_state = "TRIP"
        elif derate > 0:
            m.plc_state = "DERATE"
        else:
            m.plc_state = "RUN"
        running = m.plc_state != "TRIP"

        rpm_set = (1540.0 if is_mill else 1800.0) * (1 - derate / 100.0)
        torque = (40.0 if is_mill else 30.0) + self._noise(1.2)
        if "overload" in m.faults:
            torque += 22.0
        rpm = (rpm_set + self._noise(8.0)) if running else 0.0
        torque = torque if running else 0.0
        power_kw = torque * rpm * 2 * math.pi / 60 / 1000

        m.air_temp_k = 300.0 + 0.4 * math.sin(self.t / 60.0) + self._noise(0.05)
        if "coolant_degradation" in m.faults:
            m.h_true = max(0.55, m.h_true - 0.005 * TICK_S)  # gradual: 0.005 per second
        target = m.air_temp_k + 8.0 + 0.7 * power_kw / m.h_true
        m.process_temp_k += (target - m.process_temp_k) * TICK_S / 20.0

        wear_rate = 0.05 * (rpm / 1540.0) * (4.0 if "tool_wear_accel" in m.faults else 1.0)
        m.tool_wear_min += wear_rate
        if m.tool_wear_min > 235.0:
            m.tool_wear_min = 0.0
            self._event(m.machine_id, "WORK_ORDER", "INFO", "Tool changed at end of life (wear reset)")

        if "bearing_defect" in m.faults:
            dt = self.t - m.fault_t0["bearing_defect"]
            base_vib = 6.5 + 0.08 * dt
        else:
            base_vib = 2.0 + 0.25 * (rpm / 1540.0) ** 2
        m.vibration = max(0.0, base_vib + self._noise(0.12)) if running else 0.3

        temp_reading: float | None = m.process_temp_k + self._noise(0.15)
        dq = "OK"
        if "sensor_stuck" in m.faults:
            temp_reading = m.last_temp_readings[-1] if m.last_temp_readings else temp_reading
        if "sensor_dropout" in m.faults:
            temp_reading, dq = None, "MISSING"
        if temp_reading is not None:
            m.last_temp_readings = (m.last_temp_readings + [round(temp_reading, 2)])[-8:]
            if len(m.last_temp_readings) == 8 and len(set(m.last_temp_readings)) == 1:
                dq = "STUCK"

        active = next(iter(sorted(m.faults)), None)
        return {
            "type": "observed", "schema_version": SCHEMA_VERSION, "ts": self._ts(), "machine_id": m.machine_id,
            "template": m.template, "source_mode": "SIM", "plc_state": m.plc_state, "dq_flag": dq,
            "power_kw": round(power_kw, 3), "current_a": round(1.2 + power_kw * 1.6 + self._noise(0.1), 2),
            "vibration_rms_mm_s": round(m.vibration, 2), "derate_pct": derate, "active_fault": active,
            "parts_done": m.parts_done,
            "air_temp_k": round(m.air_temp_k, 2), "process_temp_k": None if temp_reading is None else round(temp_reading, 2),
            "rpm": round(rpm, 1), "torque_nm": round(torque, 2), "tool_wear_min": round(m.tool_wear_min, 1),
        }

    def _twin_cnc(self, m: Machine, obs: dict) -> dict:
        """Toy version of the hybrid twin: predict, residual, CUSUM, bounded calibration, freeze, forecast."""
        band, k_cusum, h_cusum = 0.6, 0.2, 2.5
        power = obs["power_kw"] or 0.0
        pred_target = obs["air_temp_k"] + 8.0 + 0.7 * power / m.h_est
        if m.pred_temp is None:
            m.pred_temp = m.process_temp_k
        m.pred_temp += (pred_target - m.pred_temp) * TICK_S / 20.0

        y = obs["process_temp_k"]
        e = 0.0 if y is None else y - m.pred_temp
        if y is not None and obs["dq_flag"] == "OK":
            prev = m.y_filt if m.y_filt is not None else y
            m.y_filt = 0.9 * prev + 0.1 * y
            m.dy_filt = 0.9 * m.dy_filt + 0.1 * (m.y_filt - prev) / TICK_S
        m.ewma = 0.9 * m.ewma + 0.1 * e
        m.cusum = max(0.0, m.cusum + abs(e) - k_cusum)
        z_spike = abs(obs["vibration_rms_mm_s"] - m.vib_ewma) > 2.5
        m.vib_ewma = 0.95 * m.vib_ewma + 0.05 * obs["vibration_rms_mm_s"]

        # freeze rules: anomaly or data-quality fault => never learn
        if obs["dq_flag"] != "OK":
            m.frozen_reason = "data_quality"
        elif z_spike or "bearing_defect" in m.faults:
            m.frozen_reason = "anomaly"
        elif m.frozen_reason and not m.faults:
            m.frozen_reason = None

        state = "IN_BAND"
        if m.frozen_reason == "anomaly":
            state = "ANOMALY"
        elif m.cusum > h_cusum and abs(m.ewma) > band / 2:
            state = "DRIFT"

        if state == "ANOMALY" and not m.anomaly_announced:
            m.anomaly_announced = True
            self._event(m.machine_id, "ANOMALY", "CRITICAL", "Abrupt vibration change: calibration frozen",
                        measured=f"Vibration {obs['vibration_rms_mm_s']} mm/s RMS (was ~{m.vib_ewma:.1f})",
                        inferred="Sudden change, not gradual drift: possible bearing defect or sensor fault",
                        suggested="Stop at next safe point; inspect spindle bearing; verify accelerometer mounting",
                        data={"freeze_reason": "anomaly"})
        if obs["dq_flag"] != "OK" and m.frozen_reason == "data_quality" and not m.anomaly_announced:
            m.anomaly_announced = True
            self._event(m.machine_id, "ANOMALY", "WARN", f"Temperature sensor {obs['dq_flag'].lower()}: calibration frozen",
                        measured=f"dq_flag={obs['dq_flag']}", inferred="Sensor data not trustworthy",
                        suggested="Check sensor wiring and mounting", data={"freeze_reason": "data_quality"})
        if state == "DRIFT" and not m.drift_announced:
            m.drift_announced = True
            self._event(m.machine_id, "DRIFT", "WARN", f"Model mismatch: sustained {m.ewma:+.1f} K on process temperature",
                        measured=f"Observed {y:.1f} K vs predicted {m.pred_temp:.1f} K",
                        inferred="Machine behaviour has changed gradually; twin will recalibrate")

        # bounded, rate-limited calibration of h_cool (toy stand-in for RLS):
        # invert the thermal model at the *projected* steady state so thermal lag doesn't cause overshoot
        if state == "DRIFT" and m.frozen_reason is None and m.y_filt is not None and power > 0.5:
            y_ss = m.y_filt + 20.0 * m.dy_filt
            h_target = 0.7 * power / max(0.5, y_ss - obs["air_temp_k"] - 8.0)
            step = max(-0.015, min(0.015, 0.08 * (h_target - m.h_est)))
            m.h_est = max(0.4, min(1.2, m.h_est + step))
            if abs(m.h_est - m.h_at_version) >= 0.05:
                old = f"v{m.param_version[0]}.{m.param_version[1]}"
                m.param_version[1] += 1
                new = f"v{m.param_version[0]}.{m.param_version[1]}"
                self._event(m.machine_id, "CALIBRATION", "INFO",
                            f"Recalibrated h_cool {m.h_at_version:.2f} → {m.h_est:.2f} ({old} → {new})",
                            inferred=f"Cooling capacity estimated {100 * (1 - m.h_est):.0f}% below nominal",
                            suggested="Check coolant level, pump and filter",
                            data={"param": "h_cool", "from": round(m.h_at_version, 3), "to": round(m.h_est, 3),
                                  "version": new, "residual_ewma": round(m.ewma, 3)})
                m.h_at_version = m.h_est

        # forecast: time for the calibrated twin to reach the temperature limit at current settings
        steady = obs["air_temp_k"] + 8.0 + 0.7 * power / m.h_est
        now_t = y if y is not None else m.pred_temp
        minutes, warning = None, None
        if now_t >= TEMP_LIMIT_K:
            minutes, warning = 0.0, "OVERTEMP"
        elif steady > TEMP_LIMIT_K:
            secs = -20.0 * math.log((steady - TEMP_LIMIT_K) / (steady - now_t))
            minutes, warning = round(secs * TIME_SCALE / 60.0, 1), "OVERTEMP"
        elif m.tool_wear_min > 200:
            minutes, warning = round((235 - m.tool_wear_min) / 0.05 * TICK_S * TIME_SCALE / 60.0, 1), "TWF_RISK"

        # supervised self-healing: SIM auto-derates the hero mill
        if m.hero and warning == "OVERTEMP" and minutes is not None and minutes < 10 and m.derate_pct == 0:
            m.derate_pct = 20.0
            self._event(m.machine_id, "ALERT", "WARN", "Over-temperature forecast: auto-derated 20% (SIM only)",
                        measured=f"Process temp {now_t:.1f} K, rising; RPM {obs['rpm']:.0f}",
                        inferred=f"Calibrated twin forecasts {TEMP_LIMIT_K:.0f} K in ~{minutes:.0f} min at current settings",
                        suggested="Check coolant flow and filter. On a LIVE machine this derate needs operator approval.",
                        data={"derate_pct": 20, "mode": "SIM_AUTO", "work_order_draft": True})

        vib = obs["vibration_rms_mm_s"]
        if (y or 0) > TEMP_LIMIT_K or vib > VIB_ZONE_D or obs["plc_state"] == "TRIP":
            rules = "HIGH"
        elif warning or vib > VIB_ZONE_C:
            rules = "MEDIUM"
        else:
            rules = "LOW"
        if rules == "HIGH" and not m.alert_announced and m.frozen_reason != "anomaly":
            m.alert_announced = True
            self._event(m.machine_id, "ALERT", "CRITICAL", "Process temperature above limit",
                        measured=f"{y} K > {TEMP_LIMIT_K} K", inferred="Cooling insufficient for current load",
                        suggested="Reduce load; inspect coolant system")
        top = []
        if warning == "OVERTEMP" or e > band:
            top.append("process_temp_k")
        if vib > VIB_ZONE_C:
            top.append("vibration_rms_mm_s")
        if m.tool_wear_min > 180:
            top.append("tool_wear_min")
        # stand-in for the AI4I RandomForest probability (the real one comes from ml/)
        torque = obs["torque_nm"] or 0.0
        z = -6.0 + 0.012 * m.tool_wear_min + 0.04 * max(0.0, torque - 40) + (2.5 if warning else 0.0)
        model_p = round(1 / (1 + math.exp(-z)), 3)

        return {
            "type": "twin", "schema_version": SCHEMA_VERSION, "ts": obs["ts"], "machine_id": m.machine_id,
            "predicted": {"process_temp_k": round(m.pred_temp, 2), "tool_wear_min": round(m.tool_wear_min, 1)},
            "residual": {"signal": "process_temp_k", "value": round(e, 3), "ewma": round(m.ewma, 3),
                         "cusum": round(m.cusum, 3), "band": band, "state": state},
            "params": {"version": f"v{m.param_version[0]}.{m.param_version[1]}",
                       "values": {"h_cool": round(m.h_est, 3), "k_heat": 1.0, "k_wear": round(m.k_wear_est, 3)},
                       "frozen": m.frozen_reason is not None, "freeze_reason": m.frozen_reason},
            "forecast": {"minutes_to_warning": minutes, "warning": warning},
            "risk": {"rules": rules, "model": model_p, "top_signals": top},
        }

    def _step_saw(self, m: Machine) -> dict:
        if "blade_wear" in m.faults:
            m.blade_wear_pct = min(100.0, m.blade_wear_pct + 0.3)
        else:
            m.blade_wear_pct = min(100.0, m.blade_wear_pct + 0.005)
        m.plc_state = "TRIP" if m.blade_wear_pct >= 95 else ("DERATE" if m.derate_pct > 0 else "RUN")
        running = m.plc_state != "TRIP"
        rpm = (900.0 * (1 - m.derate_pct / 100) + self._noise(5)) if running else 0.0
        power = (1.4 + 0.006 * m.blade_wear_pct + self._noise(0.03)) if running else 0.05
        m.vibration = (1.6 + 0.02 * m.blade_wear_pct + self._noise(0.08)) if running else 0.2
        return {
            "type": "observed", "schema_version": SCHEMA_VERSION, "ts": self._ts(), "machine_id": m.machine_id,
            "template": m.template, "source_mode": "SIM", "plc_state": m.plc_state, "dq_flag": "OK",
            "power_kw": round(power, 3), "current_a": round(power * 1.9, 2), "vibration_rms_mm_s": round(m.vibration, 2),
            "derate_pct": m.derate_pct, "active_fault": next(iter(sorted(m.faults)), None), "parts_done": m.parts_done,
            "rpm": round(rpm, 1), "blade_wear_pct": round(m.blade_wear_pct, 1),
        }

    def _step_compressor(self, m: Machine) -> dict:
        target = 5.2 if "pressure_drop" in m.faults else 7.0
        m.pressure_bar += (target - m.pressure_bar) * TICK_S / 6.0 + self._noise(0.03)
        duty = 100.0 if "pressure_drop" in m.faults else 55.0 + self._noise(2.0)
        m.discharge_temp_c += ((95.0 if duty > 90 else 82.0) - m.discharge_temp_c) * TICK_S / 15.0
        power = m.nameplate.get("rated_kw", 7.5) * (0.35 + 0.65 * duty / 100.0)
        m.vibration = 2.8 + self._noise(0.1)
        m.plc_state = "RUN"
        return {
            "type": "observed", "schema_version": SCHEMA_VERSION, "ts": self._ts(), "machine_id": m.machine_id,
            "template": m.template, "source_mode": "SIM", "plc_state": "RUN", "dq_flag": "OK",
            "power_kw": round(power, 3), "current_a": round(power * 1.8, 2), "vibration_rms_mm_s": round(m.vibration, 2),
            "derate_pct": 0.0, "active_fault": next(iter(sorted(m.faults)), None), "parts_done": None,
            "pressure_bar": round(m.pressure_bar, 2), "discharge_temp_c": round(m.discharge_temp_c, 1),
            "duty_pct": round(duty, 1),
        }

    def _twin_simple(self, m: Machine, obs: dict) -> dict:
        """Non-hero machines: same message shape, lighter logic."""
        if m.template == "air_compressor":
            signal, value = "pressure_bar", obs["pressure_bar"]
            predicted = 7.0
            low = value < PRESSURE_MIN_BAR
            rules = "HIGH" if low else ("MEDIUM" if value < 6.5 else "LOW")
            forecast = {"minutes_to_warning": 0.0 if low else None, "warning": "PRESSURE_LOW" if low else None}
            top = ["pressure_bar", "duty_pct"] if rules != "LOW" else []
        else:
            signal, value = "power_kw", obs["power_kw"]
            predicted = 1.4 + 0.006 * 35.0
            rules = "HIGH" if m.blade_wear_pct > 90 else ("MEDIUM" if m.blade_wear_pct > 75 else "LOW")
            forecast = {"minutes_to_warning": None, "warning": "BLADE_WEAR" if rules != "LOW" else None}
            top = ["blade_wear_pct"] if rules != "LOW" else []
        e = value - predicted
        return {
            "type": "twin", "schema_version": SCHEMA_VERSION, "ts": obs["ts"], "machine_id": m.machine_id,
            "predicted": {signal: round(predicted, 3)},
            "residual": {"signal": signal, "value": round(e, 3), "ewma": round(e, 3), "cusum": 0.0,
                         "band": 0.5, "state": "DRIFT" if abs(e) > 0.5 else "IN_BAND"},
            "params": {"version": "v1.0", "values": {}, "frozen": False, "freeze_reason": None},
            "forecast": forecast, "risk": {"rules": rules, "model": None, "top_signals": top},
        }

    def _common_cause(self, air_ok: bool) -> None:
        if not air_ok and not self.common_cause_announced:
            self.common_cause_announced = True
            affected = [m.machine_id for m in self.machines.values() if m.template in ("cnc_mill", "cnc_lathe")]
            self._event("COMP-01", "COMMON_CAUSE", "WARN", "Common cause: compressed-air supply low",
                        measured=f"Pressure below {PRESSURE_MIN_BAR} bar; {', '.join(affected)} derated together",
                        inferred="Root cause is the shared compressor, not the individual machines",
                        suggested="Inspect compressor intake filter and leaks; do not service the CNCs",
                        data={"affected": affected})
        elif air_ok:
            self.common_cause_announced = False

    def _step_line(self, air_ok: bool) -> dict:
        stations = []
        mill_rate_factor = 0.0
        for i, (mid, cycle) in enumerate(LINE):
            m = self.machines[mid]
            has_input = i == 0 or self.buffers[mid] > 0
            nxt = LINE[i + 1][0] if i + 1 < len(LINE) else None
            has_space = nxt is None or self.buffers[nxt] < BUFFER_CAP
            derate = m.derate_pct if m.template != "cnc_lathe" else 0.0
            if m.template in ("cnc_mill", "cnc_lathe") and not air_ok:
                derate = max(derate, 25.0)
            if m.plc_state == "TRIP":
                state, speed = "DOWN", 0.0
            elif not has_input:
                state, speed = "STARVED", 0.0
            elif not has_space:
                state, speed = "BLOCKED", 0.0
            elif derate > 0:
                state, speed = "DERATED", 1 - derate / 100.0
            else:
                state, speed = "RUNNING", 1.0
            self.progress[mid] += speed * TICK_S / cycle
            if self.progress[mid] >= 1.0:
                self.progress[mid] = 0.0
                if i > 0:
                    self.buffers[mid] -= 1
                if nxt:
                    self.buffers[nxt] += 1
                m.parts_done += 1
            if nxt is None:
                mill_rate_factor = speed
            stations.append({"machine_id": mid, "state": state,
                             "buffer_in": 0 if i == 0 else self.buffers[mid], "buffer_cap": BUFFER_CAP})

        nominal = 3600.0 / LINE[-1][1] / TIME_SCALE  # parts per plant-hour
        self.rate_ewma = 0.97 * self.rate_ewma + 0.03 * nominal * mill_rate_factor
        effective = {s["machine_id"]: (0.0 if s["state"] == "DOWN" else 1.0) for s in stations}
        bottleneck = min(LINE, key=lambda mc: effective[mc[0]] / mc[1])[0]
        return {
            "type": "line", "schema_version": SCHEMA_VERSION, "ts": self._ts(),
            "throughput_parts_per_hr": round(self.rate_ewma, 2),
            "line_loss_inr_per_hr": round(max(0.0, nominal - self.rate_ewma) * INR_PER_PART, 0),
            "bottleneck": bottleneck, "air_supply_ok": air_ok, "stations": stations,
        }
