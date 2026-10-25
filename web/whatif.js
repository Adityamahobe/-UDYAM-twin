// What-If Comparison panel: build 2–4 scenarios (machine parameters, an optional fault, self-healing on/off),
// run them on separate virtual lines (POST /api/whatif/run) and compare outcomes side by side.
// Results are SIMULATED WHAT-IF outcomes for the virtual line, not predictions for real machines.
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const COLORS = ["var(--wi-1)", "var(--wi-2)", "var(--wi-3)", "var(--wi-4)"];
  const PRESETS = {
    faster: { name: "Faster spindles (+15 %)", params: { "VMC-01": { spindle_speed_pct: 115 }, "LATHE-01": { spindle_speed_pct: 115 }, "SAW-01": { spindle_speed_pct: 115 } } },
    hot: { name: "Hot day, weak coolant", params: { "VMC-01": { ambient_temp_c: 36, cooling_capacity_pct: 70 } } },
    heavy: { name: "Heavier jobs (load 120 %)", params: { "VMC-01": { load_pct: 120 }, "LATHE-01": { load_pct: 120 } } },
    coolant: { name: "Coolant fault, no self-healing", fault: { machine_id: "VMC-01", fault: "coolant_degradation", at_min: 2 }, auto_derate: false },
    coolant_heal: { name: "Coolant fault, self-healing", fault: { machine_id: "VMC-01", fault: "coolant_degradation", at_min: 2 } },
  };
  const KPIS = [
    ["line_parts", "Line output (parts)", 0], ["line_value_inr", "Output value (₹)", 0],
    ["site_energy_kwh", "Site energy (kWh)", 2], ["energy_cost_inr", "Energy cost (₹)", 1], ["energy_kwh_per_part", "Energy per part (kWh)", 3],
    ["co2_kg", "CO₂ (kg)", 2], ["focus_oee", "OEE · focus", 3], ["focus_quality", "Quality · focus", 3],
    ["focus_peak_temp_c", "Peak temperature (°C)", 1], ["focus_min_over_temp_limit", "Minutes over temp limit", 1],
    ["focus_peak_vibration_mm_s", "Peak vibration (mm/s)", 2], ["focus_min_in_zone_c_or_d", "Minutes in vibration zone C/D", 1],
    ["focus_min_derated", "Minutes derated", 1], ["focus_min_tripped", "Minutes tripped", 1],
    ["focus_tool_changes", "Tool changes", 0], ["alerts_raised", "Alerts raised", 0],
  ];
  let opts = null, scenarios = [], polling = null;

  function hint(msg, tone) {
    const h = el("wiHint");
    h.textContent = msg;
    h.className = `sc-hint${tone ? ` ${tone}` : ""}`;
  }
  const spec = (mid, key) => (opts.machines.find(m => m.machine_id === mid)?.params || []).find(p => p.key === key);
  const newScenario = (base = {}) => ({ name: base.name || `Scenario ${scenarios.length + 1}`, params: JSON.parse(JSON.stringify(base.params || {})),
    fault: base.fault ? { ...base.fault } : null, auto_derate: base.auto_derate !== false });

  function renderScenarios() {
    el("wiScenarios").innerHTML = scenarios.map((sc, i) => {
      const chips = Object.entries(sc.params).flatMap(([mid, ps]) => Object.entries(ps).map(([k, v]) => {
        const s = spec(mid, k);
        return `<span class="wi-chip">${esc(mid)} · ${esc(s ? s.label : k)} ${esc(v)} ${esc(s ? s.unit : "")}<button data-rm="${i}|${esc(mid)}|${esc(k)}" aria-label="Remove">×</button></span>`;
      })).join("");
      const machines = opts.machines.map(m => `<option>${esc(m.machine_id)}</option>`).join("");
      return `<div class="wi-sc" style="--wi-c:${COLORS[i]}" data-i="${i}">
        <div class="head"><input value="${esc(sc.name)}" data-name="${i}" maxlength="40" aria-label="Scenario name">
          ${i === 0 ? `<span class="base-tag">baseline</span>` : `<button class="wi-x" data-del="${i}" aria-label="Remove scenario">×</button>`}</div>
        <div class="chips">${chips || `<span class="sc-hint">Default parameters</span>`}</div>
        <div class="wi-row"><select data-pm="${i}">${machines}</select><select data-pk="${i}"></select>
          <input type="number" data-pv="${i}" placeholder="value"><button class="btn btn-sm" data-padd="${i}">Set</button></div>
        <div class="wi-row"><span class="sc-label">Fault</span>
          <select data-ff="${i}"><option value="">none</option>${opts.faults.map(f => `<option ${sc.fault && sc.fault.fault === f ? "selected" : ""}>${f}</option>`).join("")}</select>
          <select data-fm="${i}">${opts.machines.map(m => `<option ${sc.fault && sc.fault.machine_id === m.machine_id ? "selected" : ""}>${esc(m.machine_id)}</option>`).join("")}</select>
          <input type="number" data-ft="${i}" min="0" step="0.5" value="${sc.fault ? sc.fault.at_min : 2}" title="minute the fault starts" style="max-width:56px"></div>
        <label class="sc-label"><input type="checkbox" data-ad="${i}" ${sc.auto_derate ? "checked" : ""}> Twin self-healing (SIM auto-derate)</label>
      </div>`;
    }).join("");
    const root = el("wiScenarios");
    const fillKeys = i => {
      const mid = root.querySelector(`[data-pm="${i}"]`).value;
      root.querySelector(`[data-pk="${i}"]`).innerHTML = (opts.machines.find(m => m.machine_id === mid)?.params || [])
        .map(p => `<option value="${p.key}">${esc(p.label)} (${p.min}–${p.max} ${esc(p.unit)})</option>`).join("");
    };
    scenarios.forEach((_, i) => fillKeys(i));
    root.querySelectorAll("[data-pm]").forEach(s => s.onchange = () => fillKeys(+s.dataset.pm));
    root.querySelectorAll("[data-name]").forEach(inp => inp.oninput = () => { scenarios[+inp.dataset.name].name = inp.value; });
    root.querySelectorAll("[data-del]").forEach(b => b.onclick = () => { scenarios.splice(+b.dataset.del, 1); renderScenarios(); });
    root.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => {
      const [i, mid, k] = b.dataset.rm.split("|");
      delete scenarios[+i].params[mid][k];
      if (!Object.keys(scenarios[+i].params[mid]).length) delete scenarios[+i].params[mid];
      renderScenarios();
    });
    root.querySelectorAll("[data-padd]").forEach(b => b.onclick = () => {
      const i = +b.dataset.padd, mid = root.querySelector(`[data-pm="${i}"]`).value, k = root.querySelector(`[data-pk="${i}"]`).value;
      const v = root.querySelector(`[data-pv="${i}"]`).value, s = spec(mid, k);
      if (v === "" || !s) return hint("Pick a parameter and a value.", "err");
      if (+v < s.min || +v > s.max) return hint(`${s.label} must be ${s.min}–${s.max} ${s.unit}.`, "err");
      (scenarios[i].params[mid] = scenarios[i].params[mid] || {})[k] = +v;
      renderScenarios();
    });
    const syncFault = i => {
      const f = root.querySelector(`[data-ff="${i}"]`).value;
      scenarios[i].fault = f ? { fault: f, machine_id: root.querySelector(`[data-fm="${i}"]`).value, at_min: +root.querySelector(`[data-ft="${i}"]`).value } : null;
    };
    root.querySelectorAll("[data-ff],[data-fm],[data-ft]").forEach(x => x.onchange = () => syncFault(+(x.dataset.ff ?? x.dataset.fm ?? x.dataset.ft)));
    root.querySelectorAll("[data-ad]").forEach(x => x.onchange = () => { scenarios[+x.dataset.ad].auto_derate = x.checked; });
    el("wiAdd").disabled = scenarios.length >= opts.max_scenarios;
  }

  function fmt(v, d) { return v == null ? "—" : (+v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }); }

  function renderResult(r) {
    const scs = r.scenarios;
    const head = `<tr><th>${esc(r.minutes)} min · seed ${esc(r.seed)} · focus ${esc(r.focus_machine)}</th>${scs.map((s, i) =>
      `<th style="--wi-c:${COLORS[i]}"><span class="sw"></span>${esc(s.name)}</th>`).join("")}</tr>`;
    const rows = KPIS.map(([k, label, d]) => `<tr><td>${label}</td>${scs.map((s, i) => {
      const v = s.kpis[k], dv = i ? (s.delta_vs_baseline || {})[k] : null, dir = r.better[k];
      const cls = dv && dir ? ((dv > 0) === (dir === "higher") ? "better" : "worse") : "";
      return `<td>${fmt(v, d)}${i && dv != null && dv !== 0 ? `<span class="d ${cls}">${dv > 0 ? "+" : ""}${fmt(dv, d)}</span>` : ""}</td>`;
    }).join("")}</tr>`).join("");
    const alerts = scs.map((s, i) => `<div><b style="border-left:3px solid ${COLORS[i]};padding-left:4px">${esc(s.name)}</b>: ${
      s.alerts.length ? s.alerts.map(a => `${esc(a.title.split(";")[0])} (min ${a.first_at_min})`).join(" · ") : "no alerts"}</div>`).join("");
    el("wiResult").innerHTML = `
      <div class="sc-hint">${esc(r.label)}. ${esc(r.method)} Green and red deltas are better or worse than the baseline.</div>
      <div class="wi-chart" id="wiChart"></div>
      <div class="wi-table-wrap"><table class="wi-table"><thead>${head}</thead><tbody>${rows}</tbody></table></div>
      <div class="wi-alerts">${alerts}</div>
      <div class="control-bar"><a class="btn btn-sm" href="/api/whatif/results/${encodeURIComponent(r.id)}/export?format=csv">Download CSV</a>
        <a class="btn btn-sm" href="/api/whatif/results/${encodeURIComponent(r.id)}/export?format=json">Download JSON</a></div>`;
    renderChart(r);
  }

  // Focus-machine temperature over the run, one line per scenario, with the configured limit for reference.
  function renderChart(r) {
    const box = el("wiChart");
    const series = r.scenarios.map((s, i) => ({ name: s.name, color: COLORS[i], pts: s.trend.filter(p => p.temp_c != null) }));
    if (!series.some(s => s.pts.length)) { box.innerHTML = ""; return; }
    const W = Math.max(280, Math.round(box.clientWidth || 640)), H = 200, L = 40, R = 12, T = 12, B = 24;
    const all = series.flatMap(s => s.pts.map(p => p.temp_c)).concat(r.temperature_limit_c != null ? [r.temperature_limit_c] : []);
    const lo = Math.floor(Math.min(...all) - 1), hi = Math.ceil(Math.max(...all) + 1), tmax = r.minutes;
    const x = t => L + (W - L - R) * t / tmax, y = v => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    const ticks = [lo, Math.round((lo + hi) / 2), hi];
    const grid = ticks.map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--border-subtle)" stroke-width="1"/>
      <text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" font-size="10" fill="var(--text-muted)">${v}°</text>`).join("");
    const xt = [0, tmax / 2, tmax].map(t => `<text x="${x(t)}" y="${H - 6}" text-anchor="middle" font-size="10" fill="var(--text-muted)">${+t.toFixed(1)} min</text>`).join("");
    const limit = r.temperature_limit_c != null ? `<line x1="${L}" x2="${W - R}" y1="${y(r.temperature_limit_c)}" y2="${y(r.temperature_limit_c)}"
      stroke="var(--text-muted)" stroke-dasharray="4 4" stroke-width="1"/><text x="${W - R}" y="${y(r.temperature_limit_c) - 4}" text-anchor="end"
      font-size="10" fill="var(--text-secondary)">limit ${r.temperature_limit_c} °C</text>` : "";
    const lines = series.map(s => `<polyline fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"
      points="${s.pts.map(p => `${x(p.t_min).toFixed(1)},${y(p.temp_c).toFixed(1)}`).join(" ")}"/>`).join("");
    box.innerHTML = `<div class="sc-label">Process temperature · ${esc(r.focus_machine)} (°C)</div>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Temperature of ${esc(r.focus_machine)} per scenario">
        ${grid}${xt}${limit}${lines}<line id="wiCross" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="var(--text-muted)" stroke-width="1" visibility="hidden"/>
        <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" id="wiHit"/></svg>
      <div class="wi-legend">${series.map(s => `<span style="--wi-c:${s.color}"><i></i>${esc(s.name)}</span>`).join("")}</div>
      <div class="wi-tip" id="wiTip" hidden></div>`;
    const svg = box.querySelector("svg"), tip = el("wiTip"), cross = el("wiCross");
    el("wiHit").addEventListener("mousemove", ev => {
      const rect = svg.getBoundingClientRect();
      const t = Math.max(0, Math.min(tmax, ((ev.clientX - rect.left) * W / rect.width - L) / (W - L - R) * tmax));
      const near = series.map(s => ({ s, p: s.pts.reduce((a, b) => Math.abs(b.t_min - t) < Math.abs(a.t_min - t) ? b : a, s.pts[0]) })).filter(n => n.p);
      if (!near.length) return;
      const px = x(near[0].p.t_min);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
      tip.hidden = false;
      tip.innerHTML = `<div>${near[0].p.t_min} min</div>` + near.map(n => `<div><span style="color:${n.s.color}">■</span> ${esc(n.s.name)}: ${n.p.temp_c} °C</div>`).join("");
      const left = (px / W) * rect.width;
      tip.style.left = `${Math.min(rect.width - tip.offsetWidth, Math.max(0, left + 10))}px`;
      tip.style.top = "24px";
    });
    el("wiHit").addEventListener("mouseleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
  }

  function poll() {
    fetch("/api/whatif/job").then(r => r.json()).then(j => {
      const running = j.state === "running";
      el("wiRun").disabled = running;
      el("wiCancel").hidden = !running;
      if (running) return hint(`Running scenario ${j.done + 1} of ${j.total}…`);
      clearInterval(polling); polling = null;
      if (j.state === "done") {
        fetch(`/api/whatif/results/${encodeURIComponent(j.id)}`).then(r => r.json()).then(r => { renderResult(r); hint(`Compared ${r.scenarios.length} scenarios in ${r.computed_in_s} s.`, "ok"); });
      } else if (j.state === "failed") hint(`Comparison failed: ${j.error}`, "err");
      else if (j.state === "cancelled") hint("Cancelled.");
    }).catch(() => {});
  }

  function start() {
    if (!el("whatifPanel")) return;
    fetch("/api/whatif/options").then(r => r.json()).then(o => {
      opts = o;
      el("wiFocus").innerHTML = o.machines.map(m => `<option value="${esc(m.machine_id)}">${esc(m.machine_id)} (${esc(m.name)})</option>`).join("");
      el("wiPreset").innerHTML = `<option value="">choose…</option>` + Object.entries(PRESETS).map(([k, p]) => `<option value="${k}">${esc(p.name)}</option>`).join("");
      el("wiMinutes").max = o.max_minutes;
      scenarios = [newScenario({ name: "Baseline" }), newScenario(PRESETS.faster)];
      renderScenarios();
      // show the latest comparison kept on the server (e.g. after a page reload)
      fetch("/api/whatif/results").then(r => r.json()).then(list => {
        if (list.length) fetch(`/api/whatif/results/${encodeURIComponent(list[0].id)}`).then(r => r.json()).then(renderResult);
      });
    });
    el("wiPreset").onchange = () => {
      const p = PRESETS[el("wiPreset").value];
      el("wiPreset").value = "";
      if (!p) return;
      if (scenarios.length >= opts.max_scenarios) return hint(`At most ${opts.max_scenarios} scenarios: remove one first.`, "err");
      scenarios.push(newScenario(p));
      renderScenarios();
    };
    el("wiAdd").onclick = () => { if (scenarios.length < opts.max_scenarios) { scenarios.push(newScenario()); renderScenarios(); } };
    el("wiFromLive").onclick = () => fetch("/api/whatif/options").then(r => r.json()).then(o => {
      const live = {};
      for (const [mid, vals] of Object.entries(o.live_params || {})) {
        for (const [k, v] of Object.entries(vals)) {
          const s = spec(mid, k);
          if (s && Math.abs(v - s.default) > 1e-9) (live[mid] = live[mid] || {})[k] = v;
        }
      }
      scenarios[0] = newScenario({ name: "Current settings", params: live });
      renderScenarios();
      hint(Object.keys(live).length ? "Baseline now uses the live Simulation Controls parameters." : "The live simulation uses default parameters.");
    });
    el("wiRun").onclick = () => {
      const body = { scenarios, minutes: +el("wiMinutes").value, seed: +el("wiSeed").value, focus_machine: el("wiFocus").value };
      fetch("/api/whatif/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(async r => {
          const res = await r.json().catch(() => ({}));
          if (!r.ok) {
            const d = res.detail;
            return hint(Array.isArray(d) ? d.map(e => `${(e.loc || []).slice(-1)[0]}: ${e.msg}`).join("; ") : (d && d.message) || `Refused (${r.status})`, "err");
          }
          if (!polling) polling = setInterval(poll, 500);
          poll();
        });
    };
    el("wiCancel").onclick = () => fetch("/api/whatif/job/cancel", { method: "POST" });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
