# UDYAM-TWIN

A browser-based, self-calibrating digital twin for MSME machine shops: a 4-machine CNC mini line (bar saw → CNC lathe → VMC, plus a shared air compressor), matching the 3D factory model, with one detailed "hero" machine (VMC-01). Built by Team Tech Titans (NIT Raipur) for the INAE Youth Conclave 2026, Theme HT14.

> **What this is and is not.** Everything you see is **simulation** unless a reading is tagged **LIVE** (a real device). Every reading carries its source (`SIM`, `REPLAY` or `LIVE`). Vibration zones use configurable demo thresholds that reference ISO 10816-3; they are not a certified assessment. The failure-risk model is trained on a public synthetic dataset and is not validated on any real machine. Derates are applied in the simulator only; nothing here commands a real machine. ROI figures are illustrative, built from assumptions you can see and edit.

## Quick start

```bash
./run.sh
```

Then open <http://localhost:8000>. The first run needs internet once: it creates `.venv`, installs the packages, downloads the AI4I 2020 dataset from UCI (checks its SHA-256) and trains the failure-risk model (about 5 s). After that it runs **fully offline** (three.js is vendored in `web/vendor/`; the UI has no external URLs, and a test enforces that).

| Command | Does |
|---|---|
| `./run.sh` | prepare if needed, then start on port 8000 |
| `./run.sh --check` | prepare only |
| `./run.sh --test` | prepare, then run the test suite |
| `PORT=8010 UDYAM_AUTO_DEMO=0 ./run.sh` | another port; start with the Auto Demo off |

Python 3.10+ is required. On Windows, run the same steps by hand: `python -m venv .venv`, `.venv\Scripts\pip install -r requirements-dev.txt`, put the AI4I CSV in `ml\data\`, `python -m ml.train_ai4i`, then `python -m uvicorn api.app:app --port 8000`.

| Page | URL |
|---|---|
| Dashboard | `/` |
| Operator view (phone) | `/operator` (the server listens on all interfaces so a phone on the same Wi-Fi can open `http://<laptop-ip>:8000/operator`) |
| IoT hub (devices) | `/iot` |
| API docs | `/docs` |

## How it fits together

One engine, one tick (500 ms), one message per machine. Every 500 ms the engine steps each simulated machine, runs its twin and rules, and emits a **`snapshot`** per machine: readings, ISO zone and the threshold crossed, rule risk, AI4I failure risk with top features, RUL, calibration state, units, and the source tag. Cards, charts, the 3D twin, alerts, work orders, analytics and reports all read that same snapshot, pushed over the WebSocket `ws://<host>/ws/telemetry` (also `GET /api/snapshots`), so two panels never show different numbers for the same quantity. The message contract is in [`docs/SCHEMA.md`](docs/SCHEMA.md) and [`docs/schema.json`](docs/schema.json).

```
sim/ plant models (hidden "true" machines) ──observed──▶ twin/ (residual, calibration, rules, ML, RUL)
        ▲ faults / scenarios / derate (SIM only)                         │
        │                                                              snapshot (one per machine per tick)
api/ commands, alerts + work orders (SQLite), analytics, reports ◀───────┘──▶ WebSocket ──▶ web/ dashboard
hub/ IoT devices (HTTP / WebSocket / MQTT) ──LIVE overlay──▶ observed
```

Every threshold, machine class and site constant lives in [`config/machines.yaml`](config/machines.yaml), validated at startup by `config/loader.py`. `GET /api/config` serves it to the UI.

## Telemetry modes

Choose with `POST /api/mode {"mode": "SIM" | "REPLAY" | "LIVE"}`; `GET /api/mode` shows the current mode and each machine's own source.

| Mode | What feeds the machine | Notes |
|---|---|---|
| **SIM** (default) | the seeded physics-style simulator: rpm, load, temperature and vibration are coupled | deterministic (seed 42); real UTC timestamps |
| **REPLAY** | UCI AI4I 2020 rows, one per tick, into VMC-01 | AI4I has no timestamps, vibration or current, so frames are tagged `replay_time`, vibration/current are null, and each frame shows the row's real failure label next to the model's risk, plus whether the row was held out of training |
| **LIVE** | a real device bound to a machine through the IoT hub | refused with **"No live device connected"** unless a real (not simulated) device is online; never faked |

## Fault lab and Auto Demo (demo simulation)

Four scenarios inject faults into the simulated plant only; the twin is not told and has to notice from the readings. Effects build up and die away gradually.

| Scenario | Machine | You should see |
|---|---|---|
| Vibration fault | VMC-01 | zone A → C (WARN) → D (CRITICAL), auto-derate 20 % (SIM only), calibration frozen |
| Over-temperature | VMC-01 | twin drifts and recalibrates, one cooling-loss alert at 30 %, over-temperature forecast → auto-derate |
| Tool wear | VMC-01 | tool wears 8× faster, RUL shortens, tool-wear alert at 200 min |
| Overload / order surge | VMC-01 | ~92 % load → overload alert, AI4I risk rises (power-failure region); left running, the heat triggers the over-temperature derate |

**Simulate resolution** (`POST /api/scenarios/resolve?machine_id=`) clears the faults, releases the SIM derate, resolves that machine's alerts and closes their work orders. **Reset** (`POST /api/reset`) returns a clean baseline: fresh seeded engine, no open alerts, scenario work orders closed (manual ones kept), charts and energy counters cleared.

**Auto Demo** (on at start; `POST /api/auto_demo?on=false` to stop, which also resets) plays a 90-second story on a loop, identically every cycle: 0 s healthy baseline → 15 s vibration fault → ~21 s zone C alert → ~27 s zone D CRITICAL + auto-derate → 40 s work order generated → 48 s acknowledged → 54 s in progress → 65 s simulated repair (alert resolved, WO closed, derate released) → ~70 s zone A → repeat.

## Alerts and work orders

Each alert card has: what happened · fact (the measured or simulated value and the threshold crossed) · inference · severity · suggested action (cautious wording) · acknowledge state. One alert per rule and machine; repeats update it and an escalation (WARN → CRITICAL) refreshes it. Alerts and work orders live in SQLite (`udyam_twin.sqlite`), so they survive a page refresh and show the same state in every tab. Work orders carry machine, cause, evidence, recommended action and priority, and move Open → Acknowledged → In Progress → Closed (forward only). "Generate work order" (`POST /api/alerts/{id}/work-order`) fills them from the alert and never duplicates.

## Analytics and reports

`GET /api/analytics` (since last reset) and `?window=session` (since server start), all from tick data:
- **OEE** = Availability × Performance × Quality per machine. Availability = share of ticks running; Performance = actual ÷ nominal speed while running (0 while the line station is starved or blocked); Quality = parts made outside a rule-based quality hold ÷ parts made (no inspection data exists in SIM, and the response says so).
- **Energy** = ∑ power × 0.5 s (site total = sum over the machines). **CO₂** = kWh × grid factor (default 0.716 kg/kWh, CEA CO₂ Baseline Database v20, 2024-25). **₹** = kWh × tariff (default ₹8.5/kWh). Both factors are editable at `POST /api/economics`.
- **Projection and ROI** are labelled *illustrative estimate* and list every assumption (operating hours, baseline downtime, an *assumed* downtime reduction, sensor cost).

`GET /api/reports` returns short trends, alert history, the work-order log and the energy/CO₂/OEE summary; `GET /api/reports/export.csv?section=summary|energy|oee|trends|alerts|work_orders` downloads CSV.

## Add your machine

`POST /api/machines` with `template` (cnc_mill, cnc_lathe, bar_saw, air_compressor), `name`, `rated_kw`, `rated_rpm`, `foundation` (rigid/flexible), `iso_class`, optional `zone_boundaries {A_B, B_C, C_D}` and `thresholds`. It is validated with the same rules as `machines.yaml` (a 422 explains what is wrong), appears in every panel at once, is stored in SQLite, and comes back after a reset or a restart. `DELETE /api/machines/{id}` removes it.

## Advisor chatbot (Gemini)

**💬 Ask the Twin** (bottom right of the dashboard) answers questions like *"VMC-01 is heating up — can I finish 40 parts?"*. Google Gemini, called from the server with the official Google Gen AI SDK and function calling, uses the twin's own tools (live state, UDYAM-SIM diagnosis, what-if forecast with the calibrated physics, OEE, ₹ assumptions, dataset statistics) and can only **propose** a derate or work order; an operator approves it through a separate, authorized endpoint, and it is applied in the simulator only. Without a key or internet it answers offline from the same tools (or from a local Ollama model). Setup: `cp .env.example .env`, set `GEMINI_API_KEY`, run `python -m tools.gemini_models` and set `GEMINI_MODEL` to one of the listed IDs, then `./run.sh`. Keys stay server-side; `.env` is git-ignored. Details in [`docs/ADVISOR.md`](docs/ADVISOR.md).

## Connecting a real device (ESP32 path)

1. Open `/iot`, register a device (e.g. `ESP32-01`) and bind it to a machine (e.g. `VMC-01`). Copy the token; it is shown once.
2. Flash [`edge/udyam_node/udyam_node.ino`](edge/udyam_node/udyam_node.ino) (ESP32 + DS18B20 temperature + MPU6050 vibration + ACS712 current; wiring in [`edge/README.md`](edge/README.md)). Set `WIFI_SSID`, `WIFI_PASS`, `HUB_URL` (`http://<laptop-ip>:8000/api/iot/telemetry`), `DEVICE_ID` and `DEVICE_TOKEN`.
3. The device posts readings at 2 Hz. That machine's readings switch to `LIVE` for the signals it measures; the rest stay simulated.

Payload (HTTP `POST /api/iot/telemetry` with headers `X-Device-Id` and `X-Device-Token`, or WebSocket `/ws/device/<device_id>?token=…`):

```json
{"temp_c": 38.25, "vibration_rms_mm_s": 1.1, "current_a": 3.4, "rssi": -55, "fw": "0.1"}
```

**MQTT**: start with `UDYAM_MQTT_HOST=<broker> ./run.sh` (paho-mqtt is in the requirements). Publish the same JSON plus `"token"` to `udyam/<device_id>/telemetry`; the hub maps the device to its bound machine. Example:

```bash
mosquitto_pub -h localhost -t udyam/ESP32-01/telemetry -m '{"token":"<token>","temp_c":38.2,"vibration_rms_mm_s":1.1,"current_a":3.4}'
```

No hardware? `python -m tools.fake_device --url http://localhost:8000 --machine VMC-01` registers a *simulated* device; its readings stay tagged `SIM`, never `LIVE`. Readings are checked for data quality (missing, stale, stuck, out of range); anything but OK freezes the twin's calibration.

## Data and models

| | Source | Use | Results (held-out test) |
|---|---|---|---|
| **AI4I 2020** | UCI ML Repository, [dataset 601](https://archive.ics.uci.edu/dataset/601/ai4i+2020+predictive+maintenance+dataset), CC BY 4.0 (Matzka, 2020); 10,000 rows, 3.39 % failures; SHA-256 checked | Random Forest per target (Machine failure + TWF, HDF, PWF, OSF, RNF) on air temp, process temp, rpm, torque, tool wear; `class_weight="balanced"`, stratified 80/20 split, seed 42. Applied to the CNC mill only (AI4I is a milling dataset). | Machine failure: precision 0.596, recall 0.779, **F1 0.675**, ROC-AUC 0.973, PR-AUC 0.778; 5-fold CV F1 0.670 ± 0.027. HDF F1 0.825, PWF 0.800, OSF 0.737, TWF 0.000, RNF 0.000. Full table: `models/metrics.json`, `GET /api/metrics` |
| **CWRU bearing data** | Case Western Reserve University Bearing Data Center (12k drive-end subset) | Bearing-fault classifier on uploaded vibration waveforms, only for machines configured as bearing/motor assets (the air compressor) | Random Forest macro F1 0.983 on the CWRU test windows (one lab rig; does not transfer to these machines without retraining) |
| **Vibration zones** | `config/machines.yaml` | velocity RMS, 10–1000 Hz; rigid A/B 1.4 · B/C 2.8 · C/D 4.5 mm/s, flexible 2.3 · 4.5 · 7.1 mm/s | configurable demo thresholds referencing ISO 10816-3, marked not verified |
| **UDYAM-SIM v1** (our own) | 440 randomised episodes of this simulator ([`ml/data/UDYAM_SIM_DATASET.md`](ml/data/UDYAM_SIM_DATASET.md)); labels = injected ground-truth faults, features = only what sensors and the twin see | 8-class fault diagnosis + minutes-to-limit for VMC-01; feeds the Advisor chatbot | Macro-F1 **0.993** on unseen episodes (rules 0.857); **0.851** on heavier, never-seen machines (rules 0.659). Coolant loss: rules F1 0.15 vs model 0.98. `ml/sim_metrics.json`, `GET /api/sim-metrics` |
| **3D models** | [`assets/ATTRIBUTION.md`](assets/ATTRIBUTION.md) | VMC geometry derived from TseNC (CERN-OHL-W) | |

Retrain with `python -m ml.train_ai4i`. Rebuild our simulation dataset and its model with `python -m tools.generate_dataset && python -m ml.train_sim` (~15 min). Vibration and current never go into the AI4I model (AI4I has no such columns); they feed the rule engine.

## Tests

```bash
./run.sh --test
```

The suite covers config validation and ISO zones, derate maths, the snapshot and WebSocket schema, the calibration throttle and cooling alert, alert and work-order lifecycles (always on a temporary database), the AI4I pipeline and RUL, every fault-lab scenario and its resolution, reset completeness, three identical Auto Demo cycles, OEE/energy/CO₂/₹ maths, REPLAY and the LIVE guard, reports and CSV, Add Your Machine, and the offline rule.

## Limitations (say these out loud to judges)

- **No real sensor data** unless a device is tagged LIVE. The simulator's behaviour is plausible, not calibrated to a specific machine.
- **Failure risk** is a Random Forest vote share, not a calibrated probability, trained on a synthetic public dataset; good test scores show it learned that dataset, not that it predicts failures on your machines. It cannot predict random (RNF) failures and has few tool-wear examples.
- **ISO zones** are demo thresholds referencing ISO 10816-3 (which formally covers machines above 15 kW); an engineer must verify them for a real machine.
- **OEE quality** is a rule-based quality hold, not inspection data. **ROI** depends on an assumed downtime reduction, not a measured one.
- **Time scales**: readings, forecasts and RUL use real time; the simulator runs tool wear faster than real life so the demo fits in minutes, and the line's parts-per-hour figure keeps the simulator's 30× time compression.
- **Derates** are simulated; on a real machine any derate needs operator approval.
- **UDYAM-SIM** is synthetic data from our own simulator: its scores show the model learned our physics, not real machines (and they drop on unseen heavier machines). The **Advisor** explains tool outputs; it never actuates, and its what-if covers thermal behaviour only.

## Layout

| Folder | What is there |
|---|---|
| `config/` | `machines.yaml` (every threshold, machine class, site constant) and its validating loader |
| `sim/` | plant models, virtual PLC, faults, scenarios, line, analytics, AI4I replay, the engine |
| `twin/` | twin model, residuals, calibration, forecast, snapshot builder |
| `api/` | FastAPI app, alerts and work orders (SQLite), Auto Demo director, IoT and vibration routes |
| `ml/` | AI4I model and training, rules, vibration health (CWRU) |
| `models/` | trained AI4I model and its metrics |
| `hub/`, `edge/` | IoT hub (HTTP, WebSocket, MQTT) and ESP32 firmware |
| `web/` | dashboard, operator view, IoT page, vendored three.js |
| `assets/` | 3D models and attribution |
| `tests/` | the test suite |
| `docs/` | message contract and plans |
