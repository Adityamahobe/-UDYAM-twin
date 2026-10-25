"""Testing-mode machine parameters and run control (simulation only).

Each parameter changes a field of the *simulated* plant (sim/plant.py) and nothing else: the plant
model has no path to real equipment, and the IoT hub has no command or downlink channel. The digital
twin is not told about a change; it sees the effect in the telemetry, like it would on a real machine
(for example, lower cooling capacity shows up as a recalibrated h_cool and, if severe, a coolant alert).

Limits are engineering limits for the simulated machine classes (config/machines.yaml nameplates):
a value outside them is refused, never clamped silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

CNC = ("cnc_mill", "cnc_lathe")
AIR_K_AT_0C = 273.15
BASE_AIR_K = 300.0          # PlantMachine's healthy shop-floor air temperature (26.85 °C)
BASE_TORQUE_NM = {"cnc_mill": 40.0, "cnc_lathe": 30.0}
POWER_LIMIT_FRAC = 1.0      # estimated spindle power at the chosen speed and load must stay within rated_kw
RUN_FOR_MAX_MIN = 240.0


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    unit: str
    lo: float
    hi: float
    default: float
    step: float
    templates: tuple[str, ...]
    help: str

    def public(self, template: str, nameplate: dict) -> dict[str, Any]:
        lo, hi = self.bounds(template, nameplate)
        return {"key": self.key, "label": self.label, "unit": self.unit, "min": lo, "max": hi,
                "default": self.default, "step": self.step, "help": self.help}

    def bounds(self, template: str, nameplate: dict) -> tuple[float, float]:
        hi = self.hi
        if self.key == "spindle_speed_pct" and nameplate.get("max_rpm") and nameplate.get("rpm_nominal"):
            hi = min(hi, math.floor(100.0 * nameplate["max_rpm"] / nameplate["rpm_nominal"]))
        return self.lo, hi


PARAMS: dict[str, ParamSpec] = {p.key: p for p in (
    ParamSpec("spindle_speed_pct", "Spindle speed", "% of nominal", 40.0, 120.0, 100.0, 5.0, CNC + ("bar_saw",),
              "Commanded speed as a share of the nameplate nominal rpm (capped by max rpm). A derate still applies on top."),
    ParamSpec("load_pct", "Cutting load", "% of nominal", 50.0, 130.0, 100.0, 5.0, CNC,
              "Cutting torque as a share of the nominal job. Speed × load must stay within the rated power."),
    ParamSpec("ambient_temp_c", "Ambient temperature", "°C", 10.0, 45.0, BASE_AIR_K - AIR_K_AT_0C, 0.5, CNC,
              "Shop-floor air temperature around the machine."),
    ParamSpec("cooling_capacity_pct", "Cooling capacity", "% of nominal", 50.0, 120.0, 100.0, 5.0, CNC,
              "Coolant system capacity. The twin is not told; it has to detect the change from the temperatures."),
    ParamSpec("extra_vibration_mm_s", "Extra vibration", "mm/s RMS", 0.0, 8.0, 0.0, 0.1, CNC + ("bar_saw", "air_compressor"),
              "Vibration added at nominal speed (e.g. imbalance or a loose mount); scales with speed on CNCs."),
)}


class ParamError(ValueError):
    pass


def specs_for(template: str, nameplate: dict) -> list[dict[str, Any]]:
    return [p.public(template, nameplate) for p in PARAMS.values() if template in p.templates]


def defaults_for(template: str) -> dict[str, float]:
    return {p.key: p.default for p in PARAMS.values() if template in p.templates}


def validate(template: str, nameplate: dict, current: dict[str, float], changes: dict[str, Any]) -> dict[str, float]:
    """Merged parameter set for one machine, or ParamError naming the first value outside its limits."""
    merged = {**defaults_for(template), **current}
    for key, raw in changes.items():
        spec = PARAMS.get(key)
        if spec is None or template not in spec.templates:
            raise ParamError(f"'{key}' is not a parameter of a {template.replace('_', ' ')}")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ParamError(f"{spec.label} must be a number")
        lo, hi = spec.bounds(template, nameplate)
        if not math.isfinite(value) or value < lo or value > hi:
            raise ParamError(f"{spec.label} {value:g} {spec.unit} is outside the limit {lo:g}–{hi:g} {spec.unit}")
        merged[key] = value
    if template in CNC:
        kw = estimated_power_kw(template, nameplate, merged)
        rated = float(nameplate.get("rated_kw") or 0.0)
        if rated and kw > rated * POWER_LIMIT_FRAC:
            raise ParamError(f"Speed {merged['spindle_speed_pct']:g} % × load {merged['load_pct']:g} % needs about "
                             f"{kw:.1f} kW, above the rated {rated:g} kW spindle power. Lower the speed or the load.")
    return merged


def estimated_power_kw(template: str, nameplate: dict, params: dict[str, float]) -> float:
    rpm = float(nameplate.get("rpm_nominal") or 0.0) * params.get("spindle_speed_pct", 100.0) / 100.0
    torque = BASE_TORQUE_NM.get(template, 0.0) * params.get("load_pct", 100.0) / 100.0
    return torque * rpm * 2.0 * math.pi / 60.0 / 1000.0


def apply(plant, params: dict[str, float]) -> None:
    """Set the simulated plant's knobs (PlantMachine fields). Neutral values leave it exactly as before."""
    p = {**defaults_for(plant.template), **params}
    plant.speed_scale = p.get("spindle_speed_pct", 100.0) / 100.0
    plant.load_scale = p.get("load_pct", 100.0) / 100.0
    plant.ambient_offset_k = p.get("ambient_temp_c", BASE_AIR_K - AIR_K_AT_0C) + AIR_K_AT_0C - BASE_AIR_K
    plant.cooling_scale = p.get("cooling_capacity_pct", 100.0) / 100.0
    plant.extra_vib_mm_s = p.get("extra_vibration_mm_s", 0.0)


def is_default(template: str, params: dict[str, float]) -> bool:
    d = defaults_for(template)
    return all(abs(params.get(k, v) - v) < 1e-9 for k, v in d.items())
