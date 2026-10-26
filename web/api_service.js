/**
 * UDYAM-TWIN — Centralized Digital Twin API & WebSocket Client
 * Production-ready integration client with auto-reconnection, pub-sub telemetry bus,
 * and seamless offline simulated fallback for standalone evaluation.
 */

(function (window) {
  "use strict";

  // ---------------------------------------------------------------------------
  // 1. Configuration & Host Auto-Detection
  // ---------------------------------------------------------------------------
  const DEFAULT_HTTP_PORT = 8000;
  const isLocalStatic = window.location.protocol === "file:" || 
                        window.location.port === "5500" || 
                        window.location.port === "3000" || 
                        window.location.port === "8080" ||
                        !window.location.port;

  const getSavedBaseUrl = () => {
    try {
      return localStorage.getItem("UDYAM_API_BASE_URL");
    } catch (e) {
      return null;
    }
  };

  const API_BASE_URL = window.API_BASE_URL || 
                       getSavedBaseUrl() || 
                       (isLocalStatic ? `http://localhost:${DEFAULT_HTTP_PORT}` : window.location.origin);

  const WS_BASE_URL = API_BASE_URL.replace(/^http/, "ws");

  // ---------------------------------------------------------------------------
  // 2. Default Machine Metadata (Aligned with config/machines.yaml & schema.json)
  // ---------------------------------------------------------------------------
  const DEFAULT_MACHINES = [
    {
      machine_id: "SAW-01",
      name: "Bar Saw",
      template: "bar_saw",
      hero: false,
      station: "Station 1 · Infeed Cutting",
      nameplate: { rated_kw: 2.2, rated_rpm: 950 },
      iso_class: "Class 1",
      foundation: "rigid",
      modelFile: "saw_01.glb",
      previewImg: "/assets/3d/UDYAM_TWIN_Camera_SAW_Detail.jpg",
      layout: { x: -4.0, y: 0.0, order: 1 },
      thresholds: {
        zone_boundaries: { A_B: 1.4, B_C: 2.8, C_D: 4.5 },
        temp_warn_c: 65,
        temp_trip_c: 80
      }
    },
    {
      machine_id: "LATHE-01",
      name: "CNC Lathe",
      template: "cnc_lathe",
      hero: false,
      station: "Station 2 · Cylindrical Turning",
      nameplate: { rated_kw: 7.5, rated_rpm: 2500 },
      iso_class: "Class 2",
      foundation: "rigid",
      modelFile: "lathe_01.glb",
      previewImg: "/assets/3d/UDYAM_TWIN_Camera_LATHE_Detail.jpg",
      layout: { x: -1.5, y: 0.0, order: 2 },
      thresholds: {
        zone_boundaries: { A_B: 1.4, B_C: 2.8, C_D: 4.5 },
        temp_warn_c: 75,
        temp_trip_c: 90
      }
    },
    {
      machine_id: "VMC-01",
      name: "CNC Milling Machine",
      template: "cnc_mill",
      hero: true,
      station: "Station 3 · 3-Axis Hero Milling",
      nameplate: { rated_kw: 11.0, rated_rpm: 6000 },
      iso_class: "Class 2",
      foundation: "rigid",
      modelFile: "vmc_01.glb",
      previewImg: "/assets/3d/UDYAM_TWIN_Camera_Machine_Detail.jpg",
      layout: { x: 1.5, y: 0.0, order: 3 },
      thresholds: {
        zone_boundaries: { A_B: 1.4, B_C: 2.8, C_D: 4.5 },
        temp_warn_c: 70,
        temp_trip_c: 85
      }
    },
    {
      machine_id: "COMP-01",
      name: "Air Compressor",
      template: "air_compressor",
      hero: false,
      station: "Shared Utility · Pneumatics",
      nameplate: { rated_kw: 5.5, rated_rpm: 1450 },
      iso_class: "Class 1",
      foundation: "flexible",
      modelFile: "comp_01.glb",
      previewImg: "/assets/3d/UDYAM_TWIN_Camera_COMP_Detail.jpg",
      layout: { x: 4.0, y: 0.0, order: -1 },
      thresholds: {
        zone_boundaries: { A_B: 1.8, B_C: 3.5, C_D: 5.5 },
        pressure_warn_bar: 5.5,
        pressure_nominal_bar: 7.2
      }
    }
  ];

  // ---------------------------------------------------------------------------
  // 3. Realistic Offline Simulation State Generator
  // ---------------------------------------------------------------------------
  class SimulationEngine {
    constructor() {
      this.mode = "SIM"; // SIM, REPLAY, LIVE
      this.operatingMode = "TESTING"; // TESTING, REAL_DATA
      this.activeScenario = null;
      this.autoDemo = false;
      this.autoDemoTick = 0;
      this.machines = JSON.parse(JSON.stringify(DEFAULT_MACHINES));
      this.customMachines = [];
      this.faults = {}; // machine_id -> fault_name
      this.derates = { "VMC-01": 0, "LATHE-01": 0, "SAW-01": 0, "COMP-01": 0 };
      this.toolWearMin = { "VMC-01": 142.5, "LATHE-01": 89.2, "SAW-01": 56.0 };
      this.hCool = { "VMC-01": 1.0, "LATHE-01": 1.0, "SAW-01": 1.0 };
      this.partsDone = { "SAW-01": 348, "LATHE-01": 312, "VMC-01": 296 };
      
      // Alerts & Work Orders Store
      this.alerts = [
        {
          alert_id: "alt_demo_01",
          machine_id: "VMC-01",
          title: "Elevated Spindle Vibration · Approaching Zone C",
          severity: "WARN",
          status: "ACTIVE",
          rule: "VIB",
          measured: "Vibration RMS 2.92 mm/s (threshold: 2.80 mm/s)",
          inferred: "Probable tool unbalance or holder resonance on BT40 spindle",
          suggested: "Inspect cutting tool clamping, verify runout, or derate speed by 20%",
          occurrences: 3,
          created_at: new Date(Date.now() - 14 * 60 * 1000).toISOString(),
          updated_at: new Date(Date.now() - 2 * 60 * 1000).toISOString()
        },
        {
          alert_id: "alt_demo_02",
          machine_id: "COMP-01",
          title: "Line Pressure Fluctuating · Duty Cycle > 85%",
          severity: "INFO",
          status: "ACKNOWLEDGED",
          rule: "PRESSURE",
          measured: "Pressure 5.9 bar (nominal: 7.2 bar), duty cycle 88%",
          inferred: "Air leak or elevated pneumatic tool demand downline",
          suggested: "Check pneumatic line couplers at VMC-01 and SAW-01 stations",
          occurrences: 1,
          created_at: new Date(Date.now() - 38 * 60 * 1000).toISOString(),
          updated_at: new Date(Date.now() - 15 * 60 * 1000).toISOString()
        }
      ];

      this.workOrders = [
        {
          wo_id: "WO-1042",
          machine_id: "VMC-01",
          title: "Inspect Spindle BT40 Tool Holder & Runout",
          cause: "Elevated vibration harmonic at 2.92 mm/s",
          evidence: "Alert alt_demo_01 · Peak RMS exceeded 2.8 mm/s threshold",
          recommended_action: "Perform dial indicator runout check on spindle taper; clean collet bore",
          priority: "Medium",
          status: "Acknowledged",
          alert_id: "alt_demo_01",
          created_at: new Date(Date.now() - 12 * 60 * 1000).toISOString(),
          updated_at: new Date(Date.now() - 8 * 60 * 1000).toISOString()
        }
      ];

      this.events = [
        {
          event_id: "evt_001",
          timestamp: new Date().toISOString(),
          machine_id: "VMC-01",
          kind: "CALIBRATION",
          severity: "INFO",
          title: "Physics Twin RLS Parameter Updated",
          data: { param: "h_cool", from: 0.98, to: 1.01, version: "v1.3" }
        }
      ];

      this.economics = {
        hourly_revenue_inr: 8400,
        downtime_cost_inr_per_hr: 4500,
        oee_pct: 88.4,
        energy_tariff_inr_kwh: 7.8,
        grid_co2_kg_kwh: 0.82,
        estimated_line_loss_inr_hr: 380
      };

      this.rlPolicy = {
        trained: true,
        states_learned: 412,
        epsilon: 0.05,
        total_decisions: 1840,
        mean_reward_inr: 124.50,
        followed_rate: 0.92,
        prediction_accuracy: 0.89,
        last_action: "continue",
        q_values: {
          continue: 142.0,
          derate_20: 118.5,
          derate_40: 84.0,
          inspect: 65.0,
          maintain: -210.0
        }
      };

      this.iotDevices = [
        {
          device_id: "ESP32_VMC01",
          name: "VMC Spindle Vibration & Temp Probe",
          mac: "24:6F:28:AE:5B:10",
          machine_id: "VMC-01",
          online: true,
          type: "virtual_wokwi",
          battery_pct: 100,
          tare_offset: { vib_rms: 0.0, temp_c: 0.0 },
          last_seen: new Date().toISOString()
        },
        {
          device_id: "ESP32_LATHE01",
          name: "Lathe Headstock Monitor",
          mac: "24:6F:28:AE:5B:14",
          machine_id: "LATHE-01",
          online: true,
          type: "virtual_wokwi",
          battery_pct: 100,
          tare_offset: { vib_rms: 0.0, temp_c: 0.0 },
          last_seen: new Date().toISOString()
        }
      ];
    }

    generateSnapshot(m) {
      const mid = m.machine_id;
      const fault = this.faults[mid] || null;
      const derate = this.derates[mid] || 0;
      const isHero = m.hero;

      // Base readings per template
      let rpmNominal = m.nameplate.rated_rpm;
      let rpmSetpoint = Math.round(rpmNominal * (1 - derate / 100));
      let rpmMeasured = rpmSetpoint + Math.round((Math.random() - 0.5) * 12);
      
      let vibNominal = isHero ? 2.3 : 1.6;
      let tempNominal = isHero ? 48.5 : 42.0;
      let loadNominal = isHero ? 74.0 : 62.0;

      // Fault dynamics
      let zone = "A";
      let coolingLossPct = 0;
      let failureRiskPct = 6.2;
      let dqFlag = "OK";

      if (fault === "bearing_defect") {
        vibNominal += 2.8;
        failureRiskPct = 78.4;
      } else if (fault === "coolant_degradation") {
        coolingLossPct = 34.0;
        tempNominal += 18.5;
        failureRiskPct = 64.0;
      } else if (fault === "overload") {
        loadNominal += 28.0;
        rpmMeasured -= 120;
        failureRiskPct = 81.0;
      } else if (fault === "sensor_dropout") {
        dqFlag = "MISSING";
      } else if (fault === "sensor_stuck") {
        dqFlag = "STUCK";
      }

      // Add slight noise
      const vibVal = parseFloat((vibNominal + (Math.random() - 0.5) * 0.15).toFixed(2));
      const tempVal = parseFloat((tempNominal + (Math.random() - 0.5) * 0.3).toFixed(1));
      const loadVal = Math.min(100, Math.round(loadNominal + (Math.random() - 0.5) * 4));

      // ISO Zone classification
      const bnd = m.thresholds.zone_boundaries;
      if (vibVal < bnd.A_B) zone = "A";
      else if (vibVal < bnd.B_C) zone = "B";
      else if (vibVal < bnd.C_D) zone = "C";
      else zone = "D";

      const currentA = parseFloat((loadVal * (m.nameplate.rated_kw * 1.8) / 100).toFixed(1));
      const powerKw = parseFloat(((loadVal / 100) * m.nameplate.rated_kw).toFixed(2));

      // Tool wear & RUL
      const wearMin = this.toolWearMin[mid] || 80.0;
      const limitMin = 240.0;
      const remainingMin = Math.max(0, limitMin - wearMin);
      const rulMinutes = Math.round(remainingMin / (fault === "tool_wear_accel" ? 2.5 : 0.8));

      return {
        ts: new Date().toISOString(),
        clock: "wall_utc",
        machine_id: mid,
        name: m.name,
        template: m.template,
        mode: this.mode,
        source_label: this.mode === "LIVE" ? "IoT Gateway (ESP32)" : "Digital Twin SIM",
        state: derate > 0 ? "DERATE" : (fault ? "ATTENTION" : "RUN"),
        active_scenario: this.activeScenario,
        dq_flag: dqFlag,
        rpm: dqFlag === "MISSING" ? null : rpmMeasured,
        rpm_setpoint: rpmSetpoint,
        rpm_nominal: rpmNominal,
        derate_pct: derate,
        vibration_rms_mm_s: dqFlag === "MISSING" ? null : vibVal,
        temp_c: dqFlag === "MISSING" ? null : tempVal,
        temp_signal: m.template === "air_compressor" ? "discharge_temp" : "process_temp",
        load_pct: loadVal,
        current_a: currentA,
        voltage_v: 415.0,
        power_kw: powerKw,
        energy_kwh: parseFloat((340.5 + (this.partsDone[mid] || 0) * 0.42).toFixed(1)),
        severity: {
          status: zone === "A" || zone === "B" ? "NORMAL" : (zone === "C" ? "WARN" : "CRITICAL"),
          zone: zone,
          reason: zone === "D" ? "Vibration above ISO 20816-3 Zone C boundary" : (zone === "C" ? "Elevated harmonic in Zone C" : "Nominal operational range"),
          label: `Zone ${zone}`
        },
        risk: {
          rules: failureRiskPct > 60 ? "HIGH" : (failureRiskPct > 25 ? "MEDIUM" : "LOW"),
          model: parseFloat((failureRiskPct / 100).toFixed(2)),
          top_signals: ["vibration_rms", "process_temp", "cutting_torque"],
          ml: {
            status: "OK",
            failure_risk_pct: failureRiskPct,
            top_features: [
              { label: "Spindle Vibration RMS", value: `${vibVal} mm/s`, contribution_pct: 42 },
              { label: "Process Over-Temp Gap", value: `${tempVal - 24} °C`, contribution_pct: 31 },
              { label: "Tool Wear Accumulated", value: `${Math.round(wearMin)} min`, contribution_pct: 18 }
            ],
            top_mode: fault ? fault.toUpperCase() : "NONE"
          }
        },
        forecast: fault === "coolant_degradation" ? { minutes_to_warning: 4.8, warning: "OVERTEMP" } : null,
        rul: {
          status: "OK",
          label: "estimate",
          method: "AI4I Cumulative Wear & Cutting Speed Regression",
          tool_wear_min: Math.round(wearMin),
          limit_min: limitMin,
          remaining_tool_min: Math.round(remainingMin),
          wear_rate_min_per_h: fault === "tool_wear_accel" ? 64.0 : 12.5,
          rul_minutes: rulMinutes
        },
        calibration: {
          version: "v1.3",
          frozen: fault === "bearing_defect",
          freeze_reason: fault === "bearing_defect" ? "Non-thermal anomaly detected (Vibration Spike)" : null,
          h_cool: this.hCool[mid] || 1.0,
          cooling_loss_pct: coolingLossPct
        },
        parts_done: this.partsDone[mid] || null
      };
    }

    generateLineMetrics() {
      let faultedCount = Object.keys(this.faults).length;
      let baseThroughput = 42.0;
      let throughput = Math.max(12, Math.round(baseThroughput - faultedCount * 8.5));
      let lineLoss = Math.round(faultedCount * 650 + (42 - throughput) * 45);

      return {
        ts: new Date().toISOString(),
        throughput_parts_per_hr: throughput,
        line_loss_inr_per_hr: lineLoss,
        bottleneck: faultedCount > 0 ? "VMC-01" : "SAW-01",
        air_supply_ok: !this.faults["COMP-01"],
        stations: this.machines.map(m => ({
          machine_id: m.machine_id,
          state: this.faults[m.machine_id] ? "DERATED" : "RUNNING",
          buffer_in: Math.floor(Math.random() * 5 + 3),
          buffer_cap: 12
        }))
      };
    }
  }

  // ---------------------------------------------------------------------------
  // 4. API Client Core Class
  // ---------------------------------------------------------------------------
  class UdyamAPIClient {
    constructor() {
      this.baseUrl = API_BASE_URL;
      this.wsUrl = WS_BASE_URL;
      this.ws = null;
      this.wsConnected = false;
      this.reconnectAttempts = 0;
      this.reconnectTimer = null;
      this.listeners = new Map(); // event -> Set of callbacks
      this.sim = new SimulationEngine();
      this.simTicker = null;
      this.operatorToken = null;
      
      this.init();
    }

    init() {
      // No silent fallback: when the backend is unreachable the UI shows it (status event), it never fakes data.
      // The SimulationEngine and handleFallbackEndpoint below are kept but unused (standalone design previews only).
      this.connectWebSocket();
    }

    // -------------------------------------------------------------------------
    // Event Bus (Pub-Sub)
    // -------------------------------------------------------------------------
    on(event, callback) {
      if (!this.listeners.has(event)) {
        this.listeners.set(event, new Set());
      }
      this.listeners.get(event).add(callback);
      return () => this.off(event, callback);
    }

    off(event, callback) {
      if (this.listeners.has(event)) {
        this.listeners.get(event).delete(callback);
      }
    }

    emit(event, data) {
      if (this.listeners.has(event)) {
        this.listeners.get(event).forEach(cb => {
          try { cb(data); } catch (err) { console.error(`Error in [${event}] listener:`, err); }
        });
      }
    }

    // -------------------------------------------------------------------------
    // WebSocket Lifecycle
    // -------------------------------------------------------------------------
    connectWebSocket() {
      if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
        return;
      }

      const socketEndpoint = `${this.wsUrl}/ws/telemetry`;
      try {
        this.ws = new WebSocket(socketEndpoint);

        this.ws.onopen = () => {
          this.wsConnected = true;
          this.reconnectAttempts = 0;
          this.emit("status", { connected: true, live: true, endpoint: socketEndpoint });
          console.info(`[UdyamAPI] Connected to backend WebSocket: ${socketEndpoint}`);
        };

        this.ws.onmessage = (event) => {
          try {
            const frame = JSON.parse(event.data);
            this.handleIncomingFrame(frame);
          } catch (e) {
            console.warn("[UdyamAPI] Failed to parse WebSocket frame:", event.data);
          }
        };

        this.ws.onclose = () => {
          const wasConnected = this.wsConnected;
          this.wsConnected = false;
          this.emit("status", { connected: false, live: false, endpoint: socketEndpoint });
          
          if (wasConnected) {
            console.warn("[UdyamAPI] WebSocket disconnected. Reconnecting…");
          }
          this.scheduleReconnect();
        };

        this.ws.onerror = () => {
          this.wsConnected = false;
        };
      } catch (err) {
        this.wsConnected = false;
        this.scheduleReconnect();
      }
    }

    scheduleReconnect() {
      if (this.reconnectTimer) return;
      this.reconnectAttempts++;
      const delay = Math.min(1000 * Math.pow(1.5, this.reconnectAttempts), 8000);
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        this.connectWebSocket();
      }, delay);
    }

    sendWsCommand(cmdObj) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify(cmdObj));
        return true;
      }
      return false;
    }

    handleIncomingFrame(frame) {
      const type = frame.type;
      if (!type) return;

      this.emit(type, frame);

      if (type === "snapshot") {
        this.emit("telemetry", frame);
      } else if (type === "alert") {
        this.emit("alert_update", frame);
      } else if (type === "work_order") {
        this.emit("work_order_update", frame);
      }
    }

    // -------------------------------------------------------------------------
    // Fallback Simulation Ticker (Guarantees UI is ALWAYS alive)
    // -------------------------------------------------------------------------
    startSimulationHeartbeat() {
      if (this.simTicker) clearInterval(this.simTicker);

      this.simTicker = setInterval(() => {
        // Only broadcast simulated data if real backend is NOT connected
        if (!this.wsConnected) {
          const allMachines = [...this.sim.machines, ...this.sim.customMachines];
          const snapshots = allMachines.map(m => this.sim.generateSnapshot(m));
          const line = this.sim.generateLineMetrics();

          snapshots.forEach(snap => {
            this.emit("snapshot", snap);
            this.emit("telemetry", snap);
          });

          this.emit("line", line);

          // Handle Auto-Demo sequence in simulation
          if (this.sim.autoDemo) {
            this.sim.autoDemoTick++;
            this.handleSimulatedAutoDemo(this.sim.autoDemoTick);
          }
        }
      }, 1000);
    }

    handleSimulatedAutoDemo(tick) {
      const mod = tick % 90;
      if (mod === 0) {
        // Reset to clean baseline
        this.sim.faults = {};
        this.sim.derates["VMC-01"] = 0;
        this.emit("event", { kind: "DEMO", title: "Auto Demo: Baseline Stage (0s)", message: "Line running in nominal state" });
      } else if (mod === 15) {
        // Inject vibration fault
        this.sim.faults["VMC-01"] = "bearing_defect";
        this.emit("event", { kind: "DEMO", title: "Auto Demo: Vibration Fault (15s)", message: "VMC-01 bearing vibration escalating" });
      } else if (mod === 27) {
        // Auto derate triggered
        this.sim.derates["VMC-01"] = 20;
        this.emit("event", { kind: "DEMO", title: "Auto Demo: Auto-Derate 20% (27s)", message: "Twin protected spindle by derating 20%" });
      } else if (mod === 40) {
        // Work order created
        this.emit("event", { kind: "DEMO", title: "Auto Demo: Work Order Generated (40s)", message: "WO-DEMO logged in SQLite" });
      } else if (mod === 65) {
        // Repair completed
        this.sim.faults = {};
        this.sim.derates["VMC-01"] = 0;
        this.emit("event", { kind: "DEMO", title: "Auto Demo: Simulated Repair (65s)", message: "Vibration normalised to Zone A" });
      }
    }

    // -------------------------------------------------------------------------
    // REST API Request Wrapper
    // -------------------------------------------------------------------------
    async request(path, options = {}) {
      const url = `${this.baseUrl}${path}`;
      const headers = {
        "Content-Type": "application/json",
        ...(options.headers || {})
      };

      if (this.operatorToken) {
        headers["X-Operator-Token"] = this.operatorToken;
      }
      if (window.udyamAuth && !headers.Authorization) {  // Firebase ID token (index.html sign-in gate)
        try { headers.Authorization = `Bearer ${await window.udyamAuth.idToken()}`; } catch (_) { /* signed out */ }
      }

      try {
        const res = await fetch(url, { ...options, headers });
        if (!res.ok) {
          const errorData = await res.json().catch(() => ({}));
          const d = errorData.detail;
          const message = (d && d.message) || (Array.isArray(d) ? d.map(e => e.msg).join("; ") : d) || res.statusText;
          throw { status: res.status, message, data: errorData };
        }
        const contentType = res.headers.get("content-type");
        if (contentType && contentType.includes("application/json")) {
          return await res.json();
        }
        return await res.text();
      } catch (err) {
        // Real errors reach the caller (no mock responses); a network failure is reported as status 0
        if (err && err.status) throw err;
        throw { status: 0, message: "Backend unreachable", data: null };
      }
    }

    // -------------------------------------------------------------------------
    // Fallback Mock Handlers for Disconnected / Demo Mode
    // -------------------------------------------------------------------------
    handleFallbackEndpoint(path, options = {}) {
      const method = (options.method || "GET").toUpperCase();

      if (path === "/api/health") {
        return { status: "ok", mode: "standalone_demo", timestamp: new Date().toISOString() };
      }
      if (path === "/api/registry" || path === "/api/config") {
        return {
          generator: "udyam_sim_standalone",
          machines: [...this.sim.machines, ...this.sim.customMachines]
        };
      }
      if (path === "/api/snapshots") {
        return [...this.sim.machines, ...this.sim.customMachines].map(m => this.sim.generateSnapshot(m));
      }
      if (path === "/api/alerts" || path.startsWith("/api/alerts?")) {
        return this.sim.alerts;
      }
      if (path === "/api/work-orders" || path.startsWith("/api/work-orders?")) {
        return this.sim.workOrders;
      }
      if (path === "/api/events" || path.startsWith("/api/events?")) {
        return this.sim.events;
      }
      if (path === "/api/economics") {
        return this.sim.economics;
      }
      if (path === "/api/rl" || path === "/api/rl/metrics") {
        return this.sim.rlPolicy;
      }
      if (path === "/api/iot/status") {
        return { mqtt_connected: false, devices_online: 2, mode: "virtual_sim" };
      }
      if (path === "/api/iot/devices") {
        return this.sim.iotDevices;
      }
      if (path === "/api/mode") {
        return { mode: this.sim.mode, sources: { "VMC-01": this.sim.mode } };
      }
      if (path === "/api/operating-mode") {
        return { mode: this.sim.operatingMode, read_only: this.sim.operatingMode === "REAL_DATA" };
      }
      if (path === "/api/auto_demo") {
        return { on: this.sim.autoDemo, tick: this.sim.autoDemoTick };
      }
      if (path === "/api/whatif/options") {
        return {
          durations_min: [5, 10, 30, 60],
          speeds: [1, 10, 50, 100],
          faults: ["coolant_degradation", "bearing_defect", "overload", "tool_wear_accel"],
          machines: this.sim.machines.map(m => ({ machine_id: m.machine_id, name: m.name }))
        };
      }
      if (path === "/api/datasets/cmapss") {
        return {
          status: "OK",
          unit: "FD001",
          max_cycles: 362,
          prediction: { current_cycle: 140, predicted_rul: 74, confidence_pct: 91.4 }
        };
      }
      if (path === "/api/datasets/ims") {
        return {
          status: "OK",
          bearing: "B1_Ch3",
          zones: { A: "0-14 days", B: "15-28 days", C: "29-33 days", D: "34+ days (Failure)" }
        };
      }

      // Default mock success response
      return { success: true, mocked: true, path };
    }

    // -------------------------------------------------------------------------
    // Core Domain API Methods
    // -------------------------------------------------------------------------

    // 1. Health & Config
    async getHealth() { return this.request("/api/health"); }
    async getConfig() { return this.request("/api/config"); }
    async getRegistry() { return this.request("/api/registry"); }
    async getSnapshots() { return this.request("/api/snapshots"); }

    // 2. Fault Injection & Scenarios — every action goes to the backend's REST endpoint and returns its real
    //    response or error (Real Data mode answers 409; the UI must show it, never a local "success").
    async injectFault(machineId, fault) {
      return this.request("/api/faults/inject", { method: "POST", body: JSON.stringify({ machine_id: machineId, fault }) });
    }

    async clearFaults(machineId = null) {
      return this.request("/api/faults/clear", { method: "POST", body: JSON.stringify({ machine_id: machineId }) });
    }

    async setDerate(machineId, pct) {
      return this.request("/api/derate", { method: "POST", body: JSON.stringify({ machine_id: machineId, pct }) });
    }

    async getScenarios() { return this.request("/api/scenarios"); }
    async startScenario(scenarioId) {
      return this.request(`/api/scenarios/${encodeURIComponent(scenarioId)}/start`, { method: "POST" });
    }

    async resolveScenario(machineId = null) {
      const query = machineId ? `?machine_id=${encodeURIComponent(machineId)}` : "";
      return this.request(`/api/scenarios/resolve${query}`, { method: "POST" });
    }

    async resetFactory() {
      return this.request("/api/reset", { method: "POST" });
    }

    async toggleAutoDemo(on) {
      return this.request(`/api/auto_demo?on=${on ? "true" : "false"}`, { method: "POST" });
    }

    // 3. Alerts & Lifecycle
    async getAlerts(includeResolved = false) {
      return this.request(`/api/alerts?include_resolved=${includeResolved}`);
    }

    async acknowledgeAlert(alertId, operatorName = "Operator") {
      return this.request(`/api/alerts/${encodeURIComponent(alertId)}/acknowledge`, {
        method: "POST", body: JSON.stringify({ by: operatorName }) });
    }

    async resolveAlert(alertId, resolutionNote = "Operator verified clear") {
      return this.request(`/api/alerts/${encodeURIComponent(alertId)}/resolve`, {
        method: "POST", body: JSON.stringify({ resolution: resolutionNote }) });
    }

    async generateWorkOrderFromAlert(alertId) {
      return this.request(`/api/alerts/${encodeURIComponent(alertId)}/work-order`, { method: "POST" });
    }

    // 4. Work Orders
    async getWorkOrders(status = null) {
      const query = status ? `?status=${encodeURIComponent(status)}` : "";
      return this.request(`/api/work-orders${query}`);
    }

    async createWorkOrder(data) {
      return this.request("/api/work-orders", { method: "POST", body: JSON.stringify(data) });
    }

    async updateWorkOrderStatus(woId, status, notes = "") {
      return this.request(`/api/work-orders/${encodeURIComponent(woId)}`, {
        method: "PATCH", body: JSON.stringify({ status, notes }) });
    }

    // Tool change: the operator's measured edge wear in minutes (0 for a new tool)
    async recordToolChange(machineId, measuredWearMin = 0) {
      return this.request("/api/tool-wear", {
        method: "POST", body: JSON.stringify({ machine_id: machineId, measured_wear_min: measuredWearMin }) });
    }

    // 5. Machine Onboarding — future scope: the backend answers 403 feature_disabled (four default machines)
    async addMachine(machineData) {
      return this.request("/api/machines", { method: "POST", body: JSON.stringify(machineData) });
    }

    async deleteMachine(machineId) {
      return this.request(`/api/machines/${encodeURIComponent(machineId)}`, { method: "DELETE" });
    }

    // 6. What-If Simulation
    async getWhatIfOptions() { return this.request("/api/whatif/options"); }
    
    async runWhatIf(payload) {
      return this.request("/api/whatif/run", {
        method: "POST",
        body: JSON.stringify(payload)
      });
    }

    async getWhatIfJob() { return this.request("/api/whatif/job"); }
    async getWhatIfResults(rid = null) {
      const endpoint = rid ? `/api/whatif/results/${rid}` : "/api/whatif/results";
      return this.request(endpoint);
    }

    // 7. Reinforcement Learning
    async getRLStatus() { return this.request("/api/rl"); }
    async getRLMetrics() { return this.request("/api/rl/metrics"); }
    async proposeRLAction(machineId) {
      return this.request(`/api/rl/${machineId}/propose`, { method: "POST" });
    }

    // 8. IoT & Edge Hardware Bridge
    async getIoTStatus() { return this.request("/api/iot/status"); }
    async getIoTDevices() { return this.request("/api/iot/devices"); }
    async bindIoTDevice(deviceId, machineId) {
      return this.request(`/api/iot/devices/${deviceId}/bind`, {
        method: "POST",
        body: JSON.stringify({ machine_id: machineId })
      });
    }
    async tareIoTDevice(deviceId) {
      return this.request(`/api/iot/devices/${deviceId}/tare`, { method: "POST" });
    }

    // 9. Vibration Diagnostics
    async getVibrationOverview() { return this.request("/api/vibration"); }
    async getMachineVibration(machineId) { return this.request(`/api/vibration/${machineId}`); }
    async recordVibrationWaveform(machineId) {
      return this.request(`/api/vibration/${machineId}/recording`, { method: "POST" });
    }

    // 10. AI Assistant / Twin Advisor
    async sendChatMessage(message, conversationId = null) {
      return this.request("/api/chat", {
        method: "POST",
        body: JSON.stringify({ message, conversation_id: conversationId })
      });
    }

    async approveProposal(proposalId, operatorToken = null) {
      const headers = {};
      if (operatorToken) headers["X-Operator-Token"] = operatorToken;
      return this.request(`/api/chat/proposals/${encodeURIComponent(proposalId)}/approve`, {
        method: "POST",
        headers,
        body: JSON.stringify({ operator: "Operator" })
      });
    }

    async dismissProposal(proposalId) {
      return this.request(`/api/chat/proposals/${encodeURIComponent(proposalId)}/dismiss`, {
        method: "POST",
        body: JSON.stringify({ operator: "Operator" })
      });
    }

    // 11. Modes & Export
    async getMode() { return this.request("/api/mode"); }
    async setMode(mode, machineId = null) {
      return this.request("/api/mode", {
        method: "POST",
        body: JSON.stringify({ mode, machine_id: machineId })
      });
    }

    async getOperatingMode() { return this.request("/api/operating-mode"); }
    async setOperatingMode(mode) {
      return this.request("/api/operating-mode", {
        method: "POST",
        body: JSON.stringify({ mode })
      });
    }

    getExportUrl(section = "summary", format = "csv") {
      if (format === "xlsx") {
        return `${this.baseUrl}/api/reports/export.xlsx`;
      }
      return `${this.baseUrl}/api/reports/export.csv?section=${encodeURIComponent(section)}`;
    }
  }

  // Instantiate and export globally
  window.UdyamAPI = new UdyamAPIClient();

})(window);
