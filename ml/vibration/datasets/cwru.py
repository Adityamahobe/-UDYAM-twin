"""Convert CWRU Bearing Data Center recordings into the ml.vibration.train dataset layout.

Source: Case Western Reserve University Bearing Data Center,
https://engineering.case.edu/bearingdatacenter (pages read 24 Oct 2026).
2 hp Reliance Electric motor test stand; single-point EDM faults on the drive-end
bearing; accelerometers on the motor housing (12 o'clock, magnetic base).

What this script uses, and the decisions it makes (all recorded in dataset.json):
- Drive-end accelerometer channel (`X<nnn>_DE_time`), selected by the file's own number:
  some files (e.g. 99.mat) also contain another recording's variables.
- Normal baseline (97-100) was recorded at 48 kHz (verified: the shaft line only falls
  at the motor speed with fs = 48 kHz); fault files at 12 kHz. Normal recordings are
  decimated ×4 with a zero-phase FIR anti-alias filter so every class has the same
  bandwidth. Otherwise the model could separate classes by sampling rate alone.
- Units are not stated by the source. Values are treated as acceleration in g; this
  is an assumption and only affects absolute velocity/acceleration magnitudes.
- Each recording is cut into non-overlapping 1 s windows (12,000 samples).
- group_id = motor load (hp), so a grouped split tests on an unseen operating load.
  The same physical bearings are recorded at every load, so this does NOT measure
  generalisation to new bearings or machines.

    python -m ml.vibration.datasets.cwru --raw ml/vibration/data/cwru/raw --out ml/vibration/data/cwru/dataset
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.io import loadmat
from scipy.signal import decimate

FS_HZ = 12000
WINDOW = 12000
NOMINAL_RPM = {0: 1797, 1: 1772, 2: 1750, 3: 1730}  # site table, used when a file has no RPM variable

# file number → (label, fault diameter in inches or None, motor load hp, native fs)
FILES: dict[int, tuple[str, float | None, int, int]] = {
    97: ("normal", None, 0, 48000), 98: ("normal", None, 1, 48000),
    99: ("normal", None, 2, 48000), 100: ("normal", None, 3, 48000),
}
for size, base in ((0.007, {"inner_race": 105, "ball": 118, "outer_race": 130}),
                   (0.014, {"inner_race": 169, "ball": 185, "outer_race": 197}),
                   (0.021, {"inner_race": 209, "ball": 222, "outer_race": 234})):
    for label, first in base.items():
        for load in range(4):
            FILES[first + load] = (label, size, load, 12000)


def convert(raw: Path, out: Path) -> pd.DataFrame:
    (out / "signals").mkdir(parents=True, exist_ok=True)
    rows = []
    for num, (label, size, load, native_fs) in sorted(FILES.items()):
        mat = loadmat(raw / f"{num}.mat")
        key = f"X{num:03d}_DE_time"
        if key not in mat:
            raise KeyError(f"{num}.mat has no {key}")
        x = mat[key].ravel().astype(float)
        rpm_key = f"X{num:03d}RPM"
        rpm = float(mat[rpm_key].ravel()[0]) if rpm_key in mat else float(NOMINAL_RPM[load])
        if native_fs != FS_HZ:
            x = decimate(x, native_fs // FS_HZ, ftype="fir", zero_phase=True)
        for w in range(len(x) // WINDOW):
            rid = f"cwru{num}_w{w:02d}"
            np.save(out / "signals" / f"{rid}.npy", x[w * WINDOW:(w + 1) * WINDOW])
            rows.append({
                "recording_id": rid,
                "machine_id": "CWRU-2hp-rig",
                "group_id": f"load_{load}hp",
                "label": label,
                "sampling_rate_hz": FS_HZ,
                "unit": "g",
                "signal_path": f"signals/{rid}.npy",
                "rpm": rpm,
                "load_pct": round(100.0 * load / 2.0, 1),
                "machine_type": "induction_motor_test_rig",
                "source_file": f"{num}.mat",
                "fault_diameter_in": size,
            })
    manifest = pd.DataFrame(rows)
    manifest.to_csv(out / "manifest.csv", index=False)
    card = {
        "name": "CWRU Bearing Data Center: 12k drive-end faults + normal baseline (subset)",
        "source": "https://engineering.case.edu/bearingdatacenter (Case Western Reserve University)",
        "license": "No licence or terms of use stated on the data pages (checked 2026-10-24); publicly downloadable. "
                   "Cite the CWRU Bearing Data Center when using results.",
        "synthetic": False,
        "label_definition": "Bearing condition as documented by CWRU: normal, or a seeded EDM single-point fault on the "
                            "drive-end bearing at the inner race, ball, or outer race (centred @6:00). Fault sizes "
                            "0.007/0.014/0.021 in are merged per location.",
        "description": "Drive-end accelerometer, 1 s windows at 12 kHz. Files: " + ", ".join(f"{n}.mat" for n in sorted(FILES)),
        "units_note": "Units not stated by the source; treated as g (assumption).",
        "preprocessing": "Normal baseline decimated 48 kHz → 12 kHz (×4, zero-phase FIR). Non-overlapping 12,000-sample windows.",
        "group_definition": "group_id = motor load (0-3 hp). Windows of one recording always share a group.",
        "limitations": "One laboratory test rig with artificially seeded faults; the same physical bearings are recorded "
                       "at every load, and there is one normal bearing. Results do not transfer to the UDYAM-Twin "
                       "CNC machines without retraining/validation on their own recordings.",
    }
    (out / "dataset.json").write_text(json.dumps(card, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert CWRU .mat files to the ml.vibration dataset layout")
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    m = convert(args.raw, args.out)
    print(m.groupby(["group_id", "label"]).size().unstack(fill_value=0))


if __name__ == "__main__":
    main()
