"use strict";
const $ = id => document.getElementById(id);
const state = {config: null, creatingSession: false, sessions: [], organization: {repositories: [], projects: []}, selected: null, detail: null, drafts: new Map(), closing: new Set(), pinning: new Set(), updatingThreads: new Set(), groupExpansion: new Map(), phaseScroll: new Map(), commandFeedback: new Map(), busy: false, listSignature: ""};
let notifications;
let savedViews;
let navigation;
state.sourceWindowId = window.agentCoordDesktop?.windowId || crypto.randomUUID();
state.paneMode = window.parent !== window && new URLSearchParams(location.search).get("pane") === "1";
if (state.paneMode) state.sourceWindowId = window.parent.agentCoordSourceWindowId || state.sourceWindowId;
window.agentCoordSourceWindowId = state.sourceWindowId;
let listRequest = 0;
const base = "/api/browser/";
const sessionPath = id => "sessions/" + encodeURIComponent(id);
const threadPath = id => "threads/" + encodeURIComponent(id);
function node(tag, text, className) { const el = document.createElement(tag); if (text != null) el.textContent = text; if (className) el.className = className; return el; }
function showError(error) {
  const message = error.message || String(error), dialogError = document.querySelector("dialog[open] .dialog-error");
  if (dialogError) { dialogError.hidden = false; dialogError.textContent = message; }
  else { $("error").hidden = false; $("error").querySelector("span").textContent = message; }
}
$("error").querySelector("button").onclick = () => { $("error").hidden = true; };
async function api(path, body) {
  const viewMutation = body !== undefined && (path === "views" || path.startsWith("views/"));
  if (viewMutation) ++listRequest;
  const options = {cache: "no-store"};
  if (body !== undefined) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-Agent-Coord-Token": state.config.token}, body: JSON.stringify(body)});
  const response = await fetch(base + path, options);
  if (response.status === 401 && location.protocol === "https:") location.reload();
  const result = await response.json();
  if (viewMutation) ++listRequest;
  if (!response.ok) throw new Error(result.error || "Request failed");
  return result;
}
async function action(fn, button) { if (button) button.disabled = true; try { await fn(); } catch (error) { showError(error); } finally { if (button) button.disabled = false; } }
async function refreshList() {
  if (state.paneMode) { window.parent.agentCoordPanes?.requestList(); return; }
  const view = $("view").value, request = ++listRequest;
  // Open threads feed every tab's badges, including while browsing a Closed view.
  const [open, closed, organization, views] = await Promise.all([api("threads?archived=false"),
    view === "archived" ? api("threads?archived=true") : null, api("organization"), api("views")]);
  if (request !== listRequest || view !== $("view").value) return;
  state.sessions = (closed || open).data;
  state.viewThreads = open.data;
  state.rollUp?.sync(open.data);
  state.organization = organization;
  for (const field of ["repository", "project"]) {
    const select = $(field), selected = select.value, choices = organization[field === "repository" ? "repositories" : "projects"];
    const signature = JSON.stringify(choices);
    if (select.dataset.signature === signature) continue;
    select.dataset.signature = signature;
    select.replaceChildren(new Option(field === "repository" ? "All repositories" : "All projects", ""),
      new Option(field === "repository" ? "No repository" : "No project", threadOrganization.NONE));
    for (const item of choices) {
      const duplicates = choices.filter(other => other.name === item.name).length > 1;
      const option = new Option(item.name + (duplicates && item.root ? " · " + item.root : ""), item.id);
      option.title = item.root || item.name; select.append(option);
    }
    if (selected && ![...select.options].some(option => option.value === selected)) {
      select.append(new Option("Unavailable " + field, selected));
    }
    select.value = selected;
  }
  savedViews?.sync(views.data);
  if (view !== $("view").value) return refreshList();
  renderList();
}
function relativeTime(timestamp) {
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - Number(timestamp)) / 60));
  if (!Number.isFinite(minutes)) return "";
  if (minutes < 1) return "Just now";
  if (minutes < 60) return minutes + "m ago";
  if (minutes < 1440) return Math.floor(minutes / 60) + "h ago";
  return Math.floor(minutes / 1440) + "d ago";
}
async function toggleThreadPinned(thread) {
  const id = thread.thread_id;
  if (state.pinning.has(id)) return;
  state.pinning.add(id); renderList();
  try {
    const updated = await api(threadPath(id), {pinned: !thread.pinned});
    state.sessions = state.sessions.map(item => item.thread_id === id ? {...item, pinned: updated.pinned} : item);
    if (state.detail?.work_thread.thread_id === id) state.detail.work_thread.pinned = updated.pinned;
    await refreshList();
  } finally {
    state.pinning.delete(id); renderList();
  }
}
async function updateThreadAttention(thread, body) {
  const id = thread.thread_id;
  if (state.updatingThreads.has(id)) return;
  state.updatingThreads.add(id); renderList();
  try {
    const updated = await api(threadPath(id), body);
    state.sessions = state.sessions.map(item => item.thread_id === id ? updated : item);
    if (state.detail?.work_thread.thread_id === id) { state.detail.work_thread = updated; renderThread(); }
    await refreshList();
  } finally {
    state.updatingThreads.delete(id);
    if (state.detail?.work_thread.thread_id === id) renderThread();
    renderList();
  }
}
async function markThreadHandled(thread) {
  if (!thread.can_handle_response || state.updatingThreads.has(thread.thread_id)) return;
  const ticket = state.rollUp?.ticket(thread.thread_id);
  await updateThreadAttention(thread, {handled: true,
    handled_checkpoint_id: thread.checkpoint?.id || 0, handled_completion_id: thread.turn_completion?.id || 0});
  await state.rollUp?.responded(ticket);
}
async function toggleThreadLater(thread) {
  await updateThreadAttention(thread, {attention: thread.attention === "later" ? "now" : "later"});
}
function snoozeTime(choice, custom, now = new Date()) {
  let date;
  if (choice === "tomorrow") {
    date = new Date(now); date.setDate(date.getDate() + 1); date.setHours(9, 0, 0, 0);
  } else if (choice === "custom") date = new Date(custom);
  else if (["1", "3"].includes(choice)) date = new Date(now.getTime() + Number(choice) * 3600000);
  if (!date || !Number.isFinite(date.getTime()) || date <= now) throw new Error("Choose a time in the future.");
  return date.getTime() / 1000;
}
function snoozeLabel(thread) {
  return thread.snooze_due ? "Snooze ended" : thread.snoozed ? "Until " + new Date(thread.snoozed_until * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  }) : "";
}
function openThreadSnooze(thread) {
  state.snoozingThread = thread;
  $("snooze-form").reset(); $("snooze-custom-label").hidden = true; $("snooze-custom").required = false;
  $("snooze-error").hidden = true;
  $("snooze-thread").textContent = thread.title;
  $("snooze-dialog").showModal(); $("snooze-duration").focus();
}
async function snoozeThread(thread, deadline) {
  const ticket = state.rollUp?.ticket(thread.thread_id);
  await updateThreadAttention(thread, {snoozed_until: deadline});
  await state.rollUp?.responded(ticket);
}
async function resumeThreadSnooze(thread) {
  const ticket = state.rollUp?.ticket(thread.thread_id);
  await updateThreadAttention(thread, {resume_snooze: thread.snoozed_until});
  await state.rollUp?.responded(ticket);
}
function threadCard(thread, compact = false, inAttention = false) {
  const reason = threadGrouping.reason(thread);
  const needsInput = threadGrouping.awaitsUser(thread) && ["blocked", "review"].includes(reason.key);
  const {key: status, label: statusLabel} = threadGrouping.status(thread);
  const button = node("button", null, "session" + (compact ? " compact" : "") + (status === "running" ? " active-thread" : "") + (needsInput ? " needs-attention" : "") + (state.selected === thread.thread_id ? " selected" : ""));
  if (!compact && !inAttention) button.className += " card-age-" + threadGrouping.cardAge(thread);
  button.dataset.thread = thread.thread_id;
  button.title = thread.title;
  button.setAttribute("aria-current", state.selected === thread.thread_id ? "true" : "false");
  const project = node("span", associationLabel(thread), "project-name");
  project.title = thread.repository_root || associationLabel(thread);
  const title = node("div", null, "session-title");
  if (compact) {
    if (status === "running") title.append(node("span", statusLabel, "sr-only"));
    else {
      const dot = node("span", "", "dot " + status); dot.setAttribute("aria-label", statusLabel);
      title.append(dot);
    }
    title.append(node("span", thread.title));
    if (thread.unread_result) title.append(node("span", "NEW", "unread-mark"));
    button.append(project, title);
    if (snoozeLabel(thread)) button.append(node("small", snoozeLabel(thread), "placement-label"));
  } else {
    const top = node("div", null, "card-top");
    const badge = node("span", null, status === "running" || inAttention || thread.response_state !== "interrupted" ? "sr-only" : "status-tag " + status);
    badge.append(node("span", statusLabel));
    top.append(project, badge);
    title.append(node("span", thread.title));
    const summary = thread.checkpoint?.summary || thread.original_request || "Ready for your first message.";
    const snippet = node("p", summary, "thread-snippet"); snippet.title = summary;
    const meta = node("div", null, "card-meta");
    meta.append(node("span", threadGrouping.phaseLabel(thread), "phase-label"));
    if (thread.attention === "later") meta.append(node("span", "Later", "placement-label"));
    if (snoozeLabel(thread)) meta.append(node("span", snoozeLabel(thread), "placement-label"));
    if (thread.unread && !inAttention) meta.append(node("span", thread.unread_result ? "NEW RESPONSE" : "NEW", "unread-mark"));
    if (thread.links.length) meta.append(node("span", thread.links.length + (thread.links.length === 1 ? " link" : " links")));
    const activityAt = threadGrouping.activityAt(thread);
    const time = node("span", relativeTime(activityAt), "card-time");
    if (activityAt) time.title = "Last activity: " + new Date(activityAt * 1000).toLocaleString();
    meta.append(time);
    button.append(top, title, snippet, meta);
    if (thread.checkpoint?.next_action) {
      const next = node("div", null, "next-action"), copy = node("span", null, "next-copy");
      const actor = {user: "Your next step", agent: "Up next", external: "Waiting on"}[thread.checkpoint.next_actor] || "Next";
      copy.append(node("span", actor + " · ", "next-label"), document.createTextNode(thread.checkpoint.next_action));
      next.append(node("span", "↳"), copy); next.title = thread.checkpoint.next_action; button.append(next);
    }
    if (thread.checkpoint_stale) button.append(node("small", "New activity since this checkpoint", "stale-checkpoint"));
    if (inAttention) {
      const copy = node("div", null, "attention-copy");
      meta.insertBefore(project, meta.lastChild);
      copy.append(title, snippet, meta);
      const label = node("div", null, "attention-reason " + reason.key);
      label.append(node("span", reason.label));
      if (reason.key === "done") label.append(node("small", "No action needed"));
      button.replaceChildren(label, copy);
    }
  }
  button.onclick = () => action(() => select(thread.thread_id));
  if (!compact && thread.attention !== "archived") {
    const card = node("div", null, "thread-card" + (inAttention ? " attention-card" : ""));
    const pin = node("button", null, "card-pin");
    pin.type = "button";
    pin.dataset.thread = thread.thread_id;
    pin.dataset.action = "pin";
    pin.title = (thread.pinned ? "Unpin " : "Pin ") + thread.title;
    pin.setAttribute("aria-label", pin.title);
    pin.setAttribute("aria-pressed", String(Boolean(thread.pinned)));
    pin.disabled = state.closing.has(thread.thread_id);
    pin.setAttribute("aria-disabled", String(state.pinning.has(thread.thread_id) || pin.disabled));
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
    shape.setAttribute("d", "M9 3h6l-1 6 4 4v2H6v-2l4-4-1-6ZM12 15v6");
    icon.append(shape); pin.append(icon);
    pin.onclick = event => {
      event.stopPropagation();
      action(() => toggleThreadPinned(thread));
    };
    const close = node("button", "×", "card-close");
    const label = (["running", "needs input"].includes(thread.status) ? "Stop and close " : "Close ") + thread.title;
    close.type = "button";
    close.title = label;
    close.setAttribute("aria-label", label);
    close.disabled = state.closing.has(thread.thread_id);
    close.onclick = event => {
      event.stopPropagation();
      action(async () => {
        try { await toggleThreadClosed(thread); }
        finally { state.listSignature = ""; renderList(); }
      }, close);
    };
    card.append(button, pin, close);
    const actions = node("div", null, "card-actions");
    function addAction(label, name, callback) {
      const control = node("button", label, "quiet");
      control.type = "button"; control.dataset.thread = thread.thread_id; control.dataset.action = name;
      control.setAttribute("aria-label", label + " · " + thread.title);
      control.disabled = state.closing.has(thread.thread_id) || state.updatingThreads.has(thread.thread_id);
      control.onclick = event => { event.stopPropagation(); action(callback); };
      actions.append(control);
    }
    if (!inAttention) {
      if (thread.can_handle_response) addAction(reason.key === "review" ? "Mark reviewed" : "Mark handled", "handle", () => markThreadHandled(thread));
      addAction(thread.attention === "later" ? "Move to Now" : "Move to Later", "later", () => toggleThreadLater(thread));
    }
    if (thread.snoozed || thread.snooze_due) addAction("Resume", "resume-snooze", () => resumeThreadSnooze(thread));
    addAction(thread.snoozed ? "Change snooze…" : thread.snooze_due ? "Snooze again…" : "Snooze…", "snooze", () => openThreadSnooze(thread));
    card.append(actions);
    return card;
  }
  return button;
}
function groupHeading(label, count, tag = "div") {
  const heading = node(tag, null, "group-heading");
  heading.append(node("h2", label), node("span", String(count).padStart(2, "0"), "group-count"));
  return heading;
}
function associationLabel(thread) {
  return [thread.repository_name ? "Repo: " + thread.repository_name : "",
    thread.project_name ? "Project: " + thread.project_name : ""].filter(Boolean).join(" · ") || "No repository · No project";
}
function renderClosedToggle() {
  const placement = $("view").value;
  const archived = placement === "archived";
  $("show-now").setAttribute("aria-pressed", String(!["later", "archived"].includes(placement)));
  $("show-later").setAttribute("aria-pressed", String(placement === "later"));
  $("show-closed").setAttribute("aria-pressed", String(archived));
  $("show-closed").title = archived ? "Return to open threads" : "Show closed threads";
}
function createOverviewMotion() {
  const running = new Map(), reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  let previousView;
  const stop = () => { for (const motion of running.values()) motion.stop(); running.clear(); };
  // Floating copies must never lag behind scrolling, resizing, or navigation.
  window.addEventListener("scroll", stop, {capture: true, passive: true});
  window.addEventListener("resize", stop, {passive: true});
  reduced.addEventListener("change", stop);
  function cards() {
    return [...$("overview").querySelectorAll("[data-card-thread]")].map(card => ({
      card, id: card.dataset.cardThread, group: card.closest("[data-group]").dataset.group,
      rect: card.getBoundingClientRect(),
    }));
  }
  function visible(item) {
    const r = item.rect, board = item.card.closest(".phase-board"), clip = board?.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.top >= 0 && r.left >= 0 &&
      r.bottom <= window.innerHeight && r.right <= window.innerWidth &&
      (!clip || r.left >= clip.left && r.right <= clip.right);
  }
  function floatingCopy(card, rect) {
    const copy = card.cloneNode(true), originals = [card, ...card.querySelectorAll("*")];
    // Preserve the destination's appearance outside its grid/Attention ancestors.
    [copy, ...copy.querySelectorAll("*")].forEach((el, index) => {
      const computed = getComputedStyle(originals[index]);
      for (const property of computed) el.style.setProperty(property, computed.getPropertyValue(property));
      el.removeAttribute("id");
      el.removeAttribute("data-thread");
      el.removeAttribute("data-card-thread");
      el.style.pointerEvents = "none";
    });
    copy.setAttribute("aria-hidden", "true");
    copy.inert = true;
    Object.assign(copy.style, {position: "fixed", left: rect.left + "px", top: rect.top + "px",
      width: rect.width + "px", height: rect.height + "px", margin: "0", zIndex: "20",
      transformOrigin: "top left", transition: "none"});
    document.body.append(copy);
    return copy;
  }
  return {
    capture(view) {
      const enabled = previousView === view && !$("welcome").hidden && !reduced.matches &&
        typeof Element.prototype.animate === "function";
      previousView = view;
      const before = new Map();
      if (enabled) for (const item of cards()) {
        const flight = running.get(item.id);
        if (flight?.copy) item.rect = flight.copy.getBoundingClientRect();
        if (visible(item)) before.set(item.id, {...item, continuing: Boolean(flight)});
      }
      stop();
      return before;
    },
    play(before) {
      const after = cards();
      if (!after.some(item => before.has(item.id) &&
        (before.get(item.id).group !== item.group || before.get(item.id).continuing))) return;
      for (const item of after) {
        const old = before.get(item.id);
        if (!old || !visible(item)) continue;
        const dx = old.rect.left - item.rect.left, dy = old.rect.top - item.rect.top;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
        const timing = {duration: 380, easing: "cubic-bezier(.22, 1, .36, 1)"};
        const animations = [];
        let copy;
        if (old.group !== item.group || old.continuing) {
          copy = floatingCopy(item.card, item.rect);
          const from = `translate(${dx}px, ${dy}px) scale(${old.rect.width / item.rect.width}, ${old.rect.height / item.rect.height})`;
          animations.push(copy.animate([{transform: from}, {transform: "none"}], timing));
          const fade = {duration: timing.duration, easing: "linear"};
          animations.push(copy.animate([{opacity: 1}, {opacity: 1, offset: .8}, {opacity: 0}], fade));
          animations.push(item.card.animate([{opacity: 0}, {opacity: 0, offset: .8}, {opacity: 1}], fade));
        } else {
          animations.push(item.card.animate([{transform: `translate(${dx}px, ${dy}px)`}, {transform: "none"}], timing));
        }
        const motion = {copy, stop() { animations.forEach(animation => animation.cancel()); copy?.remove(); }};
        running.set(item.id, motion);
        animations[0].onfinish = () => {
          if (running.get(item.id) !== motion) return;
          running.delete(item.id); motion.stop();
        };
      }
    },
  };
}
function renderList() {
  if (state.paneMode) { renderStatus(); return; }
  renderClosedToggle();
  savedViews?.render(state.viewThreads || state.sessions);
  const query = $("search").value.trim().toLocaleLowerCase();
  const projectThreads = state.sessions.filter(t => threadOrganization.matches(t, $("repository").value, $("project").value));
  const threads = projectThreads.filter(t => threadViews.matches(t,
    {show: $("view").value, phase: $("phase-filter").value, search: query}));
  const archived = $("view").value === "archived";
  const groupBy = $("group-by").value;
  const viewId = savedViews?.activeId || "all";
  const signature = JSON.stringify([threads, state.selected, [...state.pinning], [...state.updatingThreads], archived, query, $("phase-filter").value, $("view").value, $("repository").value, $("project").value, groupBy, viewId, Math.floor(Date.now() / 60000)]);
  $("home").setAttribute("aria-current", state.selected ? "false" : "page");
  $("thread-count").textContent = String(threads.length);
  const repositories = new Set(threads.map(t => t.repository_id).filter(Boolean)).size;
  const projects = new Set(threads.map(t => t.project_id).filter(Boolean)).size;
  $("results-count").textContent = threads.length + (threads.length === 1 ? " thread" : " threads") +
    " · " + repositories + (repositories === 1 ? " repository" : " repositories") + " · " + projects + (projects === 1 ? " project" : " projects");
  if (signature === state.listSignature) { renderStatus(); return; }
  state.listSignature = signature;
  state.overviewMotion ||= createOverviewMotion();
  const motionBefore = state.overviewMotion.capture(JSON.stringify([viewId, groupBy, query,
    $("view").value, $("phase-filter").value, $("repository").value, $("project").value, state.selected]));
  const focused = document.activeElement?.dataset.thread;
  const focusAction = document.activeElement?.dataset.action;
  const focusGroup = document.activeElement?.closest("[data-group]")?.dataset.group;
  const focusArea = document.activeElement?.closest("#sessions") ? $("sessions") : $("overview");
  $("sessions").replaceChildren(); $("overview").replaceChildren();
  const groups = threadGrouping.groupThreads(threads);
  const filtered = Boolean(query || $("phase-filter").value || ["attention", "completed"].includes($("view").value) || $("repository").value || $("project").value);
  function addGroup(key, label, items, parent, description = "", attentionCount = 0) {
    const section = node("section", null, "thread-group " + key), overview = node("section", null, "thread-group " + key);
    section.setAttribute("aria-label", label); overview.setAttribute("aria-label", label);
    overview.dataset.group = key;
    section.append(groupHeading(label, items.length));
    overview.append(groupHeading(label, items.length + attentionCount));
    if (description) overview.append(node("p", description, "group-description"));
    if (attentionCount) overview.append(node("p", attentionCount + " in attention", "stage-attention"));
    const cards = node("div", null, "thread-cards");
    for (const thread of items) {
      section.append(threadCard(thread, true));
      const card = threadCard(thread, false, key === "priority");
      card.dataset.cardThread = thread.thread_id;
      cards.append(card);
    }
    if (!items.length && !attentionCount) cards.append(node("p", "—", "stage-empty"));
    overview.append(cards);
    if (items.length) $("sessions").append(section);
    parent.append(overview);
  }
  if (archived) {
    if (groups.closed.length) addGroup("closed", "Closed", groups.closed, $("overview"), "Finished conversations, with their context kept for reference.");
  } else if ($("view").value === "later") {
    if (groups.later.length) addGroup("later", "Later", groups.later, $("overview"), "Set aside for another time. Pick up from the last checkpoint when you’re ready.");
  } else {
    if (groups.priority.length) {
      const attention = node("section", null, "attention-panel");
      attention.setAttribute("aria-label", "Attention queue");
      addGroup("priority", "Attention", groups.priority, attention);
      $("overview").append(attention);
    }
    const board = node("div", null, groupBy === "phase" ? "phase-board" : "organization-board");
    board.setAttribute("aria-label", "Work stages");
    const organized = groupBy === "phase" ? groups.phases : threadOrganization.groupThreads(groups.phases.flatMap(group => group.threads), groupBy);
    for (const group of organized) addGroup(group.key, group.label, group.threads, board, group.detail || "", group.attention || 0);
    $("overview").append(board);
    if (groupBy === "phase") {
      // Keep each view's place through polling and updates while a thread is open.
      board.onscroll = () => {
        if (board.isConnected && !$("welcome").hidden && board.clientWidth) state.phaseScroll.set(viewId, board.scrollLeft);
      };
      board.scrollLeft = state.phaseScroll.get(viewId) || 0;
    }
  }
  if (!threads.length) {
    $("sessions").append(node("p", filtered ? "No matching threads" : "Nothing here yet", "empty"));
    const empty = node("div", null, "empty-state");
    const emptyProject = !query && !$("phase-filter").value && !$("repository").value && $("view").value === "active" && !projectThreads.length
      && state.organization.projects.find(project => project.id === $("project").value);
    const title = emptyProject ? "No open threads in " + emptyProject.name : filtered ? "No matching threads" : archived ? "No closed threads yet" : $("view").value === "later" ? "Nothing set aside" : "Start with an idea";
    const copy = emptyProject ? "Start a new session here, or browse existing threads and choose Add to project." : filtered ? "Try another search or clear your filters." : archived ? "Closed threads stay here. Reopen one whenever you want to continue." : $("view").value === "later" ? "Move a thread to Later to keep its context out of your current workspace." : "Open a session and give your next project a place to begin.";
    empty.append(node("h3", title), node("p", copy));
    if (filtered || !archived) {
      const button = node("button", emptyProject ? "Browse existing threads" : filtered ? "Clear filters" : "New session ↗", "quiet");
      button.onclick = () => action(filtered ? async () => { $("search").value = ""; $("phase-filter").value = ""; $("repository").value = ""; $("project").value = ""; if (!archived) $("view").value = "active"; await savedViews?.persist(); await refreshList(); } : newSession);
      empty.append(button);
    }
    if (emptyProject) {
      const button = node("button", "New session", "primary");
      button.onclick = () => action(newSession);
      empty.append(button);
    }
    $("overview").append(empty);
  }
  state.overviewMotion.play(motionBefore);
  if (focused) {
    const candidates = [...focusArea.querySelectorAll("[data-thread]")].filter(el => el.dataset.thread === focused && el.dataset.action === focusAction);
    const target = candidates.find(el => el.closest("[data-group]")?.dataset.group === focusGroup) || candidates[0];
    target?.focus({preventScroll: true});
  }
  renderStatus();
}
async function select(id, {rollUp = false} = {}) {
  if (id === "new-session") return newSession();
  $("conversation").classList.remove("session-draft");
  if (!rollUp) state.rollUp?.stop();
  if (globalThis.agentCoordPanes?.active) { await globalThis.agentCoordPanes.openThread(id); return; }
  if (state.paneMode && state.selected && state.selected !== id) { await window.parent.agentCoordPanes.openThread(id); return; }
  savedViews?.remember();
  setNavigation(false);
  if (state.selected) state.drafts.set(state.selected, $("message").value);
  state.selected = id;
  notifications?.syncFocus();
  state.detail = null;
  state.titleEdit = null;
  state.attachments?.highlight(false);
  renderStatus();
  $("message").value = state.drafts.get(id) || "";
  if (navigation) navigation.remember();
  else history.replaceState(null, "", "#" + encodeURIComponent(id));
  $("welcome").hidden = true;
  $("conversation").hidden = false;
  $("timeline").replaceChildren(node("p", "Opening session…", "empty"));
  state.timelineScroll?.reset();
  $("requests").replaceChildren();
  $("thread-context").hidden = true;
  renderTitle();
  $("page-location").textContent = "Thread";
  delete $("requests").dataset.signature;
  renderList();
  await refreshDetail();
  if (state.selected !== id) return;
  if (state.paneMode) return;
  const displayed = state.detail.work_thread;
  await api(threadPath(id), {seen: true, seen_checkpoint_id: displayed.checkpoint?.id || 0,
    seen_completion_id: displayed.turn_completion?.id || 0});
  await refreshList();
  if (state.detail?.work_thread.browser_session) { $("message").focus(); loadSessionModels(); }
  else $("session-name").focus();
}
async function refreshDetail() {
  const id = state.selected;
  if (!id || isSessionDraft()) return;
  const work = await api(threadPath(id));
  const detail = work.browser_session ? await api(sessionPath(id)) : {session: {cwd: work.cwd}, thread: {turns: []}, requests: [], running: false};
  if (id !== state.selected) return;
  detail.work_thread = work;
  state.detail = detail;
  renderThread();
  renderTimeline(); renderRequests(); renderQueuedMessages(); renderStatus();
}
async function refreshThread() {
  const id = state.selected;
  if (!id || !state.detail || isSessionDraft()) return;
  const thread = await api(threadPath(id));
  if (id !== state.selected || !state.detail) return;
  state.detail.work_thread = thread; renderThread(); renderStatus();
}
function renderThread() {
  const detail = state.detail, work = detail.work_thread, checkpoint = work.checkpoint;
  $("thread-context").hidden = false;
  renderTitle();
  $("page-location").textContent = work.project_name || work.repository_name || "Thread";
  $("park").textContent = work.attention === "later" ? "Move to Now" : "Move to Later";
  $("snooze-thread-button").hidden = work.attention === "archived";
  $("snooze-thread-button").textContent = work.snoozed ? "Change snooze…" : work.snooze_due ? "Snooze again…" : "Snooze…";
  $("resume-snooze").hidden = !(work.snoozed || work.snooze_due);
  $("snooze-status").textContent = snoozeLabel(work);
  for (const id of ["snooze-thread-button", "resume-snooze"]) $(id).disabled = state.updatingThreads.has(work.thread_id);
  $("handle-response").hidden = !work.can_handle_response;
  $("handle-response").disabled = state.updatingThreads.has(work.thread_id);
  $("organize-thread").textContent = work.project_id ? "Change project" : "Add to project";
  $("checkpoint-phase").textContent = checkpoint ? " · " + checkpoint.phase : "";
  const signature = JSON.stringify(work);
  if ($("checkpoint").dataset.signature === signature) return;
  const sameThread = $("checkpoint").dataset.thread === work.thread_id;
  const opened = new Set([...$("checkpoint").querySelectorAll("details[open]")].map(el => el.dataset.section));
  $("checkpoint").dataset.signature = signature; $("checkpoint").dataset.thread = work.thread_id;
  $("checkpoint").replaceChildren(node("p", checkpoint?.summary || "No checkpoint yet. The agent will save one when it has a result to report."));
  if (checkpoint) {
    $("checkpoint").append(node("small", (checkpoint.author === "user" ? "Edited by you" : "Saved by agent") + " · " + new Date(checkpoint.refreshed_at * 1000).toLocaleString(), "muted"));
    if (checkpoint.next_action) $("checkpoint").append(node("p", "Next · " + ({user: "You", agent: "Agent", external: "Waiting externally"}[checkpoint.next_actor] || "") + ": " + checkpoint.next_action, "checkpoint-next"));
  }
  if (work.checkpoint_stale) $("checkpoint").append(node("p", "This checkpoint predates the latest turn. Review the conversation for newer work.", "stale-checkpoint"));
  const links = node("div", null, "thread-links");
  for (const link of work.links) {
    const row = node("div", null, "thread-link");
    let label = node("span", link.label);
    try { const url = new URL(link.target); if (["http:", "https:"].includes(url.protocol)) { label = node("a", link.label + " ↗"); label.href = url.href; label.target = "_blank"; label.rel = "noopener noreferrer"; } } catch (_) { /* Local references are displayed as text. */ }
    label.title = link.target;
    row.append(node("small", link.kind.replaceAll("_", " "), "muted"), label);
    const target = node("code", link.target); row.append(target);
    const remove = node("button", "×", "quiet"); remove.setAttribute("aria-label", "Remove " + link.label);
    remove.onclick = () => action(async () => { await api(threadPath(work.thread_id) + "/links", {remove: link.id}); await refreshThread(); await refreshList(); }, remove);
    row.append(remove); links.append(row);
  }
  $("checkpoint").append(links);
  if (work.children?.length) {
    const children = node("details"); children.dataset.section = "children"; children.open = sameThread && opened.has("children"); children.append(node("summary", "Delegated sessions · " + work.children.length));
    for (const child of work.children) { const button = node("button", child.title + " · " + (child.checkpoint?.phase || "No checkpoint yet"), "quiet"); button.onclick = () => action(() => select(child.thread_id)); children.append(button); }
    $("checkpoint").append(children);
  }
  if (work.original_request) { const original = node("details"); original.dataset.section = "original"; original.open = sameThread && opened.has("original"); original.append(node("summary", "Original request"), node("p", work.original_request, "original-request")); $("checkpoint").append(original); }
  if (work.checkpoints?.length > 1) {
    const history = node("details"); history.dataset.section = "history"; history.open = sameThread && opened.has("history"); history.append(node("summary", "Checkpoint history · " + work.checkpoints.length));
    for (const entry of work.checkpoints) { const item = node("article", null, "checkpoint-entry"); item.append(node("small", entry.phase + " · " + new Date(entry.created_at * 1000).toLocaleString(), "muted"), node("p", entry.summary)); if (entry.next_action) item.append(node("p", "Next (" + entry.next_actor + "): " + entry.next_action)); history.append(item); }
    $("checkpoint").append(history);
  }
  $("resume-browser").hidden = !work.can_resume || work.attention === "archived";
}
function renderTitle() {
  const editing = !!state.titleEdit && state.titleEdit.id === state.selected;
  const saving = editing && state.titleEdit.saving;
  $("title-heading").hidden = editing;
  $("rename-form").hidden = !editing;
  $("session-name").textContent = state.detail?.work_thread.title || "Opening thread…";
  $("session-name").disabled = !state.detail;
  const source = state.detail?.work_thread.forked_from;
  $("fork-origin").hidden = !source;
  $("fork-origin").replaceChildren();
  if (source) {
    const link = node("a", source.title);
    link.href = "#" + encodeURIComponent(source.thread_id);
    link.onclick = event => { event.preventDefault(); action(() => select(source.thread_id)); };
    $("fork-origin").append("Forked from ", link);
  }
  for (const id of ["rename-name", "save-rename", "cancel-rename"]) $(id).disabled = !!saving;
}
function startTitleEdit() {
  if (!state.detail || state.titleEdit?.saving) return;
  state.titleEdit = {id: state.selected, saving: false};
  $("rename-name").value = state.detail.work_thread.title;
  $("rename-name").setCustomValidity("");
  renderTitle();
  $("rename-name").focus(); $("rename-name").select();
}
function cancelTitleEdit() {
  if (state.titleEdit?.saving) return;
  state.titleEdit = null;
  renderTitle();
  $("session-name").focus();
}
async function saveTitleEdit() {
  const edit = state.titleEdit;
  if (!edit || edit.saving || edit.id !== state.selected) return;
  const title = $("rename-name").value.trim();
  if (!title || title.length > 160) {
    $("rename-name").setCustomValidity("Enter a thread title between 1 and 160 characters.");
    $("rename-name").reportValidity();
    return;
  }
  edit.saving = true; renderTitle();
  try {
    await api(threadPath(edit.id), {title});
    if (state.titleEdit === edit && state.selected === edit.id) {
      await refreshThread();
      if (state.titleEdit === edit && state.selected === edit.id) {
        edit.saving = false;
        cancelTitleEdit();
      }
    }
    await refreshList();
  } finally {
    edit.saving = false;
    if (state.titleEdit === edit) renderTitle();
  }
}
function renderSessionSettings() {
  const session = state.detail?.session, browserSession = !!state.detail?.work_thread?.browser_session;
  $("workspace").textContent = session?.cwd || state.detail?.work_thread?.cwd || "";
  $("workspace").title = $("workspace").textContent;
  $("session-settings").hidden = !browserSession;
  $("session-model").textContent = "Model · " + ((session?.client === "claude" ? "Claude Code · " : "") + (session?.model || (session?.client === "claude" ? "Claude default" : "Codex default")));
  $("session-effort").textContent = "Reasoning · " + (session?.effort || "Model default");
  $("session-yolo").hidden = !session?.yolo;
  $("edit-permissions").disabled = !browserSession || !!state.detail?.running || state.detail?.work_thread?.attention === "archived" || state.busy || state.closing.has(state.selected);
  $("command-feedback").textContent = state.commandFeedback.get(state.selected) || "";
  $("command-feedback").hidden = !browserSession || !state.commandFeedback.has(state.selected);
  renderModelPicker();
}
function openSessionPermissions() {
  const detail = state.detail, work = detail?.work_thread;
  if (!work?.browser_session || detail.running || work.attention === "archived" || state.busy || state.closing.has(state.selected)) return;
  const dialog = $("permissions-dialog");
  dialog.dataset.threadId = state.selected;
  $("permissions-thread").textContent = work.title || detail.session?.name || "This session";
  $("edit-yolo").checked = !!detail.session?.yolo;
  $("permissions-description").textContent = detail.session?.client === "claude"
    ? "With YOLO off, Claude Code uses its configured permissions and asks before restricted tool actions. Changes apply to the next turn."
    : "With YOLO off, Codex uses workspace access and asks for additional permissions. Changes apply to the next turn.";
  $("permissions-error").hidden = true;
  dialog.showModal();
}
async function saveSessionPermissions() {
  const dialog = $("permissions-dialog"), id = dialog.dataset.threadId;
  const body = {yolo: $("edit-yolo").checked};
  const draft = id === "new-session" && state.newSessionDraft;
  const session = draft && !draft.createdId
    ? Object.assign(draft.session, body) : await api(sessionPath(draft?.createdId || id), body);
  if (draft) Object.assign(draft.session, body);
  if (id === state.selected && state.detail) { state.detail.session = session; renderStatus(); }
  dialog.close();
  await refreshList();
}
function renderStatus() {
  state.rollUp?.changed();
  renderSessionSettings();
  const running = !!state.detail?.running;
  const work = state.detail?.work_thread;
  const composerLabel = work?.client === "claude" ? "Message Claude" : "Message Codex";
  $("message-label").textContent = composerLabel;
  $("message").placeholder = composerLabel + " or drop images…";
  const archived = work?.attention === "archived";
  const session = state.sessions.find(s => s.thread_id === state.selected);
  const thread = session || work;
  const status = thread ? threadGrouping.status(thread) : null;
  const active = running || status?.key === "running";
  $("status").textContent = state.detail?.requests?.length ? "Needs input" : active || status?.key === "completed" ? "" :
    thread && thread.response_state !== "reply" ? status.label : "";
  $("status").hidden = !$("status").textContent;
  $("composer").hidden = !work?.browser_session;
  $("send").disabled = !state.detail || !work?.browser_session || (running && !state.detail.activeTurn) || archived || state.busy || !!state.attachments?.pending() || state.closing.has(state.selected);
  $("send").textContent = running ? (work?.client === "claude" ? "Queue ↑" : "Steer ↑") : "Send ↑";
  $("queue").hidden = !running || work?.client === "claude";
  $("queue").disabled = $("send").disabled;
  $("message").disabled = archived;
  $("stop").hidden = !running;
  $("close-thread").textContent = archived ? "Reopen" : running || ["running", "needs input"].includes(work?.status) ? "Stop and close" : "Close thread";
  $("close-thread").disabled = !state.detail || state.closing.has(state.selected);
  $("fork-thread").hidden = work?.client !== "codex";
  $("fork-thread").disabled = !work?.can_fork || active || !!state.detail?.requests?.length || state.forking || state.closing.has(state.selected);
  $("park").disabled = !state.detail || archived || state.closing.has(state.selected);
  $("edit-checkpoint").disabled = !state.detail;
  $("add-link").disabled = !state.detail;
  $("session-name").disabled = !state.detail || isSessionDraft();
  $("organize-thread").disabled = !state.detail;
  $("composer-hint").textContent = archived ? "Reopen this thread to continue." : running ? (work?.client === "claude" ? "Enter to queue after this turn · Shift + Enter for a new line" : "Enter to steer · Tab to queue after this turn · Shift + Enter for a new line") : "Enter to send · Shift + Enter for a new line · /cd · /model · /help";
  state.attachments?.render();
  state.slashCommands?.render();
  if (state.paneMode) globalThis.agentCoordPane?.updateStatus();
}
function renderQueuedMessages() {
  const area = $("queued-messages"), id = state.selected;
  const items = state.detail?.queuedMessages || [];
  area.hidden = !items.length;
  area.replaceChildren();
  if (!items.length) return;
  area.append(node("strong", "Queued for after this turn"));
  const archived = state.detail?.work_thread?.attention === "archived";
  const change = body => action(async () => {
    await api(sessionPath(id) + "/queue", body);
    if (state.selected === id) await refreshDetail();
    await refreshList();
  });
  for (const item of items) {
    const row = node("div", null, "queued-message");
    row.append(node("p", item.message));
    if (item.images?.length) ChatImageAttachments.appendPreviews(document, row, item.images);
    if (item.error) row.append(node("small", item.error, "queued-error"));
    const cancel = node("button", "Remove", "quiet"); cancel.type = "button";
    cancel.disabled = archived || item.state === "sending";
    cancel.setAttribute("aria-label", "Remove queued message: " + (item.message.slice(0, 80) || "Attached images"));
    cancel.onclick = () => change({action: "cancel", id: item.id});
    row.append(cancel); area.append(row);
  }
  if (items.some(item => item.state === "paused")) {
    const resume = node("button", "Resume queue", "quiet"); resume.type = "button";
    resume.disabled = archived;
    resume.onclick = () => change({action: "resume"});
    area.append(resume);
  }
}
function itemText(item) {
  if (item.type === "userMessage") return (item.content || []).map(c => c.text ||
    (c.type === "localImage" || (c.type === "image" && !ChatImageAttachments.isImageURL(c.url)) ? "[Image]" : "")).filter(Boolean).join("\n");
  if (item.type === "agentMessage") return item.text || "";
  if (item.type === "reasoning") return (item.summary || item.content || []).map(x => typeof x === "string" ? x : x.text || "").join("\n");
  if (item.type === "commandExecution") return [item.command, item.aggregatedOutput, item.exitCode != null ? "Exit code: " + item.exitCode : ""].filter(Boolean).join("\n\n");
  if (item.type === "fileChange") return (item.changes || []).map(c => c.path + "\n" + (c.diff || "")).join("\n\n");
  if (item.type === "plan") return item.text || "";
  return JSON.stringify(item, null, 2);
}
function renderTimeline() {
  const el = $("timeline");
  const atBottom = state.timelineScroll?.beforeRender() ?? (el.scrollHeight - el.scrollTop - el.clientHeight < 100);
  const open = new Set([...el.querySelectorAll("details[open]")].map(d => d.dataset.id));
  const fragment = document.createDocumentFragment();
  if (state.detail?.thread.historyUnavailable) {
    const notice = node("p", "Conversation history couldn’t be fully loaded. Reload to try again.", "empty");
    notice.setAttribute("role", "status");
    fragment.append(notice);
  }
  for (const turn of state.detail?.thread.turns || []) {
    for (const item of turn.items || []) {
      const text = itemText(item);
      if (item.type === "userMessage" || item.type === "agentMessage") {
        const message = node("article", null, "message " + (item.type === "userMessage" ? "user" : "agent"));
        const body = node("div", null, item.type === "agentMessage" ? "text markdown" : "text");
        if (item.type === "agentMessage") body.innerHTML = messageMarkdown.render(text);
        else {
          body.textContent = text;
          const images = (item.content || []).filter(c => c.type === "image");
          if (images.length) ChatImageAttachments.appendPreviews(document, body, images);
        }
        message.append(node("span", item.type === "userMessage" ? "You" : (state.detail?.work_thread?.client === "claude" ? "Claude" : "Codex"), "speaker"), body);
        fragment.append(message);
      } else {
        const detail = node("details", null, "tool"); detail.dataset.id = item.id; detail.open = open.has(item.id);
        const labels = {commandExecution: "Command", fileChange: "File changes", reasoning: "Thinking", mcpToolCall: "Tool", plan: "Plan", webSearch: "Web search"};
        detail.append(node("summary", (labels[item.type] || item.type) + (item.status ? " · " + item.status : "")), node("pre", text));
        fragment.append(detail);
      }
    }
    if (turn.error) fragment.append(node("p", turn.error.message || "This turn failed.", "empty"));
    if (turn.status === "interrupted") fragment.append(node("p", "Turn stopped.", "empty"));
  }
  el.replaceChildren(fragment);
  if (!el.childNodes.length) el.append(node("p", state.detail?.work_thread.browser_session ? "This is the beginning of your session. What would you like to work on?" : "This conversation is in a terminal session. Its checkpoint and related links are saved here." + (state.detail?.work_thread.client === "codex" ? " Close that session to continue it in the browser." : " Continue the conversation in Claude Code."), "empty"));
  if (state.timelineScroll) state.timelineScroll.afterRender(atBottom);
  else if (atBottom) el.scrollTop = el.scrollHeight;
}
function requestButton(label, decision, request, form) {
  const id = state.selected;
  const button = node("button", label, decision === "accept" ? "primary" : "quiet"); button.type = "button";
  button.onclick = () => action(async () => {
    const ticket = state.rollUp?.ticket(id);
    const body = {decision};
    if (form) Object.assign(body, form());
    await api(sessionPath(id) + "/requests/" + encodeURIComponent(request.key), body);
    if (id === state.selected) await refreshDetail();
    await refreshList();
    await state.rollUp?.responded(ticket, {pending: !!state.detail?.requests?.length});
  }, button);
  return button;
}
function renderRequests() {
  const area = $("requests");
  // Preserve partially entered answers while unrelated events stream.
  const signature = JSON.stringify((state.detail?.requests || []).map(r => r.key));
  if (area.dataset.signature === signature) return;
  area.dataset.signature = signature; area.replaceChildren();
  for (const request of state.detail?.requests || []) {
    const box = node("section", null, "request"), params = request.params;
    const actions = node("div", null, "actions");
    if (request.method === "item/tool/requestUserInput") {
      box.append(node("h3", (state.detail?.work_thread?.client === "claude" ? "Claude" : "Codex") + " has a question"));
      const fields = [];
      for (const question of params.questions || []) {
        const label = node("label", question.question);
        const input = node("input"); input.type = question.isSecret ? "password" : "text";
        if (question.options?.length) {
          const options = node("datalist"); options.id = "options-" + request.key + "-" + fields.length;
          for (const option of question.options) options.append(node("option", option.label));
          input.setAttribute("list", options.id); label.append(options);
        }
        label.append(input); box.append(label); fields.push([question.id, input]);
      }
      actions.append(requestButton("Send answer", "accept", request, () => ({answers: Object.fromEntries(fields.map(([id, input]) => [id, {answers: [input.value]}]))})));
    } else if (request.method === "mcpServer/elicitation/request") {
      box.append(node("h3", "A tool needs your input"), node("p", params.message || ""));
      const fields = [];
      const properties = params.requestedSchema?.properties || {};
      for (const [key, schema] of Object.entries(properties)) {
        const label = node("label", schema.title || key);
        const input = node(schema.enum ? "select" : "input");
        if (schema.enum) for (const value of schema.enum) input.append(node("option", String(value)));
        else input.type = schema.type === "boolean" ? "checkbox" : ["number", "integer"].includes(schema.type) ? "number" : "text";
        label.append(input); box.append(label); fields.push([key, schema, input]);
      }
      if (params.mode === "url") {
        try { const url = new URL(params.url); if (["http:", "https:"].includes(url.protocol)) { const link = node("a", "Open tool sign-in page ↗"); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; box.append(link); } } catch (_) { /* Invalid URLs remain inert. */ }
      }
      actions.append(requestButton("Continue", "accept", request, () => ({content: Object.fromEntries(fields.map(([key, schema, input]) => [key, schema.type === "boolean" ? input.checked : ["number", "integer"].includes(schema.type) ? Number(input.value) : input.value]))})), requestButton("Cancel", "cancel", request));
    } else if (["item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"].includes(request.method)) {
      box.append(node("h3", (state.detail?.work_thread?.client === "claude" ? "Claude" : "Codex") + " is requesting approval"));
      if (params.reason) box.append(node("p", params.reason));
      box.append(node("pre", params.command || (params.permissions || params.networkApprovalContext ? JSON.stringify(params.permissions || params.networkApprovalContext, null, 2) : params.grantRoot || "Apply the file changes shown above.")));
      const choices = params.availableDecisions || (request.method === "item/permissions/requestApproval" ? ["accept", "decline"] : ["accept", "acceptForSession", "decline", "cancel"]);
      const labels = {accept: "Allow once", acceptForSession: "Allow for session", decline: "Decline", cancel: "Cancel"};
      for (const decision of choices) if (typeof decision === "string" && labels[decision]) actions.append(requestButton(labels[decision], decision, request));
    } else {
      box.append(node("h3", "This request is not supported yet"), node("p", "Cancel this request to continue."));
      actions.append(requestButton("Cancel request", "cancel", request));
    }
    box.append(actions); area.append(box);
  }
}
function applyEvent(event) {
  if (!state.detail || event.params.threadId !== state.selected) return;
  const {method, params} = event;
  if (event.requestKey) { state.detail.requests.push({key: event.requestKey, method, params}); renderRequests(); renderStatus(); return; }
  if (method === "turn/started") { state.detail.running = true; state.detail.activeTurn = params.turn.id; }
  if (method === "turn/completed") { state.detail.running = false; scheduleDetail(); }
  if (method === "serverRequest/resolved" || method === "browser/requests") scheduleDetail();
  if (method === "browser/changed") scheduleDetail();
  if (!method.startsWith("item/") || !params.turnId) { renderStatus(); return; }
  const turns = state.detail.thread.turns ||= [];
  let turn = turns.find(t => t.id === params.turnId);
  if (!turn) { turn = {id: params.turnId, items: [], status: "inProgress"}; turns.push(turn); }
  if (params.item) {
    const index = turn.items.findIndex(i => i.id === params.item.id);
    if (index < 0) turn.items.push(params.item); else turn.items[index] = params.item;
  } else if (params.itemId && typeof params.delta === "string") {
    let item = turn.items.find(i => i.id === params.itemId);
    if (!item && method === "item/agentMessage/delta") { item = {id: params.itemId, type: "agentMessage", text: ""}; turn.items.push(item); }
    if (item && method === "item/agentMessage/delta") item.text = (item.text || "") + params.delta;
    if (item && method === "item/commandExecution/outputDelta") item.aggregatedOutput = (item.aggregatedOutput || "") + params.delta;
  }
  renderTimeline(); renderStatus();
}
let listTimer, detailTimer;
function scheduleList() { clearTimeout(listTimer); listTimer = setTimeout(() => refreshList().catch(showError), 150); }
function scheduleDetail() { clearTimeout(detailTimer); detailTimer = setTimeout(() => refreshDetail().catch(showError), 100); }
function connect() {
  const events = new EventSource(base + "events?completion_after=" + state.config.completionCursor);
  events.onopen = () => { $("connection").textContent = "Connected locally"; $("connection").dataset.state = "connected"; scheduleList(); if (state.selected) scheduleDetail(); };
  events.onerror = () => { $("connection").textContent = "Reconnecting…"; $("connection").dataset.state = "reconnecting"; };
  events.onmessage = message => {
    const batch = JSON.parse(message.data);
    globalThis.agentCoordPanes?.receive(batch);
    // Completions apply to every thread, including when this tab is hidden.
    notifications.receive(batch.completions || []);
    notifications.receive(batch.approvals || []);
    if (batch.completions?.length) { scheduleList(); if (state.selected) scheduleDetail(); }
    if (batch.reset) scheduleDetail();
    for (const event of batch.events) {
      if (event.method === "bridge/disconnected") { showError(new Error("Codex disconnected. Restart Ribbon Field to reopen saved sessions.")); if (state.detail?.work_thread?.client !== "claude" && state.detail) state.detail.running = false; renderStatus(); }
      else applyEvent(event);
      if (!event.method.endsWith("/delta") && !event.method.endsWith("/outputDelta")) scheduleList();
    }
  };
  window.addEventListener("pagehide", () => events.close(), {once: true});
}
function newProject() {
  $("project-form").reset();
  $("project-error").hidden = true;
  $("project-dialog").showModal();
  $("project-name").focus();
}
$("create-project").onclick = $("welcome-project").onclick = newProject;
$("project-form").onsubmit = event => {
  event.preventDefault();
  action(async () => {
    const name = $("project-name").value.trim();
    if (!name) throw new Error("Enter a project name.");
    const project = await api("projects", {name});
    $("project-dialog").close();
    $("view").value = "active";
    $("search").value = ""; $("phase-filter").value = ""; $("repository").value = "";
    await refreshList();
    $("project").value = project.id;
    goHome();
  }, $("save-project"));
};
function assignmentFields(prefix) {
  for (const field of ["repository", "project"]) {
    const creating = $(prefix + "-" + field).value === "__new__";
    $(prefix + "-" + field + "-input").hidden = !creating;
    $(prefix + "-" + field + "-value").required = creating;
  }
}
async function fillAssignments(prefix, thread = null) {
  state.organization = await api("organization");
  const repository = $(prefix + "-repository"), project = $(prefix + "-project");
  repository.replaceChildren(new Option("No repository", ""));
  if (!thread) repository.prepend(new Option("Detect from workspace", "__auto__"));
  for (const item of state.organization.repositories) repository.append(new Option(item.name + " · " + item.root, item.id));
  repository.append(new Option("Add a repository…", "__new__"));
  project.replaceChildren(new Option("No project", ""));
  for (const item of state.organization.projects) project.append(new Option(item.name, item.id));
  project.append(new Option("Create a project…", "__new__"));
  repository.value = thread ? thread.repository_id || "" : "__auto__";
  if (!thread) {
    const filteredRepository = $("repository").value;
    if (filteredRepository === threadOrganization.NONE) repository.value = "";
    else if (state.organization.repositories.some(item => item.id === filteredRepository)) repository.value = filteredRepository;
  }
  const filteredProject = $("project").value;
  project.value = thread ? thread.project_id || "" : state.organization.projects.some(item => item.id === filteredProject) ? filteredProject : "";
  for (const field of ["repository", "project"]) $(prefix + "-" + field + "-value").value = "";
  $(prefix + "-organization-error").hidden = true;
  assignmentFields(prefix);
}
async function assignmentPayload(prefix) {
  const body = {};
  for (const field of ["repository", "project"]) {
    let value = $(prefix + "-" + field).value;
    if (value === "__auto__") continue;
    if (value === "__new__") {
      const input = $(prefix + "-" + field + "-value").value;
      const record = await api(field === "repository" ? "repositories" : "projects",
        field === "repository" ? {path: input} : {name: input});
      value = record.id;
    }
    body[field + "_id"] = value || null;
  }
  return body;
}
for (const prefix of ["edit"]) {
  for (const field of ["repository", "project"]) $(prefix + "-" + field).onchange = () => assignmentFields(prefix);
}
let organizationThreadId = null;
$("organize-thread").onclick = () => action(async () => {
  const id = state.selected;
  if (!id || !state.detail) return;
  await fillAssignments("edit", state.detail.work_thread);
  if (state.selected !== id) return;
  organizationThreadId = id;
  $("organization-workspace").textContent = "Workspace: " + state.detail.work_thread.cwd;
  $("organization-dialog").showModal();
  $("edit-project").focus();
}, $("organize-thread"));
$("organization-form").onsubmit = event => {
  event.preventDefault();
  action(async () => {
    const id = organizationThreadId, values = await assignmentPayload("edit");
    await api(threadPath(id), values);
    $("organization-dialog").close();
    if (state.selected === id) await refreshThread();
    await refreshList();
  }, $("save-organization"));
};
function isSessionDraft() { return state.selected === "new-session" && !!state.newSessionDraft; }
function modelValue(client, model) { return JSON.stringify([client, model || ""]); }
function renderModelPicker() {
  const session = state.detail?.session, picker = $("model-picker");
  if (!session) { picker.disabled = true; globalThis.modelPicker?.sync(); return; }
  const draft = isSessionDraft(), client = session.client || "codex";
  const clients = draft ? ["codex", "claude"] : [client];
  const catalogs = state.modelCatalogs ||= new Map();
  const signature = JSON.stringify([clients, session.model, [...catalogs]]);
  if (picker.dataset.signature !== signature) {
    picker.dataset.signature = signature;
    picker.replaceChildren();
    for (const provider of clients) {
      const group = node("optgroup"); group.label = provider === "claude" ? "Claude Code" : "Codex";
      if (draft || !session.model) group.append(new Option(group.label + " default", modelValue(provider, "")));
      const models = catalogs.get(provider) || [];
      for (const model of models) group.append(new Option(model.displayName || model.model, modelValue(provider, model.model)));
      if (provider === client && session.model && !models.some(model => model.model === session.model)) {
        group.append(new Option(session.model, modelValue(provider, session.model)));
      }
      picker.append(group);
    }
  }
  picker.value = modelValue(client, session.model);
  picker.disabled = state.busy || !!state.detail.running || state.detail.work_thread?.attention === "archived" || !!state.newSessionDraft?.createdId && draft;
  const errors = clients.filter(provider => state.modelErrors?.has(provider));
  $("model-picker-status").textContent = errors.length ? errors.map(provider => (provider === "claude" ? "Claude Code" : "Codex") + " models unavailable").join(" · ") : "";
  globalThis.modelPicker?.sync();
}
async function loadSessionModels() {
  const clients = isSessionDraft() ? ["codex", "claude"] : [state.detail?.session?.client || "codex"];
  const catalogs = state.modelCatalogs ||= new Map(), loading = state.modelLoading ||= new Set();
  const errors = state.modelErrors ||= new Map();
  await Promise.all(clients.map(async client => {
    if (catalogs.has(client) || loading.has(client)) return;
    loading.add(client);
    try { catalogs.set(client, (await api("models?client=" + client)).data); errors.delete(client); }
    catch (error) { errors.set(client, error.message); }
    finally { loading.delete(client); renderModelPicker(); }
  }));
}
async function changeSessionModel() {
  const id = state.selected, [client, model] = JSON.parse($("model-picker").value);
  const draft = isSessionDraft();
  if (state.busy || state.detail?.running || draft && state.newSessionDraft.createdId) return;
  state.busy = true;
  try {
    if (draft) {
      Object.assign(state.newSessionDraft.session, {client, model: model || null, effort: null});
      state.newSessionDraft.work_thread.client = client;
    } else {
      const session = await api(sessionPath(id), {model});
      if (state.selected === id) state.detail.session = session;
    }
  } finally { state.busy = false; renderStatus(); }
}
$("model-picker").onchange = () => action(changeSessionModel);
$("model-picker").onfocus = () => { loadSessionModels(); };
async function newSession() {
  if (!state.config || state.creatingSession) return;
  const body = {cwd: globalThis.agentCoordPanes?.focusedRecord?.thread.cwd || state.detail?.work_thread.cwd || state.config.cwd};
  const repository = state.organization.repositories.find(item => item.id === $("repository").value);
  if (repository) { body.cwd = repository.root; body.repository_id = repository.id; }
  else if ($("repository").value === threadOrganization.NONE) body.repository_id = null;
  const project = state.organization.projects.find(item => item.id === $("project").value);
  if (project) body.project_id = project.id;
  goHome();
  state.newSessionDraft ||= {
    session: {...body, client: "codex", model: null, effort: null, yolo: false},
    work_thread: {thread_id: "new-session", title: "New session", cwd: body.cwd, client: "codex", browser_session: true, attention: "now", links: []},
    thread: {turns: []}, requests: [], queuedMessages: [], running: false,
  };
  state.selected = "new-session";
  state.detail = state.newSessionDraft;
  state.titleEdit = null;
  $("message").value = state.drafts.get("new-session") || "";
  $("welcome").hidden = true;
  $("conversation").hidden = false;
  $("conversation").classList.add("session-draft");
  $("page-location").textContent = "New session";
  $("timeline").replaceChildren(node("p", "Choose a model and send a message to start.", "empty"));
  state.timelineScroll?.reset();
  $("requests").replaceChildren(); delete $("requests").dataset.signature;
  $("thread-context").hidden = true;
  renderTitle(); renderQueuedMessages(); renderStatus();
  navigation?.remember();
  $("message").focus();
  // Each provider loads independently; an unavailable CLI cannot block the other.
  await loadSessionModels();
}
async function sendSessionDraft(text, images) {
  const draft = state.newSessionDraft;
  state.creatingSession = true;
  try {
    if (!draft.createdId) {
      const result = await api("sessions", {...draft.session});
      draft.createdId = result.session.thread_id;
    }
    const body = {message: text, windowId: state.sourceWindowId};
    if (images.length) body.images = images.map(({name, url}) => ({name, url}));
    const result = await api(sessionPath(draft.createdId) + "/messages", body);
    state.attachments?.sent("new-session", images);
    const remainingImages = state.attachments?.items("new-session") || [];
    if (remainingImages.length) {
      state.attachments.drafts.set(draft.createdId, remainingImages);
      state.attachments.drafts.delete("new-session");
    }
    if (result.command) state.commandFeedback.set(draft.createdId, result.command.message);
    // Preserve edits typed while creation or sending was in flight.
    const remaining = isSessionDraft() ? $("message").value : state.drafts.get("new-session");
    if (remaining && remaining !== text) state.drafts.set(draft.createdId, remaining);
    state.drafts.delete("new-session");
    state.newSessionDraft = null;
    if (state.selected === "new-session") {
      $("message").value = "";
      await select(draft.createdId);
    }
    await refreshList();
  } finally { state.creatingSession = false; }
}
$("new-session").onclick = $("welcome-new").onclick = () => action(newSession);
function filtersChanged() { savedViews?.persist(); navigation?.remember(); renderList(); }
$("view").onchange = () => action(async () => { await savedViews?.persist(); await refreshList(); });
for (const [id, placement] of [["show-now", "active"], ["show-later", "later"]]) $(id).onclick = () => {
  $("view").value = placement;
  renderClosedToggle();
  return $("view").onchange();
};
$("show-closed").onclick = () => {
  $("view").value = $("view").value === "archived" ? "active" : "archived";
  renderClosedToggle();
  return $("view").onchange();
};
$("repository").onchange = $("project").onchange = $("phase-filter").onchange = filtersChanged;
try { const saved = localStorage.getItem("agent-coord-group-by"); if (["phase", "repository", "project", "none"].includes(saved)) $("group-by").value = saved; } catch (_) { /* Preferences are optional. */ }
$("group-by").onchange = () => {
  if (!savedViews || savedViews.activeId === "all") {
    try { localStorage.setItem("agent-coord-group-by", $("group-by").value); } catch (_) { /* Preferences are optional. */ }
  }
  filtersChanged();
};
$("search").oninput = filtersChanged;
function setChatExpanded(expanded) {
  expanded = Boolean(expanded && state.selected);
  if (expanded) setNavigation(false);
  document.body.classList.toggle("chat-expanded", expanded);
  const button = $("expand-chat"), label = expanded ? "Collapse chat" : "Expand chat";
  button.setAttribute("aria-pressed", String(expanded));
  button.setAttribute("aria-label", label);
  button.title = expanded ? label + " (Esc)" : label;
}
$("expand-chat").onclick = () => setChatExpanded(!document.body.classList.contains("chat-expanded"));
function goHome() {
  state.rollUp?.stop();
  if (state.paneMode) {
    const host = window.parent.agentCoordPanes;
    const index = host.layout?.groups.findIndex(group => group.includes(state.selected));
    if (index >= 0) host.close(index);
    return;
  }
  globalThis.agentCoordPanes?.leave();
  setChatExpanded(false);
  if (state.selected) state.drafts.set(state.selected, $("message").value);
  state.selected = null; state.detail = null; state.titleEdit = null;
  if (navigation) navigation.remember();
  else history.replaceState(null, "", location.pathname + location.search);
  state.attachments?.highlight(false);
  notifications?.syncFocus();
  $("conversation").classList.remove("session-draft");
  $("conversation").hidden = true; $("welcome").hidden = false; $("page-location").textContent = "Overview";
  setNavigation(false); renderList(); $("overview-title").focus({preventScroll: true});
  savedViews?.restoreScroll();
}
document.querySelector(".brand").onclick = event => { event.preventDefault(); goHome(); };
$("home").onclick = $("back-home").onclick = goHome;
const mobileNavigation = window.matchMedia("(max-width: 720px)");
let sidebarCollapsed = false;
try { sidebarCollapsed = localStorage.getItem("agent-coord.sidebar-collapsed") === "true"; } catch { /* Storage may be unavailable. */ }
function setNavigation(open) {
  open = mobileNavigation.matches && open;
  const expanded = mobileNavigation.matches ? open : !sidebarCollapsed;
  const label = mobileNavigation.matches ? (open ? "Close navigation" : "Open navigation") : (expanded ? "Hide sidebar" : "Show sidebar");
  document.body.classList.toggle("navigation-open", open);
  document.body.classList.toggle("sidebar-collapsed", !mobileNavigation.matches && sidebarCollapsed);
  $("sidebar-backdrop").hidden = !open;
  $("menu-toggle").setAttribute("aria-expanded", String(expanded));
  $("menu-toggle").setAttribute("aria-label", label);
  $("menu-toggle").title = label;
  $("main").inert = open;
  if (open) document.querySelector(".brand").focus();
  else if (!expanded && $("sidebar").contains(document.activeElement)) $("menu-toggle").focus();
}
$("menu-toggle").onclick = () => {
  if (mobileNavigation.matches) {
    setNavigation(!document.body.classList.contains("navigation-open"));
  } else {
    sidebarCollapsed = !sidebarCollapsed;
    try { localStorage.setItem("agent-coord.sidebar-collapsed", String(sidebarCollapsed)); } catch { /* Keep the toggle usable without storage. */ }
    setNavigation(false);
  }
};
$("sidebar-backdrop").onclick = () => { setNavigation(false); $("menu-toggle").focus(); };
mobileNavigation.addEventListener("change", () => setNavigation(false));
setNavigation(false);
const compactContext = window.matchMedia("(max-width: 1100px)");
let contextCollapsed = null;
try {
  const saved = localStorage.getItem("agent-coord.context-collapsed");
  if (saved === "true" || saved === "false") contextCollapsed = saved === "true";
} catch { /* The panel remains usable without preference storage. */ }
function layoutContext() {
  const context = $("thread-context"), panel = $("context-panel"), button = $("toggle-context");
  const collapsed = contextCollapsed ?? compactContext.matches;
  const containsFocus = panel.contains(document.activeElement);
  panel.hidden = collapsed;
  context.open = !collapsed;
  $("conversation").classList.toggle("context-collapsed", collapsed);
  const label = collapsed ? "Show thread context" : "Hide thread context";
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", label);
  button.title = label;
  if (collapsed && containsFocus) button.focus({preventScroll: true});
}
function setContextCollapsed(collapsed) {
  contextCollapsed = collapsed;
  try { localStorage.setItem("agent-coord.context-collapsed", String(collapsed)); } catch { /* Preferences are optional. */ }
  layoutContext();
}
$("toggle-context").onclick = () => setContextCollapsed(!$("context-panel").hidden);
$("thread-context").addEventListener("toggle", () => {
  // Clicking the panel heading also collapses the whole column.
  const collapsed = !$("thread-context").open;
  if (collapsed !== $("context-panel").hidden) setContextCollapsed(collapsed);
});
compactContext.addEventListener("change", layoutContext);
layoutContext();
document.addEventListener("keydown", event => {
  if (event.defaultPrevented || document.querySelector("dialog[open]")) return;
  if (event.key === "Escape" && document.body.classList.contains("chat-expanded")) {
    event.preventDefault(); setChatExpanded(false); $("expand-chat").focus(); return;
  }
  if (document.body.classList.contains("navigation-open")) {
    if (event.key === "Escape") { event.preventDefault(); setNavigation(false); $("menu-toggle").focus(); }
    if (event.key === "Tab") {
      const items = [...$("sidebar").querySelectorAll("a, button:not(:disabled), select")];
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    return;
  }
  if (event.key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey && !event.target.closest("input, textarea, select, [contenteditable]")) {
    event.preventDefault(); goHome(); $("search").focus();
  }
});
$("search").onkeydown = event => {
  if (event.key === "Escape") { $("search").value = ""; filtersChanged(); }
};
async function sendMessage(mode = "steer") {
  if (state.detail?.running && state.detail?.work_thread?.client === "claude") mode = "queue";
  const detail = state.detail;
  const id = state.selected, text = $("message").value;
  const ticket = state.rollUp?.ticket(id);
  const images = state.attachments?.snapshot(id) || [];
  if (state.busy || !detail?.work_thread?.browser_session || detail.work_thread.attention === "archived" || state.closing?.has(state.selected) ||
      (detail.running && !detail.activeTurn) || state.attachments?.pending(id) || (!text.trim() && !images.length)) return;
  const body = {message: text};
  if (state.sourceWindowId) body.windowId = state.sourceWindowId;
  if (images.length) body.images = images.map(({name, url}) => ({name, url}));
  if (detail.running && mode !== "queue") body.expectedTurnId = detail.activeTurn;
  state.busy = true; renderStatus();
  try {
    if (isSessionDraft()) {
      if (text.trim() === "/close") {
        state.drafts.delete("new-session"); state.attachments?.drafts.delete("new-session");
        $("message").value = ""; state.newSessionDraft = null; goHome(); return;
      }
      await sendSessionDraft(text, images); return;
    }
    const command = text.trim().split(/\s+/)[0].toLowerCase();
    if (command === "/fork" || command === "/close") {
      if (text.trim().toLowerCase() !== command) throw new Error("Use " + command + " without arguments.");
      if (images.length) throw new Error("Remove attached images before using " + command + ".");
      const clearCommand = () => {
        state.commandFeedback.delete(id);
        if (id === state.selected && $("message").value === text) {
          $("message").value = ""; state.drafts.delete(id);
        } else if (state.drafts.get(id) === text) state.drafts.delete(id);
      };
      // Thread actions run immediately, including when the composer is in queue mode.
      if (command === "/fork") await forkThread(clearCommand);
      else await toggleThreadClosed(detail.work_thread, clearCommand);
      return;
    }
    const result = await api(sessionPath(id) + (mode === "queue" ? "/queue" : "/messages"), body);
    state.attachments?.sent(id, images);
    if (result.command) state.commandFeedback.set(id, result.command.message);
    else state.commandFeedback.delete(id);
    if (id === state.selected) {
      if ($("message").value === text) { $("message").value = ""; state.drafts.delete(id); }
      else state.drafts.set(id, $("message").value);
      await refreshDetail();
    } else if (state.drafts.get(id) === text) state.drafts.delete(id);
    await refreshList();
    if (!result.command) await state.rollUp?.responded(ticket, {pending: !!state.detail?.requests?.length});
  }
  finally { state.busy = false; renderStatus(); if (state.paneMode) globalThis.agentCoordPane?.saveDraft(); }
}
$("composer").onsubmit = event => { event.preventDefault(); action(sendMessage); };
function composerKeydown(event) {
  if (event.isComposing || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
  if (state.slashCommands?.keydown(event)) return;
  if (event.key === "Enter") {
    event.preventDefault();
    if (!$("send").disabled) $("composer").requestSubmit();
  } else if (event.key === "Tab" && state.detail?.running && ($("message").value.trim() || state.attachments?.snapshot().length)) {
    event.preventDefault();
    if (!$("queue").disabled) action(() => sendMessage("queue"));
  }
}
$("message").onkeydown = composerKeydown;
$("queue").onclick = () => action(() => sendMessage("queue"));
$("stop").onclick = () => action(async () => { await api(sessionPath(state.selected) + "/interrupt", {}); }, $("stop"));
async function toggleThreadClosed(thread = state.detail?.work_thread, onSuccess = () => {}) {
  const id = thread?.thread_id || state.selected;
  if (!id || !thread || state.closing.has(id)) return;
  const reopening = thread.attention === "archived";
  state.closing.add(id); renderStatus();
  try {
    await api(threadPath(id) + (reopening ? "/reopen" : "/close"), {});
    onSuccess();
    if (state.selected === id) {
      $("view").value = "active";
      if (reopening) await refreshDetail();
      else { state.sessions = state.sessions.filter(thread => thread.thread_id !== id); goHome(); }
    }
    await refreshList();
  } finally { state.closing.delete(id); renderStatus(); }
}
$("close-thread").onclick = () => action(toggleThreadClosed, $("close-thread"));
$("park").onclick = () => action(() => toggleThreadLater(state.detail.work_thread), $("park"));
$("snooze-thread-button").onclick = () => openThreadSnooze(state.detail.work_thread);
$("resume-snooze").onclick = () => action(() => resumeThreadSnooze(state.detail.work_thread), $("resume-snooze"));
$("snooze-duration").onchange = () => {
  const custom = $("snooze-duration").value === "custom";
  $("snooze-custom-label").hidden = !custom; $("snooze-custom").required = custom;
  if (custom) $("snooze-custom").focus();
};
$("snooze-form").onsubmit = async event => {
  event.preventDefault();
  if ($("save-snooze").disabled) return;
  $("save-snooze").disabled = true; $("snooze-error").hidden = true;
  try {
    await snoozeThread(state.snoozingThread, snoozeTime($("snooze-duration").value, $("snooze-custom").value));
    $("snooze-dialog").close();
  } catch (error) { $("snooze-error").textContent = error.message; $("snooze-error").hidden = false; }
  finally { $("save-snooze").disabled = false; }
};
$("handle-response").onclick = () => action(() => markThreadHandled(state.detail.work_thread), $("handle-response"));
$("session-name").onclick = startTitleEdit;
$("cancel-rename").onclick = cancelTitleEdit;
$("rename-name").oninput = () => $("rename-name").setCustomValidity("");
$("rename-name").onkeydown = event => {
  if (event.key === "Escape") { event.preventDefault(); cancelTitleEdit(); }
  if (event.key === "Enter" && event.isComposing) event.preventDefault();
};
$("rename-form").onsubmit = event => { event.preventDefault(); action(saveTitleEdit); };
$("edit-permissions").onclick = openSessionPermissions;
$("permissions-form").onsubmit = event => { event.preventDefault(); action(saveSessionPermissions, $("save-permissions")); };
document.querySelectorAll("[data-close]").forEach(button => { button.onclick = () => $(button.dataset.close).close(); });
$("edit-checkpoint").onclick = () => {
  const cp = state.detail.work_thread.checkpoint;
  $("checkpoint-edit-phase").value = cp?.phase || "discussion"; $("checkpoint-summary").value = cp?.summary || "";
  $("checkpoint-next").value = cp?.next_action || ""; $("checkpoint-actor").value = cp?.next_actor || "nobody";
  $("checkpoint-dialog").showModal(); $("checkpoint-summary").focus();
};
$("checkpoint-form").onsubmit = event => { event.preventDefault(); action(async () => {
  await api(threadPath(state.selected) + "/checkpoint", {phase: $("checkpoint-edit-phase").value, summary: $("checkpoint-summary").value, next_action: $("checkpoint-next").value, next_actor: $("checkpoint-actor").value});
  $("checkpoint-dialog").close(); await refreshThread(); await refreshList();
}, $("checkpoint-form").querySelector("button.primary")); };
$("add-link").onclick = () => { $("link-form").reset(); $("link-dialog").showModal(); $("link-target").focus(); };
$("link-form").onsubmit = event => { event.preventDefault(); action(async () => {
  await api(threadPath(state.selected) + "/links", {kind: $("link-kind").value, label: $("link-label").value, target: $("link-target").value});
  $("link-dialog").close(); await refreshThread(); await refreshList();
}, $("link-form").querySelector("button.primary")); };
$("resume-browser").onclick = () => action(async () => {
  await api(threadPath(state.selected) + "/resume", {}); await refreshDetail(); await refreshList();
}, $("resume-browser"));
$("fork-thread").onclick = () => action(forkThread);
async function forkThread(onSuccess = () => {}) {
  const source = state.selected;
  if (state.forking) return;
  state.forking = true; renderStatus();
  try {
    const fork = await api(threadPath(source) + "/fork", {});
    onSuccess();
    await refreshList();
    // A response must not pull the user away from another thread they opened.
    if (state.selected === source) await select(fork.thread_id);
  } finally { state.forking = false; renderStatus(); }
}
async function boot() {
  state.timelineScroll = new ConversationScroll($("timeline"), $("jump-to-latest"));
  const initialNavigation = agentCoordNavigation.initialLink(location);
  const initialThread = decodeURIComponent(location.hash.slice(1));
  state.config = await api("config");
  state.slashCommands = new ChatSlashCommands({input: $("message"), menu: $("slash-commands"),
    getThread: () => state.selected,
    getSession: () => state.detail?.session,
    loadModels: async client => (await api("models?client=" + encodeURIComponent(client))).data,
    canComplete: () => !isSessionDraft() && !!state.detail?.work_thread?.browser_session && !$("composer").hidden && !state.busy &&
      state.detail.work_thread.attention !== "archived" && !state.closing.has(state.selected),
    onChange: () => { state.drafts.set(state.selected, $("message").value); globalThis.agentCoordPane?.saveDraft(); }});
  state.attachments = new ChatImageAttachments({document, getThread: () => state.selected,
    canAttach: () => !!state.detail?.work_thread?.browser_session && state.detail.work_thread.attention !== "archived" && !state.closing.has(state.selected),
    onChange: renderStatus, onError: showError});
  state.attachments.bind();
  if (state.paneMode) {
    await setupPaneConversation({state, document, api, select, refreshDetail, refreshThread, applyEvent, scheduleDetail, renderStatus, showError});
    return;
  }
  const Notifications = state.config.remote ? PhoneNotifications : TurnNotifications;
  notifications = new Notifications({
    button: $("notifications"),
    api,
    claim: async completion_id => (await api("notifications/claim", {completion_id})).claimed,
    claimApproval: async request_key => (await api("notifications/claim", {request_key})).claimed,
    selected: () => window.agentCoordPanes?.active ? window.agentCoordPanes.selected : state.selected,
    openThread: select,
    onError: showError,
  });
  if (state.config.remote) notifications.start().catch(showError);
  $("monitor").href = "/monitor" + location.search;
  let windowStorage, preferences;
  try { windowStorage = sessionStorage; preferences = localStorage; } catch { /* Views work without browser storage. */ }
  savedViews = new SavedViews({document, api, storage: windowStorage, preferences, scope: state.config.workspaceRoot,
    onSwitch: async () => { goHome(); await refreshList(); window.agentCoordPanes?.restoreView(); }, onError: showError});
  await savedViews.start();
  setupPaneShell({state, document, api, select, goHome, refreshList, showError, notifications: () => notifications,
    getView: () => savedViews.activeId,
    getThreads: () => state.sessions.filter(thread => threadViews.matches(thread, savedViews.read().filters))});
  setupRollUp({document, state, api, select, action, markHandled: markThreadHandled});
  window.addEventListener("agent-coord-pane-state", () => { if (window.agentCoordPanes?.active) state.rollUp.stop(); });
  await refreshList(); connect();
  savedViews.restoreScroll();
  navigation = new agentCoordNavigation.Router({api, history, location, windowId: state.sourceWindowId, onError: showError,
    blocked: () => Boolean(document.querySelector("dialog[open]") || window.agentCoordPanes?.focusedRecord?.frame?.contentDocument?.querySelector("dialog[open]")),
    capture: () => ({viewId: savedViews.activeId, ...savedViews.read(), tiled: Boolean(window.agentCoordPanes?.active),
      thread: window.agentCoordPanes?.active ? window.agentCoordPanes.selected : state.selected}),
    apply: async route => {
      if (route.kind === "thread") await select(route.id);
      else if (route.kind === "view") {
        savedViews.sync((await api("views")).data);
        await savedViews.activate(route.id);
        goHome();
      } else { await savedViews.overview(route.filters); goHome(); }
    },
    restore: async snapshot => {
      if (snapshot.viewId === "all") await savedViews.overview(snapshot.filters, snapshot.group_by);
      else await savedViews.activate(snapshot.viewId);
      if (snapshot.tiled) window.agentCoordPanes?.restoreView();
      else goHome();
      if (snapshot.thread) await select(snapshot.thread);
    },
  });
  window.agentCoordNavigate = (url, options) => navigation.open(url, options);
  window.addEventListener("agent-coord-pane-state", () => { if (window.agentCoordPanes?.active) navigation.remember(); });
  window.addEventListener("popstate", event => navigation.back(event.state?.agentCoord));
  document.addEventListener("click", event => {
    const link = event.target.closest("a[href]");
    if (!link?.getAttribute("href")?.startsWith("agentcoord:") || event.button !== 0) return;
    event.preventDefault();
    navigation.open(link.getAttribute("href"));
  });
  if (initialNavigation) await navigation.open(initialNavigation, {push: false});
  if (initialThread) await select(initialThread);
  navigation.remember();
  window.agentCoordDesktop?.navigationReady();
  // Checkpoints written by terminal agents arrive through the shared database.
  const metadataTimer = setInterval(() => {
    if (!document.hidden) { refreshList().catch(showError); refreshThread().catch(showError); window.agentCoordPanes?.refresh(); }
  }, 5000);
  window.addEventListener("pagehide", () => clearInterval(metadataTimer), {once: true});
  window.addEventListener("pagehide", () => savedViews.remember());
}
boot().catch(showError);
