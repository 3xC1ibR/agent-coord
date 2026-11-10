(function (root) {
  "use strict";
  const numeric = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const count = value => numeric(value) ? new Intl.NumberFormat(undefined, {notation: value >= 10000 ? "compact" : "standard", maximumFractionDigits: value < 10000 && !Number.isInteger(value) ? 2 : 1}).format(value) : "Unavailable";
  function bytes(value) {
    if (!numeric(value)) return "Unavailable";
    if (value < 1000) return `${count(value)} B`;
    const exponent = Math.min(3, Math.floor(Math.log10(value) / 3));
    return `${new Intl.NumberFormat(undefined, {maximumFractionDigits: 1}).format(value / 1000 ** exponent)} ${["B", "kB", "MB", "GB"][exponent]}`;
  }
  function comparison(current, previous) {
    if (!numeric(current) || !numeric(previous)) return "Previous measurement unavailable";
    if (previous === 0) return current === 0 ? "No change" : "No previous value";
    const change = (current - previous) / previous * 100;
    if (Math.abs(change) < .05) return "No change";
    return `${new Intl.NumberFormat(undefined, {maximumFractionDigits: 1}).format(Math.abs(change))}% ${change < 0 ? "less" : "more"} than previous period`;
  }
  function tokensAvailable(payload) {
    return payload?.measurement?.tokenizer_available === true && payload?.measurement?.unit === "tokens";
  }
  function value(row, kind, tokenMode) {
    const candidate = row?.[`${kind}_${tokenMode ? "tokens" : "bytes"}`];
    return numeric(candidate) ? candidate : null;
  }
  function coverageSummary(payload) {
    const providers = (payload.coverage?.providers || []).map(name => name === "codex" ? "Codex" : name).join(", ");
    const incomplete = ["running", "partial"].includes(payload.status?.state) || sourceRecoveryPending(payload);
    return `${providers || "Provider coverage unavailable"} · MCP + standalone CLI${incomplete ? " · Collection incomplete" : ""}. Compound commands and unsupported providers are excluded.`;
  }
  function measurementCoverage(row, kind) {
    const measured = row?.[`${kind}_token_calls`], calls = row?.calls;
    if (!numeric(measured) || !numeric(calls) || measured > calls) return {complete: false, caption: "Measurement coverage unavailable"};
    return {complete: measured === calls, caption: measured === calls ? "" : `Measured ${measured.toLocaleString()} of ${calls.toLocaleString()} calls`};
  }
  function tokenComparisonAvailable(current, previous, kind) {
    return measurementCoverage(current, kind).complete && measurementCoverage(previous, kind).complete;
  }
  function sourceRecoveryPending(payload) {
    const status = payload.status || {}, progress = status.progress || {}, coverage = payload.coverage || {};
    return status.running === true || progress.has_more === true ||
      (numeric(coverage.files_done) && numeric(coverage.files_total) && coverage.files_done < coverage.files_total) ||
      (numeric(progress.files_done) && numeric(progress.files_total) && progress.files_done < progress.files_total);
  }
  function createPoller({visible, read, schedule = setTimeout, cancel = clearTimeout, delay = 5000}) {
    let timer = null, running = false, disposed = false;
    function clear() { if (timer !== null) cancel(timer); timer = null; }
    function arm() {
      clear();
      if (!disposed && running && visible()) timer = schedule(() => {
        timer = null;
        if (!disposed && running && visible()) read();
      }, delay);
    }
    return {
      update(nextRunning) { running = nextRunning === true; arm(); },
      visibility() { clear(); if (!disposed && running && visible()) read(); },
      stop() { disposed = true; clear(); },
    };
  }
  function series(rows, key) {
    return (rows || []).slice(0, 366).map(row => ({date: row.date, value: numeric(row[key]) ? row[key] : null}));
  }
  const api = {numeric, count, bytes, comparison, tokensAvailable, value, coverageSummary, measurementCoverage, tokenComparisonAvailable, sourceRecoveryPending, createPoller, series};
  if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }
  root.agentCoordCoordinationMetrics = api;
  const $ = id => document.getElementById(id);
  if (!$("coordination-results")) return;
  const make = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  let token = null, snapshot = null, pending = false, disposed = false, sequence = 0, activeRead = null, refreshController = null, automaticallyRequested = false, failed = false;
  const poller = createPoller({visible: () => !document.hidden, read: () => load()});
  const report = message => {
    $("coordination-error").textContent = message || ""; $("coordination-error").hidden = !message;
    if (message) $("coordination-status").textContent = snapshot ? "Showing the last saved measurements. The update is unavailable." : "Coordination measurements are unavailable.";
  };
  function controls() {
    const running = snapshot?.status?.running === true;
    $("refresh-coordination").disabled = pending || ((!token || running) && !failed);
    $("refresh-coordination").textContent = failed ? "Retry" : pending || running ? "Updating…" : "Refresh";
  }
  function dateText(seconds, options) {
    return numeric(seconds) ? new Date(seconds * 1000).toLocaleDateString(undefined, options || {month: "short", day: "numeric", timeZone: "UTC"}) : null;
  }
  function metric(label, current, previous, format, note, comparable = true, unavailable = "Comparison unavailable: incomplete coverage") {
    const item = make("div"), number = make("dd", format(current));
    if (numeric(current)) number.title = current.toLocaleString();
    item.append(make("dt", label), number, make("small", comparable ? comparison(current, previous) : unavailable));
    if (note) item.append(make("small", note));
    return item;
  }
  function renderChart(payload, tokenMode) {
    const container = $("coordination-chart");
    container.replaceChildren();
    const rows = (payload.daily || []).slice(0, 366);
    if (!rows.length) { container.append(make("p", "Daily measurements are unavailable.", "coordination-note")); return; }
    const argumentsSeries = series(rows, tokenMode ? "argument_tokens" : "argument_bytes");
    const returnsSeries = series(rows, tokenMode ? "return_tokens" : "return_bytes");
    const values = [...argumentsSeries, ...returnsSeries].filter(point => point.value !== null).map(point => point.value);
    if (!values.length) { container.append(make("p", "Daily measurements are unavailable.", "coordination-note")); return; }
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const width = Math.max(320, container.clientWidth || 900);
    svg.setAttribute("viewBox", `0 0 ${width} 210`); svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `Daily ${tokenMode ? "token" : "byte"} payloads: arguments and commands shown as a solid line; returned text as a dashed line.`);
    const node = (tag, attributes, text) => {
      const element = document.createElementNS(svg.namespaceURI, tag);
      Object.entries(attributes).forEach(([key, item]) => element.setAttribute(key, item));
      if (text != null) element.textContent = text;
      svg.append(element); return element;
    };
    const maximum = Math.max(1, ...values), left = 64, right = width - 12, top = 14, bottom = 175;
    const x = index => left + (right - left) * index / Math.max(1, rows.length - 1);
    const y = amount => bottom - (bottom - top) * amount / maximum;
    for (const fraction of [0, .5, 1]) {
      const height = y(maximum * fraction);
      node("line", {x1: left, y1: height, x2: right, y2: height, class: "chart-grid"});
      node("text", {x: left - 10, y: height + 4, "text-anchor": "end"}, tokenMode ? count(maximum * fraction) : bytes(maximum * fraction));
    }
    for (const [points, className] of [[argumentsSeries, "argument-line"], [returnsSeries, "return-line"]]) {
      let path = "", connected = false;
      points.forEach((point, index) => {
        if (point.value === null) { connected = false; return; }
        path += `${connected ? "L" : "M"}${x(index).toFixed(1)},${y(point.value).toFixed(1)} `; connected = true;
      });
      node("path", {d: path, class: className});
      if (points.length === 1 && points[0].value !== null) node("circle", {cx: x(0), cy: y(points[0].value), r: 3, class: "chart-point"});
    }
    const indices = [...new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1])];
    for (const index of indices) {
      const date = new Date(`${rows[index].date}T12:00:00Z`);
      node("text", {x: x(index), y: 203, "text-anchor": index === 0 ? "start" : index === rows.length - 1 ? "end" : "middle"}, Number.isFinite(date.getTime()) ? date.toLocaleDateString(undefined, {month: "short", day: "numeric", timeZone: "UTC"}) : rows[index].date);
    }
    container.append(svg);
  }
  function render(payload) {
    snapshot = payload;
    const status = payload.status || {}, current = payload.current || {}, previous = payload.previous || {};
    const tokenMode = tokensAvailable(payload), format = tokenMode ? count : bytes;
    const recovering = sourceRecoveryPending(payload), comparisonUnavailable = recovering ? "Indexing incomplete" : "Comparison unavailable: incomplete coverage";
    const state = status.state || "unavailable";
    const defaultStatus = {not_loaded: "No measurements collected yet.", running: "Reading available records in the background…", ready: "Measurements are up to date.", partial: "Measurements cover part of the available history.", unavailable: "Measurements are unavailable.", error: "The latest update could not finish."};
    const observed = numeric(status.updated_at) ? ` Updated ${new Date(status.updated_at * 1000).toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"})}.` : "";
    $("coordination-status").textContent = (status.message || defaultStatus[state] || defaultStatus.unavailable) + observed;
    $("coordination-coverage-summary").textContent = coverageSummary(payload);
    $("coordination-coverage-summary").hidden = false;
    const days = payload.period?.days || Number($("coordination-period").value);
    const start = dateText(payload.period?.start), end = dateText(numeric(payload.period?.end) ? payload.period.end - 1 : null);
    $("coordination-range").textContent = `${start && end ? `${start}–${end} (UTC). ` : ""}${recovering ? "Comparison resumes after indexing completes." : `Compared with the previous ${days} days.`}`;
    $("coordination-measurement").textContent = tokenMode ? "Payload tokens, counted once. These are not billed input or output totals." : "Token counts are unavailable. Recorded text sizes are shown in bytes.";
    const totals = $("coordination-totals");
    const fieldMetric = (kind, label) => metric(label, value(current, kind, tokenMode), value(previous, kind, tokenMode), format,
      tokenMode ? measurementCoverage(current, kind).caption : null, !recovering && (!tokenMode || tokenComparisonAvailable(current, previous, kind)), comparisonUnavailable);
    totals.replaceChildren(
      fieldMetric("argument", tokenMode ? "Argument / command tokens" : "Argument / command bytes"),
      fieldMetric("return", tokenMode ? "Returned payload tokens" : "Returned payload bytes"),
      metric("Lifecycle tokens", tokenMode ? current.lifecycle_tokens : null, tokenMode ? previous.lifecycle_tokens : null, count,
        tokenMode ? measurementCoverage(current, "lifecycle").caption : null, !recovering && (!tokenMode || tokenComparisonAvailable(current, previous, "lifecycle")), comparisonUnavailable),
      metric("Coordination calls", current.calls, previous.calls, count, null, !recovering, comparisonUnavailable)
    );
    const turnText = [numeric(current.threads) ? `${count(current.threads)} threads` : "Thread coverage unavailable", numeric(current.turns) ? `${count(current.turns)} observed turns` : "Turn coverage unavailable"];
    if (numeric(current.calls_per_turn)) turnText.push(`${count(current.calls_per_turn)} calls per observed turn`);
    if (tokenMode && numeric(current.tokens_per_turn) && measurementCoverage(current, "argument").complete && measurementCoverage(current, "return").complete) turnText.push(`${count(current.tokens_per_turn)} payload tokens per observed turn`);
    $("coordination-turns").textContent = turnText.join(" · ");
    const empty = current.calls === 0;
    $("coordination-empty").hidden = !empty;
    $("coordination-daily-section").hidden = empty;
    $("coordination-tools-section").hidden = empty;
    $("coordination-table-unit").textContent = tokenMode ? "Payload tokens · argument percentiles per measured call" : "Payload bytes · token percentiles unavailable";
    const tableRows = [];
    for (const row of (payload.tools || []).slice(0, 200)) {
      const tr = make("tr"), name = make("td", row.operation || "Unknown operation");
      name.append(make("small", row.transport === "cli" ? "CLI" : row.transport === "mcp" ? "MCP" : "Unknown transport"));
      tr.append(name);
      const fields = [[row.calls, count], [value(row, "argument", tokenMode), format, "argument"], [value(row, "return", tokenMode), format, "return"],
        [tokenMode ? row.lifecycle_tokens : null, count, "lifecycle"], [tokenMode ? row.p50_argument_tokens : null, count], [tokenMode ? row.p95_argument_tokens : null, count]];
      for (const [amount, formatter, kind] of fields) {
        const cell = make("td", formatter(amount));
        if (numeric(amount)) cell.title = amount.toLocaleString();
        if (tokenMode && kind) {
          const coverage = measurementCoverage(row, kind);
          if (!coverage.complete) cell.append(make("small", coverage.caption));
        }
        tr.append(cell);
      }
      tableRows.push(tr);
    }
    $("coordination-tools").replaceChildren(...tableRows);
    renderChart(payload, tokenMode);
    const providers = (payload.coverage?.providers || []).map(name => name === "codex" ? "Codex" : name).join(", ");
    $("coordination-coverage-scope").textContent = [payload.coverage?.scope, providers && `Providers: ${providers}`, tokenMode && `Reference tokenizer: ${payload.measurement.method}`].filter(Boolean).join(" · ");
    $("coordination-coverage-notes").replaceChildren(...(payload.coverage?.notes || []).map(note => make("li", note)));
    $("coordination-results").hidden = !payload.current;
    controls(); poller.update(status.running === true);
  }
  async function request(path, {body, signal} = {}) {
    const options = {cache: "no-store", signal};
    if (body) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-Agent-Coord-Token": token}, body: JSON.stringify(body)});
    const response = await fetch(`/api/browser/${path}`, options);
    if (!response.ok) throw new Error("Could not load coordination measurements. Check the connection and try again.");
    return response.json();
  }
  async function load() {
    if (disposed || document.hidden) return;
    const requestNumber = ++sequence;
    activeRead?.abort(); activeRead = new AbortController();
    try {
      const payload = await request(`coordination-metrics?days=${encodeURIComponent($("coordination-period").value)}`, {signal: activeRead.signal});
      if (disposed || requestNumber !== sequence) return;
      failed = false; report(""); render(payload);
    } catch (error) {
      if (error.name === "AbortError" || requestNumber !== sequence || disposed) return;
      failed = true; report(error.message); poller.update(false); controls();
    }
  }
  async function refresh() {
    if (failed && !token) { failed = false; controls(); await boot(); return; }
    if (failed && snapshot?.status?.running) { await load(); return; }
    if (!token || pending || snapshot?.status?.running || document.hidden || disposed) return;
    pending = true; controls(); refreshController = new AbortController();
    try {
      await request("coordination-metrics/refresh", {body: {}, signal: refreshController.signal});
      await load();
    } catch (error) {
      if (error.name !== "AbortError" && !disposed) { failed = true; report(error.message); }
    } finally { pending = false; controls(); }
  }
  $("refresh-coordination").onclick = refresh;
  $("coordination-period").onchange = () => load();
  $("close-coordination").onclick = () => {
    if (root.parent !== root) root.parent.postMessage({type: "ribbon-field-close-usage"}, location.origin);
    else location.href = "/";
  };
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !event.isComposing && !event.defaultPrevented) { event.preventDefault(); $("close-coordination").click(); }
  });
  async function automaticallyRefresh() {
    if (!automaticallyRequested && snapshot?.status?.state === "not_loaded") {
      automaticallyRequested = true; await refresh();
    }
  }
  document.addEventListener("visibilitychange", async () => {
    if (document.hidden) activeRead?.abort();
    if (!document.hidden && !snapshot) { await load(); await automaticallyRefresh(); }
    else poller.visibility();
  });
  root.addEventListener("resize", () => { if (snapshot && !document.hidden) renderChart(snapshot, tokensAvailable(snapshot)); });
  root.addEventListener("pagehide", () => { disposed = true; activeRead?.abort(); refreshController?.abort(); poller.stop(); }, {once: true});
  async function boot() {
    try {
      const config = await request("config"); token = config.token; controls();
      await load();
      await automaticallyRefresh();
    } catch (error) { if (!disposed) { failed = true; report(error.message); controls(); } }
  }
  boot();
})(typeof globalThis !== "undefined" ? globalThis : this);
