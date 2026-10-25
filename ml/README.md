# ml/

AI4I 2020 failure-risk model and the rule baseline.

| File | What it is |
|---|---|
| `data/ai4i2020.csv` | the UCI AI4I 2020 file ([dataset 601](https://archive.ics.uci.edu/dataset/601/ai4i+2020+predictive+maintenance+dataset), CC BY 4.0). Gitignored; the loader checks its SHA-256 and refuses anything else |
| `ai4i.py` | dataset loader and `AI4IPredictor` (failure risk %, per-mode risk, top contributing features, out-of-range check) |
| `train_ai4i.py` | `python -m ml.train_ai4i` → `models/ai4i_rf.joblib` and `models/metrics.json` |
| `rules.py` | AI4I failure-mode rules (HDF/PWF/OSF/TWF) used as a baseline, plus the shop-floor vibration/temperature rules |
| `vibration/` | vibration health and the CWRU bearing classifier ([README](vibration/README.md)) |

## Model

- **Inputs:** air temperature, process temperature, rotational speed, torque, tool wear. Vibration and current never go in (AI4I has no such columns).
- **Targets:** Machine failure and the TWF / HDF / PWF / OSF / RNF modes, one `RandomForestClassifier(class_weight="balanced")` each.
- **Evaluation:** stratified 80/20 split on Machine failure (seed 42), metrics at the 0.5 vote threshold on the held-out rows, plus 5-fold CV F1 for Machine failure. Per-class precision, recall, F1, ROC-AUC and PR-AUC are in `models/metrics.json` and served at `GET /api/metrics`.
- **Applies to:** machines whose config has `ml.failure_model: ai4i` (the CNC mill). AI4I is a milling dataset, so the lathe, saw and compressor show "not applicable".
- **Output:** "failure risk %" is a Random Forest vote share, **not a calibrated probability**. Top contributing features are computed per prediction by replacing each input with its training median and measuring the drop in risk. Inputs outside the training range (for example process temperature above 40.7 °C during a coolant fault) are flagged `OUT_OF_RANGE`.

## Limits

AI4I 2020 is itself a synthetic dataset modelled on a real milling process. Good test scores show the model learned that dataset, not that it predicts failures on this shop's machines; that needs validation on recorded field data. RNF failures are random by construction and are not predictable (F1 0). TWF has 10 positives in the test split.

Before 25 Oct 2026, `data/ai4i2020.csv` in this checkout was a synthetic stand-in generated from the baseline rules (27.4 % failures; the real file has 3.39 %), which is why earlier metrics showed F1 ≈ 0.99. Those metrics are retired.
