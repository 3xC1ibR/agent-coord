"use strict";

// Kept separate from card rendering so list refreshes need no per-card listeners.
// The hover preview doubles as a lens: what the agent is doing now, where it
// stands, what comes next, what came before, its sub-agents, and the latest
// message. Clicking inside pins it.
function createThreadHover({overview, document, window, getThread, loadPreview, relativeTime,
  markdown = typeof module !== "undefined" && module.exports ? require("./markdown.js") : globalThis.messageMarkdown,
  grouping = globalThis.threadGrouping, providerLogo = globalThis.providerLogo, select = null,
  schedule = setTimeout, cancel = clearTimeout, cacheMs = 3000}) {
  const panel = document.createElement("aside");
  panel.id = "thread-hover"; panel.className = "thread-hover"; panel.hidden = true;
  panel.setAttribute("role", "tooltip"); panel.dataset.pinned = "false";
  document.body.append(panel);
  let anchor = null, current = null, openTimer, closeTimer, request = 0, pointerInside = false, pinned = false, trail = [];
  const cache = new Map();
  const PROVIDERS = {codex: "Codex", claude: "Claude", kimi: "Kimi", antigravity: "Antigravity"};
  const VERBS = {run: "Running", edit: "Editing", read: "Reading", tool: "Using"};
  function element(tag, text, className) {
    const el = document.createElement(tag); el.textContent = text; el.className = className || ""; return el;
  }
  function markdownBody(text, className) {
    const el = element("div", "", className + " markdown");
    el.innerHTML = markdown.render(text);
    return el;
  }
  function statusOf(thread) {
    if (grouping) return {...grouping.status(thread), phase: grouping.phaseLabel(thread)};
    const running = thread.response_state === "working" || thread.status === "running";
    const key = running ? "running" : ["input", "action"].includes(thread.response_state) ? "waiting" : thread.response_state === "failed" ? "failed" : "idle";
    return {key, label: {running: "Working", waiting: "Needs you", failed: "Failed", idle: "Idle"}[key], phase: thread.checkpoint?.phase || ""};
  }
  function activityOf(thread, status) {
    const tool = thread.last_tool;
    if (status.key === "running") {
      if (tool?.label || tool?.name) return {text: (VERBS[tool.kind] || "Using") + " " + (tool.label || tool.name), when: tool.at ? relativeTime(tool.at) : "", live: true};
      return {text: "Working", when: thread.updated_at ? "since " + relativeTime(thread.updated_at) : "", live: true};
    }
    if (status.key === "waiting") return {text: "Paused until you answer", when: thread.updated_at ? relativeTime(thread.updated_at) : "", live: false};
    if (status.key === "failed") return {text: "Stopped after a failure", when: thread.updated_at ? relativeTime(thread.updated_at) : "", live: false};
    const last = tool?.at && thread.updated_at ? Math.max(tool.at, thread.updated_at) : tool?.at || thread.updated_at;
    return {text: "Idle" + (status.phase ? " · " + status.phase : ""), when: last ? "since " + relativeTime(last) : "", live: false};
  }
  function hide() {
    cancel(openTimer); cancel(closeTimer); request++;
    anchor?.removeAttribute("aria-describedby");
    anchor = null; current = null; pointerInside = false; pinned = false; trail = [];
    panel.hidden = true; panel.dataset.pinned = "false"; panel.setAttribute("role", "tooltip");
  }
  function position() {
    if (!anchor?.isConnected) { hide(); return; }
    const rect = anchor.getBoundingClientRect(), gap = 10, margin = 12;
    const width = panel.offsetWidth, height = panel.offsetHeight;
    let left = rect.right + gap, top = rect.top;
    if (left + width > window.innerWidth - margin) {
      if (rect.left - width - gap >= margin) left = rect.left - width - gap;
      else { left = rect.left; top = rect.bottom + gap;
        if (top + height > window.innerHeight - margin) top = rect.top - height - gap; }
    }
    panel.style.left = Math.max(margin, Math.min(left, window.innerWidth - width - margin)) + "px";
    panel.style.top = Math.max(margin, Math.min(top, window.innerHeight - height - margin)) + "px";
  }
  function agentRow(child) {
    const status = statusOf(child), activity = activityOf(child, status);
    const button = element("button", "", "hover-agent"); button.type = "button";
    const dot = element("span", "", "dot " + status.key); dot.setAttribute("aria-hidden", "true");
    const copy = element("span", "", "hover-agent-copy");
    copy.append(element("span", child.title, "hover-agent-title"),
      element("small", activity.live ? activity.text : (status.label !== status.phase && status.label) || status.phase || activity.text, "hover-agent-status"));
    button.append(dot, copy);
    button.setAttribute("aria-label", "Peek at " + child.title);
    button.onclick = event => { event?.stopPropagation?.(); if (current) trail.push(current.thread); pinned = true; show(anchor, child.thread_id, {thread: child}); };
    return button;
  }
  function render(thread, data, {loading = false, failed = false} = {}) {
    panel.replaceChildren();
    panel.dataset.pinned = pinned ? "true" : "false";
    panel.setAttribute("role", pinned ? "dialog" : "tooltip");
    panel.append(element("h3", thread.title, "hover-title"));
    const status = statusOf(thread), activity = activityOf(thread, status);
    const state = element("div", "", "hover-state");
    const logo = providerLogo?.(thread.client, PROVIDERS[thread.client]);
    if (logo) state.append(logo);
    const dot = element("span", "", "dot " + status.key); dot.setAttribute("aria-hidden", "true");
    state.append(dot, element("span", status.label, "hover-state-label"));
    if (status.phase && status.phase !== status.label) state.append(element("span", "· " + status.phase, "hover-state-phase"));
    if (trail.length) {
      const back = element("button", "← " + trail[trail.length - 1].title, "hover-back"); back.type = "button";
      back.onclick = event => { event?.stopPropagation?.(); const parent = trail.pop(); show(anchor, parent.thread_id, {thread: parent}); };
      state.append(back);
    }
    panel.append(state);
    if (pinned) {
      const close = element("button", "×", "hover-close"); close.type = "button"; close.setAttribute("aria-label", "Close");
      close.onclick = event => { event?.stopPropagation?.(); hide(); };
      panel.append(close);
    }
    panel.append(element("h4", "Now"));
    const now = element("p", activity.text + (activity.when ? " · " + activity.when : ""), "hover-now" + (activity.live ? "" : " idle"));
    panel.append(now);
    const facts = [thread.project_cwd && "Project: " + thread.project_cwd,
      thread.project_name && "Group: " + thread.project_name,
      thread.cwd && "Workspace: " + thread.cwd,
      thread.updated_at && "Updated " + relativeTime(thread.updated_at)].filter(Boolean);
    if (facts.length) panel.append(element("p", facts.join("\n"), "hover-facts"));
    if (thread.checkpoint?.summary) {
      panel.append(element("h4", "Where things stand"), markdownBody(thread.checkpoint.summary, "hover-summary"));
      if (thread.checkpoint_stale) panel.append(element("small", "New activity since this checkpoint", "hover-muted"));
    }
    if (!thread.checkpoint_stale && thread.checkpoint?.next_action) {
      const actor = {user: "Your next step", agent: "Agent’s next step", external: "Waiting on"}[thread.checkpoint.next_actor] || "Next step";
      panel.append(element("h4", actor), markdownBody(thread.checkpoint.next_action, "hover-summary"));
    }
    const earlier = data?.previous_checkpoint;
    if (earlier?.summary) {
      const label = (grouping ? grouping.phaseLabel({checkpoint: {phase: earlier.phase}}) : earlier.phase) + (earlier.created_at ? " · " + relativeTime(earlier.created_at) : "");
      panel.append(element("h4", "Earlier"), element("p", label + " — " + earlier.summary, "hover-earlier"));
    }
    const children = data?.children || [];
    if (children.length) {
      panel.append(element("h4", "Agents · " + children.length));
      const list = element("ul", "", "hover-agents");
      for (const child of children) { const item = element("li", ""); item.append(agentRow(child)); list.append(item); }
      panel.append(list);
    }
    const section = element("section", "", "hover-message");
    section.append(element("h4", "Most recent message"));
    const message = data?.latest_message;
    if (message) {
      let speaker = message.role === "user" ? "You" : PROVIDERS[thread.client] || "Agent";
      if (message.timestamp) {
        const date = new Date(message.timestamp);
        if (!Number.isNaN(date.getTime())) speaker += " · " + date.toLocaleString();
      }
      section.append(element("small", speaker, "hover-muted"),
        markdownBody(message.text + (message.truncated ? "…" : ""), "hover-message-text"));
      if (message.truncated) section.append(element("small", "Open the thread to read more.", "hover-muted"));
    } else section.append(element("p", loading ? "Loading latest message…" : failed ? "Latest message unavailable. Open the thread to try again." : "No conversation message available yet.", "hover-muted"));
    panel.append(section);
    if (select) {
      const foot = element("div", "", "hover-foot");
      const open = element("button", "Open thread", "quiet"); open.type = "button";
      open.onclick = event => { event?.stopPropagation?.(); const id = thread.thread_id; hide(); select(id); };
      foot.append(open); panel.append(foot);
    }
    position();
  }
  async function show(button, id, {thread: given} = {}) {
    if (!button?.isConnected) return;
    anchor = button; panel.hidden = false;
    anchor.setAttribute("aria-describedby", panel.id);
    const thread = given || getThread(id);
    if (!thread) { hide(); return; }
    const token = ++request, saved = cache.get(thread.thread_id);
    current = {thread_id: thread.thread_id, thread};
    render(thread, saved?.data || null, {loading: !saved});
    try {
      const data = saved && saved.expires > Date.now() ? saved.data : await loadPreview(thread.thread_id);
      cache.set(thread.thread_id, {data, expires: Date.now() + cacheMs});
      if (cache.size > 30) cache.delete(cache.keys().next().value);
      if (token !== request || anchor !== button || !button.isConnected) return;
      current = {thread_id: thread.thread_id, thread: data.thread || thread};
      render(current.thread, data);
    } catch (_) {
      if (token === request && anchor === button) render(thread, saved?.data || null, {failed: true});
    }
  }
  function pin() {
    if (pinned || panel.hidden || !current) return;
    pinned = true; render(current.thread, cache.get(current.thread_id)?.data || null);
  }
  function refresh() {
    if (panel.hidden || !current) return;
    const fresh = getThread(current.thread_id) || current.thread;
    current.thread = fresh;
    const saved = cache.get(current.thread_id);
    if (pinned && anchor?.isConnected && (!saved || saved.expires <= Date.now())) { show(anchor, current.thread_id, {thread: fresh}); return; }
    render(fresh, saved?.data || null);
  }
  function target(event) {
    const button = event.target.closest?.(".session[data-thread], .agent-row[data-thread]");
    return button && overview.contains(button) ? button : null;
  }
  function enter(event) {
    if (event.pointerType === "touch") return;
    const button = target(event);
    if (!button) return;
    cancel(closeTimer);
    if (button === anchor) return;
    if (pinned) return; // A pinned lens stays until it is closed or another one is opened.
    hide(); anchor = button;
    // Avoid native tooltips covering the richer preview.
    button.removeAttribute("title");
    for (const el of button.querySelectorAll("[title]")) el.removeAttribute("title");
    openTimer = schedule(() => show(button, button.dataset.thread), event.type === "focusin" ? 0 : 300);
  }
  function leave(event) {
    if (pinned) return;
    if (anchor?.contains(event.relatedTarget) || panel.contains(event.relatedTarget)) return;
    cancel(openTimer);
    closeTimer = schedule(() => {
      if (!pointerInside && document.activeElement !== anchor) hide();
    }, 180);
  }
  overview.addEventListener("pointerover", enter);
  overview.addEventListener("pointerout", leave);
  overview.addEventListener("focusin", enter);
  overview.addEventListener("focusout", leave);
  overview.addEventListener("click", () => hide());
  panel.addEventListener("pointerenter", () => { pointerInside = true; cancel(closeTimer); });
  panel.addEventListener("pointerleave", () => { pointerInside = false; leave({relatedTarget: null}); });
  panel.addEventListener("pointerdown", () => pin());
  document.addEventListener("keydown", event => { if (event.key === "Escape") hide(); });
  window.addEventListener("resize", () => { if (pinned && !panel.hidden) position(); else hide(); });
  document.addEventListener("scroll", event => { if (panel.contains(event.target)) return; if (pinned && !panel.hidden) position(); else hide(); }, true);
  const observer = new window.MutationObserver(() => {
    if (!anchor || anchor.isConnected) return;
    if (pinned && current) {
      const again = overview.querySelector?.('[data-thread="' + current.thread_id + '"]');
      if (again) { anchor = again; anchor.setAttribute("aria-describedby", panel.id); position(); return; }
    }
    hide();
  });
  observer.observe(overview, {childList: true, subtree: true});
  return {hide, panel, pin, refresh, get pinned() { return pinned; }};
}

if (typeof module !== "undefined" && module.exports) module.exports = {createThreadHover};
else globalThis.threadHover = createThreadHover({overview: document.getElementById("overview"), document, window,
  getThread: id => state.sessions.find(thread => thread.thread_id === id),
  loadPreview: id => api(threadPath(id) + "/preview"),
  relativeTime, select: id => action(() => select(id))});
