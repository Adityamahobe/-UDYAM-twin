"""Raw device payload → schema v1.0 `observed` fields, with data-quality checks.

Devices send whatever is natural for the sensor (°C from a DS18B20, mm/s from an
MPU6050 pipeline, A from an ACS712). This module maps those keys onto the schema's
names and units for the bound machine's template.
"""

from __future__ import annotations

from typing import Any

# Accepted raw keys → canonical hub keys. Canonical keys are schema field names.
ALIASES = {
    "temp_c": "temp_c",
    "temperature_c": "temp_c",
    "temp_k": "temp_k",
    "process_temp_k": "temp_k",
    "air_temp_k": "air_temp_k",
    "air_temp_c": "air_temp_c",
    "vibration_rms_mm_s": "vibration_rms_mm_s",
    "vib_rms": "vibration_rms_mm_s",
    "current_a": "current_a",
    "power_kw": "power_kw",
    "rpm": "rpm",
    "pressure_bar": "pressure_bar",
    "discharge_temp_c": "discharge_temp_c",
}

# Physical plausibility windows (schema units). Outside → OUT_OF_RANGE.
RANGES = {
    "process_temp_k": (250.0, 450.0),
    "air_temp_k": (250.0, 350.0),
    "vibration_rms_mm_s": (0.0, 50.0),
    "current_a": (0.0, 200.0),
    "power_kw": (0.0, 100.0),
    "rpm": (0.0, 30000.0),
    "pressure_bar": (0.0, 16.0),
    "discharge_temp_c": (-20.0, 150.0),
}

# DS18B20 returns exactly 85.0 °C on a power-on/conversion error; treat as missing.
DS18B20_ERROR_C = 85.0

# Which schema fields a device may override, per machine template.
TEMPLATE_FIELDS = {
    "cnc_mill": {"process_temp_k", "air_temp_k", "vibration_rms_mm_s", "current_a", "power_kw", "rpm"},
    "cnc_lathe": {"process_temp_k", "air_temp_k", "vibration_rms_mm_s", "current_a", "power_kw", "rpm"},
    "bar_saw": {"vibration_rms_mm_s", "current_a", "power_kw", "rpm"},
    "air_compressor": {"vibration_rms_mm_s", "current_a", "power_kw", "pressure_bar", "discharge_temp_c"},
}


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def normalize(payload: dict[str, Any], template: str, temp_offset_k: float = 0.0) -> tuple[dict[str, float | None], str]:
    """Map a raw reading to schema fields for `template`.

    Returns (fields, dq_flag). `fields` only contains signals the device actually sent
    (a key present with a null/unparseable value maps to None → MISSING).
    """
    raw: dict[str, Any] = {}
    for k, v in payload.items():
        canon = ALIASES.get(k)
        if canon:
            raw[canon] = v

    dq = "OK"
    fields: dict[str, float | None] = {}

    # Temperature: the device's housing probe is the twin's modelled signal.
    temp_k: float | None = None
    has_temp = "temp_c" in raw or "temp_k" in raw
    if "temp_k" in raw:
        temp_k = _num(raw["temp_k"])
    elif "temp_c" in raw:
        c = _num(raw["temp_c"])
        if c is not None and c == DS18B20_ERROR_C:
            c = None
        temp_k = None if c is None else c + 273.15
    if has_temp:
        if template == "air_compressor":
            fields["discharge_temp_c"] = None if temp_k is None else round(temp_k - 273.15, 2)
        else:
            fields["process_temp_k"] = None if temp_k is None else round(temp_k + temp_offset_k, 2)

    if "air_temp_c" in raw and "air_temp_k" not in raw:
        c = _num(raw["air_temp_c"])
        fields["air_temp_k"] = None if c is None else round(c + 273.15, 2)

    for key in ("air_temp_k", "vibration_rms_mm_s", "current_a", "power_kw", "rpm", "pressure_bar", "discharge_temp_c"):
        if key in raw and key not in fields:
            v = _num(raw[key])
            fields[key] = None if v is None else round(v, 3)

    allowed = TEMPLATE_FIELDS.get(template, set())
    fields = {k: v for k, v in fields.items() if k in allowed}

    if any(v is None for v in fields.values()):
        dq = "MISSING"
    for k, v in fields.items():
        lo, hi = RANGES[k]
        if v is not None and not lo <= v <= hi:
            dq = "OUT_OF_RANGE"
            fields[k] = None
    return fields, dq
