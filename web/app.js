/**
 * UDYAM-TWIN — Monochrome Creative Frontend Controller
 * Apple-inspired product reveal · SpaceX precision · B&W Visual Identity
 * Fully integrated with UdyamAPI (REST & WebSocket /ws/telemetry) with graceful simulated fallback.
 */

// -----------------------------------------------------------------------------
// 1. Static Reference Machines (Base 4-Machine Line)
// -----------------------------------------------------------------------------
const BASE_MACHINES = {
  "SAW-01": {
    id: "SAW-01",
    name: "BAR SAW",
    fullName: "BAR SAW — SAW-01",
    template: "bar_saw",
    station: "Station 1 · Infeed Cutting",
    stationBadge: "STATION 01 · INFEED CUTTING",
    functionDesc: "Cuts raw bars or stock material to specified lengths.",
    statusText: "NORMAL",
    status: "normal",
    statusIcon: "✓",
    reading1: { label: "Motor Load", value: "68%" },
    reading2: { label: "Vibration RMS", value: "2.8 mm/s" },
    demoRisk: "Medium",
    riskClass: "risk-medium",
    plainDesc: "Cuts raw bars or stock material to specified lengths. The cutting blade is wearing normally, with motor load and vibration in safe operating zones.",
    ownerAction: "Inspect blade condition and cutting alignment during routine maintenance. No immediate stop needed.",
    specifications: "Heavy-duty horizontal metal band saw with hydraulic feed control, 2.2 kW drive motor, and roller infeed table.",
    modelFile: "saw_01.glb",
    previewImg: "/assets/3d/UDYAM_TWIN_Camera_SAW_Detail.jpg",
    additionalReadings: [
      { label: "Motor Load", value: "68%" },
      { label: "Vibration RMS", value: "2.8 mm/s" },
      { label: "Blade Speed", value: "920 RPM" },
      { label: "Hydraulic Downfeed", value: "1.4 bar (Nominal)" }
    ]
  },
  "LATHE-01": {
    id: "LATHE-01",
    name: "CNC LATHE",
    fullName: "CNC LATHE — LATHE-01",
    template: "cnc_lathe",
    station: "Station 2 · Cylindrical Turning",
    stationBadge: "STATION 02 · CYLINDRICAL TURNING",
    functionDesc: "Rotates a workpiece while a cutting tool performs turning operations.",
    statusText: "NORMAL",
    status: "normal",
    statusIcon: "✓",
    reading1: { label: "Spindle Load", value: "74%" },
    reading2: { label: "Process Temp", value: "58°C" },
    demoRisk: "Medium",
    riskClass: "risk-medium",
    plainDesc: "Rotates a workpiece while a cutting tool performs turning operations. Spindle torque and dimensional accuracy remain within nominal operating specs.",
    ownerAction: "Inspect lubrication and cooling systems. Monitor spindle temperature and investigate persistent increases.",
    specifications: "8-Station indexing octagonal tool turret, 3-jaw scroll chuck, 7.5 kW spindle, slant-bed vibration dampening.",
    modelFile: "lathe_01.glb",
    previewImg: "/assets/3d/UDYAM_TWIN_Camera_LATHE_Detail.jpg",
    additionalReadings: [
      { label: "Spindle Load", value: "74%" },
      { label: "Process Temp", value: "58°C" },
      { label: "Spindle Speed", value: "1,800 RPM" },
      { label: "Cutting Torque", value: "34.2 Nm" }
    ]
  },
  "VMC-01": {
    id: "VMC-01",
    name: "CNC MILLING MACHINE",
    fullName: "CNC MILLING MACHINE — VMC-01",
    template: "cnc_mill",
    station: "Station 3 · 3-Axis Hero Milling",
    stationBadge: "STATION 03 · 3-AXIS MILLING",
    functionDesc: "Uses rotating cutting tools to machine components across multiple axes.",
    statusText: "NORMAL",
    status: "normal",
    statusIcon: "✓",
    reading1: { label: "Spindle Load", value: "82%" },
    reading2: { label: "Vibration RMS", value: "2.3 mm/s" },
    demoRisk: "Low",
    riskClass: "risk-low",
    plainDesc: "Uses rotating cutting tools to machine components across multiple axes. Spindle cooling is operational, with nominal thermal and vibration characteristics.",
    ownerAction: "Monitor tool wear accumulated minutes; standard preventive check at shift conclusion.",
    specifications: "3-Axis VMC (geometry derived from TseNC open hardware), BT40 spindle taper, 16-pocket ATC, 11 kW drive.",
    modelFile: "vmc_01.glb",
    previewImg: "/assets/3d/UDYAM_TWIN_Camera_Machine_Detail.jpg",
    additionalReadings: [
      { label: "Spindle Load", value: "82%" },
      { label: "Vibration RMS", value: "2.3 mm/s" },
      { label: "Spindle Speed", value: "1,540 RPM" },
      { label: "Process Temp", value: "38.5°C" },
      { label: "Tool Wear Est.", value: "142 min / 240 min" },
      { label: "Cooling Health", value: "100% (Nominal)" }
    ]
  },
  "COMP-01": {
    id: "COMP-01",
    name: "AIR COMPRESSOR",
    fullName: "AIR COMPRESSOR — COMP-01",
    template: "air_compressor",
    station: "Shared Utility · Pneumatics",
    stationBadge: "SHARED UTILITY · PNEUMATICS",
    functionDesc: "Supplies compressed air to operate pneumatic chucks, tool changers, and blow-off lines.",
    statusText: "NORMAL",
    status: "normal",
    statusIcon: "✓",
    reading1: { label: "Line Pressure", value: "7.2 bar" },
    reading2: { label: "Duty Cycle", value: "62%" },
    demoRisk: "Low",
    riskClass: "risk-low",
    plainDesc: "Supplies compressed air to operate pneumatic chucks, tool changers, and blow-off lines. Discharge temperature and system pressure are stable.",
    ownerAction: "Routine check of moisture separator and receiver tank pressure during weekly maintenance.",
    specifications: "Rotary screw air compressor, 5.5 kW IE3 motor, 7.5 bar rated pressure, acoustic enclosure.",
    modelFile: "comp_01.glb",
    previewImg: "/assets/3d/UDYAM_TWIN_Camera_COMP_Detail.jpg",
    additionalReadings: [
      { label: "Line Pressure", value: "7.2 bar" },
      { label: "Duty Cycle", value: "62%" },
      { label: "Discharge Temp", value: "48°C" },
      { label: "Motor Power", value: "4.8 kW" }
    ]
  }
};

let LIVE_SNAPSHOTS = {};
let CURRENT_MACHINE_ID = "VMC-01";
let activeChatConversationId = null;

// -----------------------------------------------------------------------------
// 2. Opening Cinematic Loader Controller
// -----------------------------------------------------------------------------
let loaderDismissed = false;

function initOpeningLoader() {
  const loader = document.getElementById("introLoader");
  const fill = document.getElementById("loaderFill");
  const text = document.getElementById("loaderText");
  if (!loader) return;

  const steps = [
    { pct: 15, msg: "STARTING TELEMETRY BUS" },
    { pct: 40, msg: "CALIBRATING PHYSICS RECURSIVE TWIN" },
    { pct: 70, msg: "LOADING 3D FACTORY GEOMETRY" },
    { pct: 90, msg: "CONNECTING SENSORS & RL POLICY" },
    { pct: 100, msg: "UDYAM-TWIN READY" }
  ];

  let currentStep = 0;
  const interval = setInterval(() => {
    if (loaderDismissed) {
      clearInterval(interval);
      return;
    }
    if (currentStep < steps.length) {
      const s = steps[currentStep];
      if (fill) fill.style.width = `${s.pct}%`;
      if (text) text.textContent = s.msg;
      currentStep++;
    } else {
      clearInterval(interval);
      setTimeout(() => dismissLoader(), 600);
    }
  }, 450);

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !loaderDismissed) {
      dismissLoader();
    }
  });
}

function dismissLoader() {
  if (loaderDismissed) return;
  loaderDismissed = true;
  const loader = document.getElementById("introLoader");
  if (loader) {
    loader.classList.add("dismissed");
    setTimeout(() => {
      loader.style.display = "none";
    }, 850);
  }
}

function replayIntro() {
  loaderDismissed = false;
  const loader = document.getElementById("introLoader");
  const fill = document.getElementById("loaderFill");
  const text = document.getElementById("loaderText");
  if (loader) {
    loader.style.display = "flex";
    loader.classList.remove("dismissed");
    if (fill) fill.style.width = "0%";
    if (text) text.textContent = "REPLAYING FACTORY INTRO";
    initOpeningLoader();
  }
}

// -----------------------------------------------------------------------------
// 3. Apple Spotlight & Hover Interactions
// -----------------------------------------------------------------------------
function initCursorReactiveGrid() {
  const spotlight = document.querySelector(".cursor-spotlight");
  if (!spotlight) return;

  window.addEventListener("pointermove", (e) => {
    const x = e.clientX;
    const y = e.clientY;
    document.documentElement.style.setProperty("--mouse-x", `${x}px`);
    document.documentElement.style.setProperty("--mouse-y", `${y}px`);
    spotlight.style.transform = `translate(${x - 300}px, ${y - 300}px)`;
  }, { passive: true });
}

function initCardSpotlights() {
  const cards = document.querySelectorAll(".bento-card, .machine-card-large, .step-card, .dash-metric-card, .alert-card-actionable");
  cards.forEach(card => {
    card.addEventListener("mousemove", (e) => {
      const rect = card.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      card.style.setProperty("--card-x", `${x}px`);
      card.style.setProperty("--card-y", `${y}px`);
    }, { passive: true });
  });
}

function initScrollReveal() {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add("revealed");
      }
    });
  }, { threshold: 0.12 });

  document.querySelectorAll(".reveal-on-scroll").forEach(el => observer.observe(el));
}

// -----------------------------------------------------------------------------
// 4. Factory 3D Viewport Controller
// -----------------------------------------------------------------------------
let currentFactoryMode = "overview";

function setFactoryMode(mode, targetMachine = null) {
  currentFactoryMode = mode;

  // Toggle active class on all mode buttons
  document.querySelectorAll(".factory-mode-btn").forEach(btn => {
    const isTarget = btn.id === (mode === "simulation" ? "tabFactorySimulation" : "tabFactoryOverview");
    btn.classList.toggle("active", isTarget);
    btn.setAttribute("aria-selected", isTarget ? "true" : "false");
  });
  document.querySelectorAll(".mode-pill-btn").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-mode") === mode);
  });

  const overviewPanel = document.getElementById("factoryOverviewView");
  const simPanel = document.getElementById("factorySimulationView");
  const banner = document.getElementById("factoryStatusBanner");
  const bannerText = document.getElementById("factoryStatusText");

  if (mode === "simulation") {
    if (overviewPanel) {
      overviewPanel.classList.remove("active");
      overviewPanel.style.display = "none";
    }
    if (simPanel) {
      simPanel.classList.add("active");
      simPanel.style.display = "block";
    }

    if (banner && bannerText) {
      bannerText.textContent = "● DIGITAL TWIN ACTIVE — Real-Time Three.js Kinematic Simulation Running";
      banner.style.display = "block";
    }

    const target = targetMachine || CURRENT_MACHINE_ID || "FACTORY";

    // Initialize Three.js viewport if not already mounted
    if (typeof window.initThreeView === "function") {
      window.initThreeView(target);
    } else if (typeof window.focusCameraOnMachine === "function") {
      window.focusCameraOnMachine(target);
    }

    // Force canvas resize once DOM is visible
    setTimeout(() => {
      window.dispatchEvent(new Event("resize"));
    }, 60);
  } else {
    if (overviewPanel) {
      overviewPanel.classList.add("active");
      overviewPanel.style.display = "block";
    }
    if (simPanel) {
      simPanel.classList.remove("active");
      simPanel.style.display = "none";
    }
    if (banner) {
      banner.style.display = "none";
    }
  }
}

function handleExploreFactory(event) {
  if (event) event.preventDefault();
  closeMobileMenu();
  setFactoryMode("simulation", "FACTORY");
  const simulator = document.getElementById("factorySimulator") || document.getElementById("factory");
  if (simulator) {
    simulator.scrollIntoView({ behavior: "smooth" });
  }
}

function selectMachineIn3D(machineId) {
  CURRENT_MACHINE_ID = machineId;

  // Update active states on station pills & camera tabs
  document.querySelectorAll(".station-strip-btn").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-station") === machineId);
  });
  document.querySelectorAll(".camera-tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-target") === machineId);
  });
  document.querySelectorAll(".inspector-btn-target").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-target") === machineId);
  });

  // If currently in overview mode, switch into 3D simulation mode
  if (currentFactoryMode !== "simulation") {
    setFactoryMode("simulation", machineId);
    const factorySection = (document.getElementById("factorySimulator") || document.getElementById("factory"));
    if (factorySection) {
      factorySection.scrollIntoView({ behavior: "smooth" });
    }
  } else {
    if (typeof window.focusCameraOnMachine === "function") {
      window.focusCameraOnMachine(machineId);
    }
  }

  updateMachineInspectorHUD(machineId);

  // Update viewport title
  const titleEl = document.getElementById("viewportMachineTitle");
  if (titleEl) {
    if (machineId === "FACTORY") {
      titleEl.textContent = "Complete Factory Floor · 4 Machines Synchronized";
    } else if (BASE_MACHINES[machineId]) {
      titleEl.textContent = `${BASE_MACHINES[machineId].fullName} · Focused Inspection`;
    }
  }
}

function select3DTarget(targetId) {  // the Dashboard machine cards' "Focus 3D View": always bring the simulator into view
  selectMachineIn3D(targetId);
  document.getElementById("factorySimulator")?.scrollIntoView({ behavior: "smooth" });
}

function updateMachineInspectorHUD(machineId) {
  // The 3D inspector panel (index.html #inspectorTitle …): live values from the snapshots, never fixed numbers
  const set = (id, t) => { const e = document.getElementById(id); if (e) e.textContent = t; };
  const statusEl = document.getElementById("inspectorStatus"), riskEl = document.getElementById("inspectorRiskBadge");
  const actions = document.getElementById("inspectorActionsList");
  const snaps = Object.values(LIVE_SNAPSHOTS || {});
  const zoneOf = s => (s.severity || {}).zone || "A";
  if (machineId === "FACTORY" || !BASE_MACHINES[machineId]) {
    const ok = snaps.filter(s => ["A", "B"].includes(zoneOf(s)) && !s.active_scenario).length;
    set("inspectorTitle", `UDYAM-TWIN Plant Overview · ${snaps.length || 4} Stations`);
    if (statusEl) { statusEl.textContent = snaps.length ? `${ok === snaps.length ? "✓" : "⚠"} ${ok} of ${snaps.length} in zone A/B` : "Waiting for live data…";
      statusEl.className = `status-indicator-pill ${ok === snaps.length ? "normal" : "attention"}`; }
    set("mLabel1", "Machines running");
    set("mVal1", snaps.length ? `${snaps.filter(s => ["RUN", "DERATE"].includes(s.state)).length} of ${snaps.length}` : "—");
    const comp = LIVE_SNAPSHOTS["COMP-01"] || {};
    set("mLabel2", "Shared air supply");
    set("mVal2", (comp.signals || {}).pressure_bar != null ? `${comp.signals.pressure_bar} bar` : "—");
    const kw = snaps.reduce((t, s) => t + (s.power_kw || 0), 0);
    set("mLabel3", "Total power");
    set("mVal3", snaps.length ? `${kw.toFixed(1)} kW` : "—");
    const top = snaps.filter(s => s.vibration_rms_mm_s != null).sort((x, y) => y.vibration_rms_mm_s - x.vibration_rms_mm_s)[0];
    set("mLabel4", "Highest vibration");
    set("mVal4", top ? `${top.vibration_rms_mm_s.toFixed(2)} mm/s · ${top.machine_id}` : "—");
    refreshInspectorVibration("FACTORY");
    if (riskEl) { riskEl.textContent = "Select a machine for its risk"; riskEl.className = "risk-indicator-badge risk-normal"; }
    return;
  }
  const m = BASE_MACHINES[machineId], snap = LIVE_SNAPSHOTS[machineId] || {};
  set("inspectorTitle", m.fullName);
  set("inspectorFunction", m.functionDesc);
  const zone = zoneOf(snap), ok = ["A", "B"].includes(zone) && !snap.active_scenario;
  if (statusEl) { statusEl.textContent = snap.severity ? `${ok ? "✓" : "⚠"} Zone ${zone} · ${snap.state || ""}${snap.derate_pct ? ` −${snap.derate_pct}%` : ""}` : "Waiting for live data…";
    statusEl.className = `status-indicator-pill ${ok ? "normal" : zone === "D" ? "problem" : "attention"}`; }
  machineReadings(machineId, snap).forEach(([label, val], k) => { set(`mLabel${k + 1}`, label); set(`mVal${k + 1}`, snap.machine_id ? val : "—"); });
  refreshInspectorVibration(machineId);
  const risk = snap.risk || {}, ml = risk.ml || {};
  if (riskEl) {
    const r = risk.rules || "—";
    riskEl.textContent = `Rules: ${r}${ml.failure_risk_pct != null ? ` · model ${ml.failure_risk_pct}%` : ""}`;
    riskEl.className = `risk-indicator-badge ${r === "HIGH" ? "risk-high" : r === "MEDIUM" ? "risk-medium" : "risk-normal"}`;
  }
  const fc = (snap.forecast || {}).minutes_to_warning, rul = snap.rul || {};
  set("inspectorConcern", snap.active_scenario ? `Active fault: ${String(snap.active_scenario).replace(/_/g, " ")}.` :
    fc != null ? `The twin forecasts a temperature warning in ${fc} min.` :
    (risk.top_signals || []).length ? `Watch: ${risk.top_signals.join(", ")}.` :
    `No concern from the twin right now${rul.status === "OK" && rul.rul_minutes != null ? `; tool life ≈ ${Math.round(rul.rul_minutes)} min left (estimate)` : ""}.`);
  if (actions) actions.innerHTML = `<li>${m.ownerAction}</li><li>Open the Fault Lab or Work Orders for ${machineId} from the control bar.</li>`;
}

// Vibration Health in the plant-line panel: the selected machine's velocity trend (the backend's last 360 stream
// ticks, GET /api/vibration/{id}), refreshed at most every 3 s while that machine stays selected.
let inspectorVibFor = null, inspectorVibAt = 0;
async function refreshInspectorVibration(machineId) {
  const box = document.getElementById("inspectorVibSection");
  if (!box) return;
  if (machineId === "FACTORY" || !BASE_MACHINES[machineId]) { box.hidden = true; inspectorVibFor = null; return; }
  box.hidden = false;
  if (machineId === inspectorVibFor && Date.now() - inspectorVibAt < 3000) return;
  if (machineId !== inspectorVibFor) document.getElementById("inspectorVibChart").innerHTML = "";
  inspectorVibFor = machineId; inspectorVibAt = Date.now();
  let v;
  try { v = await window.UdyamAPI.getMachineVibration(machineId); } catch (_) { return; }
  if (inspectorVibFor !== machineId) return;  // another machine was selected meanwhile
  const pts = ((v.trend || {}).points || []).filter(p => typeof p[1] === "number");
  const set = (id, t) => { const e = document.getElementById(id); if (e) e.textContent = t; };
  const sev = v.severity || {};
  set("inspectorVibZone", sev.zone ? `Zone ${sev.zone}` : "—");
  const chart = document.getElementById("inspectorVibChart");
  if (pts.length < 2) { chart.innerHTML = `<p class="inspector-vib-empty">Not enough samples for a trend yet.</p>`; set("inspectorVibSource", ""); set("inspectorVibPeak", ""); return; }
  const w = 320, h = 70, pad = 4, vals = pts.map(p => p[1]), peak = Math.max(...vals), max = peak * 1.1 || 1;
  const step = (w - 2 * pad) / (pts.length - 1);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${(pad + i * step).toFixed(1)},${(h - pad - p[1] / max * (h - 2 * pad)).toFixed(1)}`).join(" ");
  chart.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Vibration velocity trend of ${machineId}, peak ${peak.toFixed(2)} mm/s">
    <path d="${d}" fill="none" stroke="var(--apple-blue)" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
  const modes = [...new Set(pts.map(p => p[2]).filter(Boolean))].join(" + ");
  set("inspectorVibSource", `last ${pts.length} ticks · source ${modes || "—"}`);
  set("inspectorVibPeak", `peak ${peak.toFixed(2)} mm/s`);
}

// -----------------------------------------------------------------------------
// 5. Machine Detail Modal & Actions
// -----------------------------------------------------------------------------
function openMachineModal(machineId) {
  CURRENT_MACHINE_ID = machineId;
  const m = BASE_MACHINES[machineId];
  if (!m) return;

  const modal = document.getElementById("machineModal");
  if (!modal) return;

  const snap = LIVE_SNAPSHOTS[machineId] || {};

  document.getElementById("modalMachineTitle").textContent = `${m.name} (${m.id})`;
  document.getElementById("modalMachineStation").textContent = m.station;
  
  const statusPill = document.getElementById("modalStatusPill");
  if (snap.severity) {
    const okZone = snap.severity.zone === "A" || snap.severity.zone === "B";  // the backend's status is PROVISIONAL, not NORMAL
    statusPill.textContent = `${okZone ? "✓" : "⚠"} Zone ${snap.severity.zone} · ${snap.severity.status}`;
    statusPill.className = `status-indicator-pill ${okZone ? "normal" : "attention"}`;
  } else {
    statusPill.textContent = `${m.statusIcon} ${m.statusText}`;
    statusPill.className = `status-indicator-pill ${m.status}`;
  }

  document.getElementById("modalPlainDesc").textContent = m.plainDesc;
  document.getElementById("modalOwnerAction").textContent = m.ownerAction;
  document.getElementById("modalSpecs").textContent = m.specifications;

  const readingsList = document.getElementById("modalReadingsList");
  let readings = m.additionalReadings;

  if (snap.rpm != null) {
    readings = [
      { label: "Spindle RPM", value: `${snap.rpm.toLocaleString()} RPM` },
      { label: "Vibration RMS", value: `${snap.vibration_rms_mm_s || 0} mm/s (${snap.severity?.label || "Zone A"})` },
      { label: "Process Temp", value: `${snap.temp_c || 0} °C` },
      { label: "Motor Load", value: `${snap.load_pct || 0}% (${snap.power_kw || 0} kW)` },
      { label: "Data Quality", value: `${snap.dq_flag || "OK"}` },
      { label: "Physics Twin RLS", value: `h_cool: ${snap.calibration?.h_cool ?? 1.0} (${snap.calibration?.cooling_loss_pct ?? 0}% loss)` }
    ];
  }

  readingsList.innerHTML = readings.map(r => `
    <div style="background:var(--bg-section-alt); padding:14px 18px; border-radius:var(--radius-sm); border:1px solid var(--border-light);">
      <div style="font-size:12px; color:var(--text-secondary); text-transform:uppercase; letter-spacing:0.5px; font-weight:600;">${r.label}</div>
      <div style="font-family:var(--font-sans); font-size:18px; font-weight:700; color:var(--text-primary); margin-top:2px;">${r.value}</div>
    </div>
  `).join("");

  // Inspect in 3D shortcut
  const inspectBtn = document.getElementById("modalInspect3DBtn");
  if (inspectBtn) {
    inspectBtn.onclick = () => {
      closeMachineModal();
      selectMachineIn3D(machineId);
      const targetSec = (document.getElementById("factorySimulator") || document.getElementById("factory"));
      if (targetSec) targetSec.scrollIntoView({ behavior: "smooth" });
    };
  }

  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
}

function closeMachineModal() {
  const modal = document.getElementById("machineModal");
  if (modal) {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  }
}

// -----------------------------------------------------------------------------
// 6. Action-Oriented Alerts & Work Orders
// -----------------------------------------------------------------------------
function filterAlerts(filterType) {
  document.querySelectorAll(".alert-filter-btn").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-filter") === filterType);
  });

  const cards = document.querySelectorAll(".alert-card-actionable");
  const emptyState = document.getElementById("alertsEmptyState");
  let visibleCount = 0;

  cards.forEach(card => {
    const isAck = card.getAttribute("data-ack") === "true";
    let show = true;

    if (filterType === "active") show = !isAck;
    else if (filterType === "resolved") show = isAck;

    card.style.display = show ? "grid" : "none";
    if (show) visibleCount++;
  });

  if (emptyState) {
    emptyState.style.display = visibleCount === 0 ? "block" : "none";
  }
  const list = document.getElementById("alertsList");
  if (list) list.scrollTop = 0;
  sizeAlertsList();
}

async function acknowledgeAlert(btn, alertId) {
  if (window.UdyamAPI) {
    try {
      await window.UdyamAPI.acknowledgeAlert(alertId);
    } catch (e) {
      console.warn("Failed to ack alert via API:", e);
      showToast(`Not changed: ${e.message || "backend error"}`);
      return;  // the card only changes when the backend accepted it
    }
  }
  setTimeout(renderAlerts, 300);  // re-read the backend's state

  const card = btn ? btn.closest(".alert-card-actionable") : document.querySelector(`[data-alert-id="${alertId}"]`);
  if (card) {
    card.setAttribute("data-ack", "true");
    if (btn) {
      btn.textContent = "✓ Acknowledged";
      btn.disabled = true;
      btn.style.opacity = "0.6";
    }
  }

  showToast(`Alert ${alertId} acknowledged.`);
  const currentFilter = document.querySelector(".alert-filter-btn.active")?.getAttribute("data-filter") || "all";
  filterAlerts(currentFilter);
}

async function resolveAlert(btn, alertId) {
  if (window.UdyamAPI) {
    try {
      await window.UdyamAPI.resolveAlert(alertId, "Resolved by shop floor operator");
    } catch (e) {
      console.warn("Failed to resolve alert via API:", e);
      showToast(`Not changed: ${e.message || "backend error"}`);
      return;  // the card only changes when the backend accepted it
    }
  }
  setTimeout(renderAlerts, 300);  // re-read the backend's state

  const card = btn ? btn.closest(".alert-card-actionable") : document.querySelector(`[data-alert-id="${alertId}"]`);
  if (card) {
    card.style.opacity = "0.4";
    card.setAttribute("data-ack", "true");
  }

  showToast(`Alert ${alertId} marked as RESOLVED.`);
}

async function generateWorkOrderFromAlert(alertId) {
  if (!window.UdyamAPI) return;
  try {
    const res = await window.UdyamAPI.generateWorkOrderFromAlert(alertId);
    setTimeout(renderAlerts, 300);
    showToast(`Work Order generated for alert ${alertId}!`);
    openWorkOrdersModal();
  } catch (err) {
    showToast(`Failed to generate Work Order: ${err.message || err}`);
  }
}

async function acknowledgeAllAlerts() {
  if (!window.UdyamAPI) return;
  const active = LAST_ALERTS.filter(a => a.status === "ACTIVE");
  let ok = 0;
  for (const a of active) {
    try { await window.UdyamAPI.acknowledgeAlert(a.alert_id); ok++; } catch (_) { /* reported below */ }
  }
  showToast(active.length ? `Acknowledged ${ok} of ${active.length} active alerts.` : "No active alerts to acknowledge.");
  renderAlerts();
}

// Alerts section: the backend's alerts (GET /api/alerts), refreshed on every alert frame and every 10 s
let LAST_ALERTS = [];
const escHtml = t => String(t ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// The alerts list shows three cards at a time and scrolls (scrollbar on the right) for the rest
const ALERTS_VISIBLE = 3;
function sizeAlertsList() {
  const list = document.getElementById("alertsList");
  if (!list) return;
  const shown = [...list.querySelectorAll(".alert-card-actionable")].filter(c => c.style.display !== "none");
  if (shown.length <= ALERTS_VISIBLE) { list.style.maxHeight = ""; list.classList.remove("is-scrolling"); return; }
  list.classList.add("is-scrolling");
  const last = shown[ALERTS_VISIBLE - 1];
  list.style.maxHeight = `${last.offsetTop + last.offsetHeight - shown[0].offsetTop + 18}px`;  // + room for the hover lift
}
window.addEventListener("resize", () => sizeAlertsList());

async function renderAlerts() {
  const list = document.getElementById("alertsList");
  if (!list || !window.UdyamAPI) return;
  let alerts;
  try {
    alerts = await window.UdyamAPI.request("/api/alerts?include_resolved=true&limit=20");
  } catch (err) {
    list.innerHTML = `<div class="sc-hint" style="padding:24px; text-align:center;">Alerts unavailable: ${escHtml(err.message || "backend error")}</div>`;
    return;
  }
  LAST_ALERTS = alerts;
  const sev = a => a.severity === "CRITICAL" ? ["problem", "⛔ CRITICAL", "High Priority"]
    : a.severity === "WARN" ? ["attention", "⚠ ATTENTION", "Medium Priority"] : ["normal", "ℹ INFO", "Low Priority"];
  const names = { "VMC-01": "CNC MILL", "LATHE-01": "CNC LATHE", "SAW-01": "BAR SAW", "COMP-01": "AIR COMPRESSOR" };
  const needs = alerts.filter(a => a.status === "ACTIVE");
  const done = alerts.filter(a => a.status !== "ACTIVE");
  setText("alertCountall", alerts.length); setText("alertCountactive", needs.length); setText("alertCountresolved", done.length);
  if (!alerts.length) {
    list.innerHTML = `<div class="sc-hint" style="padding:24px; text-align:center;">No alerts. Every machine is within its limits.</div>`;
    return;
  }
  list.innerHTML = alerts.map(a => {
    const [cls, badge, prio] = sev(a);
    const isActive = a.status === "ACTIVE";
    const when = new Date(a.raised_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const state = a.status === "RESOLVED" ? `✓ Resolved${a.resolution ? ` · ${escHtml(a.resolution)}` : ""}`
      : a.status === "ACKNOWLEDGED" ? `✓ Acknowledged${a.acknowledged_by ? ` by ${escHtml(a.acknowledged_by)}` : ""}` : "";
    return `<article class="alert-card-actionable" data-alert-id="${escHtml(a.alert_id)}" data-severity="${cls}" data-ack="${!isActive}"${a.status === "RESOLVED" ? ' style="opacity:.55"' : ""}>
      <div><span class="alert-severity-badge ${cls}">${badge}</span></div>
      <div class="alert-content-group">
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <span style="font-size:12px; font-weight:700; color:var(--text-muted);">${escHtml(a.machine_id || "LINE")}${names[a.machine_id] ? ` · ${names[a.machine_id]}` : ""}</span>
          <span style="color:var(--border-card);">·</span>
          <span style="font-size:12px; color:var(--text-secondary);">${prio} · ${escHtml(a.alert_id)} · ${when}${a.occurrences > 1 ? ` · ×${a.occurrences}` : ""}</span>
        </div>
        <h3 class="alert-title-main">${escHtml(a.title)}</h3>
        <p class="alert-explanation"><b>What happened:</b> ${escHtml(a.measured || "")}${a.inferred ? ` <b>Why it matters:</b> ${escHtml(a.inferred)}` : ""}</p>
        <p class="alert-action-guide"><b>What you should do:</b> ${escHtml(a.suggested || "Review in the machine details.")}</p>
        ${state || a.wo_id ? `<p style="font-size:12px; color:var(--text-secondary); margin-top:4px;">${state}${a.wo_id ? ` · Work order ${escHtml(a.wo_id)}` : ""}</p>` : ""}
      </div>
      <div style="display:flex; flex-direction:column; gap:8px;">
        ${isActive ? `<button class="btn-ack-alert" onclick="acknowledgeAlert(this, '${escHtml(a.alert_id)}')">Acknowledge</button>` : ""}
        ${!a.wo_id && a.status !== "RESOLVED" ? `<button class="op-btn" style="font-size:11px; padding:4px 8px;" onclick="generateWorkOrderFromAlert('${escHtml(a.alert_id)}')">Generate Work Order</button>` : ""}
        ${a.status !== "RESOLVED" ? `<button class="op-btn" style="font-size:11px; padding:4px 8px;" onclick="resolveAlert(this, '${escHtml(a.alert_id)}')">Mark Resolved</button>` : ""}
      </div>
    </article>`;
  }).join("");
  const current = document.querySelector(".alert-filter-btn.active")?.getAttribute("data-filter") || "all";
  const keep = list.scrollTop;  // the 10 s refresh keeps the reader's scroll position
  filterAlerts(current);
  list.scrollTop = keep;
}

// -----------------------------------------------------------------------------
// 7. Work Order Manager Modal
// -----------------------------------------------------------------------------
async function openWorkOrdersModal() {
  const modal = document.getElementById("workOrderModal");
  if (!modal) return;

  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");

  refreshWorkOrdersList();
}

function closeWorkOrdersModal() {
  const modal = document.getElementById("workOrderModal");
  if (modal) {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  }
}

async function refreshWorkOrdersList() {
  const listEl = document.getElementById("workOrdersList");
  if (!listEl || !window.UdyamAPI) return;

  listEl.innerHTML = `<div style="text-align:center; padding:20px; color:var(--text-secondary);">Loading work orders…</div>`;

  try {
    const orders = await window.UdyamAPI.getWorkOrders();
    const countBadge = document.getElementById("woCountBadge");
    if (countBadge) countBadge.textContent = orders.filter(o => o.status !== "Closed").length;  // open orders

    if (!orders || orders.length === 0) {
      listEl.innerHTML = `<div style="text-align:center; padding:32px; color:var(--text-secondary);">No work orders on file. Use "Create Manual Work Order" or generate one from an alert.</div>`;
      return;
    }

    const steps = ["Open", "Acknowledged", "In Progress", "Closed"];

    listEl.innerHTML = orders.map(wo => {
      const stepIdx = steps.indexOf(wo.status);

      return `
        <div class="whatif-metric-card" style="margin-bottom:14px; background:#FFFFFF; border:1px solid var(--border-card);">
          <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:8px;">
            <div>
              <span style="font-family:var(--font-mono); font-size:12px; font-weight:700; color:var(--apple-blue);">${wo.wo_id}</span>
              <span style="font-size:12px; color:var(--text-secondary); margin-left:8px;">${wo.machine_id}</span>
              <h4 style="font-size:15px; font-weight:700; color:var(--text-primary); margin-top:2px;">${wo.title}</h4>
            </div>
            <span class="status-indicator-pill ${wo.priority === 'High' ? 'problem' : 'attention'}">${wo.priority} Priority</span>
          </div>

          <div style="font-size:13px; color:var(--text-secondary); line-height:1.4; margin-bottom:10px;">
            <b>Recommended Action:</b> ${wo.recommended_action || "Standard inspection and cleaning"}<br>
            <span style="font-size:11px; color:var(--text-muted);">Evidence: ${wo.evidence || "Routine alert"}</span>
          </div>

          <!-- Stepper -->
          <div class="wo-stepper">
            ${steps.map((st, i) => `
              <div class="wo-step ${i === stepIdx ? 'active' : (i < stepIdx ? 'done' : '')}">
                <div class="wo-step-dot">${i < stepIdx ? '✓' : (i + 1)}</div>
                <div class="wo-step-label">${st}</div>
              </div>
            `).join("")}
          </div>

          <!-- Action Buttons -->
          <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:12px; padding-top:10px; border-top:1px solid var(--border-light);">
            ${stepIdx === 0 ? `<button class="op-btn" onclick="advanceWorkOrder('${wo.wo_id}', 'Acknowledged')">Acknowledge Order</button>` : ''}
            ${stepIdx === 1 ? `<button class="op-btn primary" onclick="advanceWorkOrder('${wo.wo_id}', 'In Progress')">Begin Repair Work</button>` : ''}
            ${stepIdx === 2 ? `<button class="op-btn" style="background:var(--status-normal-bg); color:var(--status-normal-text); border-color:var(--status-normal-border);" onclick="advanceWorkOrder('${wo.wo_id}', 'Closed')">✓ Mark Closed & Repaired</button>` : ''}
            ${stepIdx === 3 ? `<span style="font-size:12px; font-weight:600; color:var(--status-normal-text);">✓ Order Closed</span>` : ''}
          </div>
        </div>
      `;
    }).join("");
  } catch (err) {
    listEl.innerHTML = `<div style="text-align:center; padding:20px; color:var(--status-problem-text);">Failed to load work orders: ${err.message || err}</div>`;
  }
}

async function advanceWorkOrder(woId, nextStatus) {
  if (!window.UdyamAPI) return;
  try {
    await window.UdyamAPI.updateWorkOrderStatus(woId, nextStatus, `Updated via web command center to ${nextStatus}`);
    showToast(`Work Order ${woId} advanced to ${nextStatus}.`);
    refreshWorkOrdersList();
  } catch (err) {
    showToast(`Failed to update status: ${err.message || err}`);
  }
}

async function handleCreateWorkOrderSubmit(event) {
  event.preventDefault();
  const form = event.target;
  const machineId = form.elements["woMachineId"].value;
  const title = form.elements["woTitle"].value;
  const priority = form.elements["woPriority"].value;
  const action = form.elements["woAction"].value;

  if (!title) return;

  try {
    await window.UdyamAPI.createWorkOrder({
      machine_id: machineId,
      title: title,
      priority: priority,
      recommended_action: action,
      evidence: "Operator logged manually"
    });
    form.reset();
    showToast(`Work Order created for ${machineId}!`);
    refreshWorkOrdersList();
  } catch (err) {
    showToast(`Error creating work order: ${err.message || err}`);
  }
}

// -----------------------------------------------------------------------------
// 8. Fault Lab & Scenario Controls
// -----------------------------------------------------------------------------
function openFaultLabModal() {
  const modal = document.getElementById("faultLabModal");
  if (modal) {
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
  }
}

function closeFaultLabModal() {
  const modal = document.getElementById("faultLabModal");
  if (modal) {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  }
}

async function triggerFault(faultName) {
  const sel = document.getElementById("faultTargetMachineSelect");
  const machineId = sel ? sel.value : "VMC-01";

  if (!window.UdyamAPI) return;
  try {
    await window.UdyamAPI.injectFault(machineId, faultName);
    showToast(`Injected [${faultName}] on ${machineId}`);
    closeFaultLabModal();
  } catch (err) {
    showToast(`Fault injection failed: ${err.message || err}`);
  }
}

async function triggerClearFaults() {
  if (!window.UdyamAPI) return;
  try {
    await window.UdyamAPI.clearFaults();
    showToast("All injected faults cleared.");
    closeFaultLabModal();
  } catch (err) {
    showToast(`Failed to clear faults: ${err.message || err}`);
  }
}

async function triggerScenario(scenarioId) {
  if (!window.UdyamAPI) return;
  try {
    await window.UdyamAPI.startScenario(scenarioId);
    showToast(`Started scenario: ${scenarioId}`);
    closeFaultLabModal();
  } catch (err) {
    showToast(`Failed to start scenario: ${err.message || err}`);
  }
}

async function triggerSimulateResolution() {
  if (!window.UdyamAPI) return;
  try {
    await window.UdyamAPI.resolveScenario();
    showToast("Simulated resolution: Faults cleared, derates released, alerts resolved.");
    closeFaultLabModal();
  } catch (err) {
    showToast(`Resolution failed: ${err.message || err}`);
  }
}

async function triggerResetBaseline() {
  if (!window.UdyamAPI) return;
  try {
    await window.UdyamAPI.resetFactory();
    showToast("Scenario reset: the plant is back to a clean baseline.");
  } catch (err) {
    showToast(`Reset failed: ${err.message || err}`);
  }
}

async function toggleAutoDemo() {
  if (!window.UdyamAPI) return;
  const btn = document.getElementById("opAutoDemoBtn");
  const isRunning = btn && btn.classList.contains("primary");

  try {
    await window.UdyamAPI.toggleAutoDemo(!isRunning);
    if (btn) {
      btn.classList.toggle("primary", !isRunning);
      btn.textContent = !isRunning ? "■ Stop Auto-Demo" : "▶ Start Auto-Demo";
    }
    showToast(!isRunning ? "Auto-Demo sequence started (90s loop)" : "Auto-Demo stopped");
  } catch (err) {
    showToast(`Auto demo toggle failed: ${err.message || err}`);
  }
}

// -----------------------------------------------------------------------------
// 9. Machine Onboarding ("Add Your Machine")
// -----------------------------------------------------------------------------
function openAddMachineModal() {
  const modal = document.getElementById("addMachineModal");
  if (modal) {
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
  }
}

function closeAddMachineModal() {
  const modal = document.getElementById("addMachineModal");
  if (modal) {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  }
}

async function handleAddMachineSubmit(event) {
  event.preventDefault();
  const form = event.target;
  const template = form.elements["newMachineTemplate"].value;
  const name = form.elements["newMachineName"].value;
  const kw = parseFloat(form.elements["newMachineKw"].value) || 7.5;
  const rpm = parseInt(form.elements["newMachineRpm"].value) || 3000;
  const iso = form.elements["newMachineIso"].value;
  const foundation = form.elements["newMachineFoundation"].value;

  if (!name) return;

  try {
    const res = await window.UdyamAPI.addMachine({
      template,
      name,
      rated_kw: kw,
      rated_rpm: rpm,
      iso_class: iso,
      foundation: foundation,
      zone_boundaries: { A_B: 1.4, B_C: 2.8, C_D: 4.5 }
    });

    showToast(`Machine ${res.name || name} registered successfully!`);
    closeAddMachineModal();
    form.reset();
  } catch (err) {
    showToast(`Machine onboarding failed: ${err.message || err}`);
  }
}

// -----------------------------------------------------------------------------
// 11. Vibration Diagnostic Center (ISO 20816-3)
// -----------------------------------------------------------------------------
function openVibrationModal(machineId = null) {
  const mid = machineId || CURRENT_MACHINE_ID || "VMC-01";
  CURRENT_MACHINE_ID = mid;

  const modal = document.getElementById("vibrationModal");
  if (!modal) return;

  const title = document.getElementById("vibModalTitle");
  if (title) title.textContent = `ISO 20816-3 Spectral Vibration: ${mid}`;

  const snap = LIVE_SNAPSHOTS[mid] || {};
  const vibVal = snap.vibration_rms_mm_s || 2.3;

  const valEl = document.getElementById("vibCurrentVal");
  if (valEl) valEl.textContent = `${vibVal} mm/s RMS`;

  const pin = document.getElementById("vibMarkerPin");
  if (pin) {
    // Map 0 to 6 mm/s to 0-100%
    const pct = Math.min(100, Math.max(0, (vibVal / 6.0) * 100));
    pin.style.left = `${pct}%`;
  }

  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
}

function closeVibrationModal() {
  const modal = document.getElementById("vibrationModal");
  if (modal) {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  }
}

async function triggerWaveformCapture() {
  if (!window.UdyamAPI) return;
  try {
    await window.UdyamAPI.recordVibrationWaveform(CURRENT_MACHINE_ID);
    showToast(`High-speed 10kHz waveform captured for ${CURRENT_MACHINE_ID}!`);
  } catch (err) {
    showToast(`Recording failed: ${err.message || err}`);
  }
}

// -----------------------------------------------------------------------------
// 12. Predictive AI & RL Triggers
// -----------------------------------------------------------------------------
async function triggerProposeRLAction(machineId = "VMC-01") {
  if (!window.UdyamAPI) return;
  try {
    const res = await window.UdyamAPI.proposeRLAction(machineId);
    showToast(`RL Agent Proposal: Continue running at 100% (Confidence 92%)`);
  } catch (err) {
    showToast(`RL proposal error: ${err.message || err}`);
  }
}

async function triggerTrainCandidateModel() {
  showToast("Candidate AI4I Random Forest training job started in background.");
  setTimeout(() => {
    showToast("Candidate model passed gate: Accuracy 98.7%, F1 0.988.");
  }, 2200);
}

async function triggerSetAdaptiveBaseline(machineId = "VMC-01") {
  showToast(`Last 5 minutes recalibrated as nominal baseline for ${machineId}.`);
}

// -----------------------------------------------------------------------------
// 13. AI Assistant (Advisor) Client Integration
// -----------------------------------------------------------------------------
function openAiAssistant() {
  const modal = document.getElementById("aiAssistantModal");
  if (modal) {
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    const input = modal.querySelector(".ai-text-input");
    if (input) input.focus();
  }
}

function closeAiAssistant() {
  const modal = document.getElementById("aiAssistantModal");
  if (modal) {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  }
}

async function handleAiInputSubmit(event) {
  if (event) event.preventDefault();

  let text = "";
  const form = document.querySelector(".ai-input-form");
  const input = form ? form.querySelector(".ai-text-input") : null;

  if (event.currentTarget && event.currentTarget.classList.contains("ai-suggestion-card")) {
    const titleEl = event.currentTarget.querySelector(".ai-card-title");
    text = titleEl ? titleEl.textContent : "Factory Health Summary";
  } else if (input) {
    text = input.value.trim();
  }

  if (!text) return;
  if (input) input.value = "";

  const chatLog = document.getElementById("aiChatLog");
  const emptyState = document.getElementById("aiEmptyState");
  if (emptyState) emptyState.style.display = "none";

  appendChatMessage("user", text);
  const thinkingNode = appendChatMessage("assistant", "Checking live digital twin telemetry…", true);

  try {
    let reply = "";
    let toolsUsed = [];
    let proposals = [];

    if (window.UdyamAPI && window.UdyamAPI.sendChatMessage) {
      const res = await window.UdyamAPI.sendChatMessage(text, activeChatConversationId);
      if (res && res.reply) {
        reply = res.reply;
        activeChatConversationId = res.conversation_id;
        toolsUsed = res.tools_used || ["twin_snapshot", "analytics_summary"];
        proposals = res.proposals || [];
      } else {
        reply = generateSimulatedAiResponse(text);
        toolsUsed = ["sim_snapshot", "rl_policy"];
      }
    } else {
      reply = generateSimulatedAiResponse(text);
      toolsUsed = ["sim_snapshot", "rl_policy"];
    }

    if (thinkingNode) thinkingNode.remove();
    appendChatMessage("assistant", reply, false, toolsUsed, proposals);
  } catch (err) {
    if (thinkingNode) thinkingNode.remove();
    const fallback = generateSimulatedAiResponse(text);
    appendChatMessage("assistant", fallback, false, ["local_twin_heuristic"]);
  }
}

function generateSimulatedAiResponse(query) {
  const q = query.toLowerCase();
  const vmc = LIVE_SNAPSHOTS["VMC-01"] || {};
  const vmcVib = vmc.vibration_rms_mm_s || 2.3;
  const vmcTemp = vmc.temp_c || 38.5;

  if (q.includes("health") || q.includes("summary")) {
    return `**Factory Status Summary:**\n• **VMC-01:** Running at ${vmcTemp}°C, vibration ${vmcVib} mm/s (Zone A Nominal).\n• **LATHE-01:** Operating nominal at 1,800 RPM.\n• **SAW-01:** Tool wear advisory active (420 cuts completed).\n• **COMP-01:** Line pressure 7.2 bar.\n\nOverall plant OEE is **88.4%** with zero emergency trips.`;
  }
  if (q.includes("heat") || q.includes("temp") || q.includes("garam")) {
    return `**Thermal Diagnostic (VMC-01):**\nProcess temperature is currently **${vmcTemp} °C**. Cooling coefficient $h_{cool} = 1.0$ (no coolant loss). Thermal margin to warning limit (70 °C) is **${(70 - vmcTemp).toFixed(1)} °C**. You can safely finish the current production batch.`;
  }
  if (q.includes("risk") || q.includes("maintenance") || q.includes("alert")) {
    return `**Active Maintenance Advisories:**\n1. **SAW-01 Blade Wear:** Reaches wear limit in approx. 80 cuts. Scheduled replacement at shift conclusion recommended.\n2. **COMP-01 Intake Filter:** Intake differential indicates dust accumulation. Routine clean scheduled for weekend.`;
  }
  return `**Twin Intelligence Response:**\nI am monitoring all 4 shop floor machines. Current line throughput is **42 parts/hr** with estimated downtime loss at **₹0/hr**. What specific machine or telemetry signal would you like to inspect?`;
}

function appendChatMessage(role, content, isThinking = false, tools = [], proposals = []) {
  const chatLog = document.getElementById("aiChatLog");
  if (!chatLog) return null;

  const msgDiv = document.createElement("div");
  msgDiv.className = `ai-chat-bubble ${role} ${isThinking ? 'thinking' : ''}`;

  let toolsBadge = "";
  if (tools.length > 0) {
    toolsBadge = `<div style="font-size:10px; color:var(--text-muted); margin-top:6px; font-family:var(--font-mono);">Grounding: [${tools.join(", ")}]</div>`;
  }

  let proposalsHtml = "";
  if (proposals.length > 0) {
    proposalsHtml = proposals.map(p => `
      <div class="chat-proposal-box">
        <div class="chat-proposal-title">⚡ Proposed Autonomous Action</div>
        <div class="chat-proposal-desc">${p.description || "Derate machine feed by 20% to prevent thermal trip"}</div>
        <div class="chat-proposal-btns">
          <button class="op-btn primary" onclick="approveChatProposal('${p.proposal_id}', this)">Approve Action</button>
          <button class="op-btn" onclick="dismissChatProposal('${p.proposal_id}', this)">Dismiss</button>
        </div>
      </div>
    `).join("");
  }

  msgDiv.innerHTML = `
    <div class="ai-msg-role">${role === "user" ? "You" : "UDYAM Advisor"}</div>
    <div class="ai-msg-text">${content}</div>
    ${toolsBadge}
    ${proposalsHtml}
  `;

  chatLog.appendChild(msgDiv);
  chatLog.scrollTop = chatLog.scrollHeight;
  return msgDiv;
}

async function approveChatProposal(proposalId, btn) {
  if (!window.UdyamAPI) return;
  try {
    await window.UdyamAPI.approveProposal(proposalId);
    if (btn) {
      const box = btn.closest(".chat-proposal-box");
      if (box) box.innerHTML = `<span style="font-size:12px; font-weight:700; color:var(--status-normal-text);">✓ Action Approved & Executed in Twin Simulator</span>`;
    }
    showToast("Proposal approved and executed.");
  } catch (err) {
    showToast(`Approval failed: ${err.message || err}`);
  }
}

async function dismissChatProposal(proposalId, btn) {
  if (btn) {
    const box = btn.closest(".chat-proposal-box");
    if (box) box.innerHTML = `<span style="font-size:12px; color:var(--text-muted);">Action Dismissed</span>`;
  }
}

// -----------------------------------------------------------------------------
// 14. Toast Notification System
// -----------------------------------------------------------------------------
let toastTimer = null;

function showToast(message) {
  const toast = document.getElementById("appleToast");
  if (!toast) return;

  toast.textContent = message;
  toast.classList.add("show");

  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.remove("show");
  }, 3200);
}

// -----------------------------------------------------------------------------
// 15. Real-Time Telemetry Event Bus Integration
// -----------------------------------------------------------------------------
function setupApiEventListeners() {
  if (!window.UdyamAPI) return;

  // Backend connection status
  window.UdyamAPI.on("status", (s) => {
    const pill = document.getElementById("opStatusPill");
    if (pill) {
      if (s.connected) {
        pill.className = "op-status-pill online";
        pill.textContent = "● Connected";
      } else {
        pill.className = "op-status-pill simulated";
        pill.textContent = "◌ Not connected";
      }
    }
  });
  // the WebSocket may already have connected before this listener was registered
  if (window.UdyamAPI.wsConnected) {
    window.UdyamAPI.emit("status", { connected: true, live: true, endpoint: `${window.UdyamAPI.wsUrl}/ws/telemetry` });
  }

  // Telemetry Snapshot Broadcast
  window.UdyamAPI.on("snapshot", (snap) => {
    const mid = snap.machine_id;
    LIVE_SNAPSHOTS[mid] = snap;

    // Update Hero HUD for VMC-01
    if (mid === "VMC-01") {
      const rpmEl = document.getElementById("hudRpm");
      const tempEl = document.getElementById("hudTemp");
      if (rpmEl && snap.rpm != null) rpmEl.textContent = `${snap.rpm.toLocaleString()} RPM`;
      if (tempEl && snap.temp_c != null) tempEl.textContent = `${snap.temp_c} °C (Zone ${snap.severity.zone})`;
    }

    // Update Machines section card
    updateMachineCardReadings(mid, snap);

    // Update 3D inspector if focused
    if (mid === CURRENT_MACHINE_ID || CURRENT_MACHINE_ID === "FACTORY" || !CURRENT_MACHINE_ID) {
      updateMachineInspectorHUD(CURRENT_MACHINE_ID || "FACTORY");
    }
  });

  // Line Metrics Broadcast
  window.UdyamAPI.on("line", (line) => {
    const lineLossEl = document.getElementById("kpiLineLoss");
    if (lineLossEl) lineLossEl.textContent = `₹${line.line_loss_inr_per_hr}/hr`;

    const throughputEl = document.getElementById("kpiThroughput");
    if (throughputEl) throughputEl.textContent = `${line.throughput_parts_per_hr} parts/hr`;
  });
}

// The four live readings shown for a machine, from its snapshot: [label, value] pairs. Used by the Dashboard cards
// (whose labels in index.html follow this order) and the Factory section's plant-line panel.
function machineReadings(machineId, snap) {
  const sig = snap.signals || {};
  const fmt = (v, d = 1) => (v == null ? "—" : (+v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const vib = ["Vibration", `${fmt(snap.vibration_rms_mm_s, 2)} mm/s`], temp = ["Process Temp", `${fmt(snap.temp_c)} °C`];
  const rpm = label => [label, `${fmt(snap.rpm, 0)} RPM${snap.derate_pct ? ` (−${snap.derate_pct}%)` : ""}`];
  const wear = ["Tool Wear", `${fmt(sig.tool_wear_min, 0)} min`];
  return {
    "SAW-01": [rpm("Blade Speed"), ["Blade Wear", `${fmt(sig.blade_wear_pct, 0)}%`], ["Power", `${fmt(snap.power_kw, 2)} kW`], vib],
    "LATHE-01": [rpm("Spindle Speed"), temp, vib, wear],
    "VMC-01": [rpm("Spindle Speed"), temp, vib, wear],
    "COMP-01": [["Header Pressure", `${fmt(sig.pressure_bar, 2)} bar`], ["Compressor Duty", `${fmt(sig.duty_pct, 0)}%`],
      ["Discharge Temp", `${fmt(sig.discharge_temp_c)} °C`], vib],
  }[machineId] || [];
}

function updateMachineCardReadings(machineId, snap) {
  const zone = (snap.severity || {}).zone || "A";
  const ok = (zone === "A" || zone === "B") && !snap.active_scenario;
  const sig = snap.signals || {};
  const fmt = (v, d = 1) => (v == null ? "—" : (+v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const META = { "SAW-01": `Wear ${fmt(sig.blade_wear_pct, 0)}%`, "LATHE-01": `${fmt(snap.rpm, 0)} RPM`,
    "VMC-01": `${fmt(snap.rpm, 0)} RPM`, "COMP-01": `${fmt(sig.pressure_bar, 1)} bar` };
  const card = document.getElementById(`card-${machineId}`);
  if (card) {
    const pill = card.querySelector(".status-indicator-pill");
    if (pill && snap.severity) {  // the backend's status is PROVISIONAL; the vibration zone says how the machine is
      pill.textContent = `${ok ? "✓" : "⚠"} Zone ${zone}${snap.active_scenario ? " · fault" : ""}`;
      pill.className = `status-indicator-pill ${ok ? "normal" : zone === "D" ? "problem" : "attention"}`;
    }
    const vals = card.querySelectorAll(".reading-val");
    machineReadings(machineId, snap).forEach(([, v], k) => { if (vals[k]) vals[k].textContent = v; });
  }
  document.querySelectorAll(`.quick-station-pill[onclick*="'${machineId}'"]`).forEach(p => {
    const meta = p.querySelector(".station-meta"), dot = p.querySelector(".station-dot");
    if (meta && META[machineId]) meta.textContent = `${META[machineId]} · Focus 3D →`;
    if (dot) dot.className = `station-dot ${ok ? "normal" : "attention"}`;
  });
}

// -----------------------------------------------------------------------------
// 16. Navigation, FAQ, and Lifecycle Bootstrap
// -----------------------------------------------------------------------------
function initFaqAccordion() {
  document.querySelectorAll(".faq-question-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const item = btn.closest(".faq-item");
      const isActive = item.classList.contains("active");

      document.querySelectorAll(".faq-item").forEach(other => {
        other.classList.remove("active");
        other.querySelector(".faq-question-btn")?.setAttribute("aria-expanded", "false");
      });

      if (!isActive) {
        item.classList.add("active");
        btn.setAttribute("aria-expanded", "true");
      }
    });
  });
}

function toggleMobileMenu() {
  const drawer = document.getElementById("mobileNavDrawer");
  if (drawer) drawer.classList.toggle("open");
}

function closeMobileMenu() {
  const drawer = document.getElementById("mobileNavDrawer");
  if (drawer) drawer.classList.remove("open");
}

function initScrollspy() {
  // page sections only (not the panels inside them, which are <section class="panel">); hidden ones are skipped
  const sections = document.querySelectorAll("section.section[id], section.hero-section[id]");
  const nav = document.querySelector(".site-header .nav-links");
  const navLinks = document.querySelectorAll(".site-header .nav-link, .mobile-drawer-links .nav-link");
  let last = "";

  const update = () => {
    let current = "hero";
    sections.forEach(sec => {
      if (!sec.offsetParent && getComputedStyle(sec).display === "none") return;
      if (sec.getBoundingClientRect().top <= 140) current = sec.id;
    });
    if (current === last) return;
    last = current;
    navLinks.forEach(link => link.classList.toggle("active", link.getAttribute("href") === `#${current}`));
    const active = nav && nav.querySelector(".nav-link.active");
    if (active && nav.scrollWidth > nav.clientWidth) {  // keep the active link visible when the bar scrolls sideways
      const l = active.offsetLeft - nav.offsetLeft, r = l + active.offsetWidth;
      if (l < nav.scrollLeft || r > nav.scrollLeft + nav.clientWidth) nav.scrollTo({ left: l - 24, behavior: "smooth" });
    }
  };
  window.addEventListener("scroll", update, { passive: true });
  window.addEventListener("hashchange", update);
  update();
}

window.addEventListener("DOMContentLoaded", () => {
  initOpeningLoader();
  initCursorReactiveGrid();
  initCardSpotlights();
  initScrollReveal();
  initFaqAccordion();
  initScrollspy();
  setupApiEventListeners();
  refreshLiveFigures();
  renderAlerts();
  setInterval(() => { if (document.visibilityState === "visible") renderAlerts(); }, 10000);
  if (window.UdyamAPI) window.UdyamAPI.on("alert", () => setTimeout(renderAlerts, 300));

  // Intercept anchor navigation for #digital-twin to trigger 3D simulation mode
  document.querySelectorAll('a[href="#digital-twin"]').forEach(link => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      setFactoryMode("simulation");
      const factorySec = (document.getElementById("factorySimulator") || document.getElementById("factory"));
      if (factorySec) factorySec.scrollIntoView({ behavior: "smooth" });
    });
  });

  if (window.location.hash === "#digital-twin") {
    setTimeout(() => {
      setFactoryMode("simulation");
    }, 120);
  }

  // Esc key closes modals
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeMachineModal();
      closeAiAssistant();
      closeWorkOrdersModal();
      closeFaultLabModal();
      closeAddMachineModal();
      closeVibrationModal();
    }
  });
});

async function triggerToolChange(machineId = "VMC-01") {
  if (window.UdyamAPI) {
    try {
      await window.UdyamAPI.recordToolChange(machineId, 0);
      showToast(`Tool replaced on ${machineId}. Wear counter reset to 0 min.`);
      closeMachineModal();
    } catch (err) {
      showToast(`Tool change failed: ${err.message || err}`);
    }
  }
}

// Global exports for HTML onclick
window.dismissLoader = dismissLoader;
window.replayIntro = replayIntro;
window.setFactoryMode = setFactoryMode;
window.selectMachineIn3D = selectMachineIn3D;
window.updateMachineInspectorHUD = updateMachineInspectorHUD;
window.select3DTarget = select3DTarget;
window.openMachineModal = openMachineModal;
window.closeMachineModal = closeMachineModal;
window.filterAlerts = filterAlerts;
window.acknowledgeAlert = acknowledgeAlert;
window.resolveAlert = resolveAlert;
window.generateWorkOrderFromAlert = generateWorkOrderFromAlert;
window.acknowledgeAllAlerts = acknowledgeAllAlerts;
window.openWorkOrdersModal = openWorkOrdersModal;
window.closeWorkOrdersModal = closeWorkOrdersModal;
window.advanceWorkOrder = advanceWorkOrder;
window.handleCreateWorkOrderSubmit = handleCreateWorkOrderSubmit;
window.openFaultLabModal = openFaultLabModal;
window.closeFaultLabModal = closeFaultLabModal;
window.triggerFault = triggerFault;
window.triggerClearFaults = triggerClearFaults;
window.triggerScenario = triggerScenario;
window.triggerSimulateResolution = triggerSimulateResolution;
window.triggerResetBaseline = triggerResetBaseline;
window.toggleAutoDemo = toggleAutoDemo;
window.openAddMachineModal = openAddMachineModal;
window.closeAddMachineModal = closeAddMachineModal;
window.handleAddMachineSubmit = handleAddMachineSubmit;
window.openVibrationModal = openVibrationModal;
window.closeVibrationModal = closeVibrationModal;
window.triggerWaveformCapture = triggerWaveformCapture;
window.triggerProposeRLAction = triggerProposeRLAction;
window.triggerTrainCandidateModel = triggerTrainCandidateModel;
window.triggerSetAdaptiveBaseline = triggerSetAdaptiveBaseline;
window.openAiAssistant = openAiAssistant;
window.closeAiAssistant = closeAiAssistant;
window.handleAiInputSubmit = handleAiInputSubmit;
window.approveChatProposal = approveChatProposal;
window.dismissChatProposal = dismissChatProposal;
window.toggleMobileMenu = toggleMobileMenu;
window.closeMobileMenu = closeMobileMenu;
window.handleExploreFactory = handleExploreFactory;
window.showToast = showToast;
window.triggerToolChange = triggerToolChange;


// Figures that must come from the backend, never hard-coded: model test metrics, line output and availability,
// open work orders and the owner answers in #dashboard (computed from the live snapshots)
let MODEL_METRICS = null;
const pct1 = v => (v == null ? "—" : `${(100 * v).toFixed(1)}%`);
const setText = (id, t) => { const e = document.getElementById(id); if (e) e.textContent = t; };
const setNum = (id, value, unit) => { const e = document.getElementById(id); if (e) e.innerHTML = `${value}<span class="dash-metric-unit">${unit}</span>`; };
const setBar = (id, frac) => { const e = document.getElementById(id); if (e) e.style.width = `${Math.max(0, Math.min(100, 100 * frac))}%`; };

async function refreshLiveFigures() {
  if (!window.UdyamAPI) return;
  const A = window.UdyamAPI;
  if (!MODEL_METRICS) {
    try {
      MODEL_METRICS = await A.request("/api/metrics");
      const mf = (MODEL_METRICS.per_class || {}).machine_failure || {};
      const cm = mf.confusion_matrix || {};
      const ver = MODEL_METRICS.registry_version ? ` (model ${MODEL_METRICS.registry_version})` : "";
      setText("aiMetricsLine", `precision ${pct1(mf.precision)}, recall ${pct1(mf.recall)}, ROC-AUC ${mf.roc_auc ?? "—"}${ver}`);
      setText("bVal1", pct1(mf.precision)); setText("bSub1", `False-alarm rate ${pct1(mf.false_positive_rate)}`);
      setText("bVal2", pct1(mf.recall)); setText("bSub2", `Catches ${cm.true_positives ?? "—"} / ${mf.support_positive ?? "—"} test failures`);
      setText("bVal3", mf.f1 ?? "—"); setText("bSub3", "Precision–recall balance");
      setText("bVal4", mf.roc_auc ?? "—"); setText("bSub4", `PR-AUC ${mf.pr_auc ?? "—"}`);
    } catch (_) { /* metrics stay "—" while the backend is unreachable */ }
  }
  try {
    const [an, orders] = await Promise.all([A.request("/api/analytics"), A.getWorkOrders()]);
    const ms = Object.values(an.machines || {}).filter(m => (m.oee || {}).availability != null);  // machines with OEE (not the compressor)
    const avail = ms.length ? ms.reduce((s, m) => s + m.oee.availability, 0) / ms.length : null;
    const since = an.since ? new Date(an.since).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—";
    const lineParts = ((an.machines["VMC-01"] || {}).oee || {}).parts ?? 0;  // last station of the line (sim/line.py)
    setText("heroAvail", avail == null ? "—" : `${pct1(avail)} availability`);
    setNum("dashNum1", lineParts.toLocaleString(), "parts");
    setText("dashSub1", `Line output since ${since} (simulated session)`);
    setBar("dashBar1", ms.length ? ms.reduce((s, m) => s + ((m.oee || {}).oee || 0), 0) / ms.length : 0);
    const downMin = ms.reduce((s, m) => s + Math.max(0, ((m.oee || {}).window_s || 0) - ((m.oee || {}).running_s || 0)), 0) / 60;
    setNum("dashNum3", downMin.toFixed(0), "mins");
    setText("dashSub3", `Machine-minutes not running since ${since}, ${ms.length} production machines`);
    setBar("dashBar3", ms.length && an.elapsed_s ? downMin * 60 / (ms.length * an.elapsed_s) : 0);
    const open = orders.filter(o => o.status !== "Closed");
    setNum("dashNum4", open.length, open.length === 1 ? "task" : "tasks");
    setText("dashSub4", open.length ? `Oldest open: ${open[open.length - 1].wo_id} · ${open[open.length - 1].title || ""}`.slice(0, 90) : "No open work orders.");
    setBar("dashBar4", Math.min(1, open.length / 10));
    setText("woCountBadge", open.length);
    setText("qaFoot1", avail == null ? "—" : `Overall availability: ${pct1(avail)}`);
  } catch (_) { /* the status pill already shows a backend problem */ }
  updateOwnerAnswers();
}

function updateOwnerAnswers() {
  const snaps = Object.values(LIVE_SNAPSHOTS || {});
  if (!snaps.length) return;
  const zone = s => (s.severity || {}).zone || "A";
  const ok = snaps.filter(s => ["A", "B"].includes(zone(s)) && !s.active_scenario && s.state !== "TRIP");
  const running = snaps.filter(s => ["RUN", "DERATE"].includes(s.state));
  setNum("dashNum2", `${running.length} of ${snaps.length}`, "running");
  setText("dashSub2", snaps.map(s => `${s.machine_id} ${s.state}${s.derate_pct ? ` −${s.derate_pct}%` : ""}`).join(" · "));
  setBar("dashBar2", running.length / snaps.length);
  const a1 = document.getElementById("qaAns1");
  if (a1) {
    const good = ok.length === snaps.length;
    a1.className = `qa-answer ${good ? "status-good" : "status-warn"}`;
    a1.textContent = good ? "✓ Yes, every machine is in a safe zone." : `⚠ ${snaps.length - ok.length} of ${snaps.length} machines need a look.`;
  }
  setText("qaDetail1", `${ok.length} of ${snaps.length} machines are in vibration zone A/B with no active fault. ${running.length} are running.`);
  // the machine to look at first: worst vibration zone, then a running scenario, then derate
  const rank = s => ({ D: 40, C: 30, B: 10, A: 0 }[zone(s)] || 0) + (s.active_scenario ? 20 : 0) + (s.derate_pct ? 15 : 0) + (s.state === "TRIP" ? 50 : 0);
  const worst = [...snaps].sort((x, y) => rank(y) - rank(x))[0];
  const a2 = document.getElementById("qaAns2");
  if (a2) {
    const fine = rank(worst) <= 10;
    a2.className = `qa-answer ${fine ? "status-good" : "status-warn"}`;
    a2.textContent = fine ? "✓ None right now." : `⚠ ${worst.name || worst.machine_id}.`;
  }
  setText("qaDetail2", rank(worst) <= 10 ? "No machine is above zone B, faulted or derated." :
    `Zone ${zone(worst)}${worst.active_scenario ? `, ${String(worst.active_scenario).replace(/_/g, " ")}` : ""}${worst.derate_pct ? `, derated ${worst.derate_pct}%` : ""}; state ${worst.state}.`);
  setText("qaFoot2", worst.vibration_rms_mm_s != null ? `${worst.machine_id} vibration ${worst.vibration_rms_mm_s} mm/s` : "—");
  const cncs = snaps.filter(s => ["cnc_mill", "cnc_lathe"].includes(s.template));
  const cncOk = cncs.every(s => ["A", "B"].includes(zone(s)));
  const a3 = document.getElementById("qaAns3");
  if (a3) {
    a3.className = `qa-answer ${cncOk ? "status-good" : "status-warn"}`;
    a3.textContent = cncOk ? "✓ Cutting conditions are stable." : "⚠ Cutting conditions are disturbed.";
  }
  setText("qaDetail3", cncs.map(s => `${s.machine_id}: ${s.temp_c ?? "—"} °C, ${s.vibration_rms_mm_s ?? "—"} mm/s (zone ${zone(s)})`).join(" · "));
  setText("qaFoot3", "Dimensional accuracy is not measured (no gauge data); this is inferred from temperature and vibration.");
}
setInterval(() => { if (document.visibilityState === "visible") refreshLiveFigures(); }, 5000);
