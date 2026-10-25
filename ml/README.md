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

## Model registry, candidates and gated promotion

`ml/model_registry.py` versions the AI4I failure-risk model the twin uses. `ml/candidate.py` trains candidates.
Dashboard: **Model Registry** panel. API: `/api/models/ai4i`, `/api/models/ai4i/train`,
`/api/models/ai4i/{version}/promote`, `/api/models/ai4i/rollback`.

- **Baseline:** the first time the registry is used, `models/ai4i_rf.joblib` is registered as `v001` (baseline,
  production). Versions live in `models/registry/ai4i_failure/` (git-ignored). `registry.json` holds the
  production version, each version's card and the full promotion and rollback history.
- **Candidates:**
  - The machine-failure model is trained on the AI4I training split plus windows from chosen synthetic datasets.
  - The synthetic windows are mapped to the AI4I inputs: air temperature, process temperature, rpm, torque and
    tool wear (window means).
  - Their label is ground truth from the simulator: the machine's true process temperature is at or over its
    configured limit.
  - Windows with bad sensor data are dropped.
  - A dataset is refused unless it is labelled SYNTHETIC DIGITAL TWIN DATA and its checksum matches its metadata.
  - The failure-mode models have no synthetic labels and are retrained on AI4I only.
- **Evaluation:** the candidate and the current production model are scored on the same held-out rows: the AI4I
  20 % test split (seed 42) and 20 % of synthetic episodes, held out whole.
- **Gate** (`GATE` in `ml/candidate.py`, fixed before training): compared with the production model on the AI4I
  test,
  - F1 may drop by at most 0.02;
  - recall may drop by at most 0.03;
  - ROC-AUC may drop by at most 0.01;
  - the false-positive rate may rise by at most 1 point;
  - synthetic-test F1 must rise by at least 0.05;
  - there must be at least 20 training and 10 test synthetic positives, and at least 10 held-out synthetic windows
    under the limit (a one-class test set cannot show false alarms).
- **Deployment:** never automatic. Training only registers a candidate. Promotion needs an operator (the same rule
  as Advisor approvals: `UDYAM_OPERATOR_TOKEN`, or a browser on the server machine), refuses a version that failed
  its gate, and is refused in Real Data mode. Rollback returns to the previous production version (or one named
  earlier production version). The twin reloads the model at once; `/api/metrics` shows the production version's
  metrics.

**Limits:**
- Every version is trained only on synthetic data: AI4I 2020 is itself synthetic, and so is UDYAM simulator data.
  No version has learned from real machines, and none is validated on them.
- The synthetic-test gain mainly shows the candidate learned the simulated machine's temperature limit, which
  AI4I does not encode.
- The synthetic test episodes come from the same simulator as the training episodes.

## Adaptive learning: input drift and operator-confirmed labels

`ml/adaptive.py`. Dashboard: **Adaptive Learning** panel. API: `/api/adaptive`, `/api/adaptive/baseline`,
`/api/adaptive/labels`, `/api/adaptive/labels/{id}/retract`, `/api/adaptive/labels/export`.

The drift monitor watches the failure-risk model's five inputs on every machine that uses the model (VMC-01 by
default). It keeps each telemetry source apart (SIM, REPLAY, LIVE) and samples every 2 s, keeping 30 minutes.

- **Model coverage:** the share of the last 5 minutes that lies outside the production version's training data.
  A sample is outside when its nearest-neighbour distance in standardised inputs is above the 99th percentile
  of the training rows' own distances. Status: OK under 5 %, WATCH from 5 %, OUTSIDE TRAINING DATA from 25 %.
  - The reference is the production version's training inputs (`reference.npz` in its registry folder,
    written at training; v001 is rebuilt from the AI4I split).
  - PSI and KS tests are not used: AI4I spans many operating points and one machine in steady state is a
    narrow slice of it, so they would flag drift constantly.
  - Even the healthy simulated mill sits partly outside AI4I's region (about 20 % in our runs, so WATCH),
    because AI4I was not made from this machine.
- **Machine drift:** the shift of the 5-minute mean from the machine's own baseline, in baseline spreads, with a
  floor per input. WATCH from 2, DRIFT from 3. Tool wear is excluded because it grows by design.
  - The baseline is the first 5 minutes after start or a simulation reset, or "Set baseline" after an
    intended change.
  - A reset restarts the baseline and the evaluation window but keeps the buffer, so readings from before
    it can still be labelled.
- **Needs 2 minutes of samples before a verdict.** With the Auto Demo on (a reset every 90 s) it reports
  "collecting", not a guess.
- **Drift is information, not a fault.** It raises a `DATA_DRIFT` event (never an alert), recommends what to
  check, and triggers no retraining or deployment.

Operator-confirmed labels:
- **What a label is:** an operator, with the same authorisation as Advisor approvals, confirms that a window was a
  failure, a false alarm or normal operation. The window is an alert's time (from 5 min before it until 1 min
  after its last update), the last N minutes, or start and end, up to 30 minutes.
- **What is stored:** the buffered model inputs for that window are copied into the `confirmed_labels` table
  with the label, operator, source and an optional alert, fault type and note.
- **LIVE windows:** accepted only if the device sent all five inputs. Otherwise some values would be the
  simulation's.
- **Retracting:** labels are retracted, never deleted, so the audit trail stays.
- **Training:** unlabelled readings are never used as labels. A candidate can include the confirmed labels
  ("Include operator-confirmed labels" in the Model Registry). Then:
  - 20 % of labels are held out (when there are at least 5) and both models are scored on them;
  - the gate adds "confirmed-label F1 may drop by at most 0.02", or reports it as not measured when fewer
    than 5 positives and 5 negatives are held out;
  - the model card says how many training samples came from real machines (LIVE), and `real_machine_data`
    is true only then.
