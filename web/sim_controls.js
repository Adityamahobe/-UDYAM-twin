// Simulation Controls panel (Testing mode): machine parameters with engineering limits, and run control
// (pause / resume / stop / run for N minutes). GET /api/sim/controls, POST /api/sim/params, /api/sim/run.
// Simulation only: the server refuses all of this in Real Data mode, and nothing is sent to any device.
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const RUN = { RUNNING: "RUNNING", PAUSED: "PAUSED", STOPPED: "STOPPED" };
  let status = null;
  let draft = null;           // { machine_id, values } while the operator is editing sliders
  let shownFor = null;        // machine whose sliders are rendered

  const fmt = (v, step) => (+v).toFixed(step < 1 ? 1 : 0);

  function hint(msg, tone) {
    const h = el("scHint");
    h.textContent = msg;
    h.className = `sc-hint${tone ? ` ${tone}` : ""}`;
  }

  function current() {
    return status && status.machines.find(m => m.machine_id === el("scMachine").value);
  }

  function renderRun() {
    const r = status.run;
    const chip = el("scRunState");
    let text = RUN[r.state] || r.state;
    if (r.state === "RUNNING" && r.remaining_s != null) text += ` · ${Math.ceil(r.remaining_s / 60 * 10) / 10} min left`;
    chip.textContent = text;
    chip.className = `sc-run ${r.state}`;
    el("scPause").disabled = r.state !== "RUNNING";
    el("scResume").disabled = r.state === "RUNNING";
    el("scResume").textContent = r.state === "STOPPED" ? "Start" : "Resume";
  }

  function renderMachines() {
    const sel = el("scMachine");
    const ids = status.machines.map(m => m.machine_id).join();
    if (sel.dataset.ids !== ids) {
      const keep = sel.value || (typeof state !== "undefined" && state.selectedMachineId) || status.machines[0]?.machine_id;
      sel.innerHTML = status.machines.map(m =>
        `<option value="${esc(m.machine_id)}">${esc(m.machine_id)} (${esc(m.name)})</option>`).join("");
      sel.dataset.ids = ids;
      if (status.machines.some(m => m.machine_id === keep)) sel.value = keep;
    }
  }

  function renderParams(force) {
    const m = current();
    if (!m) return;
    const editing = draft && draft.machine_id === m.machine_id;
    if (!force && editing && shownFor === m.machine_id) return; // don't overwrite sliders being edited
    const values = editing ? draft.values : m.values;
    el("scTag").textContent = m.default ? "" : "Custom parameters active (simulation only)";
    el("scParams").innerHTML = m.specs.map(s => {
      const v = values[s.key], changed = Math.abs(v - s.default) > 1e-9;
      return `<div class="sc-param">
        <div class="sc-param-head"><span>${esc(s.label)}</span>
          <b class="${changed ? "changed" : ""}" id="scv-${s.key}">${fmt(v, s.step)} ${esc(s.unit)}</b></div>
        <input type="range" data-key="${s.key}" min="${s.min}" max="${s.max}" step="${s.step}" value="${v}"
          aria-label="${esc(s.label)}" title="${esc(s.help)}">
        <small>limit ${fmt(s.min, s.step)}–${fmt(s.max, s.step)} · default ${fmt(s.default, s.step)}</small>
      </div>`;
    }).join("");
    shownFor = m.machine_id;
    el("scParams").querySelectorAll("input[type=range]").forEach(inp => inp.addEventListener("input", () => {
      const spec = m.specs.find(s => s.key === inp.dataset.key);
      if (!draft || draft.machine_id !== m.machine_id) draft = { machine_id: m.machine_id, values: { ...m.values } };
      draft.values[spec.key] = +inp.value;
      const out = el(`scv-${spec.key}`);
      out.textContent = `${fmt(inp.value, spec.step)} ${spec.unit}`;
      out.className = Math.abs(+inp.value - spec.default) > 1e-9 ? "changed" : "";
      hint("Not applied yet: press “Apply to simulation”.");
    }));
  }

  function render(force) {
    if (!status) return;
    renderRun();
    renderMachines();
    renderParams(force);
  }

  function load() {
    return fetch("/api/sim/controls").then(r => r.json()).then(s => { status = s; render(false); syncAutoDemo(s.auto_demo); }).catch(() => {});
  }

  // The header's Auto Demo button shows the server's real state (it can change from the API, a reset or a mode switch).
  function syncAutoDemo(on) {
    const btn = el("btnAutoDemo");
    if (!btn || typeof on !== "boolean") return;
    if (typeof state !== "undefined") state.autoDemo = on;
    btn.textContent = `Auto Demo: ${on ? "ON" : "OFF"}`;
  }

  function post(url, body, okMsg) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined })
      .then(async r => {
        const res = await r.json().catch(() => ({}));
        if (!r.ok) {
          const d = res.detail || {};
          hint(d.message || (typeof d === "string" ? d : `Refused (${r.status})`), "err");
          return false;
        }
        status = res;
        draft = null;
        render(true);
        if (okMsg) hint(okMsg, "ok");
        return true;
      }).catch(() => { hint("Could not reach the server.", "err"); return false; });
  }

  function start() {
    if (!el("simControlsPanel")) return;
    el("scPause").onclick = () => post("/api/sim/run", { action: "pause" }, "Paused: the simulation state is frozen.");
    el("scResume").onclick = () => post("/api/sim/run", { action: status?.run.state === "STOPPED" ? "start" : "resume" }, "Running.");
    el("scStop").onclick = () => {
      if (!window.confirm("Stop the simulation?\n\nThe virtual plant is reset to a clean baseline and stops ticking. Parameters are kept.")) return;
      post("/api/sim/run", { action: "stop" }, "Stopped and reset. Press Start to run again.");
    };
    el("scRunFor").onclick = () => {
      const min = +el("scMinutes").value;
      post("/api/sim/run", { action: "run_for", minutes: min }, `Running for ${min} min, then pausing.`);
    };
    el("scMachine").onchange = () => { draft = null; renderParams(true); hint("Parameters change the simulated plant only."); };
    el("scApply").onclick = () => {
      const m = current();
      if (!m) return;
      if (!draft || draft.machine_id !== m.machine_id) return hint("Nothing changed.");
      post("/api/sim/params", { machine_id: m.machine_id, params: draft.values },
        `Applied to the simulated ${m.machine_id}. Watch the readings, alerts and 3D view respond.`);
    };
    el("scResetOne").onclick = () => {
      const m = current();
      if (m) post(`/api/sim/params/reset?machine_id=${encodeURIComponent(m.machine_id)}`, null, `${m.machine_id} back to default parameters.`);
    };
    el("scResetAll").onclick = () => post("/api/sim/params/reset", null, "All machines back to default parameters.");
    load();
    setInterval(load, 2000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
