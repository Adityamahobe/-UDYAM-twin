// Model Registry panel: train a candidate failure-risk model on AI4I + synthetic data, show its evaluation gate
// against the production model, promote a candidate that passed, or roll back. API: /api/models/ai4i*.
// Promote and rollback need an operator (token prompt when the server asks) and are refused in Real Data mode.
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = v => (v == null ? "—" : `${(100 * v).toFixed(1)}%`);
  const num = v => (v == null ? "—" : (+v).toFixed(3));
  const STATUS = { production: "PRODUCTION", candidate: "CANDIDATE · gate passed", rejected_by_gate: "REJECTED BY GATE", archived: "ARCHIVED" };
  let status = null, polling = null, operatorToken = null;
  let checkedSets = null;  // dataset checkboxes the operator ticked (kept across refreshes)

  function hint(msg, tone) {
    const h = el("mdHint");
    h.textContent = msg;
    h.className = `sc-hint${tone ? ` ${tone}` : ""}`;
  }

  function metricsTable(cmp) {
    if (!cmp) return "";
    const p = cmp.production, c = cmp.candidate;
    const row = (label, k, f) => `<tr><td>${label}</td><td>${f(p.ai4i_test[k])}</td><td>${f(c.ai4i_test[k])}</td>
      <td>${f(p.synthetic_test[k])}</td><td>${f(c.synthetic_test[k])}</td></tr>`;
    return `<table class="md-table"><thead><tr><th></th><th>AI4I · ${esc(p.version)}</th><th>AI4I · candidate</th>
      <th>Synthetic · ${esc(p.version)}</th><th>Synthetic · candidate</th></tr></thead><tbody>
      ${row("F1", "f1", num)}${row("Recall", "recall", pct)}${row("Precision", "precision", pct)}
      ${row("ROC-AUC", "roc_auc", num)}${row("False-positive rate", "false_positive_rate", pct)}
      <tr><td>Positives in test</td><td colspan="2">${p.ai4i_test.support_positive}</td><td colspan="2">${p.synthetic_test.support_positive}</td></tr>
      </tbody></table>`;
  }

  function render() {
    const s = status;
    el("mdProd").textContent = `IN PRODUCTION: ${s.production}`;
    const sets = s.synthetic_datasets || [];
    if (checkedSets === null) checkedSets = new Set(sets.map(d => d.dataset_id));
    el("mdDatasets").innerHTML = sets.length ? sets.map(d => `<label title="${esc(d.dataset_id)}">
        <input type="checkbox" value="${esc(d.dataset_id)}" ${checkedSets.has(d.dataset_id) ? "checked" : ""}>
        ${esc(d.name || d.dataset_id)} · ${esc(d.config.machine_id)} · ${d.counts.windows} windows</label>`).join("")
      : `<span class="sc-hint">No synthetic datasets yet: make one in the Synthetic Dataset Generator.</span>`;
    el("mdDatasets").querySelectorAll("input").forEach(i => i.onchange = () => {
      i.checked ? checkedSets.add(i.value) : checkedSets.delete(i.value);
    });
    const g = s.gate_criteria;
    el("mdGate").innerHTML = `
      <li>AI4I test F1 drops by at most ${-g.ai4i_f1_min_delta}, recall by at most ${-g.ai4i_recall_min_delta}, ROC-AUC by at most ${-g.ai4i_roc_auc_min_delta}</li>
      <li>AI4I false-positive rate rises by at most ${100 * g.ai4i_fpr_max_delta} points</li>
      <li>F1 on held-out synthetic episodes improves by at least ${g.synthetic_f1_min_delta}</li>
      <li>At least 20 training and 10 test over-limit synthetic windows, and 10 test windows under the limit</li>
      <li>All compared with the production model on the same held-out rows</li>`;
    el("mdVersions").innerHTML = [...s.versions].reverse().map(v => {
      const checks = (v.gate.checks || []).map(c => `<li class="${c.skipped ? "" : c.passed ? "ok" : "bad"}">${c.skipped ? "–" : c.passed ? "✓" : "✗"} ${esc(c.detail)}: ${esc(c.value)} (${esc(c.rule)})</li>`).join("");
      const canPromote = v.status === "candidate" || (v.status === "archived" && v.gate.passed && v.kind === "candidate");
      return `<div class="md-v ${v.status}">
        <div class="row1"><b>${esc(v.version)} · ${esc(v.kind)}</b><span class="tag ${v.status}">${esc(STATUS[v.status] || v.status)}</span></div>
        <div class="on">Trained on: ${(v.trained_on || []).map(esc).join(" + ")}${v.trained_at ? ` · ${esc(v.trained_at)}` : ""}</div>
        ${v.comparison ? metricsTable(v.comparison) : v.ai4i_test ? `<div class="on">AI4I test: F1 ${num(v.ai4i_test.f1)} · recall ${pct(v.ai4i_test.recall)} · ROC-AUC ${num(v.ai4i_test.roc_auc)}</div>` : ""}
        ${checks ? `<ul class="md-checks">${checks}</ul>` : ""}
        ${canPromote && v.status !== "production" ? `<button class="btn btn-sm btn-primary testing-only" data-promote="${esc(v.version)}">Promote ${esc(v.version)} to production</button>` : ""}
      </div>`;
    }).join("");
    el("mdVersions").querySelectorAll("[data-promote]").forEach(b => b.onclick = () => {
      const v = b.dataset.promote;
      if (!window.confirm(`Deploy ${v} as the failure-risk model the twin uses?\n\nIt passed its evaluation gate on synthetic and AI4I test data. It has not been validated on real machines. You can roll back at any time.`)) return;
      deploy(`/api/models/ai4i/${encodeURIComponent(v)}/promote`, {}, `${v} is now in production.`);
    });
    const job = s.training || {};
    const running = job.state === "running";
    el("mdTrain").disabled = running;
    el("mdJob").textContent = running ? `Training… (${job.step})` : job.state === "failed" ? `Training failed: ${job.error}`
      : job.state === "done" ? `Trained ${job.version}: gate ${job.gate_passed ? "passed" : "failed"}` : "";
    el("mdJob").className = `sc-hint${job.state === "failed" ? " err" : job.state === "done" ? (job.gate_passed ? " ok" : " err") : ""}`;
    if (running && !polling) polling = setInterval(load, 1000);
    if (!running && polling) { clearInterval(polling); polling = null; }
  }

  function load() {
    return fetch("/api/models/ai4i").then(async r => {
      const res = await r.json();
      if (!r.ok) { hint((res.detail && res.detail.message) || "Registry unavailable", "err"); return; }
      status = res; render();
    }).catch(() => {});
  }

  function deploy(url, body, okMsg, retried) {
    const headers = { "Content-Type": "application/json" };
    if (operatorToken) headers["X-Operator-Token"] = operatorToken;
    fetch(url, { method: "POST", headers, body: JSON.stringify({ operator: "dashboard operator", ...body }) })
      .then(async r => {
        const res = await r.json().catch(() => ({}));
        if (r.status === 401 && !retried) {
          const t = window.prompt("Operator token required to change the deployed model:");
          if (t) { operatorToken = t; return deploy(url, body, okMsg, true); }
        }
        if (r.status === 401) operatorToken = null;
        if (!r.ok) return hint(`Not changed: ${(res.detail && (res.detail.message || res.detail.error)) || r.status}`, "err");
        status = res; render(); hint(okMsg, "ok");
        if (typeof fetchMetrics === "function") fetchMetrics();
      }).catch(() => hint("Could not reach the server.", "err"));
  }

  function start() {
    if (!el("modelPanel")) return;
    el("mdTrain").onclick = () => {
      const datasets = [...el("mdDatasets").querySelectorAll("input:checked")].map(i => i.value);
      fetch("/api/models/ai4i/train", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ datasets, synthetic_weight: +el("mdWeight").value, use_confirmed_labels: el("mdUseLabels").checked }) })
        .then(async r => {
          const res = await r.json().catch(() => ({}));
          if (!r.ok) {
            const d = res.detail;
            return hint(Array.isArray(d) ? d.map(e => e.msg).join("; ") : (d && d.message) || `Refused (${r.status})`, "err");
          }
          hint("Training a candidate. Nothing is deployed until you promote it.");
          load();
        });
    };
    el("mdRollback").onclick = () => {
      if (!window.confirm("Roll back the failure-risk model to the previous production version?")) return;
      deploy("/api/models/ai4i/rollback", {}, "Rolled back.");
    };
    load();
    setInterval(() => { if (!polling) load(); }, 10000);  // picks up new synthetic datasets
    window.udyamSetLabelCount = n => { el("mdLabelCount").textContent = n; };
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
