# UDYAM-SIM v1: our own labelled dataset from the virtual factory

**What it is:** 77,000 labelled 60-second windows from 440 randomised runs (episodes) of the
UDYAM-TWIN virtual CNC line. **Synthetic (simulation) data** — say so every time you cite it.

| | |
|---|---|
| Generator | `python -m tools.generate_dataset` (seed 2026, ~13 min on a laptop) |
| Episodes | 400 in-distribution + 40 out-of-distribution (OOD), 30 simulated minutes each |
| Rows | one per 10 s of VMC-01 (window 60 s): 70,000 in-distribution + 7,000 OOD |
| Files | `udyam_sim_v1_windows.csv` (features + labels), `udyam_sim_v1_episodes.csv` (settings + outcome per run), `udyam_sim_v1_meta.json` (ranges, weights, feature list) |
| Trainer | `python -m ml.train_sim` → `ml/sim_model.joblib`, `ml/sim_metrics.json`, `ml/sim_knowledge.json` |

## How each episode differs (a different "machine" every run)

| Knob | Range | Meaning |
|---|---|---|
| `h_true` | 0.92 – 1.10 | true cooling capacity |
| `ambient_offset_k` | −3 … +1 K | shop-floor temperature |
| `load_scale` | 0.85 – 1.12 (OOD: 1.12 – 1.22) | job heaviness (cutting torque) |
| `noise_scale` | 0.7 – 1.6 | sensor / process noise |
| `fault_severity` | 0.6 – 1.6 | how hard the injected fault hits |
| tool wear at start | 0 – 200 min | |
| fault onset | 60 – 600 s | |
| fault | normal 22 %, coolant 16 %, bearing 12 %, overload 10 %, tool wear 10 %, stuck sensor 8 %, compressor pressure drop 10 %, order surge 12 % | one per episode |
| supervised self-healing | on in 50 % of episodes | gives "with vs without auto-derate" outcomes |

## Why the labels are not circular

The **label** is the fault the simulated plant was actually given — ground truth that the twin
never sees. The **features** are only what sensors and the twin observe: temperature, vibration,
current, torque, rpm, wear (mean / spread / trend / max), data-quality flags, air pressure, the
twin's residual (EWMA, CUSUM) and its **calibrated cooling coefficient and its drift**. The model
has to infer the fault from signals, as on a real machine.

Labels per window: `label_fault` (8 classes) and `minutes_to_limit` (minutes until the process
temperature limit or vibration zone D, capped at 30 = "not within 30 min").

## Results (held-out, split by episode)

| Test set | Random Forest macro-F1 | Rule baseline macro-F1 | Time-to-limit MAE (when a limit is ahead) |
|---|---|---|---|
| 80 unseen episodes (same machine ranges) | **0.993** | 0.857 | 5.3 min |
| 40 OOD episodes (heavier machines never trained on) | **0.851** | 0.659 | 4.2 min |

What the numbers say (use these in Q&A):

- **Coolant degradation is where the twin earns its keep:** rules F1 0.15, model 0.98. Fixed
  thresholds can't see a slow cooling loss; the twin's calibrated `h_cool` drift can
  (`h_cool_est` is a top-2 feature).
- **The model degrades on machines it hasn't seen** (0.99 → 0.85; heavier normal machines get
  mistaken for overload). That is exactly why each real machine needs its own calibration and
  pilot data — we measured the gap instead of hiding it.
- **Auto-derate works for thermal faults, not mechanical ones:** in the simulated episodes,
  coolant loss reached the limit in 58 % of runs without action vs 11 % with supervised
  auto-derate; a bearing defect reached zone D in 100 % either way. (Also in `sim_knowledge.json`.)
- Median detection delay after onset: bearing 4.5 s, stuck sensor 6.5 s, overload 10 s,
  pressure drop 12 s, coolant 33 s.

## Limits (say them)

- Scores measure how well the model learns **our simulator**, whose physics we wrote. They are not
  real-machine accuracy. Next step: ESP32 desk-motor data, then a pilot machine.
- Sensor dropout, blade wear, conveyor and panel faults are not in v1 (VMC-01 only).
- Re-generate after changing `sim/plant.py` physics, or the model learns an old simulator.
