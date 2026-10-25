// Reinforcement-learning maintenance policy panel. Reads GET /api/rl and /api/rl/metrics.
// The policy only recommends: "Send for approval" creates a proposal that an operator approves
// through the same authorized workflow as the Advisor (POST /api/chat/proposals/{id}/approve).
(function () {
  const ACTIONS = ["continue", "derate_20", "derate_40", "maintain"];
  const SHORT = { continue: "Continue", derate_20: "Derate 20%", derate_40: "Derate 40%", maintain: "Maintain" };
  const POLICY_NAME = { always_continue: "Always continue", rule_based: "Rule-based (twin today)", rl_policy: "RL policy" };
  let metrics = null, operatorToken = null, pending = null;

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
    return [`${margin}, ${trend}`, `vibration zone ${vz === "AB" ? "A/B" : vz}`, load === "hi" ? "overloaded" : "load OK", tool,
      d === "d0" ? "no derate" : `derate ${d.slice(1)} %`].join(" · ");
  }

  function recCard(r) {
    const best = r.action;
    const q = ACTIONS.map(a => `<div class="${a === best ? "best" : ""}">${SHORT[a]}<br>${inr(r.q_values[a])}</div>`).join("");
    const p = r.prediction || {};
    return `<div class="rl-rec">
      <div class="rl-meta">${esc(r.machine_id)} · recommendation · next decision in ${r.seconds_to_next_decision} s · ${esc(r.mode)} data</div>
      <div class="rl-action">${esc(r.label)}</div>
      <div class="rl-meta"><b>State:</b> ${esc(describe(r.state))}${r.state_seen_in_training ? "" : " (new state: rule fallback)"}</div>
      <div class="rl-meta"><b>Predicted risk:</b> rules ${esc(p.rules || "–")} · AI4I failure risk ${p.failure_risk_pct ?? "–"}% ·
        ${p.adverse_expected ? "adverse outcome expected" : "no adverse outcome expected"}</div>
      <div class="rl-q" title="Expected ₹ value of each action from this state (Q-values)">${q}</div>
      <div class="chat-actions" style="margin-top:8px;" data-mid="${esc(r.machine_id)}">
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
    return `<div style="font-size:11px;font-weight:700;color:var(--text-secondary);">Held-out evaluation · mean ₹ per 12-min simulated shift</div>
      <div class="rl-table-wrap"><table class="rl-table rl-eval"><tr><th>Scenario</th>${pols.map(p => `<th>${esc(POLICY_NAME[p] || p)}</th>`).join("")}</tr>${rows}
      <tr><td>trips / shift</td>${pols.map(p => `<td>${res[p].all.trips}</td>`).join("")}</tr></table></div>
      <div class="formula-hint">${esc(metrics.label)}. Reward = parts × ₹${metrics.reward.inr.inr_per_part} − penalties (heat, vibration, overload, worn tool, trip, maintenance); penalty rates are illustrative assumptions.</div>`;
  }

  function render(s) {
    const recs = Object.values(s.recommendations || {}).filter(Boolean);
    const f = s.feedback || {};
    const recent = (s.recent || []).map(r => `<tr>
        <td>${time(r.ts_start)}</td><td>${esc(r.machine_id)}</td>
        <td>${r.adverse_expected ? "risk" : "ok"}${r.failure_risk_pct != null ? " " + r.failure_risk_pct + "%" : ""}</td>
        <td>${SHORT[r.recommended_action]}</td><td>${SHORT[r.action_taken]}</td>
        <td>${r.outcome_adverse ? "adverse" : "ok"} · ${(+r.outcome_parts).toFixed(2)} parts</td>
        <td>${inr(r.reward_inr)}</td>
        <td class="${r.prediction_correct ? "rl-ok" : "rl-bad"}">${r.prediction_correct ? "✓" : "✗"}</td>
        <td>${r.policy_updated ? `${r.q_before} → ${r.q_after}` : "–"}</td></tr>`).join("");
    el("rlBody").innerHTML = `
      ${recs.length ? recs.map(recCard).join("") : `<div class="formula-hint">Waiting for the first 20 s decision…</div>`}
      <div style="font-size:11px;font-weight:700;color:var(--text-secondary);">Feedback loop: predicted risk / action → simulated outcome → evaluate → update policy (SIM only)</div>
      <div class="rl-loop">
        <div class="calc-card"><div class="calc-label">Decisions evaluated</div><div class="calc-val" style="font-size:15px;">${f.decisions ?? 0}</div><div class="formula-hint">${f.online_updates ?? 0} policy updates</div></div>
        <div class="calc-card"><div class="calc-label">Risk prediction correct</div><div class="calc-val" style="font-size:15px;">${pct(f.prediction_accuracy)}</div><div class="formula-hint">vs. what then happened</div></div>
        <div class="calc-card"><div class="calc-label">Advice followed</div><div class="calc-val" style="font-size:15px;">${pct(f.followed_rate)}</div><div class="formula-hint">action taken = recommended</div></div>
        <div class="calc-card"><div class="calc-label">Mean reward</div><div class="calc-val" style="font-size:15px;">${inr(f.mean_reward_inr)}</div><div class="formula-hint">per 20 s decision</div></div>
      </div>
      <div class="rl-table-wrap"><table class="rl-table"><tr><th>Time</th><th>Machine</th><th>Predicted</th><th>Advised</th><th>Taken</th><th>Outcome</th><th>Reward</th><th>Pred.</th><th>Q update</th></tr>
        ${recent || `<tr><td colspan="9">No evaluated decisions yet.</td></tr>`}</table></div>
      ${evalTable()}
      <div class="formula-hint">${esc(s.label)}.</div>`;
    el("rlBody").querySelectorAll("[data-act=propose]").forEach(b => b.onclick = () => propose(b.closest(".chat-actions")));
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
    fetch("/api/rl?recent=12").then(r => r.json()).then(render).catch(() => {});
  }

  function start() {
    if (!el("rlBody")) return;
    fetch("/api/rl/metrics").then(r => r.ok ? r.json() : null).then(m => { metrics = m; refresh(); }).catch(refresh);
    setInterval(refresh, 4000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
