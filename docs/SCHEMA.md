# Message contract v1.1 (v1.0 frozen 18 Oct 2026; v1.1 adds `snapshot`, `alert`, `work_order`)

The machine-readable source of truth is [`schema.json`](schema.json), and the tests validate every message against it. This page is the readable version. **Changes need R1's approval and a version bump.**

Transport: WebSocket `ws://<host>:<port>/ws/telemetry`. Each text frame is one JSON object, and its `type` decides the shape. Port 8765 is the fake stream; 8000 will be the real API.

## On connect

| type | When | Key fields |
|---|---|---|
| `hello` | first frame | `generator` (`fake_stream` / `udyam_core`), `tick_ms` (500) |
| `registry` | second frame, and again whenever machines change (wizard, reset) | `machines[]`: `machine_id`, `template`, `name`, `hero`, `layout {x, y, order}`, `nameplate` |

Machines: `SAW-01` (bar_saw) → `LATHE-01` (cnc_lathe) → `VMC-01` (cnc_mill, **hero**), plus `COMP-01` (air_compressor, `order: -1` = shared utility).

## Every tick (500 ms)

**`observed`**: one per machine, carrying what the machine reports.

| Field | All machines | Notes |
|---|---|---|
| `ts`, `machine_id`, `template` | ✓ | ts = ISO UTC with ms |
| `source_mode` | ✓ | `SIM` / `REPLAY` / `LIVE`: **show it on screen** |
| `plc_state` | ✓ | `IDLE` / `RUN` / `DERATE` / `TRIP` |
| `dq_flag` | ✓ | `OK` / `MISSING` / `STALE` / `OUT_OF_RANGE` / `STUCK` |
| `power_kw`, `current_a`, `vibration_rms_mm_s` | ✓ | number or null |
| `derate_pct`, `active_fault`, `parts_done` | ✓ | `parts_done` is null for the compressor |
| `air_temp_k`, `process_temp_k`, `rpm`, `torque_nm`, `tool_wear_min` | CNC only | match the AI4I 2020 columns; `process_temp_k` is null on sensor dropout |
| `rpm`, `blade_wear_pct` | saw | |
| `pressure_bar`, `discharge_temp_c`, `duty_pct` | compressor | |

**`twin`**: one per machine, carrying what the twin computes.

| Field | Meaning |
|---|---|
| `predicted` | same keys and units as `observed`, only for modelled signals (e.g. `process_temp_k`) |
| `residual` | `{signal, value (obs − pred), ewma, cusum, band, state}`, where state is `IN_BAND` / `DRIFT` / `ANOMALY` |
| `params` | `{version "v1.3", values {h_cool, k_heat, k_wear}, frozen, freeze_reason}`; `values` are multipliers with nominal = 1.0 |
| `forecast` | `{minutes_to_warning, warning}`, e.g. `OVERTEMP` in 8.7 min; nulls when nothing is coming |
| `risk` | `{rules LOW/MEDIUM/HIGH, model 0–1 or null, top_signals[]}`; `model` is the AI4I RF, null for non-CNC |

**`line`**: one per tick: `throughput_parts_per_hr`, `line_loss_inr_per_hr` (an **estimate**), `bottleneck`, `air_supply_ok`, `stations[] {machine_id, state RUNNING/STARVED/BLOCKED/DERATED/DOWN/IDLE, buffer_in, buffer_cap}`.

**`snapshot`** (v1.1): one per machine, the **single source of truth for the UI**. Built by the backend from the same `observed` frame (after any LIVE overlay) and `twin` message, so every panel shows the same number for the same quantity. Also served at `GET /api/snapshots`; thresholds at `GET /api/config`.

| Field | Meaning |
|---|---|
| `ts`, `clock` | ISO UTC; `clock` = `wall_utc` (real time), `sim_time` (tests) or `replay_time` (dataset rows) |
| `mode`, `source_label` | `SIM` / `REPLAY` / `LIVE` and a display label: **show it next to every reading** |
| `state`, `active_scenario`, `dq_flag` | PLC state (`DERATE` while derated), injected fault or null, data quality |
| `rpm`, `rpm_setpoint`, `rpm_nominal`, `derate_pct` | measured rpm scatters around the setpoint; a derate acts on the setpoint (`rpm_setpoint = rpm_nominal × (1 − derate_pct/100)`), so show the setpoint when explaining a derate |
| `vibration_rms_mm_s`, `temp_c`, `temp_signal` | temp in **°C** (process temp for CNCs, discharge temp for the compressor, null otherwise) |
| `load_pct`, `current_a`, `voltage_v`, `power_kw`, `energy_kwh` | load = power / rated kW; energy integrated from power each tick; `voltage_v` is null until a machine reports it |
| `severity` | `{status, zone A–D, threshold {crossed, crossed_value, next, next_value, unit}, reason, label}` from config/machines.yaml |
| `risk` | `{rules, model, top_signals, ml}`; `ml` (CNC mill only, config `ml.failure_model`) = `{status OK / OUT_OF_RANGE / NOT_APPLICABLE / NO_DATA, failure_risk_pct, top_features [{label, value, unit, contribution_pct}], modes_pct, top_mode, out_of_range, calibrated_probability: false, note}`. Show "Failure risk %" with the top features and the note; `model` = risk / 100 |
| `forecast` | as in `twin`; for `TWF_RISK`, `minutes_to_warning` equals `rul.rul_minutes` |
| `rul` | tool-wear RUL **estimate** (CNC): `{status, label "estimate", method, tool_wear_min, limit_min, remaining_tool_min, wear_rate_min_per_h, rul_minutes}`; show `method` with the number; no number when `status` ≠ OK |
| `calibration` | `{version, frozen, freeze_reason, h_cool, cooling_loss_pct}` |
| `units` | unit for every numeric field |

## Whenever something happens

**`event`**: `event_id`, `machine_id` (nullable), `kind`, `severity` (`INFO`/`WARN`/`CRITICAL`), `title`, plus optional `measured` / `inferred` / `suggested` (the three parts of the alert card) and `data`.

| kind | Use in the UI |
|---|---|
| `FAULT_INJECTED`, `RESET`, `MACHINE_ADDED` | event log; `MACHINE_ADDED` → R2 adds the machine to the floor |
| `DRIFT` | "the twin noticed it's wrong" banner |
| `CALIBRATION` | θ-history panel: `data {param, from, to, version, residual_ewma}` |
| `ALERT` | alert card (measured / inferred / suggested); `data.work_order_draft` → offer "Create work order" |
| `ANOMALY` | red card; `data.freeze_reason` explains why calibration stopped |
| `COMMON_CAUSE` | root-cause card; `data.affected` = machines hit by the shared compressor |
| `WORK_ORDER` | work-order log |

## Alerts and work orders (v1.1)

The server keeps alerts and work orders in SQLite, so state survives a page refresh and is the same in every tab.

**`alert`** frame: `{action: raised | updated | acknowledged | resolved, alert}`. Every WARN/CRITICAL `ALERT`, `ANOMALY` or `COMMON_CAUSE` event opens **one** alert per rule and machine; repeats while it is open bump `occurrences` instead of creating a duplicate. The alert keeps the card format: `title` (what happened) · `measured` (fact) · `inferred` · `severity` · `suggested` · `status` (`ACTIVE` → `ACKNOWLEDGED` → `RESOLVED`). Alerts resolve on scenario reset, server restart, `POST /resolve`, or when their linked work order is closed.

**`work_order`** frame: `{action: created | updated, work_order}`. Fields: `wo_id, machine_id, title, cause, evidence, recommended_action, priority (Low/Medium/High/Critical), status, alert_id`, plus `created_at, updated_at, acknowledged_at, started_at, closed_at`. Status moves **forward only**: `Open → Acknowledged → In Progress → Closed` (a step may be skipped; going back or reopening returns 409). The v1.0 `WORK_ORDER` event is still sent on every change.

| REST | Does |
|---|---|
| `GET /api/alerts?include_resolved=false` | open alerts, newest first |
| `POST /api/alerts/{id}/acknowledge` `{by?}` | ACTIVE → ACKNOWLEDGED (409 if resolved) |
| `POST /api/alerts/{id}/resolve` `{resolution?}` | resolve one alert |
| `POST /api/alerts/{id}/work-order` | **Generate Work Order** from the alert (one per alert; asking again returns it with `existing: true`) |
| `GET /api/work-orders?status=` · `GET /api/work-orders/{id}` | list / one |
| `POST /api/work-orders` `{machine_id, title or cause, evidence?, recommended_action?, priority?, alert_id?}` | create (status Open) |
| `PATCH /api/work-orders/{id}` `{status, notes?}` | lifecycle step |

## Fault lab, resolution, reset, Auto Demo (v1.1)

All of this is **demo simulation**: faults are injected into the simulated plant only, and the twin is not told; it has to notice from the readings. Effects build up gradually and die away gradually after a repair.

| Scenario (`GET /api/scenarios`) | Machine | What happens | Alerts (rule) |
|---|---|---|---|
| `vibration_fault` | VMC-01 | vibration climbs A → D over ~10 s | zone C WARN → zone D CRITICAL (`VIB`), auto-derate 20 % (SIM only) |
| `over_temperature` | VMC-01 | cooling capacity falls; twin recalibrates | `COOLING` at 30 % loss, over-temperature forecast → auto-derate (`DERATE`) |
| `tool_wear` | VMC-01 | tool wears 8× faster | `TOOL_WEAR` at 200 min, RUL shortens |
| `overload` | VMC-01 | +26 N·m cutting torque (~97 % load) | `OVERLOAD` above 90 % of rated kW; AI4I risk rises |

Each rule alerts once on entry and once more only if it escalates; a condition must hold `alert_sustain_s` (3 s) to alert or to clear.

| REST | Does |
|---|---|
| `POST /api/scenarios/{id}/start` | start a scenario (`active_scenario` in the snapshot) |
| `POST /api/scenarios/resolve?machine_id=` | **Simulate resolution**: clear faults, release SIM derate, resolve that machine's alerts, close their work orders (`RESOLUTION` event). No `machine_id` = every faulted machine |
| `POST /api/reset` | **clean baseline**: fresh seeded engine, all alerts resolved, scenario work orders closed (manual ones kept), trends and energy counters cleared (`RESET` event) |
| `GET /api/auto_demo` · `POST /api/auto_demo?on=` | Auto Demo status / toggle. Off → clean baseline |

**Auto Demo** (90 s, loops, deterministic: each cycle starts from a reset of the seeded engine and runs on tick count): 0 s baseline · 15 s vibration fault · ~21 s zone C alert · ~27 s zone D CRITICAL + auto-derate · 40 s work order generated · 48 s acknowledged · 54 s in progress · 65 s simulated repair (alert resolved, WO closed, derate released) · ~76 s zone A · 90 s repeat. Each step is narrated with a `DEMO` event.

## Telemetry mode, analytics, reports, Add Your Machine (v1.1)

**Mode** (`GET /api/mode`, `POST /api/mode {mode, machine_id?, start_row?}`): show the current mode in the header and each machine's own source (`sources`, and `snapshot.mode`).
- `SIM`: the seeded simulator.
- `REPLAY`: UCI AI4I 2020 rows, one per tick, into a CNC mill (default VMC-01). AI4I has no timestamps, vibration or current: replayed snapshots have `clock: "replay_time"`, null vibration/current, and `replay {udi, product_id, variant, labels{machine_failure, TWF…RNF}, held_out}` so risk can be shown next to the row's real label. `held_out: false` means the model saw that row in training. No drift detection, calibration or auto-derate on replayed rows.
- `LIVE`: allowed only when a real (non-simulated) device is online and bound; otherwise **409 "No live device connected"**. LIVE readings arrive through the IoT overlay on the bound machine only.

**Analytics** (`GET /api/analytics?window=window|session`; `window` = since last reset, `session` = since server start), all from 500 ms ticks:
- per machine: `energy_kwh`, `co2_kg`, `cost_inr`, `mean_power_kw`, `oee {availability, performance, quality, oee, parts, parts_quality_hold}` (CNCs and saw);
- `site`: energy / CO₂ / ₹ (site energy = sum over machines), `line_oee` (VMC-01, the pacing station);
- `factors`: grid CO₂ factor and tariff with their sources (edit with `POST /api/economics`);
- `projection` and `roi`: **labelled "illustrative estimate"** with every assumption listed; the downtime reduction is an input assumption, never a result;
- `method`: how each number is computed. Quality is a rule-based quality hold (parts completed while rule risk is HIGH or a tool-wear warning is active), not inspection data.

**Reports**: `GET /api/reports?trend_minutes=10` (trends every 2 s for up to 30 min, alert history, work-order log, energy/CO₂/OEE for both windows) and `GET /api/reports/export.csv?section=summary|energy|oee|trends|alerts|work_orders`.

**Add Your Machine**: `POST /api/machines {template, name, rated_kw, rated_rpm, foundation rigid|flexible, iso_class, zone_boundaries {A_B, B_C, C_D}, thresholds {…}, x, y}`. Templates: cnc_mill, cnc_lathe, bar_saw, air_compressor. Everything is validated with the same rules as `config/machines.yaml` (422 with the reason). Zone boundaries create an "Operator-entered thresholds — not a certified assessment" profile. The machine gets an id (e.g. VMC-02), appears in the registry (`custom: true`), snapshots, severity, analytics and reports, is stored in SQLite, and is re-created after every reset and server restart. `DELETE /api/machines/{id}` removes it (built-in machines: 409). Over the WebSocket, `{"cmd": "add_machine", …}` takes the same fields and answers a bad request with `{"type": "error", "cmd": "add_machine", "detail": …}`.

## Commands (client → server)

```json
{"cmd": "inject_fault", "machine_id": "VMC-01", "fault": "coolant_degradation"}
{"cmd": "clear_faults", "machine_id": "VMC-01"}
{"cmd": "set_derate", "machine_id": "VMC-01", "pct": 20}
{"cmd": "add_machine", "template": "air_compressor", "name": "Air Compressor", "nameplate": {"rated_kw": 7.5}}
{"cmd": "auto_demo", "on": false}
{"cmd": "reset"}
```

Faults: `coolant_degradation`, `bearing_defect`, `overload`, `tool_wear_accel`, `sensor_stuck`, `sensor_dropout`, `order_surge`, `pressure_drop`, `blade_wear`.

## Two rules everyone must follow

1. **Vibration and current never go into the AI4I model.** They feed the rule engine only (AI4I has no such columns).
2. **The coolant / over-temperature story uses the physics rule `OVERTEMP`, not AI4I's HDF.** AI4I defines heat-dissipation failure as a *small* air-to-process temperature gap at low RPM. Coolant loss makes the gap *bigger*, so calling it "HDF" would contradict the dataset, and a judge who knows AI4I would catch it.
