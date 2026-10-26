# UDYAM-TWIN — Frontend & Backend Integration Contract
**Standard Operating Contract v1.2**  
*Unified Architecture, REST API Specification and WebSocket Streaming Protocol*

> Updated at integration (branch `frontend-integration`): the page is served by the backend, every figure comes from it, there is no simulated fallback, and the Control Center hosts the features the redesign had not covered. Corrections are marked in §2, §3 and §6.

---

## 1. Executive Summary & Architecture Overview

UDYAM-TWIN is a digital twin command center engineered for precision brownfield machine shops. It bridges physical/virtual shop-floor machinery with real-time kinematic simulation, recursive physics calibration, AI-driven failure prediction, and reinforcement-learning decision support.

The system is architected into three strictly decoupled layers:
```
┌───────────────────────────────────────────────────────────────────────────┐
│                    FRONTEND CLIENT (HTML5 / Three.js)                     │
│  - Apple-inspired design system (Clean light canvas, Bento cards, 24px r) │
│  - Interactive 3D Factory Kinematics (Three.js WebGL & OrbitControls)     │
│  - Centralized UdyamAPI Client (web/api_service.js) with auto-reconnect   │
│  - No simulated fallback: offline is shown as offline                     │
└──────────────────────┬────────────────────────────▲───────────────────────┘
                       │ REST Requests              │ WebSocket Stream
                       │ (fetch JSON / HTTP)        │ (/ws/telemetry)
┌──────────────────────▼────────────────────────────┴───────────────────────┐
│                     BACKEND ENGINE (FastAPI / Python)                     │
│  - High-throughput asynchronous server (api/app.py)                       │
│  - Real-time 500 ms engine clock (sim/factory.py)                         │
│  - Digital Twin Recursive Least Squares (RLS) tracking (twin/model.py)    │
│  - AI4I 2020 Random Forest & NASA C-MAPSS / IMS degradation models (ml/)   │
│  - IoT Gateway, MQTT bridge & Virtual ESP32 Wokwi emulation (hub/, wokwi/) │
└─────────────────────────────────────┬─────────────────────────────────────┘
                                      │ SQLite Database
┌─────────────────────────────────────▼─────────────────────────────────────┐
│                       PERSISTENCE (udyam_twin.db)                         │
│  - Work orders & lifecycle states (Open → Ack → In Progress → Closed)      │
│  - Deduplicated active alerts & incident timelines                        │
│  - Historical parameter logs (h_cool, k_heat, k_wear) & RL decisions      │
└───────────────────────────────────────────────────────────────────────────┘
```

Both frontend and backend developers can run and develop against this contract independently:
- **Frontend developers** can run the client against a live FastAPI server, or rely on `web/api_service.js`'s built-in offline simulation generator when working on isolated visual components.
- **Backend developers** can validate payloads and endpoints against the schema specifications defined in this document and `docs/SCHEMA.md`.

---

## 2. Environment Configuration & Transport Layer

### 2.1 Serving and base URLs
- The backend serves the frontend: `GET /` → `web/index.html`; files under `/static/…` (from `web/`) and 3D assets under `/assets/…` (from `assets/`). Start the backend and open `http://localhost:8010` (port from `.claude/launch.json` / `PORT`).
- `web/api_service.js` resolves the API origin in this order: `window.API_BASE_URL`, `localStorage["UDYAM_API_BASE_URL"]` (Connection modal), the page's own origin. Leave both unset in normal use.
- WebSocket: the same origin with `ws:`/`wss:`, path `/ws/telemetry`.
- Everything is local: three.js, Firebase SDK and images are vendored; `tests/test_offline.py` enforces it (plain `<a href>` citation links are allowed).

### 2.2 Headers & authentication
- **Sign-in (Firebase):** when `GET /api/auth/config` says `enabled: true`, `index.html` sends signed-out visitors to `/static/login.html` and shows **Sign out**. `api_service.js` adds `Authorization: Bearer <Firebase ID token>` to every request. The backend verifies it on `GET /api/auth/me`; other endpoints are not protected yet (`require_user` in `api/firebase_auth.py`). See `docs/AUTH_API.md`.
- **X-Operator-Token:** needed only when the server sets `UDYAM_OPERATOR_TOKEN`, for operator approvals (model promotion and rollback, Advisor proposal approval, label retraction).
- `Content-Type: application/json` on JSON requests.

---

## 3. Frontend-Backend Feature Matrix (verified at integration)

| Capability | Backend | Where in the UI | Status |
|---|---|---|---|
| Live telemetry, machine cards, station pills, hero HUD, 3D inspector | `WS /ws/telemetry`, `GET /api/snapshots` | Hero, Factory, Machines | Live |
| Line output, availability, downtime, open tasks, owner answers | `GET /api/analytics`, `GET /api/work-orders`, snapshots | Dashboard, Status | Live (was fixed demo numbers) |
| Model test metrics | `GET /api/metrics` | Predictive AI | Live (fixed 98.5 % / 98.7 % claims removed) |
| Alerts and work orders | `/api/alerts…`, `/api/work-orders…` | Alerts, Work Orders modal | Real REST calls (no local copies) |
| Fault lab, scenarios, reset, auto demo | `/api/faults/*`, `/api/scenarios/*`, `/api/reset`, `/api/auto_demo` | Control bar, Fault Lab | Real REST calls; Real Data mode answers 409 |
| What-if | `POST /api/whatif/run`, `GET /api/whatif/job`, `GET /api/whatif/results/{id}[/export]` | Dashboard what-if box | Real job and results (was computed in the browser) |
| Tool change | `POST /api/tool-wear` (`measured_wear_min`) | Machine modal | Fixed field name |
| Vibration, IoT, RL proposals, C-MAPSS / IMS, chat | `/api/vibration…`, `/api/iot…`, `/api/rl…`, `/api/datasets/*`, `/api/chat…` | Modals, Predictive AI | Real |
| Simulation Controls | `/api/sim/*` | Control Center · Simulation | Existing panel |
| Factory Run Designer, Factory Dataset | `/api/factory-run/*`, `/api/factory-dataset/*` | Control Center · Simulation | Existing panels |
| Factory Learning, AI4I model registry, synthetic data, adaptive learning, RL policy | `/api/factory-learning/*`, `/api/models/ai4i*`, `/api/synthetic/*`, `/api/adaptive*`, `/api/rl*` | Control Center · Models & Learning | Existing panels |
| Wokwi virtual devices | `/api/wokwi*` | Control Center · Real Data | Existing panel (shows in Real Data mode) |
| Reports & export, live records | `/api/reports/*` | Control Center · Reports | Existing panels |
| Operating modes | `/api/operating-mode`, `/api/mode` | Mode pill; Control Center follows it | Real |
| Sign-in | `/api/auth/config`, `/api/auth/me` | Login page, Sign out | Real |
| Add Your Machine | `POST /api/machines` → **403 `feature_disabled`** | "Future scope" label | Off by design (four default machines) |

The previous dashboard is kept at `/static/legacy/index.html`.

---

## 4. REST API Specification

### 4.1 System & Configuration
- **`GET /api/health`**
  - **Description:** Server liveness check and runtime environment info.
  - **Response 200:**
    ```json
    { "status": "ok", "mode": "TESTING", "timestamp": "2026-10-26T12:00:00Z" }
    ```
- **`GET /api/config`**
  - **Description:** Factory metadata, machine nameplates, vibration bands, and stream settings.
- **`GET /schema.json`**
  - **Description:** JSON schema defining all v1.1 telemetry frames.

### 4.2 Machine Registry & Snapshots
- **`GET /api/registry`**
  - **Description:** Active machines on the shop floor (built-in + SQLite custom machines).
  - **Response 200:**
    ```json
    {
      "generator": "udyam_core",
      "machines": [
        { "machine_id": "SAW-01", "template": "bar_saw", "name": "Bar Saw", "hero": false },
        { "machine_id": "LATHE-01", "template": "cnc_lathe", "name": "CNC Lathe", "hero": false },
        { "machine_id": "VMC-01", "template": "cnc_mill", "name": "CNC Milling Machine", "hero": true },
        { "machine_id": "COMP-01", "template": "air_compressor", "name": "Air Compressor", "hero": false }
      ]
    }
    ```
- **`POST /api/machines`**
  - **Description:** Onboard custom machine to the factory floor.
  - **Request Body:**
    ```json
    {
      "template": "cnc_mill",
      "name": "VMC-02 Heavy Milling",
      "rated_kw": 15.0,
      "rated_rpm": 6000,
      "foundation": "rigid",
      "iso_class": "Class 2",
      "zone_boundaries": { "A_B": 1.4, "B_C": 2.8, "C_D": 4.5 }
    }
    ```
  - **Response 201:** Created machine object with generated `machine_id`.
- **`DELETE /api/machines/{machine_id}`**
  - **Description:** Remove a custom machine. Built-in machines (`SAW-01`, `LATHE-01`, `VMC-01`, `COMP-01`) return `409 Conflict`.
- **`GET /api/snapshots`**
  - **Description:** High-level unified snapshot array for all machines. Single source of truth for the UI.

### 4.3 Fault Injection, Scenarios & Simulation Controls
- **`POST /api/faults/inject`**
  - **Request Body:** `{"machine_id": "VMC-01", "fault": "coolant_degradation"}`
  - **Supported Faults:** `coolant_degradation`, `bearing_defect`, `overload`, `tool_wear_accel`, `sensor_stuck`, `sensor_dropout`, `order_surge`, `pressure_drop`, `blade_wear`.
- **`POST /api/faults/clear`**
  - **Request Body:** `{"machine_id": "VMC-01"}` (null clears all machines).
- **`POST /api/derate`**
  - **Request Body:** `{"machine_id": "VMC-01", "pct": 20.0}`
- **`GET /api/scenarios`**
  - **Response 200:** List of pre-built scenarios (`vibration_fault`, `over_temperature`, `tool_wear`, `overload`).
- **`POST /api/scenarios/{id}/start`**
  - **Description:** Trigger pre-built fault escalation sequence.
- **`POST /api/scenarios/resolve`**
  - **Query Params:** `?machine_id=VMC-01` (optional; defaults to all faulted machines).
  - **Description:** Clear faults, restore normal speeds, resolve active alerts, and close scenario work orders.
- **`POST /api/reset`**
  - **Description:** Reinitialize seeded factory engine to nominal baseline.
- **`GET/POST /api/auto_demo?on=true|false`**
  - **Description:** Toggle 90-second deterministic loop with narrated `DEMO` events.

### 4.4 Alerts & Maintenance Work Orders
- **`GET /api/alerts?include_resolved=false`**
  - **Response 200:** Array of active alert cards.
    ```json
    [
      {
        "alert_id": "alt_8842",
        "machine_id": "VMC-01",
        "title": "Elevated Spindle Vibration · Zone C",
        "severity": "WARN",
        "status": "ACTIVE",
        "rule": "VIB",
        "measured": "Vibration RMS 2.92 mm/s (threshold: 2.80 mm/s)",
        "inferred": "Probable tool unbalance or holder resonance",
        "suggested": "Inspect cutting tool clamping, verify runout, or derate speed by 20%",
        "occurrences": 3,
        "created_at": "2026-10-26T12:00:00Z"
      }
    ]
    ```
- **`POST /api/alerts/{id}/acknowledge`**
  - **Request Body:** `{"by": "Operator"}`
  - **Description:** Transition alert from `ACTIVE` to `ACKNOWLEDGED`.
- **`POST /api/alerts/{id}/resolve`**
  - **Request Body:** `{"resolution": "Tool collet inspected and cleaned"}`
- **`POST /api/alerts/{id}/work-order`**
  - **Description:** One-click generation of work order from an alert. Returns existing order if already generated.
- **`GET /api/work-orders?status=`**
  - **Description:** List work orders, optionally filtered by status (`Open`, `Acknowledged`, `In Progress`, `Closed`).
- **`POST /api/work-orders`**
  - **Request Body:**
    ```json
    {
      "machine_id": "VMC-01",
      "title": "Inspect Spindle BT40 Tool Holder & Runout",
      "priority": "Medium",
      "recommended_action": "Clean collet bore and check tool taper",
      "alert_id": "alt_8842"
    }
    ```
- **`PATCH /api/work-orders/{id}`**
  - **Request Body:** `{"status": "In Progress", "notes": "Technician on site"}`
  - **Note:** Status moves forward only: `Open → Acknowledged → In Progress → Closed`.
- **`POST /api/tool-wear`**
  - **Request Body:** `{"machine_id": "VMC-01", "initial_wear_min": 0.0}`
  - **Description:** Record tool replacement and reset accumulated wear minutes.

### 4.5 What-If Simulation Sandbox
- **`GET /api/whatif/options`**
  - **Description:** Available scenarios, durations, speed multipliers, and configurable machine parameters.
- **`POST /api/whatif/run`** (Status 202 Accepted)
  - **Request Body:**
    ```json
    {
      "duration_min": 10,
      "speed": 100,
      "scenarios": [
        { "name": "Baseline Line", "params": {}, "auto_derate": true },
        { "name": "Degraded Coolant", "params": { "VMC-01": { "cooling_capacity_pct": 65 } }, "auto_derate": true }
      ]
    }
    ```
- **`GET /api/whatif/job`**
  - **Description:** Poll simulation progress (`{"state": "running"|"done"|"error", "progress_pct": 75, "rid": "wi_20261026_01"}`).
- **`GET /api/whatif/results/{rid}`**
  - **Description:** Returns side-by-side KPI comparison (parts output, line value ₹, energy kWh, peak temp, minutes in Zone C/D).
- **`GET /api/whatif/results/{rid}/export?format=csv|json`**
  - **Description:** Download comparison data as CSV or JSON file.

### 4.6 Reinforcement Learning Maintenance Policy
- **`GET /api/rl`**
  - **Response 200:** Policy training state, exploration rate $\varepsilon$, total decisions evaluated, and mean reward ₹.
- **`GET /api/rl/metrics`**
  - **Response 200:** Policy learning curve and reward comparisons vs baseline rules.
- **`POST /api/rl/{machine_id}/propose`**
  - **Description:** Requests the RL agent's recommended action for the selected machine (`continue`, `derate_20`, `derate_40`, `inspect`, `maintain`) along with Q-values.

### 4.7 IoT Hardware Gateway & Wokwi Emulation
- **`GET /api/iot/status`**
  - **Response 200:** `{ "mqtt_connected": true, "devices_online": 2, "broker": "broker.emqx.io" }`
- **`GET /api/iot/devices`**
  - **Description:** List registered physical and virtual ESP32 devices.
- **`POST /api/iot/devices/{id}/bind`**
  - **Request Body:** `{"machine_id": "VMC-01"}`
- **`POST /api/iot/devices/{id}/tare`**
  - **Description:** Align sensor probe readings against the twin's calibrated baseline offset.

### 4.8 Vibration Diagnostics (ISO 20816-3)
- **`GET /api/vibration`**
  - **Description:** Factory-wide vibration summary and ISO zone counts.
- **`GET /api/vibration/{machine_id}`**
  - **Response 200:**
    ```json
    {
      "machine_id": "VMC-01",
      "vibration_rms_mm_s": 2.30,
      "zone": "B",
      "iso_class": "Class 2",
      "harmonics": {
        "1x_rpm": { "hz": 25.6, "amp_mm_s": 1.12, "cause": "Spindle unbalance" },
        "2x_rpm": { "hz": 51.2, "amp_mm_s": 0.48, "cause": "Coupling alignment" },
        "bpfo": { "hz": 1280.0, "amp_mm_s": 0.18, "cause": "Bearing outer race" }
      },
      "crest_factor": 3.4,
      "kurtosis": 3.1
    }
    ```
- **`POST /api/vibration/{machine_id}/recording`**
  - **Description:** Triggers a 10 kHz high-speed waveform capture for spectral analysis.

### 4.9 AI Assistant (Advisor) Copilot
- **`GET /api/chat/status`**
  - **Response 200:** `{ "mode": "gemini"|"ollama"|"offline", "model": "gemini-1.5-flash", "available": true }`
- **`POST /api/chat`**
  - **Request Body:** `{"message": "VMC-01 is heating up. Can I finish 40 parts?", "conversation_id": "conv_123"}`
  - **Response 200:**
    ```json
    {
      "conversation_id": "conv_123",
      "reply": "VMC-01 process temp is 52.4 °C with nominal coolant. You have 17.6 °C margin before the warning limit. Finishing 40 parts will take ~18 min and is safe.",
      "tools_used": ["factory_engine.snapshot", "analytics.summary"],
      "telemetry_mode": "SIM",
      "latency_s": 0.42,
      "proposals": [
        {
          "proposal_id": "prop_4410",
          "action": "set_derate",
          "description": "Derate VMC-01 spindle by 20% to keep temperature under 55 °C"
        }
      ]
    }
    ```
- **`POST /api/chat/proposals/{proposal_id}/approve`**
  - **Headers:** `X-Operator-Token` (optional if unconfigured)
  - **Request Body:** `{"operator": "Dashboard Operator"}`
  - **Description:** Executes the proposed action once in the simulator / work-order log.
- **`POST /api/chat/proposals/{proposal_id}/dismiss`**
  - **Description:** Dismisses the proposal without executing.

---

## 5. Real-Time WebSocket Streaming Protocol (`/ws/telemetry`)

The WebSocket interface streams bidirectional frames encoded as UTF-8 JSON objects.

### 5.1 Connection Handshake Sequence
Upon connection, the server immediately sends two initial frames:
1. **`hello` Frame:**
   ```json
   { "type": "hello", "generator": "udyam_core", "version": "1.1", "tick_ms": 500, "mode": "TESTING" }
   ```
2. **`registry` Frame:**
   ```json
   {
     "type": "registry",
     "machines": [
       { "machine_id": "SAW-01", "name": "Bar Saw", "template": "bar_saw", "hero": false },
       { "machine_id": "LATHE-01", "name": "CNC Lathe", "template": "cnc_lathe", "hero": false },
       { "machine_id": "VMC-01", "name": "CNC Milling Machine", "template": "cnc_mill", "hero": true },
       { "machine_id": "COMP-01", "name": "Air Compressor", "template": "air_compressor", "hero": false }
     ]
   }
   ```

### 5.2 Periodic Broadcast Frames (Every 500 ms)
- **`snapshot` Frame (Primary UI Feed):**
  ```json
  {
    "type": "snapshot",
    "ts": "2026-10-26T12:00:00.500Z",
    "clock": "wall_utc",
    "machine_id": "VMC-01",
    "mode": "SIM",
    "source_label": "Digital Twin SIM",
    "state": "RUN",
    "dq_flag": "OK",
    "rpm": 1540,
    "rpm_setpoint": 1540,
    "rpm_nominal": 1540,
    "derate_pct": 0,
    "vibration_rms_mm_s": 2.30,
    "temp_c": 38.5,
    "temp_signal": "process_temp",
    "load_pct": 68.4,
    "current_a": 13.8,
    "power_kw": 7.52,
    "energy_kwh": 348.2,
    "severity": { "status": "NORMAL", "zone": "A", "label": "Zone A" },
    "risk": { "rules": "LOW", "model": 0.06, "ml": { "failure_risk_pct": 6.2 } },
    "rul": { "status": "OK", "label": "estimate", "tool_wear_min": 142, "limit_min": 240, "rul_minutes": 122 },
    "calibration": { "version": "v1.3", "frozen": false, "h_cool": 1.0, "cooling_loss_pct": 0.0 }
  }
  ```
- **`line` Frame (Cell-Level Metrics):**
  ```json
  {
    "type": "line",
    "throughput_parts_per_hr": 42.0,
    "line_loss_inr_per_hr": 0,
    "bottleneck": "VMC-01",
    "air_supply_ok": true,
    "stations": [
      { "machine_id": "SAW-01", "state": "RUNNING", "buffer_in": 4, "buffer_cap": 12 },
      { "machine_id": "LATHE-01", "state": "RUNNING", "buffer_in": 5, "buffer_cap": 12 },
      { "machine_id": "VMC-01", "state": "RUNNING", "buffer_in": 3, "buffer_cap": 12 },
      { "machine_id": "COMP-01", "state": "RUNNING", "buffer_in": 0, "buffer_cap": 0 }
    ]
  }
  ```

### 5.3 Client Control Commands (Client → Server)
The frontend can send JSON commands over the WebSocket connection:
```json
{"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation"}
{"cmd": "clear_faults", "machine_id": "VMC-01"}
{"cmd": "set_derate", "machine_id": "VMC-01", "pct": 20}
{"cmd": "start_scenario", "scenario": "vibration_fault"}
{"cmd": "resolve", "machine_id": "VMC-01"}
{"cmd": "reset"}
{"cmd": "auto_demo", "on": true}
{"cmd": "add_machine", "template": "cnc_mill", "name": "VMC-02", "nameplate": {"rated_kw": 11.0}}
```

---

## 6. When the backend is unreachable

There is no simulated fallback. If the WebSocket drops, the status pill shows "Backend offline · reconnecting…", data stops updating, and the client reconnects with back-off. A failed request rejects with `{status, message, data}`: `status` is the HTTP code, or 0 when the server could not be reached, and `message` is the backend's `detail.message`. The UI shows it and never shows a local "success". (`SimulationEngine` and `handleFallbackEndpoint` remain in `api_service.js`, unused, for design previews without a server.)

---

## 7. Instructions for Backend Developers

1. **Keep Payloads Consistent with schema.json:**
   The frontend expects keys matching schema v1.1 (`rpm`, `temp_c`, `vibration_rms_mm_s`, `severity.zone`, `load_pct`). Do not rename fields without a corresponding version bump.
2. **Always Include Severity & Zone:**
   Every snapshot should include the ISO 20816-3 zone (`A`, `B`, `C`, or `D`) inside `severity.zone`.
3. **Graceful Handling of Unbound Devices:**
   When operating mode is `LIVE` but no physical device is bound, return HTTP `409 Conflict` with detail message: `"No live device connected"`.
4. **CORS Headers:**
   Ensure `CORSMiddleware` in `api/app.py` allows origins for localhost dev (`http://localhost:*`, `http://127.0.0.1:*`).
5. **Static File Serving:**
   FastAPI mounts `web/` directly at `/static` and serves `web/index.html` at `GET /`. All assets in `web/` must maintain relative paths for standalone portability.
