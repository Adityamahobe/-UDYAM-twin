# UDYAM-TWIN virtual machine (Wokwi ESP32)

An ESP32 project for the [Wokwi](https://wokwi.com) simulator that publishes **virtual** machine telemetry over
MQTT to UDYAM-TWIN's **Real Data mode**. No physical sensor or machine is involved. Every message says
`"source": "WOKWI_SIMULATOR"` and `"virtual": true`, and the dashboard labels it **WOKWI SIMULATOR · VIRTUAL DATA**.
It never enters the Testing-mode simulation.

| File | Purpose |
|---|---|
| `sketch.ino` | firmware: Wi-Fi, MQTT, virtual sensors, scenarios, JSON |
| `diagram.json` | the circuit: ESP32 DevKit V1 (`wokwi-esp32-devkit-v1`), DHT22 (D15), MPU6050 (D21 SDA, D22 SCL), SPEED and LOAD potentiometers (D34, D35), SCENARIO button (D4), FAULT LED with 220 Ω (D26) |
| `libraries.txt` | Arduino libraries Wokwi installs |
| `config.h` | machine ID, broker, topic prefix, publish interval (no secrets) |
| `secrets.example.h` | template for a private broker's login (copy to `secrets.h`, which is git-ignored) |
| `sample_message.json` | one message exactly as the sketch sends it (the backend tests use it) |

## Where each value comes from

| Field | Source |
|---|---|
| `temp_c` | Wokwi **DHT22** virtual sensor (click it while running to change the temperature). The `overheat` scenario adds +15 °C in software. `null` in `sensor_dropout`. |
| `rpm` | Wokwi **potentiometer "SPEED"** as a 0–3000 rpm setpoint, plus software noise. Wokwi has no encoder model. |
| `vibration_rms_mm_s` | **Software model:** a baseline, a speed term, and the RMS deviation of the Wokwi **MPU6050** virtual accelerometer from 1 g (change its acceleration while running). `bearing_wear` adds a speed-dependent term. It is *not* an ISO 20816 velocity measurement. |
| `current_a` | **Software model** from speed and the **potentiometer "LOAD"**. Wokwi has no current-sensor model. |

Each message also carries `value_sources` with the same information, so it travels with the data.

## Scenarios and simulated faults

Press the **SCENARIO** button to cycle through them (the red **FAULT** LED is on when the scenario isn't `normal`), or
type in the serial monitor:

```
scenario normal | high_load | bearing_wear | overheat | sensor_dropout
interval 5000        # publish every 5 s (500–60000 ms)
status
```

`simulated_fault` (`bearing_wear`, `overheating`, `temperature_sensor_dropout`) names the condition you selected
in the simulator. The dashboard treats it as a scenario label, never as a verified fault.

## Message

Topic: `<TOPIC_PREFIX>/<MACHINE_ID>/telemetry`, for example `udyam-twin/virtual-machines/machine-001/telemetry`.
The status topic `.../status` carries `online` (retained). The broker publishes the last will `offline` if the device
disappears. See [`sample_message.json`](sample_message.json) for the payload. `ts` is UTC from NTP (`null` until
synchronised); the dashboard stores its own receive time and uses `ts` only when it's plausible.

## Run it

1. Create a new ESP32 project on [wokwi.com](https://wokwi.com/projects/new/esp32). Replace `sketch.ino` and
   `diagram.json` with these files, and add `libraries.txt` and `config.h` as new files (the **+** next to the tabs).
2. **Change `TOPIC_PREFIX` in `config.h`** to something unique, for example `udyam-twin-<your-name>-7f3a/virtual-machines`.
   On a public broker anyone using the same topic would mix into your data.
3. Press **▶**. The serial monitor should show `Wi-Fi: connecting ... connected`, `MQTT: ... connected` and
   `sent #1 normal rpm=...`.
4. Configure the server in `.env` (same broker and prefix) and restart it:

   ```
   UDYAM_WOKWI_BROKER=public.cloud.shiftr.io
   UDYAM_WOKWI_PORT=1883
   UDYAM_WOKWI_USER=public
   UDYAM_WOKWI_PASS=public
   UDYAM_WOKWI_TOPIC_PREFIX=udyam-twin-<your-name>-7f3a/virtual-machines
   ```

5. In the dashboard, switch the header badge to **Real Data mode**. The **Virtual Devices · Wokwi Simulator** panel
   on the Live tab shows the machine, its values, the scenario, the last message time and the MQTT state.

**Keep the Wokwi tab in the foreground.** Browsers throttle background tabs: in our run the simulation dropped to
3 % of real time in the background (85–90 % in front). Messages then arrive far less often than the interval says,
and the dashboard correctly marks the device **Stale**.

**Verified on 26 Oct 2026:**
- This project (sketch with `config.h` inlined, `diagram.json`, `libraries.txt`) compiled and ran on wokwi.com
  without signing in.
- It joined `Wokwi-GUEST`, connected to `public.cloud.shiftr.io` and published on a unique topic prefix.
- The dashboard received 53 messages and rejected none. It showed the device Online, switched to the
  `bearing_wear` scenario sent from Wokwi's serial monitor, and went Offline about 20 s after the simulation
  stopped (the broker's last will).

## Broker notes

- Free Wokwi reaches the internet only (Wokwi's public gateway). A broker on your computer needs Wokwi's paid
  Private Gateway (`host.wokwi.internal`).
- Default broker: `public.cloud.shiftr.io:1883`, login `public` / `public`, which is shiftr.io's published shared
  login, not a secret. Everything on it is public.
- On 26 Oct 2026, from our development network:
  - `public.cloud.shiftr.io`: 2 of 2 full round trips succeeded.
  - `broker.hivemq.com`: no round trip succeeded (it refused, ignored or timed out on connections).
- For a private broker (for example HiveMQ Cloud or EMQX Cloud with TLS), put its login in `secrets.h` (never
  commit it). Note that files in a public Wokwi project are visible. Set the same login on the server with
  `UDYAM_WOKWI_USER` / `UDYAM_WOKWI_PASS` (and `UDYAM_WOKWI_TLS=1`, port 8883). TLS on the ESP32 also needs
  `WiFiClientSecure` instead of `WiFiClient` in the sketch.
- The server validates every message (schema, source, topic and machine match, value ranges, size, rate) and
  counts rejections with the reason. Its login lives only in the server's `.env`, never in the browser.

## The circuit on the dashboard

In Real Data mode, the Virtual Devices panel draws this same circuit from `diagram.json`, using Wokwi's open-source part
graphics (`@wokwi/elements`, MIT, in `web/vendor/wokwi-elements/`). The board is the ESP32 **DevKit V1**: that drawing is
in the open-source package, so Wokwi and the dashboard look the same. Each part is labelled with what it simulates and
its live value. The FAULT LED lights up in a fault scenario. The knobs follow the telemetry and are view-only.
