"""Fault-injection lab: the four demo scenarios (simulation only).

Each scenario injects one or more plant faults on one machine. Effects build up gradually
(see the lags in sim/plant.py); the twin is not told which fault was injected and has to
notice it from the readings. "Simulate resolution" clears the faults, releases any SIM
derate, and lets the readings return gradually to the healthy baseline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    label: str
    machine_id: str
    faults: tuple[str, ...]
    what_changes: str
    expect: str
    resolution: str

    def public(self) -> dict:
        return {**asdict(self), "faults": list(self.faults), "kind": "Demo simulation"}


SCENARIOS: dict[str, Scenario] = {s.scenario_id: s for s in (
    Scenario(
        "vibration_fault", "Vibration fault (spindle bearing)", "VMC-01", ("bearing_defect",),
        "Spindle vibration rises over ~10 s and keeps creeping up",
        "Zone A → C (WARN) → D (CRITICAL); auto-derate 20 % (SIM only); calibration frozen",
        "Bearing replaced: vibration returns to zone A within ~15 s; derate released",
    ),
    Scenario(
        "over_temperature", "Over-temperature (coolant loss)", "VMC-01", ("coolant_degradation",),
        "True cooling capacity falls 0.5 %/s; process temperature creeps up",
        "Twin drifts, recalibrates h_cool, one cooling-loss alert at 30 %, over-temperature forecast → auto-derate",
        "Coolant serviced: cooling capacity restores, temperature falls; twin re-baselined",
    ),
    Scenario(
        "tool_wear", "Tool wear (accelerated)", "VMC-01", ("tool_wear_accel",),
        "Tool wears 8× faster",
        "RUL estimate shortens; tool-wear alert at 200 min",
        "Tool changed: wear reset to 0 min",
    ),
    Scenario(
        "overload", "Overload / order surge", "VMC-01", ("overload",),
        "Cutting torque rises by ~23 N·m over ~8 s (heavier cuts to meet a surge)",
        "Spindle load above 85 % of rated → overload alert (~17 s); AI4I failure risk rises (power-failure region); left running, the extra heat triggers the over-temperature auto-derate (~40 s)",
        "Load normalised: torque returns to nominal",
    ),
)}
