"""IMS bearing run-to-failure data → per-snapshot vibration features with ISO 20816-3 zones.

Source: NASA Prognostics Data Repository, "IMS Bearings" (data.nasa.gov, IMS.zip, 1,061,902,801 bytes).
Data: Center for Intelligent Maintenance Systems (IMS), University of Cincinnati (J. Lee, H. Qiu, G. Yu,
J. Lin, and Rexnord Technical Services). Three test-to-failure experiments: four bearings on one shaft at
2000 rpm under a 6000 lb radial load; accelerometers on the bearing housings; one-second snapshots
(20,480 samples at 20 kHz) recorded periodically until failure.

What this script does, and the decisions it makes (recorded in features_meta.json):
- One test at a time: unpack `<n>_test.rar` from the zip, compute features for every snapshot, then
  delete the unpacked files (the zip is kept). Peak disk use is one unpacked test.
- Each bearing channel: `ml.vibration.features.extract_features` with unit g, fs 20 kHz, band 10–1000 Hz,
  so velocity RMS is computed exactly as for the live machines (frequency-domain integration).
- Test 1 records two accelerometers per bearing (x and y); the bearing's velocity RMS is the larger of the
  two, as vibration standards evaluate the highest direction. Tests 2 and 3 have one channel per bearing.
- Zone = the ISO 20816-3 Group 2 rigid demo profile from config/machines.yaml (1.4 / 2.8 / 4.5 mm/s).
  This is a label computed with the standard's method, not a certified assessment: the IMS rig is a test
  stand, not a machine in the standard's groups.
- Units: the readme describes PCB 353B33 accelerometers; values are treated as acceleration in g.

    python -m ml.vibration.datasets.ims --zip ml/vibration/data/ims/IMS.zip --out ml/vibration/data/ims
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from ml.vibration.features import extract_features

FS_HZ = 20000
RPM = 2000
BAND_HZ = (10.0, 1000.0)
SNAPSHOT_NAME = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\.(\d{2})\.(\d{2})\.(\d{2})$")
# test → bearing → its channel columns (0-based), from the IMS readme
CHANNELS = {
    1: {1: (0, 1), 2: (2, 3), 3: (4, 5), 4: (6, 7)},
    2: {1: (0,), 2: (1,), 3: (2,), 4: (3,)},
    3: {1: (0,), 2: (1,), 3: (2,), 4: (3,)},
}
# failures reported at the end of each test (IMS readme)
FAILURES = {
    1: {3: "inner_race", 4: "roller_element"},
    2: {1: "outer_race"},
    3: {3: "outer_race"},
}
FEATURES = ("velocity_rms_mm_s", "accel_rms_m_s2", "accel_peak_m_s2", "crest_factor", "kurtosis",
            "dominant_freq_hz", "disp_pp_um")


def zone_of(v: float | None, zones: dict[str, float]) -> str | None:
    if v is None or not np.isfinite(v):
        return None
    return "D" if v >= zones["C_D"] else "C" if v >= zones["B_C"] else "B" if v >= zones["A_B"] else "A"


def _snapshot_time(name: str) -> datetime | None:
    m = SNAPSHOT_NAME.match(name)
    return datetime(*map(int, m.groups())) if m else None


def _features_for_file(args: tuple[int, str]) -> list[dict]:
    test, path = args
    data = pd.read_csv(path, sep=r"\s+", header=None, engine="c").to_numpy(dtype=float)
    rows = []
    for bearing, cols in CHANNELS[test].items():
        per_channel = []
        for c in cols:
            f = extract_features(data[:, c], FS_HZ, "g", band_hz=BAND_HZ, rpm=RPM)
            per_channel.append(f.features if not any(i.severity == "error" for i in f.issues) else None)
        valid = [p for p in per_channel if p is not None]
        if not valid:
            continue
        best = max(valid, key=lambda p: p.get("velocity_rms_mm_s") or 0.0)  # highest direction
        rows.append({"test": test, "bearing": bearing, "file": Path(path).name,
                     **{k: best.get(k) for k in FEATURES},
                     "velocity_rms_channels_mm_s": "|".join(f"{(p or {}).get('velocity_rms_mm_s', '')}" for p in per_channel)})
    return rows


def _unpack(zip_path: Path, member: str, dest: Path) -> None:
    """Extract one member of the zip, then the RAR inside it, with the system bsdtar (libarchive)."""
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["bsdtar", "-xf", str(zip_path), "-C", str(dest), f"IMS/{member}"], check=True)
    rar = dest / "IMS" / member
    (dest / "raw").mkdir(exist_ok=True)
    subprocess.run(["bsdtar", "-xf", str(rar), "-C", str(dest / "raw")], check=True)
    rar.unlink()
    shutil.rmtree(dest / "IMS", ignore_errors=True)


def process_test(zip_path: Path, test: int, work: Path, workers: int) -> pd.DataFrame:
    member = f"{['1st', '2nd', '3rd'][test - 1]}_test.rar"
    t0 = time.time()
    _unpack(zip_path, member, work)
    files = sorted((p for p in (work / "raw").rglob("*") if p.is_file() and _snapshot_time(p.name)),
                   key=lambda p: _snapshot_time(p.name))
    if not files:
        raise RuntimeError(f"{member}: no snapshot files found")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        rows = [r for chunk in pool.map(_features_for_file, [(test, str(p)) for p in files], chunksize=16) for r in chunk]
    shutil.rmtree(work / "raw")  # raw text files are large; only the features are kept
    df = pd.DataFrame(rows)
    df["ts"] = df["file"].map(_snapshot_time)
    start, end = df["ts"].min(), df["ts"].max()
    df["hours"] = (df["ts"] - start).dt.total_seconds() / 3600.0
    df["hours_to_end"] = (end - df["ts"]).dt.total_seconds() / 3600.0
    df["failed"] = df["bearing"].map(lambda b: b in FAILURES[test])
    df["failure_mode"] = df["bearing"].map(lambda b: FAILURES[test].get(b))
    print(f"  test {test}: {len(files)} snapshots, {start} → {end} ({(end - start).total_seconds() / 3600:.1f} h), "
          f"{time.time() - t0:.0f} s")
    return df


def main() -> int:
    from config import get_config

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zip", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="folder for features.csv and features_meta.json")
    ap.add_argument("--tests", default="1,2,3")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    zones = get_config().profiles["demo-iso20816-3-g2-rigid"]["zone_boundaries"]
    out_csv = args.out / "features.csv"
    work = args.out / "work"
    frames = [pd.read_csv(out_csv, parse_dates=["ts"])] if out_csv.exists() else []
    done = set(frames[0]["test"].unique()) if frames else set()
    for test in (int(t) for t in args.tests.split(",")):
        if test in done:
            print(f"  test {test}: already in {out_csv.name}, skipped")
            continue
        df = process_test(args.zip, test, work, args.workers)
        df["zone"] = df["velocity_rms_mm_s"].map(lambda v: zone_of(v, zones))
        frames.append(df)
        pd.concat(frames, ignore_index=True).to_csv(out_csv, index=False)  # saved after every test
    shutil.rmtree(work, ignore_errors=True)
    all_df = pd.concat(frames, ignore_index=True)
    meta = {
        "source": "NASA Prognostics Data Repository, IMS Bearings (data.nasa.gov IMS.zip, 1,061,902,801 bytes)",
        "credit": "Center for Intelligent Maintenance Systems, University of Cincinnati (Lee, Qiu, Yu, Lin; Rexnord Technical Services)",
        "sampling_rate_hz": FS_HZ, "rpm": RPM, "band_hz": list(BAND_HZ), "unit_assumed": "g",
        "zone_profile": "demo-iso20816-3-g2-rigid", "zone_boundaries_mm_s": zones,
        "zone_note": "zones computed with the ISO 20816-3 method on a test rig; not a certified assessment",
        "direction_rule": "test 1 has x and y channels per bearing; the larger velocity RMS is used",
        "failures": {str(t): {str(b): m for b, m in fs.items()} for t, fs in FAILURES.items()},
        "rows": int(len(all_df)), "snapshots_per_test": {str(t): int(n) for t, n in all_df.groupby("test")["file"].nunique().items()},
    }
    (args.out / "features_meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"Wrote {out_csv} ({len(all_df)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
