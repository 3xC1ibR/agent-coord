"use strict";
const $ = id => document.getElementById(id);
const state = {config: null, sessions: [], organization: {repositories: [], projects: []}, selected: null, detail: null, drafts: new Map(), closing: new Set(), pinning: new Set(), updatingThreads: new Set(), groupExpansion: new Map(), commandFeedback: new Map(), listSignature: ""};
window.HTMLPreview?.bind(document, () => state.selected);
state.threadActions = createThreadActions(document, window);
let notifications;
let savedViews;
let navigation;
let conversationSearch;
state.sourceWindowId = window.agentCoordDesktop?.windowId || crypto.randomUUID();
state.paneMode = false;
if (state.paneMode) state.sourceWindowId = window.parent.agentCoordSourceWindowId || state.sourceWindowId;
window.agentCoordSourceWindowId = state.sourceWindowId;
window.agentCoordFocusMessage = focusAgentMessage;
window.agentCoordOpenMessage = openAgentMessage;
window.agentCoordFocusSearchMessage = focusSearchMessage;
let listRequest = 0;
const base = "/api/browser/";
const sessionPath = id => "sessions/" + encodeURIComponent(id);
const threadPath = id => "threads/" + encodeURIComponent(id);
function node(tag, text, className) { const el = document.createElement(tag); if (text != null) el.textContent = text; if (className) el.className = className; return el; }
const errorNotice = createTransientNotice($("error"), window);
function showError(error) {
  const message = error.message || String(error), dialogError = document.querySelector("dialog[open] .dialog-error");
  if (dialogError) { dialogError.hidden = false; dialogError.textContent = message; }
  else {
    const copy = $("error").querySelector("span");
    const changed = $("error").hidden || copy.textContent !== message;
    copy.textContent = message;
    if (changed) errorNotice.show(10000);
  }
}
$("error").querySelector("button").onclick = () => errorNotice.dismiss();
// requestOptions.entity, a {tag, value} object kept by the caller, validates a
// read: an unchanged resource answers 304 and resolves to the same value object.
async function api(path, body, requestOptions = {}) {
  const submission = body !== undefined && /^sessions\/([^/]+)\/(messages|queue)$/.exec(path);
  if (submission && !body.action) state.activity?.submission(decodeURIComponent(submission[1]), body);
  const viewMutation = body !== undefined && (path === "views" || path.startsWith("views/"));
  if (viewMutation) ++listRequest;
  const {entity, ...fetchOptions} = requestOptions;
  const options = {cache: "no-store", ...fetchOptions};
  if (submission && !body.action) options.priority = "high";
  if (entity?.tag && entity.value) options.headers = {...options.headers, "If-None-Match": entity.tag};
  if (body !== undefined) Object.assign(options, {method: "POST", headers: {...options.headers, "Content-Type": "application/json", "X-Agent-Coord-Token": state.config.token}, body: JSON.stringify(body)});
  let address = base + path;
  const visit = path === "activity/events" && body !== undefined && state.config?.interactionContinuity && state.config.visitTransport;
  if (visit && /^http:\/\/127\.0\.0\.1:\d+$/.test(visit.origin) && visit.token) {
    // Long source writes and pagehide keepalives cannot occupy a composer or
    // history connection. A visit token authorizes only this reporting route.
    address = visit.origin + base + path;
    options.headers["X-Agent-Coord-Token"] = visit.token;
    options.credentials = "omit";
    options.redirect = "error";
  }
  const read = body === undefined && (options.method || "GET").toUpperCase() === "GET";
  for (let attempt = 0; ; ++attempt) {
    let response, unreadable = false;
    try {
      // Several windows share the same loopback HTTP connection pool. Wait
      // for other windows' background visit writes before occupying a socket.
      // On page exit a keepalive fetch must be registered before the page dies.
      if (path === "activity/events" && state.config?.interactionContinuity && !options.keepalive && globalThis.navigator?.locks) {
        response = await navigator.locks.request("ribbon-field-activity-write", () => fetch(address, options));
      } else if (path === "config" && read && globalThis.agentCoordInitialConfig) {
        const initial = globalThis.agentCoordInitialConfig;
        delete globalThis.agentCoordInitialConfig;
        response = await initial || await fetch(address, options);
      } else response = await fetch(address, options);
      if (response.status === 401 && location.protocol === "https:") window.dispatchEvent(new Event("agent-coord-pairing-required"));
      if (response.status === 304 && entity?.value) return entity.value;
      let result, snapshotBytes;
      try {
        const historyRead = read && /^(sessions|conversations)\/[^/]+(?:$|\?|\/open$|\/turns\/)/.test(path);
        if (historyRead) snapshotBytes = Number(response.headers?.get("Content-Length"));
        if (historyRead && response.text && (!(snapshotBytes > 0) || response.headers?.get("Content-Encoding"))) {
          const text = await response.text();
          // Bound the uncompressed UTF-8 payload even when a proxy changes transfer encoding.
          snapshotBytes = 3 * text.length;
          result = JSON.parse(text);
        } else result = await response.json();
      }
      catch (error) {
        unreadable = true;
        console.warn("Unreadable API response", {path, status: response.status,
          contentType: response.headers?.get("Content-Type")}, error);
        const sending = body !== undefined && /^sessions\/[^/]+\/(messages|queue)$/.test(path) && !body.action;
        throw new Error(sending
          ? "Could not confirm whether the message was accepted. Your draft is still here; check the conversation before sending again."
          : body !== undefined
            ? "Could not confirm the change. Check the current state before trying again."
            : "Could not refresh the workspace: the server returned an unreadable response. Try again shortly.");
      }
      if (viewMutation) ++listRequest;
      if (!response.ok) throw Object.assign(new Error(result.error || "Request failed"), {status: response.status, code: result.code});
      if (response.headers?.get("X-Ribbon-Transfer-Read-Source") === "serving") result.transferReadSource = "serving";
      if (response.headers?.get("X-Ribbon-Untargeted-Input") === "1") result.acceptsUntargetedInput = true;
      if (read && result && typeof result === "object" && (result.thread || result.recent_turns || result.turn)) {
        if (snapshotBytes > 0) result.snapshotBytes = snapshotBytes;
      }
      if (entity) Object.assign(entity, {tag: response.headers?.get("ETag") || null, value: result});
      return result;
    } catch (error) {
      // A remote connection can briefly return a proxy page or an incomplete
      // body. Retry a safe read once; writes may already have been accepted.
      if (!read || attempt || options.signal?.aborted || error.name === "AbortError"
          || response?.status === 401
          || response && !unreadable && ![502, 503, 504].includes(response.status)) throw error;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
  }
}
async function openMentionFile(ref) {
  const result = await api("files/preview", {root: ref.root, path: ref.path});
  const dialog = node("dialog", null, "file-mention-preview");
  dialog.setAttribute("aria-label", "File preview " + ref.path);
  const close = node("button", "Close");close.type="button";close.onclick=()=>dialog.close();
  dialog.append(node("h2", ref.path), close, node("pre", result.binary ? "Preview unavailable for this file type." : result.text));
  if(result.truncated)dialog.append(node("p", "Preview truncated."));
  dialog.addEventListener("close", ()=>dialog.remove());document.body.append(dialog);dialog.showModal();
}
async function action(fn, button) { if (button) button.disabled = true; try { await fn(); } catch (error) { showError(error); } finally { if (button) button.disabled = false; } }
async function refreshList({background = false, afterMutation = false, priority = background ? "low" : "auto", initialDefinitions} = {}) {
  if (state.paneMode) { refreshDecisionsIfDue(background); window.parent.agentCoordPanes?.requestList({background, afterMutation}); return; }
  const view = $("view").value, pending = state.listRefresh;
  // Polls and streamed events must not keep superseding a slow inventory read.
  // Explicit refreshes after user actions still invalidate earlier snapshots.
  if (background && !afterMutation && pending?.view === view && pending.request === listRequest) return pending.promise;
  // Coalesce decisions and schedules with the inventory read. Every WebKit
  // fetch retains memory in the Networking process, so skipped refreshes
  // must not still issue their companion requests.
  refreshDecisionsIfDue(background);
  const refresh = {view, request: ++listRequest};
  state.listRefresh = refresh;
  refresh.promise = loadThreadList(view, refresh.request, {background, priority, initialDefinitions});
  try { return await refresh.promise; }
  finally { if (state.listRefresh === refresh) state.listRefresh = null; }
}
// Every WebKit fetch retains memory in the Networking process, so background
// refreshes read rarely changing companions on their own slower schedules.
const DECISIONS_REFRESH_INTERVAL = 15000, ORGANIZATION_REFRESH_INTERVAL = 60000;
function refreshDecisionsIfDue(background) {
  if (background && Date.now() - (state.decisionsReadAt || 0) < DECISIONS_REFRESH_INTERVAL) return;
  state.decisionsReadAt = Date.now();
  refreshDecisionsAndSchedules(background);
}
function refreshDecisionsAndSchedules(background = false) {
  state.schedules?.refresh({background}).catch(showError);
  state.asks?.refresh({background}).catch(showError);
}
async function loadThreadList(view, request, {background = false, priority = "auto", initialDefinitions} = {}) {
  const entities = state.inventoryEntities ||= {open: {}, closed: {}};
  // Groups, repositories and saved views change by explicit action; a
  // background refresh reads them at most once a minute.
  const companions = !background || !state.organization || Date.now() - (state.organizationReadAt || 0) >= ORGANIZATION_REFRESH_INTERVAL;
  // Open threads feed every tab's badges, including while browsing a Closed view.
  const [open, closed, organization, views] = await Promise.all([initialDefinitions?.open || api("threads?archived=false", undefined, {entity: entities.open, priority}),
    view === "archived" ? api("threads?archived=true", undefined, {entity: entities.closed, priority}) : null,
    companions ? initialDefinitions?.organization || api("organization", undefined, {priority}) : null,
    companions ? initialDefinitions?.views || api("views", undefined, {priority}) : null]);
  if (request !== listRequest || view !== $("view").value) return;
  if (companions) state.organizationReadAt = Date.now();
  // An unchanged list (304) needs no reprocessing or redraw; relative times
  // still advance twice a minute.
  if (background && !companions && open === state.inventoryOpen && (!closed || closed === state.inventoryClosed)) {
    if (Date.now() - (state.listRenderedAt || 0) >= 30000) { state.listRenderedAt = Date.now(); renderList(); }
    return;
  }
  state.inventoryRevision = (state.inventoryRevision || 0) + 1;
  state.inventoryOpen = open; state.inventoryClosed = closed;
  state.viewThreads = open.data.map(overlayPendingAttention);
  state.sessions = closed ? closed.data.map(overlayPendingAttention) : state.viewThreads;
  state.agentTreeController?.inventory(state.viewThreads, closed?.data);
  state.threadTabs?.update([...(state.conversationWindow ? state.sessions : state.viewThreads), ...(closed?.data || [])]);
  state.dispatchWorkspace?.update(state.viewThreads, state.detail);
  state.rollUp?.sync(state.viewThreads);
  if (organization) { state.organization = organization; if (state.detail) renderSessionSettings(); }
  for (const field of ["repository", "project"]) {
    const select = $(field), selected = select.value, choices = state.organization[field === "repository" ? "repositories" : "projects"]
      .filter(item => field === "repository" || item.cwd === state.folders?.selected?.cwd);
    const signature = JSON.stringify(choices);
    if (select.dataset.signature === signature) continue;
    select.dataset.signature = signature;
    select.replaceChildren(new Option(field === "repository" ? "All repositories" : "All groups", ""),
      new Option(field === "repository" ? "No repository" : "No group", threadOrganization.NONE));
    const nameCounts = new Map();
    for (const item of choices) nameCounts.set(item.name, (nameCounts.get(item.name) || 0) + 1);
    for (const item of choices) {
      const duplicates = nameCounts.get(item.name) > 1;
      const option = new Option(item.name + (duplicates && item.root ? " · " + item.root : ""), item.id);
      option.title = item.root || item.name; select.append(option);
    }
    if (selected && ![...select.options].some(option => option.value === selected)) {
      select.append(new Option("Unavailable " + (field === "project" ? "group" : field), selected));
    }
    select.value = selected;
  }
  if (views) savedViews?.sync(views.data, state.organization.projects);
  renderFolders();
  if (view !== $("view").value) return refreshList();
  state.listRenderedAt = Date.now();
  renderList();
}
function currentProjectDispatcher() {
  const cwd = state.folders?.selected?.cwd;
  return DispatchUI.chooseDispatcher(state.viewThreads || state.sessions, cwd, state.dispatchDock?.preference(cwd).id);
}
function syncDispatcherOverview() {
  if (state.paneMode || !state.dispatchDock) return;
  const thread = currentProjectDispatcher(), cwd = state.folders?.selected?.cwd;
  const button = $("start-dispatcher");
  button.textContent = state.dispatchStarting ? "Starting dispatcher…" : thread ? "Open dispatcher ↗" : "Start dispatcher ↗";
  button.disabled = !state.dispatchReady || !!state.dispatchStarting || !cwd;
  button.title = cwd ? "Send work to the right conversation in this project" : "Open a project to start a dispatcher";
  // Dispatcher mode is an explicit destination; visiting the overview never
  // opens it implicitly or changes the user's saved overview filters.
}
async function startDispatcher() {
  if (!state.dispatchReady || state.dispatchStarting || !state.folders?.selected?.cwd) return;
  const cwd = state.folders.selected.cwd, revision = state.navigationRevision;
  state.dispatchStarting = true; syncDispatcherOverview();
  try {
    let thread = currentProjectDispatcher();
    if (!thread) {
      // Keep a successfully created identity if configuring it fails, so retrying
      // this action does not create another conversation.
      const pending = state.dispatcherCreates ||= new Map();
      let id = pending.get(cwd);
      if (!id) {
        const created = await api("sessions", {...sessionContext(), name: "Dispatcher", client: "codex", yolo: false});
        id = created.session.thread_id; pending.set(cwd, id);
      }
      await api(sessionPath(id) + "/dispatch", {enabled: true});
      thread = {thread_id: id}; pending.delete(cwd);
    }
    state.dispatchDock.remember(cwd, {id: thread.thread_id, hidden: false});
    // Creating a dispatcher must not pull the user back after navigation.
    if (cwd === state.folders.selected?.cwd && revision === state.navigationRevision) {
      await select(thread.thread_id, {dock: true});
    }
    await refreshList();
  } finally { state.dispatchStarting = false; syncDispatcherOverview(); }
}
$("start-dispatcher").onclick = () => action(startDispatcher);
$("dispatcher-hide").onclick = () => {
  state.dispatchDock.remember(state.projectCwd, {hidden: true}); goHome();
};
$("dispatcher-open").onclick = () => {
  if (state.dispatchWorkspace?.active) state.dispatchDock.expand(true);
  else action(() => select(state.selected));
};
$("dispatcher-permissions").onclick = event => openSessionPermissions(event);
function sessionContext() {
  return newThreadContext(state.organization, {project: $("project").value}, state.folders?.selected);
}
function threadsInProject(threads) { return threads.filter(thread => inProject(thread, state.folders?.selected)); }
async function switchProject({restoreConversation = true, preserveRollUp = false, awaitInventory = true, renderOverview = true} = {}) {
  const cwd = state.folders.selected?.cwd || "";
  if (state.projectCwd === cwd) { renderFolders(); return; }
  // Keep in-memory drafts and tiled destinations as well as the saved tab.
  const destinations = state.projectDestinations ||= new Map(), panes = globalThis.agentCoordPanes;
  destinations.set(state.projectCwd, {tab: state.threadTabs?.selected,
    tiled: !!panes?.active && !state.dispatchWorkspace?.active, returnTarget: panes?.layout?.returnTarget});
  goHome({restoreDispatcher: false, rememberTab: false, preserveRollUp, render: false});
  const drafts = state.projectDrafts ||= new Map();
  drafts.set(state.projectCwd, {detail: state.newSessionDraft, text: state.drafts.get("new-session")});
  const draft = drafts.get(cwd);
  state.newSessionDraft = draft?.detail || null;
  state.drafts.set("new-session", draft?.text || "");
  state.projectCwd = cwd;
  state.projectScratchpad?.setProject(state.folders.selected);
  state.threadTabs?.setProject(cwd);
  state.dispatchRestore = true;
  // Restore the visible destination before any network request. Cached history,
  // drafts and reading positions are already available in this window.
  const viewsReady = savedViews.setProject(cwd).then(() => {
    if (state.projectCwd === cwd && renderOverview) renderList();
  });
  let opening;
  if (restoreConversation) {
    const destination = destinations.get(cwd);
    if (destination?.tiled) {
      const revision = state.navigationRevision;
      opening = viewsReady.then(() => {
        // Pane layouts belong to a saved view, so use the destination's filters.
        if (state.projectCwd !== cwd || state.navigationRevision !== revision) return;
        panes?.restoreView();
        if (panes?.active) panes.layout.returnTarget = destination.returnTarget;
        navigation?.remember();
      });
    } else opening = restoreProjectTab(destination ? destination.tab : state.threadTabs?.destination(), {refreshInventory: false});
  }
  renderFolders(); navigation?.remember();
  await Promise.all([viewsReady, opening]);
  if (state.projectCwd !== cwd) return;
  const inventory = refreshList({background: true}).then(() => {
    if (state.projectCwd === cwd && !state.selected) savedViews.restoreScroll();
  });
  if (awaitInventory) await inventory; else inventory.catch(error => showError(error));
}
async function restoreProjectTab(id = state.threadTabs?.destination(), {refreshInventory = true} = {}) {
  const cwd = state.projectCwd;
  if (id === "project-scratchpad") { openProjectScratchpad(); return; }
  if (!id || !state.threadTabs?.items.some(item => item.thread_id === id)
      || id === "new-session" && !state.newSessionDraft) return;
  const opening = select(state.threadTabs?.conversationFor?.(id) || id, {tabId: id, focus: false, stayInProject: true, refreshInventory});
  const revision = state.navigationRevision;
  try { await opening; }
  catch (error) {
    if (state.projectCwd !== cwd || state.navigationRevision !== revision) return;
    if (state.selected === id) goHome({restoreDispatcher: false});
    // Missing conversations fall back to Overview; temporary failures stay visible.
    if (error.status !== 404) throw error;
  }
}
async function openProjectDirectory(cwd, {preserveRollUp = false, forConversation = false} = {}) {
  if (!cwd || state.folders.selected?.cwd === cwd) return;
  // A saved conversation remains accessible if its directory was moved/deleted.
  // New session creation still validates the directory on the server.
  state.folders.adopt({cwd});
  // Opening a conversation in another project must not wait for that
  // project's thread list or draw its overview first; the conversation does.
  await switchProject({restoreConversation: false, preserveRollUp, awaitInventory: !forConversation, renderOverview: !forConversation});
}
function renderFolders() {
  const cwd = state.folders?.selected?.cwd || "";
  $("overview-workspace").textContent = cwd;
  $("overview-workspace").title = cwd;
  $("overview-workspace").hidden = !cwd;
  state.projectScratchpad?.setProject(state.folders?.selected);
  $("open-project-scratchpad").disabled = !state.projectScratchpad?.available;
  if (!state.folderPicker) return;
  let context;
  try { context = sessionContext(); } catch { /* Keep unavailable filters visible until the user changes them. */ }
  state.folderPicker.render(context, !state.folders.selected);
  state.projectTabs?.render(state.viewThreads || []);
  document.body.classList.toggle("no-project", !state.folders.selected);
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
async function toggleThreadQuiet(thread) {
  // Quiet threads notify and enter Attention only for failures and requests.
  const id = thread.thread_id;
  const updated = await api(threadPath(id), {quiet: !thread.quiet});
  state.sessions = state.sessions.map(item => item.thread_id === id ? {...item, quiet: updated.quiet} : item);
  if (state.detail?.work_thread.thread_id === id) state.detail.work_thread.quiet = updated.quiet;
  await refreshList();
}
async function updateThreadAttention(thread, body) {
  const id = thread.thread_id;
  if (state.updatingThreads.has(id)) return false;
  const patch = body.snoozed_until === undefined ? null : {snoozed_until: body.snoozed_until,
    attention: "later", snoozed: true, snooze_due: false, needs_attention: false, attention_reason: null};
  const current = state.sessions.find(item => item.thread_id === id) || thread;
  const previous = Object.fromEntries(Object.keys(patch || {}).map(key => [key, current[key]]));
  state.attentionMutations ||= new Map();
  const revision = (state.attentionMutations.get(id)?.revision || 0) + 1;
  state.attentionMutations.set(id, {revision, pending: patch, patch});
  ++listRequest; // Discard list snapshots requested before the user's action.
  if (patch) applyAttentionFields(id, patch);
  state.updatingThreads.add(id); renderList();
  try {
    const updated = await api(threadPath(id), body);
    const authoritative = patch ? Object.fromEntries(Object.keys(patch).map(key => [key, updated[key]])) : updated;
    if (patch) applyAttentionFields(id, authoritative);
    else {
      state.sessions = state.sessions.map(item => item.thread_id === id ? updated : item);
      if (state.detail?.work_thread.thread_id === id) { state.detail.work_thread = updated; renderThread(); }
      state.viewThreads = state.viewThreads?.map(item => item.thread_id === id ? updated : item);
    }
    state.attentionMutations.set(id, {revision: revision + 1, patch: authoritative});
    return true;
  } catch (error) {
    if (patch) applyAttentionFields(id, previous);
    state.attentionMutations.set(id, {revision: revision + 1, patch: previous});
    throw error;
  } finally {
    ++listRequest; // Pending reads cannot overwrite the reconciled result or rollback.
    state.updatingThreads.delete(id);
    if (state.snoozingThread?.thread_id === id) {
      state.snoozingThread = state.sessions.find(item => item.thread_id === id) || state.snoozingThread;
      $("save-snooze").disabled = false;
    }
    if (state.detail?.work_thread.thread_id === id) renderThread();
    renderList();
  }
}
function applyAttentionFields(id, fields) {
  for (const key of ["sessions", "viewThreads"]) {
    if (state[key]) state[key] = state[key].map(item => item.thread_id === id ? {...item, ...fields} : item);
  }
  if (state.detail?.work_thread.thread_id === id) {
    Object.assign(state.detail.work_thread, fields); renderThread();
  }
}
function overlayPendingAttention(thread) {
  const patch = state.attentionMutations?.get(thread.thread_id)?.pending;
  return patch ? {...thread, ...patch} : thread;
}
function reconcileAttentionSnapshot(thread, revision) {
  const mutation = state.attentionMutations?.get(thread.thread_id);
  const patch = mutation?.pending || (mutation && mutation.revision !== revision ? mutation.patch : null);
  return patch ? {...thread, ...patch} : thread;
}
async function markThreadHandled(thread) {
  if (!thread.can_handle_response || state.updatingThreads.has(thread.thread_id)) return;
  const ticket = state.rollUp?.ticket(thread.thread_id);
  if (await updateThreadAttention(thread, {handled: true,
    handled_checkpoint_id: thread.checkpoint?.id || 0, handled_completion_id: thread.turn_completion?.id || 0}) === false) return;
  state.threadActions?.notify("dismiss:" + thread.thread_id,
    "Update dismissed · Thread remains in " + (thread.attention === "later" ? "Later" : "Now"));
  await state.rollUp?.responded(ticket);
  await refreshList();
}
// The inbox's Mark read button, its row menu and the Working set share one read receipt.
async function markThreadRead(thread) {
  await api(threadPath(thread.thread_id), {seen: true, seen_checkpoint_id: thread.checkpoint?.id || 0,
    seen_completion_id: thread.turn_completion?.id || 0});
  await refreshList();
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
  return thread.snooze_due ? "Reminder" : thread.snoozed ? "Until " + new Date(thread.snoozed_until * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  }) : "";
}
function openThreadSnooze(thread) {
  state.snoozeDialogRevision = (state.snoozeDialogRevision || 0) + 1;
  state.snoozingThread = thread;
  $("snooze-form").reset(); $("snooze-custom-label").hidden = true; $("snooze-custom").required = false;
  $("snooze-error").hidden = true;
  $("save-snooze").disabled = state.updatingThreads.has(thread.thread_id);
  $("snooze-thread").textContent = thread.title;
  $("snooze-duration").value = "none";
  $("snooze-policy").textContent = "New results can appear in Attention. The thread stays in Later and the agent can keep working.";
  if (thread.snoozed_until > Date.now() / 1000) {
    const date = new Date(thread.snoozed_until * 1000);
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    $("snooze-duration").value = "custom";
    $("snooze-custom").value = local.toISOString().slice(0, 16);
    $("snooze-custom-label").hidden = false; $("snooze-custom").required = true;
    $("snooze-policy").textContent = "Bring back to Attention at this time. Notifications stay quiet until then; the agent can keep working.";
  }
  $("snooze-dialog").showModal(); $("snooze-duration").focus();
}
async function snoozeThread(thread, deadline) {
  const ticket = state.rollUp?.ticket(thread.thread_id);
  if (await updateThreadAttention(thread, {snoozed_until: deadline}) !== false) await state.rollUp?.responded(ticket);
}
async function resumeThreadSnooze(thread) {
  const ticket = state.rollUp?.ticket(thread.thread_id);
  if (await updateThreadAttention(thread, {resume_snooze: thread.snoozed_until}) === false) return;
  await state.rollUp?.responded(ticket);
  await refreshList();
}
function threadCard(thread, compact = false, inAttention = false) {
  const reason = threadGrouping.reason(thread);
  const acknowledged = threadGrouping.acknowledgedRequest(thread);
  const needsInput = threadGrouping.awaitsUser(thread) && ["blocked", "review"].includes(reason.key);
  const {key: status, label: statusLabel} = threadGrouping.status(thread);
  const button = node("button", null, "session" + (compact ? " compact" : "") + (status === "running" ? " active-thread" : "") + (needsInput ? " needs-attention" : "") + (state.selected === thread.thread_id ? " selected" : ""));
  if (!compact && !inAttention) button.className += " card-age-" + threadGrouping.cardAge(thread);
  const hasResponse = Boolean(thread.turn_completion && thread.turn_completion.status !== "interrupted");
  if (hasResponse) {
    button.className += thread.unread_result ? " response-unread" : " response-read";
    button.setAttribute("aria-description", thread.unread_result ? "Unread response" : "Read response");
  }
  button.dataset.thread = thread.thread_id;
  button.title = thread.title;
  button.setAttribute("aria-current", state.selected === thread.thread_id ? "true" : "false");
  const title = node("div", null, "session-title");
  if (compact) {
    if (status === "running") title.append(node("span", statusLabel, "sr-only"));
    else {
      const dot = node("span", "", "dot " + status); dot.setAttribute("aria-label", statusLabel);
      title.append(dot);
    }
    title.append(node("span", thread.title));

    button.append(title);
    if (thread.creation_mode === "orchestration" && thread.cwd) {
      const folder = node("small", thread.cwd.split("/").filter(Boolean).pop() || "/", "placement-label");
      folder.title = "Working folder: " + thread.cwd;
      button.append(folder);
    }
    if (snoozeLabel(thread)) button.append(node("small", snoozeLabel(thread), "placement-label"));
  } else {
    title.append(node("span", thread.title));
    const failed = status !== "running" && (thread.response_state === "failed" || thread.turn_completion?.status === "failed");
    const stateLabel = failed ? "Failed" : (inAttention && status !== "running" && !acknowledged ? reason.label : statusLabel).replace(/^✓\s*/, "");
    title.append(node("span", stateLabel, status === "running" ? "sr-only" : "card-state " + (failed ? "failed" : needsInput ? "waiting" : status)));
    const progress = thread.checkpoint?.summary || thread.original_request || "Ready for your first message.";
    // The name identifies the conversation; this saved update describes its latest work.
    const outcome = status === "running" ? "" : thread.turn_completion?.status === "failed" ? "Failed: " :
      thread.turn_completion?.status === "interrupted" ? "Stopped: " : thread.work_phase === "finished" ? "Completed: " : "";
    const summary = outcome + progress;
    const snippet = node("p", summary, "thread-snippet work-summary"); snippet.title = summary;
    const meta = node("div", null, "card-meta");
    const logo = globalThis.providerLogo?.(thread.client, {codex: "OpenAI · Codex", claude: "Anthropic · Claude Code",
      muse: "Meta · Muse Code", kimi: "Moonshot · Kimi Code", antigravity: "Google · Antigravity"}[thread.client]);
    if (logo) meta.append(logo);
    if (thread.creation_mode === "orchestration" && thread.cwd) {
      const folder = node("span", thread.cwd.split("/").filter(Boolean).pop() || "/", "placement-label");
      folder.title = "Working folder: " + thread.cwd;
      meta.append(folder);
    }
    if (thread.attention === "later") meta.append(node("span", "Later", "placement-label"));
    if (snoozeLabel(thread)) meta.append(node("span", snoozeLabel(thread), "placement-label"));
    if (thread.unread && !thread.unread_result && !inAttention) meta.append(node("span", "New checkpoint", "unread-mark"));
    const activityAt = threadGrouping.activityAt(thread);
    const time = node("span", relativeTime(activityAt), "card-time");
    if (activityAt) time.title = "Last activity: " + new Date(activityAt * 1000).toLocaleString();
    meta.append(time);
    const content = inAttention ? node("div", null, "attention-copy") : button;
    if (inAttention) button.append(content);
    content.append(title, snippet);
    if (!thread.checkpoint_stale && thread.checkpoint?.next_action && !(acknowledged && thread.checkpoint.next_actor === "user")) {
      const next = node("div", null, "next-action"), copy = node("span", null, "next-copy");
      const actor = {user: "Your next step", agent: "Up next", external: "Waiting on"}[thread.checkpoint.next_actor] || "Next";
      copy.append(node("span", actor + " · ", "next-label"), document.createTextNode(thread.checkpoint.next_action));
      next.append(node("span", "↳"), copy); next.title = thread.checkpoint.next_action; content.append(next);
    }
    if (thread.checkpoint_stale) content.append(node("small", "New activity since this checkpoint", "stale-checkpoint"));
    content.append(meta);
  }
  if (thread.dispatcher) {
    const designation = node("span", "Dispatcher", "dispatcher-designation");
    (compact ? title : button.querySelector(".card-meta")).append(designation);
  }
  if (thread.role) {
    const designation = node("span", thread.role.name, "role-designation");
    designation.title = "Role: " + thread.role.name;
    (compact ? title : button.querySelector(".card-meta")).append(designation);
  }
  button.onclick = () => action(() => select(thread.attention_target_thread_id || thread.thread_id));
  if (!compact && thread.attention !== "archived") {
    button.draggable = true; button.dataset.dragThread = thread.thread_id;
    const card = node("div", null, "thread-card" + (inAttention ? " attention-card" : ""));
    if (hasResponse) card.className += thread.unread_result ? " response-unread" : " response-read";
    const close = node("button", "×", "card-close");
    const label = (["running", "needs input"].includes(thread.status) ? "Stop and close " : "Close ") + thread.title;
    close.type = "button";
    close.dataset.thread = thread.thread_id; close.dataset.action = "close";
    close.title = label;
    close.setAttribute("aria-label", label);
    close.disabled = state.closing.has(thread.thread_id);
    close.onclick = event => {
      event.stopPropagation();
      action(() => toggleThreadClosed(thread), close);
    };
    card.append(button);
    if (thread.created_by_thread_id) {
      card.className += " has-app-agent";
      card.append(appAgentIndicator(thread));
    }
    const actions = node("div", null, "card-actions");
    function addAction(label, name, callback, target = actions) {
      const control = node("button", label, "quiet");
      control.type = "button"; control.dataset.thread = thread.thread_id; control.dataset.action = name;
      control.setAttribute("aria-label", label + " · " + thread.title);
      control.disabled = state.closing.has(thread.thread_id) || state.updatingThreads.has(thread.thread_id);
      control.onclick = event => { event.stopPropagation(); action(callback); };
      target.append(control);
    }
    // Peek is shown only where hover is unavailable (phones and touch); see thread-hover.css.
    const peek = node("button", "Peek", "quiet card-peek");
    peek.type = "button"; peek.dataset.thread = thread.thread_id; peek.dataset.action = "peek";
    peek.setAttribute("aria-label", "Peek · " + thread.title);
    peek.onclick = event => { event.stopPropagation(); globalThis.threadHover?.open(peek, thread.thread_id); };
    actions.append(peek);
    const childCount = state.agentTreeController?.childCount(thread.thread_id) || 0;
    if (childCount) addAction("⑂ " + childCount + (childCount === 1 ? " agent" : " agents"), "agent-tree",
      () => state.agentTreeController.open(thread.thread_id));
    const endActions = node("div", null, "card-end-actions");
    if (threadGrouping.actionFor?.(thread) === "dismiss") addAction("Dismiss", "handle", () => markThreadHandled(thread), endActions);
    if (thread.snooze_due) addAction("Dismiss reminder", "resume-snooze", () => resumeThreadSnooze(thread), endActions);
    if (endActions.children.length) actions.append(endActions);
    const titleActions = node("div", null, "card-title-actions");
    const more = node("button", "…", "card-more");
    more.type = "button"; more.title = "Thread options";
    more.dataset.thread = thread.thread_id; more.dataset.action = "more";
    more.setAttribute("aria-label", "Thread options · " + thread.title);
    more.setAttribute("aria-haspopup", "menu"); more.setAttribute("aria-expanded", "false");
    more.disabled = state.closing.has(thread.thread_id) || state.updatingThreads.has(thread.thread_id);
    more.onclick = event => state.threadActions.showMenu(more, thread.title, threadMenuItems(thread), event);
    titleActions.append(more, close);
    card.append(titleActions);
    if (actions.children.length) card.append(actions);
    return card;
  }
  if (!compact && thread.created_by_thread_id) {
    const card = node("div", null, "thread-card has-app-agent");
    card.append(button, appAgentIndicator(thread));
    return card;
  }
  return button;
}
function threadMenuItems(thread, {inbox = false} = {}) {
  const items = [
    ...(thread.orchestration_workspaces?.includes(state.projectCwd)
      && (thread.project_cwd || thread.cwd) !== state.projectCwd
      ? [{label: "Open agent’s project", run: () => action(() => openProjectDirectory(thread.project_cwd || thread.cwd))}] : []),
    {label: thread.project_id ? "Change group" : "Add to group", run: () => action(() => organizeThread(thread))},
    {label: "Move to project…", disabled: !!threadMoveUnavailable(thread), run: () => openThreadMove(thread)},
    {label: thread.pinned ? "Unpin" : "Pin", run: () => action(() => toggleThreadPinned(thread))},
    {label: thread.quiet ? "Turn off quiet" : "Make quiet", run: () => action(() => toggleThreadQuiet(thread))},
    {label: thread.attention === "later" ? "Change return time…" : "Move to Later…", run: () => openThreadSnooze(thread)},
  ];
  if (["codex", "claude"].includes(thread.client)) items.unshift({label: "Fork", disabled: !thread.can_fork || state.forking,
    run: () => action(() => forkThread(undefined, thread.thread_id))});
  if (thread.browser_session && thread.attention !== "archived") items.push({label: thread.dispatcher ? "Stop using as dispatcher" : "Use as dispatcher", run: () => action(() => toggleDispatcher(thread))});
  if (thread.browser_session && thread.attention !== "archived") items.push({label: "Attach sidecar…", run: () => action(() => state.sidecars.open(thread))});
  if (thread.browser_session && thread.attention !== "archived") items.push({label: thread.role ? "Change role…" : "Assign role…", run: () => action(() => openRoleDialog(thread))});
  if (thread.attention !== "archived" && (thread.supervised_by_user || thread.supervisor_thread_id || thread.created_by_thread_id))
    items.push({label: "Change supervisor…", run: () => openSupervisorDialog(thread)});
  if (thread.attention === "later") items.unshift({label: "Move to Now", run: () => action(() => toggleThreadLater(thread))});
  if (inbox && thread.attention !== "archived") {
    if (threadGrouping.actionFor?.(thread) === "dismiss")
      items.unshift({label: "Dismiss from Attention", run: () => action(() => markThreadHandled(thread))});
  }
  return items;
}
function appAgentIndicator(thread) {
  const creator = thread.created_by || state.sessions.find(t => t.thread_id === thread.created_by_thread_id);
  const available = creator?.available ?? state.sessions.some(t => t.thread_id === thread.created_by_thread_id);
  const label = "App agent · Created by " + (creator?.title || "another agent") + (available ? "" : " (unavailable)");
  const control = node("button", null, "card-app-agent");
  control.type = "button";
  control.dataset.thread = thread.created_by_thread_id; control.dataset.action = "creator";
  control.setAttribute("aria-label", label);
  control.setAttribute("aria-disabled", String(!available));
  // A branch leading into a window distinguishes an app agent from a Git fork.
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [key, value] of Object.entries({viewBox: "0 0 20 20", fill: "none", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false"})) icon.setAttribute(key, value);
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M4 5.5v9m0-4.5h5M9 7h9M10.5 4.5h6A1.5 1.5 0 0 1 18 6v8a1.5 1.5 0 0 1-1.5 1.5h-6A1.5 1.5 0 0 1 9 14V6a1.5 1.5 0 0 1 1.5-1.5ZM5.5 4A1.5 1.5 0 1 1 4 2.5 1.5 1.5 0 0 1 5.5 4ZM5.5 16A1.5 1.5 0 1 1 4 14.5 1.5 1.5 0 0 1 5.5 16Z");
  icon.append(path);
  const tooltip = node("span", label, "card-app-agent-tooltip");
  tooltip.setAttribute("aria-hidden", "true");
  control.append(icon, tooltip);
  control.onclick = event => { event?.stopPropagation(); if (available) action(() => select(thread.created_by_thread_id)); };
  return control;
}
function groupHeading(label, tag = "div") {
  const heading = node(tag, null, "group-heading");
  heading.append(node("h2", label));
  return heading;
}
function associationLabel(thread) {
  return thread.project_name ? "Group: " + thread.project_name : "No group";
}
function renderClosedToggle() {
  const placement = $("view").value;
  const archived = placement === "archived";
  $("show-now").setAttribute("aria-pressed", String(!["later", "archived"].includes(placement)));
  $("show-later").setAttribute("aria-pressed", String(placement === "later"));
  $("show-closed").setAttribute("aria-pressed", String(archived));
  $("show-closed").title = archived ? "Return to open threads" : "Show closed threads";
}
function renderList() {
  globalThis.agentCoordSendWork?.syncOverview();
  if (state.paneMode) { renderStatus(); return; }
  if (state.dispatchDock) syncDispatcherOverview();
  renderClosedToggle();
  const workspaceThreads = threadsInProject(state.sessions).filter(t => !state.pendingCloses?.has(t.thread_id));
  const openThreads = state.viewThreads && state.viewThreads !== state.sessions ?
    threadsInProject(state.viewThreads).filter(t => !state.pendingCloses?.has(t.thread_id)) : workspaceThreads;
  savedViews?.render(openThreads);
  if (conversationSearch?.update()) {
    if ($("atlas-overview")) $("atlas-overview").hidden = true;
    if ($("working-set-overview")) $("working-set-overview").hidden = true;
    if ($("thread-list-overview")) $("thread-list-overview").hidden = true;
    renderStatus(); return;
  }
  const query = $("search").value.trim().toLocaleLowerCase();
  const project = $("project").value;
  const projectThreads = workspaceThreads.filter(t => threadOrganization.matches(t, "", project));
  const workingSet = state.overviewLayout?.mode === "working-set";
  const filters = {show: $("view").value, phase: $("phase-filter").value, search: query};
  const includeLater = workingSet && ["active", "completed"].includes(filters.show);
  const matchesView = t => threadViews.matches(includeLater && t.attention === "later" ? {...t, attention: "now"} : t, filters);
  const threads = projectThreads.filter(matchesView);
  // The map keeps neighboring groups available while the tabs own selection.
  const groupPages = workingSet || state.overviewLayout?.mode === "list";
  const workingThreads = groupPages && savedViews?.activeId.startsWith("group-") ? workspaceThreads.filter(matchesView) : threads;
  const upcoming = upcomingSchedules(query);
  const archived = $("view").value === "archived";
  const groupBy = $("group-by").value;
  $("group-by-domain").setAttribute("aria-pressed", String(groupBy === "domain"));
  const viewId = (state.projectCwd || "") + ":" + (savedViews?.activeId || "all");
  const signature = JSON.stringify([state.inventoryRevision || 0, state.overviewLayout?.mode,
    threads.map(t => t.thread_id), upcoming.map(s => [s.id, s.version]), state.selected,
    [...(state.closing || [])], [...state.pinning], [...state.updatingThreads], archived, query,
    $("phase-filter").value, $("view").value, $("repository").value, $("project").value, groupBy, viewId,
    Math.floor(Date.now() / 60000)]);
  $("back-home").setAttribute("aria-current", state.selected && !state.dispatchDock?.active ? "false" : "page");
  const projects = new Set(threads.map(t => t.project_id).filter(Boolean)).size;
  const stopped = (state.viewThreads || []).filter(t => t.wake_pause_kind === "stop" && t.attention !== "archived").length;
  $("release-stops-open").hidden = !stopped;
  $("release-stops-open").textContent = stopped + " stopped…";
  $("release-stops-open").title = "Release " + stopped + (stopped === 1 ? " stopped conversation" : " stopped conversations");
  $("release-stops-open").ariaLabel = $("release-stops-open").title;
  $("results-count").textContent = threads.length + (threads.length === 1 ? " thread" : " threads") +
    " · " + projects + (projects === 1 ? " group" : " groups") +
    (upcoming.length ? " · " + upcoming.length + (upcoming.length === 1 ? " scheduled prompt" : " scheduled prompts") : "");
  if (signature === state.listSignature) { if (state.overviewLayout) renderOverviewLayout(threads, upcoming, workingThreads); if (state.selected || state.statusThread) renderStatus(); state.dispatchDock?.flush(); return; }
  state.listSignature = signature;
  const focused = document.activeElement?.dataset.thread;
  const focusAction = document.activeElement?.dataset.action;
  const focusGroup = document.activeElement?.closest("[data-group]")?.dataset.group;
  const focusArea = document.activeElement?.closest("#sessions") ? $("sessions") : $("welcome");
  // Rebuilding the board can clamp the scrollers while their content is empty.
  // Capture the current position on every render, including background updates.
  const scrollPositions = state.sidebarCards ? [$("welcome"), $("sessions")].map(el => ({el, top: el.scrollTop, left: el.scrollLeft})) : [];
  $("sessions").replaceChildren(); $("overview").replaceChildren();
  const agentTree = agentThreadTree(threads);
  const priorSidebar = state.sidebarCards || new Map();
  state.sidebarCards = new Map();
  function sidebarCard(thread, group) {
    if (!agentTree.children || agentTree.children.has(thread.thread_id) || thread.created_by_thread_id ||
        thread.forked_from || thread.forked_sessions?.length || thread.attention_items?.length)
      return agentThreadCard(thread, agentTree, true);
    // A leaf's compact button depends on its own record and selection. Keep
    // separate nodes if the same thread appears in more than one group.
    const key = JSON.stringify([group, thread.thread_id]);
    const revision = [thread.metadata_revision ?? state.inventoryRevision, state.selected === thread.thread_id, thread.pinned, thread.quiet, thread.attention, thread.unread_result].join("|");
    const previous = priorSidebar.get(key);
    const card = previous?.revision === revision ? previous : {revision, node: agentThreadCard(thread, agentTree, true)};
    state.sidebarCards.set(key, card);
    return card.node;
  }
  const groups = threadGrouping.groupThreads(agentTree.roots);
  const filtered = Boolean(query || $("phase-filter").value || ["attention", "completed"].includes($("view").value) || $("repository").value || $("project").value);
  function addGroup(key, label, items) {
    if (!items.length) return;
    const section = node("section", null, "thread-group " + key), heading = groupHeading(label);
    section.setAttribute("aria-label", label);
    if (key === "priority") heading.className += " sr-only";
    section.append(heading);
    for (const thread of items) section.append(sidebarCard(thread, key));
    $("sessions").append(section);
  }
  state.schedules?.renderUpcoming($("overview"), upcoming);
  if (groupBy === "domain") {
    for (const group of threadOrganization.groupThreads(agentTree.roots, "domain"))
      addGroup(group.key, group.label, group.threads);
  } else if (archived) addGroup("closed", "Closed", groups.closed);
  else if ($("view").value === "later") addGroup("later", "Later", groups.later);
  else {
    addGroup("priority", "Attention", groups.priority);
    const organized = groupBy === "phase" ? groups.phases :
      threadOrganization.groupThreads(groups.phases.flatMap(group => group.threads), groupBy);
    for (const group of organized) addGroup(group.key, group.label, group.threads);
  }
  if (!threads.length && !upcoming.length)
    $("sessions").append(node("p", filtered ? "No matching threads" : "Nothing here yet", "empty"));
  for (const {el, top, left} of scrollPositions) { el.scrollTop = top; el.scrollLeft = left; }
  state.dispatchDock?.flush();
  if (focused) {
    const candidates = [...focusArea.querySelectorAll("[data-thread]")].filter(el => el.dataset.thread === focused && el.dataset.action === focusAction);
    const target = candidates.find(el => el.closest("[data-group]")?.dataset.group === focusGroup) || candidates[0];
    target?.focus({preventScroll: true});
  }
  if (state.selected || state.statusThread) renderStatus();
  if (state.overviewLayout) renderOverviewLayout(threads, upcoming, workingThreads);
  globalThis.threadHover?.refresh();
}
function renderOverviewLayout(threads, upcoming = [], workingThreads = threads) {
  const host = $("atlas-overview");
  if (!host) return;
  const atlas = state.overviewLayout?.mode === "atlas";
  const workingSet = state.overviewLayout?.mode === "working-set", workingHost = $("working-set-overview");
  const list = !atlas && !workingSet, listHost = $("thread-list-overview");
  // Scheduled prompts remain available above every overview representation.
  $("overview").hidden = (atlas || workingSet || list) && !upcoming.length;
  $("overview").classList.toggle("atlas-schedules-only", atlas || workingSet || list);
  if (listHost) listHost.hidden = !list;
  if (list && listHost && !$("welcome").hidden) {
    state.threadList ||= WorkingSet.createList({document,
      onOpen: id => action(() => select(id)),
      onDismiss: thread => action(() => markThreadHandled(thread)),
      onClose: thread => action(() => toggleThreadClosed(thread)),
      onMenu: (thread, anchor, event) => state.threadActions.showMenu(anchor, thread.title, threadMenuItems(thread, {inbox: true}), event)});
    const view = savedViews?.activeId || "all";
    state.threadList.render(workingThreads, listHost, {workspace: state.projectCwd || "all", view,
      selectedGroup: view.startsWith("group-") ? view.slice(6) : view === "all" ? null : undefined,
      placement: $("view").value, revision: state.listSignature});
  }
  host.hidden = !atlas;
  if (workingHost) workingHost.hidden = !workingSet;
  if (workingSet && workingHost && !$("welcome").hidden) {
    state.workingSet ||= WorkingSet.create({document, window, storage: state.overviewPreferences,
      isActive: () => state.overviewLayout?.mode === "working-set" && !$("welcome").hidden,
      renderCard: (thread, needsReply) => threadCard({...thread, attention_target_thread_id: null, can_handle_response: false}, false, needsReply),
      onOpen: id => action(() => select(id)),
      onGroupChange: id => action(() => savedViews.openGroup(id)),
      onDismiss: thread => action(() => markThreadHandled(thread)),
      onRead: thread => action(() => markThreadRead(thread))});
    const view = savedViews?.activeId || "all";
    state.workingSet.render(workingThreads, workingHost, {workspace: state.projectCwd || "all", view,
      selectedGroup: view.startsWith("group-") ? view.slice(6) : view === "all" ? null : undefined,
      groupName: savedViews?.current?.()?.name,
      placement: $("view").value, revision: state.listSignature});
  }
  if (!atlas || $("welcome").hidden) return;
  state.atlas ||= LiveAtlas.create({document, window, storage: state.overviewPreferences,
    onOpen: id => action(() => select(id))});
  state.atlas.render(threads, host, {workspace: state.projectCwd || "all"});
}
function upcomingSchedules(query) {
  if ($("view").value !== "later") return [];
  return state.schedules?.upcoming({matches: item => {
    const thread = state.sessions.find(thread => thread.thread_id === item.target_thread_id);
    const context = thread || {...item.settings, cwd: item.cwd};
    return inProject(context, state.folders?.selected) && threadViews.matches(
      {...context, attention: "later", title: [thread?.title, item.message, item.cwd].filter(Boolean).join(" ")},
      {show: "later", phase: $("phase-filter").value, project: $("project").value,
        repository: $("repository").value, search: query});
  }}) || [];
}
function supervisorOf(thread) {
  if (Object.hasOwn(thread, "supervisor_thread_id")) return thread.supervisor_thread_id;
  return thread.creation_mode === "orchestration" ? thread.created_by_thread_id : null;
}
function agentThreadTree(threads) {
  // Filter first: a hidden, closed, or differently placed creator must never
  // swallow a matching child. Keep the original records and counts untouched.
  const visible = new Map(threads.map(thread => [thread.thread_id, thread]));
  const parents = new Map(), children = new Map();
  for (const thread of threads) {
    // Group under the current supervisor. Without a transfer that is an
    // explicit orchestration creator; dispatch provenance alone never groups.
    const supervisor = supervisorOf(thread);
    if (!supervisor) continue;
    const parent = visible.get(supervisor);
    if (!parent || parent.attention !== thread.attention) continue;
    const seen = new Set([thread.thread_id]);
    let ancestor = parent;
    while (ancestor && !seen.has(ancestor.thread_id)) {
      seen.add(ancestor.thread_id);
      ancestor = visible.get(supervisorOf(ancestor));
    }
    if (ancestor) continue; // Defensive against malformed historical cycles.
    parents.set(thread.thread_id, parent.thread_id);
    if (!children.has(parent.thread_id)) children.set(parent.thread_id, []);
    children.get(parent.thread_id).push(thread);
  }
  for (const items of children.values()) items.sort((a, b) =>
    (a.created_at || 0) - (b.created_at || 0) || a.thread_id.localeCompare(b.thread_id));
  // An agent row needs the user by the same rule as its tab dot and tree badge, which honors dismissal.
  const requiresInput = thread => threadViews.needsAction(thread);
  return {children, requiresInput,
    // Use the badge's attention predicate: unread findings and replies need
    // their own card even when their creator has nothing in Attention.
    roots: threads.filter(thread => !visible.has(thread.attention_grouped_under) &&
      (!parents.has(thread.thread_id) || threadGrouping.awaitsUser(thread)))};
}
function agentThreadCard(thread, tree, compact = false, inAttention = false) {
  const card = threadCard(thread, compact, inAttention);
  const children = tree.children.get(thread.thread_id) || [];
  const forks = compact && (thread.forked_from || thread.forked_sessions?.length);
  const needs = thread.attention_items || [];
  if (!children.length && !thread.created_by_thread_id && !forks && !needs.length) return card;
  const group = node("div", null, "agent-thread-group" + (compact ? " compact-agent-group" : ""));
  group.append(card);
  if (needs.length) {
    const list = node("ul", null, "agent-rows");
    for (const need of needs) {
      const item = node("li"), link = node("button", need.title + " · " + need.action, "agent-row needs-attention");
      link.type = "button"; link.dataset.thread = need.thread_id;
      link.onclick = () => action(() => select(need.thread_id));
      item.append(link); list.append(item);
    }
    group.append(list);
  }
  if (forks) group.append(forkLinks(thread));
  if (thread.created_by_thread_id && compact) {
    const creator = thread.created_by;
    const origin = node("button", "Created by " + (creator?.title || "another agent"), "agent-origin");
    origin.type = "button";
    origin.disabled = !creator?.available;
    origin.title = origin.textContent;
    origin.dataset.thread = thread.created_by_thread_id;
    origin.dataset.action = "creator";
    origin.onclick = () => action(() => select(thread.created_by_thread_id));
    group.append(origin);
  }
  if (children.length) group.append(agentThreadChildren(thread, tree, compact));
  return group;
}
function agentThreadChildren(parent, tree, compact) {
  const children = tree.children.get(parent.thread_id);
  const details = node("details", null, "agent-children");
  const expansion = state.agentExpansion ||= new Map();
  const key = (compact ? "sidebar:" : "overview:") + (savedViews?.activeId || "all") + ":" + parent.thread_id;
  details.open = expansion.get(key) ?? true;
  details.ontoggle = () => { if (details.isConnected) expansion.set(key, details.open); };
  const working = children.filter(thread => threadGrouping.status(thread).key === "running").length;
  // As in the inbox, only what escalates (approval or stall) counts toward the parent's "needs you".
  const needsInput = children.filter(child => WorkingSet.escalates(child) && WorkingSet.requestOf(child) !== "failed").length;
  const unread = children.filter(thread => thread.unread_result).length;
  const summary = node("summary", null, "agent-children-toggle");
  const counts = [needsInput && needsInput + " needs you"].filter(Boolean);
  if (counts.length) summary.append(node("span", counts.join(" · "), "agent-counts"));
  summary.dataset.thread = parent.thread_id;
  summary.dataset.action = "agents";
  const description = [children.length + (children.length === 1 ? " agent" : " agents"), working && working + " working", ...counts, unread && unread + " unread"].filter(Boolean);
  summary.setAttribute("aria-label", "Agents created by " + parent.title + " · " + description.join(" · "));
  summary.title = "Agents created by " + parent.title;
  details.append(summary);
  const list = node("ul", null, "agent-rows");
  for (const child of children) {
    const item = node("li"), needs = tree.requiresInput(child);
    // A dismissed request keeps its saved reason; without a current need it reads as the agent's phase.
    const raw = threadGrouping.status(child), status = raw.key === "waiting" && !needs ? {key: "idle", label: threadGrouping.phaseLabel(child)} : raw;
    const button = node("button", null, "agent-row" + (status.key === "running" ? " working" : "") + (needs ? " needs-attention" : "") + (child.unread_result ? " response-unread" : ""));
    button.type = "button";
    button.dataset.thread = child.thread_id;
    button.setAttribute("aria-current", state.selected === child.thread_id ? "true" : "false");
    button.title = child.checkpoint?.summary || child.title;
    if (child.turn_completion && child.turn_completion.status !== "interrupted") {
      button.setAttribute("aria-description", child.unread_result ? "Unread response" : "Read response");
    }
    button.append(node("span", child.title, "agent-row-title"));
    const phase = threadGrouping.phaseLabel(child);
    const provider = child.client === "codex" ? "Codex" : child.client === "claude" ? "Claude Code"
      : child.client === "muse" ? "Muse Code" : child.client === "kimi" ? "Kimi" : child.client === "antigravity" ? "Antigravity" : "Unknown provider";
    const logoLabel = child.client === "codex" ? "OpenAI · Codex" : child.client === "claude" ? "Anthropic · Claude Code" : child.client === "muse" ? "Meta · Muse Code" : child.client === "kimi" ? "Moonshot · Kimi Code" : "Google · Antigravity";
    const logo = globalThis.providerLogo?.(child.client, logoLabel);
    const labels = [!logo && provider, phase, status.key !== "running" && status.label !== phase && status.label,
      child.cwd && (child.cwd.split("/").filter(Boolean).pop() || "/")].filter(Boolean);
    const metadata = node("span", labels.join(" · "), "agent-row-status");
    if (child.cwd) metadata.title = "Working folder: " + child.cwd;
    if (logo) metadata.replaceChildren(logo, node("span", labels.join(" · ")));
    button.append(metadata);
    if (status.key === "running") button.append(node("span", status.label, "sr-only"));
    button.onclick = () => action(() => select(child.thread_id));
    item.append(button);
    if (!compact) {
      const peek = node("button", "Peek", "quiet agent-peek");
      peek.type = "button"; peek.dataset.thread = child.thread_id; peek.dataset.action = "peek";
      peek.setAttribute("aria-label", "Peek · " + child.title);
      peek.onclick = event => { event.stopPropagation(); globalThis.threadHover?.open(peek, child.thread_id, {thread: child}); };
      item.append(peek);
    }
    if (tree.children.has(child.thread_id)) item.append(agentThreadChildren(child, tree, compact));
    list.append(item);
  }
  details.append(list);
  return details;
}
function conversationEntry(id) {
  const cache = state.conversations ||= new Map();
  const position = state.conversationPositions?.get(id);
  const entry = cache.get(id) || {detail: null, nodes: null, scroll: position?.scroll || 0,
    following: position?.following ?? true, revision: 0, pending: null};
  cache.delete(id); cache.set(id, entry);
  // Bound retained representations by their uncompressed network bytes as well
  // as entry count. Active panes and the currently open document can exceed
  // the retained budget while a reader inspects an unusually large output.
  let bytes = 0;
  for (const record of cache.values()) bytes += record.retainedBytes || 0;
  for (const [key, retired] of cache) {
    if (cache.size <= 64 && bytes <= 32 * 1024 * 1024) break;
    if (key === id || key === state.selected || globalThis.agentCoordPanes?.layout?.tabs.includes(key)) continue;
    bytes -= retired.retainedBytes || 0;
    clearTimeout(retired.retryTimer);
    retired.viewport?.dispose();
    globalThis.OpenUIThread?.release?.(key);
    cache.delete(key);
    if (!state.submissionQueue?.lanes.has(key)) state.outboxDiscoveries?.delete(key);
    state.outgoing?.release?.(key);
  }
  return entry;
}
function replaceTimelineContent(content) {
  if (globalThis.OpenUIThread) {
    OpenUIThread.park($("timeline"));
    OpenUIThread.reconcileChildren($("timeline"), [content]);
  } else $("timeline").replaceChildren(content);
}
function retainConversation() {
  if (!state.selected || !state.detail || isSessionDraft()) return;
  if (state.titleEdit) saveTitleEdit({focus: false}).catch(showError);
  const entry = conversationEntry(state.selected);
  entry.detail = state.detail;
  // A streamed update may be waiting for its paint. Do not retain older DOM
  // beside the newer model and later mistake it for an up-to-date transcript.
  entry.nodes = state.timelineRender?.id === state.selected || state.responseActions?.pending ? null : [...$("timeline").childNodes].filter(child => !child.dataset?.openuiParked);
  entry.scroll = state.timelineScroll?.position() ?? $("timeline").scrollTop;
  entry.following = state.timelineScroll?.following ?? true;
  entry.viewport?.suspend();
  globalThis.OpenUIThread?.park?.($("timeline"));
  // Keep lightweight reading positions even after the bounded transcript cache evicts a tab.
  (state.conversationPositions ||= new Map()).set(state.selected, {scroll: entry.scroll, following: entry.following});
}
function sameConversation(first, second) {
  if (!first || first.revisionEpoch !== second.revisionEpoch) return false;
  if (!["history_revision", "stream_seq", "metadata_revision"].every(key =>
      Number.isSafeInteger(first[key]) && first[key] === second[key])) return false;
  return first.thread.beforeSeq === second.thread.beforeSeq && first.thread.turns.length === second.thread.turns.length &&
    first.thread.historyUnavailable === second.thread.historyUnavailable && first.transferReadSource === second.transferReadSource;
}
// Pages and requested output live with the bounded conversation cache. A recent
// snapshot replaces its suffix; it must not discard turns the reader opened.
function mergeHistoryWindow(entry, thread, older = false) {
  const previous = entry.detail?.thread;
  if (previous && (older || entry.historyLoaded)) {
    const recent = older ? previous : thread, earlier = older ? thread : previous;
    const ids = new Set((recent.turns || []).map(turn => turn.id));
    const overlap = (earlier.turns || []).findIndex(turn => ids.has(turn.id));
    const prefix = overlap < 0 ? earlier.turns || [] : earlier.turns.slice(0, overlap);
    thread = {...recent, turns: [...prefix, ...recent.turns],
      beforeSeq: prefix.length || older ? earlier.beforeSeq : recent.beforeSeq,
      earlierTurns: prefix.length || older ? earlier.earlierTurns : recent.earlierTurns};
  }
  if (entry.expandedOutput) {
    const retained = new Set();
    thread = {...thread, turns: (thread.turns || []).map(turn => ({...turn, items: (turn.items || []).map(item => {
      const key = JSON.stringify([turn.id, item.id]), full = entry.expandedOutput.get(key);
      if (!full) return item;
      if (item === full.item || JSON.stringify(item) === full.preview || JSON.stringify(item) === JSON.stringify(full.item)) {
        retained.add(key); return full.item;
      }
      return item; // A changed preview invalidates an older full-output read.
    })}))};
    for (const key of entry.expandedOutput.keys()) if (!retained.has(key)) entry.expandedOutput.delete(key);
  }
  return thread;
}
function historyPage(result, id, before = null) {
  const page = result?.thread;
  if (!page || page.id !== id || !Array.isArray(page.turns) || !Number.isSafeInteger(page.beforeSeq) ||
      page.beforeSeq < 0 || !Number.isSafeInteger(page.earlierTurns) || page.earlierTurns < 0 ||
      before !== null && page.beforeSeq >= before) throw new Error("Could not read the requested conversation page. Try again.");
  return page;
}
async function bridgeHistoryWindow(id, thread, target, options) {
  // More than one page can arrive while this tab is away. Fill that gap before
  // joining the retained prefix, so accepted turns never disappear between them.
  while (thread.earlierTurns && !thread.turns.some(turn => turn.id === target)) {
    const before = thread.beforeSeq;
    const page = historyPage(await api(sessionPath(id) + "?before_seq=" + before + "&limit=40", undefined, options), id, before);
    const ids = new Set(thread.turns.map(turn => turn.id));
    thread = {...thread, beforeSeq: page.beforeSeq, earlierTurns: page.earlierTurns,
      turns: [...page.turns.filter(turn => !ids.has(turn.id)), ...thread.turns]};
  }
  return thread;
}
async function select(id, {rollUp = false, dock = false, focus = true, stayInProject = false, refreshInventory = true, tabId = null} = {}) {
  state.initializeConversationView?.();
  const activitySource = state.paneMode || globalThis.agentCoordPanes?.active ? "programmatic" : state.activity?.source();
  const previousTab = state.threadTabs?.selected;
  state.projectScratchpad?.hideTab();
  state.navigationRevision = (state.navigationRevision || 0) + 1;
  state.mobileChat?.close();
  if (id === "new-session") return newSession();
  $("conversation").classList.remove("session-draft");
  if (!rollUp) state.rollUp?.stop();
  if (dock && globalThis.agentCoordPanes?.active) globalThis.agentCoordPanes.leave();
  if (dock) state.dispatchWorkspace?.hide();
  if (globalThis.agentCoordPanes?.active) { await globalThis.agentCoordPanes.openThread(id); return; }
  if (state.paneMode && state.selected && state.selected !== id) { await window.parent.agentCoordPanes.openThread(id); return; }
  savedViews?.remember();
  setNavigation(false);
  if (state.selected) state.drafts.set(state.selected, $("message").value);
  if (id !== state.selected) retainConversation();
  state.selected = id;
  state.updateSubmissions?.select(id);
  if (!state.paneMode) state.activity?.select(id, activitySource);
  state.scratchpad?.select(id, {snapshot: state.conversations?.get(id)?.detail?.scratchpad,
    defer: state.config?.conversationOpen === 1});
  if (dock) state.threadTabs?.suspend();
  else if (!tabId || !state.threadTabs?.selectConversation?.(tabId, id))
    state.threadTabs?.open(state.sessions?.find(thread => thread.thread_id === id)
      || state.conversations?.get(id)?.detail?.work_thread || {thread_id: id, title: "Conversation"}, state.agentTreeController?.tree);
  if (state.dispatchDock?.active) state.dispatchDock.hide();
  if (dock) {
    setChatExpanded(false);
    state.dispatchDock.show(id, state.folders.selected.cwd);
    state.dispatchDock.remember(state.folders.selected.cwd, {id, hidden: false});
    state.dispatchWorkspace?.show(id, state.folders.selected.cwd);
  }
  $("message").value = state.drafts.get(id) || "";
  const cached = conversationEntry(id);
  cached.refreshFailures = 0;
  if (cached.detail?.revisionEpoch && cached.detail.revisionEpoch !== state.config?.conversationRevisionEpoch) {
    cached.detail = cached.nodes = null;
    cached.viewport?.dispose(); delete cached.viewport;
    globalThis.OpenUIThread?.release?.(id);
  }
  if (state.durableDrafts && !cached.draftLoad) {
    cached.draftLoad = restoreLocalDraft(id).then(restored => { if (!restored) cached.draftLoad = null; }, error => {
      cached.draftLoad = null; showError(error);
    });
  }
  const opening = cached.opening = {};
  cached.refreshError = null;
  const current = state.streamHealthy && cached.detail?.revisionEpoch === state.config?.conversationRevisionEpoch &&
    ["history_revision", "stream_seq", "metadata_revision"].every(key => Number.isSafeInteger(cached.detail?.[key])) && cached.validatedRevision === cached.revision &&
    !state.searchTarget && !state.messageTarget && !cached.detail?.deferReadAcknowledgment;
  cached.requestsRefreshing = !current;
  notifications?.syncFocus();
  state.detail = cached.detail;
  state.detailRefreshing = !cached.detail;
  state.titleEdit = null;
  state.attachments?.highlight(false);
  if (!cached.detail) renderStatus();
  if (navigation) navigation.remember();
  else history.replaceState(null, "", "#" + encodeURIComponent(id));
  $("welcome").hidden = true;
  $("conversation").hidden = false;
  state.agentTreeController?.bind({preservePanel: !!tabId && tabId === previousTab, dock});
  if (typeof layoutContext === "function") layoutContext();
  if (!cached.detail) {
    $("requests").replaceChildren();
    $("thread-context").hidden = true;
    renderTitle();
  }
  $("page-location").textContent = dock ? "Dispatcher" : "Thread";
  delete $("requests").dataset.signature;
  if (cached.detail) {
    if (cached.nodes) {
      if (state.timelineThread !== id) {
        if (globalThis.OpenUIThread && !(current && OpenUIThread.restore?.($("timeline"), cached.nodes))) OpenUIThread.commit($("timeline"), {childNodes: cached.nodes}, {
          thread: id, theme: document.documentElement.dataset.theme || "light",
          disabled: !cached.detail.work_thread.browser_session || cached.detail.work_thread.attention === "archived",
          onAction: text => sendOpenUIResponse(id, text)});
        else if (!globalThis.OpenUIThread) $("timeline").replaceChildren(...cached.nodes);
        state.timelineScroll?.afterRender(cached.following);
      }
      state.timelineThread = id;
      cached.viewport?.resume();
    } else renderTimeline();
    if (state.timelineScroll) {
      if (!cached.following) state.timelineScroll.restore(cached.scroll);
    } else $("timeline").scrollTop = cached.scroll;
    if (current) hydrateConversationControls(id, cached.detail);
    renderThread(); renderRequests(); renderQueuedMessages(); renderStatus();
    if (focus && cached.detail.work_thread.browser_session && !state.mobileChat?.matches)
      $("message").focus({preventScroll: true});
  } else {
    replaceTimelineContent(node("p", "Opening session…", "empty"));
    state.timelineThread = null;
    state.timelineScroll?.reset();
  }
  const restorePosition = !cached.detail && state.conversationPositions?.get(id);
  // Selection changes two sidebar highlights. The overview is out of view and
  // will render the latest inventory and filters when the reader returns.
  if (state.sidebarCards && !dock) {
    for (const element of $("sessions").querySelectorAll('[aria-current="true"],[aria-current][data-thread="' + CSS.escape(id) + '"]')) {
      const selected = element.dataset.thread === id;
      element.setAttribute("aria-current", String(selected));
      element.classList.toggle("selected", selected);
    }
    $("back-home").setAttribute("aria-current", "false");
  } else if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => setTimeout(renderList, 0));
  else renderList();
  if (!cached.detail && !cached.accessDenied && state.snapshotCache) {
    state.snapshotCache.read(id).then(snapshot => {
      if (!snapshot || state.selected !== id || cached.opening !== opening || cached.detail || state.conversations.get(id) !== cached) return;
      // Disk may belong to a previous coordinator epoch or workspace. Paint
      // inert transcript rows without installing provider or action metadata.
      const content = node("div");
      content.dataset.provisionalThread = id;
      const items = (snapshot.thread?.turns || []).slice(-12).flatMap(turn => turn.items || [])
        .filter(item => item.type === "userMessage" || item.type === "agentMessage").slice(-12);
      for (const item of items) {
        const row = node("article", null, item.type === "userMessage" ? "message user" : "message agent");
        row.append(node("div", itemText(item).slice(0, 8192), "text"));
        content.append(row);
      }
      if (!items.length) for (const message of (snapshot.coordinationMessages || []).slice(-12))
        content.append(node("article", (message.body || "").slice(0, 8192), "message"));
      if (content.childElementCount) replaceTimelineContent(content);
    }).catch(() => {});
  }
  const fresh = current || await refreshDetail({markSeen: false});
  if (state.selected !== id) return;
  if (restorePosition && !restorePosition.following) {
    if (state.timelineScroll) state.timelineScroll.restore(restorePosition.scroll);
    else $("timeline").scrollTop = restorePosition.scroll;
  }
  // Streamed updates can invalidate the snapshot without changing the provider.
  // Load its controls even when another detail refresh is already scheduled.
  if (state.detail?.work_thread.browser_session && state.detail.transferReadSource !== "saved" && !Array.isArray(state.detail.models)) loadSessionModels();
  if (fresh === false) return;
  if (state.paneMode) return;
  const displayed = state.detail.work_thread;
  const checkpoint = displayed.checkpoint?.id || 0, completion = displayed.turn_completion?.id || 0;
  if (!dock && !globalThis.document?.hidden && globalThis.document?.hasFocus?.() !== false && !state.detail?.deferReadAcknowledgment &&
      (checkpoint > (displayed.seen_checkpoint_id || 0) || completion > (displayed.seen_completion_id || 0))) {
    await api(threadPath(id), {seen: true, seen_checkpoint_id: checkpoint, seen_completion_id: completion},
      {headers: {Prefer: "return=minimal"}}).catch(showError);
  }
  // The conversation is already displayed. Ancillary failures remain visible,
  // but must not abort startup before navigation and native restoration finish.
  if (refreshInventory && !state.config?.conversationOpen) refreshList({background: true}).catch(showError);
  if (state.selected !== id) return;
  if (state.detail?.work_thread.browser_session) {
    if (focus) (state.mobileChat?.matches && !dock ? $("timeline") : $("message")).focus({preventScroll: true});
  } else $("session-name").focus({preventScroll: true});
}
async function refreshDetail({markSeen = true, background = false, threadId = state.selected} = {}) {
  if (!state.config?.conversationOpen) state.schedules?.refresh({background: true}).catch(showError);
  const id = threadId;
  if (!id || id === "new-session") return;
  const entry = conversationEntry(id);
  if (entry.pending) return entry.pending;
  clearTimeout(entry.retryTimer);
  entry.retryTimer = null;
  entry.pending = (async () => {
    const revision = entry.revision, epoch = state.config?.conversationRevisionEpoch, initialSequence = entry.lastEventSeq;
    const streamedItems = entry.streamedItems = new Map();
    entry.streamedUsage = false;
    const attentionRevision = state.attentionMutations?.get(id)?.revision || 0;
    // A hung read must not hold the shared refresh promise (and Send) forever.
    // Only reads use this deadline; a submitted message must never be retried.
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    const options = {signal: controller.signal, priority: background ? "low" : "auto"};
    const paged = !!state.config?.conversationHistoryPages;
    const searchTarget = state.searchTarget?.thread === id && state.searchTarget;
    const messageTarget = state.messageTarget?.thread === id && state.messageTarget || entry.pendingMessageTarget;
    if (searchTarget || messageTarget) {
      entry.allTurns = true;
      if (!paged) entry.completeHistory = true;
    }
    const sessionOptions = !paged && entry.completeHistory ? options : {...options, headers: {"X-Agent-Coord-History-View": "recent"}};
    let work, detail;
    try {
      if (state.config?.conversationOpen === 1 && !entry.completeHistory) {
        let snapshot;
        try { snapshot = await api("conversations/" + encodeURIComponent(id) + "/open", undefined, sessionOptions); }
        catch (error) {
          if (![404, 503].includes(error.status)) throw error;
          snapshot = {legacyDetailRequired: true};
        }
        detail = {...snapshot, thread: snapshot.recent_turns};
        delete detail.recent_turns;
        if (snapshot.legacyDetailRequired) {
          const legacy = await api(sessionPath(id), undefined, {...sessionOptions,
            headers: {...sessionOptions.headers, "X-Agent-Coord-Thread-Controls": "1"}});
          detail = {...detail, ...legacy};
          delete detail.history_revision; delete detail.stream_seq;
        }
        work = detail.work_thread;
      } else if (state.config?.conversationSnapshotControls) {
        detail = await api(sessionPath(id), undefined, {...sessionOptions,
          headers: {...sessionOptions.headers, "X-Agent-Coord-Thread-Controls": "1"}});
        work = detail.work_thread;
      } else {
        // Older backends expose close status only on the separate thread route.
        const known = entry.detail?.work_thread || state.sessions?.find(thread => thread.thread_id === id);
        const session = known?.browser_session || known?.can_read_history ? api(sessionPath(id), undefined, sessionOptions) : null;
        session?.catch(() => {}); // Observed below, or superseded by the thread read's failure.
        work = await api(threadPath(id), undefined, options);
        if (work.browser_session || work.can_read_history) detail = await (session || api(sessionPath(id), undefined, sessionOptions));
      }
      if (!work.browser_session && !work.can_read_history) {
        const coordinationMessages = detail?.coordinationMessages ?? (await api(threadPath(id) + "/messages", undefined, options)).data;
        detail = {session: {cwd: work.cwd}, thread: {turns: []}, requests: [], running: false, coordinationMessages};
      }
      if (paged && detail?.thread?.beforeSeq != null && !detail.thread.historyUnavailable) {
        let thread = detail.thread;
        if (searchTarget?.turn && !thread.turns.some(turn => turn.id === searchTarget.turn)) {
          // Resolve the indexed target and join its context to the latest page.
          const target = historyPage(await api(sessionPath(id) + "?turn_id=" + encodeURIComponent(searchTarget.turn) + "&limit=40", undefined, options), id);
          thread = await bridgeHistoryWindow(id, thread, searchTarget.turn, options);
          thread = mergeHistoryWindow({detail: {thread}}, target, true);
          entry.historyLoaded = true;
        }
        if (messageTarget && thread.earlierTurns) {
          // Coordination cards have no native turn sequence. Preserve their
          // ordering by reading compact pages rather than a full transcript.
          thread = await bridgeHistoryWindow(id, thread, null, options);
          entry.historyLoaded = true;
        }
        const lastRetained = entry.historyLoaded && entry.detail?.thread.turns?.at(-1)?.id;
        if (lastRetained) thread = await bridgeHistoryWindow(id, thread, lastRetained, options);
        detail.thread = thread;
      }
    } catch (error) {
      if (controller.signal.aborted) error = new Error("Refreshing this conversation timed out.");
      entry.refreshError = error.message || String(error);
      if (error.status === 403 || error.status === 404 || error.code === "workspace_access_denied" ||
          error.status === 400 && error.message === "Work thread not found in this workspace.") {
        entry.opening = null; entry.accessDenied = true;
        entry.detail = entry.nodes = null;
        entry.viewport?.dispose(); delete entry.viewport;
        state.snapshotCache?.remove?.(id).catch(() => {});
        globalThis.agentCoordPanes?.records.get(id)?.api?.render();
        if (state.selected === id) {
          state.detail = null; state.detailRefreshing = false; state.timelineThread = null;
          replaceTimelineContent(node("p", entry.refreshError, "empty"));
          $("requests").replaceChildren();
          renderStatus();
        }
        throw error;
      }
      entry.refreshFailures = (entry.refreshFailures || 0) + 1;
      const delay = [1000, 2000, 4000][entry.refreshFailures - 1];
      if (delay && id === state.selected && state.conversations.get(id) === entry) {
        entry.retryTimer = setTimeout(() => {
          entry.retryTimer = null;
          if (id !== state.selected || state.conversations.get(id) !== entry) return;
          return refreshDetail({markSeen}).catch(() => {}); // Persistent inline recovery state.
        }, delay);
      }
      throw error;
    } finally { clearTimeout(timeout); }
    detail.work_thread ||= work;
    detail.close_when_done = work.close_when_done;
    detail.work_thread = reconcileAttentionSnapshot(detail.work_thread, attentionRevision);
    if (epoch !== state.config?.conversationRevisionEpoch) return false;
    detail.revisionEpoch = epoch;
    const revisions = ["history_revision", "metadata_revision", "stream_seq"];
    if (entry.detail && entry.detail.revisionEpoch === detail.revisionEpoch &&
        (Number.isSafeInteger(detail.stream_seq) && detail.stream_seq < initialSequence ||
        revisions.some(key => Number.isSafeInteger(detail[key]) && Number.isSafeInteger(entry.detail[key]) && detail[key] < entry.detail[key]))) {
      if (id === state.selected) scheduleDetail();
      return false;
    }
    const changed = revision !== entry.revision;
    // A background snapshot must not erase newer streamed messages or approvals.
    if (changed && entry.detail) {
      if (id === state.selected) scheduleDetail();
      return false;
    }
    // Retain items changed locally during this read without replaying deltas:
    // the snapshot may already include a delta, so replay would duplicate text.
    for (const [turnId, itemIds] of streamedItems) {
      const live = entry.detail?.thread.turns?.find(turn => turn.id === turnId);
      if (!live) continue;
      const turns = detail.thread.turns ||= [];
      let turn = turns.find(turn => turn.id === turnId);
      if (!turn) { turns.push(live); continue; }
      for (const item of live.items || []) if (itemIds.has(item.id)) {
        const index = turn.items.findIndex(current => current.id === item.id);
        if (index < 0) turn.items.push(item); else turn.items[index] = item;
      }
    }
    if (entry.streamedUsage && entry.detail?.work_thread) detail.work_thread.usage = entry.detail.work_thread.usage;
    if (!entry.historyLoaded && !entry.expandedOutput?.size)
      state.snapshotCache?.write(id, detail).catch(() => {});
    entry.retainedBytes = (typeof ConversationSnapshotCache !== "undefined" ? ConversationSnapshotCache.retainedBytes(detail) : detail.snapshotBytes || 0) + (entry.pageBytes || 0) + (entry.expandedBytes || 0);
    if (paged) detail.thread = mergeHistoryWindow(entry, detail.thread);
    const recovering = !!entry.refreshError;
    const same = sameConversation(entry.detail, detail);
    entry.refreshError = null;
    entry.refreshFailures = 0;
    entry.requestsRefreshing = false;
    entry.validatedRevision = revision;
    entry.accessDenied = false;
    entry.detail = detail;
    if (!same) entry.nodes = null;
    if (id !== state.selected || state.conversations.get(id) !== entry) return true;
    state.detail = detail;
    if (detail.agent_tree) hydrateConversationControls(id, detail);
    else if (detail.legacyDetailRequired) state.scratchpad?.select(id);
    state.updateSubmissions?.receive(id, detail.transferSubmissions);
    state.detailRefreshing = changed;
    if (state.dispatchDock?.active === id && (!detail.work_thread.dispatcher || detail.work_thread.attention === "archived")) {
      for (const key of ["sessions", "viewThreads"]) if (state[key]) state[key] = state[key].map(t => t.thread_id === id ? detail.work_thread : t);
      goHome(); return true;
    }
    renderThread();
    if (!same || state.timelineThread !== id || state.timelineRender?.id === id) renderTimeline();
    renderRequests(); renderQueuedMessages(); renderStatus();
    if (recovering && detail.work_thread.browser_session && detail.transferReadSource !== "saved") loadSessionModels();
    // Opening a tab sends its own receipt. Also acknowledge replies rendered
    // while the user stays in the conversation, using only this snapshot's IDs.
    if (markSeen && !detail.deferReadAcknowledgment && !state.paneMode && detail.work_thread.unread_result && !document.hidden && document.hasFocus() &&
        !$("conversation").hidden && (!state.dispatchDock?.active || state.dispatchDock.expanded)) {
      await api(threadPath(id), {seen: true, seen_checkpoint_id: detail.work_thread.checkpoint?.id || 0,
        seen_completion_id: detail.work_thread.turn_completion?.id || 0}, {headers: {Prefer: "return=minimal"}});
      await refreshList({background: true, afterMutation: true});
    }
    if (revision !== entry.revision) scheduleDetail();
    return !changed;
  })().finally(() => {
    entry.pending = null;
    entry.streamedItems = null;
    if (id === state.selected && state.conversations.get(id) === entry) renderStatus();
  });
  if (id === state.selected) renderStatus();
  return entry.pending;
}
async function refreshThread({background = false} = {}) {
  // A pane is refreshed by its parent window, rather than its own timer.
  // Reconcile any event snapshot deferred while that pane was inactive.
  if (state.backgroundDetailPending) {
    state.backgroundDetailPending = false;
    return refreshDetail({background});
  }
  if (state.paneMode) state.schedules?.refresh({background: true}).catch(showError);
  const id = state.selected;
  if (!id || !state.detail || isSessionDraft()) return;
  const revision = state.attentionMutations?.get(id)?.revision || 0;
  const thread = await api(threadPath(id), undefined, {priority: background ? "low" : "auto"});
  if (id !== state.selected || !state.detail) return;
  const previous = state.detail.work_thread;
  if (!state.closeWhenDoneSaving?.has(id)) state.detail.close_when_done = thread.close_when_done;
  const forksChanged = JSON.stringify([previous.forked_from, previous.forked_from_turn_id, previous.forked_sessions]) !==
    JSON.stringify([thread.forked_from, thread.forked_from_turn_id, thread.forked_sessions]);
  state.detail.work_thread = reconcileAttentionSnapshot(thread, revision); renderThread(); renderStatus();
  if (forksChanged) { conversationEntry(id).nodes = null; renderTimeline(); }
  if (!thread.browser_session && thread.can_read_history &&
      (previous.updated_at !== thread.updated_at || ["running", "needs input"].includes(thread.status))) scheduleDetail();
}
function renderThread() {
  const detail = state.detail, work = detail.work_thread, checkpoint = work.checkpoint;
  state.threadTabs?.update([work]);
  state.agentTreeController?.update(work);
  $("thread-context").hidden = false;
  renderTitle();
  $("page-location").textContent = state.dispatchDock?.active ? "Dispatcher" : "Thread";
  $("park").textContent = work.attention === "later" ? "Move to Now" : "Later";
  $("snooze-thread-button").hidden = work.attention === "archived";
  $("snooze-thread-button").title = work.attention === "later" ? "Change return time" : "Choose a return time";
  $("park").hidden = work.attention === "archived";
  $("resume-snooze").hidden = !(work.snoozed || work.snooze_due);
  $("resume-snooze").textContent = work.snooze_due ? "Dismiss reminder" : "Move to Now";
  if (work.snoozed) $("park").hidden = true;
  $("snooze-status").textContent = snoozeLabel(work);
  for (const id of ["snooze-thread-button", "resume-snooze"]) $(id).disabled = state.updatingThreads.has(work.thread_id);
  $("handle-response").hidden = threadGrouping.actionFor?.(work) !== "dismiss";
  $("handle-response").disabled = state.updatingThreads.has(work.thread_id);
  $("organize-thread").textContent = work.project_id ? "Change group" : "Add to group";
  $("quiet-thread").textContent = work.quiet ? "Turn off quiet" : "Make quiet";
  const signature = JSON.stringify(work);
  if ($("checkpoint").dataset.signature === signature) return;
  const sameThread = $("checkpoint").dataset.thread === work.thread_id;
  const opened = new Set([...$("context-info").querySelectorAll("details[open]")].map(el => el.dataset.section));
  $("checkpoint").dataset.signature = signature; $("checkpoint").dataset.thread = work.thread_id;
  const phase = checkpoint?.phase;
  $("checkpoint").replaceChildren(node("h3", "Status"), node("p", phase ? phase[0].toUpperCase() + phase.slice(1) : "No status saved yet."));
  const next = node("p", null, "checkpoint-next");
  const owner = checkpoint?.next_action && {user: "You", agent: "Agent", external: "External"}[checkpoint.next_actor];
  next.append(node("strong", owner ? owner + " next: " : "Next: "), node("span", checkpoint?.next_action || "No next action saved."));
  $("checkpoint").append(next, node("h3", "Latest"), node("p", checkpoint?.summary || "No checkpoint yet. The agent will save one when it has a result to report."));
  if (work.checkpoint_stale) $("checkpoint").append(node("p", "This checkpoint predates the latest turn. Review the conversation for newer work.", "stale-checkpoint"));
  const original = node("section", null, "original-request-section");
  original.append(node("h3", "Original request"));
  if (work.original_request?.length > 240) {
    const expanded = sameThread && opened.has("original");
    const preview = node("p", work.original_request.slice(0, 240).trimEnd() + "…", "original-request");
    const full = node("details"); full.dataset.section = "original"; full.open = expanded;
    preview.hidden = expanded;
    const toggle = node("summary", expanded ? "Show less" : "Show full request");
    full.append(toggle, node("p", work.original_request, "original-request"));
    full.addEventListener("toggle", () => { preview.hidden = full.open; toggle.textContent = full.open ? "Show less" : "Show full request"; });
    original.append(preview, full);
  } else original.append(node("p", work.original_request || "No original request saved.", "original-request"));
  $("checkpoint").append(original);
  $("context-reuse").replaceChildren();
  globalThis.agentCoordContextLifecycle?.render($("context-reuse"), work, {sameThread, opened});
  $("checkpoint-extras").replaceChildren();
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
  $("checkpoint-extras").append(links);
  if (work.forked_from || work.forked_sessions?.length) $("checkpoint-extras").append(forkLinks(work));
  if (work.children?.length) {
    const children = node("details"); children.dataset.section = "children"; children.open = sameThread && opened.has("children"); children.append(node("summary", "Delegated sessions · " + work.children.length));
    for (const child of work.children) { const button = node("button", child.title + " · " + (child.checkpoint?.phase || "No checkpoint yet"), "quiet"); button.onclick = () => action(() => select(child.thread_id)); children.append(button); }
    $("checkpoint-extras").append(children);
  }
  if (work.checkpoints?.length > 1) {
    const history = node("details"); history.dataset.section = "history"; history.open = sameThread && opened.has("history"); history.append(node("summary", "Checkpoint history · " + work.checkpoints.length));
    for (const entry of work.checkpoints) { const item = node("article", null, "checkpoint-entry"); item.append(node("small", entry.phase + " · " + new Date(entry.created_at * 1000).toLocaleString(), "muted"), node("p", entry.summary)); if (entry.next_action) item.append(node("p", "Next (" + entry.next_actor + "): " + entry.next_action)); history.append(item); }
    $("checkpoint-extras").append(history);
  }
  if (checkpoint) $("checkpoint-extras").append(node("small", (checkpoint.author === "user" ? "Edited by you" : "Saved by agent") + " · " + new Date(checkpoint.refreshed_at * 1000).toLocaleString(), "muted"));
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
  $("rename-name").disabled = !!saving;
  $("rename-hint").textContent = saving ? "Saving…" : state.titleEdit?.error || "Enter or click away to save · Esc to cancel";
  $("rename-hint").setAttribute?.("role", state.titleEdit?.error ? "alert" : "status");
}
function startTitleEdit() {
  if (!state.detail || state.titleEdit?.saving) return;
  const saved = state.titleDrafts?.get(state.selected);
  state.titleEdit = {id: state.selected, saving: false, error: saved?.error || ""};
  $("rename-name").value = saved?.value ?? state.detail.work_thread.title;
  $("rename-name").setCustomValidity("");
  renderTitle();
  $("rename-name").focus(); $("rename-name").select();
}
function cancelTitleEdit({focus = true} = {}) {
  if (state.titleEdit?.saving) return;
  state.titleDrafts?.delete(state.titleEdit?.id);
  state.titleEdit = null;
  renderTitle();
  if (focus) $("session-name").focus();
}
async function saveTitleEdit({focus = true} = {}) {
  const edit = state.titleEdit;
  if (!edit || edit.saving || edit.id !== state.selected) return;
  const value = $("rename-name").value, title = value.trim();
  const draft = {value};
  (state.titleDrafts ||= new Map()).set(edit.id, draft);
  if (!title || title.length > 160) {
    edit.error = "Enter a thread title between 1 and 160 characters.";
    state.titleDrafts.set(edit.id, {value, error: edit.error});
    $("rename-name").setCustomValidity(edit.error);
    if (focus) $("rename-name").reportValidity();
    renderTitle(); return;
  }
  if (title === state.detail?.work_thread.title) { cancelTitleEdit({focus}); return; }
  edit.saving = true; edit.error = ""; renderTitle();
  try {
    await api(threadPath(edit.id), {title});
    if (state.titleDrafts.get(edit.id) === draft) state.titleDrafts.delete(edit.id);
    for (const list of [state.sessions, state.viewThreads]) for (const thread of list || [])
      if (thread.thread_id === edit.id) thread.title = title;
    const cached = state.conversations?.get(edit.id)?.detail;
    if (cached?.work_thread) cached.work_thread.title = title;
    if (state.selected === edit.id && state.detail?.work_thread) state.detail.work_thread.title = title;
    edit.saving = false;
    if (state.titleEdit === edit && state.selected === edit.id) cancelTitleEdit({focus});
    refreshList().catch(error => showError(error));
  } catch (error) {
    edit.error = "Couldn’t save the title. " + error.message;
    if (state.titleDrafts.get(edit.id) === draft) state.titleDrafts.set(edit.id, {value, error: edit.error});
    throw error;
  } finally {
    edit.saving = false;
    if (state.titleEdit === edit) renderTitle();
  }
}
function sessionPermissionsUnavailable() {
  const detail = state.detail, work = detail?.work_thread;
  return !work?.browser_session || state.detailRefreshing || !!detail.running || work.attention === "archived" || sessionBusy() || state.closing.has(state.selected);
}
function sessionPermissionLabel(session) {
  const remembered = state.selected === "new-session" ? state.newSessionDraft?.permissionChoices?.[session?.client] : null;
  const mode = session?.permission_modes?.find(mode => mode.id === session.permission_mode) || (remembered?.id === session?.permission_mode ? remembered : null);
  return mode?.label || session?.permission_mode || "";
}
function renderSessionSettings() {
  const session = state.detail?.session, work = state.detail?.work_thread, browserSession = !!work?.browser_session;
  $("workspace").textContent = session?.cwd || work?.cwd || "";
  $("workspace").title = $("workspace").textContent;
  const group = state.organization?.projects?.find(group => group.id === work?.project_id);
  $("status-group").hidden = !work?.project_id;
  const groupLabel = group?.name || work?.project_name || "Group";
  if ($("status-group").textContent !== groupLabel) $("status-group").textContent = groupLabel;
  $("status-group").title = "Change group · " + $("status-group").textContent;
  $("status-group").setAttribute?.("aria-label", $("status-group").title);
  $("session-settings").hidden = !browserSession;
  const label = sessionPermissionLabel(session);
  const permissionLabel = label ? "Permissions · " + label : "Permissions";
  // WebKit can drop a click if its text node is replaced between pointerdown/up.
  if ($("edit-permissions").textContent !== permissionLabel) $("edit-permissions").textContent = permissionLabel;
  $("edit-permissions").disabled = sessionPermissionsUnavailable();
  state.permissionMenu?.sync(composerDraftId(), $("edit-permissions").disabled);
  const feedback = state.selected === "new-session" ? state.newSessionDraft?.commandFeedback : state.commandFeedback.get(state.selected);
  const permissions = /^Permissions: (yolo|default)\b/.exec(feedback || "")?.[1];
  $("command-feedback").textContent = permissions === "yolo" ? "YOLO · Full access, no approvals"
    : permissions === "default" ? "Permissions · Approval prompts enabled" : feedback || "";
  $("command-feedback").title = permissions ? feedback : "";
  $("command-feedback").hidden = !browserSession || !feedback;
  renderModelPicker();
}
async function sessionPermissionCatalog(session) {
  const client = session.client || state.detail?.work_thread?.client || "codex";
  let catalog = session.permission_modes ? session : state.config?.permission_modes?.[client];
  if (!catalog?.permission_modes?.length) catalog = await api("permission-modes?client=" + encodeURIComponent(client) + "&yolo=" + Boolean(session.yolo) + (session.cwd ? "&cwd=" + encodeURIComponent(session.cwd) : ""));
  const current = session.permission_mode || catalog.permission_mode;
  const modes = [...(catalog.permission_modes || [])];
  if (current && !modes.some(mode => mode.id === current)) modes.push({id: current, label: current + " (current)",
    available: false, reason: "This session's current mode is not available for a new selection."});
  return {permission_mode: current, permission_modes: modes, permission_mode_effective: session.permission_mode_effective ?? catalog.permission_mode_effective ?? null};
}
function openSessionPermissions(event) {
  if (sessionPermissionsUnavailable()) return;
  const id = state.selected, session = state.detail.session, draft = state.newSessionDraft;
  let trigger = event?.currentTarget || $("edit-permissions");
  if (!trigger.getBoundingClientRect().width) trigger = $("mobile-chat-actions");
  return state.permissionMenu.open(trigger, {id: composerDraftId(id),
    load: () => sessionPermissionCatalog(session),
    select: mode => saveSessionPermissions(mode, {id, client: session.client, draft}),
  });
}
async function saveSessionPermissions(mode, {id = state.selected, client = state.detail?.session?.client, draft = state.newSessionDraft} = {}) {
  if (state.selected !== id || state.detail?.session?.client !== client || id === "new-session" && draft !== state.newSessionDraft || sessionPermissionsUnavailable())
    throw new Error("This conversation changed. Open Permissions again to choose a mode.");
  if (!mode?.id || mode.available === false) throw new Error("This permission mode is unavailable.");
  const localDraft = id === "new-session" && draft && draft === state.newSessionDraft && !draft.createdId;
  const body = {permission_mode: mode.id};
  if (localDraft) {
    Object.assign(draft.session, body);
    (draft.permissionChoices ||= {})[client] = mode;
    draft.commandFeedback = null;
  } else {
    const session = await api(sessionPath(draft?.createdId && id === "new-session" ? draft.createdId : id), body);
    if (id === state.selected && state.detail) state.detail.session = session;
  }
  if (id === state.selected) renderStatus();
  if (!localDraft) await refreshList();
}
function renderStatus() {
  state.statusThread = state.selected;
  globalThis.OpenUIThread?.availability(state.selected, !state.detail?.work_thread?.browser_session ||
    state.detail.work_thread.attention === "archived" || state.closing?.has(state.selected));
  if (!state.config?.conversationOpen || !state.detail?.work_thread || state.detail.legacyDetailRequired && !state.detail.sidecars)
    state.sidecars?.showThread(state.detail?.work_thread, $("thread-sidecars"));
  const sidecarButton = $("attach-sidecar");
  if (sidecarButton) sidecarButton.hidden = isSessionDraft() || !state.detail?.work_thread?.browser_session || state.detail?.work_thread?.attention === "archived";
  state.schedules?.renderThread($("thread-schedules"), state.selected);
  if (!state.config?.conversationOpen || !state.selected || isSessionDraft() || state.detail?.asks || state.detail?.legacyDetailRequired)
    state.asks?.showThread(isSessionDraft() ? null : state.selected)?.catch(showError);
  globalThis.agentCoordSpeech?.update(state.selected, state.detail, state.config);
  state.rollUp?.changed();
  renderSessionSettings();
  renderTurnElapsed();
  globalThis.agentCoordThreadUsage?.render($("thread-usage"), state.detail?.work_thread?.usage,
    state.detail?.work_thread?.thread_id === state.selected ? state.selected : null);
  const running = !!state.detail?.running;
  const work = state.detail?.work_thread;
  const dispatcher = !!work?.dispatcher;
  const docked = !!state.dispatchDock?.active;
  state.mobileChat?.layoutComposer(docked);
  $("dispatcher-permissions").hidden = !docked;
  $("dispatcher-permissions").disabled = $("edit-permissions").disabled;
  if ($("dispatcher-permissions").textContent !== $("edit-permissions").textContent)
    $("dispatcher-permissions").textContent = $("edit-permissions").textContent;
  if (docked && state.detail && !state.detailRefreshing) {
    state.dispatchDock.update(state.detail);
    state.dispatchWorkspace?.update(state.viewThreads, state.detail);
  }
  if ($("dispatch-direct-label")) $("dispatch-direct-label").hidden = !dispatcher;
  const recipient = state.agentTreeController?.recipient();
  const composerLabel = recipient ? "Message " + recipient : dispatcher ? "Send a request" : work?.client === "muse" ? "Message Muse" : work?.client === "kimi" ? "Message Kimi" : work?.client === "antigravity" ? "Message Antigravity" : work?.client === "claude" ? "Message Claude" : "Message Codex";
  if ($("agent-recipient")) {
    $("agent-recipient").hidden = !recipient;
    $("agent-recipient").textContent = recipient ? "Messaging " + recipient : "";
  }
  $("message-label").textContent = composerLabel;
  if ($("quiet-send")) $("quiet-send").hidden = state.config?.quietSubmissions !== 1;
  $("quiet-send")?.setAttribute?.("aria-pressed", String(!!state.quietSubmissions?.has(state.selected)));
  const archived = work?.attention === "archived";
  const session = state.sessions.find(s => s.thread_id === state.selected);
  const thread = session || work;
  const status = thread ? threadGrouping.status(thread) : null;
  const active = running || status?.key === "running";
  const phase = thread ? threadGrouping.phaseLabel?.(thread) : "";
  $("status").textContent = state.detail?.requests?.length ? "Needs input" : active || status?.key === "completed" || phase && status?.label === phase ? "" :
    thread && thread.response_state !== "reply" ? status.label : "";
  $("status").hidden = !$("status").textContent;
  state.mobileChat?.status(state.detail?.requests?.length ? "Needs input" : active ? "Working" : status?.label || "Ready",
    state.detail?.requests?.length ? "input" : active ? "running" : status?.key || "idle");
  $("composer").hidden = !work?.browser_session && !(!state.detail && state.detailRefreshing);
  renderDetailRefresh();
  const starting = !dispatcher && running && !state.detail?.activeTurn;
  const orderedInput = !dispatcher && state.outgoing?.pendingTransferCounts?.has(state.selected) &&
    ["codex", "claude"].includes(work?.client);
  const ordinaryStartingInput = starting && (orderedInput || state.detail.acceptsUntargetedInput) && ["codex", "claude"].includes(work?.client);
  $("send").disabled = !state.detail || !work?.browser_session || (starting && !ordinaryStartingInput) || archived || sessionBusy() || !!state.attachments?.pending() || state.closing.has(state.selected);
  const sendAction = dispatcher ? "Send request" : orderedInput ? "Send after pending messages" : running && !ordinaryStartingInput ? (!["codex", "claude", "muse"].includes(work?.client) ? "Queue message" : "Steer current turn") : "Send message";
  $("send").setAttribute?.("aria-label", sendAction);
  $("send").title = sendAction + " (Enter)";
  $("queue").hidden = dispatcher || !running || !["codex", "claude", "muse"].includes(work?.client);
  $("queue").disabled = $("send").disabled || starting || !!orderedInput;
  $("message").disabled = archived || !!state.transferLocks?.has(state.selected);
  $("stop").hidden = !running;
  $("close-thread").textContent = archived ? "Reopen" : running || ["running", "needs input"].includes(work?.status) ? "Stop and close" : "Close thread";
  $("close-thread").disabled = !state.detail || state.closing.has(state.selected);
  renderCloseWhenDone();
  $("fork-thread").hidden = !["codex", "claude"].includes(work?.client);
  $("fork-thread").disabled = state.detailRefreshing || !work?.can_fork || active || !!state.detail?.requests?.length || state.forking || state.closing.has(state.selected);
  $("park").disabled = !state.detail || archived || state.closing.has(state.selected);
  $("edit-checkpoint").disabled = !state.detail;
  $("add-link").disabled = !state.detail;
  $("session-name").disabled = !state.detail || isSessionDraft();
  $("organize-thread").disabled = !state.detail;
  $("quiet-thread").disabled = !state.detail;
  $("move-thread").disabled = !!threadMoveUnavailable(work) || state.detailRefreshing;
  $("move-thread").title = threadMoveUnavailable(work);
  $("composer-hint").textContent = archived ? "Reopen this thread to continue." : running ? (!["codex", "claude", "muse"].includes(work?.client) ? "Enter to queue after this turn · Shift + Enter for a new line" : "Enter to steer · Tab to queue after this turn · Shift + Enter for a new line") : "Enter to send · Shift + Enter for a new line · /cd · /model · /help";
  if (!archived && !running && work?.client === "codex") $("composer-hint").textContent += " · $ skills";
  if (dispatcher && !archived) $("composer-hint").textContent = "Requests route to conversations that answer you directly.";
  if (!state.detail && state.detailRefreshing) $("composer-hint").textContent = "Opening conversation…";
  $("message").placeholder = $("composer-hint").textContent;
  state.mobileChat?.placeholder($("composer-hint").textContent, archived || state.detailRefreshing ? $("composer-hint").textContent :
    dispatcher ? "Send a request…" : running ? "Add a follow-up…" : "Message…");
  if (globalThis.matchMedia?.("(pointer: coarse)").matches && !archived) $("message").placeholder = "Message agent…";
  state.attachments?.render();
  state.slashCommands?.render();
  state.mentions?.render();
  state.resizeComposer?.();
  if (state.paneMode) globalThis.agentCoordPane?.updateStatus();
  state.reviewUI?.render(state.detail, isSessionDraft() ? state.newSessionDraft?.draftId || state.selected : state.selected);
}
function renderDetailRefresh() {
  const work = state.detail?.work_thread || state.sessions.find(row => row.thread_id === state.selected);
  const messageId = work?.wake_message_id;
  const paused = !!messageId && work.thread_id === state.selected && work.attention !== "archived";
  $("inbox-pause").hidden = !paused;
  if (paused) {
    const threadId = work.thread_id;
    const resume = $("resume-inbox");
    $("inbox-pause-reason").textContent = "Inbox paused at message #" + messageId + ". " + (work.wake_pause_reason || "");
    resume.disabled = !!state.detailRefreshing || !!state.detail?.running || !!state.resumingInboxes?.has(threadId);
    resume.onclick = () => action(async () => {
      state.resumingInboxes ??= new Set();
      if (state.resumingInboxes.has(threadId)) return;
      state.resumingInboxes.add(threadId);
      resume.disabled = true;
      try {
        await api(threadPath(threadId) + "/resume-inbox", {expected_message_id: messageId});
      } finally {
        state.resumingInboxes.delete(threadId);
        if (state.selected === threadId) await refreshDetail();
        await refreshList();
      }
    });
  }
  const entry = state.conversations?.get(state.selected);
  const visible = !!state.selected && !isSessionDraft() && !!(state.detailRefreshing || entry?.refreshError);
  $("conversation-refresh").hidden = !visible;
  if (!visible) return;
  const failed = !!entry?.refreshError;
  $("conversation-refresh-message").textContent = failed
    ? "Couldn’t refresh this conversation." + (entry.retryTimer || entry.pending ? " Retrying…" : "")
    : "Refreshing conversation…";
  const retry = $("retry-conversation");
  retry.hidden = !failed;
  retry.disabled = !!entry?.pending;
  retry.onclick = () => {
    entry.refreshFailures = 0;
    return action(() => refreshDetail());
  };
}
function renderCloseWhenDone() {
  const button = $("close-when-done"), detail = state.detail, work = detail?.work_thread;
  const request = detail?.close_when_done;
  const pending = ["queued", "closing"].includes(request?.status);
  const armed = !!request?.success_only && pending;
  const kept = request?.success_only && ["cancelled", "failed"].includes(request.status) && !detail?.running;
  button.hidden = !work?.browser_session || work.attention === "archived";
  button.textContent = request?.status === "closing" && armed ? "Closing…" : armed ? "Close when done ✓" : kept ? "Kept open" : "Close when done";
  button.setAttribute?.("aria-pressed", String(armed));
  button.disabled = !detail || state.detailRefreshing || state.closing.has(state.selected) ||
    state.closeWhenDoneSaving?.has(state.selected) || request?.status === "closing" || pending && !armed || !armed && !detail.running;
  button.title = kept ? "Thread kept open: " + (request.error || "Automatic closure was cancelled.") : armed
    ? "Close after successful completion. Click to cancel. New input cancels automatic closure."
    : detail?.running ? "Close after successful completion; keep open for blockers, approvals, or required review."
    : "Start a task, then enable automatic closure after successful completion.";
}
function renderTurnElapsed(now = Date.now() / 1000) {
  const element = $("turn-elapsed"), detail = state.detail, work = detail?.work_thread;
  const running = work?.browser_session ? detail.running : ["running", "needs input"].includes(work?.status);
  const turns = detail?.thread?.turns || [];
  const turn = detail?.activeTurn ? turns.find(turn => turn.id === detail.activeTurn) : turns.at(-1);
  const terminal = ["completed", "failed", "interrupted"].includes(turn?.status);
  const currentId = detail?.activeTurn || turn?.id;
  const value = work?.browser_session
    ? turn?.startedAt ?? (currentId && currentId === work.turn_id ? work.turn_started_at : null)
    : work?.turn_started_at;
  const started = typeof value === "number" ? (value > 1e12 ? value / 1000 : value) : Date.parse(value) / 1000;
  const visible = !!state.selected && running && !terminal && Number.isFinite(started);
  element.hidden = !visible;
  if (!visible || document.hidden) {
    clearTimeout(state.elapsedTimer); state.elapsedTimer = null;
    if (!visible) element.textContent = "";
    return;
  }
  if (!state.elapsedTimer) state.elapsedTimer = setTimeout(() => {
    state.elapsedTimer = null; renderTurnElapsed();
  }, 1000);
  const seconds = Math.max(0, Math.floor(now - started));
  const duration = seconds < 60 ? seconds + "s" : seconds < 3600
    ? Math.floor(seconds / 60) + "m " + seconds % 60 + "s"
    : Math.floor(seconds / 3600) + "h " + Math.floor(seconds % 3600 / 60) + "m " + seconds % 60 + "s";
  element.textContent = (detail.requests?.length || work.status === "needs input" ? "Elapsed · " : "Working · ") + duration;
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
    cancel.disabled = state.detailRefreshing || archived || item.state === "sending";
    cancel.setAttribute("aria-label", "Remove queued message: " + (item.message.slice(0, 80) || "Attached images"));
    cancel.onclick = () => change({action: "cancel", id: item.id});
    row.append(cancel); area.append(row);
  }
  if (items.some(item => item.state === "paused")) {
    const resume = node("button", "Resume queue", "quiet"); resume.type = "button";
    resume.disabled = state.detailRefreshing || archived;
    resume.onclick = () => change({action: "resume"});
    area.append(resume);
  }
}
function threadMoveUnavailable(thread) {
  if (!thread?.browser_session) return "Only app conversations can move projects.";
  if (!["codex", "claude"].includes(thread.client)) return "This provider requires a new conversation in the destination project.";
  if (thread.attention === "archived") return "Reopen the thread before moving it.";
  if (thread.turn_active || ["running", "needs input"].includes(thread.status)) return "Wait for this turn to finish before moving the thread.";
  return "";
}
function itemText(item) {
  if (item.type === "userMessage") return (item.content || []).map(c => c.text ||
    (c.type === "localImage" || (c.type === "image" && !ChatImageAttachments.isImageURL(c.url)) ? "[Image]" : "")).filter(Boolean).join("\n");
  if (item.type === "agentMessage") return item.text || "";
  if (item.type === "reasoning") return (item.summary || item.content || []).map(x => typeof x === "string" ? x : x.text || "").join("\n");
  if (item.type === "commandExecution") return [item.command, item.aggregatedOutput, item.exitCode != null ? "Exit code: " + item.exitCode : ""].filter(Boolean).join("\n\n");
  if (item.type === "fileChange") return (item.changes || []).map(c => c.path + "\n" + (c.diff || "")).join("\n\n");
  if (item.type === "plan") return item.text || "";
  if (item.type === "contextCompaction") return "Earlier context is summarized so the conversation can continue.";
  return JSON.stringify(item, null, 2);
}
function messageTimestamp(item, turn) {
  const date = value => {
    if (value == null || value === "" || typeof value === "boolean") return null;
    const result = new Date(typeof value === "number" ? (value > 1e12 ? value : value * 1000) : value);
    return Number.isFinite(result.getTime()) ? result : null;
  };
  for (const value of [item.timelineAt, item.createdAt, item.created_at, item.timestamp]) {
    const recorded = date(value);
    if (recorded) return {date: recorded, label: "Message recorded"};
  }
  // Legacy transcripts may retain only turn boundaries. Do not assign a
  // turn's start to later steering messages or its end to earlier commentary.
  const messages = (turn.items || []).filter(entry => entry.type === "userMessage" || entry.type === "agentMessage");
  if (item.type === "userMessage" && messages[0] === item) {
    const started = date(turn.startedAt ?? turn.started_at);
    if (started) return {date: started, label: "Turn started"};
  }
  if (item.type === "agentMessage" && messages.at(-1) === item && ["completed", "failed", "interrupted"].includes(turn.status)) {
    const ended = date(turn.completedAt ?? turn.completed_at);
    if (ended) return {date: ended, label: "Turn ended"};
  }
  return null;
}
let messageTimeFormats;
function messageTimeLabel(item, turn) {
  const timing = messageTimestamp(item, turn);
  if (!timing) return node("span", "Time unavailable", "message-time");
  messageTimeFormats ||= {
    label: new Intl.DateTimeFormat([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}),
    title: new Intl.DateTimeFormat([], {year: "numeric", month: "long", day: "numeric", hour: "numeric",
      minute: "2-digit", second: "2-digit", timeZoneName: "short"}),
  };
  const label = node("time", messageTimeFormats.label.format(timing.date), "message-time");
  label.dateTime = timing.date.toISOString();
  label.title = timing.label + " · " + messageTimeFormats.title.format(timing.date);
  return label;
}
function forkRelations(work) {
  const relations = [];
  if (work?.forked_from) relations.push({label: "Forked from", thread: work.forked_from, turnId: work.forked_from_turn_id, createdAt: work.created_at});
  for (const thread of work?.forked_sessions || []) relations.push({label: "Forked session", thread, turnId: thread.forked_from_turn_id, createdAt: thread.created_at});
  return relations;
}
function forkBoundary(relation, turns) {
  if (!relation.turnId) return -1;
  const index = turns.findIndex(turn => turn.id === relation.turnId);
  if (index >= 0) return index;
  // Native history can restore different turn IDs. The child's creation time
  // still separates inherited history from turns started after the fork.
  const timestamp = value => {
    if (value == null || value === "" || typeof value === "boolean") return NaN;
    return typeof value === "number" ? (value > 1e12 ? value / 1000 : value) : Date.parse(value) / 1000;
  };
  const created = timestamp(relation.createdAt);
  if (Number.isFinite(created)) {
    const starts = turns.map(turn => timestamp(turn.startedAt ?? turn.started_at));
    const next = starts.findIndex(start => start > created);
    if (next >= 0) return next - 1;
    if (starts.some(Number.isFinite)) return turns.length - 1;
  }
  return null;
}
function forkLink(thread) {
  const link = node("a", thread.title);
  link.href = "#" + encodeURIComponent(thread.thread_id);
  link.onclick = event => {
    if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); action(() => select(thread.thread_id));
  };
  return link;
}
function forkLinks(work) {
  const links = node("div", null, "fork-links");
  for (const relation of forkRelations(work)) {
    const row = node("p");
    row.append(relation.label + " ", forkLink(relation.thread)); links.append(row);
  }
  return links;
}
function forkMarker(relation) {
  const marker = node("p", null, "fork-marker");
  marker.append(node("span", relation.label + " "), forkLink(relation.thread));
  return marker;
}
function timelineDisclosures(id = state.selected) {
  const entry = conversationEntry(id);
  if (!entry.disclosures) {
    try { entry.disclosures = new Set(JSON.parse(sessionStorage.getItem("timeline-disclosures:" + id) || "[]")); }
    catch { entry.disclosures = new Set(); }
  }
  return entry.disclosures;
}
function rememberDisclosure(detail, open, thread = state.selected) {
  if (detail.open) open.add(detail.dataset.id); else open.delete(detail.dataset.id);
  try { sessionStorage.setItem("timeline-disclosures:" + thread, JSON.stringify([...open])); }
  catch { /* Expansion still survives updates when storage is unavailable. */ }
}
// A sent message appears on Enter, before the backend answers. It reads
// "Sending…" until acceptance and stays until the conversation shows the
// provider's own copy; a rejected message stays visibly marked as not sent.
class OutgoingMessages {
  constructor({itemText}) {
    this.itemText = itemText;
    this.threads = new Map();
    this.sequence = 0;
    this.transferStates = new Map();
    this.pendingTransferCounts = new Map();
    this.acceptedRequests = new Map();
    this.consumedItems = new Map();
  }

  list(id) { return this.threads.get(id) || []; }

  itemKey(turnId, itemId) { return JSON.stringify([turnId, itemId]); }

  // Retain reconciliation evidence across echo lifetimes, but only for cached
  // conversations or unresolved input. Reopening restores durable receipts.
  release(id) {
    if (this.list(id).length) return;
    this.consumedItems.delete(id);
    this.acceptedRequests.delete(id);
  }

  // Dispatcher requests settle by their durable request ID; other messages
  // settle when a user message that was not already present carries the text.
  add(id, {text, images = 0, requestId = null, submissionId = null, detail = null, transfer = false, preserveFailures = false}) {
    const known = new Set();
    for (const turn of detail?.thread?.turns || []) {
      for (const item of turn.items || []) if (item.type === "userMessage") known.add(this.itemKey(turn.id, item.id));
    }
    const entry = {key: "outgoing-" + ++this.sequence, text, images, match: text.trim(),
      requestId, submissionId, known, status: "sending", error: "", transfer};
    this.threads.set(id, [...this.list(id).filter(item => preserveFailures || transfer || item.transfer || item.status !== "failed"), entry]);
    return entry;
  }

  accepted(entry) { if (!entry.transfer) entry.status = "sent"; }

  // A completed composer receipt is an acknowledgement, not a provider item.
  // Keep it separate from history until the canonical item follows its anchor.
  confirm(id, records) {
    if (!Array.isArray(records)) return false;
    const seen = this.acceptedRequests.get(id) || new Set();
    this.acceptedRequests.set(id, seen);
    let changed = false;
    for (const record of records) {
      if (record?.threadId !== id || typeof record.requestId !== "string" || !record.requestId ||
          typeof record.turnId !== "string" || !record.turnId || typeof record.message !== "string" ||
          !Number.isInteger(record.imageCount) || record.imageCount < 0 ||
          (record.afterItemId !== null && typeof record.afterItemId !== "string")) continue;
      this.onReceipt?.(id, record.requestId);
      if (seen.has(record.requestId)) continue;
      seen.add(record.requestId);
      const entry = this.list(id).find(row => row.submissionId === record.requestId) ||
        this.add(id, {text: record.message, images: record.imageCount, submissionId: record.requestId, preserveFailures: true});
      entry.acceptance = record;
      entry.needsHistoryBaseline = false;
      entry.status = "sent";
      changed = true;
    }
    return changed;
  }

  // A native timeline item and a coordinator-transfer receipt have distinct
  // identities. Update local and remote copies of one intake by its request ID.
  transfers(id, records, detail) {
    if (!Array.isArray(records) || !records.length) return false;
    const states = this.transferStates.get(id) || new Map();
    this.transferStates.set(id, states);
    let changed = false;
    for (const record of records) {
      if (!record || record.threadId !== id || typeof record.requestId !== "string" || !record.requestId ||
          typeof record.message !== "string" || !["queued", "uncertain", "committed", "failed"].includes(record.state)) continue;
      const previous = states.get(record.requestId);
      if (previous === record.state || previous === "committed" || previous === "failed" ||
          (previous === "uncertain" && record.state === "queued")) continue;
      const wasPending = previous === "queued" || previous === "uncertain";
      const isPending = record.state === "queued" || record.state === "uncertain";
      if (isPending !== wasPending) {
        const count = (this.pendingTransferCounts.get(id) || 0) + (isPending ? 1 : -1);
        if (count) this.pendingTransferCounts.set(id, count);
        else this.pendingTransferCounts.delete(id);
      }
      states.set(record.requestId, record.state);
      this.onReceipt?.(id, record.requestId);
      let entry = this.list(id).find(row => row.submissionId === record.requestId);
      // A new or reconnected window uses ordinary history for past deliveries.
      if (!entry && record.state === "committed") continue;
      entry ||= this.add(id, {submissionId: record.requestId, text: record.message, images: record.imageCount || 0,
        detail, transfer: true});
      entry.transfer = true;
      entry.transferSequence = record.sequence;
      entry.transferReceipt = record;
      // An SSE receipt can precede the first history snapshot for this thread.
      if (!detail && entry.status === "sending") entry.needsHistoryBaseline = true;
      entry.status = {queued: "queued", uncertain: "uncertain", committed: "sent", failed: "failed"}[record.state];
      if (entry.status === "failed") entry.error = record.reason === "request_id_conflict"
        ? "This request ID was already used." : "The backend rejected the message.";
      changed = true;
    }
    if (changed) {
      const entries = this.list(id);
      const ordered = entries.filter(entry => entry.transfer).sort((a, b) => a.transferSequence - b.transferSequence);
      let next = 0;
      this.threads.set(id, entries.map(entry => entry.transfer ? ordered[next++] : entry));
    }
    return changed;
  }

  rejected(entry, message) { entry.status = "failed"; entry.error = message; }

  // The provider's copy also settles a message whose acceptance reply is still
  // in flight: its stream event can arrive first.
  settle(id, detail) {
    this.confirm(id, detail?.acceptedMessages);
    const entries = this.list(id);
    if (!entries.length || !detail) return;
    const consumed = this.consumedItems.get(id) || new Map();
    this.consumedItems.set(id, consumed);
    const positions = new Map();
    const messages = (detail.thread?.turns || []).flatMap(turn => {
      const items = (turn.items || []).filter(item => item.type === "userMessage");
      positions.set(turn.id, new Map((turn.items || []).map((item, index) => [item.id, index])));
      return items.map(item => ({...item, turnId: turn.id}));
    });
    const remaining = entries.filter(entry => {
      if (entry.needsHistoryBaseline) {
        // A cold window may already contain older identical messages. Never
        // count its initial history as evidence of a new delivery.
        if (entry.status === "sent" && detail.transferReadSource === "serving") return false;
        for (const item of messages) entry.known.add(this.itemKey(item.turnId, item.id));
        entry.needsHistoryBaseline = false;
      }
      if (entry.status === "failed" || (entry.transfer && (entry.status !== "sent" || detail.transferReadSource === "saved")) ||
          (entry.submissionId && ["sending", "uncertain"].includes(entry.status) && !entry.acceptance)) return true;
      if (entry.queueId && detail.queuedMessages?.some(row => row.id === entry.queueId)) return false;
      if (entry.requestId) return !(detail.dispatchRequests || []).some(request => request.request_id === entry.requestId);
      const match = messages.find(item => {
        if (entry.known.has(this.itemKey(item.turnId, item.id)) || consumed.get(item.turnId)?.has(item.id)) return false;
        if (entry.acceptance) {
          if (item.turnId !== entry.acceptance.turnId) return false;
          const anchor = entry.acceptance.afterItemId;
          const indices = positions.get(item.turnId);
          if (anchor !== null && (!indices.has(anchor) || indices.get(item.id) <= indices.get(anchor))) return false;
        }
        const images = (item.content || []).filter(part => ["image", "localImage"].includes(part.type)).length;
        if (images !== entry.images) return false;
        const raw = this.itemText({...item, content: (item.content || []).filter(part => part.type === "text")});
        const text = (globalThis.ThreadMentions?.displayMessage(raw).text ?? raw).trim();
        return text === entry.match || !!entry.match && text.endsWith("\n\n" + entry.match);
      });
      if (!match) return true;
      // Each provider message consumes one echo, including across repeated
      // snapshots and receipts arriving after an earlier echo was removed.
      const turnItems = consumed.get(match.turnId) || new Set();
      turnItems.add(match.id);
      consumed.set(match.turnId, turnItems);
      if (entry.submissionId) {
        // A gateway acknowledgement can settle the sender's echo before the
        // completed receipt event arrives. That event is the same submission.
        const accepted = this.acceptedRequests.get(id) || new Set();
        accepted.add(entry.submissionId);
        this.acceptedRequests.set(id, accepted);
      }
      for (const pending of entries) if (!pending.acceptance || pending.acceptance.turnId === match.turnId) pending.known.add(this.itemKey(match.turnId, match.id));
      return false;
    });
    if (remaining.length) this.threads.set(id, remaining); else this.threads.delete(id);
  }

  node(document, entry) {
    const message = document.createElement("article");
    message.className = "message user outgoing";
    message.dataset.outgoing = entry.key;
    message.dataset.status = entry.status;
    message.setAttribute("aria-label", "You");
    const body = document.createElement("div");
    body.className = "text";
    body.textContent = entry.text;
    if (entry.images) {
      const images = document.createElement("p");
      images.className = "outgoing-images";
      images.textContent = entry.images === 1 ? "1 image" : entry.images + " images";
      body.append(images);
    }
    const status = document.createElement("span");
    status.className = "message-time outgoing-status";
    status.setAttribute("role", "status");
    const statusText = {sent: "Sent", sending: "Sending…", queued: "Queued during update",
      uncertain: "Delivery unconfirmed", failed: "Not sent · " + entry.error};
    status.textContent = statusText[entry.status];
    message.append(body, status);
    if (entry.retry && entry.status === "uncertain") {
      const retry = document.createElement("button");
      retry.className = "quiet"; retry.type = "button"; retry.textContent = "Check delivery";
      retry.onclick = entry.retry; message.append(retry);
    }
    return message;
  }

  append(document, container, id) {
    for (const entry of this.list(id)) container.append(this.node(document, entry));
  }

  // Show a change in place, without rebuilding the conversation.
  update(document, timeline, id, detail) {
    this.settle(id, detail);
    for (const old of timeline.querySelectorAll("article[data-outgoing]")) old.remove();
    if (this.list(id).length) timeline.querySelector(":scope > p.empty:only-child")?.remove();
    this.append(document, timeline, id);
  }
}
// The normal provider stream invalidates these private, thread-scoped reads.
class UpdateSubmissionChanges {
  constructor({load, receive}) {
    this.load = load;
    this.receive = receive;
    this.cursors = new Map();
    this.selection = null;
  }

  select(id) {
    if (this.selection?.id === id) return;
    if (this.selection) {
      clearTimeout(this.selection.retryTimer);
      this.selection.controller?.abort();
    }
    this.selection = id && id !== "new-session" ? {id, dirty: false, retries: 0} : null;
    this.refresh();
  }

  invalidate(watchKeys) {
    if (this.selection?.watchKey && Array.isArray(watchKeys) && !watchKeys.includes(this.selection.watchKey)) return;
    return this.refresh();
  }

  refresh(retry = false) {
    const selected = this.selection;
    if (!selected) return;
    clearTimeout(selected.retryTimer);
    selected.retryTimer = null;
    if (!retry) selected.retries = 0;
    selected.dirty = true;
    if (!selected.pending) selected.pending = this.read(selected).finally(() => {
      selected.pending = null;
      if (this.selection === selected && selected.dirty && !selected.retryTimer) this.refresh();
    });
    return selected.pending;
  }

  async read(selected) {
    while (selected === this.selection && selected.dirty) {
      selected.dirty = false;
      const cursor = this.cursors.get(selected.id) || 0;
      const controller = selected.controller = new AbortController();
      const deadline = setTimeout(() => controller.abort(), 6000);
      try {
        const page = await this.load(selected.id, cursor, controller.signal);
        if (this.selection !== selected) return;
        if (page?.threadId !== selected.id || !Array.isArray(page.submissions) ||
            !Number.isSafeInteger(page.cursor) || page.cursor < cursor || typeof page.more !== "boolean" ||
            ((page.more || page.submissions.length) && page.cursor === cursor)) {
          throw new Error("Invalid update receipt page.");
        }
        selected.watchKey = page.watchKey;
        this.receive(selected.id, page.submissions);
        this.cursors.set(selected.id, page.cursor);
        selected.retries = 0;
        selected.dirty ||= page.more;
      } catch (error) {
        // A transient failure can consume the notification for this revision.
        // Retry that read only; idle windows do not poll.
        selected.dirty = false;
        if (this.selection === selected && selected.retries < 3 &&
            (error.status == null || error.status >= 500)) {
          selected.retryTimer = setTimeout(() => this.refresh(true), 500 * 2 ** selected.retries++);
        }
        return;
      } finally {
        clearTimeout(deadline);
        selected.controller = null;
      }
    }
  }
}
const TIMELINE_WINDOW_TURNS = 40;
// Read this conversation's complete history once. A failed read keeps the
// recent view and the control, so the reader can retry.
async function loadCompleteHistory(button, loadingText) {
  const id = state.selected, entry = conversationEntry(id), text = button.textContent;
  entry.completeHistory = true;
  button.disabled = true; button.textContent = loadingText;
  try {
    await entry.pending?.catch(() => {}); // A read already in flight may carry only the recent view.
    if (state.selected === id) await refreshDetail();
  } catch (error) {
    button.disabled = false; button.textContent = text;
    showError(error);
  }
}
async function readConversationContent(button, loadingText, read) {
  const id = state.selected, entry = conversationEntry(id), text = button.textContent;
  const focused = document.activeElement === button;
  button.disabled = true; button.textContent = loadingText; button.setAttribute("aria-busy", "true");
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
  try {
    await read(id, entry, {signal: controller.signal}, focused);
  } catch (error) {
    if (id === state.selected && state.conversations?.get(id) === entry) {
      showError(controller.signal.aborted ? new Error("Reading this conversation timed out. Try again.") : error);
    }
  } finally {
    clearTimeout(timeout);
    button.disabled = false; button.textContent = text; button.setAttribute("aria-busy", "false");
  }
}
function renderConversationContent(id, entry, button, {prepend = false, turnId, itemId, focus} = {}) {
  entry.nodes = null; entry.revision = (entry.revision || 0) + 1;
  if (id !== state.selected || state.conversations.get(id) !== entry) return;
  const el = $("timeline"), focused = focus ?? document.activeElement === button;
  const fromBottom = el.scrollHeight - el.scrollTop;
  const scroller = state.timelineScroll?.scroller();
  const viewport = scroller?.getBoundingClientRect();
  // The earlier-turn button is replaced by a different page's button. Anchor
  // to the first visible retained row instead of that disappearing control.
  const anchor = prepend && viewport ? [...el.children].find(child => child !== button &&
    child.getBoundingClientRect().bottom > viewport.top && child.getBoundingClientRect().top < viewport.bottom) : null;
  const offset = anchor?.getBoundingClientRect().top;
  state.timelineScroll?.hold();
  if (anchor?.dataset?.viewportKey && entry.viewport) entry.viewport.anchorKey = anchor.dataset.viewportKey;
  state.detail = entry.detail;
  renderTimeline();
  if (anchor?.isConnected) state.timelineScroll.restore(scroller.scrollTop + anchor.getBoundingClientRect().top - offset);
  if (!state.timelineScroll && prepend) el.scrollTop = el.scrollHeight - fromBottom;
  if (focused) {
    const replacement = prepend ? el.querySelector?.(".timeline-earlier") :
      entry.toolNodes?.get(JSON.stringify([turnId, itemId]))?.node.querySelector?.("summary");
    (replacement || el).focus?.({preventScroll: true});
  }
}
async function loadEarlierHistory(button) {
  const entry = conversationEntry(state.selected);
  if (entry.historyPending) return entry.historyPending;
  entry.historyPending = readConversationContent(button, "Loading earlier turns…", async (id, entry, options, focused) => {
    const before = entry.detail?.thread.beforeSeq ?? state.detail?.thread.beforeSeq;
    if (!Number.isSafeInteger(before) || before <= 0) throw new Error("Reopen this conversation to read its earlier turns.");
    const result = await api(sessionPath(id) + "?before_seq=" + before + "&limit=40", undefined, options);
    const page = historyPage(result, id, before);
    entry.pageBytes = (entry.pageBytes || 0) + (result.snapshotBytes || 0);
    entry.retainedBytes = (entry.retainedBytes || 0) + (result.snapshotBytes || 0);
    if (state.conversations.get(id) !== entry) return;
    // A recent refresh can finish while the older page is being read.
    if (entry.detail.thread.beforeSeq > before) {
      const revision = entry.revision, epoch = state.config?.conversationRevisionEpoch, initialSequence = entry.lastEventSeq;
      const thread = await bridgeHistoryWindow(id, entry.detail.thread, page.turns.at(-1)?.id, options);
      if (state.conversations.get(id) !== entry || entry.revision !== revision) return;
      entry.detail.thread = thread;
    }
    entry.detail.thread = mergeHistoryWindow(entry, page, true);
    entry.historyLoaded = entry.allTurns = true;
    renderConversationContent(id, entry, button, {prepend: true, focus: focused});
  }).finally(() => { entry.historyPending = null; });
  return entry.historyPending;
}
function earlierTurnsControl(count) {
  const button = node("button", "Show " + count + " earlier " + (count === 1 ? "turn" : "turns"), "quiet timeline-earlier");
  button.type = "button";
  button.onclick = async () => {
    if (state.config?.conversationHistoryPages && state.detail?.thread?.earlierTurns) return loadEarlierHistory(button);
    // Keep the reader's place while earlier turns are inserted above it.
    const el = $("timeline"), fromBottom = el.scrollHeight - el.scrollTop;
    conversationEntry(state.selected).allTurns = true;
    if (state.detail?.thread?.earlierTurns) await loadCompleteHistory(button, "Loading earlier turns…");
    else renderTimeline();
    if (state.timelineScroll) state.timelineScroll.restore(el.scrollHeight - fromBottom);
    else el.scrollTop = el.scrollHeight - fromBottom;
  };
  return button;
}
function completeOutputControl(turn, item) {
  const button = node("button", "Show full output", "quiet tool-complete");
  button.type = "button";
  button.onclick = () => {
    if (!state.config?.conversationHistoryPages) return loadCompleteHistory(button, "Loading full output…");
    return readConversationContent(button, "Loading full output…", async (id, entry, options, focused) => {
      const preview = JSON.stringify(item);
      const result = await api(sessionPath(id) + "/turns/" + encodeURIComponent(turn.id) + "/full", undefined, options);
      if (state.conversations.get(id) !== entry) return;
      entry.expandedBytes = (entry.expandedBytes || 0) + (result.snapshotBytes || 0);
      entry.retainedBytes = (entry.retainedBytes || 0) + (result.snapshotBytes || 0);
      const current = entry.detail?.thread.turns.find(current => current.id === turn.id);
      const index = current?.items.findIndex(current => current.id === item.id) ?? -1;
      if (index < 0 || JSON.stringify(current.items[index]) !== preview) return;
      const full = result?.turn?.id === turn.id && result.turn.items?.find(current => current.id === item.id);
      if (!full || full.abridged) throw new Error("Could not read the full tool output. Try again.");
      // A turn read can contain many large outputs. Retain only the item asked
      // for; siblings keep their compact previews and independent controls.
      (entry.expandedOutput ||= new Map()).set(JSON.stringify([turn.id, item.id]), {preview, item: full});
      current.items[index] = full;
      entry.historyLoaded = true;
      renderConversationContent(id, entry, button, {turnId: turn.id, itemId: item.id, focus: focused});
    });
  };
  return button;
}
function renderTimeline() { return renderConversationTimeline(); }
function renderConversationTimeline(pane = null) {
  const view = pane?.view || state;
  const repaint = () => renderConversationTimeline(pane);

  if (view.responseActions?.deferRender(view.selected, repaint)) return;
  if (view.timelineRender) { clearTimeout(view.timelineRender.timer); view.timelineRender = null; }
  const el = pane?.timeline || $("timeline");
  const atBottom = view.timelineScroll?.beforeRender() ?? (el.scrollHeight - el.scrollTop - el.clientHeight < 100);
  const open = timelineDisclosures(view.selected);
  if (view.timelineThread === view.selected) {
    for (const detail of el.querySelectorAll("details[data-id]")) {
      if (detail.open) open.add(detail.dataset.id); else open.delete(detail.dataset.id);
    }
    try { sessionStorage.setItem("timeline-disclosures:" + view.selected, JSON.stringify([...open])); }
    catch { /* Expansion still survives updates when storage is unavailable. */ }
  }
  view.timelineThread = view.selected;
  const fragment = globalThis.ResponseActions?.buffer() || document.createDocumentFragment();
  if (view.detail?.transferReadSource === "saved") {
    const notice = node("p", "Showing saved conversation during update.", "empty");
    notice.setAttribute("role", "status");
    fragment.append(notice);
  }
  if (view.detail?.thread.historyUnavailable) {
    const notice = node("p", "Conversation history couldn’t be fully loaded. Reload to try again.", "empty");
    notice.setAttribute("role", "status");
    fragment.append(notice);
  }
  const messages = (view.detail?.coordinationMessages || []).filter(message => !message.thread_id?.startsWith("event:review:"));
  const sourceTurns = globalThis.ReviewUI ? ReviewUI.turns(view.detail?.thread.turns || [], view.detail?.reviews || [], view.selected) : view.detail?.thread.turns || [];
  const turns = globalThis.DispatchUI && (view.detail?.work_thread?.dispatcher || view.detail?.dispatchRequests?.length)
    ? DispatchUI.turns(sourceTurns, view.detail.dispatchRequests || []) : sourceTurns;
  const allEntries = messages.length ? AgentMessages.entries(turns, messages) : turns.map(turn => ({turn}));
  // A card keeps the exact delivery prompt its agent received; the prompt itself is not repeated as a user message.
  const deliveredPrompts = messages.length ? AgentMessages.deliveredPrompts(turns) : new Map();
  const titleOf = id => view.sessions?.find(thread => thread.thread_id === id)?.title ||
    messages.find(message => message.counterpart?.session_id === id)?.counterpart.title;
  const turnIndexes = new Map(turns.map((turn, index) => [turn, index]));
  const pendingForks = new Set(forkRelations(view.detail?.work_thread).map(relation =>
    ({...relation, boundary: forkBoundary(relation, turns)})));
  const appendForks = (boundary, remaining = false) => {
    for (const relation of pendingForks) if (remaining || relation.boundary === boundary) {
      fragment.append(forkMarker(relation)); pendingForks.delete(relation);
    }
  };
  const workGroups = new Map();
  const conversation = pane?.visual || conversationEntry(view.selected);
  // A long conversation first renders its latest turns: rendering every message
  // delayed opening it. Earlier turns render on request, or when a search
  // result or message link points into the conversation.
  // A target keeps every turn rendered after it has been shown.
  if (view.searchTarget?.thread === view.selected || view.messageTarget?.thread === view.selected) conversation.allTurns = true;
  const omitted = view.detail?.thread?.earlierTurns || 0;
  const hidden = conversation.allTurns ? 0 : Math.max(0, turns.length - TIMELINE_WINDOW_TURNS);
  const windowed = omitted > 0 || hidden > 0;
  const firstShown = windowed ? Math.max(0, allEntries.findIndex(entry => entry.turn === turns[hidden])) : 0;
  let entries = firstShown > 0 ? allEntries.slice(firstShown) : allEntries;
  if (typeof ConversationViewport !== "undefined" && view.timelineScroll && entries.length) {
    const id = view.selected;
    conversation.viewport ||= new ConversationViewport(el, view.timelineScroll.scroller(), () => {
      if (view.selected === id && view.timelineThread === id) repaint();
    });
    const searchTurn = view.searchTarget?.thread === id && view.searchTarget.turn;
    const messageId = view.messageTarget?.thread === id && view.messageTarget.id;
    const messageIndex = messageId && entries.findIndex(entry => String(entry.message?.id) === String(messageId));
    const messageTurn = Number.isInteger(messageIndex) && messageIndex >= 0 &&
      (entries[messageIndex].message.turn_id || entries.slice(0, messageIndex).reverse().find(entry => entry.turn)?.turn.id || "message-" + messageId);
    entries = conversation.viewport.window(entries, atBottom, searchTurn || messageTurn);
  }
  if (windowed) {
    // Markers belong to their history, including when that history is hidden
    // or has not been loaded. The context panel retains every fork link.
    for (const relation of pendingForks) {
      if (relation.boundary !== null && relation.boundary < hidden || omitted > 0 && relation.boundary === null)
        pendingForks.delete(relation);
    }
    fragment.append(pane ? pane.earlierControl(omitted + hidden) : earlierTurnsControl(omitted + hidden));
  }
  appendForks(-1);
  const previousTools = conversation.toolNodes || new Map(), toolNodes = new Map();
  const previousMessages = conversation.messageNodes || new Map(), messageNodes = new Map();
  let previousMessageMinute = null;
  const client = view.detail?.work_thread?.client;
  const disclosureListeners = conversation.disclosureListeners ||= new WeakSet();
  for (const entry of entries) {
    if (entry.spacer) {
      const spacer = node("div");
      spacer.setAttribute("aria-hidden", "true");
      spacer.style.height = entry.spacer + "px";
      spacer.style.flexShrink = "0";
      fragment.append(spacer);
      continue;
    }
    const append = element => {
      if (entry.viewportKey) element.dataset.viewportKey = entry.viewportKey;
      fragment.append(element);
    };
    if (entry.message) {
      append(AgentMessages.card(document, entry.message, {open: openAgentMessage, expanded: open.has("agent-message:" + entry.message.id),
        prompt: deliveredPrompts.get(entry.message.id), titleOf}));
      continue;
    }
    const turn = entry.turn;
    for (const item of entry.item ? [entry.item] : entry.end ? [] : turn.items || []) {
      if (item.type === "reviewReceipt") {
        append(ReviewUI.card(document, item.review, {render: text => messageMarkdown.render(text),
          open: id => select(id).catch(showError), expanded: open.has("review:" + item.review.id)}));
        continue;
      }
      if (item.type === "dispatchReceipt") {
        append(DispatchUI.card(document, item.receipt, {open: id => select(id).catch(showError),
          dismiss: id => action(async () => { await api(sessionPath(view.selected) + "/dispatch", {id, action: "dismiss"}); await refreshDetail(); })}));
        continue;
      }
      const automatic = globalThis.AgentMessages?.automaticPrompt(item);
      if (automatic && AgentMessages.represented(automatic, turn, messages)) continue;
      if (automatic) {
        // Runtime-generated prompts are system rows, not the user's words. Reuse
        // the row across streaming renders so an open popover stays open.
        const key = JSON.stringify(["automatic", turn.id ?? turns.indexOf(turn), item.id ?? (turn.items || []).indexOf(item)]);
        const time = messageTimeLabel(item, turn), signature = JSON.stringify([item, time.outerHTML]);
        const reused = previousMessages.get(key);
        const row = reused?.signature === signature ? reused.node : AgentMessages.promptRow(document, automatic, {time, titleOf});
        messageNodes.set(key, {signature, node: row});
        append(row);
        continue;
      }
      if (item.type === "userMessage" || item.type === "agentMessage") {
        const time = messageTimeLabel(item, turn);
        const minute = time.dateTime?.slice(0, 16) ?? null;
        time.hidden = minute !== null && minute === previousMessageMinute;
        previousMessageMinute = minute;
        // A stream changes one message at a time. Rebuilding every message,
        // with its Markdown, for each chunk kept the main thread busy and
        // delayed typing in long conversations; reuse unchanged messages.
        const key = JSON.stringify([turn.id ?? turns.indexOf(turn), item.id ?? (turn.items || []).indexOf(item)]);
        const signature = item;
        const reused = previousMessages.get(key);
        if (reused?.signature === signature && reused.version === item.renderRevision && reused.client === client) {
          if (reused.node.dataset.openuiKey)
            reused.node.dataset.openuiStreaming = String(!["completed", "failed", "interrupted"].includes(turn.status));
          const previousTime = reused.node.querySelector?.(":scope > .message-time");
          if (previousTime && previousTime.outerHTML !== time.outerHTML) previousTime.replaceWith(time);
          messageNodes.set(key, reused);
          append(reused.node);
          continue;
        }
        const text = itemText(item);
        const message = node("article", null, "message " + (item.type === "userMessage" ? "user" : "agent"));
        messageNodes.set(key, {signature, version: item.renderRevision, client, node: message});
        message.dataset.searchTurn = turn.id || "search-turn-" + turns.indexOf(turn);
        message.dataset.searchItem = item.id || "search-item-" + (turn.items || []).indexOf(item);
        message.tabIndex = -1;
        const body = node("div", null, item.type === "agentMessage" ? "text markdown" : "text");
        if (item.type === "agentMessage") {
          body.innerHTML = messageMarkdown.render(text);
          globalThis.ResponseActions?.enhance(body);
          if (body.querySelector?.(".openui-block")) {
            message.dataset.openuiKey = JSON.stringify([view.selected, turn.id ?? turns.indexOf(turn), item.id ?? (turn.items || []).indexOf(item)]);
            message.dataset.openuiText = text;
            message.dataset.openuiStreaming = String(!["completed", "failed", "interrupted"].includes(turn.status));
          }
        }
        else {
          const display = globalThis.ThreadMentions?.displayMessage(text) || {text, refs: []};
          body.textContent = display.text;
          if (display.refs.length) {
            const references = node("div", null, "message-thread-references");
            for (const ref of display.refs) {
              if (ref.kind === "file") {
                const link = node("a", ref.text + " · File");link.href = "#";
                link.onclick = event => {event.preventDefault();openMentionFile(ref).catch(showError);};
                references.append(link);continue;
              }
              const link = node("a", ref.text + " · " + ref.session_id.slice(-8));
              link.href = "/#" + encodeURIComponent(ref.session_id);
              link.setAttribute("aria-label", "Thread reference " + ref.text + " " + ref.session_id);
              link.onclick = event => {
                if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                event.preventDefault(); select(ref.session_id).catch(showError);
              };
              references.append(link);
            }
            body.append(references);
          }
          const images = (item.content || []).filter(c => c.type === "image");
          if (images.length) ChatImageAttachments.appendPreviews(document, body, images);
        }
        message.setAttribute("aria-label", item.type === "userMessage" ? "You" : (view.detail?.work_thread?.client === "muse" ? "Muse Code" : view.detail?.work_thread?.client === "kimi" ? "Kimi" : view.detail?.work_thread?.client === "antigravity" ? "Antigravity" : view.detail?.work_thread?.client === "claude" ? "Claude" : "Codex"));
        message.append(body, time);
        append(message);
      } else {
        const text = itemText(item);
        // Streaming changes one item at a time. Reuse unchanged tool rows,
        // including their disclosure state, instead of churning the full history.
        const key = JSON.stringify([turn.id ?? turns.indexOf(turn), item.id ?? (turn.items || []).indexOf(item)]);
        const signature = item, cached = previousTools.get(key);
        const detail = cached?.signature === signature && cached.version === item.renderRevision ? cached.node : node("details", null, "tool");
        toolNodes.set(key, {signature, version: item.renderRevision, node: detail});
        detail.dataset.id = item.id; detail.dataset.status = item.status || ""; detail.open = open.has(item.id);
        if (detail !== cached?.node) {
          const labels = {fileChange: "File changes", reasoning: "Thinking", mcpToolCall: "Tool", plan: "Plan", webSearch: "Web search", contextCompaction: "Conversation compaction"};
          const terminal = item.type === "commandExecution" || item.type === "mcpToolCall" && ["Bash", "exec_command", "shell_command"].includes(item.tool);
          const command = String(item.command || item.arguments?.command || item.arguments?.cmd || "");
          const shell = command.match(/^((?:\/[^\s]+\/)?(?:bash|zsh|sh|fish|pwsh))(?:\s|$)/)?.[1];
          const label = terminal ? "Terminal" + (shell ? " (cmd: " + shell + ")" : "") : item.tool || labels[item.type] || item.type;
          const preview = (command || (item.type === "fileChange" ? (item.changes || []).map(c => c.path).join(", ") :
            item.type === "mcpToolCall" ? JSON.stringify(item.arguments || {}) : text)).replace(/\s+/g, " ");
          const summary = node("summary", null, "tool-summary");
          const icon = node("span", null, "tool-icon" + (terminal ? " terminal" : ""));
          icon.setAttribute("aria-hidden", "true");
          summary.append(icon, node("span", label, "tool-label"), node("span", preview, "tool-preview"));
          const statusLabel = item.status === "unknown" ? "Outcome unknown" : item.status;
          if (item.status === "unknown") summary.append(node("span", statusLabel, "tool-label"));
          summary.setAttribute("aria-label", [label, statusLabel, preview].filter(Boolean).join(" · "));
          summary.title = [label, statusLabel, item.statusReason, preview].filter(Boolean).join(" · ");
          detail.append(summary);
          if (item.statusReason) detail.append(node("p", item.statusReason));
          detail.append(node("pre", text));
          if (item.abridged) detail.append(pane ? pane.outputControl(turn, item) : completeOutputControl(turn, item));
        }
        if (["completed", "failed", "interrupted"].includes(turn.status)) {
          let group = workGroups.get(turn);
          if (!group) {
            group = node("details", null, "turn-work");
            group.dataset.id = "turn-work:" + (turn.id || turns.indexOf(turn));
            group.open = open.has(group.dataset.id);
            group.dataset.status = turn.status;
            const count = (turn.items || []).filter(i => !["userMessage", "agentMessage", "reasoning", "plan"].includes(i.type)).length;
            const summary = node("summary");
            const icon = node("span", null, "tool-icon"); icon.setAttribute("aria-hidden", "true");
            const unknown = (turn.items || []).filter(i => i.status === "unknown").length;
            const turnOutcome = turn.status === "failed" ? "Failed" : turn.status === "interrupted" ? "Stopped" : "Completed";
            const outcome = unknown ? "Turn " + turnOutcome.toLowerCase() + " · " + unknown +
              " tool outcome" + (unknown === 1 ? "" : "s") + " unknown" : turnOutcome;
            const completion = node("span", turn.status === "completed" ? (unknown ? "?" : "✓") : turn.status === "failed" ? "!" : "■", "work-state");
            completion.setAttribute("aria-label", outcome); completion.title = outcome;
            summary.append(icon, node("span", count ? "Tool calls · " + count : "Work", "work-label"), completion);
            group.append(summary);
            workGroups.set(turn, group);
            // Anchor at the first work item; prose and coordination cards retain their order.
            append(group);
          }
          group.append(detail);
        } else append(detail);
      }
    }
    if (!entry.item && turn.error) append(node("p", turn.error.message || "This turn failed.", "empty"));
    if (!entry.item && turn.status === "interrupted") append(node("p",
      turn !== turns.at(-1) || view.detail?.running ? "Earlier turn stopped." : "Turn stopped.", "empty"));
    if (!entry.item) appendForks(turnIndexes.get(turn));
  }
  // Retain navigation even when the provider cannot return the boundary turn.
  appendForks(null, true);
  if (view.outgoing) {
    view.outgoing.settle(view.selected, view.detail);
    for (const old of el.querySelectorAll("article[data-outgoing]")) old.remove();
    view.outgoing.append(document, fragment, view.selected);
  }
  // A turn may compact before emitting user or agent messages. Keep its live
  // state visible after the historical footer while the transcript catches up.
  const currentTurn = view.detail?.activeTurn ? turns.find(turn => turn.id === view.detail.activeTurn) : turns.at(-1);
  if (view.detail?.running && !(currentTurn?.items || []).some(item => item.type === "agentMessage")) {
    const working = node("p", "Working…", "empty");
    working.setAttribute("role", "status");
    fragment.append(working);
  }
  if (globalThis.OpenUIThread) {
    const thread = view.selected;
    OpenUIThread.commit(el, fragment, {
      thread,
      theme: document.documentElement?.dataset.theme || "light",
      disabled: !view.detail?.work_thread?.browser_session || view.detail.work_thread.attention === "archived" || view.closing?.has(thread),
      onAction: text => pane ? pane.submit(text, false) : sendOpenUIResponse(thread, text),
    });
  } else if (globalThis.ResponseActions) ResponseActions.commit(el, fragment);
  else el.replaceChildren(fragment);
  conversation.viewport?.observe();
  conversation.toolNodes = toolNodes;
  conversation.messageNodes = messageNodes;
  const disclosureThread = view.selected;
  for (const detail of el.querySelectorAll("details[data-id]")) {
    if (!disclosureListeners.has(detail)) {
      detail.addEventListener("toggle", () => rememberDisclosure(detail, open, disclosureThread));
      disclosureListeners.add(detail);
    }
  }
  if (view.detail?.work_thread?.browser_session === false) el.append(node("p", view.detail?.thread.historyError ||
    "Read-only conversation. Continue in its original app or terminal session, or use Continue in browser when available.", "empty"));
  else if (!el.childNodes.length) el.append(node("p", "This is the beginning of your session. What would you like to work on?", "empty"));
  view.responseActions?.update();
  if (view.timelineScroll) view.timelineScroll.afterRender(atBottom);
  else if (atBottom) el.scrollTop = el.scrollHeight;
  if (view.messageTarget && view.messageTarget.thread === view.selected) {
    if (pane) pane.focusMessage(view.messageTarget.id); else focusAgentMessage(view.messageTarget.id);
  }
  if (!pane && view.searchTarget?.thread === view.selected) focusSearchMessage(view.searchTarget);
}
async function sendOpenUIResponse(thread, text) {
  const detail = state.detail;
  if (thread !== state.selected || !detail?.work_thread?.browser_session || detail.work_thread.attention === "archived" || state.closing?.has(thread)) {
    throw new Error("Reopen this thread to send a response.");
  }
  const dispatcher = !!detail.work_thread.dispatcher;
  const body = {message: text};
  if (state.sourceWindowId) body.windowId = state.sourceWindowId;
  if (dispatcher) {
    body.askDispatcher = true;
    body.requestId = DispatchUI.requestKey(sessionStorage, thread, body);
  }
  await api(sessionPath(thread) + (!dispatcher && detail.running ? "/queue" : "/messages"), body);
  // A refresh failure must not invite resending an already accepted response.
  try { if (thread === state.selected) await refreshDetail(); await refreshList(); }
  catch (error) { console.warn("OpenUI response accepted before refresh failed", error); }
}
function focusSearchMessage(target) {
  if (target.thread !== state.selected) return false;
  state.searchTarget = target;
  const messages = [...$("timeline").querySelectorAll("[data-search-item]")];
  const found = messages.find(el => el.dataset.searchTurn === target.turn && el.dataset.searchItem === target.item);
  if (!found) return false;
  for (const el of messages) el.classList.toggle("search-target", el === found);
  found.scrollIntoView({block: "center"}); found.focus({preventScroll: true});
  state.timelineScroll?.restore($("timeline").scrollTop);
  state.searchTarget = null;
  return true;
}
// A search result can point before the latest turns. Its target makes the
// read complete; a read already in flight may still hold only the recent view.
async function openSearchTarget(route) {
  const target = route.item && {thread: route.id, turn: route.turn, item: route.item};
  if (target) state.searchTarget = target;
  await select(route.id);
  if (!target || state.selected !== route.id || focusSearchMessage(target)) return;
  if (state.detail?.thread?.view === "recent") {
    await conversationEntry(route.id).pending?.catch(() => {});
    state.searchTarget = target;
    await refreshDetail();
  }
  if (state.selected === route.id && !focusSearchMessage(target))
    throw new Error("The matching message is no longer available in this conversation's saved history.");
}
function focusAgentMessage(id) {
  if (!/^\d+$/.test(String(id))) return;
  state.messageTarget = {thread: state.selected, id};
  const card = document.getElementById("agent-message-" + id);
  if (!card) return;
  card.scrollIntoView({block: "center"});
  card.focus({preventScroll: true});
  state.timelineScroll?.restore($("timeline").scrollTop);
  state.messageTarget = null;
}
async function openAgentMessage(thread, id) {
  try {
    if (state.paneMode) { await window.parent.agentCoordOpenMessage(thread, id); return; }
    if (window.agentCoordPanes?.active) { await window.agentCoordPanes.openThread(thread, {messageId: id}); return; }
    state.messageTarget = {thread, id};
    await select(thread);
    if (state.selected === thread) focusAgentMessage(id);
  } catch (error) { showError(error); }
}
async function refreshAgentMessages() {
  const id = state.selected;
  if (!id || !state.detail || isSessionDraft()) return;
  const entry = conversationEntry(id), request = entry.messageRequest = (entry.messageRequest || 0) + 1;
  const result = await api(threadPath(id) + "/messages");
  if (state.selected !== id || state.conversations.get(id) !== entry || request !== entry.messageRequest) return;
  if (JSON.stringify(state.detail.coordinationMessages) === JSON.stringify(result.data)) return;
  state.detail.coordinationMessages = result.data;
  entry.detail = state.detail; entry.nodes = null;
  renderTimeline();
  state.dispatchDock?.update(state.detail);
}
function requestButton(label, decision, request, form, pane) {
  const id = pane?.id || state.selected;
  const button = node("button", label, decision === "accept" ? "primary" : "quiet"); button.type = "button";
  button.onclick = () => action(async () => {
    const ticket = state.rollUp?.ticket(id);
    const body = {decision};
    if (form) Object.assign(body, form());
    await api(sessionPath(id) + "/requests/" + encodeURIComponent(request.key), body);
    if (pane) await pane.refresh();
    else if (id === state.selected) await refreshDetail();
    await refreshList();
    await state.rollUp?.responded(ticket, {pending: !!state.detail?.requests?.length});
  }, button);
  return button;
}
function renderRequests() { return renderConversationRequests(); }
function renderConversationRequests(pane) {
  const area = pane?.requests || $("requests");
  const view = pane?.view || state;
  const control = (label, decision, request, form) => requestButton(label, decision, request, form, pane);
  if (view.detailRefreshing || view.conversations?.get(view.selected)?.requestsRefreshing) { area.replaceChildren(); delete area.dataset.signature; return; }
  // Preserve partially entered answers while unrelated events stream.
  const signature = JSON.stringify((view.detail?.requests || []).map(r => r.key));
  if (area.dataset.signature === signature) return;
  area.dataset.signature = signature; area.replaceChildren();
  for (const request of view.detail?.requests || []) {
    const box = node("section", null, "request"), params = request.params;
    const actions = node("div", null, "actions");
    if (request.method === "item/tool/requestUserInput") {
      box.append(node("h3", (view.detail?.work_thread?.client === "muse" ? "Muse Code" : view.detail?.work_thread?.client === "kimi" ? "Kimi" : view.detail?.work_thread?.client === "antigravity" ? "Antigravity" : view.detail?.work_thread?.client === "claude" ? "Claude" : "Codex") + " has a question"));
      const fields = [];
      for (const question of params.questions || []) {
        const label = node("label", question.question);
        const fixed = question.allowCustomAnswer === false && question.options?.length;
        const input = node(fixed ? "select" : "input");
        if (fixed) {
          input.multiple = !!question.multiSelect;
          for (const option of question.options) {
            const choice = node("option", option.label); choice.value = option.value || option.label; input.append(choice);
          }
        } else input.type = question.isSecret ? "password" : "text";
        if (!fixed && question.options?.length) {
          const options = node("datalist"); options.id = "options-" + (pane?.id || "main") + "-" + request.key + "-" + fields.length;
          for (const option of question.options) options.append(node("option", option.label));
          input.setAttribute("list", options.id); label.append(options);
        }
        label.append(input); box.append(label); fields.push([question.id, input]);
      }
      actions.append(control("Send answer", "accept", request, () => ({answers: Object.fromEntries(fields.map(([id, input]) => [id, {answers: input.multiple ? Array.from(input.selectedOptions, option => option.value) : [input.value]}]))})));
      if (params.allowCancel) actions.append(control("Cancel", "cancel", request));
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
      actions.append(control("Continue", "accept", request, () => ({content: Object.fromEntries(fields.map(([key, schema, input]) => [key, schema.type === "boolean" ? input.checked : ["number", "integer"].includes(schema.type) ? Number(input.value) : input.value]))})), control("Cancel", "cancel", request));
    } else if (["item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"].includes(request.method)) {
      box.append(node("h3", (view.detail?.work_thread?.client === "muse" ? "Muse Code" : view.detail?.work_thread?.client === "kimi" ? "Kimi" : view.detail?.work_thread?.client === "antigravity" ? "Antigravity" : view.detail?.work_thread?.client === "claude" ? "Claude" : "Codex") + " is requesting approval"));
      if (params.reason) box.append(node("p", params.reason));
      box.append(node("pre", params.command || (params.permissions || params.networkApprovalContext ? JSON.stringify(params.permissions || params.networkApprovalContext, null, 2) : params.grantRoot || "Apply the file changes shown above.")));
      const choices = params.availableDecisions || (request.method === "item/permissions/requestApproval" ? ["accept", "decline"] : ["accept", "acceptForSession", "decline", "cancel"]);
      const labels = {accept: "Allow once", acceptForSession: "Allow for session", decline: "Decline", cancel: "Cancel"};
      for (const decision of choices) if (typeof decision === "string" && labels[decision]) actions.append(control(labels[decision], decision, request));
    } else {
      box.append(node("h3", "This request is not supported yet"), node("p", "Cancel this request to continue."));
      actions.append(control("Cancel request", "cancel", request));
    }
    box.append(actions); area.append(box);
  }
}
function scheduleTimelineRender({userMessage = false} = {}) {
  const pending = state.timelineRender;
  if (pending?.id === state.selected && pending.detail === state.detail && (!userMessage || pending.userMessage)) return;
  if (pending) clearTimeout(pending.timer);
  const render = {id: state.selected, detail: state.detail, userMessage};
  state.timelineRender = render;
  const paint = () => {
    if (state.timelineRender !== render) return;
    state.timelineRender = null;
    if (state.selected !== render.id || state.detail !== render.detail) return;
    renderTimeline(); renderStatus();
  };
  // All user input in this event batch paints together immediately. Streamed
  // output still uses the bounded timer, including in hidden panes.
  if (userMessage) Promise.resolve().then(paint);
  else render.timer = setTimeout(paint, 50);
}
function applyEvent(event) {
  if (event.method === "browser/conversationInvalidated") { invalidateConversationMetadata(event.params); return; }
  if (event.method === "browser/schedules") { state.schedules?.refresh().catch(showError); return; }
  if (event.method === "browser/changed" && !event.params?.threadId) { refreshDecisionsAndSchedules(); scheduleList(); if (state.selected) scheduleDetail(); return; }
  if (event.method === "coordination/messageSent") { state.agentTreeController?.message(event.params); return; }
  if (event.method === "thread/closedWhenDone") { threadClosedWhenDone(event.params.threadId).catch(showError); return; }
  const cached = state.conversations?.get(event.params.threadId);
  if (cached && Number.isSafeInteger(event.seq)) {
    if (event.seq <= Math.max(cached.lastEventSeq ?? -1, cached.detail?.stream_seq ?? -1)) return;
    cached.lastEventSeq = event.seq;
  }
  if (event.method === "browser/messageAccepted") {
    const id = event.params.threadId;
    // Unopened conversations restore receipts on their next read. An evicted
    // conversation still needs acceptance while its outgoing send is unresolved.
    if (!cached && state.selected !== id && !state.outgoing?.list(id).length) return;
    const detail = cached?.detail || (state.selected === id ? state.detail : null);
    if (state.outgoing?.confirm(id, [event.params])) {
      if (cached) cached.revision++;
      if (state.selected === id && state.timelineThread === id)
        state.outgoing.update(document, $("timeline"), id, detail);
    }
    return;
  }
  if (event.method === "thread/usage/updated") {
    if (cached) cached.streamedUsage = true;
    if (cached?.detail?.work_thread) cached.detail.work_thread.usage = event.params.usage;
    if (state.detail?.work_thread?.thread_id === event.params.threadId) {
      state.detail.work_thread.usage = event.params.usage;
      renderStatus();
    }
    return;
  }
  const streamedItem = cached?.detail && !event.requestKey &&
    event.method.startsWith("item/") && event.params.turnId && (event.params.item?.id || event.params.itemId);
  if (cached) {
    if (streamedItem && cached.streamedItems) {
      const ids = cached.streamedItems.get(event.params.turnId) || new Set();
      ids.add(event.params.item?.id || event.params.itemId);
      cached.streamedItems.set(event.params.turnId, ids);
    } else if (!streamedItem) cached.revision++;
  }
  if (event.method === "browser/changed" && event.params.projectCwd) {
    for (const list of [state.sessions, state.viewThreads]) for (const thread of list || []) {
      if (thread.thread_id === event.params.threadId) Object.assign(thread, {project_cwd: event.params.projectCwd, cwd: event.params.projectCwd});
    }
    if (cached?.detail?.work_thread) Object.assign(cached.detail.work_thread, {project_cwd: event.params.projectCwd, cwd: event.params.projectCwd});
    if (state.selected === event.params.threadId && !state.paneMode && state.threadMoveSubmitting !== event.params.threadId) {
      select(event.params.threadId).catch(showError);
      return;
    }
  }
  if (!state.detail || event.params.threadId !== state.selected) {
    if (cached?.detail) {
      reduceConversationHistory(cached.detail, event);
      cached.nodes = null;
    }
    return;
  }
  const {method, params} = event;
  if (method === "coordination/messages") { refreshAgentMessages().catch(showError); return; }
  if (event.requestKey) { state.detail.requests.push({key: event.requestKey, method, params}); renderRequests(); renderStatus(); if (state.detailRefreshing) scheduleDetail(); return; }
  reduceConversationHistory(state.detail, event);
  if (method === "turn/started" || method === "turn/completed") renderTimeline();
  if (["turn/completed", "serverRequest/resolved", "browser/requests", "browser/changed"].includes(method)) scheduleDetail();
  if (!method.startsWith("item/") || !params.turnId) { renderStatus(); return; }
  scheduleTimelineRender({userMessage: params.item?.type === "userMessage"});
}
function reduceConversationHistory(detail, event) {
  const {method, params} = event;
  if (method === "turn/started") {
    detail.running = true; detail.activeTurn = params.turn.id;
    const turns = detail.thread.turns ||= [];
    const turn = turns.find(t => t.id === params.turn.id);
    if (turn) Object.assign(turn, {...params.turn, items: turn.items});
    else turns.push({...params.turn, items: params.turn.items || []});

  }
  if (method === "turn/completed") {
    detail.running = false;
    const turn = detail.thread.turns?.find(t => t.id === params.turn?.id);
    if (turn) Object.assign(turn, {...params.turn, items: params.turn.items?.length ? params.turn.items : turn.items});

  }
  if (!method.startsWith("item/") || !params.turnId) return;
  const turns = detail.thread.turns ||= [];
  let turn = turns.find(t => t.id === params.turnId);
  if (!turn) { turn = {id: params.turnId, items: [], status: "inProgress", startedAt: params.turnStartedAt}; turns.push(turn); }
  if (turn.startedAt == null && params.turnStartedAt != null) turn.startedAt = params.turnStartedAt;
  if (params.item) {
    const index = turn.items.findIndex(i => i.id === params.item.id);
    if (index < 0) turn.items.push(params.item); else turn.items[index] = params.item;
  } else if (params.itemId && typeof params.delta === "string") {
    let item = turn.items.find(i => i.id === params.itemId);
    if (!item && method === "item/agentMessage/delta") { item = {id: params.itemId, type: "agentMessage", text: ""}; turn.items.push(item); }
    if (item) item.renderRevision = (item.renderRevision || 0) + 1;
    if (item && method === "item/agentMessage/delta") item.text = (item.text || "") + params.delta;
    if (item && method === "item/commandExecution/outputDelta") item.aggregatedOutput = (item.aggregatedOutput || "") + params.delta;
    if (item && method === "item/mcpToolCall/outputDelta") {
      const output = typeof item.visibleOutput === "string" ? item.visibleOutput : typeof item.result === "string" ? item.result : "";
      item.visibleOutput = output + params.delta;
    }
    if (item && method === "item/reasoning/summaryTextDelta" && Number.isSafeInteger(params.summaryIndex) && params.summaryIndex >= 0) {
      const summary = item.summary ||= [], index = params.summaryIndex;
      if (summary[index] && typeof summary[index] === "object") summary[index].text = (summary[index].text || "") + params.delta;
      else summary[index] = (summary[index] || "") + params.delta;
    }
  }
}
let listTimer, detailTimer;
let listScheduledAt = 0;
// With many agents working, inventory events arrive several times a second.
// Each refresh rereads and parses the whole thread list, so read it at most
// once per interval; a pending refresh is kept rather than postponed by the
// next event, which could otherwise defer it indefinitely.
const LIST_REFRESH_INTERVAL = 2000;
const INACTIVE_REFRESH_INTERVAL = 15000;
function backgroundRefreshAllowed(kind) {
  if (document.hidden) return false;
  const now = Date.now(), previous = state.backgroundReads?.[kind];
  if (!document.hasFocus() && previous !== undefined && now - previous < INACTIVE_REFRESH_INTERVAL) return false;
  (state.backgroundReads ||= {})[kind] = now;
  return true;
}
function refreshBackgroundView({reconcile = false} = {}) {
  if (state.streamHealthy && !reconcile && !state.backgroundDetailPending) return;
  if (!state.paneMode && backgroundRefreshAllowed("list")) refreshList({background: true}).catch(showError);
  if (backgroundRefreshAllowed("detail")) {
    if (reconcile || state.backgroundDetailPending) {
      state.backgroundDetailPending = false;
      refreshDetail({background: true}).catch(showError);
    } else refreshThread({background: true}).catch(showError);
    window.agentCoordPanes?.refresh();
  }
}
function scheduleList() {
  if (listTimer) return;
  const wait = Math.max(150, LIST_REFRESH_INTERVAL - (Date.now() - listScheduledAt));
  listTimer = setTimeout(() => {
    listTimer = null; listScheduledAt = Date.now();
    if (backgroundRefreshAllowed("list")) refreshList({background: true}).catch(showError);
  }, wait);
}
function scheduleDetail() {
  if (detailTimer) return;
  detailTimer = setTimeout(() => {
    detailTimer = null;
    if (backgroundRefreshAllowed("detail")) {
      state.backgroundDetailPending = false;
      refreshDetail({background: true}).catch(showError);
    } else state.backgroundDetailPending = true;
  }, 100);
}
function invalidateConversationMetadata({revision, threadIds = [], topics = [], reset = false}) {
  if (!Number.isSafeInteger(revision) || !reset && revision <= state.config.conversationMetadataCursor) return;
  state.config.conversationMetadataCursor = revision;
  const matches = id => reset || !threadIds.length || threadIds.includes(id);
  for (const [id, entry] of state.conversations || []) if (matches(id)) entry.revision++;
  if (state.selected && matches(state.selected)) scheduleDetail();
  for (const [id, record] of globalThis.agentCoordPanes?.records || [])
    if (record.api && matches(id)) record.api.scheduleRefresh();
  if (reset || topics.includes("organization")) state.organizationReadAt = 0;
  if (reset || topics.includes("threads") || topics.includes("organization")) scheduleList();
  if (reset || topics.includes("asks")) state.asks?.refresh().catch(showError);
  if (reset || topics.includes("schedules")) state.schedules?.refresh().catch(showError);
  if (reset || topics.includes("settings")) {
    state.activity?.refreshSettings?.();
    globalThis.agentCoordJev?.refreshStatus?.();
  }
  if (!state.config.conversationOpen && (reset || topics.includes("sidecars"))) state.sidecars?.refresh().catch(showError);
}
function connect() {
  state.events?.close?.();
  state.streamHealthy = false;
  const metadata = state.config.conversationMetadataEvents === 1 && Number.isSafeInteger(state.config.conversationMetadataCursor);
  const events = state.events = new EventSource(base + "events?after=" + (state.config.eventCursor ?? 0)
    + "&completion_after=" + state.config.completionCursor + (metadata ? "&metadata_after=" + state.config.conversationMetadataCursor : ""));
  if (state.updateSubmissions) {
    // Broadcast only invalidation to this window and its same-origin panes.
    // Receipt contents are read using the selected conversation's own route.
    const changed = event => {
      let watchKeys;
      if (event.data) {
        try { watchKeys = JSON.parse(event.data).watchKeys; }
        catch { /* Unknown signals refresh the selected conversation. */ }
      }
      window.dispatchEvent(new CustomEvent("agent-coord-update-submissions", {detail: watchKeys}));
    };
    events.addEventListener("update-submission-change", changed);
    events.addEventListener("open", changed);
  }
  events.onopen = () => {
    if (state.events !== events) return;
    state.streamHealthy = metadata;
    state.asks?.setEventsConnected?.(metadata);
    state.setPolling?.(metadata);
    $("connection").replaceChildren(node("span", "Connected locally", "sr-only"));
    $("connection").title = "Connected locally"; $("connection").dataset.state = "connected";
    if (state.streamWasOpen) {
      for (const entry of state.conversations?.values() || []) entry.revision++;
      scheduleList(); scheduleDetail(); globalThis.agentCoordPanes?.refresh();
    }
    state.streamWasOpen = true;
  };
  events.onerror = () => {
    if (state.events !== events) return;
    state.streamHealthy = false; state.asks?.setEventsConnected?.(false); state.setPolling?.(false);
    $("connection").textContent = "Reconnecting…"; $("connection").title = "Reconnecting…";
    $("connection").dataset.state = "reconnecting";
  };
  let resumeCursor;
  let previousApprovals = new Map();
  events.onmessage = message => {
    if (state.events !== events) return;
    const batch = JSON.parse(message.data);
    const stableCursor = /^([0-9a-f]{32}\/\d+):\d+$/.exec(message.lastEventId || "");
    resumeCursor = {eventCursor: stableCursor ? stableCursor[1] : batch.seq, completionCursor: batch.completionSeq};
    // Approvals are a snapshot of unclaimed requests. Batches between polls
    // omit the field, and each poll can repeat the same unanswered requests.
    const changedThreads = new Set();
    if (Array.isArray(batch.approvals)) {
      const current = new Map();
      for (const approval of batch.approvals) {
        const thread = approval.thread_id, value = JSON.stringify(approval);
        const previous = previousApprovals.get(approval.request_key);
        current.set(approval.request_key, {thread, value});
        if (previous?.value !== value) {
          changedThreads.add(thread);
          if (previous) changedThreads.add(previous.thread);
        }
      }
      for (const [key, previous] of previousApprovals) {
        if (!current.has(key)) changedThreads.add(previous.thread);
      }
      previousApprovals = current;
    }
    const approvalChanges = Array.from(changedThreads, thread_id => ({thread_id}));
    globalThis.agentCoordSpeech?.receive(batch.completions || []);

    // Completions apply to every thread, including when this tab is hidden.
    for (const notification of [...(batch.completions || []), ...approvalChanges]) {
      const entry = state.conversations?.get(notification.thread_id);
      if (entry) entry.revision++;
    }
    notifications.receive(batch.completions || []);
    notifications.receive(batch.approvals || []);
    notifications.receive(batch.snoozes || []);
    // A notification can arrive after the request event was missed. Reconcile
    // the pending controls as well as notifying, even without an item event.
    if (batch.completions?.length || batch.snoozes?.length || changedThreads.size) scheduleList();
    if (state.selected && (changedThreads.has(state.selected)
        || batch.completions?.some(row => row.thread_id === state.selected)
        || batch.snoozes?.some(row => row.thread_id === state.selected))) scheduleDetail();
    if (batch.reset) {
      for (const entry of state.conversations?.values() || []) {
        entry.revision++; delete entry.lastEventSeq;
        if (entry.detail) { delete entry.detail.stream_seq; delete entry.detail.revisionEpoch; }
      }
      scheduleList(); refreshDecisionsAndSchedules(); scheduleDetail(); globalThis.agentCoordPanes?.refresh();
    }
    for (const event of batch.events) {
      if (event.method === "bridge/disconnected") { for (const entry of state.conversations?.values() || []) entry.revision++; showError(new Error("Codex disconnected. Restart Ribbon Field to reopen saved sessions.")); if (state.detail?.work_thread?.client === "codex" && state.detail) { state.detail.running = false; state.detailRefreshing = true; renderRequests(); } renderStatus(); }
      else applyEvent(event);
      if (changesThreadList(event)) scheduleList();
    }
    globalThis.agentCoordPanes?.receive({...batch, approvals: approvalChanges});
  };
  window.addEventListener("pagehide", event => {
    events.close();
    if (state.events === events) state.streamHealthy = false;
    if (event.persisted && state.events === events) {
      // The page cache restores the same document with its old stream closed.
      window.addEventListener("pageshow", restored => {
        if (restored.persisted) {
          Object.assign(state.config, resumeCursor);
          connect();
        }
      }, {once: true});
    }
  }, {once: true});
}
// Per-item provider events change the open conversation, not the inventory.
// They arrive several times a second; the five-second metadata refresh still
// picks up their side effects, such as the last tool on a card. Provider
// requests keep refreshing because they change attention.
const conversationOnlyEvents = new Set(["thread/usage/updated", "thread/tokenUsage/updated", "turn/diff/updated",
  "turn/plan/updated", "account/rateLimits/updated", "deprecationNotice", "mcpServer/startupStatus/updated", "warning"]);
function changesThreadList(event) {
  if (event.requestKey) return true;
  const method = event.method || "";
  return !conversationOnlyEvents.has(method) && !method.startsWith("item/") && !method.startsWith("hook/")
    && !method.endsWith("/delta") && !method.endsWith("/outputDelta");
}
function newProject() {
  $("project-form").reset();
  $("project-error").hidden = true;
  $("project-dialog").showModal();
  $("project-name").focus();
}
$("add-group").onclick = $("create-project").onclick = $("welcome-project").onclick = newProject;
$("project-form").onsubmit = event => {
  event.preventDefault();
  action(async () => {
    const name = $("project-name").value.trim();
    if (!name) throw new Error("Enter a group name.");
    const project = await api("projects", {name, cwd: state.folders.selected.cwd});
    $("project-dialog").close();
    await refreshList();
    await savedViews.openGroup(project.id);
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
  project.replaceChildren(new Option("No group", ""));
  for (const item of state.organization.projects.filter(item => item.cwd === (thread?.project_cwd || thread?.cwd || state.folders?.selected?.cwd))) {
    project.append(new Option(item.name + (savedViews?.isGroupHidden(item.id, item.cwd) ? " · Hidden" : ""), item.id));
  }
  project.append(new Option("Create a group…", "__new__"));
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
async function assignmentPayload(prefix, thread) {
  const body = {};
  for (const field of ["project"]) {
    let value = $(prefix + "-" + field).value;
    if (value === "__auto__") continue;
    if (value === "__new__") {
      const input = $(prefix + "-" + field + "-value").value;
      const record = await api(field === "repository" ? "repositories" : "projects",
        field === "repository" ? {path: input} : {name: input, cwd: thread?.project_cwd || thread?.cwd || state.folders.selected.cwd});
      value = record.id;
    }
    body[field + "_id"] = value || null;
  }
  return body;
}
for (const prefix of ["edit"]) {
  for (const field of ["repository", "project"]) $(prefix + "-" + field).onchange = () => assignmentFields(prefix);
}
let organizationThread = null;
let movingThread = null;

function openThreadMove(thread) {
  if (threadMoveUnavailable(thread)) return;
  movingThread = thread;
  $("move-thread-directory").value = "";
  $("move-thread-projects").replaceChildren(...(state.folders?.projects || [])
    .filter(project => project.cwd !== thread.project_cwd).map(project => new Option(project.cwd, project.cwd)));
  $("move-thread-error").hidden = true;
  $("move-thread-dialog").showModal();
  $("move-thread-directory").focus();
}
async function submitThreadMove() {
  const thread = movingThread;
  if (!thread || state.threadMoveSubmitting) return;
  state.threadMoveSubmitting = thread.thread_id;
  try {
    const moved = await api(threadPath(thread.thread_id) + "/move", {cwd: $("move-thread-directory").value.trim()});
    $("move-thread-dialog").close();
    if (state.selected === thread.thread_id) {
      await state.threadTabs?.close(thread.thread_id, {force: true});
      await openProjectDirectory(moved.project_cwd);
      await select(thread.thread_id);
    } else await refreshList();
  } catch (error) {
    $("move-thread-error").textContent = error.message;
    $("move-thread-error").hidden = false;
  } finally {
    state.threadMoveSubmitting = null;
  }
}
$("move-thread").onclick = () => openThreadMove(state.detail?.work_thread);
$("move-thread-form").onsubmit = event => {
  event.preventDefault();
  action(submitThreadMove, $("save-thread-move"));
};
$("organize-thread").onclick = () => action(() => organizeThread(state.detail?.work_thread), $("organize-thread"));
$("quiet-thread").onclick = () => action(() => toggleThreadQuiet(state.detail.work_thread), $("quiet-thread"));
// Roles: the user names positions and appoints the conversations that hold them.
let roleThread = null;
async function openRoleDialog(thread) {
  if (!thread) return;
  const roles = (await api("roles")).data;
  roleThread = thread; state.roles = roles;
  $("role-names").replaceChildren(...roles.map(role => new Option(role.name, role.name)));
  $("role-name").value = thread.role?.name || "";
  $("vacate-role").hidden = !thread.role;
  if (thread.role) $("vacate-role").textContent = "Remove from " + thread.role.name;
  $("role-error").hidden = true;
  describeRoleChoice();
  $("role-dialog").showModal();
  $("role-name").focus();
}
function matchingRole() {
  const name = $("role-name").value.trim().replace(/\s+/g, " ").toLowerCase();
  return (state.roles || []).find(role => role.name.toLowerCase() === name);
}
function plural(count, one, many) { return count + " " + (count === 1 ? one : many); }
function describeRoleChoice() {
  const role = matchingRole(), note = $("role-current"), holder = role?.holder_session_id;
  note.hidden = !role || holder === roleThread?.thread_id;
  if (role && holder) note.textContent = "Held by " + (role.holder_title || "another conversation") + ". " +
    (role.agent_count ? plural(role.agent_count, "agent", "agents") + " will report to this conversation instead." : "It has no agents.");
  else if (role) note.textContent = "Vacant." + (role.user_supervised_count
    ? " " + plural(role.user_supervised_count, "agent waiting for this role", "agents waiting for this role") + " will report to this conversation." : "");
  $("save-role").textContent = role || !$("role-name").value.trim() ? "Assign role" : "Create and assign";
}
function roleOutcome(text, result) {
  const moved = result.moved_thread_ids?.length || 0, deferred = result.deferred_thread_ids?.length || 0;
  return text + (moved ? " " + plural(moved, "agent", "agents") + " moved." : "") +
    (deferred ? " " + plural(deferred, "busy agent moves", "busy agents move") + " when " + (deferred === 1 ? "its turn ends." : "their turns end.") : "");
}
async function saveRole(sessionId) {
  const thread = roleThread;
  if (!thread) return;
  let created = null;
  try {
    let role = matchingRole();
    if (sessionId && !role) role = created = await api("roles", {name: $("role-name").value});
    const target = sessionId ? role : thread.role;
    let result;
    try { result = await api("roles/" + encodeURIComponent(target.role_id) + "/holder", {session_id: sessionId}); }
    catch (error) {
      // A role created for this assignment is removed when the assignment fails.
      if (created) await api("roles/" + encodeURIComponent(created.role_id) + "/delete", {}).catch(() => {});
      throw error;
    }
    $("role-dialog").close();
    state.threadActions.notify("role:" + target.role_id, roleOutcome(sessionId
      ? thread.title + " now holds " + result.name + "." : thread.title + " no longer holds " + result.name + "; its agents report to you.", result));
    await refreshList();
  } catch (error) {
    $("role-error").textContent = error.message;
    $("role-error").hidden = false;
  }
}
$("role-name").oninput = describeRoleChoice;
$("role-form").onsubmit = event => {
  event.preventDefault();
  if (matchingRole()?.holder_session_id === roleThread?.thread_id) { $("role-dialog").close(); return; }
  action(() => saveRole(roleThread.thread_id), $("save-role"));
};
$("vacate-role").onclick = () => action(() => saveRole(null), $("vacate-role"));
// Supervisors: the user gives one agent a new supervising conversation.
let supervisorThread = null;
function openSupervisorDialog(thread) {
  supervisorThread = thread;
  const choices = (state.sessions || []).filter(item => item.browser_session && item.attention !== "archived"
    && item.thread_id !== thread.thread_id && item.thread_id !== thread.supervisor_thread_id);
  $("supervisor-choice").replaceChildren(...choices.map(item => new Option(item.title, item.thread_id)));
  $("save-supervisor").disabled = !choices.length;
  $("supervisor-error").textContent = choices.length ? "" : "No other open app conversation can supervise this agent.";
  $("supervisor-error").hidden = !!choices.length;
  $("supervisor-dialog").showModal();
  $("supervisor-choice").focus();
}
$("supervisor-form").onsubmit = event => {
  event.preventDefault();
  action(async () => {
    const thread = supervisorThread, select = $("supervisor-choice");
    try {
      const result = await api(threadPath(thread.thread_id) + "/supervisor", {session_id: select.value});
      $("supervisor-dialog").close();
      const name = select.selectedOptions[0]?.textContent || "the chosen conversation";
      state.threadActions.notify("supervisor:" + thread.thread_id, result.deferred_thread_ids.length
        ? thread.title + " reports to " + name + " when its turn ends." : thread.title + " now reports to " + name + ".");
      await refreshList();
    } catch (error) {
      $("supervisor-error").textContent = error.message;
      $("supervisor-error").hidden = false;
    }
  }, $("save-supervisor"));
};
// Stops: one user action lifts deliberate Stops; supervisors decide what restarts.
async function openStopsDialog() {
  const stopped = (await api("stops")).data;
  const titles = new Map((state.sessions || []).map(thread => [thread.thread_id, thread.title]));
  const rows = stopped.map(item => {
    const label = node("label", null, "stops-row"), box = node("input");
    box.type = "checkbox"; box.value = item.thread_id; box.checked = true;
    const copy = node("span", null, "stops-copy");
    copy.append(node("span", item.title, "stops-title"), node("small", item.supervisor
      ? "Supervised by " + (titles.get(item.supervisor) || "another agent")
      : "No supervising agent · restart it yourself", "muted"));
    label.append(box, copy);
    return label;
  });
  $("stops-list").replaceChildren($("stops-list").querySelector("legend"), ...rows);
  if (!rows.length) $("stops-list").append(node("p", "No conversations are stopped.", "muted"));
  $("release-stops").disabled = !rows.length;
  $("stops-error").hidden = true;
  $("stops-dialog").showModal();
}
$("release-stops-open").onclick = () => action(openStopsDialog, $("release-stops-open"));
$("stops-form").onsubmit = event => {
  event.preventDefault();
  action(async () => {
    const ids = [...$("stops-list").querySelectorAll("input:checked")].map(box => box.value);
    if (!ids.length) { $("stops-error").textContent = "Choose at least one conversation."; $("stops-error").hidden = false; return; }
    try {
      const result = await api("stops/release", {thread_ids: ids});
      $("stops-dialog").close();
      const released = result.released_thread_ids.length, notified = result.notified_supervisors.length;
      state.threadActions.notify("stops", "Released " + plural(released, "conversation", "conversations") + "." +
        (notified ? " " + plural(notified, "supervising agent was", "supervising agents were") + " notified." : "") +
        (result.skipped.length ? " " + plural(result.skipped.length, "was", "were") + " no longer stopped." : ""));
      await refreshList();
    } catch (error) {
      $("stops-error").textContent = error.message;
      $("stops-error").hidden = false;
    }
  }, $("release-stops"));
};
async function organizeThread(thread) {
  if (!thread) return;
  const selected = state.selected;
  await fillAssignments("edit", thread);
  if (state.selected !== selected) return;
  organizationThread = thread;
  $("organization-workspace").textContent = "Project: " + (thread.project_cwd || thread.cwd);
  $("organization-dialog").showModal();
  $("edit-project").focus();
}
$("organization-form").onsubmit = event => {
  event.preventDefault();
  action(async () => {
    const thread = organizationThread, id = thread.thread_id, values = await assignmentPayload("edit", thread);
    const draft = id === "new-session" && state.newSessionDraft?.work_thread === thread ? state.newSessionDraft : null;
    if (id === "new-session" && !draft) throw new Error("This draft changed. Open the group editor again.");
    if (draft && !draft.createdId) { Object.assign(draft.session, values); Object.assign(draft.work_thread, values); }
    else await api(threadPath(draft?.createdId || id), values);
    savedViews?.showGroup(values.project_id, thread.project_cwd || thread.cwd);
    $("organization-dialog").close();
    if (state.selected === id) { if (draft && !draft.createdId) renderStatus(); else await refreshThread(); }
    await refreshList();
  }, $("save-organization"));
};
function isSessionDraft() { return state.selected === "new-session" && !!state.newSessionDraft; }
function composerDraftId(id = state.selected) {
  return id === "new-session" ? state.newSessionDraft?.draftId || id : id;
}
function sessionBusy() {
  return !!state.settingsMutations?.has(state.selected) || !!state.transferLocks?.has(state.selected) || !!state.sending?.has(isSessionDraft() ? state.newSessionDraft : state.selected);
}
function modelValue(client, model) { return JSON.stringify([client, model || ""]); }
function defaultsKey(session) {
  return JSON.stringify([session.client || "codex", session.cwd || state.detail?.work_thread?.cwd || state.config?.cwd, session.model || null]);
}
function effectiveSessionSettings(session) {
  const inherited = state.sessionDefaults?.get(defaultsKey(session)) || {};
  const models = state.modelCatalogs?.get(session.client || "codex") || [];
  const model = models.find(item => item.model === (inherited.model || session.model) || item.resolvedModel === inherited.model && inherited.model);
  return {...inherited, model: inherited.model || session.model, modelLabel: inherited.modelLabel || model?.displayName || inherited.model || session.model,
    effort: session.effort || inherited.effort, supportsEffort: inherited.supportsEffort ?? !!model?.supportedReasoningEfforts?.length};
}
async function loadSessionDefaults(session = state.detail?.session) {
  if (!session) return;
  const key = defaultsKey(session), cache = state.sessionDefaults ||= new Map(), loading = state.defaultsLoading ||= new Map();
  if (cache.has(key)) return cache.get(key);
  if (!loading.has(key)) {
    const [client, cwd, model] = JSON.parse(key);
    loading.set(key, api("session-defaults", {client, cwd, model}).then(result => {
      cache.set(key, {...result, unavailable: !result.model}); return result;
    }).catch(() => { cache.set(key, {unavailable: true}); }).finally(() => {
      loading.delete(key); renderStatus();
    }));
  }
  return loading.get(key);
}
function sessionModelClients() {
  return isSessionDraft() && !state.newSessionDraft.createdId ?
    ["codex", "claude", "antigravity", "kimi", "muse"] : [state.detail?.session?.client || "codex"];
}
function selectDraftModel({client, model}) {
  if (!isSessionDraft() || state.newSessionDraft.createdId || sessionBusy() || state.detailRefreshing) return;
  if (state.newSessionDraft.session.client !== client) {
    delete state.newSessionDraft.session.permission_mode;
    delete state.newSessionDraft.session.permission_modes;
    const remembered = state.newSessionDraft.permissionChoices?.[client];
    if (remembered) state.newSessionDraft.session.permission_mode = remembered.id;
  }
  Object.assign(state.newSessionDraft.session, {client, model: model || null, effort: null});
  state.newSessionDraft.work_thread.client = client;
  renderStatus();
}
function renderModelPicker() {
  const session = state.detail?.session, picker = $("model-picker");
  if (!session) { picker.disabled = true; globalThis.modelPicker?.sync(); renderEffortPicker(); return; }
  const draft = isSessionDraft(), client = session.client || "codex";
  const effective = effectiveSessionSettings(session);
  const clients = sessionModelClients();
  const catalogs = state.modelCatalogs ||= new Map();
  const saved = state.detail.transferReadSource === "saved";
  const signature = JSON.stringify([clients, session.model, [...catalogs], effective, saved]);
  if (picker.dataset.signature !== signature) {
    picker.dataset.signature = signature;
    picker.replaceChildren();
    for (const provider of clients) {
      const group = node("optgroup"); group.label = provider === "muse" ? "Muse Code" : provider === "kimi" ? "Kimi Code" : provider === "antigravity" ? "Google Antigravity" : provider === "claude" ? "Claude Code" : "Codex";
      if (draft || !session.model || session.model === "default") group.append(new Option(provider === client ?
        effective.modelLabel || (effective.unavailable ? "Model unavailable" : saved ? "Configured model" : "Loading model…") : group.label + " configured model", modelValue(provider, "")));
      const models = catalogs.get(provider) || [];
      for (const model of models) group.append(new Option(model.displayName || model.model, modelValue(provider, model.model)));
      if (provider === client && session.model && !models.some(model => model.model === session.model)) {
        group.append(new Option(session.model, modelValue(provider, session.model)));
      }
      picker.append(group);
    }
  }
  picker.value = modelValue(client, session.model === "default" ? "" : session.model);
  picker.disabled = sessionBusy() || state.detailRefreshing || !!state.detail.running || state.detail.work_thread?.attention === "archived" || !!state.newSessionDraft?.createdId && draft;
  const errors = clients.filter(provider => state.modelErrors?.has(provider));
  $("model-picker-status").textContent = errors.length ? errors.map(provider => (provider === "muse" ? "Muse Code" : provider === "kimi" ? "Kimi" : provider === "antigravity" ? "Antigravity" : provider === "claude" ? "Claude Code" : "Codex") + " models unavailable").join(" · ") : "";
  if (["antigravity", "kimi", "muse"].includes(client) && state.modelErrors?.has(client)) $("model-picker-status").textContent = state.modelErrors.get(client);
  globalThis.modelPicker?.sync();
  renderEffortPicker();
  // Looking up an inherited setting starts a provider RPC. A saved view can
  // display its stored choice immediately and resolve defaults on picker focus.
  if (!saved) loadSessionDefaults(session);
}
function renderEffortPicker() {
  const session = state.detail?.session, picker = $("effort-picker");
  const effective = session ? effectiveSessionSettings(session) : {};
  const models = state.modelCatalogs?.get(session?.client || "codex") || [];
  const model = models.find(item => item.model === session?.model || item.model === effective.model || item.resolvedModel === effective.model && effective.model);
  const efforts = model?.supportedReasoningEfforts || [];
  const inheritedEffort = session && state.sessionDefaults?.get(defaultsKey(session))?.effort;
  const signature = JSON.stringify([session?.effort, efforts, effective, inheritedEffort]);
  if (picker.dataset.signature !== signature) {
    picker.dataset.signature = signature;
    picker.replaceChildren(new Option(inheritedEffort ? inheritedEffort + " (configured)" :
      effective.unavailable ? "Effort unavailable" : effective.model && !effective.supportsEffort ? "Not configurable" : "Loading effort…", ""));
    for (const effort of efforts) picker.append(new Option(effort.reasoningEffort, effort.reasoningEffort));
    if (session?.effort && !efforts.some(item => item.reasoningEffort === session.effort)) {
      picker.append(new Option(session.effort, session.effort));
    }
  }
  picker.value = session?.effort || "";
  picker.disabled = $("model-picker").disabled || !efforts.length;
  $("effort-trigger").title = efforts.length ? "Reasoning effort for the next message" : "This model has no configurable reasoning effort";
  globalThis.effortPicker?.sync();
}
async function changeSessionEffort() {
  const id = state.selected, draft = isSessionDraft(), effort = $("effort-picker").value || null;
  if ($("effort-picker").disabled || sessionBusy() || state.detail?.running || draft && state.newSessionDraft.createdId) return;
  (state.settingsMutations ||= new Set()).add(id);
  renderStatus();
  try {
    if (draft) state.newSessionDraft.session.effort = effort;
    else {
      const session = await api(sessionPath(id), {effort});
      if (state.selected === id) state.detail.session = session;
    }
  } finally { state.settingsMutations.delete(id); renderStatus(); }
}
$("effort-picker").onchange = () => action(changeSessionEffort);
$("effort-picker").onfocus = () => loadSessionModels({refresh: true});
async function loadModelCatalog(client, {refresh = false} = {}) {
  const catalogs = state.modelCatalogs ||= new Map(), loading = state.modelLoading ||= new Map();
  const errors = state.modelErrors ||= new Map();
  if (!refresh && catalogs.has(client) && !loading.has(client)) return catalogs.get(client);
  if (!loading.has(client)) {
    loading.set(client, api("models?client=" + encodeURIComponent(client)).then(result => {
      catalogs.set(client, result.data); errors.delete(client);
      return result.data;
    }).catch(error => { errors.set(client, error.message); throw error; })
      .finally(() => { loading.delete(client); renderModelPicker(); }));
  }
  return loading.get(client);
}
async function loadSessionModels({refresh = false} = {}) {
  const clients = sessionModelClients();
  // Opening either menu rechecks native access; retain choices while loading
  // and share the request with the other picker and slash autocomplete.
  await Promise.all([loadSessionDefaults(), ...clients.map(client => loadModelCatalog(client, {refresh}).catch(() => {}))]);
}
async function changeSessionModel() {
  const id = state.selected, [client, model] = JSON.parse($("model-picker").value);
  const draft = isSessionDraft();
  if (sessionBusy() || state.detailRefreshing || state.detail?.running || draft && state.newSessionDraft.createdId) return;
  if (draft) { selectDraftModel({client, model}); await loadSessionDefaults(); return; }
  (state.settingsMutations ||= new Set()).add(id);
  try {
    const session = await api(sessionPath(id), {model});
    if (state.selected === id) state.detail.session = session;
  } finally { state.settingsMutations.delete(id); renderStatus(); }
}
$("model-picker").onchange = () => action(changeSessionModel);
$("model-picker").onfocus = () => loadSessionModels({refresh: true});
async function newSession() {
  state.initializeConversationView?.();
  state.mobileChat?.close();
  if (!state.config || state.choosingSessionFolder) return;
  let body = state.newSessionDraft?.session;
  if (!body) {
    body = sessionContext();
    if (!body.cwd) {
      state.choosingSessionFolder = true;
      try {
        if (!await state.folderPicker.choose({navigate: false})) return;
        body = {...body, cwd: state.folders.selected.cwd};
        renderFolders();
      } finally { state.choosingSessionFolder = false; }
    }
  }
  goHome({restoreDispatcher: false});
  // A fresh draft re-reads configuration changed outside the app.
  if (!state.newSessionDraft) state.sessionDefaults?.clear();
  const defaults = globalThis.NewSessionDefaults?.read() || {client: "codex", model: null, effort: null, yolo: false};
  state.newSessionDraft ||= {
    draftId: "new-session:" + body.cwd,
    session: {...body, ...defaults},
    work_thread: {thread_id: "new-session", title: "New session", ...body, project_cwd: body.cwd, client: defaults.client, browser_session: true, attention: "now", links: []},
    thread: {turns: []}, requests: [], queuedMessages: [], running: false,
  };
  state.selected = "new-session";
  state.updateSubmissions?.select(null);
  if (!state.paneMode) state.activity?.select(null);
  state.scratchpad?.select(null);
  state.detail = state.newSessionDraft;
  state.threadTabs?.open(state.newSessionDraft.work_thread);
  state.detailRefreshing = false;
  state.titleEdit = null;
  $("message").value = state.drafts.get("new-session") || "";
  if (state.durableDrafts) restoreLocalDraft(state.newSessionDraft.draftId).catch(showError);
  $("welcome").hidden = true;
  $("conversation").hidden = false;
  $("conversation").classList.add("session-draft");
  state.agentTreeController?.bind();
  if (typeof layoutContext === "function") layoutContext();
  $("page-location").textContent = "New session";
  state.timelineThread = null;
  replaceTimelineContent(node("p", "Choose a model and send a message to start.", "empty"));
  state.timelineScroll?.reset();
  const intent = state.creationIntents?.get(state.newSessionDraft.draftId);
  if (intent) showCreationIntent(state.newSessionDraft.draftId, intent);
  $("requests").replaceChildren(); delete $("requests").dataset.signature;
  $("thread-context").hidden = true;
  renderTitle(); renderQueuedMessages(); renderStatus();
  navigation?.remember();
  $("message").focus();
  // The restored draft is usable before provider catalog discovery completes.
  loadSessionModels().catch(showError);
}
async function sendSessionDraft(text, images, mentions = [], quiet = false) {
  const input = $("message"), doc = input.ownerDocument;
  const hadComposerFocus = doc?.activeElement === input;
  const navigationRevision = state.navigationRevision;
  let restoreFocus = false, focusRevision;
  const draft = state.newSessionDraft;
  const parts = text.trim().split(/\s+/), command = parts[0].toLowerCase();
  if (!draft.createdId && ["/permissions", "/yolo"].includes(command)) {
    if (images.length) throw new Error("Remove attached images before using chat configuration commands.");
    if (command === "/yolo" && parts.length !== 1)
      throw new Error("Use /yolo without arguments. Use /permissions default to turn it off.");
    if (command === "/permissions" && (parts.length > 2 || parts.length === 2 && !["default", "yolo"].includes(parts[1].toLowerCase())))
      throw new Error("Use /permissions default or /permissions yolo.");
    if (command === "/yolo" || parts.length === 2) {
      draft.session.yolo = command === "/yolo" || parts[1].toLowerCase() === "yolo";
      delete draft.session.permission_mode; delete draft.session.permission_modes;
      if (draft.permissionChoices) delete draft.permissionChoices[draft.session.client];
    }
    draft.commandFeedback = draft.session.permission_mode ? "Permissions: " + sessionPermissionLabel(draft.session) + " — applies to the next message."
      : draft.session.yolo
      ? "Permissions: yolo — full machine access without approval prompts. Use /permissions default to turn it off."
      : "Permissions: default — approval prompts enabled. Use /yolo to enable full machine access.";
    input.value = ""; state.drafts.delete("new-session");
    state.mentions?.sent(draft.draftId || "new-session", text, mentions);
    if (state.durableDrafts) {
      const id = draft.draftId || "new-session";
      await writeComposerRecord(id, () => state.durableDrafts.write(id, {text: "", mentions: []}), "text");
    }
    renderStatus();
    return;
  }
  const cwd = state.projectCwd || draft.session.cwd, draftId = draft.draftId || "new-session";
  const ordinary = !command.startsWith("/") && !state.reviewUI?.settings?.has(draftId);
  if (ordinary && !draft.createdId && state.durableDrafts) {
    const settings = {...draft.session};
    if (settings.permission_mode) delete settings.yolo;
    return startCreation(draftId, settings, text, images, mentions, quiet);
  }
  const requestId = ordinary ? crypto.randomUUID() : null;
  if (ordinary) {
    const versions = state.draftVersions ||= new Map();
    versions.set(draftId, (versions.get(draftId) || 0) + 1);
    const pending = {text, images: images.length, key: requestId, status: "sending"};
    replaceTimelineContent(state.outgoing.node(document, pending));
    input.value = ""; state.drafts.delete("new-session");
    state.timelineScroll?.latest();
  }
  if (!draft.createdId) {
    const settings = {...draft.session};
    if (settings.permission_mode) delete settings.yolo;
    let result;
    try { result = await api("sessions", settings); }
    catch (error) {
      if (ordinary && state.newSessionDraft === draft) {
        if (!state.drafts.get("new-session")) state.drafts.set("new-session", text);
        if (isSessionDraft()) {
          if (!input.value) input.value = text;
          replaceTimelineContent(state.outgoing.node(document,
            {text, images: images.length, key: requestId, status: "failed", error: error.message}));
        }
      }
      throw error;
    }
    draft.createdId = result.session.thread_id;
    draft.createdDetail = result;
    if (draft.session.project_id) savedViews?.showGroup(draft.session.project_id, draft.session.cwd);
  }
  const body = {message: text, windowId: state.sourceWindowId};
  if (quiet) body.quiet = true;
  if (mentions.length) body.mentions = mentions;
  if (images.length) body.images = images.map(({name, url}) => ({name, url}));
  state.reviewUI?.decorate(body, draftId);
  let result = {}, delivery, stage;
  if (ordinary) {
    delivery = submitComposerMessage({id: draft.createdId, requestId, text, images, mentions, body,
      path: sessionPath(draft.createdId) + "/messages", detail: draft.createdDetail, clear() {},
      onStage: promise => { stage = promise; }});
    delivery.catch(() => {}); // Observed after the new conversation has its own composer.
  } else {
    body.requestId ||= DispatchUI.requestKey(sessionStorage, draft.createdId, body);
    result = await api(sessionPath(draft.createdId) + "/messages", body);
  }
  sessionStorage.removeItem("dispatch-request:" + draft.createdId);
  // Creation and message acceptance already establish this conversation. Show
  // that state immediately while its full detail catches up in the background.
  const created = draft.createdDetail || {};
  const entry = conversationEntry(draft.createdId);
  entry.detail = {session: {...draft.session, ...created.session},
    work_thread: {...draft.work_thread, thread_id: draft.createdId, title: "Conversation", client: draft.session.client},
    thread: {...created.thread, turns: result.turn ? [result.turn] : created.thread?.turns || []},
    requests: [], queuedMessages: [], running: !!(result.turn || result.turnId), activeTurn: result.turn?.id || result.turnId || null};
  state.reviewUI?.sent(draftId);
  state.clearingSubmission = draftId;
  try {
    if (images.length) state.attachments?.sent(draftId, images);
    state.mentions?.sent(draftId, text, mentions);
  } finally { state.clearingSubmission = null; }
  state.mentions?.move(draftId, draft.createdId);
  state.attachments?.move(draftId, draft.createdId);
  if (result.command) state.commandFeedback.set(draft.createdId, result.command.message);
  // Preserve edits typed while creation or sending was in flight.
  const current = state.newSessionDraft === draft;
  const saved = state.projectDrafts?.get(cwd);
  const remaining = current ? (isSessionDraft() ? $("message").value : state.drafts.get("new-session"))
    : saved?.detail === draft ? saved.text : "";
  const nextText = ordinary || remaining !== text ? remaining || "" : "";
  if (nextText) state.drafts.set(draft.createdId, nextText);
  const next = {text: nextText, images: state.attachments?.snapshot(draft.createdId) || [],
    mentions: state.mentions?.snapshot(draft.createdId, nextText) || []};
  let migration;
  if (state.durableDrafts) {
    // Reserve both composer keys in this task. Earlier writes complete before
    // the atomic move, and later edits on either key follow that transaction.
    const prior = state.composerWrites?.get(draftId);
    migration = writeComposerRecord(draft.createdId, async () => {
      await prior; await stage;
      await state.durableDrafts.move(draftId, draft.createdId, next);
    });
    writeComposerRecord(draftId, () => migration).catch(showError);
  }
  if (saved?.detail === draft) state.projectDrafts.delete(cwd);
  if (current) {
    state.drafts.delete("new-session");
    state.newSessionDraft = null;
  }
  state.sending?.delete(draft);
  state.threadTabs?.replace("new-session", {thread_id: draft.createdId, title: "Conversation"}, {cwd});
  const destination = state.projectDestinations?.get(cwd);
  if (destination?.tab === "new-session") destination.tab = draft.createdId;
  if (current && state.selected === "new-session") {
    $("message").value = "";
    restoreFocus = !!result.command && hadComposerFocus && state.navigationRevision === navigationRevision;
    const selecting = select(draft.createdId, {focus: !result.command, refreshInventory: false});
    focusRevision = state.navigationRevision;
    await selecting.catch(error => showError(new Error("Your message was received, but the view could not refresh. " + error.message)));
  }
  await migration;
  if (delivery) {
    await delivery;
    if (state.selected === draft.createdId) refreshDetail({background: true}).catch(showError);
  }
  refreshList({background: true}).catch(showError);
  // The first command replaces the draft, including inside a pane where select
  // does not focus the composer. Preserve any intervening navigation or click.
  if (restoreFocus && state.selected === draft.createdId && state.navigationRevision === focusRevision &&
      !$("composer").hidden && !input.disabled && doc.hasFocus() && !doc.querySelector("dialog[open]") &&
      [input, doc.body, doc.documentElement].includes(doc.activeElement)) input.focus({preventScroll: true});
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
$("repository").onchange = $("phase-filter").onchange = filtersChanged;
$("project").onchange = () => {
  const id = $("project").value;
  if (savedViews?.local(savedViews.current()) && id !== threadOrganization.NONE) {
    return action(() => savedViews.openGroup(id));
  }
  if (savedViews?.current().kind === "group") return action(() => savedViews.overview({project: id}));
  filtersChanged();
};
try { const saved = localStorage.getItem("agent-coord-group-by"); if (["phase", "domain", "repository", "project", "none"].includes(saved)) $("group-by").value = saved; } catch (_) { /* Preferences are optional. */ }
$("group-by").onchange = () => {
  if (!savedViews || savedViews.activeId === "all") {
    try { localStorage.setItem("agent-coord-group-by", $("group-by").value); } catch (_) { /* Preferences are optional. */ }
  }
  filtersChanged();
};
$("group-by-domain").onclick = () => {
  const control = $("group-by");
  control.value = control.value === "domain" ? "phase" : "domain";
  control.onchange();
};
$("search").oninput = filtersChanged;
conversationSearch = new ConversationSearch.Search({document, api, filters: () => ({
  show: $("view").value, phase: $("phase-filter").value, project_cwd: state.folders?.selected?.cwd || "",
  repository: $("repository").value, project: $("project").value})});
function setChatExpanded(expanded) {
  expanded = Boolean(expanded && state.selected);
  if (expanded) setNavigation(false);
  document.body.classList.toggle("chat-expanded", expanded);
  layoutContext();
  const button = $("expand-chat"), label = expanded ? "Collapse chat" : "Expand chat";
  button.setAttribute("aria-pressed", String(expanded));
  button.setAttribute("aria-label", label);
  button.title = expanded ? label + " (Esc)" : label;
}
$("expand-chat").onclick = () => setChatExpanded(!document.body.classList.contains("chat-expanded"));
// Desktop lists the header's thread actions in one menu so they remain
// available in compact and expanded layouts; the mobile sheet shows them inline.
function headerMenuItems() {
  return [...document.querySelectorAll(".session-actions .thread-menu-action")].filter(button => !button.hidden)
    .map(button => ({label: button.getAttribute("aria-label") || button.textContent, title: button.title, disabled: button.disabled, run: () => button.click()}));
}
$("thread-actions").onclick = event => state.threadActions.showMenu($("thread-actions"),
  state.detail?.work_thread?.title || "Thread", headerMenuItems(), event);
function goHome({restoreDispatcher = true, rememberTab = true, preserveRollUp = false, render = true} = {}) {
  state.sidecars?.showThread(null, $("thread-sidecars"));
  state.projectScratchpad?.hideTab();
  state.navigationRevision = (state.navigationRevision || 0) + 1;
  state.dispatchRestore = restoreDispatcher;
  state.mobileChat?.close();
  if (!preserveRollUp) state.rollUp?.stop();
  if (state.paneMode) {
    const host = window.parent.agentCoordPanes;
    const index = host.layout?.groups.findIndex(group => group.includes(state.selected));
    if (index >= 0) host.close(index);
    return;
  }
  state.dispatchWorkspace?.hide();
  globalThis.agentCoordPanes?.leave();
  setChatExpanded(false);
  if (state.selected) state.drafts.set(state.selected, $("message").value);
  retainConversation();
  if (state.dispatchDock?.active) state.dispatchDock.hide();
  state.selected = null; state.detail = null; state.titleEdit = null;
  state.updateSubmissions?.select(null);
  state.activity?.select(null);
  state.scratchpad?.select(null);
  state.asks?.showThread(null);
  state.threadTabs?.overview({remember: rememberTab});
  state.agentTreeController?.bind();
  state.detailRefreshing = false;
  if (navigation) navigation.remember();
  else history.replaceState(null, "", location.pathname + location.search);
  state.attachments?.highlight(false);
  notifications?.syncFocus();
  $("conversation").classList.remove("session-draft");
  $("conversation").hidden = true; $("welcome").hidden = false; $("page-location").textContent = "Overview";
  setNavigation(false);
  if (render) {
    renderList(); $("overview-title").focus({preventScroll: true});
    savedViews?.restoreScroll();
  }
}
function openProjectScratchpad() {
  if (!state.projectScratchpad?.available) return;
  goHome({restoreDispatcher: false});
  $("welcome").hidden = true;
  state.projectScratchpad.showTab();
  state.threadTabs?.scratchpad();
  $("page-location").textContent = "Scratchpad";
  navigation?.remember();
}
$("open-project-scratchpad").onclick = () => openProjectScratchpad();
$("back-home").onclick = goHome;
$("sidebar-search").onclick = () => { goHome(); $("search").focus(); $("search").select(); };
const sidebarProjects = $("sidebar-projects");
document.addEventListener("pointerdown", event => { if (!sidebarProjects.contains(event.target)) sidebarProjects.open = false; });
sidebarProjects.addEventListener("change", () => { sidebarProjects.open = false; });
sidebarProjects.addEventListener("keydown", event => {
  if (event.key === "Escape" && sidebarProjects.open) {
    event.preventDefault(); event.stopPropagation(); sidebarProjects.open = false; sidebarProjects.querySelector("summary").focus();
  }
});
$("open-settings").onclick = event => {
  event.preventDefault();
  if (window.agentCoordDesktop?.openSettings) { window.agentCoordDesktop.openSettings().catch(showError); return; }
  setNavigation(false);
  const frame = workspaceDialogFrame(document, "settings-frame");
  if (!frame.getAttribute("src")) frame.src = "/settings?embedded=1";
  $("settings-dialog").showModal();
};
window.addEventListener("message", event => {
  if (event.origin === location.origin && event.source && event.source === $("settings-frame")?.contentWindow && event.data?.type === "ribbon-field-close-settings") $("settings-dialog").close();
});
$("settings-dialog").addEventListener("close", () => {
  $("settings-frame").removeAttribute("src");
  const button = $("open-settings").getClientRects().length ? $("open-settings") : $("menu-toggle");
  button.focus({preventScroll: true});
});
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
  if (open) $("sidebar-search").focus();
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
function setupAgentTree() {
  const records = new Map();
  let openThreads = [], closedThreads = [], familyThreads = [], pending;
  const breadcrumb = AgentTree.Breadcrumb && new AgentTree.Breadcrumb({document, host: $("agent-breadcrumb"),
    onSelect: id => select(id, {tabId: state.threadTabs?.selected, focus: false, stayInProject: true}),
    onError: showError});
  const currentTab = () => state.threadTabs?.items.find(item => item.thread_id === state.threadTabs.selected);
  const controller = state.agentTreeController = {
    mode: "context", root: null, tree: AgentTree.graph([]),
    message(params) {
      if (this.mode === "agents" && !contextCollapsed && !document.hidden && !isSessionDraft()) panel.message(params);
    },
    hasAgents(root = this.root) {
      if (!root || !this.tree.nodes.has(root)) return false;
      return this.tree.family(root).some(id => id !== root && this.tree.nodes.get(id)?.browser_session !== false);
    },
    inventory(open, closed) {
      openThreads = open;
      if (closed) closedThreads = closed;
      this.update();
      if (!state.config?.conversationOpen && this.mode === "agents" && state.selected && !isSessionDraft()) this.load().catch(showError);
    },
    hydrate(data) {
      familyThreads = data;
      this.update();
    },
    async load() {
      if (isSessionDraft() || state.selected === "new-session") return;
      const id = this.root || state.selected;
      if (!id || id === "new-session") return;
      if (pending?.id === id) return pending.promise;
      const request = {id}; pending = request;
      request.promise = api(threadPath(id) + "/agent-tree").then(result => {
        if (isSessionDraft() || !state.selected || (this.root !== id && state.selected !== id)) return;
        familyThreads = result.data;
        const hasAgents = result.data.some(t => t.thread_id !== result.root_thread_id && t.browser_session !== false);
        if (hasAgents || currentTab()?.tree_root)
          state.threadTabs?.setTree(hasAgents ? result.root_thread_id : null, hasAgents && this.mode === "agents");
        this.update(); state.listSignature = ""; renderList();
      }).finally(() => { if (pending === request) pending = null; });
      return request.promise;
    },
    update(work) {
      if (work && work.thread_id !== "new-session") records.set(work.thread_id, work);
      // Inventory wins over old detail snapshots, except for the displayed agent.
      const family = new Map(familyThreads.map(t => [t.thread_id, t]));
      const all = [...records.values(), ...familyThreads, ...closedThreads, ...openThreads].map(t =>
        family.has(t.thread_id) ? {...t, agent_parent_id: family.get(t.thread_id).agent_parent_id} : t);
      if (state.detail?.work_thread?.thread_id === state.selected && !isSessionDraft() && state.selected !== "new-session") {
        const work = state.detail.work_thread, relationship = family.get(work.thread_id);
        all.push(relationship ? {...work, agent_parent_id: relationship.agent_parent_id} : work);
      }
      this.tree = AgentTree.graph(all);
      breadcrumb?.update(this.tree, isSessionDraft() ? null : state.selected);
      if (state.selected && !isSessionDraft()) state.threadTabs?.reconcileFamily?.(this.tree, state.selected);
      if (!state.selected || isSessionDraft() || state.selected === "new-session") {
        this.root = null;
        $("panel-agents").hidden = true;
        $("panel-context").hidden = true;
        $("context-info").setAttribute("role", "region");
        $("conversation").classList.remove("has-agent-tree");
        if (this.mode === "agents") this.setMode("context", false);
        return;
      }
      const tab = currentTab();
      const selectedRoot = this.tree.root(state.selected);
      const tabRoot = tab?.tree_root && this.tree.family(this.tree.root(tab.tree_root)).includes(state.selected)
        ? this.tree.root(tab.tree_root) : null;
      this.root = tabRoot || selectedRoot;
      const hasAgents = this.hasAgents(this.root);
      if (tab?.tree_root && (!hasAgents || this.root !== tab.tree_root))
        state.threadTabs.setTree(hasAgents ? this.root : null, hasAgents && tab.tree_open);
      if (this.root && this.tree.nodes.has(this.root)) panel.update([...this.tree.nodes.values()], this.root, state.selected);
      $("panel-agents").hidden = !hasAgents;
      $("panel-context").hidden = !hasAgents;
      $("context-info").setAttribute("role", hasAgents ? "tabpanel" : "region");
      $("conversation").classList.toggle("has-agent-tree", hasAgents);
      if (!hasAgents && [$("panel-agents"), $("panel-context")].includes(document.activeElement))
        $(state.mobileChat?.matches ? "mobile-sheet-back" : "toggle-context").focus();
      if (!hasAgents && this.mode === "agents") {
        this.setMode("context", false);
      }
    },
    childCount(id) { return Math.max(0, this.tree.family(id).filter(child => child !== id && this.tree.nodes.get(child)?.browser_session !== false).length); },
    recipient() {
      const tab = currentTab();
      if (!tab?.tree_root) return "";
      return (state.detail?.work_thread?.thread_id === state.selected ? state.detail.work_thread.title : this.tree.nodes.get(state.selected)?.title) || "selected agent";
    },
    bind({preservePanel = false, dock = false} = {}) {
      if (!preservePanel) familyThreads = [];
      const draft = !state.selected || isSessionDraft() || state.selected === "new-session";
      if (!preservePanel || draft) {
        if (typeof readSavedContextCollapsed === "function") contextCollapsed = readSavedContextCollapsed();
        this.setMode("context", false);
      } else layoutContext();
      this.update();
      // Discover closed ancestors and children even when overview filters hide them.
      if (!dock && !state.paneMode && !draft && !state.config?.conversationOpen) this.load().catch(showError);
    },
    setMode(mode, remember = true) {
      if (mode === "agents" && !this.hasAgents(this.root)) mode = "context";
      this.mode = mode;
      if (mode !== "agents") panel.clearMessages();
      $("agent-tree-panel").hidden = mode !== "agents"; $("context-info").hidden = mode === "agents";
      $("context-panel").classList.toggle("agents-mode", mode === "agents");
      $("context-panel").setAttribute("aria-label", mode === "agents" ? "Agent tree" : "Thread context");
      for (const key of ["agents", "context"]) {
        $("panel-" + key).setAttribute("aria-selected", String(mode === key));
        $("panel-" + key).tabIndex = mode === key ? 0 : -1;
      }
      syncGrip?.();
      if (remember) state.threadTabs?.setTree(this.hasAgents(this.root) ? this.root : null, mode === "agents");
      layoutContext();
    },
    async open(id = state.selected) {
      if (!id || id === "new-session") return;
      if (state.paneMode) { await window.parent.agentCoordOpenTree(id); return; }
      if (globalThis.agentCoordPanes?.active) globalThis.agentCoordPanes.leave();
      if (state.selected !== id || !currentTab()) await select(id);
      if (state.selected !== id) return;
      this.update();
      this.root = currentTab()?.tree_root || this.tree.root(id);
      if (this.hasAgents(this.root)) {
        state.threadTabs?.setTree(this.root, true);
        this.setMode("agents");
        setContextCollapsed(false, {persist: false});
        if (state.mobileChat?.matches) state.mobileChat.open("context");
        panel.fit(); renderStatus();
      }
      try { await this.load(); }
      catch (error) {
        panel.empty.textContent = "Could not load the full tree. Open Agents again to retry.";
        if (!panel.tree?.nodes.size) panel.empty.hidden = false;
        throw error;
      }
      if (state.selected === id && this.hasAgents(this.root)) {
        state.threadTabs?.setTree(this.root, true);
        if (this.mode !== "agents") {
          this.setMode("agents");
          setContextCollapsed(false, {persist: false});
          if (state.mobileChat?.matches) state.mobileChat.open("context");
          renderStatus();
        }
        this.update(); panel.fit();
      }
    },
  };
  const panel = new AgentTree.Panel({document, window, host: $("agent-tree-panel"), providerLogo: globalThis.providerLogo,
    onSelect: id => action(async () => {
      if (id === state.selected) return;
      const tabId = state.threadTabs?.selected;
      // Use the ordinary conversation path so all state remains thread-local.
      await select(id, {tabId, focus: false});
    })});
  window.agentCoordOpenTree = id => controller.open(id);
  $("panel-agents").onclick = () => action(() => controller.open());
  $("panel-context").onclick = () => controller.setMode("context");
  $("close-context-panel").onclick = () => {
    if (state.mobileChat?.matches) state.mobileChat.close();
    else setContextCollapsed(true);
  };
  for (const key of ["agents", "context"]) $("panel-" + key).onkeydown = event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? "agents" : event.key === "End" ? "context" : key === "agents" ? "context" : "agents";
    if ($("panel-" + next).hidden) return;
    $("panel-" + next).focus(); $("panel-" + next).click();
  };
  const grip = $("agent-panel-resize");
  let resizing = false, width = 52, contextWidth = 340;
  try { width = Number(localStorage.getItem("agent-coord.agent-panel-width")) || 52; } catch { /* Optional preference. */ }
  try { contextWidth = Number(localStorage.getItem("agent-coord.context-panel-width")) || 340; } catch { /* Optional preference. */ }
  var syncGrip = () => {
    const agents = controller.mode === "agents";
    grip.setAttribute("aria-label", agents ? "Resize agent tree panel" : "Resize thread context panel");
    grip.setAttribute("aria-valuemin", agents ? "30" : "220");
    grip.setAttribute("aria-valuemax", agents ? "70" : "560");
    grip.setAttribute("aria-valuenow", String(Math.round(agents ? width : contextWidth)));
  };
  const resize = value => {
    if (controller.mode === "agents") {
      width = Math.max(30, Math.min(70, value));
      $("conversation").style.setProperty("--agent-panel-width", width + "%");
    } else {
      contextWidth = Math.max(220, Math.min(560, Math.round(value)));
      $("conversation").style.setProperty("--context-panel-width", contextWidth + "px");
    }
    syncGrip();
  };
  $("conversation").style.setProperty("--agent-panel-width", Math.max(30, Math.min(70, width)) + "%");
  $("conversation").style.setProperty("--context-panel-width", Math.max(220, Math.min(560, Math.round(contextWidth))) + "px");
  syncGrip();
  const saveWidth = () => {
    try {
      if (controller.mode === "agents") localStorage.setItem("agent-coord.agent-panel-width", width);
      else localStorage.setItem("agent-coord.context-panel-width", contextWidth);
    } catch { /* Optional preference. */ }
  };
  grip.onpointerdown = event => { resizing = true; grip.setPointerCapture(event.pointerId); event.preventDefault(); };
  grip.onpointermove = event => {
    if (!resizing) return;
    const rect = $("conversation").getBoundingClientRect();
    const px = rect.right - event.clientX;
    resize(controller.mode === "agents" ? px / rect.width * 100 : Math.min(px, Math.max(220, rect.width - 300)));
  };
  grip.onpointerup = grip.onpointercancel = () => { resizing = false; saveWidth(); };
  grip.onkeydown = event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (controller.mode === "agents") resize(event.key === "Home" ? 30 : event.key === "End" ? 70 : width + (event.key === "ArrowLeft" ? 5 : -5));
    else resize(event.key === "Home" ? 220 : event.key === "End" ? 560 : contextWidth + (event.key === "ArrowLeft" ? 24 : -24));
    saveWidth();
  };
}
let contextCollapsed = null;
function readSavedContextCollapsed() {
  try {
    const saved = localStorage.getItem("agent-coord.context-collapsed");
    if (saved === "true" || saved === "false") return saved === "true";
  } catch { /* The panel remains usable without preference storage. */ }
  return null;
}
contextCollapsed = readSavedContextCollapsed();
function layoutContext() {
  if (state.mobileChat?.matches) { state.mobileChat.layoutContext(); return; }
  const panel = $("context-panel"), button = $("toggle-context");
  const draft = isSessionDraft() || state.selected === "new-session" || $("conversation").classList.contains("session-draft");
  const expanded = document.body.classList.contains("chat-expanded");
  // Expansion temporarily hides context without changing the saved panel preference.
  const collapsed = expanded || draft || (contextCollapsed ?? compactContext.matches);
  const containsFocus = panel.contains(document.activeElement);
  panel.hidden = collapsed;
  $("conversation").classList.toggle("context-collapsed", collapsed);
  $("conversation").classList.toggle("agents-visible", !collapsed && state.agentTreeController?.mode === "agents");
  const label = (collapsed ? "Show " : "Hide ") + (state.agentTreeController?.mode === "agents" ? "agent tree" : "thread context");
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", label);
  button.title = label;
  if (collapsed && containsFocus) $(expanded ? "expand-chat" : "toggle-context").focus({preventScroll: true});
}
function setContextCollapsed(collapsed, {persist = true} = {}) {
  contextCollapsed = collapsed;
  if (persist) {
    try { localStorage.setItem("agent-coord.context-collapsed", String(collapsed)); } catch { /* Preferences are optional. */ }
  }
  layoutContext();
}
$("toggle-context").onclick = () => setContextCollapsed(!$("context-panel").hidden);
compactContext.addEventListener("change", layoutContext);
layoutContext();
if (typeof AgentTree !== "undefined") setupAgentTree();
state.scratchpad = new ThreadScratchpad({panel: $("thread-scratchpad"), input: $("scratchpad-text"), status: $("scratchpad-status"), api, compactStatus: true});
state.outgoing = new OutgoingMessages({itemText});
state.outgoing.onReceipt = (id, requestId) => {
  if (state.submissionQueue?.confirm(id, requestId)) state.durableDrafts?.acknowledge(id, requestId).catch(showError);
};
if (!state.paneMode) {
  state.projectScratchpad = new ProjectScratchpad({document, view: window, api});
  window.agentCoordProjectScratchpad = state.projectScratchpad;
}
ProjectScratchpad.bindShortcut(document, () => state.paneMode ? window.parent.agentCoordProjectScratchpad : state.projectScratchpad);
if (typeof SendWorkPalette !== "undefined") {
  if (!state.paneMode) window.agentCoordSendWork = new SendWorkPalette({document, view: window, api,
    workspace: () => state.config && state.folders?.selected?.cwd,
    threads: () => state.viewThreads || state.sessions, storage: sessionStorage,
    openThread: id => select(id).catch(showError)});
  SendWorkPalette.bindShortcut(document, () => state.paneMode ? window.parent.agentCoordSendWork : window.agentCoordSendWork);
}
state.mobileChat = new MobileChat({onBack: goHome, onLayout: () => state.timelineScroll?.layout(), onContextLayout: layoutContext});
window.agentCoordFindThreads = () => { goHome({restoreDispatcher: false}); $("search").focus(); $("search").select(); };
document.addEventListener("keydown", event => {
  if (event.defaultPrevented || document.querySelector("dialog[open]")) return;
  if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "f" &&
      (state.dispatchWorkspace?.active || state.paneMode && window.parent.document.body.classList.contains("dispatcher-workspace"))) {
    event.preventDefault();
    (state.paneMode ? window.parent : window).agentCoordFindThreads(); return;
  }
  if (event.key === "Escape" && document.body.classList.contains("chat-expanded")) {
    event.preventDefault(); setChatExpanded(false); $("expand-chat").focus(); return;
  }
  if (document.body.classList.contains("navigation-open")) {
    if (event.key === "Escape") { event.preventDefault(); setNavigation(false); $("menu-toggle").focus(); }
    if (event.key === "Tab") {
      const items = [...$("sidebar").querySelectorAll("a, button:not(:disabled), select, summary")].filter(item => item.getClientRects().length);
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
async function sendMessage(mode = "send", quiet = false) {
  const reviewing = state.detail?.reviews?.some(run => run.active);
  if (reviewing) mode = "steer";
  else if (state.detail?.running && ["antigravity", "kimi"].includes(state.detail?.work_thread?.client)) mode = "queue";
  const detail = state.detail;
  const dispatcher = !!detail?.work_thread?.dispatcher;
  const id = state.selected, text = $("message").value;
  const input = $("message"), doc = input.ownerDocument;
  const hadComposerFocus = doc?.activeElement === input;
  const navigationRevision = state.navigationRevision;
  const draft = isSessionDraft() ? state.newSessionDraft : null, sendKey = draft || id;
  const draftId = draft?.draftId || id;
  const ticket = state.rollUp?.ticket(id);
  const images = state.attachments?.snapshot(draftId) || [];
  const orderedInput = mode === "send" && !dispatcher && !reviewing && !text.trimStart().startsWith("/") &&
    state.outgoing?.pendingTransferCounts?.has(id) && ["codex", "claude"].includes(detail?.work_thread?.client);
  const ordinaryStartingInput = mode === "send" && (orderedInput || detail?.acceptsUntargetedInput) &&
    ["codex", "claude"].includes(detail.work_thread?.client) && !text.trimStart().startsWith("/");
  if (state.settingsMutations?.has(id) || state.transferLocks?.has(id) || state.sending?.has(sendKey) || !detail?.work_thread?.browser_session || detail.work_thread.attention === "archived" || state.closing?.has(state.selected) ||
      (!dispatcher && !reviewing && detail.running && !detail.activeTurn && !ordinaryStartingInput) || state.attachments?.pending(draftId) || (!text.trim() && !images.length)) return;
  const mentions = state.mentions?.snapshot(draftId, text) || [];
  const body = {message: text};
  if (mentions.length) body.mentions = mentions;
  if (state.sourceWindowId) body.windowId = state.sourceWindowId;
  if (images.length) body.images = images.map(({name, url}) => ({name, url}));
  if (!dispatcher && !reviewing && !orderedInput && detail.running && detail.activeTurn && mode !== "queue") body.expectedTurnId = detail.activeTurn;
  if (state.config?.quietSubmissions === 1 && (quiet || state.quietSubmissions?.has(id))) {
    body.quiet = true; state.quietSubmissions?.delete(id);
  }
  if (!draft && !dispatcher && !reviewing && !text.trimStart().startsWith("/") && !state.reviewUI?.settings?.has(id)) {
    const path = sessionPath(id) + (mode === "queue" ? "/queue" : "/messages");
    const result = await submitComposerMessage({id, text, images, mentions, body, path, detail, clear() {
      $("message").value = ""; state.drafts.delete(id);
      state.mentions?.sent(id, text, mentions);
      if (images.length) state.attachments?.sent(id, images);
      state.timelineScroll?.latest();
    }});
    if (id === state.selected) refreshDetail({background: true}).catch(showError);
    if (!result?.transferSubmission) state.rollUp?.responded(ticket, {pending: !!state.detail?.requests?.length});
    return;
  }
  (state.sending ||= new Set()).add(sendKey); renderStatus();
  let echo = null;
  const showOutgoing = () => {
    if (id === state.selected && state.timelineThread === id) state.outgoing.update(document, $("timeline"), id, state.detail);
  };
  let dispatchReceived = false;
  let commandCompleted = false;
  try {
    const command = text.trim().split(/\s+/)[0].toLowerCase();
    if (command === "/fork" || command === "/close") {
      if (text.trim().toLowerCase() !== command) throw new Error("Use " + command + " without arguments.");
      if (images.length) throw new Error("Remove attached images before using " + command + ".");
    }
    if (isSessionDraft()) {
      if (command === "/fork") throw new Error("Send a message before forking this conversation.");
      if (command === "/close") {
        state.drafts.delete("new-session"); state.attachments?.drafts.delete(draftId); state.mentions?.drafts.delete(draftId);
        $("message").value = ""; state.newSessionDraft = null; goHome(); return;
      }
      await sendSessionDraft(text, images, mentions, !!body.quiet); return;
    }
    if (command === "/fork" || command === "/close") {
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
    if (dispatcher && !text.trim().startsWith("/")) {
      body.askDispatcher = !!$("dispatch-direct")?.checked;
      body.requestId = DispatchUI.requestKey(sessionStorage, id, body);
    }
    if (!reviewing && !detail.running && mode !== "queue") state.reviewUI?.decorate(body, id);
    body.requestId ||= DispatchUI.requestKey(sessionStorage, id, body);
    // Queued, reviewed, and command input have their own displays.
    echo = !text.trim().startsWith("/") && body.review == null && (dispatcher || mode !== "queue")
      ? state.outgoing?.add(id, {text, images: images.length, requestId: dispatcher ? body.requestId : null,
        submissionId: body.requestId, detail: conversationEntry(id).detail || detail}) : null;
    if (echo) {
      if (id === state.selected && $("message").value === text) { $("message").value = ""; state.drafts.delete(id); }
      showOutgoing();
      if (id === state.selected) state.timelineScroll?.latest();
    }
    let result;
    try {
      result = await api(sessionPath(id) + (!dispatcher && mode === "queue" ? "/queue" : "/messages"), body);
    } catch (error) {
      // The authenticated stream can confirm durable intake before an HTTP response is lost.
      if (echo?.acceptance) result = {acceptedMessage: echo.acceptance};
      else if (echo?.transferReceipt) result = {transferSubmission: echo.transferReceipt};
      else throw error;
    }
    if (result.transferSubmission) {
      const receipt = result.transferSubmission;
      if (receipt.threadId !== id || receipt.requestId !== body.requestId || !["queued", "uncertain", "committed", "failed"].includes(receipt.state))
        throw new Error("Message receipt could not be confirmed.");
      state.outgoing.transfers(id, [receipt], conversationEntry(id).detail || detail);
      showOutgoing();
    } else if (echo) {
      if (result.acceptedMessage) state.outgoing.confirm(id, [result.acceptedMessage]);
      state.outgoing.accepted(echo); showOutgoing();
    }
    sessionStorage.removeItem("dispatch-request:" + id);
    state.reviewUI?.sent(id);
    dispatchReceived = !!result.dispatched;
    commandCompleted = !!result.command;
    if (result.dispatched) {
      sessionStorage.removeItem("dispatch-request:" + id);
      if (body.askDispatcher && state.dispatchDock?.active === id) state.dispatchDock.expand(true);
      if ($("dispatch-direct")) $("dispatch-direct").checked = false;
    }
    state.attachments?.sent(id, images);
    state.mentions?.sent(id, text, mentions);
    if (result.command) state.commandFeedback.set(id, result.command.message);
    else state.commandFeedback.delete(id);
    if (echo) { /* The composer was cleared on Enter; keep anything typed since. */ }
    else if (id === state.selected) {
      if ($("message").value === text) { $("message").value = ""; state.drafts.delete(id); }
      else state.drafts.set(id, $("message").value);
    } else if (state.drafts.get(id) === text) state.drafts.delete(id);
    if (state.durableDrafts) {
      const remaining = id === state.selected ? $("message").value : state.drafts.get(id) || "";
      const draft = {text: remaining, mentions: state.mentions?.snapshot(id, remaining) || [],
        images: state.attachments?.snapshot(id) || []};
      await writeComposerRecord(id, () => state.durableDrafts.write(id, draft));
    }
    if (result.transferSubmission && result.transferSubmission.state !== "committed") return;
    // Acceptance releases the composer. Neither transcript reconciliation nor
    // an unrelated inventory read is part of sending the user's message.
    const refreshFailed = error => showError(new Error("Your message was received, but the view could not refresh. " + error.message));
    const refreshing = id === state.selected ? refreshDetail({background: true}) : Promise.resolve();
    refreshing.then(() => {
      if (!result.command) return state.rollUp?.responded(ticket, {pending: !!state.detail?.requests?.length});
    }).catch(refreshFailed);
    refreshList({background: true}).catch(refreshFailed);
  }
  catch (error) {
    if (dispatchReceived) {
      console.warn("Dispatcher accepted request before UI refresh failed", error);
      throw new Error("Your request was received, but the view could not refresh. Do not send it again.");
    }
    if (echo?.status === "sending" && state.config?.interactionContinuity &&
        (error.status == null || error.status >= 500)) {
      echo.status = "uncertain";
      showOutgoing();
    } else if (echo?.status === "sending") {
      // Not accepted: say so where the message appeared and return the text.
      state.outgoing.rejected(echo, error.message || String(error));
      if (id === state.selected) { if (!$("message").value.trim()) $("message").value = text; }
      else if (!state.drafts.get(id)) state.drafts.set(id, text);
      showOutgoing();
    }
    throw error;
  }
  finally {
    state.sending.delete(sendKey); renderStatus();
    // Local commands leave the composer ready for the next message. Restore
    // focus only if refresh lost it, without overriding navigation or a click.
    if (commandCompleted && hadComposerFocus && state.selected === id && state.navigationRevision === navigationRevision &&
        !$("composer").hidden && !input.disabled && doc.hasFocus() && !doc.querySelector("dialog[open]") &&
        [input, doc.body, doc.documentElement].includes(doc.activeElement)) input.focus({preventScroll: true});
    if (state.paneMode) globalThis.agentCoordPane?.saveDraft();
  }
}
$("composer").onsubmit = event => { event.preventDefault(); action(sendMessage); };
async function toggleDispatcher(thread = state.detail?.work_thread) {
  if (!thread) return;
  await api(sessionPath(thread.thread_id) + "/dispatch", {enabled: !thread.dispatcher});
  if (!thread.dispatcher) state.dispatchDock?.remember(thread.project_cwd || thread.cwd, {id: thread.thread_id, hidden: false});
  if (state.selected === thread.thread_id) await refreshDetail();
  await refreshList();
}
function composerKeydown(event) {
  if (state.config?.quietSubmissions === 1 && TurnNotifications.isQuietShortcut(event)) {
    event.preventDefault();
    if (!$("send").disabled) action(() => sendMessage("send", true));
    else submitAfterFirstRead(true);
    return;
  }
  if (event.isComposing || event.keyCode === 229 || state.slashCommands?.composing || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
  if (state.slashCommands?.keydown(event)) return;
  if (state.mentions?.keydown(event)) return;
  if (event.key === "Enter") {
    if (globalThis.matchMedia?.("(hover: none) and (pointer: coarse)").matches) return;
    event.preventDefault();
    if (!$("send").disabled) requestComposerSubmit();
    else submitAfterFirstRead(false);
  } else if (event.key === "Tab" && state.detail?.running && ($("message").value.trim() || state.attachments?.snapshot().length)) {
    event.preventDefault();
    if (!$("queue").disabled) action(() => sendMessage("queue"));
  }
}
function submitAfterFirstRead(quiet) {
  const id = state.selected, entry = state.conversations?.get(id);
  if (state.detail || !state.detailRefreshing || !entry?.pending || state.attachments?.pending(id)) return;
  const revision = state.navigationRevision, text = $("message").value;
  const images = state.attachments?.snapshot(id) || [];
  if (!text.trim() && !images.length) return;
  const intent = entry.firstReadIntent = {};
  // Attach to this exact read. A retry or a later read cannot submit an
  // abandoned draft, and normal background reconciliation never holds Enter.
  entry.pending.then(fresh => {
    if (!fresh || intent !== entry.firstReadIntent || state.selected !== id || state.navigationRevision !== revision ||
        state.detail?.work_thread?.thread_id !== id || !state.detail.work_thread.browser_session ||
        $("send").disabled || $("message").value !== text || state.attachments?.pending(id)) return;
    const current = state.attachments?.snapshot(id) || [];
    if (current.length !== images.length || current.some((image,index) => image !== images[index])) return;
    if (quiet) action(() => sendMessage("send", true));
    else requestComposerSubmit();
  }).catch(() => {}).finally(() => { if (entry.firstReadIntent === intent) delete entry.firstReadIntent; });
}
function requestComposerSubmit() {
  if ($("composer").requestSubmit) $("composer").requestSubmit();
  else $("send").click();
}
$("message").onkeydown = composerKeydown;
$("quiet-send").onclick = () => {
  const id = state.selected;
  const quiet = state.quietSubmissions ||= new Set();
  if (quiet.has(id)) quiet.delete(id); else quiet.add(id);
  $("quiet-send").setAttribute("aria-pressed", String(quiet.has(id)));
};
// An idle conversation's Claude Code process may have been released. Start it
// while the user types, so sending need not wait for it; at most once in five
// minutes per conversation.
const PREWARM_INTERVAL = 300000, prewarmedAt = new Map();
$("message").addEventListener("input", () => {
  const work = state.detail?.work_thread, id = state.selected;
  if (!work?.browser_session || work.client !== "claude" || state.detail.running || isSessionDraft() || !$("message").value.trim()) return;
  if (Date.now() - (prewarmedAt.get(id) || 0) < PREWARM_INTERVAL) return;
  prewarmedAt.set(id, Date.now());
  api(sessionPath(id) + "/prewarm", {}).catch(() => {});
});
$("queue").onclick = () => action(() => sendMessage("queue"));
$("stop").onclick = () => action(async () => { await api(sessionPath(state.selected) + "/interrupt", {}); }, $("stop"));
async function toggleCloseWhenDone() {
  const id = state.selected;
  if (!id || !state.detail || state.closeWhenDoneSaving?.has(id)) return;
  state.closeWhenDoneSaving ||= new Set(); state.closeWhenDoneSaving.add(id);
  const request = state.detail.close_when_done;
  const enabled = !(request?.success_only && ["queued", "closing"].includes(request.status));
  renderStatus();
  try {
    const result = await api(threadPath(id) + "/close-when-done", {enabled});
    const entry = state.conversations?.get(id);
    if (entry) entry.revision++;
    if (id === state.selected && state.detail) state.detail.close_when_done = result;
  } finally { state.closeWhenDoneSaving.delete(id); renderStatus(); }
}
async function threadClosedWhenDone(id) {
  // The host handles the shared tab strip for pane documents.
  if (state.paneMode) return;
  const thread = await api(threadPath(id));
  if (thread.attention !== "archived") return;
  state.sessions = state.sessions.filter(item => item.thread_id !== id);
  state.viewThreads = state.viewThreads?.filter(item => item.thread_id !== id);
  state.threadActions?.notify("close:" + id, "Thread closed after successful completion");
  await state.threadTabs?.close(id, {force: true});
  if (state.selected === id) goHome();
  renderList();
  refreshList().catch(showError);
}
$("close-when-done").onclick = () => action(toggleCloseWhenDone);
async function toggleThreadClosed(thread = state.detail?.work_thread, onSuccess = () => {}) {
  const id = thread?.thread_id || state.selected;
  if (!id || !thread || state.closing.has(id) || state.confirmingCloses?.has(id)) return;
  const reopening = thread.attention === "archived";
  let closeOptions = {body: {}, subagents: []};
  if (!reopening && state.threadActions?.prepareClose) {
    state.confirmingCloses ||= new Set(); state.confirmingCloses.add(id);
    try { closeOptions = await state.threadActions.prepareClose(id, api); }
    finally { state.confirmingCloses.delete(id); }
    if (!closeOptions) return;
  }
  const previous = {...thread, thread_id: id};
  const snapshot = reopening ? null : state.threadActions?.captureClose(id);
  // Hide closing cards immediately, but keep their records for rollback. Polls
  // may still return the open thread while the provider finishes stopping it.
  state.pendingCloses ||= new Set();
  if (!reopening) state.pendingCloses.add(id);
  ++listRequest;
  state.closing.add(id); renderStatus();
  renderList();
  try {
    const result = await api(threadPath(id) + (reopening ? "/reopen" : "/close"), closeOptions.body);
    ++listRequest;
    if (!reopening && result?.closed_thread_ids) {
      const closedIds = new Set(result.closed_thread_ids);
      state.sessions = state.sessions.filter(item => !closedIds.has(item.thread_id));
      state.viewThreads = state.viewThreads?.filter(item => !closedIds.has(item.thread_id));
      // Remove stopped child tabs too, without navigating away from the parent.
      for (const child of result.closed_thread_ids.filter(child => child !== id)) {
        await state.threadTabs?.close(child, {force: true});
      }
      if (result.subagent_close_errors?.length) {
        refreshList().catch(showError);
        const errors = result.subagent_close_errors.map(item => {
          const child = closeOptions.subagents.find(child => child.thread_id === item.thread_id);
          return (child?.title || item.thread_id) + ": " + item.error;
        });
        throw new Error(`${closedIds.size} subagent thread${closedIds.size === 1 ? "" : "s"} closed. Parent kept open. ` + errors.join(" "));
      }
    }
    onSuccess();
    if (!reopening) {
      state.sessions = state.sessions.filter(item => item.thread_id !== id);
      state.viewThreads = state.viewThreads?.filter(item => item.thread_id !== id);
      const restored = new Set();
      const notice = result?.close_notice || (closeOptions.subagents.length
        ? `Thread and ${closeOptions.subagents.length} subagent thread${closeOptions.subagents.length === 1 ? "" : "s"} closed`
        : "Thread closed · " + (thread.title || "Available in Closed"));
      state.threadActions?.notify("close:" + id, notice, async () => {
        if (state.closing.has(id)) throw new Error("Wait for the thread to finish closing.");
        state.closing.add(id);
        try {
          // Do not undo a later decision made in another tab or conversation.
          for (const saved of [previous, ...closeOptions.subagents]) {
            const current = await api(threadPath(saved.thread_id));
            if (current.attention !== "archived" && !restored.has(saved.thread_id)) continue;
            await api(threadPath(saved.thread_id) + "/reopen", {});
            restored.add(saved.thread_id);
            if (saved.attention === "later") {
              const body = saved.snoozed_until > Date.now() / 1000
                ? {snoozed_until: saved.snoozed_until} : {attention: "later"};
              await api(threadPath(saved.thread_id), body);
            }
          }
          // Reopening restores visibility only; never submit or resume work.
          await refreshList();
          if (state.selected === id) await refreshDetail();
        } finally { state.closing.delete(id); renderStatus(); renderList(); }
      });
      state.threadActions?.closed(snapshot);
      if (state.selected === id) $("view").value = "active";
      // A confirmed thread close also removes its tab. /close is still busy
      // sending here, so bypass the guard for this completed lifecycle action.
      await state.threadTabs?.close(id, {force: true});
    }
    if (state.selected === id) {
      $("view").value = "active";
      if (reopening) await refreshDetail();
      else goHome();
    }
  } finally {
    ++listRequest;
    state.pendingCloses.delete(id);
    state.closing.delete(id); renderStatus(); renderList();
  }
  // The close response is authoritative. A slow inventory refresh must not
  // delay local feedback or keep the action busy after shutdown succeeds.
  refreshList().catch(showError);
}
$("close-thread").onclick = () => action(toggleThreadClosed, $("close-thread"));
$("park").onclick = () => action(() => toggleThreadLater(state.detail.work_thread), $("park"));
$("snooze-thread-button").onclick = () => openThreadSnooze(state.detail.work_thread);
$("resume-snooze").onclick = () => action(() => resumeThreadSnooze(state.detail.work_thread), $("resume-snooze"));
$("snooze-duration").onchange = () => {
  const custom = $("snooze-duration").value === "custom";
  $("snooze-custom-label").hidden = !custom; $("snooze-custom").required = custom;
  $("snooze-policy").textContent = $("snooze-duration").value === "none"
    ? "New results can appear in Attention. The thread stays in Later and the agent can keep working."
    : "Bring back to Attention at this time. Notifications stay quiet until then; the agent can keep working.";
  if (custom) $("snooze-custom").focus();
};
$("snooze-form").onsubmit = async event => {
  event.preventDefault();
  if ($("save-snooze").disabled) return;
  $("save-snooze").disabled = true; $("snooze-error").hidden = true;
  const revision = state.snoozeDialogRevision;
  try {
    const thread = state.snoozingThread, choice = $("snooze-duration").value;
    const deadline = choice === "none" ? null : snoozeTime(choice, $("snooze-custom").value);
    $("snooze-dialog").close();
    if (deadline === null) {
      const ticket = state.rollUp?.ticket(thread.thread_id);
      if (await updateThreadAttention(thread, {attention: "later"}) !== false) await state.rollUp?.responded(ticket);
    } else await snoozeThread(thread, deadline);
  } catch (error) {
    if (revision !== state.snoozeDialogRevision) { showError(error); return; }
    $("snooze-error").textContent = error.message + " Try again."; $("snooze-error").hidden = false;
    if (!$("snooze-dialog").open) $("snooze-dialog").showModal();
  }
  finally { if (revision === state.snoozeDialogRevision) $("save-snooze").disabled = false; }
};
$("handle-response").onclick = () => action(() => markThreadHandled(state.detail.work_thread), $("handle-response"));
$("session-name").onclick = startTitleEdit;
$("rename-name").oninput = () => {
  $("rename-name").setCustomValidity("");
  if (state.titleEdit) {
    state.titleEdit.error = "";
    (state.titleDrafts ||= new Map()).set(state.titleEdit.id, {value: $("rename-name").value});
    renderTitle();
  }
};
$("rename-name").onblur = () => saveTitleEdit({focus: false}).catch(showError);
$("rename-name").onkeydown = event => {
  if (event.key === "Escape") { event.preventDefault(); cancelTitleEdit(); }
  if (event.key === "Enter" && event.isComposing) event.preventDefault();
};
$("rename-form").onsubmit = event => { event.preventDefault(); action(saveTitleEdit); };
$("edit-permissions").onclick = openSessionPermissions;
$("status-group").onclick = () => action(() => organizeThread(state.detail?.work_thread), $("status-group"));
document.querySelectorAll("[data-close]").forEach(button => { button.onclick = () => $(button.dataset.close).close(); });
$("edit-checkpoint").onclick = () => {
  const work = state.detail.work_thread, cp = work.checkpoint;
  const domain = $("checkpoint-domain");
  domain.value = work.domain || ""; domain.dataset.original = domain.value;
  delete domain.dataset.reset; delete domain.dataset.edited;
  const counts = new Map();
  for (const thread of state.viewThreads || []) {
    if (thread.cwd === work.cwd && thread.attention !== "archived" && ["agent", "user"].includes(thread.domain_source) && thread.domain) {
      counts.set(thread.domain, (counts.get(thread.domain) || 0) + 1);
    }
  }
  const names = [...counts.keys()].sort((a, b) => counts.get(b) - counts.get(a) || a.localeCompare(b)).slice(0, 20);
  $("checkpoint-domains").replaceChildren(...names.map(name => new Option(name, name)));
  $("checkpoint-edit-phase").value = cp?.phase || "discussion"; $("checkpoint-summary").value = cp?.summary || "";
  $("checkpoint-next").value = cp?.next_action || ""; $("checkpoint-actor").value = cp?.next_actor || "nobody";
  $("checkpoint-dialog").showModal(); $("checkpoint-summary").focus();
};
$("checkpoint-domain-reset").onclick = () => {
  $("checkpoint-domain").value = ""; $("checkpoint-domain").dataset.reset = "true"; $("checkpoint-domain").focus();
};
$("checkpoint-domain").oninput = () => {
  delete $("checkpoint-domain").dataset.reset; $("checkpoint-domain").dataset.edited = "true";
};
$("checkpoint-form").onsubmit = event => { event.preventDefault(); action(async () => {
  const payload = {phase: $("checkpoint-edit-phase").value, summary: $("checkpoint-summary").value,
    next_action: $("checkpoint-next").value, next_actor: $("checkpoint-actor").value};
  const domain = $("checkpoint-domain"), value = domain.value.trim();
  // Editing progress alone must not turn an automatic domain into an override.
  if (domain.dataset.reset || domain.dataset.edited || value !== domain.dataset.original) payload.domain = value || null;
  await api(threadPath(state.selected) + "/checkpoint", payload);
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
async function forkThread(onSuccess = () => {}, source = state.selected) {
  const selected = state.selected;
  if (state.forking) return;
  state.forking = true; renderStatus();
  try {
    const fork = await api(threadPath(source) + "/fork", {});
    onSuccess();
    await refreshList();
    // A response must not pull the user away from another thread they opened.
    if (state.selected === selected) await select(fork.thread_id);
  } finally { state.forking = false; renderStatus(); }
}
// Only presentation state crosses windows. The backend remains the sole owner
// of the provider session, active turn, transcript, queue and pending requests.
function setupConversationTransfer({state, document, select, goHome, renderStatus}) {
  const $ = id => document.getElementById(id);
  const panes = () => window.agentCoordPanes;
  const staged = new Map();
  const field = id => panes()?.active && panes().records.get(id)?.api
    ? panes().records.get(id).api.input
    : state.selected === id ? $("message") : null;
  return {
    empty: () => state.threadTabs.items.length === 0,
    capture(id, {validate = true} = {}) {
      const shell = panes(), record = shell?.records.get(id);
      const tab = state.threadTabs.items.find(item => item.thread_id === id) || record?.thread
        || [...state.threadTabs.projects.values()].flat().find(item => item.thread_id === id)
        || (state.drafts.has(id) || state.attachments.drafts.has(id) || state.mentions?.drafts.has(id) ? {thread_id: id} : null);
      if (!tab || id === "new-session") return null;
      if (validate && (state.transferLocks?.has(id) || state.submissionQueue?.lanes?.has(id) || record?.api?.canTransfer?.() === false || state.settingsMutations?.has(id) || state.sending?.has(id) || state.attachments.pending(id) ||
          document.querySelector("dialog[open]") || record?.frame?.contentDocument?.querySelector("dialog[open]"))) return null;
      const thread = {...tab, ...state.conversations?.get(id)?.detail?.work_thread, ...record?.thread};
      const input = field(id), position = state.conversationPositions?.get(id) || state.conversations?.get(id);
      const draft = shell?.active && record ? record.api?.exportDraft() || record.initialDraft || shell.getDraft(id) : {
        text: input?.value ?? state.drafts.get(id) ?? "",
        images: state.attachments.items(id), mentions: state.mentions?.snapshot(id, input?.value ?? state.drafts.get(id) ?? "") || [],
        scroll: input ? state.timelineScroll?.position() ?? $("timeline").scrollTop : position?.scroll,
        following: input ? state.timelineScroll?.following : position?.following,
      };
      if (draft.images?.some(image => !image.url)) return null;
      let disclosures = [];
      try { disclosures = JSON.parse(sessionStorage.getItem("timeline-disclosures:" + id) || "[]"); }
      catch { /* Optional expansion state. */ }
      return {thread: {thread_id: id, title: thread.title}, cwd: thread.project_cwd || thread.cwd || state.projectCwd,
        sourceCwd: state.threadTabs.cwd, tabCount: state.threadTabs.items.length,
        draft: {...draft, following: draft.following ?? !Number.isFinite(draft.scroll), disclosures,
          selectionStart: input?.selectionStart, selectionEnd: input?.selectionEnd}};
    },
    lock(id, locked) {
      state.transferLocks ||= new Set();
      if (locked) state.transferLocks.add(id); else state.transferLocks.delete(id);
      panes()?.records.get(id)?.api?.setTransferLock?.(locked);
      renderStatus();
    },
    async stage(payload, transferId) {
      if (state.settingsMutations?.has(payload.thread.thread_id) || state.sending?.has(payload.thread.thread_id) || document.querySelector("dialog[open]"))
        throw new Error("Finish the current action in the destination window before moving a tab there.");
      const id = payload.thread.thread_id;
      staged.set(transferId, {id, incoming: payload.draft, before: this.capture(id, {validate: false}), selected: state.selected,
        wasOpen: [...state.threadTabs.projects.values()].some(items => items.some(item => item.thread_id === id)),
        paneView: panes()?.active ? panes().viewId : null});
      this.lock(id, true);
      try {
        if (state.durableDrafts) {
          await state.composerWrites?.get(id);
          const stored = await state.durableDrafts.read(id);
          const target = staged.get(transferId);
          target.stored = stored;
          if (stored && (stored.text || stored.images.length || stored.mentions.length) &&
              !this.sameDraft(stored, payload.draft))
            throw new Error("This window already has a different draft for this conversation.");
        }
        staged.get(transferId).restoring = true;
        await this.restore(payload);
        return true;
      } catch (error) {
        if (staged.get(transferId)?.restoring) await this.finish(transferId, false);
        else { staged.delete(transferId); this.lock(id, false); }
        throw error;
      }
    },
    sameDraft(first, second) {
      const a = first.images || [], b = second.images || [];
      return first.text === second.text && a.length === b.length &&
        a.every((image, index) => image.name === b[index].name && image.url === b[index].url) &&
        JSON.stringify(first.mentions || []) === JSON.stringify(second.mentions || []);
    },
    async prepareCommit(transferId) {
      const snapshot = staged.get(transferId);
      if (!snapshot) return false;
      if (!snapshot.prepared && state.durableDrafts) {
        const {id, incoming} = snapshot;
        await writeComposerRecord(id, () => state.durableDrafts.write(id,
          {text: incoming.text, images: incoming.images || [], mentions: incoming.mentions || []}));
      }
      snapshot.prepared = true;
      return true;
    },
    async finish(transferId, committed) {
      const snapshot = staged.get(transferId);
      if (!snapshot) return !committed;
      const {id, before, wasOpen, selected, paneView} = snapshot;
      if (committed && !snapshot.prepared) await this.prepareCommit(transferId);
      if (!committed && snapshot.prepared && state.durableDrafts)
        await writeComposerRecord(id, () => snapshot.stored ? state.durableDrafts.write(id, snapshot.stored) : state.durableDrafts.remove(id));
      staged.delete(transferId);
      this.lock(id, false);
      if (!committed) {
        if (before) {
          await this.restore(before, {rollback: true});
          if (!wasOpen) await this.release(id, state.threadTabs.cwd, undefined, {retainDraft: true});
        }
        else await this.release(id, undefined, undefined, {preserveStorage: true});
        if (selected && selected !== id) await select(selected, {focus: false});
        else if (!selected) goHome({restoreDispatcher: false});
        if (paneView != null) panes()?.restoreView?.();
      }
      return true;
    },
    async restore(payload, {rollback = false} = {}) {
      const id = payload?.thread?.thread_id, draft = payload?.draft;
      if (typeof id !== "string" || !id || id === "new-session" || !draft || typeof draft.text !== "string")
        throw new Error("The detached conversation state is invalid.");
      const existing = this.capture(id, {validate: false});
      if (!rollback && existing && (existing.draft.text || existing.draft.images?.length) &&
          !this.sameDraft(existing.draft, draft))
        throw new Error("This window already has a different draft for this conversation.");
      if (panes()?.active || state.dispatchDock?.active) goHome({restoreDispatcher: false});
      state.drafts.set(id, draft.text);
      state.attachments.drafts.set(id, draft.images || []);
      (state.attachmentSnapshots ||= new Map()).set(id, state.attachments.drafts.get(id).filter(image => image.url));
      state.mentions?.restore(id, draft.text, draft.mentions || []);
      const disclosures = Array.isArray(draft.disclosures) ? draft.disclosures : [];
      try { sessionStorage.setItem("timeline-disclosures:" + id, JSON.stringify(disclosures)); }
      catch { /* Optional expansion state. */ }
      if (state.conversations?.has(id)) state.conversations.get(id).disclosures = new Set(disclosures);
      (state.conversationPositions ||= new Map()).set(id, {scroll: draft.scroll || 0, following: draft.following === true});
      if (state.selected !== id) await select(id, {focus: false});
      if (state.selected !== id || state.detail?.work_thread?.thread_id !== id)
        throw new Error("The conversation could not be opened. Its original tab is still available.");
      $("message").value = draft.text;
      state.drafts.set(id, draft.text);
      state.threadTabs.open({...payload.thread, project_cwd: payload.cwd});
      if (Number.isFinite(draft.selectionStart)) $("message").setSelectionRange(draft.selectionStart, draft.selectionEnd);
      renderStatus(); state.resizeComposer?.();
      // Wait for the composer and transcript to receive their final geometry.
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      if (draft.following) state.timelineScroll?.latest();
      else if (Number.isFinite(draft.scroll)) {
        if (state.timelineScroll) state.timelineScroll.restore(draft.scroll);
        else $("timeline").scrollTop = draft.scroll;
      }
      $("message").focus({preventScroll: true});
      return true;
    },
    async discard(id, {preserveStorage = false} = {}) {
      if (!preserveStorage && state.durableDrafts) await writeComposerRecord(id, () => state.durableDrafts.remove(id));
      state.attachmentSnapshots?.delete(id);
      state.drafts.delete(id); state.attachments.drafts.delete(id); state.mentions?.drafts.delete(id);
    },
    async release(id, cwd = state.projectCwd, expected, {retainDraft = false, preserveStorage = false} = {}) {
      if (expected) {
        // Unrelated sends/dialogs may start in another tab while the target
        // loads. Only an actual draft change can invalidate this commit.
        const current = this.capture(id, {validate: false});
        if (!current || !this.sameDraft(current.draft, expected.draft))
          throw new Error("Your draft changed while the window was opening. The original tab has been kept.");
      }
      if (retainDraft) {
        const draft = this.capture(id, {validate: false})?.draft;
        if (draft) {
          state.drafts.set(id, draft.text); state.attachments.drafts.set(id, draft.images || []);
          state.mentions?.restore(id, draft.text, draft.mentions || []);
        }
      }
      // Closing a tab is navigation only; it never closes/stops its conversation.
      if (cwd === state.threadTabs.cwd) await state.threadTabs.close(id, {force: true});
      else {
        const items = state.threadTabs.projects.get(cwd) || [], index = items.findIndex(item => item.thread_id === id);
        if (index >= 0) items.splice(index, 1);
        if (state.threadTabs.selections.get(cwd) === id) state.threadTabs.selections.set(cwd, items[Math.min(index, items.length - 1)]?.thread_id || null);
        state.threadTabs.save(false, cwd);
      }
      const shell = panes();
      for (const [key, layout] of shell?.layouts || []) {
        const slot = layout.groups.findIndex(group => group.includes(id));
        if (slot < 0) continue;
        const group = layout.groups[slot]; group.splice(group.indexOf(id), 1);
        if (!group.length) paneLayout.removeGroup(layout, slot);
        else if (layout.tabs[slot] === id) layout.tabs[slot] = group[0];
        layout.order = layout.order.filter(value => value !== id);
        if (layout.selected === id) layout.selected = layout.tabs[0];
        layout.maximized = null;
        if (!layout.groups.length) {
          shell.layouts.delete(key);
          if (shell.active && shell.viewId === key) { shell.leave(); goHome({restoreDispatcher: false}); }
        } else if (shell.active && shell.viewId === key) { shell.render(); shell.focus(layout.selected); }
      }
      const record = shell?.records.get(id);
      record?.frame?.remove(); shell?.records.delete(id);
      // Prevent navigation history from restoring the moved draft in this window.
      if (!retainDraft) await this.discard(id, {preserveStorage});
    },
  };
}

function setupComposerSizing(input) {
  let frame = 0, width;
  const resize = () => {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (!input.getClientRects().length) return;
      // Reset before measuring so deleting text also shrinks the composer.
      // CSS retains the height limits for desktop, mobile, and tiled chats.
      input.style.height = "auto";
      input.style.height = input.scrollHeight + "px";
    });
  };
  input.addEventListener("input", resize);
  const observer = new ResizeObserver(entries => {
    const nextWidth = entries[0].contentRect.width;
    if (nextWidth !== width) { width = nextWidth; resize(); }
  });
  observer.observe(input);
  window.addEventListener("resize", resize);
  resize();
  return resize;
}
function workspaceConfig(config) {
  // Retained interaction gateways decorate the legacy detail read with transfer
  // receipts. Prefer that read until the same guarantee covers composite open.
  if (config.interactionContinuity === true && config.conversationOpenTransferReceipts !== 1)
    return {...config, conversationOpen: 0};
  return config;
}
async function boot() {
  const refreshTimeFormats = () => { messageTimeFormats = null; };
  window.addEventListener("languagechange", refreshTimeFormats);
  window.addEventListener("focus", refreshTimeFormats);
  const historyNavigation = new agentCoordNavigation.HistoryNavigation(window);
  state.initializeConversationView = () => {
    state.permissionMenu = new PermissionMenu({document, view: window});
    state.resizeComposer = setupComposerSizing($("message"));
    state.timelineScroll = new ConversationScroll($("timeline"), $("jump-to-latest"));
    state.responseActions = new ResponseActions({timeline: $("timeline"), input: $("message"), composer: $("composer"),
      canDiscuss: () => !!state.selected && !$("conversation").hidden && !$("composer").hidden && !$("message").disabled,
      onHold: () => state.timelineScroll.hold(), onError: showError});
    state.initializeConversationView = null;
  };
  if (!state.paneMode) {
    let storage; try { storage = localStorage; } catch { /* Optional preference. */ }
    state.dispatchDock = new DispatchUI.Dock({document, view: window, storage,
      open: id => action(() => select(id)), dismiss: receipt => action(async () => {
        await api(sessionPath(state.selected) + "/dispatch", {action: "dismiss", id: receipt});
        await refreshDetail();
      }), onExpand: expanded => {
        notifications?.syncFocus();
        if (expanded) {
          state.timelineScroll?.layout();
          const work = state.detail?.work_thread;
          if (work && !state.detailRefreshing && state.dispatchDock?.active === work.thread_id) {
            action(() => api(threadPath(work.thread_id), {seen: true, seen_checkpoint_id: work.checkpoint?.id || 0,
              seen_completion_id: work.turn_completion?.id || 0}));
          }
        }
      }});
  }
  const initialNavigation = agentCoordNavigation.initialLink(location);
  const initialThread = decodeURIComponent(location.hash.slice(1));
  const initialDispatcher = new URLSearchParams(location.search).get("dispatcher");
  const initialMessage = new URLSearchParams(location.search).get("message");
  if (initialThread && /^\d+$/.test(initialMessage || "")) state.messageTarget = {thread: initialThread, id: initialMessage};
  // Consume the jump once; new tiled frames inherit the remaining page query.
  if (initialMessage !== null) {
    const clean = new URL(location.href);
    clean.searchParams.delete("message");
    history.replaceState(history.state, "", clean.pathname + clean.search + clean.hash);
  }
  state.config = workspaceConfig(await api("config"));
  globalThis.agentCoordJev?.start?.(state.config);
  state.snapshotCache = new ConversationSnapshotCache(state.config.conversationCacheScope);
  state.submissionQueue = new ConversationSubmissionQueue();
  if (globalThis.ComposerDrafts && globalThis.indexedDB) {
    state.durableDrafts = window.agentCoordComposerDrafts = ComposerDrafts.create({
      scope: await composerWindowScope(window)});
  }
  window.agentCoordDrainComposerWrites = drainComposerWrites;
  window.agentCoordStreamConnected = () => state.streamHealthy;
  window.agentCoordReconnect = async config => {
    const fresh = config || await api("config");
    if (!fresh?.token) throw new Error("Reconnect requires an authenticated workspace.");
    state.config = workspaceConfig(fresh);
    connect();
    refreshBackgroundView({reconcile: true});
  };
  if (state.config.interactionContinuity === true) {
    state.updateSubmissions = new UpdateSubmissionChanges({
      load: (id, after, signal) => api("update-submissions?thread=" + encodeURIComponent(id) +
        "&after=" + after, undefined, {signal}),
      receive: (id, records) => {
        if (id !== state.selected) return;
        const detail = state.conversations?.get(id)?.detail;
        const wasWaiting = state.outgoing.pendingTransferCounts.has(id);
        if (state.outgoing.transfers(id, records, detail) && state.timelineThread === id)
          state.outgoing.update(document, $("timeline"), id, detail);
        if (wasWaiting !== state.outgoing.pendingTransferCounts.has(id)) renderStatus();
      }
    });
    // Tiled conversations share the workspace window's existing provider stream.
    const owner = state.paneMode ? window.parent : window;
    const refreshReceipts = event => state.updateSubmissions.invalidate(event.detail);
    owner.addEventListener("agent-coord-update-submissions", refreshReceipts);
    state.updateSubmissions.select(state.selected);
    window.addEventListener("pagehide", () => {
      owner.removeEventListener("agent-coord-update-submissions", refreshReceipts);
      state.updateSubmissions.select(null);
    });
    window.addEventListener("pageshow", event => {
      if (!event.persisted) return;
      owner.addEventListener("agent-coord-update-submissions", refreshReceipts);
      state.updateSubmissions.select(state.selected);
    });
  }
  state.activity = globalThis.ActivityCollector?.bind({window, document, state, api});
  state.schedules = new ScheduledPromptsUI({document, api, openThread: select, onError: showError,
    supportsRecurrence: state.config.recurringSchedules === true,
    onChange: () => {
      if ($("view").value === "later" && state.inventoryOpen) renderList();
      else if (state.selected) state.schedules.renderThread($("thread-schedules"), state.selected);
    },
    getThread: id => state.sessions.find(thread => thread.thread_id === id) ||
      (state.detail?.work_thread?.thread_id === id ? state.detail.work_thread : null),
    getDraft: () => {
      const current = state.detail?.session;
      const settings = current ? {...current} : {...sessionContext(), client: "codex"};
      const thread = state.detail?.work_thread;
      for (const key of ["repository_id", "project_id"]) {
        if (thread && key in thread) settings[key] = thread[key];
      }
      return {settings, source: state.selected, message: state.selected ? $("message").value : "",
        images: state.attachments?.items(composerDraftId()) || [], mentions: state.mentions?.snapshot() || []};
    },
    onSaved: draft => {
      if (draft.source && state.selected === draft.source && $("message").value === draft.message) {
        $("message").value = ""; state.drafts.delete(draft.source); renderStatus();
      }
    }});
  state.schedules.connected = () => state.streamHealthy;
  state.mentions = new ThreadMentions({input: $("message"), menu: $("thread-mentions"), references: $("thread-references"),
    getThread: () => composerDraftId(),
    getWorkspace: () => state.detail?.session?.cwd || state.detail?.work_thread?.cwd || state.config.cwd,
    loadFiles: async query => {
      const root = state.detail?.session?.cwd || state.detail?.work_thread?.cwd || state.config.cwd;
      return root ? (await api("files/search", {root, query})).entries : [];
    },
    loadThreads: async () => {
      const results = await Promise.all([api("threads"), api("threads?archived=true")]);
      return [...results[0].data, ...results[1].data];
    },
    canComplete: () => state.slashCommands?.query()?.kind !== "skill" && !!state.detail?.work_thread?.browser_session && !$("composer").hidden && !sessionBusy() &&
      state.detail.work_thread.attention !== "archived" && !state.closing.has(state.selected),
    onChange: () => { persistComposer(); state.drafts.set(state.selected, $("message").value); state.resizeComposer?.(); globalThis.agentCoordPane?.saveDraft(); }});
  state.slashCommands = new ChatSlashCommands({input: $("message"), menu: $("slash-commands"),
    getThread: () => state.selected,
    getSession: () => ({...state.detail?.session,
      model: state.detail?.session?.model || (state.detail?.session && effectiveSessionSettings(state.detail.session).model),
      cwd: state.detail?.session?.cwd || state.detail?.work_thread?.cwd || state.config?.cwd}),
    catalogs: (state.modelCatalogs ||= new Map()),
    getModelClients: sessionModelClients,
    onSelectModel: selectDraftModel,
    loadModels: loadModelCatalog,
    loadSkills: async session => (await api("skills", {client: session.client || "codex", cwd: session.cwd})).data,
    onComplete: ({apply}) => {
      // Only a complete single-line local command can be applied here. Skill
      // insertion and Tab completion must never submit a model prompt.
      if (apply && !/[\r\n]/.test($("message").value.trim())) action(sendMessage);
    },
    canComplete: () => !!state.detail?.work_thread?.browser_session && !$("composer").hidden && !sessionBusy() &&
      state.detail.work_thread.attention !== "archived" && !state.closing.has(state.selected),
    onReplace: (start, end, text) => state.mentions?.draft().edit(start, end, text),
    onChange: () => { persistComposer(); state.mentions?.render(); state.drafts.set(state.selected, $("message").value); state.resizeComposer?.(); globalThis.agentCoordPane?.saveDraft(); }});
  state.attachments = new ChatImageAttachments({document, getThread: () => composerDraftId(),
    canAttach: () => !!state.detail?.work_thread?.browser_session && state.detail.work_thread.attention !== "archived" && !state.closing.has(state.selected) && !state.transferLocks?.has(state.selected),
    onChange: () => {
      persistAttachmentChanges();
      renderStatus();
    }, onError: showError});
  state.attachments.bind();
  window.addEventListener("pagehide", () => { clearTimeout(state.elapsedTimer); state.elapsedTimer = null; });
  document.addEventListener("visibilitychange", () => renderTurnElapsed());
  window.addEventListener("focus", () => refreshBackgroundView({reconcile: true}));
  document.addEventListener("visibilitychange", () => refreshBackgroundView({reconcile: true}));

  const Notifications = state.config.remote ? PhoneNotifications : TurnNotifications;
  notifications = new Notifications({
    button: $("notifications"),
    api,
    claim: async completion_id => (await api("notifications/claim", {completion_id})).claimed,
    claimApproval: async request_key => (await api("notifications/claim", {request_key})).claimed,
    claimSnooze: async snooze_id => (await api("notifications/claim", {snooze_id})).claimed,
    selected: () => window.agentCoordPanes?.active ? window.agentCoordPanes.selected :
      state.dispatchDock?.active && !state.dispatchDock.expanded ? null : state.selected,
    openThread: select,
    onError: showError,
  });
  if (state.config.remote) notifications.start().catch(showError);
  let windowStorage, preferences;
  try { windowStorage = sessionStorage; } catch { /* Session storage is optional. */ }
  try { preferences = localStorage; } catch { /* Views work without persistent storage. */ }
  state.overviewPreferences = preferences;
  state.overviewLayout = new OverviewLayout({document, window, storage: preferences,
    canNavigate: () => !document.querySelector("dialog[open]"),
    onChange: (_mode, {navigate}) => { state.listSignature = ""; if (navigate) goHome({restoreDispatcher: false}); else renderList(); }});
  const desktop = window.agentCoordDesktop;
  state.conversationWindow = desktop?.conversationWindow === true;
  // Web apps must survive a discarded browser session on iPad/iPhone. Only
  // temporary native conversation windows keep their working set in session storage.
  const workspaceStorage = state.conversationWindow ? windowStorage : preferences || windowStorage;
  const legacyWorkspaceStorage = state.conversationWindow ? null : windowStorage;
  const publishDesktopProjects = async snapshot => {
    try {
      await api("desktop-projects", {windowId: state.sourceWindowId, projects: snapshot.projects, active: snapshot.active});
    } catch (error) {
      // The local selection stays in the window and publishes on reconnection.
      if (error.code !== "coordinator_offline") throw error;
    }
  };
  state.folders = new WorkingFolders({storage: workspaceStorage, fallbackStorage: legacyWorkspaceStorage, preferences,
    key: (state.conversationWindow ? desktop.windowId : desktop?.folderSlot) || "browser", scope: state.config.workspaceRoot || "", api,
    onChange: desktop ? snapshot => publishDesktopProjects(snapshot).catch(showError) : null});
  const projectQuery = new URLSearchParams(location.search);
  // Browsers copy sessionStorage into a new window. Explicitly opening a
  // project there must not duplicate the originating window's project tabs.
  if (projectQuery.get("new_project") === "1") state.folders.reset();
  else await state.folders.restore(state.config.workspaceRoot).catch(showError);
  const initialDirectory = projectQuery.get("directory");
  if (initialDirectory && state.folders.selected?.cwd !== initialDirectory) await state.folders.open(initialDirectory).catch(showError);
  if (state.config.remote) {
    const shared = await api("desktop-projects").catch(error => { showError(error); return null; });
    if (shared?.projects?.length) await state.folders.replace(shared.projects, shared.active).catch(showError);
  } else if (desktop) {
    await publishDesktopProjects(state.folders.snapshot()).catch(showError);
    window.addEventListener("pagehide", () => fetch("/api/browser/desktop-projects", {
      method: "POST", keepalive: true,
      headers: {"Content-Type": "application/json", "X-Agent-Coord-Token": state.config.token},
      body: JSON.stringify({windowId: state.sourceWindowId, projects: [], active: ""}),
    }).catch(() => {}), {once: true});
  }
  state.folderPicker = new FolderPicker({document, folders: state.folders, api, desktop, remote: state.config.remote,
    onError: showError, onOpened: switchProject});
  state.projectTabs = new ProjectTabs({document, folders: state.folders, onSwitch: switchProject, onError: showError});
  // Restoring the selected view needs its definitions before inventory can choose
  // the right history filter. Reuse those exact reads for the first inventory.
  let initialDefinitions = {};
  const definitionRevision = listRequest;
  // All views, including Closed, use the open inventory for tab indicators.
  // Workspace validation has finished, so run this read alongside definitions.
  const inventoryEntities = state.inventoryEntities ||= {open: {}, closed: {}};
  initialDefinitions.open = api("threads?archived=false", undefined,
    {entity: inventoryEntities.open, priority: initialThread ? "low" : "auto"});
  initialDefinitions.open.catch(() => {}); // Consumed by refreshList unless startup is superseded.
  const viewApi = (path, body, options) => {
    const result = api(path, body, options);
    if (initialDefinitions && body === undefined && (path === "views" || path === "organization"))
      initialDefinitions[path] = result;
    return result;
  };
  savedViews = new SavedViews({document, api: viewApi, storage: windowStorage, preferences,
    getLayout: () => state.overviewLayout.mode,
    onRefresh: refreshList,
    notify: (...args) => state.threadActions.notify(...args),
    scope: (state.config.workspaceRoot || "") + ":" + (desktop?.folderSlot || "browser"),
    onSwitch: async () => { goHome(); await refreshList(); window.agentCoordPanes?.restoreView(); }, onError: showError});
  state.projectCwd = state.folders.selected?.cwd || "";
  state.sidecars = new SidecarsUI({document, api, connected: () => state.streamHealthy, getThreads: () => state.sessions, openThread: select, onError: showError});
  if (globalThis.ReviewUI) state.reviewUI = new ReviewUI({document, api, onError: showError, changed: renderStatus});
  window.addEventListener("pagehide", () => clearInterval(state.sidecars.timer), {once: true});
  let askStorage; try { askStorage = localStorage; } catch { /* Reply marks then last for this page. */ }
  const askThread = id => state.sessions?.find(thread => thread.thread_id === id)
    || (state.selected === id ? state.detail?.work_thread : state.conversations?.get(id)?.detail?.work_thread);
  state.asks = new AsksUI({document, api, typedAnswers: state.config.askTypedAnswers === 1, connected: () => state.streamHealthy, navigate: url => window.agentCoordNavigate(url), onError: showError,
    kinds: Boolean(state.config.askKinds), replyRecords: state.config.askReplies === true, storage: askStorage,
    threadInfo: askThread,
    // A reply sent from an ask is a message in its conversation; show it there at once.
    messageSent: id => { if (id === state.selected) scheduleDetail(); },
    // Where text for a conversation's message goes: its composer when it is open, else its saved
    // draft, which opening the conversation restores. Null when the conversation takes no messages.
    openComposer: async id => {
      if (state.selected === id) return !$("composer").hidden && !$("message").disabled ? $("message") : null;
      const thread = askThread(id);
      if (thread && (!thread.browser_session || thread.attention === "archived")) { action(() => select(id)); return null; }
      return {get value() { return state.drafts.get(id) || ""; }, set value(text) { state.drafts.set(id, text); },
        focus() { action(() => select(id)); }};
    }});
  const sidecarButton = document.createElement("button");
  sidecarButton.id = "attach-sidecar"; sidecarButton.type = "button"; sidecarButton.className = "quiet thread-menu-action";
  sidecarButton.textContent = "Attach sidecar…"; sidecarButton.hidden = true;
  sidecarButton.onclick = () => action(() => state.sidecars.open(state.detail?.work_thread));
  $("close-when-done").before(sidecarButton);
  await savedViews.setProject(state.projectCwd);
  state.viewDrops = ViewDrops.create({document, api,
    getThread: id => state.closing.has(id) || state.updatingThreads.has(id) ? null
      : state.sessions.find(thread => thread.thread_id === id),
    getViews: () => savedViews.items.filter(view => view.kind === "group").map(view => ({...view,
      ...savedViews.effective(view), cwd: savedViews.cwd, pending: savedViews.saves.has(view.id)})),
    getProjects: () => state.organization.projects,
    onChanged: async thread => {
      await refreshList();
      if (state.selected === thread.thread_id) await refreshThread();
    },
    notify: (...args) => state.threadActions.notify(...args), onError: showError});
  state.threadTabs = new ThreadTabs({document, container: $("conversation-tabs"),
    onOverviewMenu: (anchor, event) => state.overviewLayout.open(anchor, event),
    storage: workspaceStorage, fallbackStorage: legacyWorkspaceStorage,
    scope: (state.config.workspaceRoot || "") + ":" + ((state.conversationWindow ? desktop.windowId : desktop?.folderSlot) || "browser"),
    onError: showError, onScratchpad: openProjectScratchpad,
    onSelect: async id => {
      if (await globalThis.agentCoordPanes?.selectTab(id)) return;
      // Explicit overview tiling and dispatcher workspaces remain separate destinations.
      if (globalThis.agentCoordPanes?.active || state.dispatchDock?.active) goHome({restoreDispatcher: false});
      if (id) await select(state.threadTabs.conversationFor(id), {tabId: id});
      else goHome({restoreDispatcher: false});
    }});
  state.threadTabs.setProject(state.projectCwd);
  setupPaneShell({state, document, api, select, goHome, refreshList, showError, notifications: () => notifications,
    getView: () => state.projectCwd + ":" + savedViews.activeId,
    getThreads: () => threadsInProject(state.sessions).filter(thread => threadViews.matches(thread, savedViews.read().filters))});
  if (state.conversationWindow) {
    const closePane = state.threadTabs.onClose;
    state.threadTabs.onClose = async id => {
      const handled = await closePane?.(id);
      if (!state.threadTabs.items.length) {
        // Let tab/draft cleanup finish before the native view is released.
        setTimeout(() => {
          if (!state.threadTabs.items.length) desktop.closeConversationWindow().catch(showError);
        }, 0);
        return true;
      }
      return handled;
    };
    $("conversation-add-tab").onclick = () => newSession().catch(showError);
  }
  state.dispatchWorkspace = new DispatcherWorkspace.Workspace({document, view: window, storage: preferences,
    panes: window.agentCoordPanes, fetchThread: id => api(threadPath(id)), onError: showError,
    dismissThread: async thread => {
      await markThreadHandled(thread);
      state.dispatchWorkspace.update(state.viewThreads, state.detail);
    },
    openDispatcher: () => state.dispatchDock.expand(true)});
  setupRollUp({document, state, api, select, action, markHandled: markThreadHandled});
  window.addEventListener("agent-coord-pane-state", () => {
    if (window.agentCoordPanes?.active) {
      state.rollUp.stop();
      if (!window.agentCoordPanes.tabLayout) state.threadTabs?.suspend();
    }
  });
  const inventoryProject = state.projectCwd;
  const inventoryReady = refreshList({priority: initialThread ? "low" : "auto",
    initialDefinitions: listRequest === definitionRevision ? initialDefinitions : null}).then(() => {
    if (state.projectCwd === inventoryProject) savedViews.restoreScroll();
  });
  initialDefinitions = null;
  // A direct conversation has its own detail and history routes.
  if (initialThread && initialThread !== "new-session" && !initialNavigation && !initialDispatcher) {
    inventoryReady.catch(showError);
  } else await inventoryReady;
  connect();
  navigation = new agentCoordNavigation.Router({api, history, location, windowId: state.sourceWindowId, onError: showError,
    blocked: () => Boolean(document.querySelector("dialog[open]")),
    capture: () => ({directory: state.projectCwd, viewId: savedViews.activeId, ...savedViews.read(),
      ...(savedViews.current().kind === "group" ? {groupId: savedViews.current().projectId} : {}),
      ...(state.projectScratchpad?.tabActive ? {scratchpad: true} : {}),
      ...(state.dispatchWorkspace?.active ? {dispatcher: state.dispatchWorkspace.id} : {}),
      ...(state.threadTabs?.selected && state.threadTabs.selected !== state.selected ? {conversationTab: state.threadTabs.selected} : {}),
      tiled: Boolean(window.agentCoordPanes?.active && !state.dispatchWorkspace?.active),
      thread: window.agentCoordPanes?.active ? window.agentCoordPanes.selected : state.dispatchDock?.active ? null : state.selected}),
    apply: async route => {
      if (route.kind === "thread") {
        // Message results open a full conversation so an exact target is visible
        // even when the overview previously used tiled panes.
        if (route.item && window.agentCoordPanes?.active) goHome();
        await openSearchTarget(route);
      }
      else if (route.kind === "view") {
        const views = (await api("views")).data;
        const view = views.find(item => item.id === route.id);
        if (view?.cwd) await openProjectDirectory(view.cwd);
        savedViews.sync(views);
        await savedViews.activate(route.id);
        goHome();
      } else {
        // Old repository links still open the corresponding directory project.
        const repository = state.organization.repositories.find(item => item.id === route.filters.repository);
        if (repository) await openProjectDirectory(repository.root);
        const group = state.organization.projects.find(item => item.id === route.filters.project);
        if (group?.cwd) await openProjectDirectory(group.cwd);
        if (group) await savedViews.openGroup(group.id);
        else await savedViews.overview({...route.filters, repository: ""});
        goHome();
      }
    },
    restore: async snapshot => {
      if (snapshot.directory) await openProjectDirectory(snapshot.directory);
      else if (snapshot.directory === "" && state.projectCwd) { state.folders.activate(""); await switchProject({restoreConversation: false}); }
      if (snapshot.viewId === "all") await savedViews.overview(snapshot.filters, snapshot.group_by);
      else await savedViews.activate(snapshot.viewId, false, {revealGroup: !snapshot.thread});
      if (snapshot.dispatcher) await select(snapshot.dispatcher, {dock: true, focus: false});
      else if (snapshot.tiled) window.agentCoordPanes?.restoreView();
      else goHome();
      if (snapshot.thread) await select(snapshot.thread, {tabId: snapshot.conversationTab || null});
      else if (snapshot.scratchpad) openProjectScratchpad();
    },
  });
  window.agentCoordNavigate = (url, options) => navigation.open(url, options);
  window.addEventListener("agent-coord-pane-state", () => { if (window.agentCoordPanes?.active) navigation.remember(); });
  document.addEventListener("click", event => {
    const link = event.target.closest("a[href]");
    if (!link?.getAttribute("href")?.startsWith("agentcoord:") || event.button !== 0) return;
    event.preventDefault();
    navigation.open(link.getAttribute("href"));
  });
  if (initialNavigation) await navigation.open(initialNavigation, {push: false});
  if (initialDispatcher) await select(initialDispatcher, {dock: true, focus: false});
  if (initialThread && initialThread !== initialDispatcher) {
    const home = state.threadTabs.destination();
    await select(initialThread, {tabId: home && state.threadTabs.conversationFor(home) === initialThread ? home : null});
  }
  if (!initialThread && !initialDispatcher && projectQuery.get("scratchpad") === "1") openProjectScratchpad();
  if (!initialNavigation && !initialThread && !initialDispatcher && projectQuery.get("scratchpad") !== "1") {
    await restoreProjectTab().catch(showError);
  }
  if (initialThread && /^\d+$/.test(initialMessage || "")) focusAgentMessage(initialMessage);
  state.dispatchReady = true;
  syncDispatcherOverview();
  navigation.remember();
  window.agentCoordConversationTransfer = setupConversationTransfer({state, document, select, goHome, renderStatus});
  await historyNavigation.ready(navigation);
  recoverWorkspaceOutbox().catch(showError);
  window.agentCoordDesktop?.navigationReady();
  if (state.config.remote) await notifications.ready();
  // Checkpoints written by terminal agents arrive through the shared database.
  let metadataTimer;
  state.setPolling = connected => {
    clearInterval(metadataTimer);
    metadataTimer = connected ? null : setInterval(refreshBackgroundView, 5000);
    state.sidecars?.setEventsConnected(connected);
    state.schedules?.setEventsConnected(connected);
  };
  state.setPolling(!!state.streamHealthy);
  window.addEventListener("pagehide", () => state.setPolling(true));
  window.addEventListener("pageshow", event => { if (event.persisted) state.setPolling(!!state.streamHealthy); });
  window.addEventListener("pagehide", () => savedViews.remember());
}
performance.mark?.("workspace-evaluated");
boot().then(() => performance.mark?.("workspace-ready")).catch(showError);
