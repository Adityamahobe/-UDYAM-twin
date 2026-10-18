# R3: Backend + ML

**You are the plumbing and the honesty.** Everything flows through you, and the metrics you produce are what judges trust.

**Also read:** MASTER_PLAN §4.2 (schema), §4.4 (AI/analytics), §6 (evidence pack)

## Deliverables
| Due | Output |
|---|---|
| Sun 18 | FastAPI skeleton; `/ws/telemetry` streaming R1's fake data; serves `web/` statically; `start.bat` / `start.sh` |
| Mon 19 | AI4I downloaded; `ml/rules.py` (HDF, PWF, OSF, TWF definitions); `ml/train.py` RandomForest (balanced, stratified, fixed seed) |
| Tue 20 | `metrics.json` (precision, recall, F1, PR-AUC, confusion matrix) for **rules vs RF**; risk + top signals per tick for each machine |
| Wed 21 | SQLite: events, work orders (Open → Ack → In Progress → Closed), param-version log; REST for faults, work orders, tool-wear entry → R1's calibrator |
| Thu 22 | `POST /machines` for the onboarding wizard; ₹ / kWh / CO₂ / line-loss formulas with editable assumptions (CEA factor cited with year); REPLAY mode streaming AI4I rows |
| Fri 23 | Works fully offline; data-quality tests (missing, late, out-of-range). Freeze. |

## Rules
- The AI4I model gets **AI4I fields only**; vibration and current go to the rules engine.
- Never report plain accuracy. Write one honest line into `metrics.json`: "AI4I labels are rule-generated; high scores are expected and are not real-world validation."
