"""One `snapshot` per machine per tick: the single source of truth for every panel.

The engine builds it from the same `observed` frame (after any LIVE overlay) and `twin`
message it broadcasts, plus severity from the machine's configured vibration profile.
Cards, charts, the 3D twin, alerts and the operator view should all render from this
message, so the same quantity always shows the same number.
"""

from __future__ import annotations

from typing import Any

from ml.vibration.severity import SeverityAssessor

SNAPSHOT_SCHEMA_VERSION = "1.1"
K_TO_C = 273.15
SOURCE_LABEL = {
    "SIM": "Simulated (SIM)",
    "REPLAY": "Replayed dataset (REPLAY)",
    "LIVE": "Live device (LIVE)",
}
UNITS = {
    "rpm": "rpm",
    "vibration_rms_mm_s": "mm/s RMS",
    "temp_c": "°C",
    "load_pct": "% of rated kW",
    "current_a": "A",
    "voltage_v": "V",
    "power_kw": "kW",
    "energy_kwh": "kWh",
    "derate_pct": "%",
    "cooling_loss_pct": "% below nominal",
}
# Template-specific readings carried in `signals`, with units.
SIGNALS = {
    "cnc_mill": {"torque_nm": "N·m", "tool_wear_min": "min", "air_temp_k": "K", "process_temp_k": "K"},
    "cnc_lathe": {"torque_nm": "N·m", "tool_wear_min": "min", "air_temp_k": "K", "process_temp_k": "K"},
    "bar_saw": {"blade_wear_pct": "%"},
    "air_compressor": {"pressure_bar": "bar", "duty_pct": "%", "discharge_temp_c": "°C"},
}
TEMP_FIELDS = (("discharge_temp_c", "discharge_temp"),)


def _temp_c(obs: dict[str, Any]) -> tuple[float | None, str | None]:
    """Machine temperature in °C and which signal it is."""
    if obs.get("process_temp_k") is not None:
        return round(obs["process_temp_k"] - K_TO_C, 2), "process_temp"
    for key, name in TEMP_FIELDS:
        if obs.get(key) is not None:
            return round(obs[key], 2), name
    return None, None


def build_snapshot(
    obs: dict[str, Any],
    twin: dict[str, Any],
    *,
    name: str,
    rated_kw: float | None,
    energy_kwh: float,
    stream_band_hz: list[float] | None,
    assessor: SeverityAssessor,
    clock: str,
    active_scenario: str | None = None,
) -> dict[str, Any]:
    mode = obs.get("source_mode", "SIM")
    power = obs.get("power_kw")
    load_pct = round(100.0 * power / rated_kw, 1) if isinstance(power, (int, float)) and rated_kw else None
    temp_c, temp_signal = _temp_c(obs)
    vib = obs.get("vibration_rms_mm_s")

    # Same assessment the Vibration panel makes for the stream value: band declared only for SIM/REPLAY.
    sev = assessor.assess(
        obs["machine_id"],
        {"velocity_rms_mm_s": vib},
        measurement_band_hz=stream_band_hz if mode in ("SIM", "REPLAY") else None,
        rpm=obs.get("rpm") if isinstance(obs.get("rpm"), (int, float)) else None,
        power_kw_rated=rated_kw,
        operating_state=obs.get("plc_state"),
        dq_flag=obs.get("dq_flag", "OK"),
        template=obs.get("template"),
    )
    profile = sev.profile or {}
    severity = {
        "status": sev.status,
        "zone": sev.zone,
        "threshold": sev.threshold,
        "reason": sev.reason,
        "label": profile.get("label"),
        "profile_id": profile.get("profile_id"),
        "basis": "stream",
    }
    if vib is None and sev.status == "UNAVAILABLE" and not profile:
        severity.update(status="NOT_APPLICABLE", reason="No vibration sensor on this asset.", basis=None)

    params = twin.get("params") or {}
    h_cool = (params.get("values") or {}).get("h_cool")
    calibration = {
        "version": params.get("version"),
        "frozen": params.get("frozen", False),
        "freeze_reason": params.get("freeze_reason"),
        "h_cool": h_cool,
        "cooling_loss_pct": round(100.0 * (1.0 - h_cool), 1) if isinstance(h_cool, (int, float)) else None,
    }

    return {
        "type": "snapshot",
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "ts": obs["ts"],
        "clock": clock,
        "machine_id": obs["machine_id"],
        "template": obs.get("template"),
        "name": name,
        "mode": mode,
        "source_label": SOURCE_LABEL.get(mode, mode),
        "state": obs.get("plc_state"),
        "dq_flag": obs.get("dq_flag", "OK"),
        "active_scenario": active_scenario or obs.get("active_fault"),
        "rpm": obs.get("rpm"),
        "rpm_setpoint": obs.get("rpm_setpoint"),
        "rpm_nominal": obs.get("rpm_nominal"),
        "derate_pct": obs.get("derate_pct", 0.0),
        "vibration_rms_mm_s": vib,
        "temp_c": temp_c,
        "temp_signal": temp_signal,
        "load_pct": load_pct,
        "current_a": obs.get("current_a"),
        "voltage_v": obs.get("voltage_v"),
        "power_kw": power,
        "energy_kwh": round(energy_kwh, 4),
        "severity": severity,
        "signals": {k: obs.get(k) for k in SIGNALS.get(obs.get("template"), {})},
        "signal_units": SIGNALS.get(obs.get("template"), {}),
        "risk": twin.get("risk"),
        "forecast": twin.get("forecast"),
        "rul": twin.get("rul"),
        "calibration": calibration,
        "replay": obs.get("replay"),
        "units": UNITS,
    }
