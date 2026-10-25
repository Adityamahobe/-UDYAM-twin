"""Fake ESP32 node: registers with the IoT hub and pushes readings over HTTP.

Use it to exercise the hub without hardware:

    python -m tools.fake_device                       # binds ESP32-SIM to VMC-01
    python -m tools.fake_device --machine LATHE-01 --heat 3.0
    python -m tools.fake_device --token <token>       # reuse an existing device

The device registers as `simulated`, so its readings replace the plant simulator's but
stay labelled `source_mode: "SIM"`: they are not real sensor data.

`--heat` adds a slow temperature rise (K over 60 s), like holding a hand over the fan,
so the twin sees a real residual and recalibrates.

`--waveform-every N` also uploads a synthetic accelerometer waveform every N seconds
(shaft 1x + 2x + noise; `--bearing` adds periodic impacts), which exercises vibration
feature extraction. It is labelled simulated end to end.
"""

from __future__ import annotations

import argparse
import math
import random
import time

import httpx
import numpy as np

WAVE_FS_HZ = 2560.0
WAVE_N = 4096


def synthetic_waveform(rpm: float, bearing: bool, rng: np.random.Generator) -> list[float]:
    """Synthetic acceleration in m/s² (not a measurement): 1x/2x shaft tones, noise, optional impacts."""
    t = np.arange(WAVE_N) / WAVE_FS_HZ
    f1 = rpm / 60.0
    v_rms_1x = 2.0  # mm/s at 1x
    a1 = v_rms_1x * math.sqrt(2) / 1000.0 * 2 * math.pi * f1
    x = a1 * np.sin(2 * math.pi * f1 * t) + 0.4 * a1 * np.sin(2 * math.pi * 2 * f1 * t + 0.7)
    x += rng.normal(0, 0.05, WAVE_N)
    if bearing:
        period = int(WAVE_FS_HZ / (3.6 * f1))  # impacts at ~3.6x shaft speed
        ring = np.exp(-np.arange(64) / 8.0) * np.sin(2 * math.pi * 900.0 * np.arange(64) / WAVE_FS_HZ)
        for start in range(0, WAVE_N - 64, period):
            x[start:start + 64] += 3.0 * ring
    return np.round(x, 5).tolist()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--device", default="ESP32-SIM")
    ap.add_argument("--machine", default="VMC-01")
    ap.add_argument("--token", default=None, help="existing device token; otherwise the device is registered")
    ap.add_argument("--hz", type=float, default=2.0)
    ap.add_argument("--base-c", type=float, default=38.0, help="baseline housing temperature in °C")
    ap.add_argument("--heat", type=float, default=0.0, help="extra K reached after 60 s")
    ap.add_argument("--waveform-every", type=float, default=0.0, help="seconds between synthetic waveform uploads (0 = off)")
    ap.add_argument("--rpm", type=float, default=1500.0, help="shaft speed used for the synthetic waveform")
    ap.add_argument("--bearing", action="store_true", help="add bearing-like impacts to the synthetic waveform")
    args = ap.parse_args()

    with httpx.Client(base_url=args.url, timeout=5.0) as http:
        token = args.token
        if token is None:
            res = http.post("/api/iot/devices", json={"device_id": args.device, "machine_id": args.machine, "name": "Fake ESP32", "simulated": True})
            if res.status_code == 409:
                raise SystemExit(f"{args.device} already exists: pass --token, or delete it on {args.url}/iot")
            res.raise_for_status()
            token = res.json()["token"]
            print(f"registered {args.device} → {args.machine}")
        headers = {"X-Device-Id": args.device, "X-Device-Token": token}

        t0 = time.monotonic()
        sent = 0
        rng = np.random.default_rng(0)
        next_wave = 0.0
        while True:
            t = time.monotonic() - t0
            heat = args.heat * min(1.0, t / 60.0)
            reading = {
                "temp_c": round(args.base_c + heat + 0.3 * math.sin(t / 20.0) + random.gauss(0, 0.05), 4),
                "vibration_rms_mm_s": round(2.1 + random.gauss(0, 0.1), 3),
                "current_a": round(3.4 + random.gauss(0, 0.08), 3),
                "rssi": -55,
                "fw": "fake-0.1",
            }
            res = http.post("/api/iot/telemetry", json=reading, headers=headers)
            sent += 1
            if res.status_code != 200:
                print(f"rejected: {res.status_code} {res.text}")
            elif sent % 10 == 1:
                print(f"#{sent} {reading['temp_c']} °C → {res.json()['dq_flag']}")
            if args.waveform_every > 0 and t >= next_wave:
                next_wave = t + args.waveform_every
                wave = {"sampling_rate_hz": WAVE_FS_HZ, "unit": "m/s2", "axis": "radial",
                        "samples": synthetic_waveform(args.rpm, args.bearing, rng), "rpm": args.rpm}
                res = http.post("/api/iot/waveform", json=wave, headers=headers)
                if res.status_code != 200:
                    print(f"waveform rejected: {res.status_code} {res.text}")
                else:
                    f = res.json()["features"]
                    print(f"waveform → v_rms {f['velocity_rms_mm_s']} mm/s, kurtosis {f['kurtosis']}, dominant {f['dominant_freq_hz']} Hz")
            time.sleep(1.0 / args.hz)


if __name__ == "__main__":
    main()
