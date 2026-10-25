"""Vibration waveform validation and feature extraction.

Input is a uniformly sampled time series from a vibration sensor (acceleration,
velocity or displacement) plus its sampling rate and unit. Nothing here is computed
from AI4I columns: AI4I has no vibration signal.

Conventions
- Acceleration is converted to m/s² (1 g = 9.80665 m/s²), velocity to mm/s, displacement to µm.
- The mean is removed before any feature (accelerometers carry gravity / DC offset).
- Frequency-domain features (band velocity RMS, dominant frequency, displacement p-p)
  are only computed when the record is long enough and sampled fast enough for the
  requested band; otherwise they are None and an issue explains why.
- Band velocity RMS comes from frequency-domain integration of the unwindowed spectrum
  (Parseval), restricted to `band_hz`. The default band is a configurable placeholder:
  confirm the band required by the standard and machine class you assess against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

G_M_S2 = 9.80665

# unit → (quantity, factor to canonical unit)
UNITS = {
    "m/s2": ("acceleration", 1.0),
    "m/s^2": ("acceleration", 1.0),
    "g": ("acceleration", G_M_S2),
    "mm/s": ("velocity", 1.0),
    "m/s": ("velocity", 1000.0),
    "um": ("displacement", 1.0),
    "µm": ("displacement", 1.0),
    "mm": ("displacement", 1000.0),
}

DEFAULT_BAND_HZ = (10.0, 1000.0)
# Sample rate must be at least this multiple of the band's upper edge (anti-alias margin
# conventionally used by vibration analysers; plain Nyquist would be 2.0).
SAMPLING_MARGIN = 2.56
MIN_SAMPLES = 64
MAX_SAMPLES = 1 << 18
MIN_CYCLES_LOW_EDGE = 4  # record must span this many periods of the band's lower edge
CLIP_FRACTION = 0.001

# Features the ML pipeline may use. Identifiers, timestamps and labels are never features.
FEATURE_COLUMNS = [
    "velocity_rms_mm_s",
    "accel_rms_m_s2",
    "accel_peak_m_s2",
    "crest_factor",
    "kurtosis",
    "dominant_freq_hz",
    "dominant_order",
    "disp_pp_um",
    "rpm",
    "load_pct",
]


@dataclass
class Issue:
    code: str
    severity: str  # "error" (feature/record unusable) or "warning"
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass
class VibrationFeatures:
    features: dict[str, float | None]
    quantity: str | None
    sampling_rate_hz: float | None
    n_samples: int
    duration_s: float | None
    band_hz: tuple[float, float]
    effective_band_hz: tuple[float, float] | None
    methods: dict[str, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "features": self.features,
            "quantity": self.quantity,
            "sampling_rate_hz": self.sampling_rate_hz,
            "n_samples": self.n_samples,
            "duration_s": self.duration_s,
            "band_hz": list(self.band_hz),
            "effective_band_hz": list(self.effective_band_hz) if self.effective_band_hz else None,
            "methods": self.methods,
            "issues": [i.as_dict() for i in self.issues],
        }


def _empty(band: tuple[float, float], issues: list[Issue], quantity: str | None = None,
           fs: float | None = None, n: int = 0) -> VibrationFeatures:
    return VibrationFeatures(
        features={k: None for k in FEATURE_COLUMNS},
        quantity=quantity,
        sampling_rate_hz=fs,
        n_samples=n,
        duration_s=(n / fs) if fs and n else None,
        band_hz=band,
        effective_band_hz=None,
        issues=issues,
    )


def _finite_positive(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) and f > 0 else None


def _band_rms(spectrum: np.ndarray, freqs: np.ndarray, n: int, mask: np.ndarray) -> float:
    """RMS of the band-limited signal from an unwindowed rfft (one-sided Parseval)."""
    power = np.abs(spectrum) ** 2
    weights = np.full(len(freqs), 2.0)
    weights[0] = 1.0
    if n % 2 == 0:
        weights[-1] = 1.0
    return float(np.sqrt(np.sum(power * weights * mask)) / n)


def extract_features(
    samples: Sequence[float] | np.ndarray,
    sampling_rate_hz: Any,
    unit: str,
    *,
    band_hz: tuple[float, float] = DEFAULT_BAND_HZ,
    rpm: float | None = None,
    load_pct: float | None = None,
    sensor_range: float | None = None,
) -> VibrationFeatures:
    """Validate a waveform and extract vibration features.

    `sensor_range` is the sensor's full-scale amplitude in `unit`, used to detect clipping.
    """
    lo, hi = float(band_hz[0]), float(band_hz[1])
    band = (lo, hi)
    issues: list[Issue] = []

    if not (np.isfinite(lo) and np.isfinite(hi) and 0 < lo < hi):
        return _empty(band, [Issue("INVALID_BAND", "error", f"Band {band_hz} must satisfy 0 < low < high")])

    unit_key = (unit or "").strip().lower().replace("²", "2")
    if unit_key not in UNITS:
        return _empty(band, [Issue("UNKNOWN_UNIT", "error", f"Unit '{unit}' not supported; use one of {sorted(set(UNITS))}")])
    quantity, factor = UNITS[unit_key]

    fs = _finite_positive(sampling_rate_hz)
    if fs is None:
        return _empty(band, [Issue("INVALID_SAMPLING_RATE", "error", f"Sampling rate {sampling_rate_hz!r} must be a positive number")], quantity)

    try:
        x = np.asarray(samples, dtype=float).ravel()
    except (TypeError, ValueError):
        return _empty(band, [Issue("NON_NUMERIC_SAMPLES", "error", "Samples must be numbers")], quantity, fs)
    n = int(x.size)
    if n < MIN_SAMPLES:
        return _empty(band, [Issue("TOO_FEW_SAMPLES", "error", f"{n} samples; at least {MIN_SAMPLES} required")], quantity, fs, n)
    if n > MAX_SAMPLES:
        return _empty(band, [Issue("TOO_MANY_SAMPLES", "error", f"{n} samples; at most {MAX_SAMPLES} accepted")], quantity, fs, n)
    bad = int(np.count_nonzero(~np.isfinite(x)))
    if bad:
        return _empty(band, [Issue("NON_FINITE_SAMPLES", "error", f"{bad} missing or non-finite samples; gaps are not interpolated")], quantity, fs, n)

    if sensor_range is not None and sensor_range > 0:
        clipped = np.count_nonzero(np.abs(x) >= 0.99 * sensor_range) / n
        if clipped > CLIP_FRACTION:
            issues.append(Issue("CLIPPED", "error", f"{clipped:.1%} of samples at sensor full scale; amplitudes are not trustworthy"))

    x = (x - x.mean()) * factor
    std = float(x.std())
    if std == 0.0:
        issues.append(Issue("FLAT_SIGNAL", "error", "Signal is constant after mean removal (stuck or disconnected sensor)"))
        out = _empty(band, issues, quantity, fs, n)
        return out

    feats: dict[str, float | None] = {k: None for k in FEATURE_COLUMNS}
    methods: dict[str, str] = {}
    feats["rpm"] = float(rpm) if rpm is not None and np.isfinite(rpm) and rpm >= 0 else None
    feats["load_pct"] = float(load_pct) if load_pct is not None and np.isfinite(load_pct) else None

    # ---- time domain (full bandwidth of the record, mean removed)
    rms = float(np.sqrt(np.mean(x ** 2)))
    peak = float(np.max(np.abs(x)))
    if quantity == "acceleration":
        feats["accel_rms_m_s2"] = round(rms, 5)
        feats["accel_peak_m_s2"] = round(peak, 5)
        feats["crest_factor"] = round(peak / rms, 4)
        feats["kurtosis"] = round(float(np.mean(x ** 4) / std ** 4), 4)
        for k in ("accel_rms_m_s2", "accel_peak_m_s2", "crest_factor", "kurtosis"):
            methods[k] = "time domain, mean removed, full record bandwidth"
    elif quantity == "velocity":
        feats["crest_factor"] = round(peak / rms, 4)
        feats["kurtosis"] = round(float(np.mean(x ** 4) / std ** 4), 4)
        methods["crest_factor"] = methods["kurtosis"] = "time domain on velocity signal"

    # ---- frequency domain
    eff_hi = min(hi, fs / SAMPLING_MARGIN)
    duration = n / fs
    eff_band: tuple[float, float] | None = None
    if eff_hi <= lo:
        issues.append(Issue(
            "INSUFFICIENT_SAMPLING_RATE", "warning",
            f"{fs:g} Hz cannot resolve the {lo:g}-{hi:g} Hz band (needs ≥ {SAMPLING_MARGIN}×{hi:g} Hz); frequency features skipped",
        ))
    elif duration < MIN_CYCLES_LOW_EDGE / lo:
        issues.append(Issue(
            "RECORD_TOO_SHORT", "warning",
            f"{duration:.3f} s record is shorter than {MIN_CYCLES_LOW_EDGE} periods of {lo:g} Hz; frequency features skipped",
        ))
    else:
        if eff_hi < hi:
            issues.append(Issue(
                "BAND_TRUNCATED", "warning",
                f"Band upper edge reduced from {hi:g} to {eff_hi:g} Hz by the {fs:g} Hz sampling rate",
            ))
        eff_band = (lo, round(eff_hi, 3))
        spec = np.fft.rfft(x)
        freqs = np.fft.rfftfreq(n, d=1.0 / fs)
        mask = ((freqs >= lo) & (freqs <= eff_hi)).astype(float)
        omega = 2.0 * np.pi * np.where(freqs > 0, freqs, 1.0)

        if quantity == "acceleration":
            vel_spec = spec / (1j * omega) * 1000.0  # m/s² → mm/s
            disp_spec = spec / (-(omega ** 2)) * 1e6  # m/s² → µm
        elif quantity == "velocity":
            vel_spec = spec
            disp_spec = spec / (1j * omega) * 1000.0  # mm/s → µm
        else:
            vel_spec = None
            disp_spec = spec

        if vel_spec is not None:
            feats["velocity_rms_mm_s"] = round(_band_rms(vel_spec, freqs, n, mask), 4)
            methods["velocity_rms_mm_s"] = (
                f"frequency-domain {'integration of acceleration' if quantity == 'acceleration' else 'band-limit of velocity'}, "
                f"{eff_band[0]:g}-{eff_band[1]:g} Hz"
            )
        disp = np.fft.irfft(disp_spec * mask, n=n)
        feats["disp_pp_um"] = round(float(disp.max() - disp.min()), 3)
        methods["disp_pp_um"] = f"peak-to-peak of band-limited {'double ' if quantity == 'acceleration' else ''}integration, {eff_band[0]:g}-{eff_band[1]:g} Hz"

        win_mag = np.abs(np.fft.rfft(x * np.hanning(n))) * mask
        if win_mag.max() > 0:
            dom = float(freqs[int(np.argmax(win_mag))])
            feats["dominant_freq_hz"] = round(dom, 3)
            methods["dominant_freq_hz"] = f"Hann-windowed spectrum peak in band, resolution {fs / n:.3g} Hz"
            if feats["rpm"]:
                feats["dominant_order"] = round(dom / (feats["rpm"] / 60.0), 3)
                methods["dominant_order"] = "dominant_freq_hz / (rpm / 60)"

    return VibrationFeatures(
        features=feats,
        quantity=quantity,
        sampling_rate_hz=fs,
        n_samples=n,
        duration_s=round(duration, 6),
        band_hz=band,
        effective_band_hz=eff_band,
        methods=methods,
        issues=issues,
    )
