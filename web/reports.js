// Reports & Export panel: pick a time range (local time in the browser), download one section as CSV
// or every section as an Excel workbook. The server filters in UTC and adds an IST column.
(function () {
  const el = id => document.getElementById(id);
  const RANGED = new Set(["telemetry", "real_telemetry", "virtual_telemetry", "alerts", "work_orders", "events", "rl_decisions"]);

  // <input type="datetime-local"> wants local "YYYY-MM-DDTHH:MM:SS"
  function toLocalInput(d) {
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  function setLast(minutes) {
    const end = new Date();
    el("exportEnd").value = toLocalInput(end);
    el("exportStart").value = toLocalInput(new Date(end.getTime() - minutes * 60000));
    el("exportQuick").querySelectorAll(".btn").forEach(b => b.classList.toggle("active", +b.dataset.min === minutes));
    hint();
  }

  function range() {
    const s = new Date(el("exportStart").value), e = new Date(el("exportEnd").value);
    if (isNaN(s) || isNaN(e)) return { error: "Pick a start and an end time." };
    if (s >= e) return { error: "Start must be before end." };
    return { start: s.toISOString(), end: e.toISOString() };
  }

  function hint(msg) {
    const r = range();
    const sec = el("exportSection").value;
    el("exportHint").textContent = msg || (r.error ? r.error
      : RANGED.has(sec) ? `${sec.replace("_", " ")}: ${new Date(r.start).toLocaleString()} → ${new Date(r.end).toLocaleString()}. Files store UTC plus an IST column.`
      : "This section covers since the last reset (not the range). The Excel file has every section.");
  }

  // Download via fetch so a bad range shows the server's message instead of a broken file.
  function download(url) {
    hint("Preparing file…");
    fetch(url).then(async r => {
      if (!r.ok) {
        const res = await r.json().catch(() => ({}));
        const d = res.detail || {};
        hint(`Export failed: ${d.message || d.error || (typeof d === "string" ? d : r.status)}`);
        return;
      }
      const blob = await r.blob();
      const name = ((r.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/) || [])[1] || "udyam-twin-export";
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      hint(`Downloaded ${name} (${(blob.size / 1024).toFixed(0)} KB).`);
    }).catch(() => hint("Could not reach the server."));
  }

  // Report snapshot: the figures the summary sheet would hold now, since the last reset (GET /api/reports "window")
  function snapshot() {
    if (document.visibilityState !== "visible") return;
    fetch("/api/reports?trend_minutes=1").then(r => r.ok ? r.json() : Promise.reject(r.status)).then(rep => {
      const w = rep.window || {}, ms = Object.values(w.machines || {});
      const sum = f => ms.reduce((t, m) => t + (f(m) || 0), 0);
      const oees = ms.map(m => (m.oee || {}).oee).filter(v => typeof v === "number");
      const since = w.since ? new Date(w.since) : null;
      const alerts = (rep.alerts || []).filter(x => !since || new Date(x.raised_at) >= since).length;
      const open = (rep.work_orders || []).filter(x => x.status !== "Closed").length;
      const set = (id, t) => { el(id).textContent = t; };
      set("snapSince", since ? `since reset · ${since.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "—");
      set("snapParts", sum(m => (m.oee || {}).parts).toLocaleString("en-IN"));
      set("snapOee", oees.length ? `${(100 * oees.reduce((t, v) => t + v, 0) / oees.length).toFixed(1)}%` : "—");
      set("snapEnergy", `${(w.site_energy_kwh || 0).toFixed(1)} kWh`);
      set("snapCost", `₹${Math.round(sum(m => m.cost_inr)).toLocaleString("en-IN")}`);
      set("snapCo2", `${sum(m => m.co2_kg).toFixed(1)} kg`);
      set("snapAlerts", `${alerts} · ${open}`);
    }).catch(() => { el("snapSince").textContent = "server unreachable"; });
  }

  function start() {
    if (!el("exportPanel")) return;
    snapshot();
    setInterval(snapshot, 15000);
    el("exportQuick").querySelectorAll(".btn").forEach(b => b.onclick = () => setLast(+b.dataset.min));
    ["exportStart", "exportEnd", "exportSection"].forEach(id => el(id).addEventListener("change", () => {
      el("exportQuick").querySelectorAll(".btn").forEach(b => b.classList.remove("active")); hint();
    }));
    el("exportCsv").onclick = () => {
      const r = range(), sec = el("exportSection").value;
      if (r.error && RANGED.has(sec)) return hint(r.error);
      const q = new URLSearchParams({ section: sec });
      if (RANGED.has(sec)) { q.set("start", r.start); q.set("end", r.end); }
      download(`/api/reports/export.csv?${q}`);
    };
    el("exportXlsx").onclick = () => {
      const r = range();
      if (r.error) return hint(r.error);
      download(`/api/reports/export.xlsx?${new URLSearchParams({ start: r.start, end: r.end, tz: "Asia/Kolkata" })}`);
    };
    setLast(60);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
