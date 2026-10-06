"use strict";

// Kept separate from card rendering so list refreshes need no per-card listeners.
function createThreadHover({overview, document, window, getThread, loadPreview, statusLabel, relativeTime,
  schedule = setTimeout, cancel = clearTimeout}) {
  const panel = document.createElement("aside");
  panel.id = "thread-hover"; panel.className = "thread-hover"; panel.hidden = true;
  panel.setAttribute("role", "tooltip");
  document.body.append(panel);
  let anchor = null, openTimer, closeTimer, request = 0, pointerInside = false;
  const cache = new Map();
  function element(tag, text, className) {
    const el = document.createElement(tag); el.textContent = text; el.className = className || ""; return el;
  }
  function hide() {
    cancel(openTimer); cancel(closeTimer); request++;
    anchor?.removeAttribute("aria-describedby");
    anchor = null; pointerInside = false; panel.hidden = true;
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
  function render(thread, message, loading = false, failed = false) {
    panel.replaceChildren();
    panel.append(element("div", [statusLabel(thread), thread.checkpoint?.phase || "Getting started"].filter(Boolean).join(" · "), "hover-status"),
      element("h3", thread.title, "hover-title"));
    const facts = [thread.project_name && "Project: " + thread.project_name,
      thread.repository_name && "Repository: " + thread.repository_name,
      thread.cwd && "Workspace: " + thread.cwd,
      thread.updated_at && "Updated " + relativeTime(thread.updated_at)].filter(Boolean);
    panel.append(element("p", facts.join("\n"), "hover-facts"));
    if (thread.checkpoint?.summary) {
      panel.append(element("h4", "Where things stand"), element("p", thread.checkpoint.summary, "hover-summary"));
      if (thread.checkpoint_stale) panel.append(element("small", "New activity since this checkpoint", "hover-muted"));
    }
    if (thread.checkpoint?.next_action) {
      const actor = {user: "Your next step", agent: "Agent’s next step", external: "Waiting on"}[thread.checkpoint.next_actor] || "Next step";
      panel.append(element("h4", actor), element("p", thread.checkpoint.next_action, "hover-summary"));
    }
    const section = element("section", "", "hover-message");
    section.append(element("h4", "Most recent message"));
    if (message) {
      let speaker = message.role === "user" ? "You" : thread.client === "claude" ? "Claude" : "Codex";
      if (message.timestamp) {
        const date = new Date(message.timestamp);
        if (!Number.isNaN(date.getTime())) speaker += " · " + date.toLocaleString();
      }
      section.append(element("small", speaker, "hover-muted"),
        element("p", message.text + (message.truncated ? "…" : ""), "hover-message-text"));
      if (message.truncated) section.append(element("small", "Open the thread to read more.", "hover-muted"));
    } else section.append(element("p", loading ? "Loading latest message…" : failed ? "Latest message unavailable. Open the thread to try again." : "No conversation message available yet.", "hover-muted"));
    panel.append(section);
    position();
  }
  async function show(button) {
    if (!button.isConnected) return;
    anchor = button; panel.hidden = false;
    anchor.setAttribute("aria-describedby", panel.id);
    const thread = getThread(button.dataset.thread);
    if (!thread) { hide(); return; }
    const current = ++request, saved = cache.get(thread.thread_id);
    render(thread, null, true);
    try {
      const data = saved && saved.expires > Date.now() ? saved.data : await loadPreview(thread.thread_id);
      cache.set(thread.thread_id, {data, expires: Date.now() + 3000});
      if (cache.size > 30) cache.delete(cache.keys().next().value);
      if (current !== request || anchor !== button || !button.isConnected) return;
      render(data.thread, data.latest_message);
    } catch (_) {
      if (current === request && anchor === button) render(thread, null, false, true);
    }
  }
  function target(event) {
    const button = event.target.closest?.(".session[data-thread]");
    return button && overview.contains(button) ? button : null;
  }
  function enter(event) {
    if (event.pointerType === "touch") return;
    const button = target(event);
    if (!button) return;
    cancel(closeTimer);
    if (button === anchor) return;
    hide(); anchor = button;
    // Avoid native tooltips covering the richer preview.
    button.removeAttribute("title");
    for (const el of button.querySelectorAll("[title]")) el.removeAttribute("title");
    openTimer = schedule(() => show(button), event.type === "focusin" ? 0 : 300);
  }
  function leave(event) {
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
  overview.addEventListener("click", hide);
  panel.addEventListener("pointerenter", () => { pointerInside = true; cancel(closeTimer); });
  panel.addEventListener("pointerleave", () => { pointerInside = false; leave({relatedTarget: null}); });
  document.addEventListener("keydown", event => { if (event.key === "Escape") hide(); });
  window.addEventListener("resize", hide);
  document.addEventListener("scroll", event => { if (!panel.contains(event.target)) hide(); }, true);
  const observer = new window.MutationObserver(() => { if (anchor && !anchor.isConnected) hide(); });
  observer.observe(overview, {childList: true, subtree: true});
  return {hide, panel};
}

if (typeof module !== "undefined" && module.exports) module.exports = {createThreadHover};
else createThreadHover({overview: document.getElementById("overview"), document, window,
  getThread: id => state.sessions.find(thread => thread.thread_id === id),
  loadPreview: id => api(threadPath(id) + "/preview"),
  statusLabel: thread => {
    const status = threadGrouping.status(thread);
    return status.key === "running" ? "" : status.label;
  }, relativeTime});
