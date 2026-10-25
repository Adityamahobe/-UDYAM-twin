// Synthetic Dataset Generator panel: configure, generate (background job with progress), list and download
// SYNTHETIC DIGITAL TWIN DATA as CSV or JSON. API: /api/synthetic/* (api/synthetic.py).
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const FAULT_LABEL = {
    normal: "Normal (no fault)", coolant_degradation: "Coolant degradation", bearing_defect: "Bearing defect",
    overload: "Overload", tool_wear_accel: "Fast tool wear", sensor_stuck: "Sensor stuck", sensor_dropout: "Sensor dropout",
    pressure_drop: "Air pressure drop", order_surge: "Order surge",
  };
  let opts = null;
  let polling = null;

  function hint(msg, tone) {
    const h = el("synHint");
    h.textContent = msg;
    h.className = `sc-hint${tone ? ` ${tone}` : ""}`;
  }

  function renderRanges() {
    const m = opts.machines.find(x => x.machine_id === el("synMachine").value);
    const dec = s => (s.step < 1 ? 1 : 0);
    el("synRanges").innerHTML = m.params.map(s => `
      <div class="synth-range" data-key="${s.key}">
        <span>${esc(s.label)} (${esc(s.unit)})</span>
        <div class="pair">
          <input type="number" class="lo" min="${s.min}" max="${s.max}" step="${s.step}" value="${s.default.toFixed(dec(s))}" aria-label="${esc(s.label)} minimum">
          <span>–</span>
          <input type="number" class="hi" min="${s.min}" max="${s.max}" step="${s.step}" value="${s.default.toFixed(dec(s))}" aria-label="${esc(s.label)} maximum">
        </div>
        <small>limit ${s.min}–${s.max} · equal values = fixed</small>
      </div>`).join("");
  }

  function config() {
    const ranges = {};
    el("synRanges").querySelectorAll(".synth-range").forEach(r => {
      const lo = +r.querySelector(".lo").value, hi = +r.querySelector(".hi").value;
      ranges[r.dataset.key] = [lo, hi];
    });
    return {
      machine_id: el("synMachine").value, episodes: +el("synEpisodes").value, minutes: +el("synMinutes").value,
      seed: +el("synSeed").value, auto_derate_share: +el("synDerate").value, name: el("synName").value.trim(),
      faults: [...el("synFaults").querySelectorAll("input:checked")].map(i => i.value), param_ranges: ranges,
    };
  }

  function errText(res, status) {
    const d = res.detail;
    if (Array.isArray(d)) return d.map(e => `${(e.loc || []).slice(-1)[0]}: ${e.msg}`).join("; ");
    return (d && (d.message || d)) || `Refused (${status})`;
  }

  function showJob(j) {
    const running = j && j.state === "running";
    el("synGenerate").disabled = running;
    el("synCancel").hidden = !running;
    el("synProgress").hidden = !running;
    if (running) {
      el("synBar").style.width = `${Math.round(100 * j.done / j.total)}%`;
      hint(`Generating ${j.dataset_id}: episode ${j.done} of ${j.total}…`);
    }
    return running;
  }

  function poll() {
    fetch("/api/synthetic/job").then(r => r.json()).then(j => {
      if (showJob(j)) return;
      clearInterval(polling); polling = null;
      if (j.state === "done") hint(`Done: ${j.dataset_id}. Download it below.`, "ok");
      else if (j.state === "failed") hint(`Generation failed: ${j.error}`, "err");
      else if (j.state === "cancelled") hint("Cancelled. Nothing was kept.");
      loadList();
    }).catch(() => {});
  }

  function startPolling() {
    if (!polling) polling = setInterval(poll, 700);
    poll();
  }

  function loadList() {
    fetch("/api/synthetic/datasets").then(r => r.json()).then(res => {
      const list = res.datasets || [];
      el("synList").innerHTML = list.length ? list.map(d => {
        const c = d.counts || {}, cfg = d.config || {};
        const labels = Object.entries(c.windows_by_label || {}).map(([k, v]) => `${k} ${v}`).join(" · ");
        const dl = (part, fmt, text) => `<a class="btn btn-sm" href="/api/synthetic/datasets/${encodeURIComponent(d.dataset_id)}/download?part=${part}&format=${fmt}">${text}</a>`;
        return `<div class="synth-item">
          <div class="row1"><b>${esc(d.name || d.dataset_id)}</b><span class="synth-label">${esc(d.data_label)}</span></div>
          <div class="row2">${esc(d.dataset_id)} · ${esc(cfg.machine_id)} · ${c.episodes} episodes × ${cfg.minutes} min · seed ${cfg.seed} ·
            ${c.windows} windows (${esc(labels)}) · ${new Date(d.created_at).toLocaleString()}
            ${c.episodes_over_limit_from_settings ? `<br><span class="rb-warn">${c.episodes_over_limit_from_settings} of ${c.episodes} episodes start over the temperature limit from the sampled settings alone (not from a fault; see over_limit_from_settings).</span>` : ""}</div>
          <div class="acts">${dl("windows", "csv", "Windows CSV")}${dl("episodes", "csv", "Episodes CSV")}${dl("windows", "json", "Windows JSON")}${dl("episodes", "json", "Episodes JSON")}${dl("metadata", "json", "Metadata")}
            <button class="btn btn-sm" data-del="${esc(d.dataset_id)}">Delete</button></div>
        </div>`;
      }).join("") : `<div class="sc-hint">No synthetic datasets yet.</div>`;
      el("synList").querySelectorAll("[data-del]").forEach(b => b.onclick = () => {
        udyamConfirm(`Delete ${b.dataset.del}?\n\nThe files are removed from ml/data/synthetic.`, { ok: "Delete", danger: true })
          .then(ok => { if (ok) fetch(`/api/synthetic/datasets/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }).then(loadList); });
      });
    }).catch(() => {});
  }

  function start() {
    if (!el("synthPanel")) return;
    fetch("/api/synthetic/options").then(r => r.json()).then(o => {
      opts = o;
      el("synMachine").innerHTML = o.machines.map(m => `<option value="${esc(m.machine_id)}">${esc(m.machine_id)} (${esc(m.template.replace("_", " "))})</option>`).join("");
      el("synFaults").innerHTML = o.faults.map(f =>
        `<label><input type="checkbox" value="${esc(f)}" checked> ${esc(FAULT_LABEL[f] || f)}</label>`).join("");
      el("synEpisodes").max = o.limits.episodes;
      el("synMinutes").max = o.limits.minutes;
      renderRanges();
      el("synMachine").onchange = renderRanges;
    });
    el("synGenerate").onclick = () => {
      fetch("/api/synthetic/generate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config()) })
        .then(async r => {
          const res = await r.json().catch(() => ({}));
          if (!r.ok) return hint(errText(res, r.status), "err");
          startPolling();
        }).catch(() => hint("Could not reach the server.", "err"));
    };
    el("synCancel").onclick = () => fetch("/api/synthetic/job/cancel", { method: "POST" });
    fetch("/api/synthetic/job").then(r => r.json()).then(j => { if (j.state === "running") startPolling(); });
    loadList();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
