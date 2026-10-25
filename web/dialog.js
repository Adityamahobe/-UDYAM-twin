// On-page confirm / prompt dialogs. Browser popups (window.confirm / window.prompt) are not shown in some
// embedded browsers, where confirm() silently returns false; that made switches such as the mode badge do nothing.
//   udyamConfirm(message, { ok, cancel, danger }) -> Promise<boolean>
//   udyamPrompt(message, { password, placeholder }) -> Promise<string | null>
(function () {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let open = null;

  function show(message, opts, withInput) {
    if (open) open.cancel();
    return new Promise(resolve => {
      const prev = document.activeElement;
      const [title, ...rest] = String(message).split("\n\n");
      const wrap = document.createElement("div");
      wrap.className = "ud-dialog-backdrop";
      wrap.innerHTML = `<div class="ud-dialog" role="dialog" aria-modal="true" aria-labelledby="udDialogTitle">
        <div class="ud-dialog-title" id="udDialogTitle">${esc(title)}</div>
        ${rest.length ? `<div class="ud-dialog-body">${rest.map(p => `<p>${esc(p)}</p>`).join("")}</div>` : ""}
        ${withInput ? `<input class="ud-dialog-input" type="${opts.password ? "password" : "text"}" autocomplete="off" placeholder="${esc(opts.placeholder || "")}">` : ""}
        <div class="ud-dialog-actions">
          <button type="button" class="btn btn-sm" data-act="cancel">${esc(opts.cancel || "Cancel")}</button>
          <button type="button" class="btn btn-sm ${opts.danger ? "btn-danger" : "btn-primary"}" data-act="ok">${esc(opts.ok || "OK")}</button>
        </div></div>`;
      const input = wrap.querySelector(".ud-dialog-input");
      const finish = ok => {
        document.removeEventListener("keydown", onKey, true);
        wrap.remove();
        open = null;
        if (prev && prev.focus) prev.focus();
        resolve(withInput ? (ok ? input.value : null) : ok);
      };
      const onKey = e => {
        if (e.key === "Escape") { e.preventDefault(); finish(false); }
        else if (e.key === "Enter" && (withInput ? document.activeElement === input : true)) { e.preventDefault(); finish(true); }
        else if (e.key === "Tab") {  // keep focus inside the dialog
          const f = [...wrap.querySelectorAll("input, button")];
          const i = f.indexOf(document.activeElement);
          e.preventDefault();
          f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
        }
      };
      wrap.addEventListener("click", e => {
        const act = e.target.closest("[data-act]")?.dataset.act;
        if (act) finish(act === "ok");
        else if (e.target === wrap) finish(false);
      });
      document.addEventListener("keydown", onKey, true);
      document.body.appendChild(wrap);
      open = { cancel: () => finish(false) };
      (input || wrap.querySelector('[data-act="ok"]')).focus();
    });
  }

  window.udyamConfirm = (message, opts = {}) => show(message, opts, false);
  window.udyamPrompt = (message, opts = {}) => show(message, opts, true);
})();
