// Workspace tabs: Live / Simulate / Data & AI / Reports. Every panel keeps its id and its script; only one pane
// is shown at a time. The choice is remembered (URL hash, then this browser) and Simulate is hidden in Real Data
// mode (its controls are refused there anyway).
(function () {
  const TABS = ["live", "simulate", "ai", "reports"];
  const KEY = "udyam.workspace.tab";
  const tabs = () => [...document.querySelectorAll("#wsTabs .ws-tab")];

  function remembered() {
    const fromHash = location.hash.replace("#", "");
    if (TABS.includes(fromHash)) return fromHash;
    try { const t = localStorage.getItem(KEY); if (TABS.includes(t)) return t; } catch (_) { /* storage blocked */ }
    return "live";
  }

  function available(tab) {
    return !(tab === "simulate" && document.body.classList.contains("real-mode"));
  }

  function show(tab, focus) {
    if (!TABS.includes(tab) || !available(tab)) tab = "live";
    tabs().forEach(b => {
      const on = b.dataset.tab === tab;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    });
    document.querySelectorAll(".ws-pane").forEach(p => { p.hidden = p.dataset.pane !== tab; });
    try { localStorage.setItem(KEY, tab); } catch (_) { /* storage blocked */ }
    if (location.hash !== `#${tab}`) history.replaceState(null, "", `#${tab}`);
    // the 3D view and charts size themselves from their container; tell them it may have changed
    window.dispatchEvent(new Event("resize"));
  }

  function current() {
    return (tabs().find(b => b.getAttribute("aria-selected") === "true") || {}).dataset?.tab || "live";
  }

  function start() {
    const bar = document.getElementById("wsTabs");
    if (!bar) return;
    bar.addEventListener("click", e => { const b = e.target.closest(".ws-tab"); if (b) show(b.dataset.tab); });
    bar.addEventListener("keydown", e => {  // arrow keys move between tabs (WAI-ARIA tabs pattern)
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      const list = tabs().filter(b => b.offsetParent !== null);
      const i = list.findIndex(b => b.dataset.tab === current());
      const next = e.key === "Home" ? 0 : e.key === "End" ? list.length - 1
        : (i + (e.key === "ArrowRight" ? 1 : -1) + list.length) % list.length;
      show(list[next].dataset.tab, true);
      e.preventDefault();
    });
    window.addEventListener("hashchange", () => show(location.hash.replace("#", "")));
    // the tab bar sticks just under the header, whose height changes when it wraps on narrow screens
    const header = document.querySelector("header");
    if (header && window.ResizeObserver) {
      new ResizeObserver(() => document.documentElement.style.setProperty("--header-h", `${header.offsetHeight}px`)).observe(header);
    }
    // leaving Testing mode while on Simulate goes back to Live
    new MutationObserver(() => { if (!available(current())) show("live"); })
      .observe(document.body, { attributes: true, attributeFilter: ["class"] });
    // the Live tab shows the active-alert count from the alerts panel
    const src = document.getElementById("alertCount"), out = document.getElementById("wsAlertCount");
    if (src && out) {
      const sync = () => {
        const n = parseInt(src.textContent, 10) || 0;
        out.hidden = n === 0;
        out.textContent = n;
        out.title = `${n} active alert${n === 1 ? "" : "s"}`;
      };
      new MutationObserver(sync).observe(src, { childList: true, characterData: true, subtree: true });
      sync();
    }
    show(remembered());
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
