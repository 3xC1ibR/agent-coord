/* Shared helpers, workspace launcher, and the standalone Activity report. */
(function (root) {
  "use strict";
  const metrics = [["messages", "Human messages"], ["visits", "Visits"], ["return_visits", "Return visits"],
    ["visits_without_message", "Visits without a message"], ["decisions", "Decisions"], ["threads", "Threads"]];
  function dateValue(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  }
  function localRange(from, through) {
    const parse = value => {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw Error("Choose a valid date range.");
      const [y, m, d] = value.split("-").map(Number), result = new Date(y, m - 1, d);
      if (dateValue(result) !== value) throw Error("Choose a valid date range.");
      return result;
    };
    const start = parse(from), last = parse(through);
    if (start > last) throw Error("The end date must be on or after the start date.");
    // Calendar arithmetic, not a fixed 86400 seconds: local days can cross DST.
    last.setDate(last.getDate() + 1);
    return {start: start.getTime() / 1000, end: last.getTime() / 1000};
  }
  function queryString(filters, cursor) {
    const params = new URLSearchParams();
    for (const key of ["start", "end", "project", "domain", "group", "thread_id", "group_by"])
      if (filters[key] != null) params.set(key, filters[key]);
    params.set("limit", "100"); if (cursor) params.set("cursor", cursor);
    return params.toString();
  }
  function reviewFilters(query) {
    const result = {...query.filters, start: query.start, end: Math.min(query.end, query.as_of), group_by: query.group_by};
    delete result.allowed_cwd;
    return Object.fromEntries(Object.entries(result).filter(([key, value]) => value != null && ["start", "end", "project", "domain", "group", "thread_id", "group_by"].includes(key)));
  }
  function coverageText(coverage, totals) {
    const parts = [];
    if (coverage.recording_started_at) parts.push("Recording began " + new Date(coverage.recording_started_at * 1000).toLocaleString() + ".");
    if (coverage.enabled === false) parts.push("Recording is paused.");
    if (coverage.history_cleared_at) parts.push("History was cleared; earlier records may be missing.");
    if (coverage.paused_intervals?.length) parts.push("This period includes gaps while recording was paused.");
    if (totals.incomplete_visits) parts.push(`${totals.incomplete_visits} incomplete visit${totals.incomplete_visits === 1 ? "" : "s"} excluded from visits without a message.`);
    if (coverage.note) parts.push(coverage.note);
    if (!coverage.complete && !parts.length) parts.push("Coverage is partial. Earlier activity is not reconstructed.");
    return parts.join(" ");
  }
  const helpers = {dateValue, localRange, queryString, reviewFilters, coverageText};
  if (typeof module !== "undefined" && module.exports) { module.exports = helpers; return; }
  const doc = root.document, $ = id => doc.getElementById(id);
  if ($("open-activity")) {
    const dialog = $("activity-dialog"), frame = $("activity-frame");
    $("open-activity").onclick = event => {
      event.preventDefault();
      const backdrop = $("sidebar-backdrop"); if (backdrop && !backdrop.hidden) backdrop.click();
      root.agentCoordActivity?.end(); root.agentCoordActivity?.flush();
      frame.src = "/activity?embedded=1"; dialog.showModal();
    };
    root.addEventListener("message", event => {
      if (event.origin !== root.location.origin || event.source !== frame.contentWindow) return;
      if (event.data?.type === "ribbon-field-close-activity") dialog.close();
      if (event.data?.type === "ribbon-field-activity-thread" && typeof event.data.thread_id === "string") {
        dialog.close();
        const id = event.data.thread_id;
        Promise.resolve(root.agentCoordNavigate?.("agentcoord://thread/" + encodeURIComponent(id))).then(() => {
          const recorder = root.agentCoordActivity;
          if (recorder?.thread !== id) return;
          recorder.setForeground(!doc.hidden && doc.hasFocus() && !doc.querySelector("dialog[open]"));
          recorder.interact(id, "navigation");
        });
      }
    });
    dialog.addEventListener("close", () => {
      frame.removeAttribute("src");
      ($("open-activity").getClientRects().length ? $("open-activity") : $("menu-toggle")).focus({preventScroll: true});
    });
    return;
  }
  if (!$("activity-filters")) return;
  let token, config, settings, query, applied, cursor, revision = 0, busy = false, dirty = true;
  let drilldown = null, reviewRequest = null, reviewSignature = null, reviewPending = false, reviewUnconfirmed = false;
  const events = new Map(), inventory = [];
  const channel = typeof root.BroadcastChannel === "function" ? new root.BroadcastChannel("ribbon-field-activity") : null;
  root.addEventListener("pagehide", () => channel?.close());
  const make = (tag, text, className) => { const el = doc.createElement(tag); if (text != null) el.textContent = text; if (className) el.className = className; return el; };
  const report = text => { $("activity-error").textContent = text; $("activity-error").hidden = !text; };
  const count = value => typeof value === "number" ? value.toLocaleString() : "—";
  const timestamp = epoch => Number.isFinite(epoch) ? new Date(epoch * 1000).toLocaleString() : "Unknown";
  async function request(path, body) {
    const options = {cache: "no-store"};
    if (body !== undefined) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-Agent-Coord-Token": token}, body: JSON.stringify(body)});
    const response = await fetch("/api/browser/" + path, options);
    let result; try { result = await response.json(); } catch { throw Error("Could not read the response. Check the connection and try again."); }
    if (!response.ok) throw Error(result.error || "Could not load activity. Check the connection and try again.");
    return result;
  }
  function threadLink(id, title) {
    const link = make("a", title || id); link.href = "/#" + encodeURIComponent(id);
    link.onclick = event => {
      if (root.parent !== root && !event.metaKey && !event.ctrlKey && !event.shiftKey) {
        event.preventDefault(); root.parent.postMessage({type: "ribbon-field-activity-thread", thread_id: id}, root.location.origin);
      }
    };
    return link;
  }
  function controls() {
    $("activity-apply").disabled = busy || !token || reviewPending;
    $("activity-apply").textContent = busy ? "Loading…" : "Apply";
    $("activity-review").disabled = busy || dirty || !query || !token || reviewPending;
    $("activity-more").disabled = busy || dirty;
    for (const key of ["start", "end", "project", "domain", "group", "group-by"])
      $("activity-" + key).disabled = busy || reviewPending;
    $("activity-question").disabled = reviewPending;
    $("activity-results").setAttribute("aria-busy", String(busy));
  }
  function filterValues() {
    const result = {...localRange($("activity-start").value, $("activity-end").value), group_by: $("activity-group-by").value};
    for (const key of ["project", "domain", "group"]) if ($("activity-" + key).value) result[key] = $("activity-" + key).value === "__unassigned__" ? "" : $("activity-" + key).value;
    if (drilldown) result[drilldown.field] = drilldown.value;
    return result;
  }
  function options() {
    const rows = [...inventory, ...(query?.groups || []), ...[...events.values()].map(event => event.current || event.snapshot || {})];
    for (const key of ["project", "domain", "group"]) {
      const select = $("activity-" + key), selected = select.value, values = new Map();
      for (const row of rows) {
        const value = key === "project" ? row.project_cwd || row.project || row.cwd : key === "domain" ? row.domain : row.group_id || row.group?.id;
        if (typeof value === "string" && value) values.set(value, key === "group" ? row.group_name || row.group?.name || value : value);
      }
      // Keep historical and currently selected values available across drilldowns.
      for (const option of [...select.options].slice(1)) values.set(option.value, option.textContent);
      if (key !== "project") values.set("__unassigned__", key === "domain" ? "No domain" : "No group");
      select.replaceChildren(new Option("All " + (key === "project" ? "projects" : key === "domain" ? "domains" : "groups"), ""),
        ...[...values].sort((a, b) => a[1].localeCompare(b[1])).map(([value, label]) => new Option(label, value)));
      select.value = selected;
    }
  }
  function renderGroups() {
    const label = {thread: "Thread", project: "Project", domain: "Domain", group: "Group"}[query.group_by];
    $("activity-breakdown-title").textContent = "By " + label.toLowerCase(); $("activity-group-column").textContent = label;
    const rows = (query.groups || []).map(group => {
      const tr = make("tr"), name = make("td"), button = make("button", group.label || "Unassigned", "activity-link"); button.type = "button";
      button.title = "Show activity for " + (group.label || "Unassigned");
      button.onclick = () => {
        drilldown = {field: query.group_by === "thread" ? "thread_id" : query.group_by, value: group.key ?? "", label: group.label || "Unassigned"};
        if (query.group_by !== "thread") $("activity-group-by").value = "thread";
        load();
      };
      name.append(button);
      if (query.group_by === "thread") {
        if (group.thread_id) {
          const open = threadLink(group.thread_id, "↗"); open.className = "activity-open-thread";
          open.title = "Open thread"; open.setAttribute("aria-label", "Open thread: " + group.label); name.append(open);
        }
        name.append(make("small", [group.project, group.domain].filter(Boolean).join(" · ")));
      }
      tr.append(name);
      for (const key of ["messages", "visits", "return_visits", "visits_without_message", "decisions"]) tr.append(make("td", count(group[key])));
      return tr;
    });
    if (!rows.length) { const row = make("tr"), cell = make("td", "No matching threads."); cell.colSpan = 6; row.append(cell); rows.push(row); }
    $("activity-groups").replaceChildren(...rows);
    $("activity-drilldown").hidden = !drilldown;
    $("activity-drilldown").querySelector("span").textContent = drilldown ? "Showing: " + drilldown.label : "";
  }
  function renderEvents() {
    const labels = {visit_started: "Visit started", visit_ended: "Visit ended", decision_submitted: "Decision submitted", message_submitted: "Human message", organization_changed: "Organization changed"};
    const rows = [...events.values()].sort((a,b) => a.occurred_at - b.occurred_at || a.sequence - b.sequence).map(event => {
      const exposure = event.actor === "system" && {visit_started: "Foreground exposure", visit_ended: "Exposure ended"}[event.kind];
      const tr = make("tr"), when = make("td", timestamp(event.occurred_at)), kind = make("td", exposure || labels[event.kind] || event.kind.replaceAll("_", " "));
      const detail = make("details"), summary = make("summary", "Event details");
      detail.append(summary, make("pre", JSON.stringify(event, null, 2))); kind.append(detail);
      const thread = make("td");
      if (event.thread_id) thread.append(threadLink(event.thread_id, event.current?.title || event.snapshot?.title || event.thread_id));
      else thread.textContent = "—";
      const source = make("td", event.source || "Unknown"); source.append(make("small", event.actor || "Unknown actor"));
      tr.append(when, kind, thread, source); return tr;
    });
    if (!rows.length) { const row = make("tr"), cell = make("td", "No recorded events in this selection."); cell.colSpan = 4; row.append(cell); rows.push(row); }
    $("activity-events").replaceChildren(...rows); $("activity-more").hidden = !cursor;
  }
  function render() {
    $("activity-totals").replaceChildren(...metrics.map(([key, label]) => { const block = make("div"); block.append(make("dt", label), make("dd", count(query.totals?.[key]))); return block; }));
    $("activity-coverage").textContent = coverageText(query.coverage || {}, query.totals || {});
    $("activity-cutoff").textContent = "Through " + timestamp(query.end);
    const empty = !(query.groups?.length || events.size);
    $("activity-empty").hidden = !empty;
    doc.querySelector(".activity-review").hidden = empty;
    $("activity-events-title").closest("section").hidden = empty;
    $("activity-breakdown-title").closest("section").hidden = empty;
    options(); renderGroups(); renderEvents();
  }
  async function load(more = false) {
    const version = ++revision;
    try {
      const filters = more ? applied : filterValues();
      busy = true; report(""); controls();
      if (!more) { $("activity-status").hidden = false; $("activity-status").textContent = "Loading activity…"; $("activity-results").hidden = true; }
      const result = await request("activity?" + queryString(filters, more ? cursor : null));
      if (version !== revision) return;
      if (!more) {
        query = result; applied = {...filters, start: result.start, end: result.end}; events.clear(); dirty = false;
      }
      for (const event of result.events || []) events.set(event.event_id, event);
      cursor = result.next_cursor; render();
      $("activity-status").hidden = true; $("activity-results").hidden = false;
    } catch (error) { if (version === revision) { report(error.message); $("activity-status").hidden = true; if (!more) { query = null; dirty = true; } } }
    finally { if (version === revision) { busy = false; controls(); } }
  }
  function renderSettings() {
    $("activity-recording").textContent = settings.enabled ? "Recording on" : "Recording paused";
    $("activity-toggle").textContent = settings.enabled ? "Pause recording" : "Resume recording";
    $("activity-toggle").disabled = false; $("activity-clear").disabled = false;
  }
  $("activity-filters").onsubmit = event => { event.preventDefault(); drilldown = null; load(); };
  $("activity-filters").oninput = () => { dirty = true; controls(); };
  $("activity-group-by").onchange = () => load();
  $("activity-drilldown").querySelector("button").onclick = () => { drilldown = null; load(); };
  $("activity-more").onclick = () => load(true);
  $("activity-review-form").onsubmit = async event => {
    event.preventDefault(); if (!query || dirty || reviewPending) return;
    const payload = {filters: reviewFilters(query), question: $("activity-question").value.trim()};
    const signature = JSON.stringify(payload);
    if (!reviewRequest || !reviewUnconfirmed && reviewSignature !== signature) {
      reviewRequest = {...payload, request_id: crypto.randomUUID()}; reviewSignature = signature;
      if (config.cwd) reviewRequest.cwd = config.cwd;
    }
    reviewPending = true; reviewUnconfirmed = true; controls(); $("activity-review").textContent = "Starting review…";
    const result = $("activity-review-result"); result.hidden = false; result.textContent = "Creating a conversation…";
    try {
      const response = await request("activity/review", reviewRequest);
      reviewUnconfirmed = response.status !== "completed";
      result.replaceChildren(make("span", response.status === "completed" ? "Review ready. " : "Review request saved. "));
      if (response.thread_id) result.append(threadLink(response.thread_id, "Open review ↗"));
      if (response.error) result.append(make("span", " " + response.error));
      if (reviewUnconfirmed) result.append(make("span", " Startup is unconfirmed. Check this review before starting another."));
      $("activity-review").textContent = response.status === "completed" ? "Review with an agent" : "Check review";
    } catch (error) { result.textContent = error.message + " Retry checks the same request."; $("activity-review").textContent = "Retry review"; }
    finally { reviewPending = false; controls(); }
  };
  $("activity-toggle").onclick = async () => {
    $("activity-toggle").disabled = true;
    try { settings = await request("activity/settings", {enabled: !settings.enabled}); channel?.postMessage({enabled: settings.enabled}); renderSettings(); await load(); }
    catch (error) { report(error.message); $("activity-toggle").disabled = false; }
  };
  $("activity-clear").onclick = () => { $("activity-clear-error").hidden = true; $("activity-clear-dialog").showModal(); };
  $("activity-clear-cancel").onclick = () => $("activity-clear-dialog").close();
  $("activity-clear-confirm").onclick = async () => {
    $("activity-clear-confirm").disabled = true;
    try { await request("activity/clear", {}); channel?.postMessage({type: "clear"}); $("activity-clear-dialog").close(); await load(); }
    catch (error) { $("activity-clear-error").textContent = error.message; $("activity-clear-error").hidden = false; }
    finally { $("activity-clear-confirm").disabled = false; }
  };
  $("close-activity").onclick = () => {
    if (root.parent !== root) root.parent.postMessage({type: "ribbon-field-close-activity"}, root.location.origin);
    else root.location.href = "/";
  };
  doc.addEventListener("keydown", event => { if (event.key === "Escape" && !event.isComposing && !doc.querySelector("dialog[open]")) { event.preventDefault(); $("close-activity").click(); } });
  async function boot() {
    controls();
    const today = new Date(), start = new Date(today); start.setDate(start.getDate() - 6);
    $("activity-start").value = dateValue(start); $("activity-end").value = dateValue(today);
    $("activity-zone").textContent = "Dates use " + Intl.DateTimeFormat().resolvedOptions().timeZone + ". End date is inclusive.";
    config = await request("config"); token = config.token;
    const settingsTask = request("activity/settings").then(value => { settings = value; renderSettings(); }).catch(() => { $("activity-recording").textContent = "Recording status unavailable"; });
    const inventoryTask = Promise.allSettled([request("threads"), request("threads?archived=true")]).then(results => {
      for (const result of results) if (result.status === "fulfilled") inventory.push(...(result.value.data || [])); options();
    });
    await load(); await Promise.all([settingsTask, inventoryTask]);
  }
  boot().catch(error => { report(error.message); $("activity-status").hidden = true; });
})(typeof globalThis !== "undefined" ? globalThis : this);
