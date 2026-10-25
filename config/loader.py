"""Load and validate config/machines.yaml: the single home for machine classes and thresholds.

    from config import get_config
    cfg = get_config()
    cfg.thresholds("VMC-01", "cnc_mill")["process_temp_limit_k"]   # 315.5
    cfg.vibration_profile("VMC-01", "cnc_mill")["zone_boundaries"]  # {"A_B": 1.4, ...}

A machine resolves to its template's defaults merged with any `machines.<id>` overrides, so
machines added at runtime inherit their template. Any problem raises ConfigError at load time.
"""

from __future__ import annotations

import copy
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(os.environ.get("UDYAM_CONFIG", Path(__file__).with_name("machines.yaml")))

ZONE_KEYS = ("A_B", "B_C", "C_D")
SUPPORTED_QUANTITIES = {"velocity_rms_mm_s", "disp_pp_um", "accel_rms_m_s2"}
REQUIRED_THRESHOLDS = {
    "cnc_mill": ("process_temp_limit_k", "tool_wear_warn_min", "tool_wear_forecast_min", "tool_wear_limit_min",
                 "vibration_trip_mm_s", "cooling_loss_alert_pct", "cooling_loss_rearm_pct",
                 "calibration_event_min_delta", "calibration_event_min_interval_s", "overload_load_pct",
                 "overtemp_derate_horizon_min",
                 "alert_sustain_s"),
    "cnc_lathe": ("process_temp_limit_k", "tool_wear_warn_min", "tool_wear_forecast_min", "tool_wear_limit_min",
                  "vibration_trip_mm_s", "cooling_loss_alert_pct", "cooling_loss_rearm_pct",
                  "calibration_event_min_delta", "calibration_event_min_interval_s", "overload_load_pct",
                 "overtemp_derate_horizon_min",
                 "alert_sustain_s"),
    "bar_saw": ("blade_wear_medium_pct", "blade_wear_high_pct", "blade_wear_trip_pct", "vibration_trip_mm_s"),
    "air_compressor": ("pressure_min_bar", "pressure_warn_bar"),
}
# Thresholds that must be strictly increasing within a template.
ORDERED_THRESHOLDS = (
    ("tool_wear_warn_min", "tool_wear_forecast_min", "tool_wear_limit_min"),
    ("blade_wear_medium_pct", "blade_wear_high_pct", "blade_wear_trip_pct"),
    ("pressure_min_bar", "pressure_warn_bar"),
    ("cooling_loss_rearm_pct", "cooling_loss_alert_pct"),
)
SITE_KEYS = ("grid_co2_kg_per_kwh", "tariff_inr_per_kwh", "inr_per_part", "downtime_cost_inr_per_hr",
             "sensor_node_cost_inr", "operating_hours_per_month", "baseline_unplanned_downtime_h_per_month",
             "assumed_downtime_reduction_pct", "sensor_nodes")


class ConfigError(ValueError):
    pass


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def _band_ok(b: Any) -> bool:
    return isinstance(b, list) and len(b) == 2 and all(_num(x) for x in b) and 0 <= b[0] < b[1]


class MachineConfig:
    def __init__(self, raw: dict[str, Any], source: str = "<dict>"):
        if not isinstance(raw, dict):
            raise ConfigError(f"{source}: top level must be a mapping")
        self.source = source
        self.site: dict[str, Any] = raw.get("site") or {}
        self.profiles: dict[str, dict[str, Any]] = raw.get("vibration_profiles") or {}
        self.templates: dict[str, dict[str, Any]] = raw.get("templates") or {}
        self.machines: dict[str, dict[str, Any]] = raw.get("machines") or {}
        self.runtime: dict[str, str | None] = {}  # machines added at runtime → their custom profile id
        errors = self._validate()
        if errors:
            raise ConfigError(f"{source}: " + "; ".join(errors))

    # ------------------------------------------------------------------ validation
    def _validate(self) -> list[str]:
        errs: list[str] = []
        for k in SITE_KEYS:
            if not _num(self.site.get(k)) or self.site[k] <= 0:
                errs.append(f"site.{k} must be a positive number")

        for pid, p in self.profiles.items():
            where = f"vibration_profiles.{pid}"
            if p.get("quantity") not in SUPPORTED_QUANTITIES:
                errs.append(f"{where}.quantity must be one of {sorted(SUPPORTED_QUANTITIES)}")
            if not p.get("standard"):
                errs.append(f"{where}.standard is required")
            if not _band_ok(p.get("band_hz")):
                errs.append(f"{where}.band_hz must be [low, high] with 0 <= low < high")
            zb = p.get("zone_boundaries") or {}
            vals = [zb.get(k) for k in ZONE_KEYS]
            if not all(_num(v) for v in vals):
                errs.append(f"{where}.zone_boundaries needs numeric A_B, B_C, C_D")
            elif not 0 < vals[0] < vals[1] < vals[2]:
                errs.append(f"{where}.zone_boundaries must be positive and strictly increasing")

        if not self.templates:
            errs.append("templates: at least one template is required")
        for tpl, t in self.templates.items():
            errs += self._validate_resolved(f"templates.{tpl}", tpl, t)
        for mid, m in self.machines.items():
            tpl = (m or {}).get("template")
            if tpl not in self.templates:
                errs.append(f"machines.{mid}.template '{tpl}' is not a defined template")
                continue
            errs += self._validate_resolved(f"machines.{mid}", tpl, _merge(self.templates[tpl], m))
        return errs

    def _validate_resolved(self, where: str, tpl: str, t: dict[str, Any]) -> list[str]:
        errs: list[str] = []
        if not t.get("prefix"):
            errs.append(f"{where}.prefix is required")
        rated = (t.get("nameplate") or {}).get("rated_kw")
        if not _num(rated) or rated <= 0:
            errs.append(f"{where}.nameplate.rated_kw must be a positive number")
        nominal = (t.get("nameplate") or {}).get("rpm_nominal")
        if tpl in ("cnc_mill", "cnc_lathe", "bar_saw") and (not _num(nominal) or nominal <= 0):
            errs.append(f"{where}.nameplate.rpm_nominal must be a positive number")
        ml = t.get("ml") or {}
        if ml.get("failure_model") not in (None, "ai4i"):
            errs.append(f"{where}.ml.failure_model must be 'ai4i' or null")
        if not isinstance(ml.get("bearing_model", False), bool):
            errs.append(f"{where}.ml.bearing_model must be true or false")
        vib = t.get("vibration") or {}
        pid = vib.get("profile")
        if pid is not None:
            prof = self.profiles.get(pid)
            if prof is None:
                errs.append(f"{where}.vibration.profile '{pid}' is not defined")
            else:
                for key in ("measurement_location", "mounting"):
                    have = vib.get("foundation") if key == "mounting" else vib.get(key)
                    if prof.get(key) and prof[key] != have:
                        errs.append(f"{where}.vibration: profile '{pid}' requires {key}='{prof[key]}', machine has '{have}'")
        if vib.get("stream_band_hz") is not None and not _band_ok(vib["stream_band_hz"]):
            errs.append(f"{where}.vibration.stream_band_hz must be [low, high] or null")
        th = t.get("thresholds") or {}
        for k in REQUIRED_THRESHOLDS.get(tpl, ()):
            if not _num(th.get(k)) or th[k] <= 0:
                errs.append(f"{where}.thresholds.{k} must be a positive number")
        for chain in ORDERED_THRESHOLDS:
            vals = [th.get(k) for k in chain if k in th]
            if len(vals) == len(chain) and all(_num(v) for v in vals) and not all(a < b for a, b in zip(vals, vals[1:])):
                errs.append(f"{where}.thresholds: {' < '.join(chain)} must hold")
        if _num(th.get("cooling_loss_alert_pct")) and not 0 < th["cooling_loss_alert_pct"] < 100:
            errs.append(f"{where}.thresholds.cooling_loss_alert_pct must be between 0 and 100")
        return errs

    # ------------------------------------------------------------------ runtime machines
    def register_machine(self, machine_id: str, spec: dict[str, Any], profile: dict[str, Any] | None = None) -> None:
        """Add a machine at runtime ("Add Your Machine"), validated exactly like machines.yaml.

        `spec` is a machines.<id> entry (template + overrides); `profile` an optional custom vibration
        profile (with `profile_id`) that the spec may reference. Raises ConfigError and changes nothing
        if the result would be invalid.
        """
        if machine_id in self.machines:
            raise ConfigError(f"machine '{machine_id}' already exists")
        unknown = set((spec.get("thresholds") or {})) - set((self.templates.get(spec.get("template")) or {}).get("thresholds") or {})
        if unknown:
            raise ConfigError(f"unknown thresholds for {spec.get('template')}: {sorted(unknown)}")
        profiles = dict(self.profiles)
        if profile is not None:
            profiles[profile["profile_id"]] = {k: v for k, v in profile.items() if k != "profile_id"}
        MachineConfig({"site": self.site, "vibration_profiles": profiles, "templates": self.templates,
                       "machines": {**self.machines, machine_id: spec}}, source=f"new machine {machine_id}")
        self.profiles = profiles
        self.machines = {**self.machines, machine_id: copy.deepcopy(spec)}
        self.runtime[machine_id] = profile["profile_id"] if profile else None

    def unregister_machine(self, machine_id: str) -> None:
        if machine_id not in self.runtime:
            raise ConfigError(f"'{machine_id}' is not a runtime machine")
        pid = self.runtime.pop(machine_id)
        self.machines = {k: v for k, v in self.machines.items() if k != machine_id}
        if pid:
            self.profiles = {k: v for k, v in self.profiles.items() if k != pid}

    # ------------------------------------------------------------------ lookups
    def template(self, template: str) -> dict[str, Any]:
        if template not in self.templates:
            raise ConfigError(f"unknown template '{template}'")
        return self.templates[template]

    def machine(self, machine_id: str, template: str | None = None) -> dict[str, Any]:
        """Resolved config for a machine: template defaults + per-machine overrides."""
        over = self.machines.get(machine_id) or {}
        tpl = over.get("template") or template
        if tpl is None:
            raise ConfigError(f"machine '{machine_id}' is not configured and no template was given")
        return _merge(self.template(tpl), {**over, "template": tpl})

    def thresholds(self, machine_id: str, template: str | None = None) -> dict[str, float]:
        return dict(self.machine(machine_id, template).get("thresholds") or {})

    def nameplate(self, template: str) -> dict[str, Any]:
        return dict(self.template(template).get("nameplate") or {})

    def prefixes(self) -> dict[str, str]:
        return {tpl: t["prefix"] for tpl, t in self.templates.items()}

    def vibration_profile(self, machine_id: str, template: str | None = None) -> dict[str, Any] | None:
        pid = (self.machine(machine_id, template).get("vibration") or {}).get("profile")
        return {"profile_id": pid, **self.profiles[pid]} if pid else None

    def risk_zones(self, machine_id: str, template: str | None = None) -> tuple[float, float] | None:
        """(medium, high) vibration risk boundaries: zone C starts at B/C, zone D at C/D."""
        p = self.vibration_profile(machine_id, template)
        if not p or p.get("quantity") != "velocity_rms_mm_s":
            return None
        return p["zone_boundaries"]["B_C"], p["zone_boundaries"]["C_D"]

    def ml(self, machine_id: str, template: str | None = None) -> dict[str, Any]:
        """{"failure_model": "ai4i" | None, "bearing_model": bool} for this machine."""
        ml = self.machine(machine_id, template).get("ml") or {}
        return {"failure_model": ml.get("failure_model"), "bearing_model": bool(ml.get("bearing_model", False))}

    def stream_band_hz(self, machine_id: str, template: str | None = None) -> list[float] | None:
        band = (self.machine(machine_id, template).get("vibration") or {}).get("stream_band_hz")
        return list(band) if band else None

    def severity_config(self) -> dict[str, Any]:
        """Profiles and assignments in the shape ml.vibration.severity.SeverityAssessor expects."""
        profiles = []
        for pid, p in self.profiles.items():
            label = p.get("label")
            profiles.append({
                "profile_id": pid,
                "standard": p["standard"],
                "edition": p.get("edition"),
                "machine_group": p.get("machine_group", ""),
                "quantity": p["quantity"],
                "zone_boundaries": dict(p["zone_boundaries"]),
                "band_hz": list(p["band_hz"]),
                "measurement_location": p.get("measurement_location"),
                "mounting": p.get("mounting"),
                "label": label,
                "verification": {"verified": bool(p.get("verified")), "verified_by": p.get("verified_by"),
                                 "source_reference": p.get("source_reference")},
                "notes": p.get("notes"),
            })

        def assignment(resolved: dict[str, Any]) -> dict[str, Any] | None:
            vib = resolved.get("vibration") or {}
            if not vib.get("profile"):
                return None
            return {"profile_id": vib["profile"], "machine_class": resolved.get("machine_class"),
                    "measurement_location": vib.get("measurement_location"), "mounting": vib.get("foundation"),
                    "sensor_axis": vib.get("sensor_axis")}

        machines = {mid: a for mid in self.machines if (a := assignment(self.machine(mid)))}
        templates = {tpl: a for tpl in self.templates if (a := assignment(self.templates[tpl]))}
        return {"threshold_profiles": profiles, "machine_assignments": machines, "template_assignments": templates}

    def public(self) -> dict[str, Any]:
        """Everything the UI needs to stop hardcoding thresholds (served at /api/config)."""
        return {
            "source": Path(self.source).name,
            "site": dict(self.site),
            "vibration_profiles": copy.deepcopy(self.profiles),
            "templates": copy.deepcopy(self.templates),
            "machines": {mid: self.machine(mid) for mid in self.machines},
            "runtime_machines": sorted(self.runtime),
        }


def load_config(path: Path | str | None = None) -> MachineConfig:
    p = Path(path) if path else CONFIG_PATH
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ConfigError(f"config file not found: {p}") from err
    except yaml.YAMLError as err:
        raise ConfigError(f"{p}: invalid YAML: {err}") from err
    return MachineConfig(raw or {}, source=str(p))


@lru_cache(maxsize=1)
def get_config() -> MachineConfig:
    """Process-wide config, loaded once."""
    return load_config()
