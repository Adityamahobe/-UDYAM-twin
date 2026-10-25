// NASA C-MAPSS turbofan RUL panel. Everything comes from GET /api/datasets/cmapss?predictions=true
// (ml/cmapss.py); nothing is hard-coded. A separate research experiment on a turbofan simulation:
// it never feeds the CNC machines' RUL.
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const MODEL = { gradient_boosting: "Gradient boosting", linear_regression: "Linear (ridge)", predict_training_mean: "Predict the mean" };
  let data = null, selected = "FD001";

  // official test engines sorted by true RUL: line = true RUL, dots = predicted RUL
  function chart(p) {
    const w = 300, h = 90, n = p.true_rul.length, max = Math.max(...p.true_rul, ...p.predicted_rul, 1);
    const x = i => (i / Math.max(1, n - 1)) * w, y = v => h - 3 - (v / max) * (h - 6);
    const line = p.true_rul.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const dots = p.predicted_rul.map((v, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="1.6"><title>true ${p.true_rul[i]} · predicted ${v}</title></circle>`).join("");
    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" class="cm-chart" role="img" aria-label="true vs predicted RUL">
      <line x1="0" x2="${w}" y1="${y(125).toFixed(1)}" y2="${y(125).toFixed(1)}" class="rl-zero"><title>125-cycle cap</title></line>
      <polyline points="${line}" class="cm-true"/>${dots}</svg>`;
  }

  function render() {
    const subs = data.subsets, s = subs.find(x => x.subset === selected) || subs[0];
    const gb = s.test.gradient_boosting, lin = s.test.linear_regression, mean = s.test.predict_training_mean;
    const tile = (label, value, sub) => `<div class="kpi"><div class="kpi-label">${label}</div><div class="kpi-val">${value}</div><div class="kpi-sub">${sub}</div></div>`;
    const rows = subs.map(x => `<tr${x.subset === s.subset ? ' class="cm-sel"' : ""}><td>${x.subset}</td><td>${esc(x.description)}</td>
      ${["gradient_boosting", "linear_regression", "predict_training_mean"].map(k => `<td class="${k === "gradient_boosting" ? "win" : ""}">${x.test[k].rmse}</td>`).join("")}
      <td>${x.test.gradient_boosting.rmse_true_capped_125}</td><td>${x.test.gradient_boosting.nasa_score.toLocaleString("en-IN")}</td>
      <td>${x.cv_rmse_capped_target.mean} ± ${x.cv_rmse_capped_target.std}</td></tr>`).join("");
    el("cmapssBody").innerHTML = `
      <div class="sev-chips" role="tablist">${subs.map(x => `<button type="button" class="sev-chip${x.subset === s.subset ? " active" : ""}" data-sub="${x.subset}">${x.subset}</button>`).join("")}</div>
      <div class="kpi-row">
        ${tile(`${s.subset} test RMSE`, `${gb.rmse} cycles`, `gradient boosting · ${gb.rmse_true_capped_125} with true RUL capped at 125`)}
        ${tile("vs baselines", `${lin.rmse} · ${mean.rmse}`, "linear regression · predict the mean (RMSE, cycles)")}
        ${tile("PHM08 score", gb.nasa_score.toLocaleString("en-IN"), `lower is better; late predictions cost more (linear ${lin.nasa_score.toLocaleString("en-IN")})`)}
        ${tile("Engines", `${s.train_engines} / ${s.test_engines}`, `train / official test · ${esc(s.description)}`)}
      </div>
      <figure class="cm-fig"><figcaption>${s.subset}: official test engines sorted by true RUL — line = true, dots = predicted (gradient boosting); dashed = 125-cycle cap</figcaption>
        ${s.predictions ? chart(s.predictions) : `<div class="rl-chart-empty">no predictions</div>`}</figure>
      <details class="rl-details"><summary>All subsets (RMSE in cycles, official test engines)</summary>
        <div class="rl-table-wrap"><table class="rl-table rl-eval"><tr><th>Subset</th><th>Conditions / faults</th><th>Boosting</th><th>Linear</th><th>Mean</th><th>Boosting, capped</th><th>PHM08 score</th><th>CV RMSE</th></tr>${rows}</table></div>
      </details>
      <details class="rl-details"><summary>Method</summary><div class="rl-method">
        ${Object.entries(data.method).map(([k, v]) => `<div><b>${esc(k)}:</b> ${esc(Array.isArray(v) ? v.join("; ") : v)}</div>`).join("")}
        <div>Settings were fixed before looking at the test engines. On FD002/FD004 many test engines have more than 125 cycles left, which a capped model cannot predict; that is why the uncapped RMSE is much higher there.</div></div>
      </details>
      <div class="formula-hint">${esc(data.label)}. ${esc(data.source)}; ${esc(data.reference)}.</div>`;
    el("cmapssBody").querySelectorAll("[data-sub]").forEach(b => b.onclick = () => { selected = b.dataset.sub; render(); });
  }

  function start() {
    if (!el("cmapssBody")) return;
    fetch("/api/datasets/cmapss?predictions=true").then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(m => { data = m; render(); })
      .catch(() => { el("cmapssBody").innerHTML = `<div class="formula-hint">No C-MAPSS results yet: run <code>python -m ml.cmapss</code>.</div>`; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
