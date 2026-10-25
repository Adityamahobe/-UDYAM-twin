// Operating mode switch on the existing header badge: TESTING (simulation) ↔ REAL DATA (live telemetry).
// Reads/sets GET/POST /api/operating-mode. In REAL DATA mode:
//  - simulator controls (.testing-only) are disabled (the server refuses them too);
//  - machines without a real device show "no real data" instead of simulated values;
//  - a banner shows the connection state (Connected / Connecting / Disconnected / Error / Stale data),
//    last message time and devices. MQTT credentials never reach the browser.
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const STATE = {
    CONNECTED: ["Connected", "ok"], CONNECTING: ["Connecting", "warn"], DISCONNECTED: ["Disconnected", "err"],
    ERROR: ["Error", "err"], STALE_DATA: ["Stale data", "warn"], NOT_CONFIGURED: ["Not configured", "err"],
  };
  // Shared with app.js (event log / alert filters); set before the first poll so the badge is ours.
  const mode = window.udyamMode = { mode: "TESTING", liveMachines: new Set(), connection: null };
  let switching = false;

  function ago(iso) {
    if (!iso) return "never";
    const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    return s < 60 ? `${s} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
  }

  function apply(s) {
    const real = s.mode === "REAL_DATA";
    const changed = real !== (mode.mode === "REAL_DATA");
    const live = new Set(s.connection.live_machines || []);
    const liveChanged = [...live].join() !== [...mode.liveMachines].join();
    mode.mode = s.mode; mode.liveMachines = live; mode.connection = s.connection;
    document.body.classList.toggle("real-mode", real);

    const [label, tone] = STATE[s.connection.state] || [s.connection.state, "warn"];
    const badge = el("globalModeBadge");
    badge.textContent = real ? `REAL DATA MODE · ${label}` : "TESTING MODE · SIMULATION";
    badge.className = `badge-sim mode-switch${real ? ` real tone-${tone}` : ""}`;
    badge.title = real ? `${s.connection.message}. Click to switch to Testing mode.`
      : "Simulation (software in the loop). Click to switch to Real Data mode.";

    const banner = el("realBanner");
    banner.hidden = !real;
    if (real) {
      const c = s.connection, b = c.broker, d = c.devices;
      banner.className = `real-banner tone-${tone}`;
      banner.innerHTML = `
        <div class="rb-main"><span class="rb-state">${esc(label)}</span><span>${esc(c.message)}</span></div>
        <div class="rb-facts">
          <span>MQTT: <b>${esc(b.state.replace("_", " ").toLowerCase())}</b>${b.address ? ` · ${esc(b.address)}` : ""}${b.topic ? ` · ${esc(b.topic)}` : ""}</span>
          <span>Real devices: <b>${d.online}</b> online / ${d.real}${d.stale_or_offline ? ` · <b class="rb-warn">${d.stale_or_offline} stale</b>` : ""}</span>
          <span>Last message: <b>${esc(ago(c.last_message_at))}</b></span>
          <span>Live machines: <b>${live.size ? [...live].map(esc).join(", ") : "none"}</b></span>
          ${d.simulated_stand_ins ? `<span>${d.simulated_stand_ins} simulated test device(s) not counted</span>` : ""}
        </div>
        <div class="rb-note">Read-only monitoring: simulator controls are off and simulated readings are hidden. UDYAM-TWIN sends no commands to machines.</div>`;
    }
    // machines without a real device: values hidden in REAL DATA mode
    document.querySelectorAll(".machine-card").forEach(card => {
      const mid = card.id.replace(/^card-/, "");
      card.classList.toggle("no-real-data", real && !live.has(mid));
    });
    const sel = typeof state !== "undefined" ? state.selectedMachineId : null;
    ["detailView", "vibrationPanel"].forEach(id => {  // panels about the selected machine
      const p = el(id);
      if (p) p.classList.toggle("no-real-data", real && !!sel && !live.has(sel));
    });
    if ((changed || liveChanged) && typeof renderEventLog === "function") { renderEventLog(); renderAlerts(); }
  }

  function poll() {
    fetch("/api/operating-mode").then(r => r.json()).then(apply).catch(() => {});
  }

  function toggle() {
    if (switching) return;
    const toReal = mode.mode !== "REAL_DATA";
    const msg = toReal
      ? "Switch to REAL DATA mode?\n\nRead-only monitoring of real devices. Simulator controls are disabled, simulated readings are hidden, and the background simulation is reset to a clean baseline."
      : "Switch to TESTING mode?\n\nThe dashboard shows the simulation again and all simulator controls are enabled. Real devices keep being recorded.";
    if (!window.confirm(msg)) return;
    switching = true;
    fetch("/api/operating-mode", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: toReal ? "REAL_DATA" : "TESTING" }) })
      .then(r => r.json()).then(apply).catch(() => {}).finally(() => { switching = false; });
  }

  function start() {
    const badge = el("globalModeBadge");
    if (!badge) return;
    badge.addEventListener("click", toggle);
    poll();
    setInterval(poll, 2000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
