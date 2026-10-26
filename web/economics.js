// MSME Business Impact & Sustainability Calculator (Control Center → Reports, index.html #economicsPanel).
// Port of the previous dashboard's calculator (web/legacy). Every figure comes from the backend:
// assumptions from GET /api/economics, the downtime/payback estimate and the monthly energy projection from
// GET /api/analytics (backend formulas, labelled "illustrative estimate"), live line loss from the "line"
// WebSocket frame and live power from the machine snapshots. Edit Assumptions saves through POST /api/economics.
// Net MSME Benefit per row: downtime = backend ROI; scrap = quality-hold parts (measured rate, projected to a
// month) × share actually scrapped × ₹/part × assumed scrap reduction; energy = monthly energy projection × assumed energy saving.
// 1-Year Profit = 12 × net monthly benefit − sensor hardware.
(function () {
  const $ = id => document.getElementById(id);
  const inr = v => v == null || isNaN(v) ? "—" : "₹" + Math.round(v).toLocaleString("en-IN");
  const num = (v, d = 1) => v == null || isNaN(v) ? "—" : (+v).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d });
  const set = (id, t) => { const e = $(id); if (e) e.textContent = t; };

  const MIN_SCRAP_HOURS = 0.5;  // project quality holds to a month only after 30 min of run time

  const FIELDS = [  // dialog input → backend key (api/app.py EconomicsUpdateRequest)
    ["econDowntimeCost", "downtime_cost_per_hr"],
    ["econPartMargin", "inr_per_part"],
    ["econTariff", "electricity_tariff_per_kwh"],
    ["econCea", "cea_co2_factor"],
    ["econNodeCost", "sensor_node_cost_inr"],
    ["econNodes", "sensor_nodes"],
    ["econBaseHours", "baseline_downtime_h_per_month"],
    ["econReduction", "assumed_downtime_reduction_pct"],
    ["econHoldScrap", "quality_hold_scrap_pct"],
    ["econScrapReduction", "assumed_scrap_reduction_pct"],
    ["econEnergySaving", "assumed_energy_saving_pct"],
    ["econMonthHours", "operating_hours_per_month"],
  ];

  let econ = null, analytics = null, lineLoss = null;
  const powerKw = {};

  function render() {
    if (!$("economicsPanel")) return;
    const a = econ || {};
    set("econInrPerPartHint", a.inr_per_part != null ? num(a.inr_per_part, 0) : "—");
    set("econTariffHint", a.electricity_tariff_per_kwh ?? "—");
    set("econCeaHint", a.cea_co2_factor ?? "—");
    set("econNodesHint", a.sensor_nodes != null ? num(a.sensor_nodes, 0) : "—");
    set("econNodeCostHint", a.sensor_node_cost_inr != null ? num(a.sensor_node_cost_inr, 0) : "—");

    set("calcLineLoss", lineLoss == null ? "Waiting for the line…" : `${inr(lineLoss)} /hr`);
    const kws = Object.values(powerKw);
    const kw = kws.length ? kws.reduce((s, v) => s + v, 0) : null;
    if (kw == null || !econ) {
      set("calcEnergy", "Waiting for live data…");
      set("calcCo2", "Waiting for live data…");
    } else {
      set("calcEnergy", `${inr(kw * a.electricity_tariff_per_kwh)} /hr (${num(kw)} kW)`);
      set("calcCo2", `${num(kw * a.cea_co2_factor, 2)} kg/hr`);
    }

    const roi = (analytics || {}).roi, proj = (analytics || {}).projection;
    if (!roi) {
      set("calcPayback", "—");
      return;
    }
    set("calcPayback", roi.payback_months == null ? "No saving assumed" : `${num(roi.payback_months, 2)} Months`);
    const base = a.baseline_downtime_h_per_month, pct = a.assumed_downtime_reduction_pct;
    set("scenBaseHours", `${num(base)} hrs`);
    set("scenTwinHours", `${num(base - roi.avoided_downtime_h_per_month)} hrs (−${num(pct, 0)}%)`);
    set("scenDowntimeSave", `${inr(roi.avoided_downtime_inr_per_month)} /mo saved`);
    // scrap risk: parts on quality hold so far, as a monthly rate
    const ms = Object.values((analytics || {}).machines || {});
    const holdParts = ms.reduce((t, m) => t + (((m.oee || {}).parts_quality_hold) || 0), 0);
    const hours = (analytics.elapsed_s || 0) / 3600;
    const holdPerMonth = hours >= MIN_SCRAP_HOURS ? holdParts / hours * a.operating_hours_per_month : null;
    const scrapShare = (a.quality_hold_scrap_pct ?? 0) / 100;
    const scrappedPerMonth = holdPerMonth == null ? null : holdPerMonth * scrapShare;
    const scrapBase = scrappedPerMonth == null ? null : scrappedPerMonth * a.inr_per_part;
    const scrapPct = a.assumed_scrap_reduction_pct ?? 0;
    const scrapSave = scrapBase == null ? null : scrapBase * scrapPct / 100;
    set("scenBaseScrap", scrapBase == null ? `Collecting data (${num(hours * 60, 0)} of 30 min run time)` : `${inr(scrapBase)} /mo · ${num(scrappedPerMonth, 0)} of ${num(holdPerMonth, 0)} held parts scrapped`);
    set("scenTwinScrap", scrapBase == null ? "—" : `${inr(scrapBase - scrapSave)} /mo (−${num(scrapPct, 0)}%)`);
    set("scenScrapSave", scrapSave == null ? "Not yet estimated" : `${inr(scrapSave)} /mo saved`);

    // energy: monthly projection at the current mean power
    const eCost = proj ? proj.cost_inr_per_month : null, eCo2 = proj ? proj.co2_kg_per_month : null;
    const ePct = a.assumed_energy_saving_pct ?? 0;
    const eSave = eCost == null ? null : eCost * ePct / 100, co2Save = eCo2 == null ? null : eCo2 * ePct / 100;
    set("scenBaseEnergy", eCost == null ? "—" : `${inr(eCost)} /mo · ${num(eCo2, 0)} kg CO₂`);
    set("scenTwinEnergy", eCost == null ? "—" : `${inr(eCost - eSave)} /mo · ${num(eCo2 - co2Save, 0)} kg CO₂`);
    set("scenEnergySave", eSave == null ? "—" : `${inr(eSave)} /mo · ${num(co2Save, 0)} kg CO₂`);

    const monthly = roi.avoided_downtime_inr_per_month + (scrapSave || 0) + (eSave || 0);
    const hw = roi.sensor_hardware_inr;
    set("scenNetMonthly", `${inr(monthly)} /mo`);
    const profit = monthly * 12 - hw;
    set("scenProfitAnnual", `1-Year Profit: ${profit < 0 ? "−" : ""}${inr(Math.abs(profit))}`);
    const avoidedH = roi.avoided_downtime_h_per_month;
    [1500, 2500, 3500].forEach(rate => set(`sensPay${rate}`, avoidedH > 0 ? `${num(hw / (avoidedH * rate))} mo` : "—"));
    set("econAssumptionNote", `${roi.label}: the downtime reduction (${num(pct, 0)}%), scrap reduction (${num(scrapPct, 0)}%) and energy saving `
      + `(${num(ePct, 1)}%) are input assumptions, not measured or guaranteed savings. Scrap baseline = parts on quality hold in this run × ${num(scrapShare * 100, 0)}% scrapped. `
      + `Hardware ${inr(hw)} (${num(a.sensor_nodes, 0)} nodes × ${inr(a.sensor_node_cost_inr)}).`);
  }

  async function load() {
    try {
      const [e, an] = await Promise.all([
        fetch("/api/economics").then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
        fetch("/api/analytics").then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
      ]);
      econ = e.assumptions || null;
      analytics = an;
      set("econStatus", "");
    } catch (_) {
      set("econStatus", "Backend unreachable: figures are not current.");
    }
    render();
  }

  function openDialog() {
    if (!econ) return;
    FIELDS.forEach(([id, key]) => { const el = $(id); if (el) el.value = econ[key]; });
    set("econFormMsg", "");
    $("economicsModal").classList.add("open");
    $(FIELDS[0][0]).focus();
  }
  const closeDialog = () => $("economicsModal").classList.remove("open");

  async function save(ev) {
    ev.preventDefault();
    const body = {};
    FIELDS.forEach(([id, key]) => { const v = parseFloat($(id).value); if (!isNaN(v)) body[key] = v; });
    const btn = $("econSave");
    btn.disabled = true;
    set("econFormMsg", "Saving…");
    try {
      const r = await fetch("/api/economics", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        const detail = Array.isArray(d.detail) ? d.detail.map(x => `${(x.loc || []).slice(-1)[0]}: ${x.msg}`).join("; ") : (d.detail || r.status);
        throw new Error(detail);
      }
      closeDialog();
      await load();
    } catch (err) {
      set("econFormMsg", `Not saved: ${err.message || "backend unreachable"}`);
    } finally {
      btn.disabled = false;
    }
  }

  function start() {
    if (!$("economicsPanel")) return;
    $("econEdit").addEventListener("click", openDialog);
    $("econCancel").addEventListener("click", closeDialog);
    $("econForm").addEventListener("submit", save);
    $("economicsModal").addEventListener("click", e => { if (e.target.id === "economicsModal") closeDialog(); });
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeDialog(); });
    const api = window.UdyamAPI;
    if (api) {
      api.on("line", f => { if (f && f.line_loss_inr_per_hr != null) { lineLoss = f.line_loss_inr_per_hr; render(); } });
      api.on("snapshot", s => { if (s && s.machine_id && typeof s.power_kw === "number") { powerKw[s.machine_id] = s.power_kw; } });
    }
    fetch("/api/snapshots").then(r => r.json()).then(list => {
      (list || []).forEach(s => { if (typeof s.power_kw === "number") powerKw[s.machine_id] = s.power_kw; });
      render();
    }).catch(() => {});
    load();
    setInterval(() => { if (document.visibilityState === "visible") load(); }, 15000);
    setInterval(render, 2000);  // live kW from the snapshot stream
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
