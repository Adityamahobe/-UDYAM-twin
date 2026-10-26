// Factory Learning panel (Data & AI tab): train fault diagnosis, time-to-limit and RL policy candidates on factory
// datasets, show each candidate's held-out results and evaluation gate, promote one that passed, or roll back.
// API: /api/factory-learning* (ml/factory_learning.py). Promote and rollback need an operator and Testing mode.
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nice = s => String(s ?? "").replace(/_/g, " ");
  const pct = v => (v == null ? "—" : `${(100 * v).toFixed(1)}%`);
  const num = (v, d = 3) => (v == null ? "—" : (+v).toFixed(d));
  const inr = v => (v == null ? "—" : `₹${Math.round(v).toLocaleString("en-IN")}`);
  const STATUS = { production: "PRODUCTION", candidate: "CANDIDATE · gate passed", rejected_by_gate: "REJECTED BY GATE", archived: "ARCHIVED" };
  const WHAT = {
    fault: "Which fault a machine has, from its last 60 s, including the compressor's pressure drop seen on the CNCs.",
    ttf: "Minutes until a machine reaches its temperature limit or vibration zone D (up to 30), and whether within 10 min.",
    rl: "The maintenance policy (continue, derate, inspect, maintain), fine-tuned from the current one on the datasets' fault scenarios.",
  };
  let status = null, polling = null, operatorToken = null, checked = null;
  const metrics = {};   // "kind/version" -> metrics.json

  function hint(msg, tone) {
    const h = el("flHint");
    h.textContent = msg;
    h.className = `sc-hint${tone ? ` ${tone}` : ""}`;
  }

  // ---------------------------------------------------------------- held-out results per kind
  function table(cols, rows) {
    return `<table class="md-table"><thead><tr><th></th>${cols.map(c => `<th>${esc(c[0])}</th>`).join("")}</tr></thead><tbody>${
      rows.map(([label, f]) => `<tr><td>${esc(label)}</td>${cols.map(c => `<td>${c[1] ? f(c[1]) : "—"}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }

  function results(kind, m) {
    const r = m.results;
    if (kind === "fault") {
      const cols = [["candidate", r.candidate], [r.production ? `prod ${r.production.version}` : "production", r.production], ["always normal", r.majority_baseline]];
      const faults = r.labels.filter(l => l !== "normal" && r.candidate.per_label[l].support > 0);
      return table(cols, [["Macro F1", x => num(x.macro_f1)], ["Accuracy", x => pct(x.accuracy)],
        ["Fault named right", x => pct(x.fault_named_correctly)], ["Right machine blamed", x => pct(x.cause_machine_correct)],
        ...faults.map(f => [`Recall · ${nice(f)}`, x => pct(x.per_label[f] && x.per_label[f].recall)])])
        + `<div class="fl-what">${r.candidate.rows} held-out windows from ${m.split.test_runs} test runs (${r.candidate.fault_windows} with a fault).</div>`;
    }
    if (kind === "ttf") {
      const cols = [["candidate", r.candidate], [r.production ? `prod ${r.production.version}` : "production", r.production],
        ["twin forecast", r.twin_forecast_baseline], ["always 30", r.always_censored_baseline]];
      return table(cols, [["Error, min (reaching a limit)", x => num(x.mae_reaching_limit_min, 1)],
        ["≤ 10 min: recall", x => pct(x.within_10min.recall)], ["≤ 10 min: precision", x => pct(x.within_10min.precision)],
        ["≤ 10 min: F1", x => num(x.within_10min.f1)]])
        + `<div class="fl-what">${r.candidate.rows} held-out windows, ${r.candidate.within_10min.positives} within 10 min of a limit. "Twin forecast" is the twin's physics forecast of its temperature warning.</div>`;
    }
    const cols = [["candidate", r.candidate.all], [`current ${r.production.version}`, r.production.all], ["rules", r.rule_based.all],
      ["rules + inspect", r.rule_with_inspection.all], ["always continue", r.always_continue.all]];
    return table(cols, [["₹ per shift", x => inr(x.return_inr)], ["Trips", x => num(x.trips, 2)], ["Parts", x => num(x.parts, 1)],
      ["Zone D (s)", x => num(x.zone_d_s, 1)], ["Maint. stops", x => num(x.maintenance_stops, 2)], ["Inspections", x => num(x.inspections, 2)]])
      + `<div class="fl-what">Means per 12-min shift over ${r.candidate.all.shifts} held-out shifts (${r.scenarios.test} test-run scenarios × held-out seeds). Simulation only.</div>`;
  }

  function loadMetrics(kind, version) {
    const key = `${kind}/${version}`;
    if (metrics[key]) return;
    metrics[key] = "loading";
    fetch(`/api/factory-learning/${kind}/${encodeURIComponent(version)}/metrics`).then(r => r.json())
      .then(m => { metrics[key] = m; render(); }).catch(() => { delete metrics[key]; });
  }

  // ---------------------------------------------------------------- panel
  function gateList(kind, g) {
    if (kind === "fault") return `<li>Macro F1 ≥ ${g.macro_f1_min} and ≥ ${g.macro_f1_vs_majority_min_delta} above always answering "normal"</li>
      <li>Every fault with ≥ ${g.min_test_windows_per_fault} held-out windows: recall ≥ ${pct(g.fault_recall_min)}</li>
      <li>Macro F1 at most ${-g.macro_f1_vs_production_min_delta} below the production model on the same rows</li>`;
    if (kind === "ttf") return `<li>≥ ${g.min_train_positives} train and ${g.min_test_positives} test windows within 10 min of a limit</li>
      <li>Within 10 min: recall ≥ ${pct(g.within_10min_recall_min)}, precision ≥ ${pct(g.within_10min_precision_min)}</li>
      <li>Error in minutes no worse than the twin's own forecast; F1 at most ${-g.within_10min_f1_vs_production_min_delta} below production</li>`;
    return `<li>≥ ${g.min_train_scenarios} training scenarios and ≥ ${g.min_test_fault_scenarios} held-out scenarios with a fault</li>
      <li>₹ per shift on held-out scenarios at least the current policy's and the rule baseline's</li>
      <li>Trips per shift rise by at most ${g.trips_vs_production_max_delta}</li>`;
  }

  function kindCard(kind, s, job) {
    const running = job.state === "running";
    const versions = [...s.versions].reverse().map(v => {
      const m = metrics[`${kind}/${v.version}`];
      if (!m && v.kind === "candidate") loadMetrics(kind, v.version);
      const checks = (v.gate.checks || []).map(c => `<li class="${c.skipped ? "" : c.passed ? "ok" : "bad"}">${c.skipped ? "–" : c.passed ? "✓" : "✗"} ${esc(c.detail)}: ${esc(c.value)} (${esc(c.rule)})</li>`).join("");
      const canPromote = v.status === "candidate" || (v.status === "archived" && v.gate.passed);
      return `<div class="md-v ${v.status}">
        <div class="row1"><b>${esc(v.version)} · ${esc(v.kind)}</b><span class="tag ${v.status}">${esc(STATUS[v.status] || v.status)}</span></div>
        <div class="on">${v.kind === "baseline" ? esc(v.summary) : `Trained on: ${(v.trained_on || []).map(esc).join(" + ")} · ${esc(v.trained_at || "")}`}</div>
        ${m && m !== "loading" ? results(kind, m) : ""}
        ${checks ? `<details class="md-gate" ${v.status !== "archived" ? "open" : ""}><summary>Gate ${v.gate.passed ? "passed" : "failed"}</summary><ul class="md-checks">${checks}</ul></details>` : ""}
        ${canPromote && v.status !== "production" ? `<button class="btn btn-sm btn-primary testing-only" data-promote="${kind}/${esc(v.version)}">Promote ${esc(v.version)} to production</button>` : ""}
      </div>`;
    }).join("") || `<div class="sc-hint">No versions yet.</div>`;
    return `<div class="fl-kind" data-kind="${kind}">
      <h3>${esc(s.title)} <span class="badge-sim">IN PRODUCTION: ${esc(s.production || "none")}</span></h3>
      <div class="fl-what">${esc(WHAT[kind])}</div>
      <div class="control-bar">
        ${kind === "rl" ? `<label class="sc-label">shifts <input type="number" class="sc-num" id="flEpisodes" min="200" max="20000" step="100" value="1500"></label>` : ""}
        <button class="btn btn-sm btn-primary" data-train="${kind}" ${running ? "disabled" : ""}>Train candidate</button>
        <button class="btn btn-sm testing-only" data-rollback="${kind}" ${s.history.some(h => h.action === "promote") ? "" : "disabled"}>Roll back</button>
      </div>
      ${running && job.kind === kind ? `<div class="sc-hint">Training… (${esc(job.step)})</div>` : ""}
      <details class="md-gate"><summary>Evaluation gate (fixed before training)</summary><ul>${gateList(kind, s.gate_criteria)}</ul></details>
      <div class="md-versions">${versions}</div></div>`;
  }

  function render() {
    const s = status;
    if (!s) return;
    if (checked === null) checked = new Set(s.datasets.slice(0, 1).map(d => d.dataset_id));
    el("flDatasets").innerHTML = s.datasets.length ? s.datasets.map(d => `<label title="${esc(d.dataset_id)}">
        <input type="checkbox" value="${esc(d.dataset_id)}" ${checked.has(d.dataset_id) ? "checked" : ""}>
        ${esc(d.name || d.dataset_id)} · ${d.counts.runs} runs (${d.counts.test_runs} test) · ${d.counts.windows} windows</label>`).join("")
      : `<span class="sc-hint">No factory datasets yet: collect one in the Simulate tab (Factory Dataset).</span>`;
    el("flDatasets").querySelectorAll("input").forEach(i => i.onchange = () => { i.checked ? checked.add(i.value) : checked.delete(i.value); });
    const episodes = el("flEpisodes") ? el("flEpisodes").value : null;
    const open = new Set([...el("flKinds").querySelectorAll("details[open]")].map(d => d.querySelector("summary").textContent));
    el("flKinds").innerHTML = Object.entries(s.models).map(([k, m]) => kindCard(k, m, s.training)).join("");
    if (episodes && el("flEpisodes")) el("flEpisodes").value = episodes;
    el("flKinds").querySelectorAll("details.md-gate").forEach(d => { if (open.has(d.querySelector("summary").textContent) && d.querySelector("summary").textContent.startsWith("Evaluation")) d.open = true; });
    el("flKinds").querySelectorAll("[data-train]").forEach(b => b.onclick = () => train(b.dataset.train));
    el("flKinds").querySelectorAll("[data-promote]").forEach(b => b.onclick = () => {
      const [kind, v] = b.dataset.promote.split("/");
      udyamConfirm(`Deploy ${v} as the ${s.models[kind].title} model?\n\nIt passed its evaluation gate on held-out simulated runs. It has not been validated on real machines.${kind === "rl" ? " The live RL recommendations will use it (they stay advice: nothing is applied without an operator)." : " The live readout below will use it (read-only)."} You can roll back at any time.`,
        { ok: `Deploy ${v}` }).then(ok => { if (ok) deploy(`/api/factory-learning/${kind}/${encodeURIComponent(v)}/promote`, `${v} is now in production.`); });
    });
    el("flKinds").querySelectorAll("[data-rollback]").forEach(b => b.onclick = () => {
      const kind = b.dataset.rollback;
      udyamConfirm(`Roll back the ${s.models[kind].title} model to its previous production version?`, { ok: "Roll back" })
        .then(ok => { if (ok) deploy(`/api/factory-learning/${kind}/rollback`, "Rolled back."); });
    });
    const job = s.training;
    if (job.state === "running" && !polling) polling = setInterval(load, 1000);
    if (job.state !== "running" && polling) {
      clearInterval(polling); polling = null;
      if (job.state === "done") hint(`Trained ${s.models[job.kind].title} ${job.version}: gate ${job.gate_passed ? "passed. Promote it to deploy it" : "failed, so it cannot be deployed"}.`, job.gate_passed ? "ok" : "err");
      if (job.state === "failed") hint(`Training failed: ${job.error}`, "err");
    }
  }

  function load() {
    return fetch("/api/factory-learning").then(r => r.json()).then(res => { status = res; render(); }).catch(() => {});
  }

  function train(kind) {
    const datasets = [...checked];
    const body = { datasets, ...(kind === "rl" && el("flEpisodes") ? { episodes: +el("flEpisodes").value } : {}) };
    fetch(`/api/factory-learning/${kind}/train`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(async r => {
        const res = await r.json().catch(() => ({}));
        if (!r.ok) {
          const d = res.detail;
          return hint(Array.isArray(d) ? d.map(e => `${(e.loc || []).slice(-1)[0]}: ${e.msg}`).join("; ") : (d && d.message) || `Refused (${r.status})`, "err");
        }
        hint("Training a candidate on the training runs. Nothing is deployed until you promote it.");
        load();
      });
  }

  function deploy(url, okMsg, retried) {
    const headers = { "Content-Type": "application/json" };
    if (operatorToken) headers["X-Operator-Token"] = operatorToken;
    fetch(url, { method: "POST", headers, body: JSON.stringify({ operator: "dashboard operator" }) })
      .then(async r => {
        const res = await r.json().catch(() => ({}));
        if (r.status === 401 && !retried) {
          const t = await udyamPrompt("Operator token required to change a deployed model", { password: true, ok: "Continue" });
          if (t) { operatorToken = t; return deploy(url, okMsg, true); }
        }
        if (r.status === 401) operatorToken = null;
        if (!r.ok) return hint(`Not changed: ${(res.detail && (res.detail.message || res.detail.error)) || r.status}`, "err");
        status = res; render(); hint(okMsg, "ok"); live();
      }).catch(() => hint("Could not reach the server.", "err"));
  }

  // ---------------------------------------------------------------- live readout
  function live() {
    if (!el("flLive") || el("factoryLearningPanel").closest("[hidden]")) return;
    fetch("/api/factory-learning/live").then(r => r.json()).then(d => {
      if (!d.models || (!d.models.fault && !d.models.ttf)) {
        el("flLive").innerHTML = `<div class="sc-hint">${esc(d.models ? "No fault or time-to-limit model is in production yet: promote one that passed its gate." : d.note)}</div>`;
        return;
      }
      el("flLive").innerHTML = `<table class="wi-table fr-left"><thead><tr><th>Machine</th><th>Diagnosis ${esc(d.models.fault || "")}</th><th>Cause</th><th>Time to limit ${esc(d.models.ttf || "")}</th></tr></thead><tbody>${
        d.machines.map(m => `<tr><td>${esc(m.machine_id)}</td>${m.status ? `<td colspan="3" class="fl-p">${esc(m.status)}</td>` : `
          <td>${m.fault ? `<b class="${m.fault.label === "normal" ? "fr-ok" : "fd-warn"}">${esc(nice(m.fault.label))}</b> <span class="fl-p">${pct(m.fault.probability)}${m.fault.top.length > 1 ? ` · next: ${esc(nice(m.fault.top[1].label))} ${pct(m.fault.top[1].p)}` : ""}</span>` : "—"}</td>
          <td>${m.fault && m.fault.cause_machine ? esc(m.fault.cause_machine) : "—"}</td>
          <td>${m.ttf ? (m.ttf.capped ? "> 30 min" : `<span class="${m.ttf.within_10min ? "fd-warn" : ""}">${num(m.ttf.minutes_to_limit, 1)} min</span>`) : "—"}</td>`}</tr>`).join("")}</tbody></table>
        <div class="sc-hint">${esc(d.note)}</div>`;
    }).catch(() => {});
  }

  function start() {
    if (!el("factoryLearningPanel")) return;
    load();
    live();
    setInterval(() => { if (!polling) load(); }, 15000);
    setInterval(live, 3000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
