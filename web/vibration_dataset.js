// IMS bearing run-to-failure panel: how the ISO 20816-3 zones and the early-warning model behave on real
// bearings that failed. Everything comes from GET /api/datasets/ims?trends=true (ml/vibration/train_ims.py);
// nothing here is hard-coded. Real recorded data, but from a test rig, not one of the CNC machines.
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const h = v => v == null ? "none" : `${v} h`;
  const MODE = { inner_race: "inner race", outer_race: "outer race", roller_element: "roller element" };

  // velocity RMS on a log scale (0.1 … 30 mm/s) with the zone boundaries drawn in
  function trendChart(t, zones) {
    const w = 240, hgt = 64, lo = Math.log10(0.1), hi = Math.log10(30);
    const xs = t.hours, end = xs[xs.length - 1] || 1;
    const y = v => hgt - 2 - ((Math.log10(Math.max(0.1, Math.min(30, v))) - lo) / (hi - lo)) * (hgt - 4);
    const pts = t.velocity_rms_mm_s.map((v, i) => `${(xs[i] / end * w).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const lines = [["A/B", zones.A_B], ["B/C", zones.B_C], ["C/D", zones.C_D]].map(([n, v]) =>
      `<line x1="0" x2="${w}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="ims-zone ${n === "C/D" ? "cd" : ""}"><title>${n} ${v} mm/s</title></line>`).join("");
    return `<svg viewBox="0 0 ${w} ${hgt}" preserveAspectRatio="none" role="img" aria-label="velocity trend">${lines}
      <polyline points="${pts}" class="rl-line"/></svg>`;
  }

  function render(m) {
    const zones = m.dataset.zone_boundaries_mm_s || { A_B: 1.4, B_C: 2.8, C_D: 4.5 };
    const zp = m.zone_progression, failed = zp.filter(z => z.failed);
    const ew = m.early_warning, rf = ew.random_forest, rules = ew.rules;
    const leads = r => r.lead_times.filter(l => l.failed).map(l => l.first_alarm_hours_before_end);
    const warned = r => leads(r).filter(v => v != null).length;
    const lateZone = failed.map(z => z.first_reached_h.C == null ? null : +(z.duration_h - z.first_reached_h.C).toFixed(1));
    const snaps = Object.values(m.dataset.snapshots_per_test || {}).reduce((a, b) => a + b, 0);
    const tile = (label, value, sub) => `<div class="kpi"><div class="kpi-label">${label}</div><div class="kpi-val">${value}</div><div class="kpi-sub">${sub}</div></div>`;
    const charts = failed.map(z => {
      const key = `T${z.test}-B${z.bearing}`, t = (m.trends || {})[key];
      return `<figure><figcaption>Test ${z.test} · bearing ${z.bearing} · ${esc(MODE[z.failure_mode] || z.failure_mode)}</figcaption>
        ${t ? trendChart(t, zones) : `<div class="rl-chart-empty">no trend</div>`}
        <div class="rl-axis">${z.baseline_vel_mm_s} → max ${z.max_vel_mm_s} mm/s · ends in zone ${esc(z.final_zone)} · ${z.duration_h} h</div></figure>`;
    }).join("");
    const methodRow = (name, r) => `<tr><td>${esc(name)}</td><td>${r.overall.precision}</td><td>${r.overall.recall}</td><td>${r.overall.f1}</td>
      <td>${(100 * (r.overall.false_alarm_rate_healthy_bearings || 0)).toFixed(2)}%</td><td>${leads(r).map(h).join(" · ")}</td></tr>`;
    const zoneRows = zp.map(z => `<tr><td>T${z.test}-B${z.bearing}</td><td>${z.failed ? esc(MODE[z.failure_mode] || z.failure_mode) : "ok"}</td>
      <td>${z.baseline_vel_mm_s}</td><td>${z.max_vel_mm_s}</td><td>${esc(z.final_zone)}</td>
      <td>${["A", "B", "C", "D"].map(k => z.hours_in_zone[k]).join(" / ")}</td><td>${h(z.first_reached_h.C)}</td></tr>`).join("");
    const rulRows = m.rul_check.folds.map(f => `<tr><td>T${f.test}-B${f.bearing}</td><td>${f.mae_h} h</td><td>${f.baseline_mean_mae_h} h</td></tr>`).join("");

    el("imsBody").innerHTML = `
      <div class="kpi-row">
        ${tile("Run-to-failure data", `${zp.length} bearings`, `${failed.length} failed · ${snaps.toLocaleString("en-IN")} one-second snapshots, 3 tests`)}
        ${tile("Zone C reached", `${lateZone.filter(v => v != null).length}/${failed.length} failures`,
          lateZone.map(v => v == null ? "never" : v < 0.1 ? "final snapshot" : `${v} h before`).join(" · "))}
        ${tile("Early-warning model", `${warned(rf)}/${failed.length} alarmed`,
          `first alarm ${leads(rf).map(v => v == null ? "none" : v).join(" / ")} h before failure · recall ${rf.overall.recall}, precision ${rf.overall.precision} (${ew.horizon_h} h window)`)}
        ${tile("False alarms", `${(100 * rf.overall.false_alarm_rate_healthy_bearings).toFixed(2)}%`, "of snapshots on bearings that did not fail")}
      </div>
      <div class="ims-charts">${charts}</div>
      <div class="formula-hint">Velocity RMS (10–1000 Hz, log scale) over each failed bearing's test; dashed lines = ISO 20816-3 zone boundaries
        ${zones.A_B} / ${zones.B_C} / ${zones.C_D} mm/s (demo Group 2 rigid, not verified). On this rig the zones change only at the very end.</div>
      <details class="rl-details"><summary>Early warning vs rules (${esc(ew.validation)})</summary>
        <div class="rl-table-wrap"><table class="rl-table"><tr><th>Method</th><th>Precision</th><th>Recall</th><th>F1</th><th>False alarms</th><th>First alarm before failure</th></tr>
        ${methodRow("Random Forest (baseline-normalised features)", rf)}${methodRow("Rule: zone C or worse", rules.zone_c_or_worse)}${methodRow("Rule: velocity ≥ 2× own baseline", rules.velocity_2x_baseline)}</table></div>
        <div class="formula-hint">Alarm = the method's call held for ${m.method.alarm_persistence_snapshots} snapshots in a row. Lead times are listed per failed bearing (T1-B3, T1-B4, T2-B1, T3-B3).</div>
      </details>
      <details class="rl-details"><summary>Zone progression per bearing</summary>
        <div class="rl-table-wrap"><table class="rl-table"><tr><th>Bearing</th><th>Outcome</th><th>Baseline</th><th>Max</th><th>Final</th><th>h in A / B / C / D</th><th>First zone C</th></tr>${zoneRows}</table></div>
      </details>
      <details class="rl-details"><summary>RUL check (not usable yet)</summary>
        <div class="rl-table-wrap"><table class="rl-table"><tr><th>Held-out bearing</th><th>Model MAE</th><th>Predict-the-mean MAE</th></tr>${rulRows}</table></div>
        <div class="formula-hint">${esc(m.rul_check.target)}; ${esc(m.rul_check.note)}</div>
      </details>
      <div class="formula-hint">${esc(m.label)}. ${esc(m.dataset.source)}; ${esc(m.dataset.credit)}.</div>`;
  }

  function start() {
    if (!el("imsBody")) return;
    fetch("/api/datasets/ims?trends=true").then(r => r.ok ? r.json() : Promise.reject(r.status)).then(render)
      .catch(() => { el("imsBody").innerHTML = `<div class="formula-hint">No IMS results yet: run <code>python -m ml.vibration.datasets.ims</code> then <code>python -m ml.vibration.train_ims</code>.</div>`; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
