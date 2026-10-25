/**
 * UDYAM-TWIN Industrial Digital Twin Client
 * Handles real-time telemetry over WebSocket, UI state management,
 * fault injection, calibration tracking, work orders, and MSME economics.
 */

const state = {
  machines: {},
  obs: {},
  twin: {},
  line: null,
  selectedMachineId: "VMC-01",
  autoDemo: true,
  workOrders: [],
  events: [],           // newest first, capped at EVENT_CAP; the log renders a filtered view
  eventFilter: { sev: "ALL", kind: "ALL" },
  expandedEvents: new Set(),
  alerts: [],
  snap: {},             // latest snapshot per machine (zones come from the configured ISO 20816-3 profile)
  economics: {
    inr_per_part: 450.0,
    downtime_cost_per_hr: 2500.0,
    electricity_tariff_per_kwh: 8.5,
    cea_co2_factor: 0.716,
  },
  metrics: null,
};

let ws = null;
let reconnectTimer = null;

// Determine WebSocket URL
const loc = window.location;
const wsProto = loc.protocol === "https:" ? "wss://" : "ws://";
const wsHost = loc.host || "localhost:8000";
const wsUrl = `${wsProto}${wsHost}/ws/telemetry`;

// Helpers
const fmt = (v, d = 1) => (v === null || v === undefined ? "—" : (typeof v === "number" ? v.toFixed(d) : v));
const esc = s => String(s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function initWebSocket() {
  updateStatus("connecting...", false);
  try {
    ws = new WebSocket(wsUrl);
  } catch (err) {
    updateStatus("failed, retrying...", false);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    updateStatus("connected", true);
    if (reconnectTimer) clearTimeout(reconnectTimer);
  };

  ws.onclose = () => {
    updateStatus("disconnected, retrying...", false);
    scheduleReconnect();
  };

  ws.onerror = () => {
    updateStatus("connection error", false);
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleMessage(msg);
    } catch (err) {
      console.error("Malformed telemetry frame:", err);
    }
  };
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(initWebSocket, 2000);
}

function updateStatus(text, connected) {
  const el = document.getElementById("connectionStatusText");
  const dot = document.getElementById("statusDot");
  if (el) el.textContent = text;
  if (dot) {
    if (connected) dot.classList.add("connected");
    else dot.classList.remove("connected");
  }
}

function handleMessage(msg) {
  if (!msg || !msg.type) return;

  if (msg.type === "registry") {
    state.machines = Object.fromEntries(msg.machines.map(m => [m.machine_id, m]));
    updateMachineDropdowns();
    renderAll();
  } else if (msg.type === "observed") {
    state.obs[msg.machine_id] = msg;
    renderMachineCard(msg.machine_id);
    updateModeBadge();
    if (msg.machine_id === state.selectedMachineId) {
      renderDetailPanel();
      if (window.updateMachineVisualization) {
        window.updateMachineVisualization(msg.machine_id, msg, state.twin[msg.machine_id]);
      } else if (window.updateSpindleSpeed && typeof msg.rpm === 'number') {
        window.updateSpindleSpeed(msg.rpm);
      }
    }
  } else if (msg.type === "twin") {
    state.twin[msg.machine_id] = msg;
    renderMachineCard(msg.machine_id);
    if (msg.machine_id === state.selectedMachineId) {
      renderDetailPanel();
      if (window.updateMachineVisualization && state.obs[msg.machine_id]) {
        window.updateMachineVisualization(msg.machine_id, state.obs[msg.machine_id], msg);
      }
    }
  } else if (msg.type === "line") {
    state.line = msg;
    renderLineBanner(msg);
    renderEconomics();
  } else if (msg.type === "snapshot") {
    state.snap[msg.machine_id] = msg;
  } else if (msg.type === "alert") {
    scheduleAlertsFetch();
  } else if (msg.type === "event") {
    logEvent(msg);
    if (msg.kind === "MACHINE_ADDED") fetchRegistry();
    if (msg.kind === "WORK_ORDER") fetchWorkOrders();
  }
}

function updateMachineDropdowns() {
  const sel = document.getElementById("faultMachineSelect");
  if (!sel) return;
  const ids = Object.keys(state.machines);
  sel.innerHTML = ids.map(id => `<option value="${esc(id)}">${esc(id)} (${esc(state.machines[id].template)})</option>`).join("");
}

function renderAll() {
  renderMachineGrid();
  if (state.line) renderLineBanner(state.line);
  renderDetailPanel();
}

function renderMachineGrid() {
  const grid = document.getElementById("machineGrid");
  if (!grid) return;

  const sorted = Object.values(state.machines).sort((a, b) =>
    (a.layout.order < 0) - (b.layout.order < 0) || a.layout.order - b.layout.order
  );

  grid.innerHTML = sorted.map(m => {
    const isSelected = m.machine_id === state.selectedMachineId ? "selected" : "";
    return `
      <div class="machine-card ${isSelected}" id="card-${esc(m.machine_id)}" onclick="selectMachine('${esc(m.machine_id)}')">
        <div class="card-header">
          <div class="card-title">
            <h3>${esc(m.name)}</h3>
            ${m.hero ? '<span class="hero-star" title="Detailed Hero Asset">★ Hero</span>' : ''}
          </div>
          <div class="machine-badges" id="badges-${esc(m.machine_id)}">
            <span class="pill OK">RUN</span>
          </div>
        </div>
        <div style="font-size: 11px; color: var(--text-muted);">
          ID: <b>${esc(m.machine_id)}</b> · ${esc(m.template)} · Rated: ${esc(m.nameplate?.rated_kw || 7.5)} kW
        </div>
        <div id="content-${esc(m.machine_id)}">
          <div class="text-muted" style="font-size: 12px;">Waiting for telemetry tick...</div>
        </div>
      </div>
    `;
  }).join("");

  sorted.forEach(m => renderMachineCard(m.machine_id));
}

// Data source per schema v1.0: SIM / REPLAY / LIVE. A LIVE feed with bad data quality
// (STALE, MISSING, ...) shows the flag too, so a dead sensor never looks healthy.
function sourceBadge(o) {
  const mode = o.source_mode || "SIM";
  const dq = o.dq_flag || "OK";
  const degraded = mode === "LIVE" && dq !== "OK";
  const label = degraded ? `${mode} · ${dq}` : mode;
  const title = mode === "LIVE" ? `Real sensor data from the IoT hub (data quality: ${dq})`
    : mode === "REPLAY" ? "Replayed AI4I 2020 data" : "Simulated plant data";
  return `<span class="pill src-${esc(mode)}${degraded ? " src-degraded" : ""}" title="${esc(title)}">${esc(label)}</span>`;
}

function updateModeBadge() {
  const el = document.getElementById("globalModeBadge");
  if (!el) return;
  const live = Object.values(state.obs).filter(o => o.source_mode === "LIVE").length;
  const text = live ? `LIVE · ${live} MACHINE${live > 1 ? "S" : ""}` : "SIMULATION MODE (SUPERVISED SIL)";
  if (el.textContent !== text) {
    el.textContent = text;
    el.classList.toggle("live", live > 0);
  }
}

function renderMachineCard(machineId) {
  const container = document.getElementById(`content-${machineId}`);
  const badgesContainer = document.getElementById(`badges-${machineId}`);
  if (!container) return;

  const m = state.machines[machineId];
  const o = state.obs[machineId] || {};
  const t = state.twin[machineId] || {};

  if (badgesContainer) {
    const risk = t.risk?.rules || "LOW";
    const plc = o.plc_state || "RUN";
    badgesContainer.innerHTML = `
      ${sourceBadge(o)}
      <span class="pill ${esc(plc)}">${esc(plc)}</span>
      <span class="pill ${esc(risk)}">${esc(risk)} RISK</span>
    `;
  }

  let fields = [];
  if (m.template.startsWith("cnc")) {
    fields = [
      ["Spindle Speed", `${fmt(o.rpm, 0)} RPM`],
      ["Torque", `${fmt(o.torque_nm, 1)} Nm`],
      ["Process Temp", `${fmt(o.process_temp_k, 2)} K`],
      ["Twin Predicted", `${fmt(t.predicted?.process_temp_k, 2)} K`],
      ["Tool Wear", `${fmt(o.tool_wear_min, 1)} min`],
      ["Spindle Vibration", `${fmt(o.vibration_rms_mm_s, 2)} mm/s`],
    ];
  } else if (m.template === "air_compressor") {
    fields = [
      ["Line Pressure", `${fmt(o.pressure_bar, 2)} bar`],
      ["Duty Cycle", `${fmt(o.duty_pct, 1)} %`],
      ["Discharge Temp", `${fmt(o.discharge_temp_c, 1)} °C`],
      ["Vibration", `${fmt(o.vibration_rms_mm_s, 2)} mm/s`],
    ];
  } else {
    fields = [
      ["Blade Speed", `${fmt(o.rpm, 0)} RPM`],
      ["Blade Wear", `${fmt(o.blade_wear_pct, 1)} %`],
      ["Motor Power", `${fmt(o.power_kw, 2)} kW`],
      ["Vibration", `${fmt(o.vibration_rms_mm_s, 2)} mm/s`],
    ];
  }

  const res = t.residual || { signal: "process_temp_k", value: 0.0, state: "IN_BAND" };
  const params = t.params || { version: "v1.0", values: { h_cool: 1.0 }, frozen: false };
  const forecast = t.forecast || { minutes_to_warning: null };

  container.innerHTML = `
    <table class="telemetry-table">
      ${fields.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")}
    </table>
    <div class="twin-section">
      <div class="twin-header">
        <span>Residual (${esc(res.signal || "e")})</span>
        <span class="pill ${esc(res.state || 'IN_BAND')}">${fmt(res.value, 2)} (${esc(res.state)})</span>
      </div>
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <span>Params ${esc(params.version || 'v1.0')} ${params.frozen ? '<b style="color:var(--status-crit)">[FROZEN]</b>' : ''}</span>
        <span style="font-family:var(--font-mono); font-size:11px;">
          ${Object.entries(params.values || {}).map(([k, v]) => `${esc(k)}=${fmt(v, 2)}`).join(" ")}
        </span>
      </div>
      ${forecast.warning ? `
        <div style="background:rgba(239,68,68,0.15); color:#f87171; padding:4px 6px; border-radius:4px; font-weight:600; font-size:11px;">
          ⚠ ${esc(forecast.warning)} in ~${fmt(forecast.minutes_to_warning, 0)} min at current rate
        </div>
      ` : ''}
    </div>
  `;
}

function selectMachine(machineId) {
  const prevId = state.selectedMachineId;
  state.selectedMachineId = machineId;
  document.querySelectorAll(".machine-card").forEach(c => c.classList.remove("selected"));
  const card = document.getElementById(`card-${machineId}`);
  if (card) card.classList.add("selected");

  renderDetailPanel();
  if (prevId !== machineId) fetchVibration();

  if (window.initThreeView) {
    if (prevId !== machineId || !document.getElementById("threeCanvas")) {
      window.initThreeView(machineId);
    }
  }
}

// "Zone B · ISO 20816-3 (provisional)" from the server's severity assessment of the latest snapshot
function isoZone(mid) {
  const sev = (state.snap[mid] || {}).severity;
  if (!sev || !sev.zone) return "ISO 20816-3 zone: unavailable";
  return `Zone ${sev.zone} · ISO 20816-3${sev.status === "PROVISIONAL" ? " (provisional)" : ""}`;
}

function renderDetailPanel() {
  const panel = document.getElementById("detailView");
  if (!panel) return;

  const mid = state.selectedMachineId;
  const m = state.machines[mid];
  if (!m) {
    panel.innerHTML = "<div class='text-muted'>Select an asset to inspect digital twin.</div>";
    return;
  }

  // Ensure persistent split containers exist: detailMetrics and threeContainer
  let metricsContainer = document.getElementById("detailMetrics");
  let threeContainer = document.getElementById("threeContainer");
  if (!metricsContainer || !threeContainer) {
    panel.innerHTML = `
      <div id="detailMetrics"></div>
      <div id="threeContainer" style="margin-top:16px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
          <div style="display:flex; align-items:center; gap:8px;">
            <h3 id="threeTitle" style="font-size:14px; margin:0; font-weight:700;">3D Kinematic Twin</h3>
            <span class="pill OK" id="threeSpindleRpm">— RPM</span>
          </div>
          <div style="display:flex; gap:6px;">
            <button class="btn btn-sm" onclick="if(window.showFactoryOverview) window.showFactoryOverview()">Factory Floor 🏭</button>
            <button class="btn btn-sm" onclick="if(window.resetThreeView) window.resetThreeView()">Reset Camera</button>
            <button class="btn btn-sm" id="btnToggleAnim" onclick="if(window.toggleThreeAnimation) window.toggleThreeAnimation()">Pause</button>
          </div>
        </div>
        <div id="threeCanvasWrapper" style="position:relative; width:100%; border:1px solid var(--border-subtle); border-radius:var(--radius-sm); overflow:hidden; background:#0a0e17;"></div>
        <div style="display:flex; justify-content:space-between; font-size:11px; color:var(--text-muted); margin-top:4px;">
          <span>Left-click + drag: Orbit · Scroll: Zoom · Right-click + drag: Pan</span>
          <span id="threeModelBadge" style="font-weight:600; color:var(--status-ok);">Blender Photorealistic GLB</span>
        </div>
      </div>
    `;
    metricsContainer = document.getElementById("detailMetrics");
    threeContainer = document.getElementById("threeContainer");
  }

  const o = state.obs[mid] || {};
  const t = state.twin[mid] || {};
  const r = t.residual || { signal: "process_temp_k", value: 0.0, ewma: 0.0, cusum: 0.0, band: 0.6, state: "IN_BAND" };
  const p = t.params || { version: "v1.0", values: {}, frozen: false, freeze_reason: null };
  const f = t.forecast || {};
  const risk = t.risk || { rules: "LOW", model: 0.01, top_signals: [] };

  // Render asset-specific technical cards
  let specificHtml = "";

  if (m.template.startsWith("cnc")) {
    specificHtml = `
      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px;">
        <div class="calc-card">
          <div class="calc-label">Process Temp (Observed / Target)</div>
          <div class="calc-val">${fmt(o.process_temp_k, 2)} / ${fmt(t.predicted?.process_temp_k, 2)} K</div>
          <div class="formula-hint">T_air + 8.0 + 0.7 × P / h_cool (Thermal model)</div>
        </div>
        <div class="calc-card">
          <div class="calc-label">Thermal Residual e (Obs - Pred)</div>
          <div class="calc-val ${r.state}">${fmt(r.value, 3)}</div>
          <div class="formula-hint">EWMA: ${fmt(r.ewma, 3)} · CUSUM: ${fmt(r.cusum, 2)} (${esc(r.state)})</div>
        </div>
        <div class="calc-card">
          <div class="calc-label">Self-Calibration State</div>
          <div class="calc-val">${esc(p.version)} ${p.frozen ? '<span style="color:var(--status-crit)">(FROZEN)</span>' : '<span style="color:var(--status-ok)">(ACTIVE)</span>'}</div>
          <div class="formula-hint">${p.freeze_reason ? `Frozen: ${esc(p.freeze_reason)} (Safeguard)` : 'Bounded RLS online parameter estimation'}</div>
        </div>
        <div class="calc-card">
          <div class="calc-label">AI4I Failure Risk Model</div>
          <div class="calc-val">${risk.model !== null ? (risk.model * 100).toFixed(1) + '%' : 'N/A'}</div>
          <div class="formula-hint">Rule status: <b>${esc(risk.rules)}</b> · Signals: ${esc(risk.top_signals?.join(", ") || 'nominal')}</div>
        </div>
      </div>

      <div style="background:var(--bg-card); border-radius:var(--radius-sm); padding:12px; margin-top:10px;">
        <h4 style="font-size:13px; margin-bottom:8px; color:var(--text-secondary);">Parameter Coefficients (θ Vector)</h4>
        <div style="display:flex; gap:16px; font-family:var(--font-mono); font-size:13px;">
          <div>h_cool: <b>${fmt(p.values?.h_cool, 3)}</b> (thermal cooling)</div>
          <div>k_heat: <b>${fmt(p.values?.k_heat, 3)}</b> (frictional heat)</div>
          <div>k_wear: <b>${fmt(p.values?.k_wear, 3)}</b> (tool wear rate)</div>
        </div>
      </div>

      <div style="background:var(--bg-card); border-radius:var(--radius-sm); padding:12px; margin-top:10px;">
        <h4 style="font-size:13px; margin-bottom:8px; color:var(--text-secondary);">Spindle Dynamics & Mechanical State</h4>
        <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap:10px; font-size:12px;">
          <div>Spindle Speed: <b>${fmt(o.rpm, 0)} RPM</b></div>
          <div>Cutting Torque: <b>${fmt(o.torque_nm, 1)} Nm</b></div>
          <div>Motor Power: <b>${fmt(o.power_kw, 2)} kW</b></div>
          <div>Vibration RMS: <b>${fmt(o.vibration_rms_mm_s, 2)} mm/s</b> (${isoZone(mid)})</div>
          <div>Tool Wear: <b>${fmt(o.tool_wear_min, 1)} min</b></div>
        </div>
      </div>
    `;
  } else if (m.template === "air_compressor") {
    specificHtml = `
      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px;">
        <div class="calc-card">
          <div class="calc-label">Header Line Pressure</div>
          <div class="calc-val" style="color:${(o.pressure_bar || 7.0) < 6.0 ? 'var(--status-crit)' : 'var(--status-ok)'}">${fmt(o.pressure_bar, 2)} / 7.00 bar</div>
          <div class="formula-hint">Pneumatic reservoir mass balance</div>
        </div>
        <div class="calc-card">
          <div class="calc-label">Pressure Residual e (Obs - 7.0)</div>
          <div class="calc-val ${r.state}">${fmt(r.value, 2)} bar</div>
          <div class="formula-hint">EWMA: ${fmt(r.ewma, 2)} · Threshold: &lt;6.0 bar critical</div>
        </div>
        <div class="calc-card">
          <div class="calc-label">Factory Air Supply State</div>
          <div class="calc-val">${(o.pressure_bar || 7.0) >= 6.0 ? '<span style="color:var(--status-ok)">OPTIMAL (≥ 6.0 bar)</span>' : '<span style="color:var(--status-crit)">UNDERPRESSURE (&lt; 6.0 bar)</span>'}</div>
          <div class="formula-hint">Feeds pneumatic clamps on VMC-01 & LATHE-01</div>
        </div>
        <div class="calc-card">
          <div class="calc-label">Compressor Duty & Thermal</div>
          <div class="calc-val">${fmt(o.duty_pct, 1)}% Duty</div>
          <div class="formula-hint">Discharge: ${fmt(o.discharge_temp_c, 1)} °C · Vib: ${fmt(o.vibration_rms_mm_s, 2)} mm/s</div>
        </div>
      </div>

      <div style="background:var(--bg-card); border-radius:var(--radius-sm); padding:12px; margin-top:10px;">
        <h4 style="font-size:13px; margin-bottom:6px; color:var(--text-secondary); font-weight:700;">Shared Utility Infrastructure Analysis (Common Cause)</h4>
        <div style="font-size:12px; color:var(--text-secondary); line-height:1.5;">
          This compressor acts as a single-point shared utility node across the floor. When line pressure falls below <b>6.0 bar</b>, pneumatic tool changers and workholding chucks on <b>VMC-01</b> and <b>LATHE-01</b> cannot safely clamp, triggering automatic derating and line bottlenecking.
        </div>
      </div>
    `;
  } else if (m.template === "bar_saw") {
    specificHtml = `
      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px;">
        <div class="calc-card">
          <div class="calc-label">Motor Cutting Power</div>
          <div class="calc-val">${fmt(o.power_kw, 2)} / 3.20 kW</div>
          <div class="formula-hint">Specific cutting energy model: P = k_cut × feed × depth</div>
        </div>
        <div class="calc-card">
          <div class="calc-label">Power Residual e (Obs - 3.20)</div>
          <div class="calc-val ${r.state}">${fmt(r.value, 2)} kW</div>
          <div class="formula-hint">EWMA: ${fmt(r.ewma, 2)} · Band: ±0.5 kW</div>
        </div>
        <div class="calc-card">
          <div class="calc-label">Band Blade Health & Wear</div>
          <div class="calc-val" style="color:${(o.blade_wear_pct || 0) > 75 ? 'var(--status-crit)' : 'var(--status-ok)'}">${fmt(o.blade_wear_pct, 1)}% Wear</div>
          <div class="formula-hint">Blade speed: ${fmt(o.rpm, 0)} RPM · Est: ~${Math.max(0, (100 - (o.blade_wear_pct || 0)) * 4).toFixed(0)} cuts left</div>
        </div>
        <div class="calc-card">
          <div class="calc-label">Vibration & Drive Health</div>
          <div class="calc-val">${fmt(o.vibration_rms_mm_s, 2)} mm/s</div>
          <div class="formula-hint">${isoZone(mid)}</div>
        </div>
      </div>

      <div style="background:var(--bg-card); border-radius:var(--radius-sm); padding:12px; margin-top:10px;">
        <h4 style="font-size:13px; margin-bottom:6px; color:var(--text-secondary); font-weight:700;">Upstream Production Line Feed (Station 1)</h4>
        <div style="font-size:12px; color:var(--text-secondary); line-height:1.5;">
          SAW-01 cuts raw cylindrical metal bar stock and feeds the downstream inter-stage buffer for <b>LATHE-01</b>. If SAW-01 experiences accelerated blade wear or stalls, the downstream buffer starves, reducing overall factory line throughput.
        </div>
      </div>
    `;
  }

  metricsContainer.innerHTML = `
    <div class="panel-header">
      <h2>
        <span>${esc(m.name)}</span>
        <span style="font-size:13px; color:var(--text-muted); font-weight:normal;">[${esc(mid)}] · ${esc(m.template)}</span>
      </h2>
      <div style="display:flex; gap:8px;">
        ${sourceBadge(state.obs[mid] || {})}
        ${m.template.startsWith("cnc") ? `<button class="btn btn-sm" onclick="openToolWearModal('${esc(mid)}')">Enter Tool Wear</button>` : ''}
      </div>
    </div>

    ${specificHtml}

    ${f.warning ? `
      <div style="background:rgba(239, 68, 68, 0.1); border:1px solid rgba(239, 68, 68, 0.3); border-radius:var(--radius-sm); padding:12px; margin-top:10px; display:flex; justify-content:space-between; align-items:center;">
        <div>
          <b style="color:var(--status-crit);">Impending Warning: ${esc(f.warning)}</b>
          <div style="font-size:12px; color:var(--text-secondary);">Forecast horizon: ~${fmt(f.minutes_to_warning, 1)} minutes at current load setpoint.</div>
        </div>
        <button class="btn btn-sm btn-primary" onclick="createWorkOrderFromAlert('${esc(mid)}', '${esc(f.warning)}')">Create Work Order</button>
      </div>
    ` : ''}
  `;

  // Update 3D Container title and auto-init
  if (threeContainer) {
    threeContainer.style.display = "block";
    const titleEl = document.getElementById("threeTitle");
    if (titleEl) titleEl.textContent = `3D Kinematic Twin — ${m.name} [${mid}]`;

    if (!document.getElementById("threeCanvas") && window.initThreeView) {
      window.initThreeView(mid);
    }
  }
}

function renderLineBanner(line) {
  const flowEl = document.getElementById("lineStationsFlow");
  const tpEl = document.getElementById("lineThroughput");
  const lossEl = document.getElementById("lineLoss");
  const bnEl = document.getElementById("lineBottleneck");
  const airEl = document.getElementById("lineAirSupply");

  if (flowEl && line.stations) {
    flowEl.innerHTML = line.stations.map((s, idx) => `
      <div class="station-node">
        <span>${esc(s.machine_id)}</span>
        <span class="buffer-pill">buf ${s.buffer_in}/${s.buffer_cap}</span>
        <span class="pill ${esc(s.state)}" style="font-size:10px;">${esc(s.state)}</span>
      </div>
      ${idx < line.stations.length - 1 ? '<span class="flow-arrow">→</span>' : ''}
    `).join("");
  }

  if (tpEl) tpEl.textContent = `${fmt(line.throughput_parts_per_hr, 1)} /h`;
  if (lossEl) lossEl.textContent = `₹${fmt(line.line_loss_inr_per_hr, 0)}/h`;
  if (bnEl) bnEl.textContent = line.bottleneck || "None";
  if (airEl) {
    airEl.textContent = line.air_supply_ok ? "OPTIMAL (7.0 bar)" : "LOW PRESSURE (< 6.0 bar)";
    airEl.style.color = line.air_supply_ok ? "var(--status-ok)" : "var(--status-crit)";
  }
}

function renderEconomics() {
  const econ = state.economics || {};
  const inrPerPart = econ.inr_per_part || 450.0;
  const downtimeCostPerHr = econ.downtime_cost_per_hr || 2500.0;
  const kwhCost = econ.electricity_tariff_per_kwh || 8.5;
  const nodeBOM = econ.sensor_node_cost_inr || 2200.0;
  const ceaFactor = econ.cea_co2_factor || 0.716;
  const baseDowntimeHours = econ.base_breakdown_hours || 12.0;

  // Live line loss
  const lineLoss = state.line?.line_loss_inr_per_hr || 0;

  // Active floor total power
  let totalKw = 0;
  Object.values(state.obs).forEach(o => totalKw += (o.power_kw || 0));
  const hourlyKwhCost = (totalKw * kwhCost).toFixed(1);
  const hourlyCo2 = (totalKw * ceaFactor).toFixed(2);

  // Avoided monthly breakdown losses
  const avoidedHoursPerMonth = baseDowntimeHours * 0.70; // 70% avoided with digital twin
  const monthlyAvoidedLossInr = avoidedHoursPerMonth * downtimeCostPerHr;
  const totalSensorNodes = Object.keys(state.machines).length || 4;
  const totalHardwareCapEx = totalSensorNodes * nodeBOM;
  const paybackMonths = monthlyAvoidedLossInr > 0 ? (totalHardwareCapEx / monthlyAvoidedLossInr).toFixed(2) : "N/A";

  // Tool wear scrap savings
  const monthlyScrapSavings = 6500.0;
  // Energy efficiency savings (~5% waste avoided via timely blade/compressor/coolant maintenance)
  const monthlyHours = 400.0;
  const monthlyEnergyKwh = totalKw * monthlyHours;
  const monthlyEnergySavingsKwh = monthlyEnergyKwh * 0.05;
  const monthlyEnergySavingsInr = monthlyEnergySavingsKwh * kwhCost;
  const monthlyCo2AvoidedKg = monthlyEnergySavingsKwh * ceaFactor;

  const totalMonthlyBenefit = monthlyAvoidedLossInr + monthlyScrapSavings + monthlyEnergySavingsInr;
  const annualNetRoi = totalHardwareCapEx > 0 ? (((totalMonthlyBenefit * 12) - totalHardwareCapEx) / totalHardwareCapEx * 100).toFixed(0) : "0";

  // Update primary KPI cards
  const elLineLoss = document.getElementById("calcLineLoss");
  const elPayback = document.getElementById("calcPayback");
  const elEnergy = document.getElementById("calcEnergy");
  const elCo2 = document.getElementById("calcCo2");

  if (elLineLoss) elLineLoss.textContent = `₹${fmt(lineLoss, 0)} /hr`;
  if (elPayback) elPayback.textContent = `${paybackMonths} Months`;
  if (elEnergy) elEnergy.textContent = `₹${hourlyKwhCost} /hr (${totalKw.toFixed(1)} kW)`;
  if (elCo2) elCo2.textContent = `${hourlyCo2} kg/hr`;

  // Update hints
  const hInr = document.getElementById("econInrPerPartHint");
  const hNode = document.getElementById("econNodeCostHint");
  const hTariff = document.getElementById("econTariffHint");
  const hCea = document.getElementById("econCeaHint");
  if (hInr) hInr.textContent = inrPerPart;
  if (hNode) hNode.textContent = nodeBOM;
  if (hTariff) hTariff.textContent = kwhCost;
  if (hCea) hCea.textContent = ceaFactor;

  // Update Scenario Comparison Table
  const sBaseHours = document.getElementById("scenBaseHours");
  const sTwinHours = document.getElementById("scenTwinHours");
  const sDownSave = document.getElementById("scenDowntimeSave");
  const sEnergySave = document.getElementById("scenEnergySave");
  const sNetMonthly = document.getElementById("scenNetMonthly");
  const sRoi = document.getElementById("scenRoiAnnual");

  if (sBaseHours) sBaseHours.textContent = `${baseDowntimeHours.toFixed(1)} hrs`;
  if (sTwinHours) sTwinHours.textContent = `${(baseDowntimeHours * 0.3).toFixed(1)} hrs (-70%)`;
  if (sDownSave) sDownSave.textContent = `₹${monthlyAvoidedLossInr.toLocaleString('en-IN', {maximumFractionDigits:0})} /mo saved`;
  if (sEnergySave) sEnergySave.textContent = `₹${monthlyEnergySavingsInr.toFixed(0)} /mo · ${monthlyCo2AvoidedKg.toFixed(0)} kg CO₂`;
  if (sNetMonthly) sNetMonthly.textContent = `₹${totalMonthlyBenefit.toLocaleString('en-IN', {maximumFractionDigits:0})} /mo`;
  if (sRoi) sRoi.textContent = `1-Yr Net ROI: ${annualNetRoi}%`;

  // Update Sensitivity Matrix
  const s1500 = document.getElementById("sensPay1500");
  const s2500 = document.getElementById("sensPay2500");
  const s3500 = document.getElementById("sensPay3500");
  if (s1500) s1500.textContent = `${(totalHardwareCapEx / (avoidedHoursPerMonth * 1500)).toFixed(1)} mo`;
  if (s2500) s2500.textContent = `${(totalHardwareCapEx / (avoidedHoursPerMonth * 2500)).toFixed(1)} mo`;
  if (s3500) s3500.textContent = `${(totalHardwareCapEx / (avoidedHoursPerMonth * 3500)).toFixed(1)} mo`;
}

const EVENT_CAP = 200;
const SEV_LABEL = { INFO: "Info", WARN: "Warning", CRITICAL: "Critical" };

function machineName(mid) {
  return (state.machines[mid] && state.machines[mid].name) || mid || "Factory";
}

function shortTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d) ? String(ts).slice(11, 19) : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function logEvent(e) {
  const id = e.event_id || `${e.ts}-${e.kind}-${e.title}`;
  if (state.events.some(x => x._id === id)) return;
  state.events.unshift({ ...e, _id: id, severity: e.severity || "INFO" });
  if (state.events.length > EVENT_CAP) state.events.length = EVENT_CAP;
  updateKindFilter(e.kind);
  renderEventLog();
}

function updateKindFilter(kind) {
  const sel = document.getElementById("eventKindFilter");
  if (!sel || !kind || [...sel.options].some(o => o.value === kind)) return;
  sel.appendChild(new Option(kind.replace(/_/g, " ").toLowerCase(), kind));
}

function renderEventLog() {
  const container = document.getElementById("eventLog");
  if (!container) return;
  const { sev, kind } = state.eventFilter;
  const rows = state.events.filter(e => (sev === "ALL" || e.severity === sev) && (kind === "ALL" || e.kind === kind));
  if (!rows.length) {
    container.innerHTML = `<div class="list-empty">${state.events.length ? "No events match the filter." : "No events yet."}</div>`;
    return;
  }
  container.innerHTML = rows.map(e => {
    const open = state.expandedEvents.has(e._id);
    const detail = e.measured || e.inferred || e.suggested;
    const summary = e.inferred || e.measured || "";
    const actionable = e.severity === "WARN" || e.severity === "CRITICAL";
    return `<div class="ev-row ${esc(e.severity)}${open ? " open" : ""}" data-id="${esc(e._id)}">
      <button type="button" class="ev-line" aria-expanded="${open}" ${detail || actionable ? "" : "disabled"}>
        <span class="sev-dot" title="${esc(SEV_LABEL[e.severity] || e.severity)}"></span>
        <span class="ev-time">${esc(shortTime(e.ts))}</span>
        <span class="ev-text"><b>${esc(e.title)}</b>${summary && !open ? `<span class="ev-sum"> — ${esc(summary)}</span>` : ""}</span>
        <span class="ev-tag">${esc(e.machine_id || "FACTORY")} · ${esc((e.kind || "").replace(/_/g, " ").toLowerCase())}</span>
      </button>
      ${open ? `<div class="ev-detail">
        ${e.measured ? `<div><b>Measured:</b> ${esc(e.measured)}</div>` : ""}
        ${e.inferred ? `<div><b>Inferred:</b> ${esc(e.inferred)}</div>` : ""}
        ${e.suggested ? `<div><b>Suggested:</b> ${esc(e.suggested)}</div>` : ""}
        ${actionable ? `<button type="button" class="btn btn-sm btn-primary" data-wo-mid="${esc(e.machine_id || "FACTORY")}" data-wo-title="${esc(e.title)}">Create work order</button>` : ""}
      </div>` : ""}
    </div>`;
  }).join("");
}

function initEventLog() {
  const log = document.getElementById("eventLog");
  const filters = document.getElementById("eventFilters");
  if (!log || !filters) return;
  log.addEventListener("click", ev => {
    const wo = ev.target.closest("[data-wo-mid]");
    if (wo) { createWorkOrderFromAlert(wo.dataset.woMid, wo.dataset.woTitle); wo.disabled = true; wo.textContent = "Work order requested"; return; }
    const line = ev.target.closest(".ev-line");
    if (!line || line.disabled) return;
    const id = line.parentElement.dataset.id;
    state.expandedEvents.has(id) ? state.expandedEvents.delete(id) : state.expandedEvents.add(id);
    renderEventLog();
  });
  filters.querySelectorAll(".sev-chip").forEach(b => b.onclick = () => {
    state.eventFilter.sev = b.dataset.sev;
    filters.querySelectorAll(".sev-chip").forEach(x => x.classList.toggle("active", x === b));
    renderEventLog();
  });
  document.getElementById("eventKindFilter").onchange = e => { state.eventFilter.kind = e.target.value; renderEventLog(); };
  // Recent history from SQLite so the log is not empty after a reload (oldest first, so newest ends on top)
  fetch("/api/events?limit=100").then(r => r.json()).then(rows => rows.reverse().forEach(logEvent)).catch(() => {});
}

// Active alerts from the server alert store (raised, acknowledged and resolved server-side)
let alertsTimer = null;
function scheduleAlertsFetch() {
  clearTimeout(alertsTimer);
  alertsTimer = setTimeout(fetchAlerts, 250);  // several alert frames often arrive in one batch
}

function fetchAlerts() {
  fetch("/api/alerts").then(r => r.json()).then(data => { state.alerts = data; renderAlerts(); }).catch(() => {});
}

const SEV_RANK = { CRITICAL: 0, WARN: 1, INFO: 2 };
function renderAlerts() {
  const list = document.getElementById("alertList");
  const count = document.getElementById("alertCount");
  if (!list) return;
  const alerts = [...state.alerts].sort((a, b) => (SEV_RANK[a.severity] ?? 3) - (SEV_RANK[b.severity] ?? 3)
    || (a.status === "ACTIVE" ? -1 : 1) - (b.status === "ACTIVE" ? -1 : 1) || String(b.raised_at).localeCompare(String(a.raised_at)));
  const active = alerts.filter(a => a.status === "ACTIVE").length;
  if (count) { count.textContent = `${active} active`; count.classList.toggle("zero", active === 0); }
  list.classList.toggle("empty", !alerts.length);
  if (!alerts.length) { list.innerHTML = `<div class="list-empty ok">✓ No open alerts</div>`; return; }
  list.innerHTML = alerts.map(a => `
    <div class="alert-card ${esc(a.severity)}${a.status === "ACKNOWLEDGED" ? " acked" : ""}" title="${esc([a.measured, a.inferred, a.suggested].filter(Boolean).join("\n"))}">
      <div class="alert-main">
        <div class="alert-head"><span class="sev-pill ${esc(a.severity)}">${esc(SEV_LABEL[a.severity] || a.severity)}</span>
          <b>${esc(machineName(a.machine_id))}</b><span class="alert-time">${esc(shortTime(a.raised_at))}${a.occurrences > 1 ? ` · ×${esc(a.occurrences)}` : ""}</span></div>
        <div class="alert-msg">${esc(a.title)}</div>
      </div>
      <div class="alert-actions">
        ${a.status === "ACTIVE" ? `<button type="button" class="btn btn-sm" data-alert-ack="${esc(a.alert_id)}">Ack</button>` : `<span class="alert-state">Ack'd</span>`}
        ${a.wo_id ? `<span class="alert-state">${esc(a.wo_id)}</span>` : `<button type="button" class="btn btn-sm btn-primary" data-alert-wo="${esc(a.alert_id)}">Work order</button>`}
      </div>
    </div>`).join("");
}

function initAlerts() {
  const list = document.getElementById("alertList");
  if (!list) return;
  list.addEventListener("click", ev => {
    const ack = ev.target.closest("[data-alert-ack]"), wo = ev.target.closest("[data-alert-wo]");
    const btn = ack || wo;
    if (!btn) return;
    btn.disabled = true;
    const id = encodeURIComponent(ack ? ack.dataset.alertAck : wo.dataset.alertWo);
    fetch(ack ? `/api/alerts/${id}/acknowledge` : `/api/alerts/${id}/work-order`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: ack ? JSON.stringify({ by: "dashboard operator" }) : undefined,
    }).then(() => { fetchAlerts(); if (wo) fetchWorkOrders(); }).catch(() => { btn.disabled = false; });
  });
  fetchAlerts();
}

// -----------------------------------------------------------------------------
// Interactive Commands & API Integrations
// -----------------------------------------------------------------------------
function sendCommand(cmd) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(cmd));
  } else {
    // Fallback: send via REST when WebSocket is unavailable
    if (cmd.cmd === "inject_fault") {
      fetch("/api/faults/inject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ machine_id: cmd.machine_id, fault: cmd.fault }),
      }).catch(console.error);
    } else if (cmd.cmd === "clear_faults") {
      fetch("/api/faults/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ machine_id: cmd.machine_id }),
      }).catch(console.error);
    } else if (cmd.cmd === "reset") {
      fetch("/api/reset", { method: "POST" }).catch(console.error);
    } else if (cmd.cmd === "set_derate") {
      fetch("/api/derate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ machine_id: cmd.machine_id, pct: cmd.pct }),
      }).catch(console.error);
    } else if (cmd.cmd === "auto_demo") {
      fetch(`/api/auto_demo?on=${cmd.on}`, { method: "POST" }).catch(console.error);
    }
  }
}

function injectSelectedFault() {
  const mSel = document.getElementById("faultMachineSelect");
  const fSel = document.getElementById("faultTypeSelect");
  if (!mSel || !fSel) return;
  sendCommand({
    cmd: "inject_fault",
    machine_id: mSel.value,
    fault: fSel.value,
  });
}

function clearAllFaults() {
  sendCommand({ cmd: "clear_faults" });
}

function toggleAutoDemo() {
  state.autoDemo = !state.autoDemo;
  const btn = document.getElementById("btnAutoDemo");
  if (btn) btn.textContent = `Auto Demo: ${state.autoDemo ? 'ON' : 'OFF'}`;
  sendCommand({ cmd: "auto_demo", on: state.autoDemo });
}

function resetScenario() {
  sendCommand({ cmd: "reset" });
}

function applyDerate(pct) {
  const mid = state.selectedMachineId;
  sendCommand({ cmd: "set_derate", machine_id: mid, pct: pct });
}

// Onboarding Modal
function openOnboardingModal() {
  document.getElementById("onboardingModal").classList.add("open");
}

function closeOnboardingModal() {
  document.getElementById("onboardingModal").classList.remove("open");
}

function submitOnboardingForm(e) {
  e.preventDefault();
  const template = document.getElementById("onboardTemplate").value;
  const name = document.getElementById("onboardName").value;
  const rated_kw = parseFloat(document.getElementById("onboardKw").value) || 7.5;
  const max_rpm = parseFloat(document.getElementById("onboardRpm").value) || 3000;

  sendCommand({
    cmd: "add_machine",
    template: template,
    name: name,
    nameplate: { rated_kw: rated_kw, max_rpm: max_rpm },
  });

  closeOnboardingModal();
}

// Tool Wear Measurement Feedback Modal
function openToolWearModal(machineId) {
  const midInput = document.getElementById("toolWearMachineId");
  if (midInput) midInput.value = machineId;
  document.getElementById("toolWearModal").classList.add("open");
}

function closeToolWearModal() {
  document.getElementById("toolWearModal").classList.remove("open");
}

function submitToolWearForm(e) {
  e.preventDefault();
  const mid = document.getElementById("toolWearMachineId").value;
  const wear = parseFloat(document.getElementById("toolWearValue").value);

  fetch("/api/tool-wear", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ machine_id: mid, measured_wear_min: wear }),
  }).then(r => r.json()).then(data => {
    closeToolWearModal();
  });
}

// Work Orders
function fetchWorkOrders() {
  fetch("/api/work-orders")
    .then(r => r.json())
    .then(data => {
      state.workOrders = data;
      renderWorkOrders();
    })
    .catch(() => {});
}

const WO_RANK = { Critical: 0, High: 1, Medium: 2, Low: 3 };
const WO_OPEN = wo => wo.status !== "Closed";

function renderWorkOrders() {
  const list = document.getElementById("workOrderList");
  const count = document.getElementById("woCount");
  if (!list) return;
  const open = state.workOrders.filter(WO_OPEN).length;
  if (count) { count.textContent = `${open} open`; count.title = `${state.workOrders.length} in total`; }

  if (state.workOrders.length === 0) {
    list.innerHTML = "<div class='list-empty'>No work orders.</div>";
    return;
  }
  // Open orders first (by priority), then closed ones, newest first within each group
  const rows = [...state.workOrders].sort((a, b) => (WO_OPEN(b) - WO_OPEN(a)) || (WO_RANK[a.priority] ?? 9) - (WO_RANK[b.priority] ?? 9)
    || String(b.created_at).localeCompare(String(a.created_at)));
  list.innerHTML = rows.map(wo => `
    <div class="wo-row${WO_OPEN(wo) ? "" : " closed"}" title="${esc(wo.cause || wo.title)}">
      <span class="wo-id">${esc(wo.wo_id)}</span>
      <span class="wo-main"><b>${esc(wo.machine_id)}</b> <span class="wo-title">${esc(wo.title)}</span></span>
      <span class="prio-pill ${esc(wo.priority)}">${esc(wo.priority)}</span>
      <span class="status-pill ${esc((wo.status || "").replace(/\s+/g, "-"))}">${esc(wo.status)}</span>
      <span class="wo-act">
        ${wo.status === 'Open' ? `<button class="btn btn-sm btn-primary" onclick="updateWo('${esc(wo.wo_id)}', 'In Progress')">Acknowledge</button>` : ''}
        ${wo.status === 'In Progress' ? `<button class="btn btn-sm btn-primary" onclick="updateWo('${esc(wo.wo_id)}', 'Closed')">Close</button>` : ''}
      </span>
    </div>
  `).join("");
}

function createWorkOrderFromAlert(machineId, warning) {
  fetch("/api/work-orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      machine_id: machineId,
      title: `Corrective maintenance: ${warning}`,
      priority: "High",
      notes: "Auto-generated from digital twin forecast alert",
    }),
  }).then(() => fetchWorkOrders());
}

function updateWo(woId, status) {
  fetch(`/api/work-orders/${woId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: status }),
  }).then(() => fetchWorkOrders());
}

function fetchRegistry() {
  fetch("/api/registry")
    .then(r => r.json())
    .then(data => {
      if (data.machines) {
        state.machines = Object.fromEntries(data.machines.map(m => [m.machine_id, m]));
        updateMachineDropdowns();
        renderAll();
      }
    })
    .catch(() => {});
}

// -----------------------------------------------------------------------------
// Interactive MSME Economics Modal & Calculations
// -----------------------------------------------------------------------------

function openEconomicsModal() {
  const econ = state.economics || {};
  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (el && val !== undefined) el.value = val;
  };
  setVal("inputDowntimeCost", econ.downtime_cost_per_hr || 2500);
  setVal("inputPartMargin", econ.inr_per_part || 450);
  setVal("inputElectricityTariff", econ.electricity_tariff_per_kwh || 8.5);
  setVal("inputSensorCost", econ.sensor_node_cost_inr || 2200);
  setVal("inputBaseHours", econ.base_breakdown_hours || 12);
  setVal("inputCeaFactor", econ.cea_co2_factor || 0.716);

  document.getElementById("economicsModal").classList.add("open");
}

function closeEconomicsModal() {
  document.getElementById("economicsModal").classList.remove("open");
}

function submitEconomicsForm(e) {
  e.preventDefault();
  const updates = {
    downtime_cost_per_hr: parseFloat(document.getElementById("inputDowntimeCost").value) || 2500,
    inr_per_part: parseFloat(document.getElementById("inputPartMargin").value) || 450,
    electricity_tariff_per_kwh: parseFloat(document.getElementById("inputElectricityTariff").value) || 8.5,
    sensor_node_cost_inr: parseFloat(document.getElementById("inputSensorCost").value) || 2200,
    base_breakdown_hours: parseFloat(document.getElementById("inputBaseHours").value) || 12,
    cea_co2_factor: parseFloat(document.getElementById("inputCeaFactor").value) || 0.716,
  };

  Object.assign(state.economics, updates);
  closeEconomicsModal();
  renderEconomics();

  // Persist to SQLite backend
  fetch("/api/economics", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  }).catch(() => {});
}

function fetchEconomics() {
  fetch("/api/economics")
    .then(r => r.json())
    .then(data => {
      if (data.assumptions) {
        Object.assign(state.economics, data.assumptions);
        renderEconomics();
      }
    })
    .catch(() => {});
}

// -----------------------------------------------------------------------------
// Competition Demo Scenarios
// -----------------------------------------------------------------------------

function demoScenario(name) {
  if (name === "coolant") {
    selectMachine("VMC-01");
    sendCommand({ cmd: "inject_fault", machine_id: "VMC-01", fault: "coolant_degradation" });
  } else if (name === "bearing") {
    selectMachine("LATHE-01");
    sendCommand({ cmd: "inject_fault", machine_id: "LATHE-01", fault: "bearing_defect" });
  } else if (name === "compressor") {
    selectMachine("COMP-01");
    sendCommand({ cmd: "inject_fault", machine_id: "COMP-01", fault: "pressure_drop" });
  } else if (name === "tool_wear") {
    selectMachine("VMC-01");
    openToolWearModal("VMC-01");
  }
}

// -----------------------------------------------------------------------------
// AI4I Research Intelligence Metrics
// -----------------------------------------------------------------------------

function fetchMetrics() {
  fetch("/api/metrics")
    .then(r => r.json())
    .then(data => {
      state.metrics = data;
      if (data.random_forest) {
        const rf = data.random_forest;
        const rule = data.rule_baseline || {};
        const elRf = document.getElementById("mlRfF1");
        const elRule = document.getElementById("mlRuleF1");
        const elRoc = document.getElementById("mlRocAuc");
        const elPrec = document.getElementById("mlPrec");
        const elFpr = document.getElementById("mlFpr");

        if (elRf) elRf.textContent = `${(rf.f1 * 100).toFixed(1)}%`;
        if (elRule) elRule.textContent = `${(rule.f1 * 100).toFixed(1)}%`;
        if (elRoc) elRoc.textContent = `${rf.roc_auc.toFixed(3)}`;
        if (elPrec) elPrec.textContent = `${(rf.precision * 100).toFixed(1)}%`;
        if (elFpr) elFpr.textContent = `${(rf.false_positive_rate * 100).toFixed(2)}%`;
      }
    })
    .catch(() => {});
}

// Expose interactive functions globally for HTML inline handlers
window.toggleAutoDemo = toggleAutoDemo;
window.resetScenario = resetScenario;
window.openOnboardingModal = openOnboardingModal;
window.closeOnboardingModal = closeOnboardingModal;
window.submitOnboardingForm = submitOnboardingForm;
window.openToolWearModal = openToolWearModal;
window.closeToolWearModal = closeToolWearModal;
window.submitToolWearForm = submitToolWearForm;
window.openEconomicsModal = openEconomicsModal;
window.closeEconomicsModal = closeEconomicsModal;
window.submitEconomicsForm = submitEconomicsForm;
window.injectSelectedFault = injectSelectedFault;
window.clearAllFaults = clearAllFaults;
window.applyDerate = applyDerate;
window.demoScenario = demoScenario;
window.createWorkOrderFromAlert = createWorkOrderFromAlert;
window.updateWo = updateWo;
// -----------------------------------------------------------------------------
// Vibration Health panel (GET /api/vibration/{id}); every value carries its provenance
// -----------------------------------------------------------------------------

const VIB_PROVENANCE_LABEL = {
  measured: "Measured vibration",
  simulated: "Simulated vibration",
  replayed: "Replayed vibration",
  recorded: "Recorded vibration",
};
const SEVERITY_LABEL = {
  ASSESSED: "Rule-based severity",
  PROVISIONAL: "Provisional (thresholds not verified)",
  UNAVAILABLE: "Assessment unavailable",
  NOT_APPLICABLE: "Not applicable",
  NO_DATA: "No data",
};
const ML_LABEL = {
  TRAINING_NOT_CONFIGURED: "Training not configured",
  SYNTHETIC_MODEL: "Synthetic test model (not published)",
  INSUFFICIENT_FEATURES: "Insufficient features",
  OUT_OF_DOMAIN: "Out of model domain",
  OK: "ML prediction",
  OK_UNVALIDATED: "ML prediction (unvalidated)",
};

function fetchVibration() {
  const mid = state.selectedMachineId;
  if (!mid) return;
  fetch(`/api/vibration/${encodeURIComponent(mid)}`)
    .then(r => (r.ok ? r.json() : Promise.reject(r.status)))
    .then(v => { if (v.machine_id === state.selectedMachineId) renderVibrationPanel(v); })
    .catch(() => {
      const panel = document.getElementById("vibrationPanel");
      if (panel) panel.innerHTML = `<div class="text-muted">Vibration health unavailable for ${esc(mid)}.</div>`;
    });
}

function provenancePill(prov) {
  if (!prov) return `<span class="pill vib-prov-none">No data</span>`;
  return `<span class="pill vib-prov-${esc(prov)}">${esc(VIB_PROVENANCE_LABEL[prov] || prov)}</span>`;
}

function vibrationSparkline(points) {
  const vals = points.map(p => p[1]).filter(v => typeof v === "number");
  if (vals.length < 2) return `<div class="text-muted" style="font-size:12px;">Not enough samples for a trend yet.</div>`;
  const w = 320, h = 70, pad = 4;
  const max = Math.max(...vals) * 1.1 || 1;
  const step = (w - 2 * pad) / Math.max(1, points.length - 1);
  // Break the line at missing values instead of interpolating across them.
  let d = "", pen = false;
  points.forEach((p, i) => {
    if (typeof p[1] !== "number") { pen = false; return; }
    const x = pad + i * step, y = h - pad - (p[1] / max) * (h - 2 * pad);
    d += `${pen ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)} `;
    pen = true;
  });
  const modes = [...new Set(points.map(p => p[2]).filter(Boolean))].join(" + ");
  return `
    <svg class="vib-trend" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img"
         aria-label="Vibration velocity trend, max ${fmt(max / 1.1, 2)} mm/s">
      <path d="${d}" fill="none" stroke="var(--accent-cyan)" stroke-width="1.5" vector-effect="non-scaling-stroke"/>
    </svg>
    <div class="vib-trend-caption">
      <span>last ${points.length} ticks · source ${esc(modes || "—")}</span>
      <span>peak ${fmt(Math.max(...vals), 2)} mm/s</span>
    </div>`;
}

function renderVibrationPanel(v) {
  const panel = document.getElementById("vibrationPanel");
  if (!panel) return;
  const sv = v.stream_velocity || {};
  const sev = v.severity || {};
  const ml = v.ml || {};
  const op = v.operating || {};
  const wf = v.waveform;
  const dq = v.data_quality || { warnings: [] };

  const wfFeats = wf ? wf.features || {} : {};
  const wfRows = wf ? [
    ["Velocity RMS (band)", wfFeats.velocity_rms_mm_s, "mm/s", 3],
    ["Acceleration RMS", wfFeats.accel_rms_m_s2, "m/s²", 3],
    ["Peak acceleration", wfFeats.accel_peak_m_s2, "m/s²", 3],
    ["Crest factor", wfFeats.crest_factor, "", 2],
    ["Kurtosis", wfFeats.kurtosis, "", 2],
    ["Dominant frequency", wfFeats.dominant_freq_hz, "Hz", 1],
    ["Dominant order (×shaft)", wfFeats.dominant_order, "", 2],
    ["Displacement p-p", wfFeats.disp_pp_um, "µm", 1],
  ] : [];

  panel.innerHTML = `
    <div class="panel-header">
      <h2>Vibration Health <span style="font-size:13px; color:var(--text-muted); font-weight:normal;">[${esc(v.machine_id)}] · ${esc(v.template || "")}</span></h2>
    </div>

    <div class="vib-grid">
      <div class="vib-box">
        <div class="vib-box-title">Velocity RMS (stream)</div>
        <div class="vib-value">${fmt(sv.value, 2)} <small>mm/s</small></div>
        <div class="vib-meta">${provenancePill(sv.provenance)}</div>
        <div class="vib-meta">${esc(sv.method || "")}${sv.band_hz ? "" : " · band not declared"}</div>
        <div class="vib-meta">stream ts ${esc(sv.stream_ts || "—")}</div>
      </div>

      <div class="vib-box">
        <div class="vib-box-title">Rule-based severity</div>
        <div class="vib-value vib-status-${esc(sev.status || "UNAVAILABLE")}">${esc(SEVERITY_LABEL[sev.status] || sev.status || "—")}${sev.zone ? ` · Zone ${esc(sev.zone)}` : ""}</div>
        <div class="vib-meta">${esc(sev.reason || "")}</div>
        ${sev.basis ? `<div class="vib-meta">basis: ${esc(sev.basis)}</div>` : ""}
      </div>

      <div class="vib-box">
        <div class="vib-box-title">ML prediction</div>
        <div class="vib-value vib-ml-${esc(ml.status || "TRAINING_NOT_CONFIGURED")}">${ml.label ? esc(ml.label) : esc(ML_LABEL[ml.status] || ml.status || "—")}</div>
        ${ml.label ? `<div class="vib-meta"><span class="pill ${ml.status === "OK" ? "OK" : "WARN"}">${esc(ML_LABEL[ml.status] || ml.status)}</span></div>` : ""}
        ${ml.label && ml.class_vote_share ? `<div class="vib-meta">vote share ${esc(fmt((ml.class_vote_share[ml.label] || 0) * 100, 0))}% (${ml.calibrated ? "calibrated" : "uncalibrated"}) · from ${esc(VIB_PROVENANCE_LABEL[ml.basis_provenance] || "")}</div>` : ""}
        <div class="vib-meta">${esc(ml.detail || "")}</div>
      </div>

      <div class="vib-box">
        <div class="vib-box-title">Operating conditions</div>
        <table class="telemetry-table vib-table">
          <tr><td>State</td><td>${esc(op.plc_state || "—")}</td></tr>
          <tr><td>Speed</td><td>${fmt(op.rpm, 0)} rpm</td></tr>
          <tr><td>Load</td><td>${fmt(op.load_pct, 0)} % <span class="vib-meta" title="${esc(op.load_definition || "")}">of rated kW</span></td></tr>
        </table>
      </div>
    </div>

    <div class="vib-box" style="margin-top:12px;">
      <div class="vib-box-title">Velocity trend ${provenancePill(sv.provenance)}</div>
      ${vibrationSparkline((v.trend && v.trend.points) || [])}
    </div>

    <div class="vib-box" style="margin-top:12px;">
      <div class="vib-box-title">Waveform features ${wf ? provenancePill(wf.provenance) : ""}</div>
      ${wf ? `
        <table class="telemetry-table vib-table">
          ${wfRows.map(([k, val, u, d]) => `<tr><td>${esc(k)}</td><td>${val === null || val === undefined ? "—" : `${fmt(val, d)} ${esc(u)}`}</td></tr>`).join("")}
        </table>
        <div class="vib-meta">${esc(wf.source || "")} · ${fmt(wf.sampling_rate_hz, 0)} Hz × ${esc(wf.n_samples)} samples · band ${wf.effective_band_hz ? esc(wf.effective_band_hz.join("–")) + " Hz" : "n/a"} · received ${esc(wf.received_at || "—")}${wf.stale ? " · STALE" : ""}</div>
      ` : `<div class="text-muted" style="font-size:12px;">No waveform received for this machine. Frequency features (acceleration RMS, kurtosis, dominant frequency) need a sampled waveform via POST /api/iot/waveform.</div>`}
    </div>

    <div class="vib-box vib-dq" style="margin-top:12px;">
      <div class="vib-box-title">Sensor &amp; data quality <span class="pill ${esc(dq.dq_flag === "OK" ? "OK" : "WARN")}">${esc(dq.dq_flag || "—")}</span></div>
      ${dq.warnings && dq.warnings.length ? `<ul>${dq.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : `<div class="vib-meta">No warnings.</div>`}
    </div>
  `;
}

window.selectMachine = selectMachine;
window.fetchRegistry = fetchRegistry;
window.fetchWorkOrders = fetchWorkOrders;
window.fetchEconomics = fetchEconomics;
window.fetchMetrics = fetchMetrics;

// -----------------------------------------------------------------------------
// Initial Bootstrap
// -----------------------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
  initWebSocket();
  initEventLog();
  initAlerts();
  fetchRegistry();
  fetchWorkOrders();
  fetchEconomics();
  fetchMetrics();
  setInterval(fetchVibration, 2000);
});

