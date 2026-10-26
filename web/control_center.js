// Control Center (index.html #control-center): tabs for the existing feature panels, and the operating mode.
// The panels' own scripts (sim_controls.js, factory_*.js, models.js, …) fetch their data from the backend.
// body.real-mode follows GET /api/operating-mode, so Testing-only panels lock and the Wokwi panel shows in Real Data
// mode exactly as in the previous dashboard (web/legacy).
(function () {
  const root = () => document.getElementById("controlCenter");

  function show(key) {
    const cc = root();
    if (!cc) return;
    cc.querySelectorAll(".cc-tab").forEach(b => b.setAttribute("aria-selected", String(b.dataset.ccTab === key)));
    cc.querySelectorAll(".cc-pane").forEach(p => { p.hidden = p.dataset.ccPane !== key; });
    try { localStorage.setItem("udyam.cc.tab", key); } catch (_) { /* storage blocked */ }
    window.dispatchEvent(new Event("resize"));  // panels that size charts on show
  }

  let currentMode = null, switching = false;
  function applyMode(m) {
    currentMode = m.mode;
    const real = m.mode === "REAL_DATA";
    document.body.classList.toggle("real-mode", real);
    document.body.classList.toggle("testing-mode", !real);
    const v = m.virtual || {}, c = m.connection || {};
    // no real machine set up yet (future scope): Real Data mode runs on the Wokwi virtual sensors, so lead with them
    const realSetUp = c.state && (c.state !== "NOT_CONFIGURED" || ((c.devices || {}).real || 0) > 0);
    const virtualMode = real && !realSetUp && v.state && v.state !== "NOT_CONFIGURED";
    renderBanner(real, virtualMode, v, c);
    const pill = document.getElementById("opModePill");  // the control bar's mode pill shows and switches the mode
    if (pill) {
      pill.innerHTML = `<span>MODE:</span> <b>${!real ? "TESTING · simulation" : virtualMode ? "REAL DATA · WOKWI (VIRTUAL)" : "REAL DATA · read-only"}</b>`;
      pill.title = real ? "Switch to Testing mode (simulation)" : "Switch to Real Data mode (live telemetry, read-only)";
      pill.style.cursor = "pointer";
    }
  }

  const esc = t => String(t ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const ago = ts => {
    if (!ts) return "never";
    const s = Math.max(0, (Date.now() - Date.parse(ts)) / 1000);
    return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
  };

  function renderBanner(real, virtualMode, v, c) {  // the Real Data banner of the previous dashboard (web/mode.js)
    const banner = document.getElementById("rdBanner");
    if (!banner || !real) return;
    const vb = v.broker || {};
    const lastV = (v.devices || []).map(x => x.last_message_at).filter(Boolean).sort().pop();
    const future = `<div class="rb-future"><b>Future scope · real machines:</b> connect shop-floor sensors (ESP32 / PLC) over MQTT by
      setting UDYAM_MQTT_HOST; their readings then appear here next to the virtual ones. Not set up yet.</div>`;
    if (virtualMode) {
      banner.className = "real-banner tone-virtual";
      banner.innerHTML = `
        <div class="rb-main"><span class="rb-state">Wokwi virtual sensors · ${esc((v.state || "").replace("_", " ").toLowerCase())}</span><span>${esc(v.message || "")}</span></div>
        <div class="rb-facts">
          <span>MQTT <b>${esc((vb.state || "—").replace("_", " ").toLowerCase())}</b>${vb.broker ? ` · ${esc(vb.broker)}` : ""}</span>
          ${(v.online_machines || []).length ? `<span>Online <b>${v.online_machines.map(esc).join(", ")}</b></span>` : ""}
          <span>Last message <b>${esc(ago(lastV))}</b></span>
          <span class="rb-ro">Virtual data, not a real machine · read-only · sends no commands</span>
        </div>${future}`;
    } else {
      banner.className = "real-banner tone-warn";
      banner.innerHTML = `
        <div class="rb-main"><span class="rb-state">${esc((c.state || "—").replace("_", " ").toLowerCase())}</span><span>${esc(c.message || "")}</span></div>
        <div class="rb-facts"><span>No data source: connect the Wokwi virtual device (UDYAM_WOKWI_BROKER) or a real machine.</span>
          <span class="rb-ro">Read-only · sends no commands to machines</span></div>${future}`;
    }
  }

  function syncMode() {
    fetch("/api/operating-mode").then(r => r.json()).then(applyMode).catch(() => {});
  }

  function switchMode() {  // same flow as the previous dashboard (web/mode.js): confirm, then POST /api/operating-mode
    if (switching || !currentMode) return;
    const toReal = currentMode !== "REAL_DATA";
    const msg = toReal
      ? "Switch to Real Data mode?\n\nThe dashboard shows live telemetry only (Wokwi virtual devices; no real machine is connected yet). Simulator controls are locked and any running simulation is stopped."
      : "Switch to Testing mode?\n\nThe dashboard shows the simulation again. Real telemetry keeps being recorded.";
    switching = true;
    (window.udyamConfirm ? udyamConfirm(msg, { ok: toReal ? "Switch to Real Data" : "Switch to Testing" }) : Promise.resolve(confirm(msg))).then(ok => {
      if (!ok) { switching = false; return; }
      fetch("/api/operating-mode", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: toReal ? "REAL_DATA" : "TESTING" }) })
        .then(r => r.json()).then(m => {
          applyMode(m);
          if (m.mode === "REAL_DATA") document.getElementById("real-data")?.scrollIntoView({ behavior: "smooth" });
        }).catch(() => {}).finally(() => { switching = false; });
    });
  }

  function start() {
    const cc = root();
    if (!cc) return;
    cc.querySelectorAll(".cc-tab").forEach(b => { b.onclick = () => show(b.dataset.ccTab); });
    let saved = null;
    try { saved = localStorage.getItem("udyam.cc.tab"); } catch (_) { /* storage blocked */ }
    if (saved && cc.querySelector(`[data-cc-tab="${saved}"]`)) show(saved);
    const pill = document.getElementById("opModePill");
    if (pill) pill.addEventListener("click", switchMode);
    syncMode();
    setInterval(syncMode, 5000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
