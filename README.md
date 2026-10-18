# UDYAM-TWIN

A self-calibrating, browser-based digital twin for MSME machine shops: a 4-machine CNC mini line with one detailed hero machine. Built by Tech Titans (NIT Raipur) for INAE Youth Conclave 2026, HT14.

- **Plan:** [`docs/MASTER_PLAN.md`](docs/MASTER_PLAN.md) · **your role:** [`docs/briefs/`](docs/briefs/)
- **Message contract (frozen v1.0):** [`docs/SCHEMA.md`](docs/SCHEMA.md) + [`docs/schema.json`](docs/schema.json)

## Quick start (day 1: fake stream)

The real twin core isn't built yet. A **fake stream** emits exactly the messages the real one will, so 3D, backend and dashboard work can start now.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m tools.fake_stream                              # then open http://localhost:8765
```

- WebSocket: `ws://localhost:8765/ws/telemetry`. Every message follows `docs/schema.json`.
- It runs a scripted **150-second demo loop**: coolant drift → twin recalibrates → over-temperature forecast → auto-derate → compressor common cause → bearing anomaly (calibration frozen) → reset.
- `--machines 3` starts without the compressor (to test the "Add your machine" wizard); `--no-auto` turns off the scripted faults.
- **No Python?** Use [`samples/demo_stream.jsonl`](samples/demo_stream.jsonl): one message per line, one full loop. Replay it at 10 messages per tick.

Run the tests: `python -m pytest`

## Layout

| Folder | Owner | What goes there |
|---|---|---|
| `sim/` | R1 | plant simulators (hidden "true" machines), virtual PLC, fault injectors, line model |
| `twin/` | R1 | twin model, residuals, calibration (RLS), forecast |
| `api/` | R3 | FastAPI app, WebSocket, REST, SQLite |
| `ml/` | R3 | AI4I rules + RandomForest, metrics |
| `web/` | R2 + R4 | Three.js scenes + dashboard |
| `assets/` | R2 | glb models + attribution (TseNC, CERN-OHL-W) |
| `edge/` | R5 | ESP32 firmware (stretch) |
| `tools/` | R1 | fake stream + dev viewer |
| `tests/` | all | contract and behaviour tests |

## Rules

Each person works on their own branch; R1 merges `main` every evening, and `main` must always run the demo. Schema changes need R1's approval and a version bump.
