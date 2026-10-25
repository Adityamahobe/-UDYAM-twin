// Live Records panel (Reports tab): the newest rows the app has stored (telemetry, events, alerts, work orders),
// newest first, from GET /api/reports/recent. Polls every 2 s only while the Reports tab is visible; Pause freezes it.
(function () {
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const KIND = { telemetry: "Telemetry", event: "Event", alert: "Alert", work_order: "Work order" };
  let kind = "all", paused = false, seen = new Set(), first = true;

  const keyOf = e => `${e.kind}|${e.ts}|${e.machine_id}|${e.summary}`;
  const time = ts => { const d = new Date(ts); return isNaN(d) ? "—" : d.toLocaleTimeString([], { hour12: false }); };

  function render(entries) {
    const body = el("rcBody");
    if (!entries.length) { body.innerHTML = `<tr><td colspan="4" class="sc-hint">No records yet.</td></tr>`; return; }
    const fresh = new Set();
    body.innerHTML = entries.map(e => {
      const k = keyOf(e), isNew = !first && !seen.has(k);
      fresh.add(k);
      const detail = e.kind === "event" && e.detail ? `${esc(e.detail)}: ` : e.detail ? `[${esc(e.detail)}] ` : "";
      return `<tr class="${isNew ? "rc-new" : ""}">
        <td>${time(e.ts)}</td>
        <td><span class="rc-kind ${esc(e.kind)}">${esc(KIND[e.kind] || e.kind)}</span>${e.source ? `<span class="rc-src ${esc(e.source)}">${esc(e.source)}</span>` : ""}</td>
        <td>${esc(e.machine_id || "—")}</td>
        <td class="${e.severity ? `rc-sev-${esc(e.severity)}` : ""}">${detail}${esc(e.summary)}</td>
      </tr>`;
    }).join("");
    seen = fresh;
    first = false;
  }

  function load() {
    const pane = el("recentPanel")?.closest(".ws-pane");
    if (paused || (pane && pane.hidden) || document.hidden) return;
    fetch(`/api/reports/recent?kind=${encodeURIComponent(kind)}&limit=80`).then(r => r.json())
      .then(res => render(res.entries || [])).catch(() => { el("rcHint").textContent = "Could not reach the server."; });
  }

  function start() {
    if (!el("recentPanel")) return;
    el("rcKinds").querySelectorAll(".sev-chip").forEach(b => b.onclick = () => {
      el("rcKinds").querySelectorAll(".sev-chip").forEach(x => x.classList.toggle("active", x === b));
      kind = b.dataset.kind; first = true; seen = new Set();
      load();
    });
    el("rcPause").onclick = () => {
      paused = !paused;
      el("rcPause").setAttribute("aria-pressed", String(paused));
      el("rcPause").textContent = paused ? "Resume" : "Pause";
      el("rcHint").textContent = paused ? "Paused: the list is frozen so you can read it." : "Newest first. Updates every 2 s while this tab is open.";
      if (!paused) load();
    };
    window.addEventListener("hashchange", load);
    document.querySelectorAll('#wsTabs .ws-tab[data-tab="reports"]').forEach(b => b.addEventListener("click", () => setTimeout(load, 50)));
    load();
    setInterval(load, 2000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
