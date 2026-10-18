# Message contract v1.0 (frozen 18 Oct 2026)

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
