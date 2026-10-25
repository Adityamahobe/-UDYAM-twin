# NASA C-MAPSS turbofan RUL (experimental research module)

A separate remaining-useful-life experiment on NASA's simulated turbofan engines. **It is not one of the CNC
machines and nothing here feeds their RUL**; it shows the RUL method on a standard public benchmark.

## Data

NASA Prognostics Data Repository, *CMAPSS Jet Engine Simulated Data* (data.nasa.gov `CMAPSSData.zip`,
12,425,978 bytes; committed in the repo). Engines simulated with C-MAPSS run from normal operation to failure
(A. Saxena, K. Goebel, D. Simon, N. Eklund, *Damage Propagation Modeling for Aircraft Engine Run-to-Failure
Simulation*, PHM08). Each row is one flight cycle: unit, cycle, 3 operating settings, 21 sensors. Training
engines run to failure; test engines stop earlier and `RUL_FD00x.txt` gives their true remaining cycles.
The archive's FD004 has 249 training and 248 test engines (its readme says the reverse); the files are used as they are.

The zip is committed at `ml/data/cmapss/CMAPSSData.zip` (see [`DATA.md`](../DATA.md)); the module reads it directly.

```bash
python -m ml.cmapss          # ~2.5 min, writes models/cmapss_metrics.json
```

## Method (fixed before looking at the test engines)

- Target: cycles to failure, capped at 125 (early cycles count as healthy, the usual piecewise-linear target).
- Operating conditions: settings rounded and grouped (1 condition in FD001/FD003, 6 in FD002/FD004); every sensor
  z-scored per condition with training statistics; sensors without variance dropped.
- Features per cycle, past cycles only: normalised sensors, 10-cycle rolling mean, 20-cycle slope, cycle count.
- Model: `HistGradientBoostingRegressor` with fixed settings. Baselines: predict the training mean; ridge regression
  on the same features.
- Test: every official test engine at its last recorded cycle. RMSE against the true RUL as given and with the true
  RUL capped at 125 (both conventions appear in papers), and the PHM08 score (Σ e^(−d/13)−1 for early, e^(d/10)−1 for
  late predictions, d = predicted − true; lower is better). Also 5-fold cross-validation on training engines,
  grouped by engine.

## Results (official test engines; RMSE in cycles)

| Subset | Conditions / faults | Train / test engines | Boosting RMSE | Boosting RMSE (true capped 125) | PHM08 score | Linear RMSE | Mean RMSE | CV RMSE (capped target) |
|---|---|---|---|---|---|---|---|---|
| FD001 | 1 condition, HPC degradation | 100 / 100 | **15.7** | 14.5 | 433 | 20.23 | 43.07 | 13.99 ± 0.38 |
| FD002 | 6 conditions, HPC degradation | 260 / 259 | **26.3** | 13.98 | 9,166 | 30.36 | 54.08 | 15.11 ± 0.49 |
| FD003 | 1 condition, HPC + fan degradation | 100 / 100 | **14.95** | 13.23 | 362 | 20.7 | 45.07 | 12.07 ± 1.31 |
| FD004 | 6 conditions, HPC + fan degradation | 249 / 248 | **26.88** | 15.21 | 4,794 | 32.2 | 54.9 | 14.51 ± 1.26 |

Gradient boosting beats both baselines on every subset. On FD002/FD004 the uncapped RMSE is much higher than the
capped one because many test engines have more than 125 cycles left, which a capped model cannot predict.
Published deep-learning models report lower errors on these subsets; this module is a transparent baseline, not
a state-of-the-art claim.

Served at `GET /api/datasets/cmapss` (`?predictions=true` adds true vs predicted RUL per test engine), shown in the
dashboard panel *Research · Turbofan RUL (NASA C-MAPSS)*, and exported as the *C-MAPSS RUL* sheet.
