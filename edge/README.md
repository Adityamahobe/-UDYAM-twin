# edge/ (R5, stretch)

ESP32 + DS18B20 (housing temp) + MPU6050 (vibration) + ACS712/SCT-013 (current) on a desk motor/fan, publishing `observed` messages with `source_mode: "LIVE"`. Read-only. Only start after the Fri 23 Oct feature freeze passes.

## IoT hub

The API includes an IoT hub ([`hub/`](../hub/), endpoints in [`api/iot.py`](../api/iot.py), page at `http://localhost:8000/iot`).

1. **Register** a device on `/iot` and link it to a machine (e.g. `VMC-01`). Copy the token; it is shown only once.
2. **Send readings** by any of these transports. All of them take the same JSON:
   ```json
   {"temp_c": 38.25, "vibration_rms_mm_s": 2.1, "current_a": 3.4, "rssi": -55, "fw": "0.1"}
   ```
   | Transport | How |
   |---|---|
   | HTTP | `POST /api/iot/telemetry` with headers `X-Device-Id`, `X-Device-Token` |
   | WebSocket | `ws://<hub>:8000/ws/device/<device_id>?token=<token>`, one reading per frame |
   | MQTT | topic `udyam/<device_id>/telemetry`, add `"token"` to the JSON; start the API with `UDYAM_MQTT_HOST=<broker>` (needs `pip install paho-mqtt`) |
3. **Tare** on `/iot`. The housing probe reads lower than the twin's process temperature, so Tare sets an offset that makes the current reading match the twin's prediction. After that, a change in the real temperature shows up as a residual.

From then on, that machine's `observed` frames carry the device's values with `source_mode: "LIVE"`. Signals the device doesn't measure (RPM, torque, tool wear) still come from the simulator.

### Data quality

| Condition | `dq_flag` |
|---|---|
| Fresh reading, values in range | `OK` |
| A sent field is null, or the DS18B20 error value 85.0 °C | `MISSING` |
| Value outside its physical range (e.g. −127 °C, the disconnected probe) | `OUT_OF_RANGE` |
| 60 identical temperatures in a row | `STUCK` |
| No reading for 3 s (last values held) | `STALE` |
| No reading for 30 s (live values become null) | `MISSING` |

Any flag other than `OK` freezes the twin's calibration, the same as the simulated sensor faults.

### Accepted keys

`temp_c` / `temp_k` (becomes `process_temp_k` on CNCs, `discharge_temp_c` on the compressor), `air_temp_c` / `air_temp_k`, `vibration_rms_mm_s`, `current_a`, `power_kw`, `rpm`, `pressure_bar`. A key is dropped if the linked machine's template doesn't have that signal. Other keys (`rssi`, `fw`) are kept for display only.

### Simulated devices

Register a software stand-in with `"simulated": true`. Its readings still replace the plant simulator's, but they stay `source_mode: "SIM"` and are never shown as measured. `tools/fake_device` does this.

### Vibration waveforms

Devices can also upload raw acceleration waveforms to `POST /api/iot/waveform` (same headers) for vibration features, as described in [`ml/vibration/README.md`](../ml/vibration/README.md). The firmware's `vibration_rms_mm_s` is an approximation: it converts acceleration RMS to velocity assuming a single 25 Hz frequency. It is useful for trends but not for severity assessment.

## Firmware

[`udyam_node/udyam_node.ino`](udyam_node/udyam_node.ino) (Arduino IDE, ESP32 core). It needs these libraries: OneWire, DallasTemperature, Adafruit MPU6050. Set the Wi-Fi details, `HUB_URL`, `DEVICE_ID` and `DEVICE_TOKEN` at the top. Wiring: DS18B20 on GPIO4 (4.7 kΩ pull-up), MPU6050 on I²C (GPIO21/22), ACS712 output to GPIO34 through a 10k/10k divider.

## No hardware?

```bash
python -m tools.fake_device --machine VMC-01 --heat 3
```
This registers `ESP32-SIM` and posts readings at 2 Hz. `--heat 3` adds a 3 K rise over 60 s, like holding a hand over the fan.
