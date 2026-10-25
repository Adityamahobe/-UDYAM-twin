"""Per-machine vibration health: joins the stream, waveform uploads, severity and ML.

Two kinds of input, kept apart:
- Scalar `vibration_rms_mm_s` from each `observed` frame. Its provenance follows the
  frame's `source_mode` (SIM → simulated, LIVE → measured by a device, REPLAY → replayed).
  For SIM/REPLAY the band is the machine's `stream_band_hz` from config/machines.yaml
  (the simulator generates velocity RMS over that band by definition). A LIVE device's
  band is not declared, so band-conditioned severity is unavailable for LIVE values.
- Waveforms uploaded by a device (IoT hub) or from a recording. Features are computed
  here with a known band, so they carry their own method and band.

Timestamps: `stream_ts` is the twin engine's clock (as in the WebSocket stream);
`received_at` is the server's UTC wall clock when the data arrived.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from ml.vibration.features import DEFAULT_BAND_HZ, extract_features
from ml.vibration.inference import VibrationPredictor
from ml.vibration.severity import SeverityAssessor

TREND_POINTS = 360          # 3 min at the 500 ms tick
WAVEFORM_FRESH_S = 120.0    # a waveform older than this no longer represents the machine
PROVENANCE = {"SIM": "simulated", "LIVE": "measured", "REPLAY": "replayed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class VibrationMonitor:
    def __init__(
        self,
        assessor: SeverityAssessor | None = None,
        predictor: VibrationPredictor | None = None,
        machine_info: Callable[[str], dict[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        band_hz: tuple[float, float] = DEFAULT_BAND_HZ,
    ):
        """`machine_info(mid)` returns {"template", "name", "rated_kw", "stream_band_hz", "bearing_model"}
        or None if unknown. The CWRU bearing classifier only runs where `bearing_model` is true."""
        self.assessor = assessor or SeverityAssessor()
        self.predictor = predictor or VibrationPredictor()
        self.machine_info = machine_info or (lambda _mid: None)
        self.clock = clock
        self.band_hz = band_hz
        self._lock = threading.RLock()
        self._obs: dict[str, dict[str, Any]] = {}
        self._trend: dict[str, deque] = {}
        self._wave: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ inputs
    def observe(self, msgs: list[dict[str, Any]]) -> None:
        now_iso = _utc_now()
        with self._lock:
            for m in msgs:
                if m.get("type") == "event" and m.get("kind") == "RESET":
                    # Clean baseline: drop trends and waveforms so charts start empty.
                    self._trend.clear()
                    self._wave.clear()
                    continue
                if m.get("type") == "registry":
                    live = {e["machine_id"] for e in m.get("machines", [])}
                    for store in (self._obs, self._trend, self._wave):
                        for mid in [k for k in store if k not in live]:
                            del store[mid]
                    continue
                if m.get("type") != "observed":
                    continue
                mid = m["machine_id"]
                self._obs[mid] = {**m, "_received_at": now_iso}
                v = m.get("vibration_rms_mm_s")
                self._trend.setdefault(mid, deque(maxlen=TREND_POINTS)).append(
                    [m["ts"], v, m.get("source_mode"), m.get("dq_flag")]
                )

    def ingest_waveform(
        self,
        machine_id: str,
        payload: dict[str, Any],
        *,
        provenance: str,
        source: str,
    ) -> dict[str, Any]:
        """Extract features from an uploaded waveform. `provenance`: measured | simulated | recorded."""
        obs = self._obs.get(machine_id, {})
        info = self.machine_info(machine_id) or {}
        rpm = payload.get("rpm", obs.get("rpm"))
        fx = extract_features(
            payload.get("samples") or [],
            payload.get("sampling_rate_hz"),
            str(payload.get("unit", "")),
            band_hz=self.band_hz,
            rpm=rpm if isinstance(rpm, (int, float)) else None,
            load_pct=self._load_pct(obs, info),
            sensor_range=payload.get("sensor_range"),
        )
        rec = {
            "provenance": provenance,
            "source": source,
            "axis": payload.get("axis"),
            "measurement_location": payload.get("location"),
            "device_ts": payload.get("ts"),
            "received_at": _utc_now(),
            "_t": self.clock(),
            **fx.as_dict(),
        }
        with self._lock:
            if fx.valid:
                self._wave[machine_id] = rec
        return {k: v for k, v in rec.items() if not k.startswith("_")}

    # ------------------------------------------------------------------ outputs
    @staticmethod
    def _load_pct(obs: dict[str, Any], info: dict[str, Any]) -> float | None:
        p, rated = obs.get("power_kw"), info.get("rated_kw")
        if isinstance(p, (int, float)) and isinstance(rated, (int, float)) and rated > 0:
            return round(100.0 * p / rated, 1)
        return None

    def snapshot(self, machine_id: str, trend: bool = True) -> dict[str, Any] | None:
        with self._lock:
            info = self.machine_info(machine_id)
            if info is None:
                return None
            obs = dict(self._obs.get(machine_id, {}))
            wave = self._wave.get(machine_id)
            if wave and self.clock() - wave["_t"] > WAVEFORM_FRESH_S:
                wave = {**wave, "stale": True}
            pts = list(self._trend.get(machine_id, []))

        source_mode = obs.get("source_mode")
        dq = obs.get("dq_flag", "MISSING") if obs else "MISSING"
        v = obs.get("vibration_rms_mm_s")
        warnings: list[str] = []
        if not obs:
            warnings.append("No telemetry received for this machine yet.")
        elif dq != "OK":
            warnings.append(f"Stream data quality: {dq}.")
        if obs and v is None:
            warnings.append("Vibration velocity not reported.")
        if source_mode == "LIVE":
            warnings.append("Device-reported velocity: frequency band and derivation are not declared by the device.")
        stream_band = info.get("stream_band_hz") if source_mode in ("SIM", "REPLAY") else None

        stream_velocity = {
            "value": v,
            "unit": "mm/s",
            "quantity": "velocity_rms",
            "provenance": PROVENANCE.get(source_mode, "unknown") if obs else None,
            "source_mode": source_mode,
            "method": {"SIM": "simulator output (plant model or simulated IoT device)", "LIVE": "reported by IoT device",
                       "REPLAY": "replayed recording"}.get(source_mode),
            "band_hz": stream_band,
            "stream_ts": obs.get("ts"),
            "received_at": obs.get("_received_at"),
        }

        wave_out = None
        wave_usable = wave is not None and not wave.get("stale")
        if wave is not None:
            wave_out = {k: val for k, val in wave.items() if not k.startswith("_")}
            if wave.get("stale"):
                warnings.append(f"Last waveform is older than {WAVEFORM_FRESH_S:.0f} s.")
            for issue in wave.get("issues", []):
                warnings.append(f"Waveform: {issue['message']}")

        # Severity: prefer band-known waveform values, else the stream scalar (band unknown).
        if wave_usable:
            values = {k: wave["features"].get(k) for k in ("velocity_rms_mm_s", "disp_pp_um", "accel_rms_m_s2")}
            band = wave.get("effective_band_hz")
            sev_basis = f"waveform ({wave['provenance']})"
        else:
            values = {"velocity_rms_mm_s": v}
            band = stream_band
            sev_basis = f"stream scalar ({stream_velocity['provenance']})" if obs else None
        severity = self.assessor.assess(
            machine_id, values,
            measurement_band_hz=band,
            rpm=obs.get("rpm") if isinstance(obs.get("rpm"), (int, float)) else None,
            power_kw_rated=info.get("rated_kw"),
            operating_state=obs.get("plc_state"),
            dq_flag=dq if not wave_usable else "OK",
            template=info.get("template"),
        ).as_dict()
        if v is None and not wave_usable and severity["status"] == "UNAVAILABLE" and not severity.get("profile"):
            # Same wording as the snapshot (twin/snapshot.py): an asset without a vibration sensor.
            severity.update(status="NOT_APPLICABLE", reason="No vibration sensor on this asset.")
            sev_basis = None
        severity["basis"] = sev_basis
        severity["basis_provenance"] = wave["provenance"] if wave_usable else stream_velocity["provenance"]

        if not info.get("bearing_model", False):
            ml = {
                "status": "NOT_APPLICABLE",
                "detail": f"The CWRU bearing-fault classifier applies only to machines configured as bearing/motor "
                          f"assets (config/machines.yaml `ml.bearing_model`); {machine_id} is a {info.get('template')}.",
            }
        elif wave_usable:
            ml = self.predictor.predict(
                wave["features"],
                context={"sampling_rate_hz": wave.get("sampling_rate_hz"), "machine_type": info.get("template")},
            )
            ml["basis_provenance"] = wave["provenance"]
        else:
            ml = self.predictor.predict({})
            if ml["status"] == "INSUFFICIENT_FEATURES":
                ml["detail"] = "No recent waveform; the model needs waveform features, not the scalar stream value."
        out = {
            "machine_id": machine_id,
            "name": info.get("name"),
            "template": info.get("template"),
            "operating": {
                "plc_state": obs.get("plc_state"),
                "rpm": obs.get("rpm"),
                "power_kw": obs.get("power_kw"),
                "rated_kw": info.get("rated_kw"),
                "load_pct": self._load_pct(obs, info),
                "load_definition": "power_kw / nameplate rated_kw",
            },
            "stream_velocity": stream_velocity,
            "waveform": wave_out,
            "severity": severity,
            "ml": ml,
            "data_quality": {"dq_flag": dq, "warnings": warnings},
        }
        if trend:
            out["trend"] = {"unit": "mm/s", "points": [[ts, val, sm, q] for ts, val, sm, q in pts],
                            "columns": ["stream_ts", "velocity_rms_mm_s", "source_mode", "dq_flag"]}
        return out
