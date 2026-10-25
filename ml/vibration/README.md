# ml/vibration/

Vibration-based machine health, kept separate from the AI4I model in `ml/ai4i.py`. AI4I 2020 has no vibration signal, so its columns are never used as vibration features and its failure labels are never used as vibration fault labels.

## Status (24 Oct 2026)

| Part | Status |
|---|---|
| Feature extraction + input validation (`features.py`) | Implemented and tested against analytic signals |
| Severity framework (`severity.py`) | Implemented, **unconfigured**: no threshold profiles or machine classes, so every machine reports *Assessment unavailable* |
| Training pipeline (`train.py`, `evaluation.py`) | Implemented. **Trained on a public lab dataset (CWRU), not on UDYAM-Twin machines**; see below |
| Inference (`inference.py`) | Serves the CWRU model with domain checks: other sampling rates → *Out of model domain*; other machine types → *unvalidated* |
| Monitor + API + dashboard (`monitor.py`, `api/vibration.py`, `web/`) | Live in the app; every value is labelled measured / simulated / replayed / recorded |

## Inputs

1. **Stream scalar.** `vibration_rms_mm_s` from each `observed` frame: the plant simulator (SIM), an IoT device (LIVE), or a device registered as `simulated` (SIM). Its frequency band is not declared, so severity profiles that specify a band cannot use it.
2. **Waveform.** `POST /api/iot/waveform` from a device (headers `X-Device-Id`, `X-Device-Token`), or `POST /api/vibration/{machine_id}/recording` with a `source` for recorded files:
   ```json
   {"sampling_rate_hz": 2560, "unit": "m/s2", "samples": [...], "axis": "radial", "location": "bearing_housing", "sensor_range": 39.2}
   ```
   Units: `m/s2`, `g`, `mm/s`, `m/s`, `um`, `mm`. At most 262,144 samples.

## Features

| Feature | From | Notes |
|---|---|---|
| `accel_rms_m_s2`, `accel_peak_m_s2`, `crest_factor`, `kurtosis` | acceleration waveform | time domain, mean removed, full record bandwidth |
| `velocity_rms_mm_s` | acceleration or velocity waveform | frequency-domain integration within the band (default 10–1000 Hz) |
| `disp_pp_um` | any waveform | band-limited integration, peak-to-peak |
| `dominant_freq_hz` | any waveform | Hann-windowed spectrum peak in the band |
| `dominant_order` | + rpm | dominant frequency ÷ shaft frequency |
| `rpm`, `load_pct` | stream | load = power_kw ÷ nameplate rated_kw |

Validation rejects or flags each of these:
- **Rejected:** unknown units, non-positive or non-numeric sampling rates, non-finite samples (gaps are not interpolated), fewer than 64 samples, flat signals, and clipping when `sensor_range` is given.
- **Frequency features skipped:** when `fs < 2.56 × band high` (the band is truncated to fit if possible), or when the record is shorter than 4 periods of the band's low edge.

The default band is a placeholder. Confirm the band that the applicable standard and machine class require.

## Severity configuration

Profiles and machine assignments live in [`config/machines.yaml`](../../config/machines.yaml) (`vibration_profiles` and each template's `vibration` block), loaded and validated by `config/loader.py`. Machines added at runtime inherit their template's profile.

The shipped profiles use the commonly cited ISO 20816-3 Group 2 boundaries (unchanged from the withdrawn ISO 10816-3) as **configurable demo thresholds, not a certified assessment**. They are marked `verified: false`, so every result is reported as **Provisional**, with the zone and the boundary crossed (for example `Zone B (2.33 mm/s ≥ A/B 1.4 mm/s, below B/C 2.8 mm/s)`).
- VMC, lathe, saw: rigid foundation, A/B 1.4 · B/C 2.8 · C/D 4.5 mm/s RMS, 10–1000 Hz.
- Compressor: flexible foundation, A/B 2.3 · B/C 4.5 · C/D 7.1 mm/s RMS, 10–1000 Hz.

ISO 20816-3 Group 2 covers machines of 15–300 kW, so for these smaller machines the values are illustrative. To mark a profile verified, an engineer sets `verified: true`, `verified_by` and `source_reference` after checking the applicable part and edition.

The simulated stream value is defined as velocity RMS over the machine's `stream_band_hz`. A LIVE device's band is not declared, so LIVE values return **UNAVAILABLE** until the device contract declares one.

Assessment returns **UNAVAILABLE** or **NOT_APPLICABLE**, with the reason, in any of these cases:
- the machine is not classified;
- the profile is incomplete;
- the mounting, location, band, speed or power doesn't match the profile;
- the machine is not in steady operation (RUN or DERATE);
- data quality is not OK.

Rule risk in the twin uses the same profile: zone C → MEDIUM, zone D → HIGH.

## Training a real model

```bash
python -m ml.vibration.train --data path/to/dataset --split auto
```

The dataset needs these files:
- **`dataset.json`:** `name`, `source`, `license`, `synthetic` (true/false), `label_definition`.
- **`manifest.csv`:** one row per recording.
  - Required columns: `recording_id, machine_id, label, sampling_rate_hz, unit, signal_path`.
  - Optional columns: `recorded_at, rpm, load_pct, machine_type, sensor_range`.
- **Signal files:** `.npy` or single-column `.csv` waveforms.

How the pipeline trains:
- **Splits:** by machine when there are at least 3 machines, otherwise by `recorded_at` time. Random row splits are not offered.
- **Preprocessing:** median imputation and scaling, fitted on the training split only.
- **Imbalance:** class weights on the training split; the test split keeps its natural distribution.
- **Models compared:** majority-class baseline, logistic-regression baseline and Random Forest. Selection uses validation macro F1; the test split is used only for reporting.
- **Outputs:** `artifacts/model.joblib` and `artifacts/metrics.json`, with the dataset card, features, seed, split and per-class metrics.
- **Synthetic datasets:** refused unless `allow_synthetic` is set (used only in tests). A synthetic model's predictions are never published.
- **Vote share:** the Random Forest's class vote share is shown as an uncalibrated vote fraction, not a probability.

### Data needed for genuine training

- **Waveforms** from the target machines (or the same machine class): an accelerometer on the bearing housings with known axis and mounting, at least 2.56 × the band's upper edge (e.g. ≥ 2.56 kHz for 1 kHz), with a few seconds per record.
- **Operating context** per recording: speed and load.
- **Confirmed condition labels** (healthy, imbalance, misalignment, looseness, bearing defect, …) from maintenance records or teardown, not inferred from thresholds.
- **Coverage:** several machines, with enough recordings per class that each split contains every class.
- **Provenance and licence** for any public dataset you use.

## Trained model: CWRU Bearing Data Center (24 Oct 2026)

**Data.** The source is https://engineering.case.edu/bearingdatacenter; 40 files were downloaded to `data/cwru/raw/`, which git ignores. Conversion (reproducible):
```bash
python -m ml.vibration.datasets.cwru --raw ml/vibration/data/cwru/raw --out ml/vibration/data/cwru/dataset
python -m ml.vibration.train --data ml/vibration/data/cwru/dataset --split machine
```
- **Classes:** normal, plus faults at the inner race, ball and outer race (@6:00). Each fault was recorded at sizes 0.007, 0.014 and 0.021 in and at loads of 0–3 hp.
- **Signal:** the drive-end accelerometer, cut into 1 s windows at 12 kHz. That gives 395 windows: 35 normal and 120 per fault class.
- **Sampling rates:** the normal baseline was recorded at 48 kHz. I verified this from where the shaft-speed line falls, and decimated it to 12 kHz to match the fault recordings.
- **Units:** not stated by the source, so treated as g.
- **Licence:** none stated on the data pages. Cite the CWRU Bearing Data Center.

**Evaluation.** Groups are motor loads, so each test load is never seen in training (the "cross-load" protocol). The validation split selected the model, and the test split was used only for reporting.

| Model | Held-out test (load 1 hp), macro F1 | Leave-one-load-out macro F1, mean ± sd (min) |
|---|---|---|
| Majority-class baseline | 0.115 | 0.117 ± 0.002 (0.115) |
| Logistic regression | 0.719 | 0.707 ± 0.107 (0.538) |
| **Random Forest (selected)** | **0.983** | **0.930 ± 0.066 (0.821)** |

The Random Forest scored 0.831 on the validation load (0 hp); ball faults at 0 hp are its weakest case. Per-class metrics and confusion matrices are in `artifacts/metrics.json`.

**Limitations (read before using predictions):**
- **One rig, shared bearings.** The data comes from one laboratory rig with artificially seeded (EDM) faults. Every load uses the same physical bearings, and there is only one normal bearing. The scores therefore measure generalisation to an unseen *load*, not to new bearings or machines, and are optimistic for field use.
- **Different machines.** Not validated for the UDYAM-Twin CNC machines. The app shows those predictions as *ML prediction (unvalidated)*.
- **Same sampling rate required.** Waveforms must be sampled at 12 kHz to get a prediction. The ESP32 / fake-device waveforms (2560 Hz) are reported as *Out of model domain*.
- **Vote share.** The class vote share is not a calibrated probability.

## IMS bearing run-to-failure validation (ISO 20816-3)

Real bearings that ran until they failed, used to check how the ISO 20816-3 zones and an early-warning
model behave before a failure. Source: NASA Prognostics Data Repository, *IMS Bearings* (`IMS.zip`,
1,061,902,801 bytes, data.nasa.gov); data by the Center for Intelligent Maintenance Systems, University of
Cincinnati, with Rexnord. Four Rexnord ZA-2115 bearings on one shaft at 2000 rpm under 6000 lb radial load;
one-second snapshots (20,480 samples at 20 kHz), three tests, 9,464 snapshots in total. The archive's third test
has 6,324 snapshots up to 18 April 2004 (the readme says 4,448 up to 4 April); the archive is used as it is.

```bash
python -m ml.vibration.datasets.ims --zip ml/vibration/data/ims/IMS.zip --out ml/vibration/data/ims   # ~5 min
python -m ml.vibration.train_ims                                                                       # ~15 s
```

The loader unpacks one test at a time, computes the same features as the live machines (velocity RMS by
frequency-domain integration, 10–1000 Hz; test 1's x/y channels → the larger), labels every snapshot with the
demo Group 2 rigid zone, and deletes the raw files. Snapshots where every bearing reads < 0.1 mm/s (rig
stopped) are excluded. Results: `ml/vibration/artifacts/ims_metrics.json`, `GET /api/datasets/ims`, the
dashboard panel *Vibration Dataset · ISO 20816-3 on IMS Run-to-Failure*, and two sheets in the Excel export.

**Zones before failure** (velocity RMS in mm/s):

| Bearing | Failure | Test length | Baseline → max | Final zone | Zone C first reached |
|---|---|---|---|---|---|
| T1-B3 | inner race | 827.6 h | 0.41 → 7.01 | D | 0.0 h before the end |
| T1-B4 | roller element | 827.6 h | 0.31 → 0.98 | A | never |
| T2-B1 | outer race | 163.5 h | 0.64 → 3.17 | B | 0.3 h before the end |
| T3-B3 | outer race | 1073.1 h | 0.51 → 27.49 | D | 0.0 h before the end |

On this rig velocity RMS stays in zone A for almost the whole life and crosses into C/D only at the very end
(the roller-element failure never leaves zone A), so the zones alone give almost no warning. That is a real
finding, not a bug: the bearings are heavily loaded and the defects show first in impulsiveness (kurtosis)
and high-frequency content, not in broadband velocity.

**Early warning "fails within 24 h"**, leave-one-test-out (train on two tests, evaluate on the third), alarm = held for 3 snapshots:

| Method | Precision | Recall | False alarms (healthy bearings) | First alarm before failure (T1-B3, T1-B4, T2-B1, T3-B3) |
|---|---|---|---|---|
| Random Forest on baseline-normalised features | 0.261 | 0.597 | 0.46 % | 12.5 h, 128.3 h, 2 h, 25.2 h |
| Rule: velocity ≥ 2 × the bearing's own baseline | 0.663 | 0.151 | 0.00 % | 10.7 h, 92.7 h, 1 h, 3.3 h |
| Rule: zone C or worse | 1.0 | 0.008 | 0.00 % | none, none, none, none |

**RUL**: a regressor for hours-to-failure (capped at 100 h, leave-one-failed-bearing-out) is *worse* than
predicting the mean on 3 of the 4 bearings; with four run-to-failure bearings there is not enough data, so no
RUL is claimed. A time-to-zone-D model is not trainable either: zone D appears only in the final snapshot.

Limits: one test rig at one speed and load, four failures; zones are computed with the standard's method on a
rig outside its machine groups, so they are labels for comparison, not a certified assessment.
