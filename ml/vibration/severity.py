"""Configurable, standard-referenced vibration severity assessment.

ISO 20816 evaluation zones (ISO 10816 before it was withdrawn) depend on the standard part and edition, the
machine group/class, power and speed range, foundation (rigid/flexible), measurement
location and direction, the measured quantity and its frequency band.

Profiles and machine assignments come from config/machines.yaml (config.loader). The
shipped profiles are configurable demo thresholds referencing ISO 20816-3 with
`verified: false`, so results are reported as "PROVISIONAL", never as a verified
severity. A machine without a complete profile returns status "UNAVAILABLE".

This module is independent of the ML prediction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

UNAVAILABLE_UNCONFIGURED = "Assessment unavailable: machine classification or applicable thresholds not configured."
SUPPORTED_QUANTITIES = {"velocity_rms_mm_s", "disp_pp_um", "accel_rms_m_s2"}
ZONE_KEYS = ("A_B", "B_C", "C_D")
STEADY_STATES = {"RUN", "DERATE"}


@dataclass
class ThresholdProfile:
    profile_id: str
    standard: str
    edition: str | None
    machine_group: str
    quantity: str
    zone_boundaries: dict[str, float | None]
    band_hz: list[float] | None = None
    measurement_location: str | None = None
    mounting: str | None = None
    rpm_range: list[float] | None = None
    power_kw_range: list[float] | None = None
    verification: dict[str, Any] = field(default_factory=dict)
    notes: str | None = None
    label: str | None = None

    @property
    def verified(self) -> bool:
        v = self.verification or {}
        return bool(v.get("verified")) and bool(v.get("verified_by")) and bool(v.get("source_reference"))

    def problems(self) -> list[str]:
        out = []
        if self.quantity not in SUPPORTED_QUANTITIES:
            out.append(f"quantity '{self.quantity}' not supported")
        vals = [self.zone_boundaries.get(k) for k in ZONE_KEYS]
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) for v in vals):
            out.append("zone boundaries A_B, B_C, C_D must all be configured numbers")
        elif not (0 < vals[0] < vals[1] < vals[2]):
            out.append("zone boundaries must be positive and strictly increasing")
        if not self.standard:
            out.append("standard not specified")
        for name in ("band_hz", "rpm_range", "power_kw_range"):
            r = getattr(self, name)
            if r is None:
                continue
            ok = (isinstance(r, list) and len(r) == 2
                  and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in r)
                  and 0 <= r[0] < r[1])
            if not ok:
                out.append(f"{name} must be null or [low, high] numbers")
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "standard": self.standard,
            "edition": self.edition,
            "machine_group": self.machine_group,
            "quantity": self.quantity,
            "band_hz": self.band_hz,
            "measurement_location": self.measurement_location,
            "mounting": self.mounting,
            "zone_boundaries": self.zone_boundaries,
            "label": self.label,
            "verified": self.verified,
            "source_reference": (self.verification or {}).get("source_reference"),
        }


@dataclass
class SeverityResult:
    status: str  # ASSESSED | PROVISIONAL | UNAVAILABLE | NOT_APPLICABLE | NO_DATA
    zone: str | None
    value: float | None
    quantity: str | None
    reason: str
    profile: dict[str, Any] | None = None
    threshold: dict[str, Any] | None = None  # boundary crossed (or next one up) for the zone

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "rule_based_severity",
            "status": self.status,
            "zone": self.zone,
            "value": self.value,
            "quantity": self.quantity,
            "reason": self.reason,
            "threshold": self.threshold,
            "profile": self.profile,
        }


def _zone_and_threshold(value: float, zb: dict[str, float], unit: str) -> tuple[str, dict[str, Any], str]:
    """Zone letter, {crossed, crossed_value, next, next_value}, and a one-line fact."""
    bounds = [("A/B", zb["A_B"]), ("B/C", zb["B_C"]), ("C/D", zb["C_D"])]
    idx = sum(1 for _, b in bounds if value >= b)
    zone = "ABCD"[idx]
    crossed = bounds[idx - 1] if idx > 0 else None
    nxt = bounds[idx] if idx < len(bounds) else None
    parts = []
    if crossed:
        parts.append(f"{value:g} {unit} ≥ {crossed[0]} {crossed[1]:g} {unit}")
    if nxt:
        parts.append(f"{'below' if crossed else f'{value:g} {unit} below'} {nxt[0]} {nxt[1]:g} {unit}")
    threshold = {
        "crossed": crossed[0] if crossed else None,
        "crossed_value": crossed[1] if crossed else None,
        "next": nxt[0] if nxt else None,
        "next_value": nxt[1] if nxt else None,
        "unit": unit,
    }
    return zone, threshold, ", ".join(parts)


QUANTITY_UNIT = {"velocity_rms_mm_s": "mm/s", "disp_pp_um": "µm", "accel_rms_m_s2": "m/s²"}


class SeverityAssessor:
    def __init__(self, config: dict[str, Any] | None = None):
        """`config` in severity_config shape; None loads config/machines.yaml."""
        if config is None:
            from config import get_config
            config = get_config().severity_config()
        self.config_errors: list[str] = []
        self.profiles: dict[str, ThresholdProfile] = {}
        for raw in config.get("threshold_profiles", []):
            try:
                p = ThresholdProfile(**{k: v for k, v in raw.items() if not k.startswith("_")})
            except TypeError as err:
                self.config_errors.append(f"invalid profile {raw.get('profile_id')}: {err}")
                continue
            self.profiles[p.profile_id] = p
        self.assignments: dict[str, dict[str, Any]] = {
            k: v for k, v in config.get("machine_assignments", {}).items() if not k.startswith("_")
        }
        # Fallback for machines added at runtime: they inherit their template's assignment.
        self.template_assignments: dict[str, dict[str, Any]] = dict(config.get("template_assignments", {}))

    def status(self) -> dict[str, Any]:
        return {
            "profiles_configured": len(self.profiles),
            "profiles_verified": sum(1 for p in self.profiles.values() if p.verified),
            "machines_assigned": sorted(self.assignments),
            "templates_assigned": sorted(self.template_assignments),
            "config_errors": self.config_errors,
        }

    def assess(
        self,
        machine_id: str,
        values: dict[str, float | None],
        *,
        measurement_band_hz: list[float] | tuple[float, float] | None = None,
        rpm: float | None = None,
        power_kw_rated: float | None = None,
        operating_state: str | None = None,
        dq_flag: str = "OK",
        template: str | None = None,
    ) -> SeverityResult:
        a = self.assignments.get(machine_id) or (self.template_assignments.get(template) if template else None)
        if not a or not a.get("profile_id"):
            return SeverityResult("UNAVAILABLE", None, None, None, UNAVAILABLE_UNCONFIGURED)
        p = self.profiles.get(a["profile_id"])
        if p is None:
            return SeverityResult("UNAVAILABLE", None, None, None,
                                  f"Assessment unavailable: profile '{a['profile_id']}' assigned to {machine_id} is not defined.")
        problems = p.problems()
        if problems:
            return SeverityResult("UNAVAILABLE", None, None, p.quantity,
                                  "Assessment unavailable: profile incomplete (" + "; ".join(problems) + ").", p.summary())

        for key in ("measurement_location", "mounting"):
            want, have = getattr(p, key), a.get(key)
            if want and want != have:
                return SeverityResult("NOT_APPLICABLE", None, None, p.quantity,
                                      f"Profile requires {key}='{want}', machine is configured as '{have}'.", p.summary())

        value = values.get(p.quantity)
        if dq_flag != "OK":
            return SeverityResult("UNAVAILABLE", None, value, p.quantity,
                                  f"Assessment unavailable: sensor data quality is {dq_flag}.", p.summary())
        if value is None:
            return SeverityResult("NO_DATA", None, None, p.quantity,
                                  f"No {p.quantity} measurement available for this machine.", p.summary())

        if p.band_hz:
            if not measurement_band_hz:
                return SeverityResult("UNAVAILABLE", None, value, p.quantity,
                                      f"Assessment unavailable: measurement band unknown; profile requires {p.band_hz[0]:g}-{p.band_hz[1]:g} Hz.",
                                      p.summary())
            if abs(measurement_band_hz[0] - p.band_hz[0]) > 1e-6 or abs(measurement_band_hz[1] - p.band_hz[1]) > 1e-6:
                return SeverityResult("NOT_APPLICABLE", None, value, p.quantity,
                                      f"Measured band {measurement_band_hz[0]:g}-{measurement_band_hz[1]:g} Hz differs from profile band "
                                      f"{p.band_hz[0]:g}-{p.band_hz[1]:g} Hz.", p.summary())
        if p.rpm_range:
            if rpm is None:
                return SeverityResult("UNAVAILABLE", None, value, p.quantity,
                                      "Assessment unavailable: operating speed unknown; profile has a speed range.", p.summary())
            if not p.rpm_range[0] <= rpm <= p.rpm_range[1]:
                return SeverityResult("NOT_APPLICABLE", None, value, p.quantity,
                                      f"Speed {rpm:g} rpm outside profile range {p.rpm_range[0]:g}-{p.rpm_range[1]:g} rpm.", p.summary())
        if p.power_kw_range:
            if power_kw_rated is None or not p.power_kw_range[0] <= power_kw_rated <= p.power_kw_range[1]:
                return SeverityResult("NOT_APPLICABLE", None, value, p.quantity,
                                      f"Rated power {power_kw_rated} kW outside profile range {p.power_kw_range}.", p.summary())
        if operating_state is not None and operating_state not in STEADY_STATES:
            return SeverityResult("NOT_APPLICABLE", None, value, p.quantity,
                                  f"Machine state {operating_state}: severity is evaluated in steady operation only.", p.summary())

        zone, threshold, fact = _zone_and_threshold(value, p.zone_boundaries, QUANTITY_UNIT.get(p.quantity, ""))
        if p.verified:
            return SeverityResult("ASSESSED", zone, value, p.quantity,
                                  f"Zone {zone} ({fact}) per {p.standard} ({p.edition or 'edition n/a'}), {p.machine_group}.",
                                  p.summary(), threshold)
        caveat = p.label or f"thresholds in profile '{p.profile_id}' are not engineering-verified"
        return SeverityResult("PROVISIONAL", zone, value, p.quantity, f"Zone {zone} ({fact}). {caveat}.",
                              p.summary(), threshold)
