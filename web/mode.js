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
    const v = s.virtual || {};
    // Wokwi devices are virtual: they never change the real-device state, only add their own line and badge text
    // With no real machine set up yet (future scope), Real Data mode runs on the Wokwi virtual sensors: lead with them.
    const realSetUp = s.connection.state !== "NOT_CONFIGURED" || (s.connection.devices || {}).real > 0;
    const virtualMode = !realSetUp && v.state && v.state !== "NOT_CONFIGURED";
    const [vLabel, vTone0] = STATE[v.state] || [v.state || "—", "warn"];
    const vTone = v.state === "CONNECTED" && (v.online_machines || []).length ? "virtual" : vTone0;
    const badge = el("globalModeBadge");
    badge.textContent = !real ? "TESTING MODE · SIMULATION"
      : virtualMode ? `REAL DATA MODE · WOKWI (VIRTUAL)${vTone === "virtual" ? "" : ` · ${vLabel}`}` : `REAL DATA MODE · ${label}`;
    badge.className = `badge-sim mode-switch${real ? ` real tone-${virtualMode ? vTone : tone}` : ""}`;
    badge.title = real ? `${virtualMode ? v.message : s.connection.message}. Click to switch to Testing mode.`
      : "Simulation (software in the loop). Click to switch to Real Data mode.";

    const banner = el("realBanner");
    banner.hidden = !real;
    if (real) {
      const c = s.connection, b = c.broker, d = c.devices;
      banner.className = `real-banner tone-${tone}`;
      const vb = v.broker || {};
      const lastV = (v.devices || []).map(x => x.last_message_at).filter(Boolean).sort().pop();
      const future = `<div class="rb-future"><b>Future scope · real machines:</b> connect shop-floor sensors (ESP32 / PLC) over MQTT by
        setting UDYAM_MQTT_HOST; their readings then appear here next to the virtual ones. Not set up yet.</div>`;
      if (virtualMode) {  // Wokwi is the data source for now
        banner.className = `real-banner tone-${vTone}`;
        banner.innerHTML = `
          <div class="rb-main"><span class="rb-state">Wokwi virtual sensors · ${esc(vLabel)}</span><span>${esc(v.message || "")}</span></div>
          <div class="rb-facts">
            <span>MQTT <b>${esc((vb.state || "—").replace("_", " ").toLowerCase())}</b>${vb.broker ? ` · ${esc(vb.broker)}` : ""}</span>
            ${(v.online_machines || []).length ? `<span>Online <b>${v.online_machines.map(esc).join(", ")}</b></span>` : ""}
            <span>Last message <b>${esc(ago(lastV))}</b></span>
            <span class="rb-ro">Virtual data, not a real machine · read-only · sends no commands</span>
          </div>${future}`;
      } else {
        banner.innerHTML = `
          <div class="rb-main"><span class="rb-state">${esc(label)}</span><span>${esc(c.message)}</span></div>
          <div class="rb-facts">
            ${realSetUp ? `<span>MQTT <b>${esc(b.state.replace("_", " ").toLowerCase())}</b>${b.address ? ` · ${esc(b.address)}` : ""}</span>
            <span>Devices <b>${d.online}</b>/${d.real} online${d.stale_or_offline ? ` · <b class="rb-warn">${d.stale_or_offline} stale</b>` : ""}</span>
            <span>Last message <b>${esc(ago(c.last_message_at))}</b></span>` : `<span>No data source: connect the Wokwi virtual device (UDYAM_WOKWI_BROKER) or a real machine.</span>`}
            ${v.state && v.state !== "NOT_CONFIGURED" ? `<span class="rb-virtual">Wokwi (virtual): <b>${esc(vLabel)}</b>${
              (v.online_machines || []).length ? ` · ${v.online_machines.map(esc).join(", ")}` : ""}</span>` : ""}
            <span class="rb-ro">Read-only · sends no commands to machines</span>
          </div>`;
      }
    }
    // machines without a real device: values hidden in REAL DATA mode
    document.querySelectorAll(".machine-card").forEach(card => {
      const mid = card.id.replace(/^card-/, "");
      card.classList.toggle("no-real-data", real && !live.has(mid));
    });
    let sel = typeof state !== "undefined" ? state.selectedMachineId : null;
    if (real && live.size && !live.has(sel) && typeof selectMachine === "function") {  // show a machine that has data
      sel = [...live][0];
      selectMachine(sel);
    }
    ["detailView", "vibrationPanel"].forEach(id => {  // panels about the selected machine
      const p = el(id);
      if (p) p.classList.toggle("no-real-data", real && !!sel && !live.has(sel));
    });
    // in Real Data mode, parts with nothing to show are hidden rather than shown as empty boxes
    const grid = el("machineGrid");
    if (grid) grid.classList.toggle("rd-empty", real && !live.size);
    document.querySelectorAll(".lower-layout").forEach(ll => {
      const left = ll.querySelector(".ws-col");
      const empty = real && left && [...left.children].every(ch => ch.classList.contains("no-real-data"));
      ll.classList.toggle("rd-solo", !!empty);
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
    switching = true;
    udyamConfirm(msg, { ok: toReal ? "Switch to Real Data" : "Switch to Testing" }).then(ok => {
      if (!ok) { switching = false; return; }
      fetch("/api/operating-mode", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: toReal ? "REAL_DATA" : "TESTING" }) })
        .then(r => r.json()).then(apply).catch(() => {}).finally(() => { switching = false; });
    });
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
