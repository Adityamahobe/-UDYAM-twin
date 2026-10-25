# Operating modes: Testing and Real Data

The header badge is the mode switch (click it; there is no second selector). The server keeps the mode
(`GET/POST /api/operating-mode`); it starts in **Testing** after every restart.

| | Testing mode | Real Data mode |
|---|---|---|
| Data shown | the virtual plant (software in the loop), as before | readings from real devices only (source `LIVE`) |
| Simulator controls (fault injection, derate, scenarios, reset, Auto Demo, REPLAY, derate approvals) | available | **refused** by the server (409 `real_data_read_only`), over REST and the WebSocket, and greyed out |
| Machines without a real device | simulated values | covered: "No real data … simulated values hidden" |
| Signals a device does not send (e.g. torque) | simulated | shown as "—" (the overlay lists `live_fields`) |
| Line flow, economics, vibration panel of a non-live machine | simulated | covered |
| Alerts / events about non-live machines | shown | hidden, with a count |
| Commands to machines | none exist | none exist |

Entering Real Data mode stops the Auto Demo and REPLAY and resets the background simulation to a healthy baseline,
so test faults cannot raise alerts that look real. The simulation keeps running in the background (tagged `SIM`)
so Testing mode resumes normally.

## Connection states (Real Data mode)

From the MQTT bridge (`hub/mqtt_bridge.py`) and the IoT hub's devices; simulated stand-ins never count.

| State | Meaning |
|---|---|
| Connected | a real device is sending data (MQTT, HTTP or WebSocket), or the broker is connected but no device has sent yet (the banner says which) |
| Connecting | connecting to the MQTT broker |
| Disconnected | the broker connection dropped; paho keeps retrying |
| Error | the broker refused the connection or failed |
| Stale data | real devices were seen but none has sent for > 3 s; values are not live (after 30 s they are nulled) |
| Not configured | no `UDYAM_MQTT_HOST` and no real device has sent data |

MQTT settings come from the environment (`UDYAM_MQTT_HOST`, `UDYAM_MQTT_PORT`, `UDYAM_MQTT_USER`, `UDYAM_MQTT_PASS`);
credentials are never sent to the browser. Topic: `udyam/<device_id>/telemetry`, JSON payload with the device token.

## Storage

Telemetry from real devices goes to the `real_telemetry` table; simulated and replayed telemetry to `telemetry`.
Export them separately: `GET /api/reports/export.csv?section=real_telemetry` and the *Real telemetry* sheet of the
Excel export.

## Simulation controls (Testing mode)

The **Simulation Controls** panel (`web/sim_controls.js`; API `GET /api/sim/controls`, `POST /api/sim/params`,
`POST /api/sim/params/reset`, `POST /api/sim/run`) changes the simulated plant only (`sim/controls.py`).
The plant model has no path to real equipment and the IoT hub has no command channel, so nothing here reaches
a device. In Real Data mode every write is refused (409 `real_data_read_only`).

| Parameter | Machines | Limit | Default |
|---|---|---|---|
| Spindle speed | CNC mill, lathe, bar saw | 40–120 % of nominal rpm (and ≤ max rpm) | 100 % |
| Cutting load | CNC mill, lathe | 50–130 % of nominal torque | 100 % |
| Ambient temperature | CNC mill, lathe | 10–45 °C | 26.85 °C |
| Cooling capacity | CNC mill, lathe | 50–120 % | 100 % |
| Extra vibration | all | 0–8 mm/s RMS | 0 |

- A value outside its limit is refused (422) with the reason; it is never clamped. On CNCs, speed × load must also
  keep the estimated spindle power within the rated kW (e.g. the 7.5 kW lathe refuses 120 % speed at 130 % load).
- The twin is not told about a change. It sees the effect in the telemetry, as it would on a real machine: lower
  cooling shows up as a recalibrated h_cool, a cooling alert and, if the forecast crosses the limit, the existing
  SIM-only auto-derate.
- Parameters are kept across resets and Stop. "Defaults" restores them. Changing a parameter stops the Auto Demo,
  whose scripted story assumes defaults.
- Every change is logged as a `SIM_PARAMS` event (with from/to values); run actions as `SIM_RUN`.

Run control: **Pause** freezes the simulation (no ticks, nothing recorded). **Resume** continues. **Stop** resets
the plant to a clean baseline and stops ticking until **Start**. **Run for N minutes** (≤ 240) runs, then pauses.
Entering Real Data mode always sets the engine running, because real readings reach the dashboard through the
engine tick.

## What-if comparisons

The **What-If Comparison** panel (`web/whatif.js`; API `/api/whatif/*`, `sim/whatif.py`) runs 2–4 scenarios side by
side, each on its own fresh virtual line with a simulated clock. The live simulation and real telemetry are not
touched, so it works in both operating modes.

- **A scenario** is Testing-mode parameters per machine (same limits as the Simulation Controls), optionally one
  fault starting at a given minute, and twin self-healing (the SIM auto-derate) on or off. Presets cover faster
  spindles, a hot day with weak coolant, heavier jobs, and a coolant fault with and without self-healing.
  "Baseline = current settings" copies the live parameters.
- **Same seed:** every scenario uses the same seed, so they start identically and differences come from the
  settings. Identical scenarios give identical results (tested).
- **Outcomes** come from the dashboard's own models:
  - line output and value;
  - site energy, cost, CO₂ and energy per part;
  - OEE and quality of the focus machine;
  - peak temperature and minutes over its limit;
  - peak vibration and minutes in zone C/D;
  - minutes derated or tripped, tool changes, and alerts raised.

  Each scenario shows deltas against the baseline, coloured better or worse, plus a temperature chart. Download
  as CSV or JSON. The AI4I failure-risk model is not evaluated in what-if runs.
- **Results are labelled SIMULATED WHAT-IF.** They compare settings on the virtual line; they are not
  predictions for real machines.

Two model fixes came with this:
- **Spindle speed and line output:** the production line's cycle now follows the Testing-mode spindle speed
  (unchanged at the default 100 %). Faster spindles produce more parts, not only more heat and energy.
- **Honest derate alerts:** when self-healing is off, the twin's alerts now say "20% derate proposed, not applied"
  instead of "auto-derated". A derate's target setpoint is 20 % below the current setpoint, which includes any
  speed setting.

## Local MQTT test broker and end-to-end tests (dev only)

`tools/mqtt_test_broker.py` runs [amqtt](https://github.com/Yakifo/amqtt) as a local test broker. It runs in its own
venv because amqtt pins `websockets==15.0.1`, while the app's venv uses websockets 17 for the dashboard's WebSocket.

```bash
python3 -m venv .venv-mqtt && .venv-mqtt/bin/pip install amqtt==0.12.1
.venv-mqtt/bin/python tools/mqtt_test_broker.py --port 1883                 # anonymous
UDYAM_MQTT_TEST_PASS=... .venv-mqtt/bin/python tools/mqtt_test_broker.py --port 1883 --user udyam
```

- **Safety:** it listens on 127.0.0.1 only and refuses any other address. It never prints the password; the
  password hash lives in a temporary file that is removed on exit.
- **Connecting the app:** start it with `UDYAM_MQTT_HOST=127.0.0.1` and, if needed, `UDYAM_MQTT_PORT`,
  `UDYAM_MQTT_USER` and `UDYAM_MQTT_PASS`, set in the server environment or `.env`, never in the frontend.
- **Devices** publish JSON to `udyam/<device_id>/telemetry` with their token in the payload.
  - A device can send process or air temperature, rpm, vibration, current and power (plus pressure and
    discharge temperature on the compressor). Torque and tool wear are not device fields.
  - So the failure-risk model's drift check and confirmed labels need those two inputs from a real machine
    before they can use LIVE data.

`tests/e2e/test_dual_mode_e2e.py` covers the brief's TEST 1–9 against real processes: the app under uvicorn,
the broker with a password, and HTTP, WebSocket and MQTT clients. Each run uses a temporary database,
synthetic-data folder and model registry, a random broker password and a random operator token. It is excluded
from the default `pytest` run and is skipped (not faked) when `.venv-mqtt` is missing:

```bash
pytest -m e2e -s tests/e2e
```
