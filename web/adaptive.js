// Adaptive Learning panel: input drift of the failure-risk model's inputs (coverage of its training data, and
// change from each machine's own baseline), and operator-confirmed labels. API: /api/adaptive*.
// Drift triggers nothing; labels need an operator (token prompt when the server asks).
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const STATUS = { OK: "OK", WATCH: "WATCH", DRIFT: "DRIFT", OUT_OF_DISTRIBUTION: "OUTSIDE TRAINING DATA",
    NO_BASELINE: "BUILDING BASELINE", INSUFFICIENT_DATA: "COLLECTING" };
  const FEAT = { air_temp_k: "air temp", process_temp_k: "process temp", rpm: "speed", torque_nm: "torque", tool_wear_min: "tool wear" };
  let data = null, operatorToken = null, alertsFor = null;

  function hint(msg, tone) {
    const h = el("adHint");
    h.textContent = msg;
    h.className = `sc-hint${tone ? ` ${tone}` : ""}`;
  }
  const chip = s => `<span class="ad-status ${esc(s)}">${esc(STATUS[s] || s)}</span>`;

  function renderDrift() {
    const r = data.reference;
    el("adRef").textContent = `REFERENCE: ${r.version}${r.approximate ? " (approx.)" : ""} · ${r.rows} rows`;
    el("adDrift").innerHTML = data.drift.length ? data.drift.map(d => {
      const cov = d.coverage || {}, dr = d.drift || {};
      const shifts = dr.features ? Object.entries(dr.features).map(([f, v]) =>
        `${FEAT[f] || f} ${v.shift >= 0 ? "+" : ""}${v.shift}×`).join(" · ") : "";
      return `<div class="md-v">
        <div class="row1"><b>${esc(d.machine_id)} · <span class="ad-src ${esc(d.source)}">${d.source === "LIVE" ? "LIVE (real device)" : esc(d.source)}</span></b>
          <span>Coverage ${chip(cov.status)} Drift ${chip(dr.status)}</span></div>
        <div class="on">${esc(d.message || "")}${cov.outside_share != null ? ` · model ${esc(cov.model_version)}` : ""}</div>
        ${shifts ? `<div class="on">Change vs baseline (in baseline spreads): ${esc(shifts)}${dr.baseline ? ` · baseline ${esc(dr.baseline.set_by)}${dr.baseline.set_at ? ` at ${new Date(dr.baseline.set_at).toLocaleTimeString()}` : ""}` : ""}</div>` : ""}
        ${d.recommendation ? `<div class="on rb-warn">${esc(d.recommendation)}</div>` : ""}
        <button class="btn btn-sm" data-base="${esc(d.machine_id)}|${esc(d.source)}" title="Use the last 5 minutes as this machine's normal">Set baseline (last 5 min)</button>
      </div>`;
    }).join("") : `<div class="sc-hint">Collecting data from machines that use the failure-risk model…</div>`;
    el("adDrift").querySelectorAll("[data-base]").forEach(b => b.onclick = () => {
      const [machine_id, source] = b.dataset.base.split("|");
      fetch("/api/adaptive/baseline", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine_id, source }) })
        .then(async r => { const res = await r.json(); if (!r.ok) return hint(res.detail.message, "err"); data = res; render(); hint(`New baseline for ${machine_id} (${source}).`, "ok"); });
    });
  }

  function renderLabels() {
    const c = data.label_counts;
    if (window.udyamSetLabelCount) window.udyamSetLabelCount(c.active);
    el("adLabels").innerHTML = data.labels.length ? data.labels.map(l => `<div class="md-v ${l.status === "ACTIVE" ? "" : "retracted"}">
        <div class="row1"><b>#${l.id} · ${esc(l.label_text)}${l.fault_type ? ` (${esc(l.fault_type)})` : ""} · ${esc(l.machine_id)}</b>
          <span class="ad-src ${esc(l.source)}">${l.source === "LIVE" ? "real machine" : esc(l.source.toLowerCase()) + " data"}</span></div>
        <div class="on">${new Date(l.start_ts).toLocaleTimeString()}–${new Date(l.end_ts).toLocaleTimeString()} · ${l.n_samples} samples ·
          by ${esc(l.operator)}${l.alert_id ? ` · alert ${esc(l.alert_id)}` : ""}${l.note ? ` · “${esc(l.note)}”` : ""}${l.status !== "ACTIVE" ? ` · retracted by ${esc(l.retracted_by)}` : ""}</div>
        ${l.status === "ACTIVE" ? `<button class="btn btn-sm" data-retract="${l.id}">Retract</button>` : ""}
      </div>`).join("") : `<div class="sc-hint">No confirmed labels yet.</div>`;
    el("adLabels").querySelectorAll("[data-retract]").forEach(b => b.onclick = () => {
      if (!window.confirm(`Retract label #${b.dataset.retract}? It stays in the audit trail but is no longer used for training.`)) return;
      send(`/api/adaptive/labels/${b.dataset.retract}/retract`, { operator: "dashboard operator" }, `Label #${b.dataset.retract} retracted.`);
    });
  }

  function renderMachines() {
    const sel = el("adMachine");
    const ids = [...new Set([...data.drift.map(d => d.machine_id),
      ...(typeof state !== "undefined" && state.machines ? Object.keys(state.machines) : [])])];
    const key = ids.join();
    if (sel.dataset.ids !== key) {
      const keep = sel.value;
      sel.innerHTML = ids.map(m => `<option>${esc(m)}</option>`).join("");
      sel.dataset.ids = key;
      if (ids.includes(keep)) sel.value = keep;
    }
    if (alertsFor !== sel.value) loadAlerts();
  }

  function loadAlerts() {
    const mid = el("adMachine").value;
    alertsFor = mid;
    fetch("/api/alerts?include_resolved=true&limit=50").then(r => r.json()).then(list => {
      const recent = list.filter(a => a.machine_id === mid && Date.now() - new Date(a.last_seen_at).getTime() < 30 * 60000);
      el("adWindow").innerHTML = [5, 10, 30].map(m => `<option value="min:${m}">Last ${m} min</option>`).join("") +
        recent.map(a => `<option value="alert:${esc(a.alert_id)}">Alert ${new Date(a.raised_at).toLocaleTimeString()}: ${esc(a.title.slice(0, 60))}</option>`).join("");
    }).catch(() => {});
  }

  function render() { renderDrift(); renderLabels(); renderMachines(); }

  function load() {
    fetch("/api/adaptive").then(r => r.json()).then(d => { data = d; render(); }).catch(() => {});
  }

  function send(url, body, okMsg, retried) {
    const headers = { "Content-Type": "application/json" };
    if (operatorToken) headers["X-Operator-Token"] = operatorToken;
    return fetch(url, { method: "POST", headers, body: JSON.stringify(body) }).then(async r => {
      const res = await r.json().catch(() => ({}));
      if (r.status === 401 && !retried) {
        const t = window.prompt("Operator token required to record labels:");
        if (t) { operatorToken = t; return send(url, body, okMsg, true); }
      }
      if (r.status === 401) operatorToken = null;
      if (!r.ok) {
        const d = res.detail;
        return hint(Array.isArray(d) ? d.map(e => e.msg).join("; ") : (d && (d.message || d.error)) || `Refused (${r.status})`, "err");
      }
      hint(typeof okMsg === "function" ? okMsg(res) : okMsg, "ok");
      load();
    }).catch(() => hint("Could not reach the server.", "err"));
  }

  function start() {
    if (!el("adaptivePanel")) return;
    el("adMachine").onchange = loadAlerts;
    el("adConfirm").onclick = () => {
      const [kind, val] = el("adWindow").value.split(":");
      const body = { machine_id: el("adMachine").value, label: el("adLabel").value, operator: "dashboard operator",
        fault_type: el("adFault").value.trim() || null, note: el("adNote").value.trim() || null,
        ...(kind === "alert" ? { alert_id: val } : { minutes: +val }) };
      if (body.label !== "failure") body.fault_type = null;
      send("/api/adaptive/labels", body, res => `Label #${res.id} saved: ${res.label_text}, ${res.n_samples} samples (${res.source === "LIVE" ? "real machine" : res.source.toLowerCase() + " data"}).`);
    };
    load();
    setInterval(load, 5000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
