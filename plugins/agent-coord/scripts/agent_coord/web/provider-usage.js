/* Shared by the workspace launcher and the standalone/embedded Usage page. */
(function (root) {
  "use strict";
  const numeric = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
  function remaining(used) { return numeric(used) ? Math.max(0, Math.min(100, 100 - used)) : null; }
  function countdown(reset, now = Date.now() / 1000) {
    if (!numeric(reset)) return "Reset time unavailable";
    const seconds = reset - now;
    if (seconds <= 0) return "Reset time passed · awaiting updated usage";
    const minutes = Math.ceil(seconds / 60), days = Math.floor(minutes / 1440), hours = Math.floor(minutes % 1440 / 60);
    return "Resets in " + (days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`);
  }
  function status(row, now = Date.now() / 1000) {
    if (row.observed_at != null && (row.state !== "ready" || now - row.observed_at >= 120)) return "Last known usage";
    return {ready: "Up to date", not_loaded: "Not loaded", unsupported: "Unavailable",
      sign_in_required: "Sign-in needed", unavailable: "Unavailable", error: "Refresh failed"}[row.state] || "Unavailable";
  }
  function due(row, now = Date.now() / 1000) { return row.state !== "unsupported" && now >= (row.next_refresh_at || 0); }
  const api = {remaining, countdown, status, due};
  if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }
  root.agentCoordProviderUsage = api;
  const $ = id => document.getElementById(id);
  if ($("open-usage")) {
    const dialog = $("usage-dialog"), frame = $("usage-frame");
    $("open-usage").onclick = event => {
      event.preventDefault();
      const backdrop = $("sidebar-backdrop");
      if (backdrop && !backdrop.hidden) backdrop.click();
      frame.src = "/usage?embedded=1";
      dialog.showModal();
    };
    root.addEventListener("message", event => {
      if (event.origin === location.origin && event.source === frame.contentWindow && event.data?.type === "ribbon-field-close-usage") dialog.close();
    });
    dialog.addEventListener("close", () => {
      frame.removeAttribute("src");
      ($("open-usage").getClientRects().length ? $("open-usage") : $("menu-toggle")).focus({preventScroll: true});
    });
    return;
  }
  if (!$("usage-providers")) return;
  let token, rows = new Map(), pending = new Set();
  const report = message => { $("usage-error").textContent = message; $("usage-error").hidden = !message; };
  const make = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  function render() {
    const active = document.activeElement?.id;
    const cards = [];
    for (const row of rows.values()) {
      const card = make("section", null, "usage-card"), header = make("header"), identity = make("div");
      const heading = make("h2", row.name); heading.id = `usage-title-${row.provider}`;
      card.setAttribute("aria-labelledby", heading.id);
      identity.append(heading);
      const detail = [row.account, row.plan].filter(Boolean).join(" · ");
      if (detail) identity.append(make("p", detail, "usage-account"));
      const badge = make("span", null, "usage-status"); badge.dataset.providerStatus = row.provider;
      header.append(identity, badge); card.append(header);
      for (const window of row.windows || []) {
        const section = make("div", null, "quota-window"), label = make("div", null, "quota-label");
        const left = remaining(window.used_percent);
        section.dataset.level = left === 0 ? "empty" : left !== null && left <= 20 ? "low" : "normal";
        label.append(make("span", window.label), make("strong", left === null ? "Unknown" : `${Math.round(left)}% remaining`));
        section.append(label);
        if (left !== null) {
          const bar = make("progress", null, "quota-bar"); bar.max = 100; bar.value = left;
          bar.setAttribute("aria-label", `${window.label}: ${Math.round(left)}% remaining`);
          section.append(bar);
        } else section.append(make("p", "The provider did not report a usage percentage.", "quota-unknown"));
        const reset = make("p", null, "quota-reset"), relative = make("span");
        relative.dataset.reset = numeric(window.resets_at) ? String(window.resets_at) : "";
        reset.append(relative);
        if (numeric(window.resets_at)) {
          const date = new Date(window.resets_at * 1000);
          const absolute = make("time", date.toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short"}));
          absolute.dateTime = date.toISOString(); reset.append(absolute);
        }
        section.append(reset); card.append(section);
      }
      if (row.message) card.append(make("p", row.message, "usage-message"));
      const footer = make("footer"), checked = make("span"); checked.dataset.observed = row.provider;
      footer.append(checked);
      if (row.state !== "unsupported") {
        const button = make("button", "Refresh"); button.type = "button";
        button.id = `refresh-provider-${row.provider}`; button.dataset.refreshProvider = row.provider;
        button.setAttribute("aria-label", `Refresh ${row.name} usage`);
        button.onclick = () => refresh(row.provider);
        footer.append(button);
      }
      card.append(footer); cards.push(card);
    }
    $("usage-providers").replaceChildren(...cards);
    tick();
    if (active) $(active)?.focus({preventScroll: true});
  }
  function merge(payload) {
    for (const row of payload.providers || []) {
      const previous = rows.get(row.provider);
      if (!previous || (row.checked_at || 0) >= (previous.checked_at || 0)) rows.set(row.provider, row);
    }
    render();
  }
  function tick() {
    const now = Date.now() / 1000;
    document.querySelectorAll("[data-reset]").forEach(node => {
      node.textContent = countdown(node.dataset.reset === "" ? null : Number(node.dataset.reset), now);
    });
    document.querySelectorAll("[data-provider-status]").forEach(node => {
      const row = rows.get(node.dataset.providerStatus);
      node.textContent = pending.has(row.provider) ? "Refreshing…" : status(row, now);
      node.dataset.stale = String(status(row, now) === "Last known usage");
    });
    document.querySelectorAll("[data-observed]").forEach(node => {
      const row = rows.get(node.dataset.observed), age = row.observed_at == null ? null : Math.max(0, Math.floor((now - row.observed_at) / 60));
      node.textContent = age === null ? "No reading yet" : age === 0 ? "Updated just now" : `Updated ${age}m ago`;
    });
    document.querySelectorAll("[data-refresh-provider]").forEach(button => {
      const row = rows.get(button.dataset.refreshProvider);
      button.disabled = pending.has(row.provider) || !due(row, now);
      button.title = button.disabled && !pending.has(row.provider) ? `Next refresh in ${Math.max(1, Math.ceil(row.next_refresh_at - now))}s` : "Refresh account limits";
    });
    $("refresh-usage").disabled = !token || ![...rows.values()].some(row => due(row, now) && !pending.has(row.provider));
  }
  async function request(path, body) {
    const options = {cache: "no-store"};
    if (body) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-Agent-Coord-Token": token}, body: JSON.stringify(body)});
    const response = await fetch("/api/browser/" + path, options);
    if (!response.ok) throw Error("Could not refresh usage. Check the connection and try again.");
    return response.json();
  }
  async function refresh(provider) {
    if (!token || pending.has(provider) || !due(rows.get(provider))) return;
    pending.add(provider); tick();
    try { merge(await request("provider-usage/refresh", {provider})); report(""); }
    catch (error) { report(error.message); }
    finally { pending.delete(provider); tick(); }
  }
  function refreshAll() { if (!document.hidden) for (const provider of rows.keys()) refresh(provider); }
  $("refresh-usage").onclick = refreshAll;
  $("close-usage").onclick = () => {
    if (root.parent !== root) root.parent.postMessage({type: "ribbon-field-close-usage"}, location.origin);
    else location.href = "/";
  };
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !event.isComposing && !event.defaultPrevented) { event.preventDefault(); $("close-usage").click(); }
  });
  document.addEventListener("visibilitychange", refreshAll);
  async function boot() {
    $("refresh-usage").disabled = true;
    const config = await request("config"); token = config.token;
    merge(await request("provider-usage")); refreshAll();
    const timer = setInterval(() => { if (!document.hidden) { tick(); refreshAll(); } }, 10000);
    root.addEventListener("pagehide", () => clearInterval(timer), {once: true});
  }
  boot().catch(error => report(error.message));
})(typeof globalThis !== "undefined" ? globalThis : this);
