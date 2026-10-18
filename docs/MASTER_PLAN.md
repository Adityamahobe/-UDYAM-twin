# UDYAM-TWIN — Master Plan

**Team:** Tech Titans (NIT Raipur) · **Event:** INAE Youth Conclave 2026 (YEISS-2026), Hackathon, 26–27 Oct 2026
**Theme:** HT14 — Digital Twin Applications for MSMEs and Smart Factories (also touches HT4 and HT13)
**Plan written:** 17 Oct 2026 · **Revised:** 18 Oct 2026 to add the hybrid self-calibrating twin, the CNC workshop target and the TseNC asset (8 days to go)

---

## 0. The one-line idea (use it everywhere)

> **"Start virtual, earn trust, then connect."**
> UDYAM-TWIN is a zero-licence-cost, browser-based 3D digital twin with a virtual PLC. It lets an MSME test faults, read explained alerts and see ₹ impact on a simulated machine first. It then moves the same software to a real machine in read-only stages.

There are two submissions:

| | **The PPT** (bold pitch) | **The Report** (careful research proposal) |
|---|---|---|
| Tone | Vision, impact, Viksit Bharat | Evidence, safety, honest limits |
| Says | Self-healing auto-throttle, RUL, ₹ & CO₂ savings, < ₹2,500 node, < 3-month ROI, 60 FPS | Advisory-only, risk instead of RUL, savings stay hypotheses until measured, staged A→D roadmap |

### Locked decisions (18 Oct)

| Decision | Choice | Why it doesn't drift from what we submitted |
|---|---|---|
| Target MSME | **Small CNC machining job-shop** (auto-ancillary / fabrication supplier) | The PPT already names CNC machining and automotive ancillaries; AI4I is a machining/tool-wear scenario |
| Physical asset | **One 3-axis CNC milling machine**, plus a small job (workpiece → parts) | The report's focus is "one machine, few signals, clear workflow" |
| Primary objective | **Predictive maintenance**; production KPIs (parts, cycle time, OEE, ₹) are secondary | PS = "monitor, simulate faults, AI predictive maintenance" |
| Twin type | **Hybrid, self-calibrating**: reduced-order physics + AI4I risk model + residual-based calibration | The PPT's "coupled physics engine" + AI4I; the report's "validate before trusting" |
| 3D geometry | **TseNC** open-source CNC mill (STEP, CERN-OHL-W), simplified to glb; fallback is a primitive-built mill | Reuse instead of modelling from scratch |
| Data | SIM (plant simulator) → REPLAY (AI4I) → LIVE (ESP32 on a desk motor, stretch) | Same three sources as the report's Layer 1 |

The new core idea: **the twin predicts, compares its prediction with what it observes, and recalibrates itself, and the drift in its calibrated parameters is itself a health signal** (§4.3). This is the strongest version of the PPT's "AI that gets smarter" and stays inside the report's guardrails.

They look like they disagree, but they don't. **The PPT describes where the product is going. The report describes how we get there safely.** The merge keeps every PPT feature and passes each one through the report's guardrails. The device that ties them together is the **Source-Mode + Claim-Status system** in §2.

---

## 1. Reconciliation table: one wording for both documents

Use the right-hand column on the slides, in the demo voice-over and in Q&A. Under this wording nothing from the PPT gets dropped, and nothing in the report gets contradicted.

| PPT claim | Report position | **Merged wording (use this)** | How the demo proves it |
|---|---|---|---|
| "AI-Driven Self-Healing: automatically adjusts machine speed" | No auto control; advisory only | **"Supervised Self-Healing."** In **SIM mode**, the virtual PLC auto-derates RPM (closed loop in software-in-the-loop, which is safe because nothing physical moves). In **LIVE mode**, the same logic only *proposes* a derate, and a human approves it. The actuation path is locked off until Stage D. | Toggle *Auto (SIM)* vs *Advisory (LIVE)*. In LIVE the button shows a lock icon with "requires safety review". |
| "AI Predictive Maintenance" (AI4I-trained) | AI4I is synthetic; validate per machine | **"Hybrid self-calibrating twin."** Physics predicts expected behaviour, AI4I-trained model scores risk, and residuals between prediction and observation recalibrate a few bounded parameters. | Predicted-vs-observed chart, residual band, parameter history with versions (v1.0 → v1.3). |
| "Predicts failures and estimates RUL" | Show risk/inspection priority; RUL needs run-to-failure data | **"Failure-risk + Remaining Tool Life."** Machine-level output is a *risk category*. RUL is shown only for the **cutting tool**, from the tool-wear model (AI4I tool-wear failure window ≈ 200–240 min), and labelled as an estimate. | Tool-life gauge counts down. Machine RUL is shown as "planned (needs pilot data)". |
| "Tracks ₹ savings, CO₂ reductions in real time" | Estimates only, editable assumptions, cited emission factor | **"Telemetry-to-₹ exposure calculator."** It shows the ₹ at risk and the ₹ exposure avoided *in this scenario*, from factory-entered values. CO₂ = kWh × CEA grid factor (cited with year). | Assumptions panel is editable live. Every number has an ⓘ showing its formula. |
| "< ₹2,500 sensor node per machine" | Itemise, confirm with quotes | **"Indicative BOM ≈ ₹1,500–2,500 (Oct 2026 retail quotes)."** Show the itemised table (§6). | One slide or backup slide with the itemised BOM and quote dates. |
| "Target ROI < 3 months" | Payback = cost / *verified* monthly benefit | **"Payback calculator: example scenario shows < 3 months *if* one avoided breakdown/month is worth ≥ ₹X; the pilot will verify."** | Calculator with a sensitivity slider. |
| "vs ₹5+ lakh commercial" | Avoid unsupported comparisons | "Commercial DT suites are typically priced for large plants (licences, integrators). Ours has ₹0 licence cost; total cost still includes sensors, setup and support." | — |
| "Runs at 60 FPS on any laptop/mobile" | Measure on actual demo laptop | "**Measured: N FPS on our demo laptop** (Chrome, i5/integrated GPU)." Fill in the real number. | FPS counter in the engineer view. |
| "Wokwi-inspired Virtual PLC" | SIL simulation, not a certified PLC | "Software virtual PLC with a scan-cycle and interlock logic, used for training and testing. It is not a certified PLC." | Show the PLC rung/state panel ticking. |
| "Integration with real sensors via MQTT/JSON, Modbus" | Adapters behind a common schema; read-only first | "One JSON schema. Sources are swappable: **SIM → REPLAY (AI4I) → LIVE (ESP32/MQTT)**. Brownfield connectors are read-only." | Same dashboard switches source with zero UI change. |
| McKinsey/Capgemini 10–15% gains | Literature, not our findings | "Literature reports X–Y%. **We treat these as targets to measure, not results.**" | — |
| ISO 10816/20816 thresholds | Machine-specific, configurable | "Vibration zones are **configurable demo settings referencing ISO 20816**. They must be set per machine class." | Threshold config panel. |
| TRL implied high | "software demonstrator" | "Software demonstrator; field readiness via staged validation (Stage A done → B/C next)." | — |

**Rule for the pitch:** say the bold line and then the guardrail in the same breath. For example: *"It heals itself in simulation, and on a real machine it asks the operator first."* Judges reward ambition when the safety thinking comes with it.

---

## 2. The "one ground": three labels on everything

Build these into the product itself, not just the slides. They make the honesty visible, which turns the report's caution into a feature.

1. **Source-Mode badge on every reading:** `SIM` (virtual PLC) · `REPLAY` (AI4I 2020) · `LIVE` (sensor). It is a coloured pill *with text*, so it doesn't depend on colour alone.
2. **Fact / Inference / Action split on every alert:**
   - *Measured:* "Process temp 312.4 K, ΔT = 7.9 K, RPM 1,320"
   - *Inferred:* "Heat-dissipation failure risk: HIGH (rule + model agree)"
   - *Suggested:* "Check coolant flow; verify temperature sensor; consider derating to 1,500 RPM"
3. **Claim-Status tags in slides/docs:** ✅ Built · 🧪 Demo (simulated) · 🗺️ Planned · 📏 Hypothesis to measure.

---

## 3. What we demo on 26–27 Oct (Stage A, fully software)

### 3.1 The 5-minute demo script

This is the backbone, and everything we build serves it. The report says a convincing demo is *normal → controlled fault → explained alert → 3D change → ₹ consequence*. The PPT adds *self-healing → work order*. The hybrid twin adds the beat most teams won't have: **the twin catches itself being wrong and recalibrates.**

| t | Screen | What we say |
|---|---|---|
| 0:00 | 3D CNC mill cutting a job (spindle spinning, part counter 12/50), all green, `SIM` badge | "A small CNC job-shop. One machine, no sensors, no budget for a ₹5-lakh suite. We start virtual." |
| 0:30 | Engineer view: **Predicted vs Observed** spindle temperature, residual inside the band | "The twin predicts what the machine *should* do, then checks what it *does*. Right now they agree." |
| 0:50 | Click **Inject → Gradual coolant degradation** (slow drift in the *plant*, not the twin) | "Coolant is slowly getting worse. Nobody told the twin." |
| 1:10 | Residual creeps out of band → **"Model mismatch: sustained, +6 °C"** | "The twin notices it's wrong before any alarm limit is hit." |
| 1:30 | Calibration fires: cooling coefficient `h` 1.00 → 0.78 (bounded, versioned v1.0 → v1.1); predicted curve snaps back onto observed | "It recalibrates, within physical limits, and logs every change. And the parameter drift *is* the diagnosis: cooling capacity is down 22%." |
| 2:00 | Forecast: **"At current settings, process-temperature warning in ~14 min"** | "Because it's calibrated, it can forecast. That's the 'predictive' in predictive maintenance." |
| 2:20 | Alert card: Measured / Inferred / Suggested + AI4I-model risk | "What changed, which part, why, what to check: coolant flow and pump filter." |
| 2:45 | **Supervised self-healing:** auto-derate in SIM → forecast pushes out, risk drops | "In simulation the PLC derates itself. On a live machine this needs the operator's approval." |
| 3:05 | Click **Inject → Bearing spike** (abrupt) → flagged **ANOMALY, calibration frozen** | "Sudden jumps are *not* learned. A faulty bearing or sensor must never teach the twin that broken is normal." |
| 3:25 | **Generate Work Order** → Open → Acknowledged; operator enters measured tool wear at tool change → wear-rate recalibrates | "Every alert becomes traceable maintenance, and the operator's own measurements make the twin better. That's human-centric Industry 5.0." |
| 3:50 | ₹ panel: exposure avoided, parts lost, kWh, CO₂ with assumptions shown | "What that event would have cost *this* shop, with their numbers and an open formula." |
| 4:15 | Switch source **SIM → REPLAY (AI4I)**, metrics panel | "Same twin on an open industrial dataset: precision, recall, confusion matrix, rule baseline vs model." |
| 4:35 | (Stretch) **LIVE**: ESP32 on a desk motor; hold a hand over the fan → real residual → real calibration | "And this is real data from a ₹2k node, recalibrating live." |
| 4:50 | Roadmap slide A→B→C→D | "Start virtual, earn trust, then connect." |

**Backup:** record a screen video of the full script by 24 Oct. If the Wi-Fi or laptop fails, play it. If the slot is 3 minutes, cut the 0:30, 3:05 and 4:15 beats.

### 3.2 Feature list, prioritised (MoSCoW)

**Must (demo breaks without it)**
- M1 3D CNC mill (`.glb`, named parts: spindle, head, table, column, workpiece) with spindle rotation ← RPM, head heat shader ← temp, jitter ← vibration, table motion during the cutting cycle
- M2 **Plant simulator** (hidden "true" machine with its own parameters) + virtual PLC + fault injectors + Reset/Replay (seeded)
- M3 **Twin model** (same equations, nominal parameters) running in parallel → predicted vs observed + residuals
- M4 **Calibration loop**: residual detector (EWMA/CUSUM) → bounded RLS update of 2–3 parameters → versioned parameter log, with a freeze when an anomaly or data-quality fault is present
- M5 FastAPI + WebSocket stream at 500 ms ticks, using the common JSON schema (§4.2)
- M6 Rule-based alert engine with Measured/Inferred/Suggested cards
- M7 Supervised self-healing: auto-derate in SIM, approve-gate in LIVE
- M8 Dashboard: health, temp, vibration, RPM, energy, risk, part counter, source badge, trend sparkline

**Should (wins points)**
- S1 Random Forest on AI4I with a metrics panel (vs the rule baseline), scoring both observed and *forecast* state
- S2 Forecast "time to warning at current settings" + Remaining Tool Life gauge
- S3 ₹/energy/CO₂ calculator with editable assumptions and payback
- S4 Work orders + event log (SQLite); operator-entered tool-wear measurement feeds calibration
- S5 Operator view ↔ Engineer view toggle
- S6 REPLAY mode (AI4I rows streamed as telemetry)

**Could (stretch)**
- C1 ESP32 LIVE node on a desk motor/fan (DS18B20 on the housing + MPU6050 + ACS712/SCT-013), which enables real-data calibration
- C2 Job view: order surge → cycle time / OEE / bottleneck
- C3 Hindi labels toggle for the operator view
- C4 What-if: baseline vs scenario side-by-side using the calibrated twin

**Won't (say "planned")**: real-machine actuation, machine-level RUL, OPC UA/AAS connectors, multi-machine factory floor, self-retraining of the failure classifier (needs real labels).

---

## 4. Architecture (merged from PPT slide 3 + Report §5/§7 + hybrid twin)

```
 ┌──────────────── Layer 1: Telemetry Sources (the "observed" side) ─────────────┐
 │ SIM: Plant simulator + Virtual PLC │ REPLAY: AI4I 2020 │ LIVE: ESP32→MQTT (read-only) │
 └──────────────┬────────────────────────────────────────────────────────────────┘
                │  observed JSON @ 500 ms  (source_mode + dq_flag tagged)
 ┌──────────────▼────── Layer 2: Hybrid Twin Core (Python/FastAPI) ──────────────┐
 │ ① validate units/timestamps → data-quality flag                               │
 │ ② TWIN MODEL (physics, calibrated params θ) → predicted state ŷ               │
 │ ③ residual e = y − ŷ → EWMA band · CUSUM (drift) · z-spike (anomaly)          │
 │ ④ CALIBRATOR: if drift ∧ DQ ok ∧ no anomaly → bounded RLS update θ (versioned)│
 │ ⑤ FORECAST: roll calibrated twin forward → time-to-warning at current setpoint│
 │ ⑥ RISK: rules (AI4I failure definitions, ISO-20816-referenced vib zones)      │
 │        + RandomForest (AI4I features) on observed AND forecast state          │
 │ ⑦ PLC supervisor: derate proposal ─► SIM: auto apply ─► LIVE: human OK        │
 │ ⑧ ₹ / kWh / CO₂ / OEE calculator · SQLite event + parameter log               │
 └──────────────┬────────────────────────────────────────────────────────────────┘
                │  WebSocket state sync (observed + twin + residuals + θ)
 ┌──────────────▼────── Layer 3: 3D Twin (TseNC STEP → Blender → glTF → Three.js) ┐
 │ RPM→spindle · temp→shader · vibration→jitter · job→table motion · fault→glow    │
 └──────────────┬────────────────────────────────────────────────────────────────┘
 ┌──────────────▼────── Layer 4: Industry 5.0 Operator & Decision Layer ─────────┐
 │ Operator view (status, 1-line reason, safe next step, ACK, enter tool wear)    │
 │ Engineer view (predicted vs observed, residuals, θ history, model metrics, DQ) │
 │ Work orders · ₹ calculator · fault-injection panel                             │
 └────────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Repo layout

```
udyam-twin/
├── sim/        # plant.py (hidden "true" machine), plc.py, faults.py (pure Python, seeded)
├── twin/       # model.py (same physics, params θ), residuals.py, calibrate.py (RLS), forecast.py
├── api/        # FastAPI app, WebSocket /ws/telemetry, REST for faults/work-orders/config/params
├── ml/         # AI4I download, train.py, metrics.json, model.joblib, rules.py
├── web/        # index.html, Three.js scene, dashboard (vanilla JS or Vite)
├── assets/     # machine.glb (from TseNC, CERN-OHL-W) + ATTRIBUTION.md; fallback primitive mill
├── edge/       # ESP32 firmware (stretch)
├── docs/       # this plan, schema, demo script, BOM, test log
└── tests/      # schema, replay determinism, fault transitions, calibration converges/freezes
```

### 4.2 Message schema (single source of truth; freeze on Day 1)

**Observed** (from SIM / REPLAY / LIVE):

```json
{
  "ts": "2026-10-26T10:15:30.500Z",
  "machine_id": "VMC-01",
  "source_mode": "SIM",
  "air_temp_k": 300.1,
  "process_temp_k": 310.4,
  "rpm": 1540,
  "torque_nm": 41.2,
  "tool_wear_min": 118,
  "vibration_rms_mm_s": 2.4,
  "current_a": 6.1,
  "power_kw": 6.64,
  "plc_state": "RUN",
  "derate_pct": 0,
  "parts_done": 12,
  "active_fault": null,
  "dq_flag": "OK"
}
```

**Twin state** (computed by the core and pushed alongside):

```json
{
  "ts": "2026-10-26T10:15:30.500Z",
  "predicted": { "process_temp_k": 309.1, "tool_wear_min": 117.6 },
  "residual":  { "process_temp_k": 1.3, "ewma": 0.9, "cusum": 2.1, "state": "IN_BAND" },
  "params":    { "version": "v1.1", "h_cool": 0.78, "k_heat": 1.02, "k_wear": 1.00, "frozen": false },
  "forecast":  { "minutes_to_warning": 14, "warning": "HDF_RISK" },
  "risk":      { "rules": "MEDIUM", "model": 0.41, "top_signals": ["delta_t", "rpm"] }
}
```

The first six process fields deliberately match **AI4I 2020's columns** (air temp K, process temp K, rotational speed, torque, tool wear). The ML model can therefore score SIM data, REPLAY data and (partially) LIVE data with no remapping. `vibration_rms_mm_s` and `current_a` go to the **rule engine only**, never to the AI4I model (the report is explicit about this). `source_mode`, `dq_flag` and `params.frozen` are what keep the calibrator honest.

### 4.3 Hybrid twin core: physics + AI4I + residual calibration

The twin needs **two simulators with different parameters**. Without that, SIM mode has nothing to calibrate against:

- **Plant** (`sim/plant.py`) stands in for the real machine. It has "true" parameters the twin can't see, plus noise and faults. In LIVE mode, the ESP32 replaces it.
- **Twin model** (`twin/model.py`) uses the *same equations* with *its own estimate* θ, starting at nominal values.

**Reduced-order physics** (deliberately simple and documented, no FEA/CFD):
- Power: `P = torque × rpm × 2π / 60`
- Thermal: `dT/dt = k_heat·P_loss − h_cool·(T − T_air)` (first order, with the process temp above air)
- Tool wear: `dW/dt = k_wear · f(rpm, torque)`, where AI4I's wear range is 0–~250 min
- Vibration: `v = v0 + c·rpm²·imbalance + bearing(t)`, rule engine only
- Job: cycle time from a fixed part program; `parts_done` increments per cycle; scrap is flagged when wear passes a quality limit

**The learning loop (what "gets better over time" means, precisely):**
1. **Predict** ŷ with the current θ.
2. **Residual** `e = y − ŷ`. Track the EWMA (noise band) and CUSUM (sustained drift), and a z-score for spikes.
3. **Decide what kind of error it is:**
   - *Inside the band* → do nothing.
   - *Sustained, smooth drift* (CUSUM) → **calibrate**.
   - *Abrupt spike* or `dq_flag ≠ OK` → **anomaly or sensor fault: freeze calibration** and alert instead.
4. **Calibrate** with recursive least squares (forgetting factor ≈ 0.98) on the 2–3 identifiable parameters (`h_cool`, `k_heat`, `k_wear`). Clamp each to physical bounds and limit its rate of change.
5. **Version** every update (v1.0 → v1.1 …) with its reason and the residual before and after. Provide a one-click rollback.
6. **Parameter drift = health indicator.** `h_cool` falling 22% *is* the finding "cooling capacity degraded". Trend it like any other signal.
7. **Human-in-the-loop calibration:** at a tool change, the operator enters the measured wear (or a yes/no on edge wear), which updates `k_wear`. This is the only kind of "label" the stage-A system has, and it is real.

**What is deliberately *not* claimed:** recalibrating the physics makes *state prediction* better. It does **not** make the *failure classifier* better. Improving failure prediction needs real outcome labels (maintenance records, failures), so it is Stage C+. Say this distinction out loud; it's the most credible sentence in the pitch.

**Virtual PLC (scan every tick):** `IDLE → RUN → DERATE → TRIP` with interlocks. For example, `vib > zone_D or T > T_trip → TRIP`, and `forecast warning < 10 min or risk ≥ HIGH → propose derate 20%`.

**Fault injectors (applied to the plant only):**
- `coolant_degradation` (gradual, so it calibrates)
- `bearing_defect` (abrupt then growing, so it raises an anomaly)
- `overload` (torque step)
- `tool_wear_accel`
- `sensor_stuck` / `sensor_dropout` (a DQ fault, so calibration must freeze)
- `order_surge`

Every run is **seeded**, so Reset → Replay gives the identical curve.

### 4.4 AI / analytics (honest and strong)

- **Rule baseline first**, mirroring the AI4I failure definitions:
  - HDF: ΔT < 8.6 K and rpm < 1,380
  - PWF: power < 3.5 kW or > 9 kW
  - OSF: wear × torque above the type limit (11k/12k/13k min·Nm for L/M/H)
  - TWF: wear in the 200–240 min window
- **Random Forest** on AI4I features plus engineered `power` and `ΔT`, with `class_weight="balanced"` and a stratified split with a fixed seed. Report **precision, recall, F1, PR-AUC and the confusion matrix**, never plain accuracy. (A "time-aware" split adds nothing here: AI4I rows are independent synthetic samples, not one machine's history. Say so if asked.)
- The model scores **both the observed state and the twin's forecast state**. Risk *now* and risk *if nothing changes* make a stronger story than a single number.
- **Judge-proof honesty point:** AI4I failure modes are themselves generated by rules, so a strong score is expected and **is not proof of real-world accuracy**. Say this before a judge does. It shows maturity.
- Output = risk category + **top contributing signals** (feature importance / rule hit), shown in the alert card.

### 4.5 3D asset: TseNC, used properly

- **Source:** [TseNC](https://github.com/fab-machines/TseNC), an open-source desktop CNC mill (Rhine-Waal University), with STEP/IGES/Fusion files, released under **CERN-OHL-W**. Keep attribution in `assets/ATTRIBUTION.md` and publish our simplified model under the same licence.
- **Pipeline:** STEP → FreeCAD (or Blender STEP importer) → remove fasteners, merge into ~6 named parts → decimate → Blender pivots → export `.glb` (target < 5 MB).
- **Time-box: 4 hours on D1.** If conversion fights back, build the mill from primitives in Blender (base, column, table, head, spindle). Judges score behaviour, not bolts.
- **Don't borrow TseNC's specs.** It has a 1 kW router spindle, while our operating envelope follows the AI4I scenario (≈1,200–2,900 rpm, up to ~9 kW). Label the model a **"representative 3-axis VMC (geometry: TseNC)"**.

### 4.6 What we ship: a local web app

- **Form:** a browser-based web app. The Python/FastAPI backend serves the Three.js frontend and the WebSocket from the same laptop (`http://localhost:8000`). It is not a native desktop app and not a cloud-hosted website. This matches both submissions ("browser-accessible", "runs on any laptop or mobile browser").
- **Feels like an app:** `start.bat` / `start.sh` starts the server and opens the browser in full-screen kiosk mode. Everything is bundled locally (three.js, fonts, model, glb), so it runs **with no internet**.
- **Phone as operator screen:** the laptop runs a hotspot and a phone opens `http://<laptop-ip>:8000/operator`. In the demo, a judge holds the operator view while we inject faults on the laptop. This proves the "mobile browser" claim live.
- **Not before the event:** a public hosted link is optional, for after the conclave (a free-tier host, or a GitHub Pages copy running REPLAY only) to put on the QR code and in the paper. Don't let the demo depend on venue Wi-Fi.
- **Later (Route 6):** the same app installs on a low-cost mini-PC or Raspberry Pi beside the machine as an on-premise edge box. No code changes, which is the advantage of choosing the web route.

---

## 5. Eight-day sprint (18 → 26 Oct)

Five people, five workstreams. Each person owns one, and two pair on integration days.

| Workstream | Owns | Suggested |
|---|---|---|
| **W1 3D Twin** | TseNC → glb (time-boxed), Three.js scene & bindings, job motion | modeller/frontend |
| **W2 Plant + Virtual PLC** | plant physics, faults, PLC state machine, determinism tests | Python |
| **W3 Twin Core + ML** | twin model, residuals, RLS calibrator, forecast; AI4I model + metrics | Python/ML (strongest math person) |
| **W4 Backend + Dashboard** | FastAPI/WebSocket/SQLite; operator/engineer views, predicted-vs-observed chart, alert cards, ₹ calc, work orders | full-stack |
| **W5 Pitch + Evidence + Edge** | slides v2, demo script, video, BOM quotes, ESP32 stretch, Q&A drill | presenter |

| Day | Date | Goal (end-of-day check) |
|---|---|---|
| D1 | Sun 18 Oct | Schema frozen; repo scaffold; TseNC glb **or** fallback mill loads in browser; AI4I downloaded; physics equations written on one page |
| D2 | Mon 19 Oct | Plant streams over WebSocket; 3D spindle spins from live RPM; twin model runs alongside, residual computed |
| D3 | Tue 20 Oct | Fault injectors + PLC states; rule-engine alerts; predicted-vs-observed chart live |
| D4 | Wed 21 Oct | **Calibration loop**: CUSUM drift → RLS update → versioned θ; freeze on spike/DQ fault; RF trained + metrics.json |
| D5 | Thu 22 Oct | Forecast time-to-warning; self-heal loop; alert cards; ₹/energy/CO₂; work orders + tool-wear entry; REPLAY mode |
| D6 | Fri 23 Oct | **Feature freeze.** Full demo script runs end-to-end 3× without touching code; calibration tests pass |
| D7 | Sat 24 Oct | Polish + FPS measured; **backup video recorded**; slides v2; ESP32 stretch only if D6 passed |
| D8 | Sun 25 Oct | Offline packaging (local three.js, no CDN), 3 timed rehearsals, Q&A drill, BOM quotes captured |
| — | 26–27 Oct | Conclave. Arrive early, test projector/HDMI, run on battery + hotspot fallback |

**Kill rule:** if something isn't working by its day, cut it to "planned" and don't sink the demo for it. Cut order: ESP32 → REPLAY → job/OEE view → forecast. **Never cut the calibration loop**; it is now the differentiator.

---

## 6. Evidence pack (makes the PPT numbers defensible)

**Indicative sensor-node BOM.** Fill in actual quotes and dates before 24 Oct.

| Item | Purpose | Indicative ₹ |
|---|---|---|
| ESP32 DevKit | MCU + Wi-Fi/MQTT | 350–500 |
| MPU6050 / ADXL345 | vibration (accelerometer) | 120–300 |
| SCT-013 clamp CT + burden | non-invasive current | 450–700 |
| DS18B20 / MLX90614 | housing temperature | 80–900 |
| PSU, enclosure, magnet mount, wiring | install | 300–500 |
| **Total** | | **≈ ₹1,300–2,900 (state the range)** |

**Also prepare:**
- `metrics.json` screenshot (precision/recall/F1/confusion matrix + rule baseline)
- FPS measured on the demo laptop
- Test log: schema tests, replay determinism, each fault's state transitions, latency (tick → render)
- Emission factor source: **CEA CO₂ Baseline Database (latest version)**, with year cited, not hard-coded from memory
- Payback worked example with every assumption visible

---

## 7. Judge Q&A prep (likely questions → one-breath answers)

| Q | A |
|---|---|
| "Isn't this just a 3D animation?" | "The 3D view is the interface. The twin is the synchronised state, physics, PLC logic and analytics behind it. Switch the source to REPLAY and the same twin runs on real-format industrial data." |
| "Your model is trained on synthetic data." | "Yes, and we say so on screen. AI4I gives a reproducible baseline. Stage B/C validates per machine. That's why the machine-level output is *risk*, not a false-precision RUL." |
| "Self-healing on a real machine is dangerous." | "Agreed. That's why it only auto-acts in simulation. On live machines it's advisory with human approval. Actuation needs hazard analysis and interlocks (Stage D)." |
| "How is it different from commercial DT?" | "Brownfield-first, simulation-first, ₹0 licence, explainable alerts, ₹ impact in the factory's own numbers. It's built for a 10-machine unit, not a 1,000-machine plant." |
| "What's the ROI proof?" | "Our calculator shows the formula and assumptions. A real payback number comes from a baseline-vs-pilot comparison on one machine, which is our next step." |
| "Which standards?" | "Architecture maps to ISO 23247 concepts (no conformity claim). Vibration zones reference ISO 20816 and are configured per machine class." |
| "How does it 'learn'? Isn't that just retraining?" | "No. It compares its own prediction with what it observes. Sustained, smooth errors recalibrate a few physical parameters within bounds, and every change is versioned and can be rolled back. The drift in those parameters is itself the diagnosis." |
| "What if a sensor is faulty? Won't it learn garbage?" | "Spikes and data-quality faults freeze calibration and raise an anomaly instead. Watch: we inject a stuck sensor and the parameters don't move." |
| "Does calibration make failure prediction better?" | "Not by itself. It makes *state* prediction better. Better *failure* prediction needs real outcome labels from a pilot, and we keep those two claims separate." |
| "Why did you start from AI4I instead of pure physics?" | "Both from day 0. Physics gives behaviour you can explain from the first minute; AI4I gives a reproducible failure-risk baseline. Neither is trusted alone." |
| "Is the 3D model your own?" | "Geometry is adapted from TseNC, an open-source CNC mill under CERN-OHL-W, credited in the app. Our contribution is the behaviour: physics, calibration, PLC, analytics." |
| "Why will MSMEs adopt it?" | "Zero risk to start: no sensor, no downtime, no PLC change. They try it on a virtual copy, then a ₹2k read-only node on one machine." |

---

## 8. Expansion routes (after the conclave)

Each route keeps the same core (schema + twin + analytics), so none of them is a pivot.

### Route 1 — Depth: real pilot (Report Stages B → C)
1. **Stage B (Nov–Dec 2026), lab:** run the ESP32 node on a lab lathe/motor in the NIT Raipur Mechanical workshop. Do a usability study for RQ1 (3D + alert vs numeric dashboard: task time, errors).
2. **Stage C (Q1 2027), one-machine pilot:** partner with one MSME in **Urla / Siltara / Bhanpuri industrial areas** (Raipur) or the Bhilai ancillary belt. Read-only sensing; 2-week baseline then 4-week pilot. Measure alert precision, false alarms/hour, downtime, kWh per good part.
3. **Output:** the first *measured* numbers, which turn the PPT's claims into findings.

### Route 2 — Publication (cheapest credibility win)
- Submit the report as a paper to **NC MEET 2026** (Scopus-indexed special issue / INAE Transactions per the brochure) and the **INAE e-Magazine** if selected.
- Paper angle: *"A simulation-first, staged adoption framework for digital twins in brownfield MSMEs"*, plus the usability study results.

### Route 3 — Breadth: machine template library
- Turn each machine type into a **template**: 3D asset + physics params + thresholds + fault set + inspection checklists. Start with lathe/CNC, then induction motor-pump, air compressor, power press, and welding set.
- Describe templates in an **AAS-style (Asset Administration Shell) descriptor**, so a new machine is configuration, not code.

### Route 4 — Line / factory twin
- Multi-machine floor view → **OEE**, bottleneck and order-surge simulation (already teased in the PPT), plus maintenance scheduling across machines.
- Connectors: **Modbus RTU/TCP → MQTT**, then **OPC UA** for newer PLCs (still read-only first).

### Route 5 — Theme cross-over (same platform, other YEISS themes)
| Theme | Extension |
|---|---|
| **ET4 / AI-driven smart welding** | Welding twin: current/voltage signature → bead-quality/defect risk |
| **ET2 / Metallurgy, water & energy** (fits Chhattisgarh's steel/ancillary base) | Energy twin for induction furnaces/compressors: idle-running and kWh/tonne |
| **HT10 / Scrap management** | Link alerts to scrap/rework counts → quality-cost twin |
| **HT12 / Clean energy** | Rooftop-solar + load-shifting what-if on the energy twin |
| **HT13 / Industry 5.0** | Hindi/Chhattisgarhi operator UI, voice read-out of alerts, accessibility study |

### Route 6 — Startup / scale path (only after Route 1 gives real numbers)
- **"Twin-in-a-Box" kit:** sensor node + preconfigured template + 30-day trial on the virtual twin first.
- **Cluster model:** sell or lease through MSME cluster associations and MSME Technology Centres/Tool Rooms; shared engineer per cluster.
- **Explore alignment** (verify current scheme rules first): MSME ZED certification (quality/energy), SAMARTH Udyog Bharat 4.0 centres, college incubator / startup grants.
- Revenue idea: free sim tier → paid per-machine monitoring + support.

### Route 7 — Research depth
- Sim-to-real calibration on real machines: the stage-A calibrator (RLS on 2–3 parameters) carries straight into the pilot. Next steps are more parameters, a Kalman-filter state estimator, and cluster-level priors (a new machine starts from the fleet's calibrated θ).
- Learn failure prediction from pilot labels: maintenance records + operator tool-wear entries → fine-tune the risk model away from AI4I.
- Machine-level RUL once run-to-failure data exists (or via public datasets like NASA bearing/IMS for the bearing template).
- Federated/cluster models: share learning across MSMEs without sharing raw data.

**Suggested order:** Route 2 (paper) and Route 1 (pilot) start immediately after the conclave. Routes 3–5 grow out of the pilot. Route 6 starts only once there are measured results.

---

## 9. Definition of done for 26 Oct

- [ ] Demo script (§3.1) runs end-to-end, offline, in < 4 min, 3 times in a row
- [ ] Every number on screen has a source-mode badge or an ⓘ formula
- [ ] Slides v2 use the merged wording (§1); no "TRL 6", no "verified savings"
- [ ] Metrics panel shows rule baseline vs RF (precision/recall/F1/confusion matrix)
- [ ] Calibration demo: gradual fault → residual drift → θ update → curves re-converge; abrupt fault and stuck sensor → calibration freezes
- [ ] TseNC attribution (CERN-OHL-W) visible in the app and in `assets/ATTRIBUTION.md`
- [ ] Backup video on 2 laptops + a pen drive
- [ ] Everyone can answer every question in §7

---

## 10. What we took from the ChatGPT advice, and what we changed

| Advice | Decision |
|---|---|
| Target a small CNC machining workshop, one milling machine | **Adopted** (§0). It is what the PPT and report already imply. |
| Hybrid twin: physics + AI4I + residual-based calibration | **Adopted as the core** (§4.3). Physics and AI4I both run from day 0. |
| "Recalibration ≠ learning to predict failures" | **Adopted**, and turned into a pitch line (§4.3, §7). |
| Don't update on every reading; faulty sensors can poison the twin | **Adopted**: freeze rules + versioned, bounded, reversible updates. |
| Reuse TseNC geometry | **Adopted with limits**: CERN-OHL-W attribution, 4-hour time-box, our own operating envelope (TseNC's 1 kW router specs don't match AI4I). |
| Time-aware holdout on AI4I | **Changed**: AI4I rows aren't a time series; use a stratified, seeded split and say why. |
| Production optimisation vs maintenance | **Maintenance primary**; parts/OEE/₹ secondary, matching the submitted problem statement. |
| Its constraint questions | Answered: predictive maintenance primary · 8 days · ESP32 + desk motor, no industrial machine. |
