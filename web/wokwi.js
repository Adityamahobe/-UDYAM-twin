// Virtual Devices panel (Real Data mode only): telemetry from ESP32 sketches running in the Wokwi simulator,
// from GET /api/wokwi. VIRTUAL data (simulated sensors + software models): never shown as a real machine
// and never mixed into the simulated plant's cards.
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const STATE = { CONNECTED: ["Connected", "ok"], CONNECTING: ["Connecting", "warn"], DISCONNECTED: ["Disconnected", "err"],
    ERROR: ["Error", "err"], STALE_DATA: ["Stale data", "warn"], NOT_CONFIGURED: ["Not configured", "err"], NOT_STARTED: ["Starting", "warn"] };
  const DEV = { ONLINE: ["Online", "ok"], STALE: ["Stale", "warn"], OFFLINE: ["Offline", "err"] };
  const f = (v, d, u) => (v == null ? "—" : `${(+v).toFixed(d)}<small> ${u}</small>`);
  const dur = s => (s == null ? "" : s < 60 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);
  const ago = s => (s == null ? "" : `${dur(s)} ago`);

  function card(d) {
    const [label, tone] = DEV[d.state] || [d.state, "warn"];
    const l = d.last || {}, m = d.monitoring || {};
    const fault = l.simulated_fault ? `<span class="wk-fault">simulated fault: ${esc(l.simulated_fault.replace(/_/g, " "))}</span>` : "";
    const sources = Object.entries(l.value_sources || {}).map(([k, v]) => `${esc(k)}: ${esc(v)}`).join(" · ");
    // one row per device: identity on the left, the four readings side by side (horizontal) on the right
    return `<div class="wk-card ${d.state !== "ONLINE" ? "wk-dim" : ""}">
      <div class="wk-row">
        <div class="wk-id">
          <div class="card-title"><h3>${esc(d.machine_id)}</h3></div>
          <div class="machine-badges"><span class="pill wk-src">WOKWI SIMULATOR</span><span class="pill wk-virtual">VIRTUAL DATA</span>
            <span class="pill wk-${tone}">${esc(label)}</span></div>
          <div class="mc-foot">Scenario <b>${esc((l.scenario || "—").replace(/_/g, " "))}</b> ${fault}</div>
        </div>
        <div class="mc-metrics wk-metrics">
          <div class="mc-metric"><span>RPM</span><b>${f(l.rpm, 0, "rpm")}</b></div>
          <div class="mc-metric"><span>Temperature</span><b>${f(l.temp_c, 1, "°C")}</b></div>
          <div class="mc-metric"><span>Vibration</span><b>${f(l.vibration_rms_mm_s, 2, "mm/s")}</b></div>
          <div class="mc-metric"><span>Current</span><b>${f(l.current_a, 2, "A")}</b></div>
        </div>
      </div>
      <div class="mc-foot">Last message ${l.ts ? new Date(l.ts).toLocaleTimeString([], { hour12: false }) : "—"} (${esc(ago(d.age_s))})
        · #${esc(l.seq)} · ${esc(d.messages)} received · clock: ${esc(l.ts_source || "—")}</div>
      ${d.state === "OFFLINE" ? `<div class="mc-warn">No message for ${esc(dur(d.age_s))}${d.device_status === "offline" ? " (the device reported offline)" : ""}: values hidden.</div>`
        : d.state === "STALE" ? `<div class="mc-warn">No new message for ${esc(dur(d.age_s))}: these values are not current.</div>` : ""}
      <details class="wk-more"><summary>Monitoring and value sources</summary>
        <div>Vibration severity: <b>${esc(m.vibration_severity?.status)}</b> — ${esc(m.vibration_severity?.note)}</div>
        <div>Failure risk: <b>${esc(m.failure_risk?.status)}</b> — ${esc(m.failure_risk?.detail)}</div>
        <div>Labels: ${esc(m.labels)}</div>
        <div>Value sources: ${sources || "—"}</div>
      </details>
    </div>`;
  }

  // The Wokwi circuit (wokwi/udyam-virtual-machine/diagram.json) drawn with what each part simulates and its live value.
  // Blue = a Wokwi virtual part feeding the reading; amber = the reading is computed by a software model in the sketch.
  function circuit(d, s) {
    const l = (d && d.state !== "OFFLINE" && d.last) || {};
    const live = !!(d && d.state === "ONLINE");
    const v = (x, n, u) => (x == null ? "—" : `${(+x).toFixed(n)} ${u}`);
    const faultOn = live && l.scenario && l.scenario !== "normal";
    const VS = "#3B9BFF", SW = "#fbbf24", WIRE = "var(--text-muted)";
    const T = (x, y, txt, o = {}) => `<text x="${x}" y="${y}" font-size="${o.size || 11}" fill="${o.fill || "var(--text-secondary)"}"
      ${o.anchor ? `text-anchor="${o.anchor}"` : ""} ${o.bold ? 'font-weight="700"' : ""} ${o.mono ? 'font-family="var(--font-mono)"' : ""}>${esc(txt)}</text>`;
    const part = (x, y, name, role, color, value) =>
      T(x, y, name, { size: 12, bold: true, fill: "var(--text-primary)" }) + T(x, y + 15, role, { size: 10.5, fill: color }) +
      T(x, y + 31, value, { size: 12, bold: true, mono: true, fill: live ? "var(--text-primary)" : "var(--text-muted)" });
    const wire = (pts, label) => `<polyline points="${pts}" fill="none" stroke="${WIRE}" stroke-width="1.5"/>` + (label || "");
    const pin = (x, y, txt, side) => `<circle cx="${x}" cy="${y}" r="3" fill="var(--accent-cyan)"/>` +
      T(side === "l" ? x + 8 : x - 8, y + 4, txt, { size: 10, anchor: side === "l" ? "start" : "end", mono: true, fill: "var(--text-secondary)" });
    const pot = (cx, cy) => `<circle cx="${cx}" cy="${cy}" r="15" fill="var(--bg-card)" stroke="${VS}" stroke-width="2"/>
      <line x1="${cx}" y1="${cy}" x2="${cx + 9}" y2="${cy - 9}" stroke="${VS}" stroke-width="2.5" stroke-linecap="round"/>`;
    const b = s.broker || {};
    const net = `Wi-Fi “Wokwi-GUEST”  →  MQTT ${b.broker || "not configured"}  →  UDYAM-TWIN`;
    return `<div class="wk-circuit-wrap"><div class="wk-circuit-title">Virtual circuit (Wokwi simulator) · machine ${esc(d ? d.machine_id : "machine-001")}
      <span>${live ? "live values" : d ? "not live: last values hidden or stale" : "waiting for the device"}</span></div>
      <div class="wk-circuit-scroll"><svg class="wk-circuit" viewBox="0 0 760 372" role="img"
        aria-label="Wokwi ESP32 virtual circuit: DHT22 temperature, MPU6050 accelerometer, speed and load potentiometers, scenario button and fault LED">
        <!-- network path -->
        <polyline points="385,72 385,34" fill="none" stroke="${WIRE}" stroke-width="1.5" stroke-dasharray="3 3"/>
        <rect x="150" y="12" width="470" height="24" rx="12" fill="var(--bg-card)" stroke="var(--border-subtle)"/>
        ${T(385, 28, net, { anchor: "middle", size: 10.5, fill: live ? "var(--status-ok)" : "var(--text-muted)" })}
        <!-- ESP32 -->
        <rect x="300" y="72" width="170" height="248" rx="8" fill="var(--bg-card)" stroke="#64748b" stroke-width="1.5"/>
        <rect x="340" y="90" width="90" height="56" rx="4" fill="var(--bg-surface)" stroke="#475569"/>
        ${T(385, 116, "ESP32", { anchor: "middle", size: 16, bold: true, fill: "var(--text-primary)" })}
        ${T(385, 134, "DevKit-C · Wokwi", { anchor: "middle", size: 10 })}
        ${pin(300, 160, "GPIO34", "l")}${pin(300, 200, "GPIO35", "l")}${pin(300, 248, "GPIO4", "l")}${pin(300, 292, "GPIO26", "l")}
        ${pin(470, 160, "GPIO15", "r")}${pin(470, 240, "GPIO21 SDA", "r")}${pin(470, 264, "GPIO22 SCL", "r")}
        ${T(385, 312, "3V3 / GND to every part", { anchor: "middle", size: 9.5, fill: "var(--text-muted)" })}
        <!-- left: SPEED pot -> RPM -->
        ${wire("66,160 300,160")}${pot(50, 160)}${part(76, 128, "SPEED potentiometer", "virtual part → RPM setpoint", VS, v(l.rpm, 0, "rpm"))}
        <!-- LOAD pot -> current model -->
        ${wire("66,200 300,200")}${pot(50, 200)}
        ${T(76, 195, "LOAD potentiometer", { size: 12, bold: true, fill: "var(--text-primary)" })}
        ${T(76, 209, "software model → current", { size: 10.5, fill: SW })}
        ${T(76, 224, v(l.current_a, 2, "A"), { size: 12, bold: true, mono: true, fill: live ? "var(--text-primary)" : "var(--text-muted)" })}
        <!-- SCENARIO button -->
        ${wire("62,248 300,248")}
        <rect x="34" y="236" width="28" height="24" rx="4" fill="var(--bg-card)" stroke="var(--accent-blue)" stroke-width="2"/>
        <circle cx="48" cy="248" r="6" fill="var(--accent-blue)"/>
        ${T(76, 246, "SCENARIO button", { size: 12, bold: true, fill: "var(--text-primary)" })}
        ${T(76, 260, (l.scenario || "—").replace(/_/g, " ") + (l.simulated_fault ? " · simulated fault" : ""), { size: 11, bold: true, mono: true,
          fill: faultOn ? "var(--status-warn)" : live ? "var(--text-primary)" : "var(--text-muted)" })}
        <!-- FAULT LED + 220 ohm -->
        ${wire("49,292 300,292")}
        <rect x="62" y="286" width="26" height="12" rx="3" fill="#c2410c" stroke="#9a3412"/>
        <circle cx="40" cy="292" r="10" fill="${faultOn ? "#ef4444" : "#3f1d1d"}" stroke="#ef4444" stroke-width="1.5"/>
        ${faultOn ? '<circle cx="40" cy="292" r="16" fill="none" stroke="#ef4444" stroke-opacity=".35" stroke-width="4"/>' : ""}
        ${T(96, 290, "FAULT LED · 220 Ω", { size: 12, bold: true, fill: "var(--text-primary)" })}
        ${T(96, 304, live ? (faultOn ? "on: fault scenario active" : "off: normal")
          : l.scenario ? `not live (last known: ${l.scenario === "normal" ? "off" : "on"})` : "not live",
          { size: 10.5, fill: faultOn ? "var(--status-crit)" : "var(--text-muted)" })}
        <!-- right: DHT22 -> temperature -->
        ${wire("470,160 532,160")}
        <rect x="532" y="136" width="28" height="48" rx="4" fill="#e5e7eb" stroke="${VS}" stroke-width="2"/>
        ${[146, 154, 162, 170].map(y => `<line x1="536" y1="${y}" x2="556" y2="${y}" stroke="#9ca3af"/>`).join("")}
        ${part(570, 140, "DHT22", "virtual sensor → temperature", VS, v(l.temp_c, 1, "°C"))}
        <!-- MPU6050 -> vibration model -->
        ${wire("470,240 532,240")}${wire("470,264 532,264")}
        <rect x="532" y="226" width="34" height="50" rx="4" fill="#1d4ed8" stroke="${VS}" stroke-width="2"/>
        <rect x="542" y="242" width="14" height="14" fill="#111827"/>
        ${T(576, 226, "MPU6050", { size: 12, bold: true, fill: "var(--text-primary)" })}
        ${T(576, 241, "virtual accelerometer", { size: 10.5, fill: VS })}
        ${T(576, 255, "+ software model", { size: 10.5, fill: SW })}
        ${T(576, 269, "→ vibration", { size: 10.5, fill: SW })}
        ${T(576, 285, v(l.vibration_rms_mm_s, 2, "mm/s"), { size: 12, bold: true, mono: true, fill: live ? "var(--text-primary)" : "var(--text-muted)" })}
        <!-- legend -->
        <rect x="150" y="340" width="10" height="10" rx="2" fill="${VS}"/>${T(166, 349, "Wokwi virtual part", { size: 10.5 })}
        <rect x="290" y="340" width="10" height="10" rx="2" fill="${SW}"/>${T(306, 349, "value computed by a software model", { size: 10.5 })}
        ${T(520, 349, "Not a real machine: simulated sensors", { size: 10.5, fill: "var(--text-muted)" })}
      </svg></div></div>`;
  }

  // ---------------------------------------------------------------------------------------------------------
  // The Wokwi circuit drawn with Wokwi's own part graphics (@wokwi/elements, MIT, web/vendor/wokwi-elements) at the
  // positions and with the wires of wokwi/udyam-virtual-machine/diagram.json. Built once; live state updated in place.
  // Falls back to the plain schematic (circuit()) if the part library cannot load.
  // ---------------------------------------------------------------------------------------------------------
  const LIB = "/static/vendor/wokwi-elements/wokwi-elements.bundle.min.js";
  const BOARD_SUBSTITUTE = {  // board drawings not in the MIT package -> the closest one that is, same GPIOs
    "board-esp32-devkit-c-v4": { tag: "wokwi-esp32-devkit-v1", note: "Board drawn as ESP32 DevKit V1 (open-source Wokwi drawing); the Wokwi project uses DevKit-C v4 with the same GPIOs.",
      pin: n => ({ TX: "TX0", RX: "RX0" }[n] || (/^\d+$/.test(n) ? `D${n}` : n)) },
  };
  const LABELS = {  // what each part simulates; colour: blue = Wokwi virtual part, amber = software model
    potSpeed: { title: "SPEED potentiometer", role: "→ RPM setpoint", kind: "vs", at: "left" },
    potLoad: { title: "LOAD potentiometer", role: "→ current (software model)", kind: "sw", at: "above" },
    dht1: { title: "DHT22 · virtual sensor", role: "→ temperature", kind: "vs", at: "below" },
    imu1: { title: "MPU6050 · virtual accelerometer", role: "→ vibration (software model)", kind: "sw", at: "below" },
    btnScenario: { title: "SCENARIO button", role: "", kind: "vs", at: "below" },
    ledFault: { title: "FAULT LED", role: "", kind: "vs", at: "left" },
    r1: { title: "220 Ω", role: "", kind: "", at: "below" },
    esp: { title: "ESP32 · Wokwi", role: "", kind: "", at: "below" },
  };
  let lib = null, built = null, building = false;

  function loadLib() {
    if (lib) return lib;
    lib = new Promise((resolve, reject) => {
      const sc = document.createElement("script");
      sc.src = LIB; sc.async = true;
      sc.onload = () => customElements.whenDefined("wokwi-led").then(resolve, reject);
      sc.onerror = () => reject(new Error("part library failed to load"));
      document.head.appendChild(sc);
    });
    return lib;
  }

  async function buildCircuit(host) {
    const diagram = await (await fetch("/api/wokwi/diagram")).json();
    await loadLib();
    host.hidden = false;
    host.innerHTML = `<div class="wk-circuit-title">Virtual circuit (Wokwi simulator) <span id="wkcState">waiting for the device</span></div>
      <div class="wkc-fit"><div class="wkc-stage"><svg class="wkc-wires"></svg></div></div>
      <div class="wkc-legend"><span><i class="vs"></i>Wokwi virtual part</span><span><i class="sw"></i>value computed by a software model</span>
        <span id="wkcNote"></span><span>Not a real machine: simulated sensors</span></div>`;
    const fit = host.querySelector(".wkc-fit"), stage = host.querySelector(".wkc-stage"), svg = host.querySelector(".wkc-wires");
    const parts = {}, notes = [];
    for (const p of diagram.parts) {
      const sub = BOARD_SUBSTITUTE[p.type];
      const tag = sub ? sub.tag : p.type;
      if (!customElements.get(tag)) { notes.push(`${p.type} not drawn`); continue; }
      const node = document.createElement(tag);
      for (const [k, v] of Object.entries(p.attrs || {})) node.setAttribute(k, v);
      node.style.cssText = `position:absolute;left:${p.left}px;top:${p.top}px`;
      stage.appendChild(node);
      parts[p.id] = { node, part: p, pin: sub ? sub.pin : (n => n) };
      if (sub) notes.push(sub.note);
    }
    await Promise.all(Object.values(parts).map(x => x.node.updateComplete));
    // pin position in stage coordinates (diagram coordinates are the same pixels Wokwi uses)
    const pinAt = (ref) => {
      const [id, name] = ref.split(":");
      const x = parts[id];
      if (!x) return null;
      const pi = (x.node.pinInfo || []).find(q => q.name === x.pin(name));
      return pi ? { x: x.part.left + pi.x, y: x.part.top + pi.y } : null;
    };
    // bounds: every part plus label room
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const { node, part } of Object.values(parts)) {
      const r = node.getBoundingClientRect();
      minX = Math.min(minX, part.left); minY = Math.min(minY, part.top);
      maxX = Math.max(maxX, part.left + r.width); maxY = Math.max(maxY, part.top + r.height);
    }
    const pad = { l: 170, r: 150, t: 60, b: 60 };
    const ox = pad.l - minX, oy = pad.t - minY, W = maxX - minX + pad.l + pad.r, H = maxY - minY + pad.t + pad.b;
    stage.style.width = `${W}px`; stage.style.height = `${H}px`;
    for (const { node, part } of Object.values(parts)) { node.style.left = `${part.left + ox}px`; node.style.top = `${part.top + oy}px`; }
    svg.setAttribute("width", W); svg.setAttribute("height", H); svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    // wires: Wokwi auto-routes them; we draw right-angled paths between the same pins, in the same colours
    svg.innerHTML = diagram.connections.map(([a, b, color]) => {
      const p = pinAt(a), q = pinAt(b);
      if (!p || !q) return "";
      const ax = p.x + ox, ay = p.y + oy, bx = q.x + ox, by = q.y + oy, mx = Math.round((ax + bx) / 2);
      return `<polyline points="${ax},${ay} ${mx},${ay} ${mx},${by} ${bx},${by}" fill="none" stroke="${esc(color || "#888")}"
        stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round" opacity=".95"/>`;
    }).join("");
    // labels next to each part (values filled in by updateCircuit)
    for (const [id, { node, part }] of Object.entries(parts)) {
      const L = LABELS[id];
      if (!L) continue;
      const r = node.getBoundingClientRect();
      const lab = document.createElement("div");
      lab.className = `wkc-label ${L.kind}`;
      lab.dataset.part = id;
      lab.innerHTML = `<b>${esc(L.title)}</b>${L.role ? `<span>${esc(L.role)}</span>` : ""}<em data-v></em>`;
      const x = part.left + ox, y = part.top + oy;
      const place = { above: [x, y - 6, "translateY(-100%)"], below: [x, y + r.height + 4, ""], left: [x - 8, y, "translateX(-100%)"] }[L.at];
      lab.style.cssText = `left:${place[0]}px;top:${place[1]}px;transform:${place[2]}`;
      stage.appendChild(lab);
    }
    const scale = () => {
      const k = Math.min(1.3, fit.clientWidth / W);
      stage.style.transform = `scale(${k})`;
      fit.style.height = `${H * k}px`;
    };
    new ResizeObserver(scale).observe(fit);
    scale();
    host.querySelector("#wkcNote").textContent = notes.join(" ");
    return { parts, host };
  }

  function updateCircuit(s) {
    const host = el("wkCircuit");
    if (!host) return;
    if (s.state === "NOT_CONFIGURED") { host.hidden = true; return; }
    if (!built) {
      if (building) return;
      building = true;
      buildCircuit(host).then(b => { built = b; updateCircuit(s); })
        .catch(() => { host.hidden = false; host.innerHTML = circuit(s.machines[0], s); })  // plain schematic
        .finally(() => { building = false; });
      return;
    }
    const d = s.machines[0];
    const l = (d && d.state !== "OFFLINE" && d.last) || {};
    const live = !!(d && d.state === "ONLINE");
    const faultOn = live && l.scenario && l.scenario !== "normal";
    const P = built.parts;
    if (P.ledFault) P.ledFault.node.value = !!faultOn;
    // knob positions reconstructed from the telemetry (the sketch's own formulas, inverted)
    if (live && P.potSpeed && l.rpm != null) {
      const setpoint = l.rpm / (l.scenario === "high_load" ? 0.96 : 1);
      P.potSpeed.node.value = Math.max(0, Math.min(1023, setpoint / 3000 * 1023));
      if (P.potLoad && l.current_a != null && setpoint > 5) {
        const load = (l.current_a - 1.2) / (6.5 * (l.rpm / 1500)) / (l.scenario === "high_load" ? 1.35 : 1);
        P.potLoad.node.value = Math.max(0, Math.min(1023, load / 1.2 * 1023));
      }
    }
    const v = (x, n, u) => (x == null ? "—" : `${(+x).toFixed(n)} ${u}`);
    const values = {
      potSpeed: v(l.rpm, 0, "rpm"), potLoad: v(l.current_a, 2, "A"), dht1: v(l.temp_c, 1, "°C"),
      imu1: v(l.vibration_rms_mm_s, 2, "mm/s"),
      btnScenario: l.scenario ? l.scenario.replace(/_/g, " ") + (l.simulated_fault ? " · simulated fault" : "") : "—",
      ledFault: live ? (faultOn ? "on: fault scenario" : "off: normal") : l.scenario ? `not live (last: ${l.scenario === "normal" ? "off" : "on"})` : "not live",
    };
    built.host.querySelectorAll(".wkc-label").forEach(lab => {
      const out = lab.querySelector("[data-v]");
      out.textContent = values[lab.dataset.part] || "";
      out.className = live ? "" : "stale";
      lab.classList.toggle("alert", lab.dataset.part === "ledFault" && !!faultOn || lab.dataset.part === "btnScenario" && !!faultOn);
    });
    built.host.classList.toggle("wkc-stale", !live);
    el("wkcState").textContent = live ? `machine ${d.machine_id} · live values` : d ? `machine ${d.machine_id} · not live: last values hidden or stale` : "waiting for the device";
  }

  function render(s) {
    const [label, tone] = STATE[s.state] || [s.state, "warn"];
    el("wkState").textContent = label;
    el("wkState").className = `wk-chip wk-${tone}`;
    const b = s.broker, c = s.counters || {};
    const head = `<div class="wk-facts">
      <span>${esc(s.message)}</span>
      <span>MQTT: <b>${b ? `${esc((b.state || "").toLowerCase().replace("_", " "))} · ${esc(b.broker)}${b.tls ? " (TLS)" : ""}` : "not configured"}</b></span>
      ${b ? `<span>Topic: <code>${esc(b.topics[0])}</code></span>` : ""}
      <span>Messages: ${esc(c.accepted ?? 0)} accepted · ${esc(c.rejected ?? 0)} rejected${c.last_reject ? ` (last: ${esc(c.last_reject.reason)})` : ""}</span>
    </div>`;
    const body = s.state === "NOT_CONFIGURED"
      ? `<div class="sc-hint">No Wokwi broker is configured. Set <code>UDYAM_WOKWI_BROKER</code> (and the topic prefix used in the
         Wokwi sketch's config.h) in the server's .env and restart. See wokwi/udyam-virtual-machine/README.md.</div>`
      : s.machines.length ? `<div class="wk-grid">${s.machines.map(card).join("")}</div>`
      : `<div class="sc-hint">Waiting for a Wokwi virtual device to publish on the topic above.</div>`;
    el("wkBody").innerHTML = head + body + `<div class="sc-hint">${esc(s.notice)}</div>`;
    updateCircuit(s);
  }

  function load() {
    if (!document.body.classList.contains("real-mode")) return;  // Real Data mode only
    fetch("/api/wokwi").then(r => r.json()).then(render).catch(() => {});
  }

  function start() {
    if (!el("wokwiPanel")) return;
    new MutationObserver(load).observe(document.body, { attributes: true, attributeFilter: ["class"] });
    load();
    setInterval(load, 2000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
