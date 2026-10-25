"""Plant simulator for physical machine assets (hidden 'true' machine).

Produces schema-compliant 'observed' messages under SIM mode.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from config import get_config
from sim.faults import FaultManager
from sim.plc import PlcState, VirtualPlc

SCHEMA_VERSION = "1.0"
TICK_S = 0.5

# Healthy vibration baselines (velocity RMS, mm/s). Chosen so a healthy machine sits in
# zone A of its configured profile (rigid A/B 1.4, flexible A/B 2.3; config/machines.yaml)
# with noise well clear of the boundary. Faults push values up from here.
CNC_VIB_BASE = 0.8           # + 0.25 × (rpm / 1540)²  → VMC ≈ 1.05, lathe ≈ 1.14 at nominal speed
CNC_VIB_NOISE = 0.06
SAW_VIB_BASE = 0.2           # + 0.028 × blade_wear_pct → ≈ 1.18 at 35 %, ≈ 2.9 at 95 % (trip)
SAW_VIB_NOISE = 0.06
COMP_VIB_BASE = 1.8          # flexible mounts
COMP_VIB_NOISE = 0.06

# Fault effects build up and die away as first-order lags (time constants in s), so readings
# move gradually both when a fault starts and after a (simulated) repair.
BEARING_EXCESS_MM_S = 5.45   # extra vibration at nominal speed once a bearing defect is established
BEARING_GROWTH_MM_S_PER_S = 0.05
BEARING_TAU_ON, BEARING_TAU_OFF = 10.0, 4.0
BEARING_AFTER_REPAIR_MM_S = 1.2  # new bearing: residual excess right after repair (run-in), then decays
OVERLOAD_EXTRA_NM = 23.0     # ~63 N·m at 1540 rpm ≈ 10.2 kW ≈ 92 % of the VMC's 11 kW, just under the temperature limit
OVERLOAD_TAU_ON, OVERLOAD_TAU_OFF = 8.0, 4.0
TOOL_WEAR_ACCEL = 8.0        # tool_wear_accel multiplies the wear rate
COOLANT_LOSS_PER_S = 0.005   # true cooling capacity lost per second of coolant degradation


def _lag(value: float, target: float, tau: float) -> float:
    """One tick of a first-order lag toward `target` with time constant `tau` seconds."""
    return value + (target - value) * min(1.0, TICK_S / tau)

# Machine classes and thresholds live in config/machines.yaml.
TEMPLATE_PREFIX = get_config().prefixes()
NAMEPLATE_DEFAULTS = {tpl: get_config().nameplate(tpl) for tpl in TEMPLATE_PREFIX}


@dataclass
class PlantMachine:
    """Represents the physical plant of a machine with true hidden state."""

    machine_id: str
    template: str
    name: str
    hero: bool = False
    x: float = 0.0
    y: float = 0.0
    order: int = 0
    nameplate: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)  # thresholds from config/machines.yaml

    # Faults and PLC
    faults: FaultManager = field(default_factory=FaultManager)
    plc: VirtualPlc = field(default_factory=VirtualPlc)
    derate_pct: float = 0.0

    # CNC physical state
    air_temp_k: float = 300.0
    process_temp_k: float = 311.5
    tool_wear_min: float = 118.0
    h_true: float = 1.0  # true cooling capacity multiplier (hidden ground truth)
    vibration: float = 1.05
    parts_done: int = 0
    last_temp_readings: list[float] = field(default_factory=list)

    energy_kwh: float = 0.0  # integrated from reported power_kw each tick (FactoryEngine)

    # Gradual fault effects (see _lag)
    vib_excess: float = 0.0
    torque_excess: float = 0.0

    # Saw & Compressor state
    blade_wear_pct: float = 35.0
    pressure_bar: float = 7.0
    discharge_temp_c: float = 82.0

    # Variation knobs for the UDYAM-SIM training dataset (tools/generate_dataset.py).
    # Neutral defaults: with these values the live demo behaves exactly as before.
    ambient_offset_k: float = 0.0   # hotter or cooler shop floor
    load_scale: float = 1.0         # heavier or lighter jobs (cutting torque)
    noise_scale: float = 1.0        # sensor and process noise level
    fault_severity: float = 1.0     # how aggressive an injected fault is
    # Testing-mode machine parameters (sim/controls.py); neutral defaults change nothing.
    speed_scale: float = 1.0        # commanded spindle speed as a share of nominal
    cooling_scale: float = 1.0      # coolant system capacity (multiplies h_true)
    extra_vib_mm_s: float = 0.0     # added vibration at nominal speed

    def __post_init__(self):
        if not self.nameplate:
            self.nameplate = dict(NAMEPLATE_DEFAULTS.get(self.template, {}))
        self.limits = get_config().thresholds(self.machine_id, self.template)
        if "vibration_trip_mm_s" in self.limits:
            self.plc.vib_trip_threshold = self.limits["vibration_trip_mm_s"]
        if self.template == "cnc_lathe":
            self.tool_wear_min = 64.0
            self.process_temp_k = 310.8

    def registry_entry(self) -> dict:
        return {
            "machine_id": self.machine_id,
            "template": self.template,
            "name": self.name,
            "hero": self.hero,
            "layout": {"x": self.x, "y": self.y, "order": self.order},
            "nameplate": self.nameplate,
        }

    def step_cnc(self, t: float, rng: random.Random, air_ok: bool, ts_str: str) -> tuple[dict, list[dict]]:
        """Advance CNC mill or lathe plant dynamics by one tick."""
        is_mill = self.template == "cnc_mill"
        events: list[dict] = []

        # Air-pressure interlock derates CNCs by 25% if air pressure is insufficient
        air_derate = 0.0 if air_ok else 25.0
        effective_derate = max(self.derate_pct, air_derate)

        # Critical trip condition for bearing defect
        has_trip = self.faults.is_active("bearing_defect") and self.vibration > self.limits["vibration_trip_mm_s"]
        plc_state = self.plc.evaluate(
            has_critical_trip=has_trip,
            vibration_rms=self.vibration,
            derate_pct=effective_derate,
        )
        running = self.plc.is_running

        # Spindle mechanics: a derate scales the commanded setpoint; measured rpm scatters around it
        rpm_nominal = float(self.nameplate["rpm_nominal"])
        ns, sev = self.noise_scale, self.fault_severity
        surge = self.faults.is_active("order_surge")  # rush order: faster spindle, heavier feeds
        rpm_set = rpm_nominal * self.speed_scale * (1.0 - effective_derate / 100.0)
        if surge:
            rpm_set *= 1.15
        torque_nominal = (40.0 if is_mill else 30.0) * self.load_scale
        if surge:
            torque_nominal *= 1.10
        self.torque_excess = _lag(self.torque_excess, OVERLOAD_EXTRA_NM * sev if self.faults.is_active("overload") else 0.0,
                                  OVERLOAD_TAU_ON if self.faults.is_active("overload") else OVERLOAD_TAU_OFF)
        torque = torque_nominal + self.torque_excess + rng.gauss(0.0, 1.2 * ns)

        rpm = (rpm_set + rng.gauss(0.0, 8.0 * ns)) if running else 0.0
        torque = torque if running else 0.0
        power_kw = torque * rpm * 2.0 * math.pi / 60.0 / 1000.0

        # Ambient and cooling dynamics
        self.air_temp_k = 300.0 + self.ambient_offset_k + 0.4 * math.sin(t / 60.0) + rng.gauss(0.0, 0.05 * ns)
        if self.faults.is_active("coolant_degradation"):
            self.h_true = max(0.55, self.h_true - COOLANT_LOSS_PER_S * sev * TICK_S)


        thermal_target = self.air_temp_k + 8.0 + 0.7 * power_kw / (self.h_true * self.cooling_scale)
        self.process_temp_k += (thermal_target - self.process_temp_k) * TICK_S / 20.0

        # Tool wear progression
        wear_rate = 0.05 * (rpm / rpm_nominal) * ((1.0 + (TOOL_WEAR_ACCEL - 1.0) * sev) if self.faults.is_active("tool_wear_accel") else 1.0)
        self.tool_wear_min += wear_rate
        if self.tool_wear_min > self.limits["tool_wear_limit_min"]:
            self.tool_wear_min = 0.0
            events.append({
                "type": "event",
                "schema_version": SCHEMA_VERSION,
                "ts": ts_str,
                "machine_id": self.machine_id,
                "kind": "WORK_ORDER",
                "severity": "INFO",
                "title": "Tool changed at end of life (wear reset)",
            })

        # Vibration: healthy level plus a bearing-defect excess that builds up (and dies away) gradually
        if self.faults.is_active("bearing_defect"):
            dt = self.faults.time_since("bearing_defect", t)
            self.vib_excess = _lag(self.vib_excess, (BEARING_EXCESS_MM_S + BEARING_GROWTH_MM_S_PER_S * dt) * sev,
                                   BEARING_TAU_ON)
        else:
            self.vib_excess = _lag(self.vib_excess, 0.0, BEARING_TAU_OFF)
        speed = rpm / rpm_nominal
        base_vib = CNC_VIB_BASE + 0.25 * speed ** 2 + (self.vib_excess + self.extra_vib_mm_s) * speed
        self.vibration = max(0.0, base_vib + rng.gauss(0.0, CNC_VIB_NOISE * ns)) if running else 0.3

        # Sensor readings and Data Quality (DQ) simulation
        temp_reading: float | None = self.process_temp_k + rng.gauss(0.0, 0.15 * ns)
        dq = "OK"
        if self.faults.is_active("sensor_stuck"):
            temp_reading = self.last_temp_readings[-1] if self.last_temp_readings else temp_reading
        if self.faults.is_active("sensor_dropout"):
            temp_reading, dq = None, "MISSING"

        if temp_reading is not None:
            self.last_temp_readings = (self.last_temp_readings + [round(temp_reading, 2)])[-8:]
            if len(self.last_temp_readings) == 8 and len(set(self.last_temp_readings)) == 1:
                dq = "STUCK"

        current_a = round(1.2 + power_kw * 1.6 + rng.gauss(0.0, 0.1 * ns), 2)
        observed = {
            "type": "observed",
            "schema_version": SCHEMA_VERSION,
            "ts": ts_str,
            "machine_id": self.machine_id,
            "template": self.template,
            "source_mode": "SIM",
            "plc_state": plc_state.value,
            "dq_flag": dq,
            "power_kw": round(power_kw, 3),
            "current_a": current_a,
            "vibration_rms_mm_s": round(self.vibration, 2),
            "derate_pct": effective_derate,
            "active_fault": self.faults.primary_fault(),
            "parts_done": self.parts_done,
            "air_temp_k": round(self.air_temp_k, 2),
            "process_temp_k": None if temp_reading is None else round(temp_reading, 2),
            "rpm": round(rpm, 1),
            "rpm_setpoint": round(rpm_set, 1) if running else 0.0,
            "rpm_nominal": rpm_nominal,
            "torque_nm": round(torque, 2),
            "tool_wear_min": round(self.tool_wear_min, 1),
        }
        return observed, events

    def step_saw(self, rng: random.Random, ts_str: str) -> dict:
        """Advance bar saw plant dynamics by one tick."""
        if self.faults.is_active("blade_wear"):
            self.blade_wear_pct = min(100.0, self.blade_wear_pct + 0.3)
        else:
            self.blade_wear_pct = min(100.0, self.blade_wear_pct + 0.005)

        has_trip = self.blade_wear_pct >= self.limits["blade_wear_trip_pct"]
        plc_state = self.plc.evaluate(has_critical_trip=has_trip, derate_pct=self.derate_pct)
        running = self.plc.is_running

        rpm_nominal = float(self.nameplate["rpm_nominal"])
        rpm_set = rpm_nominal * self.speed_scale * (1.0 - self.derate_pct / 100.0)
        rpm = (rpm_set + rng.gauss(0.0, 5.0)) if running else 0.0
        power = (1.4 + 0.006 * self.blade_wear_pct + rng.gauss(0.0, 0.03)) if running else 0.05
        self.vibration = (SAW_VIB_BASE + 0.028 * self.blade_wear_pct + self.extra_vib_mm_s + rng.gauss(0.0, SAW_VIB_NOISE)) if running else 0.2

        return {
            "type": "observed",
            "schema_version": SCHEMA_VERSION,
            "ts": ts_str,
            "machine_id": self.machine_id,
            "template": self.template,
            "source_mode": "SIM",
            "plc_state": plc_state.value,
            "dq_flag": "OK",
            "power_kw": round(power, 3),
            "current_a": round(power * 1.9, 2),
            "vibration_rms_mm_s": round(self.vibration, 2),
            "derate_pct": self.derate_pct,
            "active_fault": self.faults.primary_fault(),
            "parts_done": self.parts_done,
            "rpm": round(rpm, 1),
            "rpm_setpoint": round(rpm_set, 1) if running else 0.0,
            "rpm_nominal": rpm_nominal,
            "blade_wear_pct": round(self.blade_wear_pct, 1),
        }

    def step_compressor(self, rng: random.Random, ts_str: str) -> dict:
        """Advance air compressor dynamics by one tick."""
        target_p = 5.2 if self.faults.is_active("pressure_drop") else 7.0
        self.pressure_bar += (target_p - self.pressure_bar) * TICK_S / 6.0 + rng.gauss(0.0, 0.03)

        duty = 100.0 if self.faults.is_active("pressure_drop") else 55.0 + rng.gauss(0.0, 2.0)
        temp_target = 95.0 if duty > 90.0 else 82.0
        self.discharge_temp_c += (temp_target - self.discharge_temp_c) * TICK_S / 15.0

        rated_kw = float(self.nameplate.get("rated_kw", 7.5))
        power = rated_kw * (0.35 + 0.65 * duty / 100.0)
        self.vibration = COMP_VIB_BASE + self.extra_vib_mm_s + rng.gauss(0.0, COMP_VIB_NOISE)
        self.plc.state = PlcState.RUN

        return {
            "type": "observed",
            "schema_version": SCHEMA_VERSION,
            "ts": ts_str,
            "machine_id": self.machine_id,
            "template": self.template,
            "source_mode": "SIM",
            "plc_state": "RUN",
            "dq_flag": "OK",
            "power_kw": round(power, 3),
            "current_a": round(power * 1.8, 2),
            "vibration_rms_mm_s": round(self.vibration, 2),
            "derate_pct": 0.0,
            "active_fault": self.faults.primary_fault(),
            "parts_done": None,
            "pressure_bar": round(self.pressure_bar, 2),
            "discharge_temp_c": round(self.discharge_temp_c, 1),
            "duty_pct": round(duty, 1),
        }

    def resolve(self, ts_str: str) -> list[dict]:
        """Simulated repair: clear this machine's faults and release any SIM derate.

        Effects then die away gradually (lags, coolant restore). A tool-wear repair is a tool change.
        """
        events: list[dict] = []
        if self.faults.is_active("bearing_defect"):
            self.vib_excess = min(self.vib_excess, BEARING_AFTER_REPAIR_MM_S)
        if self.faults.is_active("coolant_degradation"):
            self.h_true = 1.0  # coolant serviced; the temperature still falls gradually (thermal lag)
        if self.faults.is_active("tool_wear_accel"):
            self.tool_wear_min = 0.0
            events.append({
                "type": "event", "schema_version": SCHEMA_VERSION, "ts": ts_str, "machine_id": self.machine_id,
                "kind": "RESOLUTION", "severity": "INFO", "title": "Tool changed (simulated repair): tool wear reset to 0 min",
            })
        self.faults.clear()
        self.derate_pct = 0.0
        return events
