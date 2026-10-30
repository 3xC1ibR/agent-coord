/* Global generated-report inbox. Dismissal applies to a report, never its agent. */
(function (root) {
  "use strict";
  const document = root.document, element = id => document.getElementById(id);
  if (element("open-status-reports")) {
    const dialog = element("status-reports-dialog"), frame = element("status-reports-frame");
    element("open-status-reports").onclick = event => {
      event.preventDefault();
      const backdrop = element("sidebar-backdrop"); if (backdrop && !backdrop.hidden) backdrop.click();
      frame.src = "/status?embedded=1"; dialog.showModal();
    };
    root.addEventListener("message", event => {
      if (event.origin === root.location.origin && event.source === frame.contentWindow && event.data?.type === "ribbon-field-close-status") dialog.close();
    });
    dialog.addEventListener("close", () => {
      frame.removeAttribute("src");
      (element("open-status-reports").getClientRects().length ? element("open-status-reports") : element("menu-toggle")).focus({preventScroll: true});
    });
    return;
  }
  if (!element("status-report-list")) return;
  let reports = [], token, selected = null, revision = 0, contentRevision = 0, previousSignature = "", undo = null;
  const pending = new Set();
  const make = (tag, text, className) => { const node = document.createElement(tag); if (text != null) node.textContent = text; if (className) node.className = className; return node; };
  const feedback = text => { element("status-report-feedback").textContent = text; };
  const showError = text => { element("status-report-error").textContent = text; element("status-report-error").hidden = !text; };
  const time = value => new Date(value).toLocaleString();
  const origin = report => [report.origin.agent, report.origin.provider, report.origin.workspace, report.origin.host].filter(Boolean).join(" · ");
  function filtered() {
    const view = element("status-report-view").value, workspace = element("status-report-workspace").value, query = element("status-report-search").value.trim().toLowerCase();
    return reports.filter(report => (view === "all" || Boolean(report.dismissed_at) === (view === "dismissed")) &&
      (!workspace || report.origin.workspace === workspace) && (!query || [report.title, report.subject, report.summary, origin(report)].join(" ").toLowerCase().includes(query)));
  }
  async function request(path, body) {
    const response = await fetch("/api/browser/" + path, {cache: "no-store", ...(body === undefined ? {} : {
      method: "POST", headers: {"Content-Type": "application/json", "X-Agent-Coord-Token": token}, body: JSON.stringify(body)})});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "The status inbox could not be loaded. Try Refresh.");
    return result;
  }
  function closeReport(focus = false) {
    const previous = selected; selected = null; contentRevision += 1;
    element("status-report-reader").hidden = true;
    element("status-report-frame").removeAttribute("srcdoc");
    element("status-report-layout").classList.remove("has-reader");
    if (focus) document.querySelector(`[data-report-id="${previous}"] .status-report-title`)?.focus();
  }
  async function openReport(report) {
    selected = report.id; const current = ++contentRevision;
    render();
    element("status-report-title").textContent = report.title;
    element("status-report-origin").textContent = origin(report) + " · Generated " + time(report.generated_at);
    element("status-report-reader").hidden = false;
    element("status-report-layout").classList.add("has-reader");
    element("status-report-reader-error").hidden = true;
    element("status-report-frame").removeAttribute("srcdoc");
    try {
      const response = await fetch("/api/browser/status-report-content/" + report.id, {cache: "no-store"});
      if (!response.ok) { const result = await response.json(); throw Error(result.error || "The report could not be opened."); }
      const html = await response.text();
      if (current !== contentRevision) return;
      // The opaque sandbox and this policy also apply when viewing local producers' HTML.
      element("status-report-frame").srcdoc = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; base-uri \'none\'; form-action \'none\'">' + html;
      element("close-status-report").focus({preventScroll: true});
    } catch (error) {
      if (current !== contentRevision) return;
      element("status-report-reader-error").textContent = error.message;
      element("status-report-reader-error").hidden = false;
    }
  }
  function render() {
    const visible = filtered();
    if (selected && !visible.some(report => report.id === selected)) closeReport();
    const currentReport = reports.find(report => report.id === selected);
    element("dismiss-selected-status-report").textContent = currentReport?.dismissed_at ? "Restore" : "Dismiss";
    element("dismiss-selected-status-report").disabled = !currentReport || pending.has(selected);
    const list = element("status-report-list"); list.replaceChildren();
    for (const report of visible) {
      const row = make("li", null, "status-report-row"); row.dataset.reportId = report.id;
      const heading = make("div", null, "status-report-row-heading");
      const title = make("button", report.title, "status-report-title"); title.type = "button";
      title.setAttribute("aria-current", String(selected === report.id)); title.onclick = () => openReport(report);
      const action = make("button", report.dismissed_at ? "Restore" : "Dismiss", "quiet"); action.type = "button";
      action.setAttribute("aria-label", `${report.dismissed_at ? "Restore" : "Dismiss"} ${report.title}`);
      action.disabled = pending.has(report.id) || !token;
      action.onclick = () => changeDismissal(report.id, !report.dismissed_at);
      heading.append(title, action);
      row.append(heading, make("p", report.summary, "status-report-row-summary"), make("p", origin(report) || report.subject, "status-report-note"),
        make("p", "Generated " + time(report.generated_at) + (report.dismissed_at ? " · Dismissed " + time(report.dismissed_at) : ""), "status-report-note"));
      list.append(row);
    }
    element("status-report-empty").hidden = visible.length > 0;
    element("status-report-empty").textContent = reports.length ? "No reports match this view." : "No status reports yet. Reports generated with the status skill will appear here.";
  }
  async function load(manual = false) {
    const current = ++revision;
    try {
      if (!token) token = (await request("config")).token;
      const result = await request("status-reports");
      if (current !== revision) return;
      showError("");
      element("status-report-coverage").hidden = !result.unreadable;
      element("status-report-coverage").textContent = `${result.unreadable} report record(s) could not be read. Other reports remain available.`;
      const signature = JSON.stringify(result);
      if (signature !== previousSignature) {
        previousSignature = signature; reports = result.reports;
        const picker = element("status-report-workspace"), selectedWorkspace = picker.value;
        picker.replaceChildren(new Option("All projects", ""));
        for (const workspace of [...new Set(reports.map(report => report.origin.workspace).filter(Boolean))].sort()) picker.add(new Option(workspace, workspace));
        picker.value = [...picker.options].some(option => option.value === selectedWorkspace) ? selectedWorkspace : "";
        render();
      }
      element("status-report-counts").textContent = `${reports.filter(report => !report.dismissed_at).length} in inbox · ${reports.filter(report => report.dismissed_at).length} dismissed`;
      if (!undo || manual) feedback(manual ? "Reports refreshed." : "");
    } catch (error) { if (current === revision) { showError(error.message); feedback("Could not refresh reports."); } }
  }
  async function changeDismissal(identifier, dismissed, isUndo = false) {
    if (pending.has(identifier)) return;
    pending.add(identifier); revision += 1; render();
    try {
      await request("status-reports/dismiss", {id: identifier, dismissed});
      undo = isUndo ? null : {id: identifier, dismissed: !dismissed};
      element("undo-status-report").hidden = !undo;
      previousSignature = ""; await load();
      feedback(dismissed ? "Report dismissed. Future reports from this agent will still appear." : "Report restored to the inbox.");
      if (undo) element("undo-status-report").focus({preventScroll: true});
    } catch (error) { showError(error.message); }
    finally { pending.delete(identifier); render(); }
  }
  for (const id of ["status-report-view", "status-report-workspace", "status-report-search"]) element(id).addEventListener("input", render);
  element("refresh-status-reports").onclick = () => load(true);
  element("undo-status-report").onclick = () => { if (undo) changeDismissal(undo.id, undo.dismissed, true); };
  element("dismiss-selected-status-report").onclick = () => {
    const report = reports.find(report => report.id === selected);
    if (report) changeDismissal(report.id, !report.dismissed_at);
  };
  element("close-status-report").onclick = () => {
    const previous = selected; closeReport(); render();
    document.querySelector(`[data-report-id="${previous}"] .status-report-title`)?.focus();
  };
  element("close-status-reports").onclick = () => root.parent !== root ? root.parent.postMessage({type: "ribbon-field-close-status"}, root.location.origin) : root.location.assign("/");
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
  const timer = root.setInterval(() => { if (!document.hidden && !pending.size) load(); }, 15000);
  root.addEventListener("pagehide", () => root.clearInterval(timer));
  load();
})(globalThis);
