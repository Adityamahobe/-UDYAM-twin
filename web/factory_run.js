// Factory Run Designer (Simulate tab): build a whole-factory test run from an event list, then run it fast on a
// separate copy of the factory, as a batch with variation, or play it on the live simulation in real time.
// API: /api/factory-run/* (sim/factory_run.py). Results are SYNTHETIC DIGITAL TWIN DATA.
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ACTION_LABEL = { set_params: "set parameters", inject_fault: "inject fault", repair: "repair",
    derate: "derate", release_derate: "release derate", self_healing: "twin self-healing" };
  const nice = s => String(s ?? "").replace(/_/g, " ");
  let opts = null, events = [], polling = null, livePoll = null;

  function hint(msg, tone) {
    const h = el("frHint");
    h.textContent = msg;
    h.className = `sc-hint${tone ? ` ${tone}` : ""}`;
  }
  const machine = id => opts.machines.find(m => m.machine_id === id);
  const spec = () => ({ name: el("frName").value.trim() || "Factory run", minutes: +el("frMinutes").value,
    seed: +el("frSeed").value, self_healing: el("frHeal").checked, events });

  // ---------------------------------------------------------------- event list
  function argsCell(ev, i) {
    const m = machine(ev.machine_id);
    if (ev.action === "set_params") {
      const specs = m ? m.params : [];
      const key = Object.keys(ev.params || {})[0] || (specs[0] && specs[0].key);
      const sp = specs.find(p => p.key === key);
      return `<select data-f="pkey" data-i="${i}">${specs.map(p => `<option value="${p.key}" ${p.key === key ? "selected" : ""}>${esc(p.label)}</option>`).join("")}</select>
        <input type="number" data-f="pval" data-i="${i}" value="${esc((ev.params || {})[key] ?? (sp ? sp.default : ""))}"
          ${sp ? `min="${sp.min}" max="${sp.max}" step="${sp.step}" title="${sp.min}–${sp.max} ${esc(sp.unit)}"` : ""}>
        <span class="sc-label">${sp ? esc(sp.unit) : ""}</span>`;
    }
    if (ev.action === "inject_fault") {
      const faults = m ? m.faults : [];
      return `<select data-f="fault" data-i="${i}">${faults.map(f => `<option value="${f}" ${f === ev.fault ? "selected" : ""}>${esc(nice(f))}</option>`).join("")}</select>
        <span class="sc-label">severity</span><input type="number" data-f="severity" data-i="${i}" min="0.5" max="2" step="0.1" value="${esc(ev.severity ?? 1)}">`;
    }
    if (ev.action === "derate") return `<input type="number" data-f="pct" data-i="${i}" min="1" max="50" value="${esc(ev.pct ?? 20)}"><span class="sc-label">%</span>`;
    if (ev.action === "self_healing") return `<select data-f="on" data-i="${i}"><option value="1" ${ev.on !== false ? "selected" : ""}>on</option><option value="0" ${ev.on === false ? "selected" : ""}>off</option></select>`;
    return `<span class="sc-label">—</span>`;
  }

  function renderEvents() {
    events.sort((a, b) => a.at_min - b.at_min);
    el("frEvents").innerHTML = events.length ? `<table class="fr-table"><thead><tr><th>at min</th><th>machine</th><th>action</th><th>details</th><th></th></tr></thead><tbody>${
      events.map((ev, i) => `<tr>
        <td><input type="number" data-f="at" data-i="${i}" min="0" step="0.5" value="${esc(ev.at_min)}" class="fr-at"></td>
        <td>${ev.action === "self_healing" ? '<span class="sc-label">whole factory</span>'
          : `<select data-f="machine" data-i="${i}">${opts.machines.map(m => `<option value="${m.machine_id}" ${m.machine_id === ev.machine_id ? "selected" : ""}>${esc(m.machine_id)}</option>`).join("")}</select>`}</td>
        <td><select data-f="action" data-i="${i}">${opts.actions.map(a => `<option value="${a}" ${a === ev.action ? "selected" : ""}>${esc(ACTION_LABEL[a] || a)}</option>`).join("")}</select></td>
        <td class="fr-args">${argsCell(ev, i)}</td>
        <td><button class="wi-x" data-del="${i}" aria-label="Remove event">×</button></td></tr>`).join("")}</tbody></table>`
      : `<div class="sc-hint">No events: the run is a normal shift. Add events or pick a preset.</div>`;
    const root = el("frEvents");
    root.querySelectorAll("[data-del]").forEach(b => b.onclick = () => { events.splice(+b.dataset.del, 1); renderEvents(); });
    root.querySelectorAll("[data-f]").forEach(inp => inp.onchange = () => {
      const ev = events[+inp.dataset.i], f = inp.dataset.f, v = inp.value;
      if (f === "at") ev.at_min = +v;
      else if (f === "machine") { ev.machine_id = v; defaults(ev); }
      else if (f === "action") { ev.action = v; defaults(ev); }
      else if (f === "pkey") { const sp = machine(ev.machine_id).params.find(p => p.key === v); ev.params = { [v]: sp.default }; }
      else if (f === "pval") { const k = Object.keys(ev.params)[0]; ev.params = { [k]: +v }; }
      else if (f === "fault") ev.fault = v;
      else if (f === "severity") ev.severity = +v;
      else if (f === "pct") ev.pct = +v;
      else if (f === "on") ev.on = v === "1";
      renderEvents();
    });
  }

  function defaults(ev) {  // keep the event consistent with its machine and action
    const m = machine(ev.machine_id) || opts.machines[0];
    ev.machine_id = m.machine_id;
    delete ev.params; delete ev.fault; delete ev.severity; delete ev.pct; delete ev.on;
    if (ev.action === "set_params") ev.params = { [m.params[0].key]: m.params[0].default };
    if (ev.action === "inject_fault") { ev.fault = m.faults[0]; ev.severity = 1; }
    if (ev.action === "derate") ev.pct = 20;
    if (ev.action === "self_healing") { ev.on = false; delete ev.machine_id; }
    if (!m.faults.length && ev.action === "inject_fault") hint(`${m.machine_id} has no faults to inject.`, "err");
  }

  function loadPreset(key) {
    const p = opts.presets[key];
    if (!p) return;
    el("frName").value = p.name; el("frMinutes").value = p.minutes; el("frHeal").checked = p.self_healing !== false;
    events = JSON.parse(JSON.stringify(p.events || []));
    renderEvents();
  }

  // ---------------------------------------------------------------- running
  function errText(res, status) {
    const d = res.detail;
    return Array.isArray(d) ? d.map(e => `${(e.loc || []).slice(-1)[0]}: ${e.msg}`).join("; ") : (d && (d.message || d)) || `Refused (${status})`;
  }
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(async r => { const res = await r.json().catch(() => ({})); if (!r.ok) throw new Error(errText(res, r.status)); return res; });
  }

  function poll() {
    fetch("/api/factory-run/job").then(r => r.json()).then(j => {
      const running = j.state === "running";
      ["frRun", "frBatch"].forEach(id => el(id).disabled = running);
      el("frCancel").hidden = !(running && j.kind === "batch");
      if (running) return hint(j.kind === "batch" ? `Batch: run ${j.done + 1} of ${j.total}…` : "Running…");
      clearInterval(polling); polling = null;
      if (j.state === "done") fetch(`/api/factory-run/results/${encodeURIComponent(j.id)}`).then(r => r.json())
        .then(r => { (r.kind === "batch" ? renderBatch : renderRun)(r); hint(`Done in ${r.computed_in_s} s.`, "ok"); });
      else if (j.state === "failed") hint(`Run failed: ${j.error}`, "err");
      else if (j.state === "cancelled") hint("Cancelled.");
    }).catch(() => {});
  }
  const startPolling = () => { if (!polling) polling = setInterval(poll, 400); poll(); };

  // ---------------------------------------------------------------- results
  const fmt = (v, d = 1) => (v == null ? "—" : (+v).toFixed(d));

  function lanes(r) {  // per machine: injected-fault bars (named), alert markers, event ticks
    const W = 760, L = 92, R = 12, rowH = 30, top = 18, ms = Object.keys(r.machines), H = top + ms.length * rowH + 32;
    const x = t => L + (W - L - R) * t / r.minutes;
    const rows = ms.map((mid, i) => {
      const y = top + i * rowH;
      const bars = r.labels.filter(l => l.machine_id === mid).map(l => {
        const x0 = x(l.start_min), x1 = x(l.end_min == null ? r.minutes : l.end_min);
        return `<rect x="${x0}" y="${y + 6}" width="${Math.max(2, x1 - x0)}" height="14" rx="3" fill="var(--wi-2)" opacity=".85"/>
          <text x="${x0 + 4}" y="${y + 17}" font-size="10" fill="#fff">${esc(nice(l.fault))} ×${l.severity}</text>`;
      }).join("");
      const marks = r.alerts.filter(a => a.machine_id === mid).map(a => `<path d="M${x(a.at_min)} ${y + 3} l5 9 h-10 z"
        fill="${a.severity === "CRITICAL" ? "var(--status-crit)" : "var(--status-warn)"}"><title>${esc(fmt(a.at_min, 2))} min · ${esc(a.title)}</title></path>`).join("");
      const ticks = r.events.filter(e => e.machine_id === mid).map(e => `<line x1="${x(e.at_min)}" x2="${x(e.at_min)}" y1="${y + 2}" y2="${y + 24}"
        stroke="var(--text-muted)" stroke-dasharray="2 2"><title>${esc(e.at_min)} min · ${esc(e.text)}</title></line>`).join("");
      return `<text x="${L - 8}" y="${y + 17}" font-size="11" text-anchor="end" fill="var(--text-secondary)">${esc(mid)}</text>
        <line x1="${L}" x2="${W - R}" y1="${y + 13}" y2="${y + 13}" stroke="var(--border-subtle)"/>${ticks}${bars}${marks}`;
    }).join("");
    // time axis: a tick and a faint gridline every minute; labelled every minute on short runs, every 5 min otherwise
    const axisY = top + ms.length * rowH + 4, every = r.minutes <= 15 ? 1 : 5;
    const mins = Array.from({ length: Math.floor(r.minutes) + 1 }, (_, m) => m), last = mins[mins.length - 1];
    const grid = mins.map(m => `<line x1="${x(m)}" x2="${x(m)}" y1="${top}" y2="${axisY}" stroke="var(--border-subtle)" stroke-opacity="${m % 5 ? .35 : .8}"/>`).join("");
    const axis = `<line x1="${L}" x2="${W - R}" y1="${axisY}" y2="${axisY}" stroke="var(--text-muted)" stroke-opacity=".5"/>` + mins.map(m => {
      const major = m % 5 === 0;
      return `<line x1="${x(m)}" x2="${x(m)}" y1="${axisY}" y2="${axisY + (major ? 6 : 3)}" stroke="var(--text-muted)"><title>${m} min</title></line>`
        + (m === last || (m % every === 0 && last - m >= Math.max(1, every / 2 + .5)) ? `<text x="${x(m)}" y="${axisY + 16}" font-size="9" text-anchor="${m === 0 ? "start" : m === last ? "end" : "middle"}"
          fill="var(--text-muted)">${m}${m === last ? " min" : ""}</text>` : "");
    }).join("");
    return `<div class="fr-lanes"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Faults, alerts and events per machine over the run">${grid}${rows}${axis}</svg></div>
      <div class="wi-legend"><span><i style="background:var(--wi-2);height:8px"></i>injected fault (ground truth)</span>
        <span><i style="background:var(--status-warn);height:8px"></i>alert raised by the twin</span><span>dashed line: run event</span></div>`;
  }

  function renderRun(r) {
    const k = r.kpis;
    const mrows = Object.entries(r.machines).map(([mid, m]) => `<tr><td>${esc(mid)} <span class="sc-label">${esc(m.name)}</span></td>
      <td>${fmt(m.oee, 3)}</td><td>${m.parts ?? "—"}</td><td>${fmt(m.energy_kwh, 2)}</td><td>${fmt(m.peak_temp_c)}</td><td>${fmt(m.peak_vibration_mm_s, 2)}</td><td>${m.alerts}</td></tr>`).join("");
    const lrows = r.labels.map(l => `<tr><td>${esc(l.machine_id)}</td><td>${esc(nice(l.fault))}</td><td>${l.severity}</td>
      <td>${fmt(l.start_min)}–${l.end_min == null ? "end" : fmt(l.end_min)}</td>
      <td class="${l.detected ? "fr-ok" : "fr-miss"}">${l.detected ? `after ${fmt(l.detection_delay_min, 2)} min` : "not detected"}</td>
      <td class="fr-alert">${esc(l.first_alert || "")}</td></tr>`).join("");
    el("frResult").innerHTML = `
      <div class="sc-hint">${esc(r.label)} · ${esc(r.name)} · ${r.minutes} min · seed ${r.seed} · self-healing ${r.self_healing ? "on" : "off"}</div>
      <div class="fr-kpis"><span>Line output <b>${k.line_parts}</b> parts</span><span>Energy <b>${fmt(k.site_energy_kwh, 2)}</b> kWh</span>
        <span>Alerts <b>${k.alerts}</b></span><span>Faults detected <b>${k.faults_detected}/${k.faults_injected}</b></span></div>
      ${lanes(r)}
      ${r.labels.length ? `<div class="synth-sub">Ground truth and detection</div><div class="wi-table-wrap"><table class="wi-table fr-left">
        <thead><tr><th>Machine</th><th>Injected fault</th><th>Severity</th><th>Active (min)</th><th>Twin detected</th><th>First alert after onset</th></tr></thead><tbody>${lrows}</tbody></table></div>
        <div class="sc-hint">"First alert" is the first alert on that machine after the fault started; check that it is about the fault.</div>` : ""}
      <div class="synth-sub">Machines</div><div class="wi-table-wrap"><table class="wi-table fr-left"><thead><tr><th>Machine</th><th>OEE</th><th>Parts</th><th>kWh</th><th>Peak °C</th><th>Peak mm/s</th><th>Alerts</th></tr></thead><tbody>${mrows}</tbody></table></div>
      <div class="control-bar"><a class="btn btn-sm" href="/api/factory-run/results/${encodeURIComponent(r.id)}/export?format=csv">Download CSV (every 10 s, labelled)</a>
        <a class="btn btn-sm" href="/api/factory-run/results/${encodeURIComponent(r.id)}/export?format=json">Download JSON</a></div>`;
  }

  function renderBatch(b) {
    const drows = Object.entries(b.detection).map(([k, d]) => `<tr><td>${esc(nice(k))}</td><td>${d.detected}/${d.injected}</td>
      <td>${(100 * d.detection_rate).toFixed(0)} %</td><td>${fmt(d.mean_delay_min, 2)}</td><td>${fmt(d.max_delay_min, 2)}</td></tr>`).join("");
    const m = b.kpis_mean;
    el("frResult").innerHTML = `
      <div class="sc-hint">${esc(b.label)} · batch "${esc(b.name)}" · ${b.runs} runs × ${b.minutes} min · seeds ${b.base_seed}–${b.base_seed + b.runs - 1}
        · event time ±${b.variation.fault_time_jitter_min} min${b.variation.severity_range ? ` · severity ${b.variation.severity_range.join("–")}` : ""} · parameters ±${b.variation.param_jitter_pct} %</div>
      <div class="fr-kpis"><span>Mean line output <b>${fmt(m.line_parts)}</b> parts</span><span>Mean energy <b>${fmt(m.site_energy_kwh, 2)}</b> kWh</span>
        <span>Mean alerts <b>${fmt(m.alerts)}</b></span></div>
      ${drows ? `<div class="synth-sub">Did the twin detect the injected faults?</div><div class="wi-table-wrap"><table class="wi-table fr-left">
        <thead><tr><th>Machine · fault</th><th>Detected</th><th>Rate</th><th>Mean delay (min)</th><th>Max delay (min)</th></tr></thead><tbody>${drows}</tbody></table></div>`
        : `<div class="sc-hint">No faults in this run, so there is nothing to detect.</div>`}
      <details class="wk-more"><summary>Every run</summary><div class="wi-table-wrap"><table class="wi-table fr-left">
        <thead><tr><th>Run</th><th>Seed</th><th>Parts</th><th>kWh</th><th>Alerts</th><th>Detected</th></tr></thead><tbody>${
        b.per_run.map(r => `<tr><td>${esc(r.name)}</td><td>${r.seed}</td><td>${r.line_parts}</td><td>${fmt(r.site_energy_kwh, 2)}</td><td>${r.alerts}</td><td>${r.faults_detected}/${r.faults_injected}</td></tr>`).join("")}</tbody></table></div></details>
      <div class="control-bar"><a class="btn btn-sm" href="/api/factory-run/results/${encodeURIComponent(b.id)}/export?format=csv">Download labels CSV</a>
        <a class="btn btn-sm" href="/api/factory-run/results/${encodeURIComponent(b.id)}/export?format=json">Download JSON</a></div>`;
  }

  // ---------------------------------------------------------------- live playback
  function liveStatus() {
    fetch("/api/factory-run/live").then(r => r.json()).then(s => {
      const running = s.state === "running";
      el("frLive").hidden = running; el("frLiveStop").hidden = !running;
      el("frLiveState").textContent = running
        ? `Live: "${s.name}" ${fmt(s.elapsed_min)} / ${s.minutes} min${s.next_event ? ` · next at ${s.next_event.at_min} min: ${s.next_event.text}` : ""} · watch the Live tab`
        : s.state && s.state !== "idle" ? `Last live run ${s.state}.` : "";
      if (!running && livePoll) { clearInterval(livePoll); livePoll = null; }
    }).catch(() => {});
  }

  function start() {
    if (!el("factoryRunPanel")) return;
    fetch("/api/factory-run/options").then(r => r.json()).then(o => {
      opts = o;
      el("frPreset").innerHTML = `<option value="">choose…</option>` + Object.entries(o.presets).map(([k, p]) => `<option value="${k}">${esc(p.name)}</option>`).join("");
      el("frMinutes").max = o.max_minutes; el("frRuns").max = o.max_batch;
      loadPreset("compressor_mid_shift");
      el("frPreset").value = "compressor_mid_shift";
      fetch("/api/factory-run/results").then(r => r.json()).then(list => {
        if (list.length) fetch(`/api/factory-run/results/${encodeURIComponent(list[0].id)}`).then(r => r.json()).then(r => (r.kind === "batch" ? renderBatch : renderRun)(r));
      });
    });
    el("frPreset").onchange = () => loadPreset(el("frPreset").value);
    el("frAdd").onclick = () => {
      const last = events[events.length - 1];
      const ev = { at_min: last ? Math.min(+el("frMinutes").value - 1, last.at_min + 5) : 5, action: "inject_fault", machine_id: opts.machines[0].machine_id };
      defaults(ev); events.push(ev); renderEvents();
    };
    el("frRun").onclick = () => post("/api/factory-run/run", spec()).then(startPolling).catch(e => hint(e.message, "err"));
    el("frBatch").onclick = () => post("/api/factory-run/batch", { spec: spec(), runs: +el("frRuns").value,
      fault_time_jitter_min: +el("frJitter").value, severity_range: [+el("frSevLo").value, +el("frSevHi").value],
      param_jitter_pct: +el("frParamJ").value }).then(startPolling).catch(e => hint(e.message, "err"));
    el("frCancel").onclick = () => fetch("/api/factory-run/job/cancel", { method: "POST" });
    el("frLive").onclick = () => udyamConfirm(`Play "${spec().name}" on the live simulation?\n\nThe live plant is reset to a clean baseline (Auto Demo off, default parameters), then the ${events.length} event(s) are applied in real time over ${spec().minutes} min. Watch it on the Live tab and in the 3D view.`,
      { ok: "Play live" }).then(ok => {
      if (!ok) return;
      post("/api/factory-run/live/start", spec()).then(() => { hint("Playing on the live simulation.", "ok"); if (!livePoll) livePoll = setInterval(liveStatus, 1000); liveStatus(); })
        .catch(e => hint(e.message, "err"));
    });
    el("frLiveStop").onclick = () => post("/api/factory-run/live/stop", {}).then(liveStatus);
    liveStatus();
    setInterval(() => { if (!livePoll) liveStatus(); }, 5000);
  }
  // the Factory Dataset panel (factory_dataset.js) adds the current design to its recipe
  window.udyamFactoryRun = {
    spec: () => JSON.parse(JSON.stringify(spec())),
    variation: () => ({ runs: +el("frRuns").value, fault_time_jitter_min: +el("frJitter").value,
      severity_range: [+el("frSevLo").value, +el("frSevHi").value], param_jitter_pct: +el("frParamJ").value }),
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
