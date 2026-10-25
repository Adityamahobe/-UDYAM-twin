// UDYAM-TWIN Advisor chat panel. Talks to POST /api/chat.
// The advisor only PROPOSES actions; the operator approves them here.
(function () {
  let conversationId = null;  // the server keeps the history; we only send this id
  let busy = false;

  const SUGGESTIONS = [
    "What's happening on the line right now?",
    "VMC-01 is heating up. Can I finish a batch of 40 parts?",
    "Why is the twin recalibrating?",
    "How often does coolant degradation hit the limit in the sim data?",
    "VMC-01 garam ho raha hai, kya karu?",
  ];

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Minimal, safe markdown: escape first, then bold / headings / bullets / line breaks.
  function md(text) {
    const lines = esc(text).split("\n");
    let html = "", inList = false;
    for (let line of lines) {
      line = line.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
      const li = line.match(/^\s*[-*•]\s+(.*)/);
      if (li) {
        if (!inList) { html += "<ul>"; inList = true; }
        html += `<li>${li[1]}</li>`;
        continue;
      }
      if (inList) { html += "</ul>"; inList = false; }
      const h = line.match(/^#{1,4}\s+(.*)/);
      html += h ? `<div class="chat-h">${h[1]}</div>` : (line.trim() ? `<p>${line}</p>` : "");
    }
    if (inList) html += "</ul>";
    return html;
  }

  function el(id) { return document.getElementById(id); }

  function build() {
    const fab = document.createElement("button");
    fab.id = "chatFab";
    fab.className = "chat-fab";
    fab.type = "button";
    fab.innerHTML = "💬 Ask the Twin";
    fab.onclick = toggle;
    document.body.appendChild(fab);

    const panel = document.createElement("section");
    panel.id = "chatPanel";
    panel.className = "chat-panel";
    panel.setAttribute("aria-label", "UDYAM-TWIN Advisor");
    panel.innerHTML = `
      <div class="chat-head">
        <div>
          <div class="chat-title">UDYAM-TWIN Advisor</div>
          <div class="chat-sub">Answers from the live twin · actions need your approval</div>
        </div>
        <span id="chatMode" class="chat-mode">…</span>
        <button type="button" class="chat-x" aria-label="Close" onclick="window.udyamChat.toggle()">✕</button>
      </div>
      <div id="chatLog" class="chat-log" aria-live="polite"></div>
      <div id="chatChips" class="chat-chips"></div>
      <form id="chatForm" class="chat-form">
        <input id="chatInput" type="text" autocomplete="off" placeholder="Ask about a machine, a decision, or the sim data…" maxlength="2000" />
        <button class="btn btn-primary btn-sm" type="submit">Send</button>
      </form>`;
    document.body.appendChild(panel);

    el("chatChips").innerHTML = SUGGESTIONS.map(s => `<button type="button" class="chat-chip">${esc(s)}</button>`).join("");
    el("chatChips").querySelectorAll(".chat-chip").forEach(b => b.onclick = () => send(b.textContent));
    el("chatForm").onsubmit = e => { e.preventDefault(); send(el("chatInput").value); };

    addMsg("assistant", "Hi! I read the live twin, run what-if forecasts with its calibrated physics, and use what we learned from the UDYAM-SIM dataset. Ask me what to do.");
    refreshStatus();
  }

  function refreshStatus() {
    fetch("/api/chat/status").then(r => r.json()).then(s => {
      const m = el("chatMode");
      if (s.llm_ready) {
        m.textContent = s.provider_label || "Gemini"; m.className = "chat-mode on"; m.title = s.llm_model || "";
      } else {
        m.textContent = "Offline"; m.className = "chat-mode off";
        m.title = s.llm_not_ready_reason || "No chat model configured";
      }
    }).catch(() => {});
  }

  function toggle() {
    const p = el("chatPanel");
    p.classList.toggle("open");
    if (p.classList.contains("open")) { el("chatInput").focus(); refreshStatus(); }
  }

  function addMsg(role, text, meta) {
    const d = document.createElement("div");
    d.className = `chat-msg ${role}`;
    d.innerHTML = role === "user" ? `<p>${esc(text)}</p>` : md(text);
    if (meta) {
      const m = document.createElement("div");
      m.className = "chat-meta";
      m.textContent = meta;
      d.appendChild(m);
    }
    el("chatLog").appendChild(d);
    el("chatLog").scrollTop = el("chatLog").scrollHeight;
    return d;
  }

  function addProposal(p) {
    const d = document.createElement("div");
    d.className = "chat-proposal";
    const what = p.action === "derate" ? `Derate ${esc(p.machine_id)} to ${Number(p.derate_pct).toFixed(0)}%`
      : p.action === "clear_derate" ? `Clear derate on ${esc(p.machine_id)}`
      : `Work order: ${esc(p.title || p.machine_id)}`;
    d.innerHTML = `<div><b>Proposed:</b> ${what}</div><div class="chat-meta">${esc(p.reason || "")}</div>
      <div class="chat-actions"><button type="button" class="btn btn-sm btn-primary">Approve</button>
      <button type="button" class="btn btn-sm">Dismiss</button></div>`;
    const [ok, no] = d.querySelectorAll("button");
    ok.onclick = () => decide(p, d, "approve");
    no.onclick = () => decide(p, d, "dismiss");
    el("chatLog").appendChild(d);
    el("chatLog").scrollTop = el("chatLog").scrollHeight;
  }

  // Approval is a separate, authorized server workflow: the server checks the operator, executes the
  // proposal once (in the simulator / work-order log only) and refuses replays or expired proposals.
  let operatorToken = null;  // typed by the operator when the server asks; kept in memory only

  function decide(p, d, decision, retried) {
    const box = d.querySelector(".chat-actions");
    if (!p.proposal_id) { box.textContent = "This proposal cannot be approved here."; return; }
    const headers = { "Content-Type": "application/json" };
    if (operatorToken) headers["X-Operator-Token"] = operatorToken;
    fetch(`/api/chat/proposals/${encodeURIComponent(p.proposal_id)}/${decision}`,
      { method: "POST", headers, body: JSON.stringify({ operator: "dashboard operator" }) })
      .then(r => r.json().then(res => ({ r, res })))
      .then(async ({ r, res }) => {
        if (r.status === 401 && !retried) {
          const t = await udyamPrompt("Operator token required to " + decision + " this action", { password: true, ok: "Continue" });
          if (t) { operatorToken = t; return decide(p, d, decision, true); }
        }
        d.classList.add("done");
        if (!r.ok) {
          const det = res.detail || {};
          box.textContent = `Not applied: ${det.message || det.error || r.status}`;
          if (r.status === 401) operatorToken = null;
          return;
        }
        if (decision === "dismiss") { box.textContent = "Dismissed"; return; }
        const wo = (res.result || {}).work_order;
        box.textContent = wo ? `✓ Approved · work order ${wo} created` : "✓ Approved · applied in the simulator";
        if (wo && window.fetchWorkOrders) window.fetchWorkOrders();
      })
      .catch(() => { box.textContent = "Could not reach the server"; });
  }

  function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    busy = true;
    el("chatInput").value = "";
    addMsg("user", text);
    const thinking = addMsg("assistant", "Checking the twin…");
    thinking.classList.add("thinking");
    fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, conversation_id: conversationId }) })
      .then(r => r.json().then(res => ({ ok: r.ok, status: r.status, res })))
      .then(({ ok, status, res }) => {
        thinking.remove();
        if (!ok) {
          const d = res.detail || {};
          if (d.error === "unknown_conversation" || d.error === "conversation_too_long") conversationId = null;
          const msg = typeof d === "string" ? d : (d.message || JSON.stringify(d));
          addMsg("system", `Error ${status}${d.error ? " (" + d.error + ")" : ""}: ${msg}`);
          return;
        }
        conversationId = res.conversation_id;
        const tools = (res.tools_used || []).filter((t, i, a) => a.indexOf(t) === i).join(", ");
        const label = { gemini: "Gemini", ollama: "Local model", offline: "Offline" }[res.mode] || res.mode;
        const meta = `${label}${res.model && res.mode !== "offline" ? " (" + res.model + ")" : ""} · ${res.telemetry_mode || "SIM"} data${tools ? " · used " + tools : ""}${res.latency_s != null ? " · " + res.latency_s + " s" : ""}`;
        addMsg("assistant", res.reply || "(no answer)", meta);
        if (res.notice) addMsg("system", res.notice);
        const unverified = (res.checks || {}).unverified_numbers || [];
        if (unverified.length) addMsg("system", `Check: ${unverified.join(", ")} did not match any tool result.`);
        (res.proposals || []).forEach(addProposal);
      })
      .catch(err => { thinking.remove(); addMsg("system", "Could not reach the server: " + err); })
      .finally(() => { busy = false; refreshStatus(); });
  }

  window.udyamChat = { toggle, send };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build); else build();
})();
