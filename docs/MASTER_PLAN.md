# UDYAM-TWIN — Master Plan

**Team:** Tech Titans (NIT Raipur) · **Event:** INAE Youth Conclave 2026 (YEISS-2026), Hackathon, 26–27 Oct 2026
**Theme:** HT14 — Digital Twin Applications for MSMEs and Smart Factories (also touches HT4 and HT13)
**Plan written:** 17 Oct 2026, which leaves **9 days** until the event

---

## 0. The one-line idea (use it everywhere)

> **"Start virtual, earn trust, then connect."**
> UDYAM-TWIN is a zero-licence-cost, browser-based 3D digital twin with a virtual PLC. It lets an MSME test faults, read explained alerts and see ₹ impact on a simulated machine first. It then moves the same software to a real machine in read-only stages.

There are two submissions:

| | **The PPT** (bold pitch) | **The Report** (careful research proposal) |
|---|---|---|
| Tone | Vision, impact, Viksit Bharat | Evidence, safety, honest limits |
| Says | Self-healing auto-throttle, RUL, ₹ & CO₂ savings, < ₹2,500 node, < 3-month ROI, 60 FPS | Advisory-only, risk instead of RUL, savings stay hypotheses until measured, staged A→D roadmap |

They look like they disagree, but they don't. **The PPT describes where the product is going. The report describes how we get there safely.** The merge keeps every PPT feature and passes each one through the report's guardrails. The device that ties them together is the **Source-Mode + Claim-Status system** in §2.

---

## 1. Reconciliation table: one wording for both documents

Use the right-hand column on the slides, in the demo voice-over and in Q&A. Under this wording nothing from the PPT gets dropped, and nothing in the report gets contradicted.

| PPT claim | Report position | **Merged wording (use this)** | How the demo proves it |
|---|---|---|---|
| "AI-Driven Self-Healing: automatically adjusts machine speed" | No auto control; advisory only | **"Supervised Self-Healing."** In **SIM mode**, the virtual PLC auto-derates RPM (closed loop in software-in-the-loop, which is safe because nothing physical moves). In **LIVE mode**, the same logic only *proposes* a derate, and a human approves it. The actuation path is locked off until Stage D. | Toggle *Auto (SIM)* vs *Advisory (LIVE)*. In LIVE the button shows a lock icon with "requires safety review". |
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

### 3.1 The 4-minute demo script

This is the backbone, and everything we build serves it. The report itself says a convincing demo is *normal → controlled fault → explained alert → 3D change → ₹ consequence*. The PPT adds *self-healing → work order*.

| t | Screen | What we say |
|---|---|---|
| 0:00 | 3D lathe/CNC spinning, all green, `SIM` badge | "This is a 200-machine MSME problem on one screen: an old machine with no sensors. We start virtual." |
| 0:30 | Click **Inject Fault → Coolant loss** | "We can't break a real machine to learn, so the virtual PLC lets us break this one safely." |
| 0:45 | Temperature climbs, spindle housing shader goes amber→red, trend graph | "The twin reacts the way the physics says it should." |
| 1:15 | Alert card: Measured / Inferred / Suggested | "No raw probability dump. It says what changed, which part, why, and what to check." |
| 1:45 | **Supervised self-healing:** auto-derate in SIM → temperature stabilises, risk drops | "In simulation the PLC derates itself. On a live machine this button needs the operator's approval. That's our safety-by-design." |
| 2:15 | **Generate Work Order** → Open → Acknowledged | "Every alert becomes traceable maintenance, not a forgotten beep." |
| 2:35 | ₹ panel: exposure avoided, kWh, CO₂ with assumptions shown | "Here's what that event would have cost *this* factory. These are their inputs, and the formula is open." |
| 3:05 | Switch source **SIM → REPLAY (AI4I)** | "Same twin, now replaying an open industrial dataset. Here are our model metrics: precision, recall, confusion matrix." |
| 3:30 | (Stretch) **LIVE**: ESP32 on a desk fan/motor shakes → vibration rises | "And the same schema takes a ₹2k sensor node. Read-only, non-invasive, no PLC changes." |
| 3:50 | Roadmap slide A→B→C→D | "Start virtual, earn trust, then connect." |

**Backup:** record a screen video of the full script by 24 Oct. If the Wi-Fi or laptop fails, play it.

### 3.2 Feature list, prioritised (MoSCoW)

**Must (demo breaks without it)**
- M1 3D machine (`.glb`) with spindle rotation ← RPM, housing heat shader ← temp, mesh jitter ← vibration
- M2 Virtual PLC + physics simulator with 4 fault injectors and Reset/Replay (seeded)
- M3 FastAPI + WebSocket stream at 500 ms ticks, using the common JSON schema (§4.2)
- M4 Rule-based alert engine with Measured/Inferred/Suggested cards
- M5 Supervised self-healing: auto-derate in SIM, approve-gate in LIVE
- M6 Dashboard: health, temp, vibration, RPM, energy, risk, source badge, trend sparkline

**Should (wins points)**
- S1 Random Forest on AI4I with a metrics panel (vs the rule baseline)
- S2 Remaining Tool Life gauge
- S3 ₹/energy/CO₂ calculator with editable assumptions and payback
- S4 Work orders + event log (SQLite)
- S5 Operator view ↔ Engineer view toggle
- S6 REPLAY mode (AI4I rows streamed as telemetry)

**Could (stretch)**
- C1 ESP32 LIVE node (MPU6050 + DS18B20) over MQTT/WebSocket
- C2 Order-surge scenario → OEE/bottleneck view
- C3 Hindi labels toggle for the operator view
- C4 What-if: baseline vs scenario side-by-side

**Won't (say "planned")**: real-machine actuation, machine-level RUL, OPC UA/AAS connectors, multi-machine factory floor.

---

## 4. Architecture (merged from PPT slide 3 + Report §5/§7)

```
 ┌──────────────── Layer 1: Telemetry Sources ────────────────┐
 │  SIM: Virtual PLC + physics  │ REPLAY: AI4I 2020 │ LIVE: ESP32→MQTT (read-only) │
 └──────────────┬─────────────────────────────────────────────┘
                │  common JSON @ 500 ms  (source_mode tagged)
 ┌──────────────▼────── Layer 2: Sync & Analytics (Python/FastAPI) ──────┐
 │ validate units/timestamps → data-quality flag                        │
 │ rules (ISO-20816-referenced vib zones, ΔT, power, wear×torque)       │
 │ RandomForest (AI4I features only) → risk + contributing signals      │
 │ tool-life estimator · OEE/energy/CO₂/₹ calculator · SQLite event log │
 │ Virtual PLC supervisor: derate proposal  ──► SIM: auto apply         │
 │                                          ──► LIVE: needs human OK    │
 └──────────────┬───────────────────────────────────────────────────────┘
                │  WebSocket state sync
 ┌──────────────▼────── Layer 3: 3D Twin (Blender → glTF → Three.js) ───┐
 │ RPM→spindle · temp→shader · vibration→jitter · fault→component glow  │
 └──────────────┬───────────────────────────────────────────────────────┘
 ┌──────────────▼────── Layer 4: Industry 5.0 Operator & Decision Layer ┐
 │ Operator view (status, 1-line reason, safe next step, ACK)           │
 │ Engineer view (trends, raw, model metrics, DQ, scenario params)      │
 │ Work orders · ₹ calculator · fault-injection panel                   │
 └──────────────────────────────────────────────────────────────────────┘
```

### 4.1 Repo layout

```
udyam-twin/
├── sim/        # virtual PLC + physics model + fault injectors (pure Python, seeded)
├── api/        # FastAPI app, WebSocket /ws/telemetry, REST for faults/work-orders/config
├── ml/         # AI4I download, train.py, metrics.json, model.joblib, rules.py
├── web/        # index.html, Three.js scene, dashboard (vanilla JS or Vite)
├── assets/     # machine.blend, machine.glb (named parts: spindle, housing, bearing, motor)
├── edge/       # ESP32 firmware (stretch)
├── docs/       # this plan, schema, demo script, BOM, test log
└── tests/      # schema, replay determinism, fault state transitions
```

### 4.2 Telemetry schema (single source of truth; freeze on Day 1)

```json
{
  "ts": "2026-10-26T10:15:30.500Z",
  "machine_id": "LATHE-01",
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
  "active_fault": null,
  "dq_flag": "OK"
}
```

The first six process fields deliberately match **AI4I 2020's columns** (air temp K, process temp K, rotational speed, torque, tool wear). The ML model can therefore score SIM data, REPLAY data and (partially) LIVE data with no remapping. `vibration_rms_mm_s` and `current_a` go to the **rule engine only**, never to the AI4I model (the report is explicit about this).

### 4.3 Simulator + Virtual PLC (keep it simple and explainable)

- **Thermal:** `dT/dt = (a·P_loss − b·(T − T_air) − coolant)/C`, where heat goes up with torque × rpm.
- **Power:** `P = torque × rpm × 2π / 60`
- **Tool wear:** grows each tick in proportion to rpm, with a faster rate for the "tool wear" fault.
- **Vibration:** `base + k·rpm² · imbalance + bearing_defect(t)` (grows exponentially after injection).
- **Current:** roughly proportional to torque, plus noise.
- **PLC scan (every tick):** `IDLE → RUN → DERATE → TRIP` with interlocks. For example, `vib > zone_D or T > T_trip → TRIP`, and `risk ≥ HIGH → propose derate 20%`.
- **Fault injectors:** `bearing_defect`, `overload`, `coolant_loss`, `tool_wear_accel`, `order_surge`, `sensor_dropout` (the last one tests data-quality handling).
- Every run is **seeded**, so Reset → Replay gives the identical curve (report validation item "deterministic replay").

### 4.4 AI / analytics (honest and strong)

- **Rule baseline first**, mirroring the AI4I failure definitions:
  - HDF: ΔT < 8.6 K and rpm < 1,380
  - PWF: power < 3.5 kW or > 9 kW
  - OSF: wear × torque above the type limit (11k/12k/13k min·Nm for L/M/H)
  - TWF: wear in the 200–240 min window
- **Random Forest** on AI4I features plus engineered `power` and `ΔT`, with `class_weight="balanced"` and a stratified split with a fixed seed. Report **precision, recall, F1, PR-AUC and the confusion matrix**, never plain accuracy.
- **Judge-proof honesty point:** AI4I failure modes are themselves generated by rules, so a strong score is expected and **is not proof of real-world accuracy**. Say this before a judge does. It shows maturity.
- Output = risk category + **top contributing signals** (feature importance / rule hit), shown in the alert card.

---

## 5. Nine-day sprint (17 → 26 Oct)

Five people, five workstreams. Each person owns one, and two pair on integration days.

| Workstream | Owns | Suggested |
|---|---|---|
| **W1 3D Twin** | Blender model, glb export, Three.js scene & bindings | modeller/frontend |
| **W2 Sim + Virtual PLC** | physics, faults, PLC state machine, determinism tests | Python |
| **W3 Backend + ML** | FastAPI, WebSocket, SQLite, AI4I model + metrics | Python/ML |
| **W4 Dashboard + UX** | operator/engineer views, alert cards, ₹ calculator, work orders | frontend |
| **W5 Pitch + Evidence + Edge** | slides v2, demo script, video, BOM quotes, ESP32 stretch, Q&A drill | presenter |

| Day | Date | Goal (end-of-day check) |
|---|---|---|
| D1 | Sat 17 Oct | Schema frozen; repo scaffold; glb with named parts loads in browser; AI4I downloaded |
| D2 | Sun 18 Oct | Sim streams over WebSocket; 3D spindle spins from live RPM |
| D3 | Mon 19 Oct | 4 fault injectors + PLC states; dashboard tiles live; rule engine alerts |
| D4 | Tue 20 Oct | RF trained + metrics.json; alert cards Measured/Inferred/Suggested; self-heal loop in SIM |
| D5 | Wed 21 Oct | ₹/energy/CO₂ calculator; work orders + SQLite log; REPLAY mode |
| D6 | Thu 22 Oct | **Feature freeze.** Full demo script runs end-to-end 3× without touching code |
| D7 | Fri 23 Oct | Polish: operator/engineer toggle, labels, FPS measured; ESP32 stretch (only if D6 passed) |
| D8 | Sat 24 Oct | Slides v2 (merged wording); **backup demo video recorded**; BOM quotes captured |
| D9 | Sun 25 Oct | Offline packaging (no CDN dependency; local copy of three.js), 3 timed rehearsals, Q&A drill |
| — | 26–27 Oct | Conclave. Arrive early, test projector/HDMI, run on battery + hotspot fallback |

**Kill rule:** if something isn't working by its day, cut it to "planned" and don't sink the demo for it. ESP32 is the first thing to cut.

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
- Sim-to-real calibration: tune physics params from pilot data (this is what makes it a *true* digital twin).
- Machine-level RUL once run-to-failure data exists (or via public datasets like NASA bearing/IMS for the bearing template).
- Federated/cluster models: share learning across MSMEs without sharing raw data.

**Suggested order:** Route 2 (paper) and Route 1 (pilot) start immediately after the conclave. Routes 3–5 grow out of the pilot. Route 6 starts only once there are measured results.

---

## 9. Definition of done for 26 Oct

- [ ] Demo script (§3.1) runs end-to-end, offline, in < 4 min, 3 times in a row
- [ ] Every number on screen has a source-mode badge or an ⓘ formula
- [ ] Slides v2 use the merged wording (§1); no "TRL 6", no "verified savings"
- [ ] Metrics panel shows rule baseline vs RF (precision/recall/F1/confusion matrix)
- [ ] Backup video on 2 laptops + a pen drive
- [ ] Everyone can answer every question in §7

---

*Note: the shared ChatGPT conversation the team referenced couldn't be loaded from this environment. Paste its text in and its ideas can be folded into this plan.*
