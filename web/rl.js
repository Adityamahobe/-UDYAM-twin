// Reinforcement-learning maintenance policy panel. Reads GET /api/rl and /api/rl/metrics.
// Every number shown comes from those two endpoints; nothing is hard-coded. Labels keep apart:
//   - simulated training / held-out evaluation (models/rl_metrics.json),
//   - the live run (decisions evaluated on the simulator stream),
//   - predictions (Q-values, predicted risk) made before the outcome was known.
// The policy only recommends: "Send for approval" creates a proposal that an operator approves
// through the same authorized workflow as the Advisor (POST /api/chat/proposals/{id}/approve).
(function () {
  const ACTIONS = ["continue", "derate_20", "derate_40", "maintain"];
  const SHORT = { continue: "Continue", derate_20: "Derate 20%", derate_40: "Derate 40%", maintain: "Maintain" };
  const POLICY_NAME = { always_continue: "Always continue", rule_based: "Rule-based (twin today)", rl_policy: "RL policy" };
  let metrics = null, operatorToken = null, pending = null;
  const openDetails = new Set();

  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inr = v => v == null ? "–" : "₹" + Math.round(v).toLocaleString("en-IN");
  const pct = v => v == null ? "–" : (100 * v).toFixed(0) + "%";
  const time = ts => ts ? new Date(ts).toLocaleTimeString() : "";

  function describe(state) {
    const [m, tr, vz, load, tw, d] = (state || "").split("|");
    const margin = { m0: "above temp limit", m1: "< 1 °C to limit", m2: "1–3 °C to limit", m3: "> 3 °C to limit", "m?": "temp unknown" }[m];
    const trend = { up: "rising", dn: "falling", st: "steady" }[tr];
    const tool = { t0: "tool OK", t1: "tool worn (warn)", t2: "tool near end of life", "t?": "tool unknown" }[tw];
    return [`${margin}, ${trend}`, `zone ${vz === "AB" ? "A/B" : vz}`, load === "hi" ? "overloaded" : "load OK", tool,
      d === "d0" ? "no derate" : `derate ${d.slice(1)} %`].join(" · ");
  }

  // Tiny inline SVG charts (no chart library on this page)
  function lineChart(values, label) {
    if (values.length < 2) return `<div class="rl-chart-empty">Not enough data yet</div>`;
    const w = 240, h = 54, lo = Math.min(...values, 0), hi = Math.max(...values, 0), span = hi - lo || 1;
    const x = i => (i / (values.length - 1)) * w, y = v => h - 4 - ((v - lo) / span) * (h - 8);
    const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}">
      <line x1="0" x2="${w}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}" class="rl-zero"/>
      <polyline points="${pts}" class="rl-line"/></svg>`;
  }

  function barChart(values, label) {
    if (!values.length) return `<div class="rl-chart-empty">No evaluated decisions yet</div>`;
    const w = 240, h = 54, n = Math.max(values.length, 12), bw = w / n, lo = Math.min(...values, 0), hi = Math.max(...values, 0), span = hi - lo || 1;
    const y = v => h - 2 - ((v - lo) / span) * (h - 4), y0 = y(0);
    const bars = values.map((v, i) => {
      const top = Math.min(y(v), y0), height = Math.max(1, Math.abs(y(v) - y0));
      return `<rect x="${(i * bw + 1).toFixed(1)}" y="${top.toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${height.toFixed(1)}" class="${v < 0 ? "neg" : "pos"}"><title>${esc(inr(v))}</title></rect>`;
    }).join("");
    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}">
      <line x1="0" x2="${w}" y1="${y0.toFixed(1)}" y2="${y0.toFixed(1)}" class="rl-zero"/>${bars}</svg>`;
  }

  function kpis(s) {
    const f = s.feedback || {}, meta = (metrics && metrics.policy) || s.policy || {};
    const all = metrics && metrics.results ? {
      rl: metrics.results.rl_policy.all.return_inr, rule: metrics.results.rule_based.all.return_inr,
    } : null;
    const gain = all && all.rule ? (all.rl - all.rule) / Math.abs(all.rule) : null;
    const tile = (label, value, sub, tag) => `<div class="kpi"><div class="kpi-label">${label}${tag ? ` <span class="src-tag ${tag}">${tag === "sim" ? "SIM training" : "Live run"}</span>` : ""}</div>
      <div class="kpi-val">${value}</div><div class="kpi-sub">${sub}</div></div>`;
    return `<div class="kpi-row">
      ${tile("Training", s.trained ? "Trained" : "Not trained",
        s.trained ? `${(meta.episodes || 0).toLocaleString("en-IN")} episodes · ${s.policy.states_learned} states` : "run python -m ml.train_rl", "sim")}
      ${tile("vs rule-based", gain == null ? "–" : `${gain >= 0 ? "+" : ""}${(100 * gain).toFixed(1)}%`,
        all ? `${inr(all.rl)} vs ${inr(all.rule)} / 12-min shift, held-out` : "no evaluation file", "sim")}
      ${tile("Decisions evaluated", (f.decisions ?? 0).toLocaleString("en-IN"), `${f.online_updates ?? 0} policy updates · ${pct(f.prediction_accuracy)} risk calls right`, "live")}
      ${tile("Mean reward", inr(f.mean_reward_inr), `per 20 s decision · advice followed ${pct(f.followed_rate)}`, "live")}
    </div>`;
  }

  function recCard(r) {
    const best = r.action;
    const q = ACTIONS.map(a => `<div class="${a === best ? "best" : ""}">${SHORT[a]}<b>${inr(r.q_values[a])}</b></div>`).join("");
    const p = r.prediction || {};
    return `<div class="rl-rec">
      <div class="rl-rec-top"><span class="rl-action">${esc(r.machine_id)}: ${esc(r.label)}</span>
        <span class="rl-meta">next decision in ${r.seconds_to_next_decision} s · ${esc(r.mode)} data</span></div>
      <div class="rl-meta">${esc(describe(r.state))}${r.state_seen_in_training ? "" : " · new state (rule fallback)"} ·
        predicted risk ${esc(p.rules || "–")}${p.failure_risk_pct != null ? `, AI4I ${p.failure_risk_pct}%` : ""}</div>
      <div class="rl-q" title="Predicted value of each action from this state (Q-values, ₹) — a prediction, not an outcome">${q}</div>
      <div class="chat-actions rl-act" data-mid="${esc(r.machine_id)}">
        <button type="button" class="btn btn-sm btn-primary" data-act="propose">Send for operator approval</button>
      </div>
    </div>`;
  }

  function evalTable() {
    const res = metrics && metrics.results;
    if (!res) return `<div class="formula-hint">No trained policy yet: run <code>python -m ml.train_rl</code>.</div>`;
    const scen = ["none", "vibration_fault", "over_temperature", "tool_wear", "overload", "all"];
    const pols = Object.keys(res);
    const rows = scen.map(sc => {
      const vals = pols.map(p => res[p][sc].return_inr);
      const top = Math.max(...vals);
      return `<tr><td>${esc(sc.replace(/_/g, " "))}</td>${vals.map(v => `<td class="${v === top ? "win" : ""}">${inr(v)}</td>`).join("")}</tr>`;
    }).join("");
    return `<div class="rl-table-wrap"><table class="rl-table rl-eval"><tr><th>Scenario</th>${pols.map(p => `<th>${esc(POLICY_NAME[p] || p)}</th>`).join("")}</tr>${rows}
      <tr><td>trips / shift</td>${pols.map(p => `<td>${res[p].all.trips}</td>`).join("")}</tr></table></div>
      <div class="formula-hint">${esc(metrics.label)}; mean ₹ per 12-minute simulated shift, ${metrics.eval_episodes_per_scenario} held-out seeds per scenario.</div>`;
  }

  function method(s) {
    const r = (s.reward || {}).inr || {}, meta = (metrics && metrics.policy) || s.policy || {};
    return `<div class="rl-method">
      <div><b>Algorithm:</b> ${esc(meta.algorithm || "tabular Q-learning")}</div>
      <div><b>Environment:</b> ${esc(meta.environment || "VMC plant physics (sim/plant.py)")}</div>
      <div><b>Decision:</b> every ${s.decision_interval_s} s · actions ${ACTIONS.map(a => SHORT[a]).join(", ")}</div>
      <div><b>Reward:</b> parts × ₹${r.inr_per_part} − ₹${r.over_temp_inr_per_s}/s over temp − ₹${r.zone_c_inr_per_s}/s zone C − ₹${r.zone_d_inr_per_s}/s zone D
        − ₹${r.overload_inr_per_s}/s overload − ₹${r.worn_tool_inr_per_s}/s worn tool − ₹${r.maintenance_inr} per maintenance stop − trip ${r.trip_downtime_h} h × ₹${r.downtime_cost_inr_per_hr}/h.
        Penalty rates are illustrative assumptions.</div>
      <div><b>Trained:</b> ${esc(meta.trained_at || "–")}${meta.train_seconds ? ` in ${meta.train_seconds} s` : ""}. Online updates happen in SIM mode only and are not saved on restart.</div>
    </div>`;
  }

  function details(key, title, body) {
    return `<details class="rl-details" data-key="${key}"${openDetails.has(key) ? " open" : ""}><summary>${title}</summary>${body}</details>`;
  }

  function render(s) {
    const recs = Object.values(s.recommendations || {}).filter(Boolean);
    const recent = s.recent || [];
    const curve = (metrics && metrics.returns_curve || []).map(p => p.mean_return_inr);
    const live = [...recent].reverse().map(r => r.reward_inr);
    const rows = recent.slice(0, 30).map(r => `<tr>
        <td>${time(r.ts_start)}</td><td>${esc(r.machine_id)}</td>
        <td>${r.adverse_expected ? "risk" : "ok"}${r.failure_risk_pct != null ? " " + r.failure_risk_pct + "%" : ""}</td>
        <td>${SHORT[r.recommended_action]}</td><td>${SHORT[r.action_taken]}</td>
        <td>${r.outcome_adverse ? "adverse" : "ok"} · ${(+r.outcome_parts).toFixed(2)} parts</td>
        <td>${inr(r.reward_inr)}</td>
        <td class="${r.prediction_correct ? "rl-ok" : "rl-bad"}">${r.prediction_correct ? "✓" : "✗"}</td>
        <td>${r.policy_updated ? `${r.q_before} → ${r.q_after}` : "–"}</td></tr>`).join("");
    el("rlBody").innerHTML = `
      ${kpis(s)}
      ${recs.length ? recs.map(recCard).join("") : `<div class="formula-hint">Waiting for the first 20 s decision…</div>`}
      <div class="rl-charts">
        <figure><figcaption>Training reward per episode <span class="src-tag sim">SIM training</span></figcaption>${lineChart(curve, "Training reward curve")}
          <div class="rl-axis">${curve.length ? `${inr(curve[0])} → ${inr(curve[curve.length - 1])} over ${(((metrics || {}).policy || {}).episodes || 0).toLocaleString("en-IN")} episodes` : ""}</div></figure>
        <figure><figcaption>Reward per live decision <span class="src-tag live">Live run</span></figcaption>${barChart(live, "Live reward per decision")}
          <div class="rl-axis">${live.length ? `last ${live.length} decisions on the simulator stream` : ""}</div></figure>
      </div>
      ${details("log", `Evaluated decisions <span class="count-pill">${s.feedback.decisions ?? 0}</span>`,
        `<div class="formula-hint">Predicted risk and advice (before) → what then happened in the simulator (after) → reward and Q-table update.</div>
        <div class="rl-table-wrap"><table class="rl-table"><tr><th>Time</th><th>Machine</th><th>Predicted</th><th>Advised</th><th>Taken</th><th>Outcome</th><th>Reward</th><th>Pred.</th><th>Q update</th></tr>
        ${rows || `<tr><td colspan="9">No evaluated decisions yet.</td></tr>`}</table></div>`)}
      ${details("eval", "Held-out evaluation by scenario", evalTable())}
      ${details("method", "Method, reward and assumptions", method(s))}
      <div class="formula-hint">${esc(s.label)}.</div>`;
    el("rlBody").querySelectorAll("[data-act=propose]").forEach(b => b.onclick = () => propose(b.closest(".chat-actions")));
    el("rlBody").querySelectorAll("details.rl-details").forEach(d => d.addEventListener("toggle", () => {
      d.open ? openDetails.add(d.dataset.key) : openDetails.delete(d.dataset.key);
    }));
    if (pending) showPending();
  }

  function showPending() {
    const box = el("rlBody").querySelector(`.chat-actions[data-mid="${pending.machine_id}"]`);
    if (!box) return;
    box.innerHTML = `<span class="rl-meta">Proposal ready · </span>
      <button type="button" class="btn btn-sm btn-primary" data-d="approve">Approve</button>
      <button type="button" class="btn btn-sm" data-d="dismiss">Dismiss</button>`;
    box.querySelectorAll("[data-d]").forEach(b => b.onclick = () => decide(box, b.dataset.d));
  }

  function propose(box) {
    const mid = box.dataset.mid;
    fetch(`/api/rl/${encodeURIComponent(mid)}/propose`, { method: "POST" })
      .then(r => r.json().then(res => ({ r, res })))
      .then(({ r, res }) => {
        if (!r.ok) { box.textContent = (res.detail && res.detail.message) || `Error ${r.status}`; return; }
        pending = res.proposal; showPending();
      }).catch(() => { box.textContent = "Could not reach the server"; });
  }

  function decide(box, decision, retried) {
    const headers = { "Content-Type": "application/json" };
    if (operatorToken) headers["X-Operator-Token"] = operatorToken;
    fetch(`/api/chat/proposals/${encodeURIComponent(pending.proposal_id)}/${decision}`,
      { method: "POST", headers, body: JSON.stringify({ operator: "dashboard operator" }) })
      .then(r => r.json().then(res => ({ r, res })))
      .then(({ r, res }) => {
        if (r.status === 401 && !retried) {
          const t = window.prompt("Operator token required to " + decision + " this action:");
          if (t) { operatorToken = t; return decide(box, decision, true); }
        }
        pending = null;
        if (!r.ok) { box.textContent = `Not applied: ${(res.detail && (res.detail.message || res.detail.error)) || r.status}`; return; }
        const wo = (res.result || {}).work_order;
        box.textContent = decision === "dismiss" ? "Dismissed" : wo ? `✓ Approved · work order ${wo} created` : "✓ Approved · applied in the simulator";
        if (wo && window.fetchWorkOrders) window.fetchWorkOrders();
      }).catch(() => { box.textContent = "Could not reach the server"; });
  }

  function refresh() {
    if (pending) return;  // keep the Approve / Dismiss buttons in place until the operator decides
    fetch("/api/rl?recent=60").then(r => r.json()).then(render).catch(() => {});
  }

  function start() {
    if (!el("rlBody")) return;
    fetch("/api/rl/metrics").then(r => r.ok ? r.json() : null).then(m => { metrics = m; refresh(); }).catch(refresh);
    setInterval(refresh, 4000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
