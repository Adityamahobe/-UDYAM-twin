// Factory Dataset (Simulate tab): collect labelled whole-factory data from Factory Run Designer designs.
// A recipe is a list of parts (a run design × N runs with its batch variation); train/test is split by whole run.
// API: /api/factory-dataset/* (ml/factory_dataset.py). Everything is SYNTHETIC DIGITAL TWIN DATA.
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nice = s => String(s ?? "").replace(/_/g, " ");
  const API = "/api/factory-dataset";
  let recipe = [], limits = { runs: 100, parts: 10, simulated_minutes: 3000 }, polling = null, current = null;

  function hint(msg, tone) {
    const h = el("fdHint");
    h.textContent = msg;
    h.className = `sc-hint${tone ? ` ${tone}` : ""}`;
  }
  function errText(res, status) {
    const d = res.detail;
    return Array.isArray(d) ? d.map(e => `${(e.loc || []).slice(-1)[0]}: ${e.msg}`).join("; ") : (d && (d.message || d)) || `Refused (${status})`;
  }
  const json = r => r.json().then(res => { if (!r.ok) throw new Error(errText(res, r.status)); return res; });

  // ---------------------------------------------------------------- recipe
  function renderRecipe() {
    const runs = recipe.reduce((s, p) => s + p.runs, 0), mins = recipe.reduce((s, p) => s + p.runs * p.spec.minutes, 0);
    el("fdRecipe").innerHTML = recipe.length ? `<table class="fr-table"><thead><tr><th>Run design</th><th>Events</th><th>Runs</th><th>Variation</th><th></th></tr></thead><tbody>${
      recipe.map((p, i) => `<tr><td>${esc(p.spec.name)} <span class="sc-label">${p.spec.minutes} min · seed ${p.spec.seed} · self-healing ${p.spec.self_healing ? "on" : "off"}</span></td>
        <td>${p.spec.events.length}</td>
        <td><input type="number" class="sc-num" data-runs="${i}" min="1" max="${limits.runs}" value="${p.runs}"></td>
        <td class="sc-label">${p.runs > 1 ? `time ±${p.fault_time_jitter_min} min · severity ${p.severity_range.join("–")} · parameters ±${p.param_jitter_pct} %` : "single run"}</td>
        <td><button class="wi-x" data-del="${i}" aria-label="Remove part">×</button></td></tr>`).join("")}</tbody></table>`
      : `<div class="sc-hint">Empty. Pick a preset or build a run above, set its batch settings, then add it here. Mix several designs (for example normal shifts, compressor failures and bearing wear) so every fault type is represented.</div>`;
    el("fdTotal").textContent = recipe.length ? `${runs} runs · ${mins} simulated min (limit ${limits.runs} runs, ${limits.simulated_minutes} min)` : "";
    el("fdCollect").disabled = !recipe.length;
    el("fdRecipe").querySelectorAll("[data-del]").forEach(b => b.onclick = () => { recipe.splice(+b.dataset.del, 1); renderRecipe(); });
    el("fdRecipe").querySelectorAll("[data-runs]").forEach(inp => inp.onchange = () => {
      recipe[+inp.dataset.runs].runs = Math.max(1, Math.min(limits.runs, Math.round(+inp.value || 1))); renderRecipe();
    });
  }

  function addCurrent() {
    const fr = window.udyamFactoryRun;
    if (!fr) return hint("The Factory Run Designer is not loaded.", "err");
    const { runs, ...v } = fr.variation();
    if (recipe.length >= limits.parts) return hint(`At most ${limits.parts} parts.`, "err");
    recipe.push({ spec: fr.spec(), runs: Math.max(1, runs || 1), ...v });
    renderRecipe();
    hint(`Added "${recipe[recipe.length - 1].spec.name}".`, "ok");
  }

  function collect() {
    const body = { name: el("fdName").value.trim(), test_share: (+el("fdTest").value || 0) / 100,
      split_seed: +el("fdSplitSeed").value || 0, parts: recipe };
    fetch(`${API}/collect`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(json).then(startPolling).catch(e => hint(e.message, "err"));
  }

  function poll() {
    fetch(`${API}/job`).then(r => r.json()).then(j => {
      const running = j.state === "running";
      el("fdCollect").disabled = running || !recipe.length;
      el("fdCancel").hidden = !running;
      if (running) return hint(`Collecting: run ${Math.min(j.done + 1, j.total)} of ${j.total}…`);
      clearInterval(polling); polling = null;
      if (j.state === "done") { hint(`Dataset collected in ${Math.round(j.finished_at - j.started_at)} s.`, "ok"); loadList(j.dataset_id); }
      else if (j.state === "failed") hint(`Collection failed: ${j.error}`, "err");
      else if (j.state === "cancelled") hint("Cancelled: nothing was kept.");
    }).catch(() => {});
  }
  const startPolling = () => { if (!polling) polling = setInterval(poll, 700); poll(); };

  // ---------------------------------------------------------------- library
  const kb = b => b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.round(b / 1e3)} kB`;

  function loadList(open) {
    fetch(`${API}/datasets`).then(r => r.json()).then(d => {
      limits = d.limits || limits;
      renderRecipe();
      el("fdList").innerHTML = d.datasets.length ? `<div class="wi-table-wrap"><table class="wi-table fr-left"><thead><tr><th>Dataset</th><th>Created</th><th>Runs (train / test)</th><th>Windows</th><th>Checks</th><th>Size</th><th></th></tr></thead><tbody>${
        d.datasets.map(x => `<tr class="${current === x.dataset_id ? "fd-sel" : ""}"><td>${esc(x.name || x.dataset_id)}<div class="sc-label">${esc(x.dataset_id)}</div></td>
          <td>${esc((x.created_at || "").replace("T", " ").replace("Z", ""))}</td>
          <td>${x.counts.runs} (${x.counts.train_runs} / ${x.counts.test_runs})</td><td>${x.counts.windows}</td>
          <td class="${x.checks_warn ? "fd-warn" : "fr-ok"}">${x.checks_warn ? `⚠ ${x.checks_warn} to review` : "✓ all pass"}</td><td>${kb(x.bytes)}</td>
          <td class="fd-actions"><button class="btn btn-sm" data-view="${esc(x.dataset_id)}">View</button>
            <a class="btn btn-sm" href="${API}/datasets/${encodeURIComponent(x.dataset_id)}/download?part=zip">Download .zip</a>
            <button class="wi-x" data-rm="${esc(x.dataset_id)}" aria-label="Delete dataset">×</button></td></tr>`).join("")}</tbody></table></div>`
        : `<div class="sc-hint">No factory datasets yet.</div>`;
      el("fdList").querySelectorAll("[data-view]").forEach(b => b.onclick = () => view(b.dataset.view));
      el("fdList").querySelectorAll("[data-rm]").forEach(b => b.onclick = () => udyamConfirm(`Delete dataset ${b.dataset.rm}? Its files are removed from ml/data/factory.`, { ok: "Delete" })
        .then(ok => ok && fetch(`${API}/datasets/${encodeURIComponent(b.dataset.rm)}`, { method: "DELETE" }).then(json)
          .then(() => { if (current === b.dataset.rm) { current = null; el("fdDetail").innerHTML = ""; } loadList(); })
          .catch(e => hint(e.message, "err"))));
      if (open) view(open);
    }).catch(() => {});
  }

  // ---------------------------------------------------------------- one dataset
  function balanceChart(c) {  // per label: train and test windows, side by side (one axis: window count)
    const labels = Object.keys(c.windows_by_label).sort((a, b) => (a === "normal") - (b === "normal") || c.windows_by_label[b] - c.windows_by_label[a]);
    const max = Math.max(1, ...labels.flatMap(l => [c.train_windows_by_label[l] || 0, c.test_windows_by_label[l] || 0]));
    const W = 560, L = 150, R = 56, barH = 9, gap = 2, rowH = 2 * barH + gap + 10, H = labels.length * rowH + 4;
    const w = n => Math.max(n ? 2 : 0, (W - L - R) * n / max);
    const rows = labels.map((l, i) => {
      const y = i * rowH + 2, tr = c.train_windows_by_label[l] || 0, te = c.test_windows_by_label[l] || 0;
      const bar = (n, yy, fill, side) => `<g><rect x="${L}" y="${yy - 2}" width="${W - L}" height="${barH + 4}" fill="transparent"><title>${esc(nice(l))} · ${side}: ${n} windows</title></rect>
        <rect x="${L}" y="${yy}" width="${w(n)}" height="${barH}" rx="2" fill="${fill}" pointer-events="none"/>
        <text x="${L + w(n) + 4}" y="${yy + barH - 1}" font-size="10" fill="var(--text-secondary)" pointer-events="none">${n}</text></g>`;
      return `<text x="${L - 8}" y="${y + barH + 3}" font-size="11" text-anchor="end" fill="var(--text-secondary)">${esc(nice(l))}</text>
        ${bar(tr, y, "var(--wi-1)", "train")}${bar(te, y + barH + gap, "var(--wi-2)", "test")}`;
    }).join("");
    return `<div class="fd-chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Feature windows per label, train and test">${rows}</svg></div>
      <div class="wi-legend"><span><i style="background:var(--wi-1);height:8px"></i>train windows</span><span><i style="background:var(--wi-2);height:8px"></i>test windows</span></div>`;
  }

  function trace(pts, key, title, unit) {  // one series per chart; injected-fault periods shaded and named
    const vals = pts.map(p => p[key] === "" ? null : +p[key]);
    if (!vals.some(v => v != null)) return "";
    const W = 560, L = 44, R = 8, T = 16, B = 18, H = 120;
    const t = pts.map(p => +p.t_s), t1 = t[t.length - 1] || 1;
    const nn = vals.filter(v => v != null), lo = Math.min(...nn), hi = Math.max(...nn), pad = (hi - lo) * 0.08 || 1;
    const x = s => L + (W - L - R) * s / t1, y = v => T + (H - T - B) * (1 - (v - (lo - pad)) / (hi - lo + 2 * pad));
    const bands = [];
    pts.forEach((p, i) => {
      const last = bands[bands.length - 1];
      if (p.label_fault !== "normal") {
        if (last && last.label === p.label_fault && last.i1 === i - 1) last.i1 = i;
        else bands.push({ label: p.label_fault, knock: p.label_knock_on === "1", i0: i, i1: i });
      }
    });
    const shade = bands.map(b => `<rect x="${x(t[b.i0])}" y="${T}" width="${Math.max(1, x(t[b.i1]) - x(t[b.i0]))}" height="${H - T - B}" fill="var(--wi-2)" opacity=".14"/>
      <text x="${x(t[b.i0]) + 3}" y="${T + 10}" font-size="10" fill="var(--text-secondary)">${esc(nice(b.label))}${b.knock ? " (knock-on)" : ""}</text>`).join("");
    let d = "", pen = false;
    vals.forEach((v, i) => { if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(t[i]).toFixed(1)} ${y(v).toFixed(1)}`; pen = true; });
    const ticks = [lo, (lo + hi) / 2, hi].map(v => `<text x="${L - 6}" y="${y(v) + 3}" font-size="10" text-anchor="end" fill="var(--text-muted)">${+v.toFixed(v < 10 ? 2 : 1)}</text>
      <line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--border-subtle)"/>`).join("");
    const axis = [0, t1 / 2, t1].map((s, i) => `<text x="${x(s)}" y="${H - 4}" font-size="10" text-anchor="${["start", "middle", "end"][i]}" fill="var(--text-muted)">${+(s / 60).toFixed(1)} min</text>`).join("");
    return `<div class="fd-trace" data-key="${key}" data-unit="${esc(unit)}"><div class="sc-label">${esc(title)} (${esc(unit)}) <span class="fd-read"></span></div>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)} over the run, injected faults shaded" data-l="${L}" data-r="${R}" data-w="${W}">
      ${ticks}${shade}<path d="${d}" fill="none" stroke="var(--wi-1)" stroke-width="2" stroke-linejoin="round"/>${axis}
      <line class="fd-cross" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="var(--text-muted)" visibility="hidden"/></svg></div>`;
  }

  function hover(root, pts) {  // crosshair + readout of the value and the label at that moment
    root.querySelectorAll(".fd-trace").forEach(div => {
      const svg = div.querySelector("svg"), cross = svg.querySelector(".fd-cross"), read = div.querySelector(".fd-read");
      const L = +svg.dataset.l, R = +svg.dataset.r, W = +svg.dataset.w, t1 = +pts[pts.length - 1].t_s;
      svg.onmousemove = e => {
        const box = svg.getBoundingClientRect(), sx = (e.clientX - box.left) * W / box.width;
        const s = Math.max(0, Math.min(t1, (sx - L) / (W - L - R) * t1));
        const p = pts.reduce((a, b) => Math.abs(+b.t_s - s) < Math.abs(+a.t_s - s) ? b : a);
        const px = L + (W - L - R) * (+p.t_s) / t1;
        cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
        read.textContent = `· ${(+p.t_s / 60).toFixed(2)} min: ${p[div.dataset.key] || "—"} ${div.dataset.unit} · label ${nice(p.label_fault)}`;
      };
      svg.onmouseleave = () => { cross.setAttribute("visibility", "hidden"); read.textContent = ""; };
    });
  }

  function preview(id, runId, machineId) {
    const q = new URLSearchParams({ ...(runId ? { run_id: runId } : {}), ...(machineId ? { machine_id: machineId } : {}) });
    fetch(`${API}/datasets/${encodeURIComponent(id)}/preview?${q}`).then(json).then(p => {
      const box = el("fdPreview");
      if (!box) return;
      box.innerHTML = `<div class="control-bar">
          <label class="sc-label">run <select id="fdPrevRun">${p.runs.map(r => `<option ${r === p.run_id ? "selected" : ""}>${esc(r)}</option>`).join("")}</select></label>
          <label class="sc-label">machine <select id="fdPrevMachine">${p.machines.map(m => `<option ${m === p.machine_id ? "selected" : ""}>${esc(m)}</option>`).join("")}</select></label>
          <span class="sc-hint">${esc(p.run_id)} is a ${p.split} run · readings every 2 s · shaded: injected ground truth</span></div>
        ${trace(p.points, "temp_c", "Temperature", "°C")}${trace(p.points, "vibration_mm_s", "Vibration", "mm/s")}
        ${trace(p.points, "pressure_bar", "Air pressure", "bar")}${trace(p.points, "rpm", "Speed", "rpm")}`;
      hover(box, p.points);
      el("fdPrevRun").onchange = el("fdPrevMachine").onchange = () => preview(id, el("fdPrevRun").value, el("fdPrevMachine").value);
    }).catch(e => { if (el("fdPreview")) el("fdPreview").innerHTML = `<div class="sc-hint err">${esc(e.message)}</div>`; });
  }

  function view(id) {
    current = id;
    fetch(`${API}/datasets/${encodeURIComponent(id)}`).then(json).then(m => {
      el("fdList").querySelectorAll("tr").forEach(tr => tr.classList.toggle("fd-sel", !!tr.querySelector(`[data-view="${CSS.escape(id)}"]`)));
      const c = m.counts, dl = (part, split) => `${API}/datasets/${encodeURIComponent(id)}/download?part=${part}${split ? `&split=${split}` : ""}`;
      const icon = { pass: "✓", warn: "⚠", fail: "✕" };
      const files = ["windows", "readings", "line", "events", "labels", "runs"].map(p => `<tr><td>${p}.csv</td><td>${kb(m.files[`${p}.csv`].bytes)}</td>
        <td><a href="${dl(p)}">all</a> · <a href="${dl(p, "train")}">train</a> · <a href="${dl(p, "test")}">test</a></td></tr>`).join("");
      el("fdDetail").innerHTML = `<div class="fd-detail">
        <div class="sc-hint">${esc(m.data_label)} · ${esc(m.name || m.dataset_id)} · ${c.runs} runs (${c.train_runs} train, ${c.test_runs} test) · ${c.simulated_minutes} simulated min · collected in ${m.generated_in_s} s</div>
        <div class="fr-kpis"><span>Readings <b>${c.readings}</b></span><span>Feature windows <b>${c.windows}</b></span><span>Line rows <b>${c.line_rows}</b></span>
          <span>Injected faults <b>${c.injected_faults}</b></span><span>Knock-on windows <b>${c.knock_on_windows}</b></span><span>Windows ≤ 10 min to a limit <b>${c.windows_within_10min_of_limit}</b></span></div>
        <div class="fd-cols"><div><div class="synth-sub">Checks</div><ul class="fd-checks">${m.checks.map(k => `<li class="fd-${k.status}"><b>${icon[k.status]} ${esc(k.check)}</b> ${esc(k.detail)}</li>`).join("")}</ul></div>
          <div><div class="synth-sub">Label balance (feature windows)</div>${balanceChart(c)}
            <details class="wk-more"><summary>As a table</summary><table class="wi-table fr-left"><thead><tr><th>Label</th><th>Train</th><th>Test</th></tr></thead><tbody>${
            Object.keys(c.windows_by_label).map(l => `<tr><td>${esc(nice(l))}</td><td>${c.train_windows_by_label[l] || 0}</td><td>${c.test_windows_by_label[l] || 0}</td></tr>`).join("")}</tbody></table></details></div></div>
        <div class="synth-sub">Preview</div><div id="fdPreview"><div class="sc-hint">Loading…</div></div>
        <div class="synth-sub">Files</div><div class="wi-table-wrap"><table class="wi-table fr-left"><thead><tr><th>File</th><th>Size</th><th>Download CSV</th></tr></thead><tbody>${files}</tbody></table></div>
        <div class="control-bar"><a class="btn btn-sm btn-primary" href="${dl("zip")}">Download everything (.zip with metadata)</a><a class="btn btn-sm" href="${dl("metadata")}">metadata.json</a></div>
        <div class="sc-hint">Split by whole run: test runs (${esc(m.split.test.join(", ") || "none")}) are never in the training files.</div></div>`;
      preview(id);
    }).catch(e => hint(e.message, "err"));
  }

  function start() {
    if (!el("factoryDatasetPanel")) return;
    el("fdAdd").onclick = addCurrent;
    el("fdCollect").onclick = collect;
    el("fdCancel").onclick = () => fetch(`${API}/job/cancel`, { method: "POST" });
    renderRecipe();
    loadList();
    fetch(`${API}/job`).then(r => r.json()).then(j => { if (j.state === "running") startPolling(); }).catch(() => {});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
