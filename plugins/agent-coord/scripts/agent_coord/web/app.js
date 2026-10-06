"use strict";
const $ = id => document.getElementById(id);
const state = {config: null, models: [], sessions: [], organization: {repositories: [], projects: []}, selected: null, detail: null, drafts: new Map(), closing: new Set(), pinning: new Set(), commandFeedback: new Map(), busy: false, listSignature: ""};
let notifications;
let savedViews;
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
  const result = await response.json();
  if (viewMutation) ++listRequest;
  if (!response.ok) throw new Error(result.error || "Request failed");
  return result;
}
async function action(fn, button) { if (button) button.disabled = true; try { await fn(); } catch (error) { showError(error); } finally { if (button) button.disabled = false; } }
async function refreshList() {
  const view = $("view").value, request = ++listRequest;
  // Open threads feed every tab's badges, including while browsing a Closed view.
  const [open, closed, organization, views] = await Promise.all([api("threads?archived=false"),
    view === "archived" ? api("threads?archived=true") : null, api("organization"), api("views")]);
  if (request !== listRequest || view !== $("view").value) return;
  state.sessions = (closed || open).data;
  state.viewThreads = open.data;
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
function threadCard(thread, compact = false) {
  const needsInput = threadGrouping.awaitsUser(thread);
  const {key: status, label: statusLabel} = threadGrouping.status(thread);
  const button = node("button", null, "session" + (compact ? " compact" : "") + (status === "running" ? " active-thread" : "") + (needsInput ? " needs-attention" : "") + (state.selected === thread.thread_id ? " selected" : ""));
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
  } else {
    const top = node("div", null, "card-top");
    const badge = node("span", null, status === "running" ? "sr-only" : "status-tag " + status);
    if (status !== "running") badge.append(node("span", "", "dot " + status));
    badge.append(node("span", statusLabel));
    top.append(project, badge);
    title.append(node("span", thread.title));
    const summary = thread.checkpoint?.summary || thread.original_request || "Ready for your first message.";
    const snippet = node("p", summary, "thread-snippet"); snippet.title = summary;
    const meta = node("div", null, "card-meta");
    meta.append(node("span", thread.checkpoint?.phase || "Getting started", "phase-label"));
    if (thread.attention === "later") meta.append(node("span", "Later", "placement-label"));
    if (thread.unread) meta.append(node("span", thread.response_state === "completed" ? "NEW RESULT" : "NEW", "unread-mark"));
    if (thread.links.length) meta.append(node("span", thread.links.length + (thread.links.length === 1 ? " link" : " links")));
    const time = node("span", relativeTime(thread.updated_at), "card-time");
    if (thread.updated_at) time.title = new Date(thread.updated_at * 1000).toLocaleString();
    meta.append(time);
    button.append(top, title, snippet, meta);
    if (thread.checkpoint?.next_action) {
      const next = node("div", null, "next-action"), copy = node("span", null, "next-copy");
      const actor = {user: "Your next step", agent: "Up next", external: "Waiting on"}[thread.checkpoint.next_actor] || "Next";
      copy.append(node("span", actor + " · ", "next-label"), document.createTextNode(thread.checkpoint.next_action));
      next.append(node("span", "↳"), copy); next.title = thread.checkpoint.next_action; button.append(next);
    }
    if (thread.checkpoint_stale) button.append(node("small", "New activity since this checkpoint", "stale-checkpoint"));
  }
  button.onclick = () => action(() => select(thread.thread_id));
  if (!compact && thread.attention !== "archived") {
    const card = node("div", null, "thread-card");
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
    return card;
  }
  return button;
}
function groupHeading(label, count) {
  const heading = node("div", null, "group-heading");
  heading.append(node("h2", label), node("span", String(count).padStart(2, "0"), "group-count"));
  return heading;
}
function associationLabel(thread) {
  return [thread.repository_name ? "Repo: " + thread.repository_name : "",
    thread.project_name ? "Project: " + thread.project_name : ""].filter(Boolean).join(" · ") || "No repository · No project";
}
function renderList() {
  savedViews?.render(state.viewThreads || state.sessions);
  const query = $("search").value.trim().toLocaleLowerCase();
  const projectThreads = state.sessions.filter(t => threadOrganization.matches(t, $("repository").value, $("project").value));
  const threads = projectThreads.filter(t => threadViews.matches(t,
    {show: $("view").value, phase: $("phase-filter").value, search: query}));
  const archived = $("view").value === "archived";
  const groupBy = $("group-by").value;
  const signature = JSON.stringify([threads, state.selected, [...state.pinning], archived, query, $("phase-filter").value, $("view").value, $("repository").value, $("project").value, groupBy, Math.floor(Date.now() / 60000)]);
  $("home").setAttribute("aria-current", state.selected ? "false" : "page");
  $("thread-count").textContent = String(threads.length);
  const repositories = new Set(threads.map(t => t.repository_id).filter(Boolean)).size;
  const projects = new Set(threads.map(t => t.project_id).filter(Boolean)).size;
  $("results-count").textContent = threads.length + (threads.length === 1 ? " thread" : " threads") +
    " · " + repositories + (repositories === 1 ? " repository" : " repositories") + " · " + projects + (projects === 1 ? " project" : " projects");
  if (signature === state.listSignature) { renderStatus(); return; }
  state.listSignature = signature;
  const focused = document.activeElement?.dataset.thread;
  const focusAction = document.activeElement?.dataset.action;
  const focusGroup = document.activeElement?.closest("[data-group]")?.dataset.group;
  const focusArea = document.activeElement?.closest("#sessions") ? $("sessions") : $("overview");
  $("sessions").replaceChildren(); $("overview").replaceChildren();
  const groups = threadGrouping.groupThreads(threads);
  const filtered = Boolean(query || $("phase-filter").value || ["attention", "completed"].includes($("view").value) || $("repository").value || $("project").value);
  function addGroup(key, label, items, parent, description = "", emptyText = "") {
    const section = node("section", null, "thread-group " + key), overview = node("section", null, "thread-group " + key);
    section.setAttribute("aria-label", label); overview.setAttribute("aria-label", label);
    overview.dataset.group = key;
    section.append(groupHeading(label, items.length));
    overview.append(groupHeading(label, items.length));
    if (description) overview.append(node("p", description, "group-description"));
    const cards = node("div", null, "thread-cards");
    const subgroups = ["pinned", "priority", "completed", "later"].includes(key) && ["repository", "project"].includes(groupBy)
      ? threadOrganization.groupThreads(items, groupBy) : [{threads: items}];
    for (const group of subgroups) {
      if (group.label) {
        const heading = node("h3", group.label, "association-heading"); heading.title = group.detail;
        const compactHeading = node("h3", group.label, "association-heading"); compactHeading.title = group.detail;
        cards.append(heading); section.append(compactHeading);
      }
      for (const thread of group.threads) { section.append(threadCard(thread, true)); cards.append(threadCard(thread)); }
    }
    if (!items.length && emptyText) cards.append(node("p", emptyText, "group-empty"));
    overview.append(cards);
    if (items.length) $("sessions").append(section);
    parent.append(overview);
  }
  if (!archived) {
    const focus = node("section", null, "focus-board");
    focus.setAttribute("aria-label", groups.pinned.length ? "Pinned and your turn" : "Your turn overview");
    if (groups.pinned.length) addGroup("pinned", "Pinned", groups.pinned, focus, "Keep threads here as work progresses.");
    addGroup("priority", "Your turn", groups.priority, focus, "Replies and requests waiting for you.",
      filtered ? "No threads waiting for you match your filters." : "No threads waiting for you.");
    $("overview").append(focus);
  }
  if (groups.completed.length) addGroup("completed", "Completed", groups.completed, $("overview"), "Finished tasks. New results appear first; no reply is needed.");
  if (groups.phases.length) {
    const board = node("div", null, groupBy === "phase" ? "phase-board" : "organization-board");
    const active = threads.filter(thread => !threadGrouping.awaitsUser(thread) && thread.attention !== "later" &&
      !groups.completed.includes(thread) && !groups.pinned.includes(thread));
    const organized = groupBy === "phase" ? groups.phases : threadOrganization.groupThreads(active, groupBy);
    for (const group of organized) addGroup(group.key, group.label, group.threads, board, group.detail || "");
    $("overview").append(board);
  }
  if (groups.later.length) addGroup("later", "Later", groups.later, $("overview"), "Saved for another day.");
  if (!threads.length) {
    $("sessions").append(node("p", filtered ? "No matching threads" : "Nothing here yet", "empty"));
    const empty = node("div", null, "empty-state");
    const emptyProject = !query && !$("phase-filter").value && !$("repository").value && $("view").value === "active" && !projectThreads.length
      && state.organization.projects.find(project => project.id === $("project").value);
    const title = emptyProject ? "No open threads in " + emptyProject.name : filtered ? "No matching threads" : archived ? "No closed threads yet" : "Start with an idea";
    const copy = emptyProject ? "Start a new session here, or browse existing threads and choose Add to project." : filtered ? "Try another search or clear your filters." : archived ? "Closed threads stay here. Reopen one whenever you want to continue." : "Open a session and give your next project a place to begin.";
    empty.append(node("h3", title), node("p", copy));
    if (filtered || !archived) {
      const button = node("button", emptyProject ? "Browse existing threads" : filtered ? "Clear filters" : "New session ↗", "quiet");
      button.onclick = () => action(filtered ? async () => { $("search").value = ""; $("phase-filter").value = ""; $("repository").value = ""; $("project").value = ""; if (!archived) $("view").value = "active"; await refreshList(); } : newSession);
      empty.append(button);
    }
    if (emptyProject) {
      const button = node("button", "New session", "primary");
      button.onclick = () => action(newSession);
      empty.append(button);
    }
    $("overview").append(empty);
  }
  if (focused) {
    const candidates = [...focusArea.querySelectorAll("[data-thread]")].filter(el => el.dataset.thread === focused && el.dataset.action === focusAction);
    const target = candidates.find(el => el.closest("[data-group]")?.dataset.group === focusGroup) || candidates[0];
    target?.focus({preventScroll: true});
  }
  renderStatus();
}
async function select(id) {
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
  history.replaceState(null, "", "#" + encodeURIComponent(id));
  $("welcome").hidden = true;
  $("conversation").hidden = false;
  $("timeline").replaceChildren(node("p", "Opening session…", "empty"));
  $("requests").replaceChildren();
  $("thread-context").hidden = true;
  renderTitle();
  $("page-location").textContent = "Thread";
  delete $("requests").dataset.signature;
  renderList();
  await refreshDetail();
  if (state.selected !== id) return;
  const displayed = state.detail.work_thread;
  await api(threadPath(id), {seen: true, seen_checkpoint_id: displayed.checkpoint?.id || 0,
    seen_completion_id: displayed.turn_completion?.id || 0});
  await refreshList();
  if (state.detail?.work_thread.browser_session) $("message").focus();
  else $("session-name").focus();
}
async function refreshDetail() {
  const id = state.selected;
  if (!id) return;
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
  if (!id || !state.detail) return;
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
  $("session-model").textContent = "Model · " + (session?.model || "Codex default");
  $("session-effort").textContent = "Reasoning · " + (session?.effort || "Model default");
  $("session-yolo").hidden = !session?.yolo;
  $("edit-permissions").disabled = !browserSession || !!state.detail?.running || state.detail?.work_thread?.attention === "archived" || state.busy || state.closing.has(state.selected);
  $("command-feedback").textContent = state.commandFeedback.get(state.selected) || "";
  $("command-feedback").hidden = !browserSession || !state.commandFeedback.has(state.selected);
}
function openSessionPermissions() {
  const detail = state.detail, work = detail?.work_thread;
  if (!work?.browser_session || detail.running || work.attention === "archived" || state.busy || state.closing.has(state.selected)) return;
  const dialog = $("permissions-dialog");
  dialog.dataset.threadId = state.selected;
  $("permissions-thread").textContent = work.title || detail.session?.name || "This session";
  $("edit-yolo").checked = !!detail.session?.yolo;
  $("permissions-error").hidden = true;
  dialog.showModal();
}
async function saveSessionPermissions() {
  const dialog = $("permissions-dialog"), id = dialog.dataset.threadId;
  const session = await api(sessionPath(id), {yolo: $("edit-yolo").checked});
  if (id === state.selected && state.detail) { state.detail.session = session; renderStatus(); }
  dialog.close();
  await refreshList();
}
function renderStatus() {
  renderSessionSettings();
  const running = !!state.detail?.running;
  const work = state.detail?.work_thread;
  const archived = work?.attention === "archived";
  const session = state.sessions.find(s => s.thread_id === state.selected);
  const thread = session || work;
  const active = running || (thread && threadGrouping.status(thread).key === "running");
  $("status").textContent = state.detail?.requests?.length ? "Needs input" : active ? "" :
    thread && thread.response_state !== "reply" ? threadGrouping.status(thread).label : "";
  $("status").hidden = !$("status").textContent;
  $("composer").hidden = !work?.browser_session;
  $("send").disabled = !state.detail || !work?.browser_session || (running && !state.detail.activeTurn) || archived || state.busy || !!state.attachments?.pending() || state.closing.has(state.selected);
  $("send").textContent = running ? "Steer ↑" : "Send ↑";
  $("queue").hidden = !running;
  $("queue").disabled = $("send").disabled;
  $("message").disabled = archived;
  $("stop").hidden = !running;
  $("close-thread").textContent = archived ? "Reopen" : running || ["running", "needs input"].includes(work?.status) ? "Stop and close" : "Close thread";
  $("close-thread").disabled = !state.detail || state.closing.has(state.selected);
  $("park").disabled = !state.detail || archived || state.closing.has(state.selected);
  $("edit-checkpoint").disabled = !state.detail;
  $("add-link").disabled = !state.detail;
  $("session-name").disabled = !state.detail;
  $("organize-thread").disabled = !state.detail;
  $("composer-hint").textContent = archived ? "Reopen this thread to continue." : running ? "Enter to steer · Tab to queue after this turn · Shift + Enter for a new line" : "Enter to send · Shift + Enter for a new line · /model · /effort · /help";
  state.attachments?.render();
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
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
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
        message.append(node("span", item.type === "userMessage" ? "You" : "Codex", "speaker"), body);
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
  if (atBottom) el.scrollTop = el.scrollHeight;
}
function requestButton(label, decision, request, form) {
  const button = node("button", label, decision === "accept" ? "primary" : "quiet"); button.type = "button";
  button.onclick = () => action(async () => {
    const body = {decision};
    if (form) Object.assign(body, form());
    await api(sessionPath(state.selected) + "/requests/" + encodeURIComponent(request.key), body);
    await refreshDetail(); await refreshList();
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
      box.append(node("h3", "Codex has a question"));
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
      box.append(node("h3", "Codex is requesting approval"));
      if (params.reason) box.append(node("p", params.reason));
      box.append(node("pre", params.command || (params.permissions || params.networkApprovalContext ? JSON.stringify(params.permissions || params.networkApprovalContext, null, 2) : params.grantRoot || "Apply the file changes shown above.")));
      const choices = params.availableDecisions || (request.method === "item/permissions/requestApproval" ? ["accept", "decline"] : ["accept", "acceptForSession", "decline", "cancel"]);
      const labels = {accept: "Allow once", acceptForSession: "Allow for session", decline: "Decline", cancel: "Cancel"};
      for (const decision of choices) if (typeof decision === "string" && labels[decision]) actions.append(requestButton(labels[decision], decision, request));
    } else {
      box.append(node("h3", "This request is not supported yet"), node("p", "Cancel this request so Codex can continue."));
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
    // Completions apply to every thread, including when this tab is hidden.
    notifications.receive(batch.completions || []);
    if (batch.completions?.length) { scheduleList(); if (state.selected) scheduleDetail(); }
    if (batch.reset) scheduleDetail();
    for (const event of batch.events) {
      if (event.method === "bridge/disconnected") { showError(new Error("Codex disconnected. Restart Agent Coord to reopen saved sessions.")); state.detail && (state.detail.running = false); renderStatus(); }
      else applyEvent(event);
      if (!event.method.endsWith("/delta") && !event.method.endsWith("/outputDelta")) scheduleList();
    }
  };
  window.addEventListener("pagehide", () => events.close(), {once: true});
}
function efforts() {
  $("effort").replaceChildren(new Option("Model default", ""));
  for (const item of state.models.find(m => m.model === $("model").value)?.supportedReasoningEfforts || []) $("effort").append(new Option(item.reasoningEffort, item.reasoningEffort));
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
for (const prefix of ["new", "edit"]) {
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
async function newSession() {
  $("new-yolo").checked = false;
  const workspaces = (await api("workspaces")).data;
  $("workspace-choice").replaceChildren();
  for (const workspace of workspaces) $("workspace-choice").append(new Option(workspace.name + " · " + workspace.cwd, workspace.cwd));
  $("workspace-choice").append(new Option("Enter another path…", ""));
  $("workspace-choice").value = workspaces.some(workspace => workspace.cwd === state.config.cwd) ? state.config.cwd : "";
  $("new-cwd").value = state.config.cwd;
  $("workspace-path-label").hidden = Boolean($("workspace-choice").value);
  $("workspace-hint").textContent = state.config.workspaceRoot ? "Choose a folder within " + state.config.workspaceRoot + "." : "Choose a folder or enter a workspace path.";
  await fillAssignments("new");
  const repository = state.organization.repositories.find(item => item.id === $("new-repository").value);
  if (repository && workspaces.some(workspace => workspace.cwd === repository.root)) {
    $("workspace-choice").value = repository.root; $("new-cwd").value = repository.root;
    $("workspace-path-label").hidden = true;
  }
  $("create-dialog").showModal(); $("new-name").focus();
  state.models = (await api("models")).data;
  $("model").replaceChildren(new Option("Codex default", ""));
  for (const model of state.models) $("model").append(new Option(model.displayName || model.model, model.model));
  efforts();
}
$("new-session").onclick = $("welcome-new").onclick = () => action(newSession);
$("close-dialog").onclick = () => $("create-dialog").close();
$("workspace-choice").onchange = () => {
  const cwd = $("workspace-choice").value;
  $("workspace-path-label").hidden = Boolean(cwd);
  if (cwd) $("new-cwd").value = cwd;
  else $("new-cwd").focus();
};
$("model").onchange = efforts;
$("view").onchange = () => action(refreshList);
$("repository").onchange = $("project").onchange = $("phase-filter").onchange = renderList;
try { const saved = localStorage.getItem("agent-coord-group-by"); if (["phase", "repository", "project", "none"].includes(saved)) $("group-by").value = saved; } catch (_) { /* Preferences are optional. */ }
$("group-by").onchange = () => {
  if (!savedViews || savedViews.activeId === "all") {
    try { localStorage.setItem("agent-coord-group-by", $("group-by").value); } catch (_) { /* Preferences are optional. */ }
  }
  renderList();
};
$("search").oninput = renderList;
function goHome() {
  if (state.selected) state.drafts.set(state.selected, $("message").value);
  state.selected = null; state.detail = null; state.titleEdit = null; history.replaceState(null, "", location.pathname + location.search);
  state.attachments?.highlight(false);
  notifications?.syncFocus();
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
function layoutContext() {
  const context = $("thread-context");
  const containsFocus = context.contains(document.activeElement);
  context.open = !compactContext.matches;
  if (!context.open && containsFocus) context.querySelector("summary").focus({preventScroll: true});
}
compactContext.addEventListener("change", layoutContext);
layoutContext();
document.addEventListener("keydown", event => {
  if (document.querySelector("dialog[open]")) return;
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
  if (event.key === "Escape") { $("search").value = ""; renderList(); }
};
$("create-form").onsubmit = event => { event.preventDefault(); action(async () => {
  const associations = await assignmentPayload("new");
  const result = await api("sessions", {name: $("new-name").value, cwd: $("new-cwd").value, model: $("model").value, effort: $("effort").value, yolo: $("new-yolo").checked, ...associations});
  $("create-dialog").close(); $("new-name").value = "";
  await refreshList(); await select(result.session.thread_id);
}, $("create")); };
async function sendMessage(mode = "steer") {
  const detail = state.detail;
  const id = state.selected, text = $("message").value;
  const images = state.attachments?.snapshot(id) || [];
  if (state.busy || !detail?.work_thread?.browser_session || detail.work_thread.attention === "archived" || state.closing?.has(state.selected) ||
      (detail.running && !detail.activeTurn) || state.attachments?.pending(id) || (!text.trim() && !images.length)) return;
  const body = {message: text};
  if (images.length) body.images = images.map(({name, url}) => ({name, url}));
  if (detail.running && mode !== "queue") body.expectedTurnId = detail.activeTurn;
  state.busy = true; renderStatus();
  try {
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
  }
  finally { state.busy = false; renderStatus(); }
}
$("composer").onsubmit = event => { event.preventDefault(); action(sendMessage); };
function composerKeydown(event) {
  if (event.isComposing || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
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
async function toggleThreadClosed(thread = state.detail?.work_thread) {
  const id = thread?.thread_id || state.selected;
  if (!id || !thread || state.closing.has(id)) return;
  const reopening = thread.attention === "archived";
  state.closing.add(id); renderStatus();
  try {
    await api(threadPath(id) + (reopening ? "/reopen" : "/close"), {});
    if (state.selected === id) {
      $("view").value = "active";
      if (reopening) await refreshDetail();
      else { state.sessions = state.sessions.filter(thread => thread.thread_id !== id); goHome(); }
    }
    await refreshList();
  } finally { state.closing.delete(id); renderStatus(); }
}
$("close-thread").onclick = () => action(toggleThreadClosed, $("close-thread"));
$("park").onclick = () => action(async () => {
  await api(threadPath(state.selected), {attention: state.detail.work_thread.attention === "later" ? "now" : "later"});
  await refreshThread(); await refreshList();
}, $("park"));
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
async function boot() {
  state.config = await api("config");
  state.attachments = new ChatImageAttachments({document, getThread: () => state.selected,
    canAttach: () => !!state.detail?.work_thread?.browser_session && state.detail.work_thread.attention !== "archived" && !state.closing.has(state.selected),
    onChange: renderStatus, onError: showError});
  state.attachments.bind();
  notifications = new TurnNotifications({
    button: $("notifications"),
    claim: async completion_id => (await api("notifications/claim", {completion_id})).claimed,
    selected: () => state.selected,
    openThread: select,
    onError: showError,
  });
  $("monitor").href = "/monitor" + location.search;
  let windowStorage, preferences;
  try { windowStorage = sessionStorage; preferences = localStorage; } catch { /* Views work without browser storage. */ }
  savedViews = new SavedViews({document, api, storage: windowStorage, preferences, scope: state.config.workspaceRoot,
    onSwitch: async () => { goHome(); await refreshList(); }, onError: showError});
  await savedViews.start();
  await refreshList(); connect();
  savedViews.restoreScroll();
  const selected = decodeURIComponent(location.hash.slice(1));
  if (selected) await select(selected);
  // Checkpoints written by terminal agents arrive through the shared database.
  const metadataTimer = setInterval(() => {
    if (!document.hidden) { refreshList().catch(showError); refreshThread().catch(showError); }
  }, 5000);
  window.addEventListener("pagehide", () => clearInterval(metadataTimer), {once: true});
  window.addEventListener("pagehide", () => savedViews.remember());
}
boot().catch(showError);
