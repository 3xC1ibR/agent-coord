"use strict";
const $ = id => document.getElementById(id);
const state = {config: null, sessions: [], organization: {repositories: [], projects: []}, selected: null, detail: null, drafts: new Map(), closing: new Set(), pinning: new Set(), updatingThreads: new Set(), groupExpansion: new Map(), phaseScroll: new Map(), commandFeedback: new Map(), busy: false, listSignature: ""};
state.threadActions = createThreadActions(document, window);
let notifications;
let savedViews;
let navigation;
let conversationSearch;
state.sourceWindowId = window.agentCoordDesktop?.windowId || crypto.randomUUID();
state.paneMode = window.parent !== window && new URLSearchParams(location.search).get("pane") === "1";
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
async function api(path, body, requestOptions = {}) {
  const viewMutation = body !== undefined && (path === "views" || path.startsWith("views/"));
  if (viewMutation) ++listRequest;
  const options = {cache: "no-store", ...requestOptions};
  if (body !== undefined) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-Agent-Coord-Token": state.config.token}, body: JSON.stringify(body)});
  const response = await fetch(base + path, options);
  if (response.status === 401 && location.protocol === "https:") location.reload();
  let result;
  try { result = await response.json(); }
  catch (error) {
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
  if (!response.ok) throw Object.assign(new Error(result.error || "Request failed"), {status: response.status});
  return result;
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
async function refreshList({background = false} = {}) {
  state.schedules?.refresh({background: true}).catch(showError);
  if (state.paneMode) { window.parent.agentCoordPanes?.requestList({background}); return; }
  const view = $("view").value, pending = state.listRefresh;
  // Polls and streamed events must not keep superseding a slow inventory read.
  // Explicit refreshes after user actions still invalidate earlier snapshots.
  if (background && pending?.view === view && pending.request === listRequest) return pending.promise;
  const refresh = {view, request: ++listRequest};
  state.listRefresh = refresh;
  refresh.promise = loadThreadList(view, refresh.request);
  try { return await refresh.promise; }
  finally { if (state.listRefresh === refresh) state.listRefresh = null; }
}
async function loadThreadList(view, request) {
  // Open threads feed every tab's badges, including while browsing a Closed view.
  const [open, closed, organization, views] = await Promise.all([api("threads?archived=false"),
    view === "archived" ? api("threads?archived=true") : null, api("organization"), api("views")]);
  if (request !== listRequest || view !== $("view").value) return;
  state.sessions = (closed || open).data.map(overlayPendingAttention);
  state.viewThreads = open.data.map(overlayPendingAttention);
  state.agentTreeController?.inventory(state.viewThreads, closed?.data);
  state.threadTabs?.update([...(state.conversationWindow ? state.sessions : state.viewThreads), ...(closed?.data || [])]);
  state.dispatchWorkspace?.update(state.viewThreads, state.detail);
  state.rollUp?.sync(state.viewThreads);
  state.organization = organization;
  for (const field of ["repository", "project"]) {
    const select = $(field), selected = select.value, choices = organization[field === "repository" ? "repositories" : "projects"]
      .filter(item => field === "repository" || item.cwd === state.folders?.selected?.cwd);
    const signature = JSON.stringify(choices);
    if (select.dataset.signature === signature) continue;
    select.dataset.signature = signature;
    select.replaceChildren(new Option(field === "repository" ? "All repositories" : "All groups", ""),
      new Option(field === "repository" ? "No repository" : "No group", threadOrganization.NONE));
    for (const item of choices) {
      const duplicates = choices.filter(other => other.name === item.name).length > 1;
      const option = new Option(item.name + (duplicates && item.root ? " · " + item.root : ""), item.id);
      option.title = item.root || item.name; select.append(option);
    }
    if (selected && ![...select.options].some(option => option.value === selected)) {
      select.append(new Option("Unavailable " + (field === "project" ? "group" : field), selected));
    }
    select.value = selected;
  }
  savedViews?.sync(views.data, organization.projects);
  renderFolders();
  if (view !== $("view").value) return refreshList();
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
  if (!state.dispatchReady || state.dispatchStarting || state.busy || !state.folders?.selected?.cwd) return;
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
$("dispatcher-permissions").onclick = () => openSessionPermissions();
function sessionContext() {
  return newThreadContext(state.organization, {project: $("project").value}, state.folders?.selected);
}
function threadsInProject(threads) { return threads.filter(thread => inProject(thread, state.folders?.selected?.cwd)); }
async function switchProject({restoreConversation = true, preserveRollUp = false} = {}) {
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
    if (state.projectCwd === cwd) renderList();
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
  await refreshList({background: true});
  if (state.projectCwd === cwd && !state.selected) savedViews.restoreScroll();
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
async function openProjectDirectory(cwd, {preserveRollUp = false} = {}) {
  if (!cwd || state.folders.selected?.cwd === cwd) return;
  // A saved conversation remains accessible if its directory was moved/deleted.
  // New session creation still validates the directory on the server.
  state.folders.adopt({cwd});
  await switchProject({restoreConversation: false, preserveRollUp});
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
  await updateThreadAttention(thread, {resume_snooze: thread.snoozed_until});
  await state.rollUp?.responded(ticket);
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
    if (thread.can_handle_response) addAction("Dismiss", "handle", () => markThreadHandled(thread), endActions);
    if (thread.snooze_due) addAction("Dismiss reminder", "resume-snooze", () => resumeThreadSnooze(thread), endActions);
    if (endActions.children.length) actions.append(endActions);
    const titleActions = node("div", null, "card-title-actions");
    const more = node("button", "…", "card-more");
    more.type = "button"; more.title = "Thread options";
    more.dataset.thread = thread.thread_id; more.dataset.action = "more";
    more.setAttribute("aria-label", "Thread options · " + thread.title);
    more.setAttribute("aria-haspopup", "menu"); more.setAttribute("aria-expanded", "false");
    more.disabled = state.closing.has(thread.thread_id) || state.updatingThreads.has(thread.thread_id);
    more.onclick = event => {
      const items = [
        ...(thread.orchestration_workspaces?.includes(state.projectCwd)
          && (thread.project_cwd || thread.cwd) !== state.projectCwd
          ? [{label: "Open agent’s project", run: () => action(() => openProjectDirectory(thread.project_cwd || thread.cwd))}] : []),
        {label: thread.project_id ? "Change group" : "Add to group", run: () => action(() => organizeThread(thread))},
        {label: "Move to project…", disabled: !!threadMoveUnavailable(thread), run: () => openThreadMove(thread)},
        {label: thread.pinned ? "Unpin" : "Pin", run: () => action(() => toggleThreadPinned(thread))},
        {label: thread.attention === "later" ? "Change return time…" : "Move to Later…", run: () => openThreadSnooze(thread)},
      ];
      if (["codex", "claude"].includes(thread.client)) items.unshift({label: "Fork", disabled: !thread.can_fork || state.forking,
        run: () => action(() => forkThread(undefined, thread.thread_id))});
      if (thread.browser_session && thread.attention !== "archived") items.push({label: thread.dispatcher ? "Stop using as dispatcher" : "Use as dispatcher", run: () => action(() => toggleDispatcher(thread))});
      if (thread.browser_session && thread.attention !== "archived") items.push({label: "Attach sidecar…", run: () => action(() => state.sidecars.open(thread))});
      if (thread.attention === "later") items.unshift({label: "Move to Now", run: () => action(() => toggleThreadLater(thread))});
      state.threadActions.showMenu(more, thread.title, items, event);
    };
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
function createOverviewMotion() {
  const running = new Map(), reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  let previousView;
  const stop = () => { for (const motion of running.values()) motion.stop(); running.clear(); };
  // Floating copies must never lag behind scrolling, resizing, or navigation.
  window.addEventListener("scroll", stop, {capture: true, passive: true});
  window.addEventListener("resize", stop, {passive: true});
  reduced.addEventListener("change", stop);
  function cards() {
    return [...$("overview").querySelectorAll("[data-card-thread]")].map(card => {
      const group = card.closest("[data-group]").dataset.group;
      // A pin is a separate card; the other copy can move between work stages.
      return {card, id: JSON.stringify([card.dataset.cardThread, group === "pinned"]), group,
        rect: card.getBoundingClientRect()};
    });
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
  globalThis.agentCoordSendWork?.syncOverview();
  if (state.paneMode) { renderStatus(); return; }
  if (state.dispatchDock) syncDispatcherOverview();
  renderClosedToggle();
  savedViews?.render(threadsInProject(state.viewThreads || state.sessions).filter(t => !state.pendingCloses?.has(t.thread_id)));
  if (conversationSearch?.update()) {
    if ($("atlas-overview")) $("atlas-overview").hidden = true;
    if ($("working-set-overview")) $("working-set-overview").hidden = true;
    renderStatus(); return;
  }
  const query = $("search").value.trim().toLocaleLowerCase();
  const workspaceThreads = threadsInProject(state.sessions).filter(t => !state.pendingCloses?.has(t.thread_id));
  const projectThreads = workspaceThreads.filter(t => threadOrganization.matches(t, "", $("project").value));
  const workingSet = state.overviewLayout?.mode === "working-set";
  const matchesView = t => threadViews.matches(workingSet && t.attention === "later" && ["active", "completed"].includes($("view").value) ? {...t, attention: "now"} : t,
    {show: $("view").value, phase: $("phase-filter").value, search: query});
  const threads = projectThreads.filter(matchesView);
  // The map keeps neighboring groups available while the tabs own selection.
  const workingThreads = workingSet && savedViews?.activeId.startsWith("group-") ? workspaceThreads.filter(matchesView) : threads;
  const upcoming = upcomingSchedules(query);
  const archived = $("view").value === "archived";
  const groupBy = $("group-by").value;
  $("group-by-domain").setAttribute("aria-pressed", String(groupBy === "domain"));
  const viewId = (state.projectCwd || "") + ":" + (savedViews?.activeId || "all");
  const signature = JSON.stringify([state.overviewLayout?.mode, threads, upcoming, state.selected, [...(state.closing || [])], [...state.pinning], [...state.updatingThreads], archived, query, $("phase-filter").value, $("view").value, $("repository").value, $("project").value, groupBy, viewId, Math.floor(Date.now() / 60000)]);
  $("back-home").setAttribute("aria-current", state.selected && !state.dispatchDock?.active ? "false" : "page");
  const projects = new Set(threads.map(t => t.project_id).filter(Boolean)).size;
  $("results-count").textContent = threads.length + (threads.length === 1 ? " thread" : " threads") +
    " · " + projects + (projects === 1 ? " group" : " groups") +
    (upcoming.length ? " · " + upcoming.length + (upcoming.length === 1 ? " scheduled prompt" : " scheduled prompts") : "");
  if (signature === state.listSignature) { if (state.overviewLayout) renderOverviewLayout(threads, upcoming, workingThreads); renderStatus(); state.dispatchDock?.flush(); return; }
  state.listSignature = signature;
  state.overviewMotion ||= createOverviewMotion();
  const motionBefore = state.overviewMotion.capture(JSON.stringify([viewId, groupBy, query,
    $("view").value, $("phase-filter").value, $("repository").value, $("project").value, state.selected]));
  const focused = document.activeElement?.dataset.thread;
  const focusAction = document.activeElement?.dataset.action;
  const focusGroup = document.activeElement?.closest("[data-group]")?.dataset.group;
  const focusArea = document.activeElement?.closest("#sessions") ? $("sessions") : $("overview");
  // Rebuilding the board can clamp the scrollers while their content is empty.
  // Capture the current position on every render, including background updates.
  const scrollPositions = [$("welcome"), $("sessions")].map(el => ({el, top: el.scrollTop, left: el.scrollLeft}));
  $("sessions").replaceChildren(); $("overview").replaceChildren();
  const agentTree = agentThreadTree(threads);
  const groups = threadGrouping.groupThreads(agentTree.roots);
  const filtered = Boolean(query || $("phase-filter").value || ["attention", "completed"].includes($("view").value) || $("repository").value || $("project").value);
  function addGroup(key, label, items, parent, description = "", sidebarItems = items) {
    const section = node("section", null, "thread-group " + key), overview = node("section", null, "thread-group " + key);
    section.setAttribute("aria-label", label); overview.setAttribute("aria-label", label);
    overview.dataset.group = key;
    overview.dataset.empty = String(items.length === 0);
    const sidebarHeading = groupHeading(label), overviewHeading = groupHeading(label);
    if (key === "priority") { sidebarHeading.className += " sr-only"; overviewHeading.className += " sr-only"; }
    section.append(sidebarHeading);
    overview.append(overviewHeading);
    if (description) overview.append(node("p", description, "group-description"));
    const cards = node("div", null, "thread-cards");
    for (const thread of sidebarItems) section.append(agentThreadCard(thread, agentTree, true));
    for (const thread of items) {
      const card = agentThreadCard(thread, agentTree, false, key === "priority" || groupBy === "domain" && threadGrouping.awaitsUser(thread));
      card.dataset.cardThread = thread.thread_id;
      cards.append(card);
    }
    if (!items.length) cards.append(node("p", "—", "stage-empty"));
    overview.append(cards);
    if (sidebarItems.length) $("sessions").append(section);
    parent.append(overview);
  }
  state.schedules?.renderUpcoming($("overview"), upcoming);
  if (groupBy === "domain") {
    const pinned = agentTree.roots.filter(thread => thread.pinned && thread.attention === "now");
    const pinnedIds = new Set(pinned.map(thread => thread.thread_id));
    if (pinned.length) addGroup("pinned", "Pinned", pinned, $("overview"), "", []);
    const board = node("div", null, "organization-board");
    board.setAttribute("aria-label", "Work domains");
    for (const group of threadOrganization.groupThreads(agentTree.roots, "domain")) {
      // Urgent responses lead their domain, preserving their attention styling.
      const ordered = [...group.threads].sort((a, b) => Number(threadGrouping.awaitsUser(b)) - Number(threadGrouping.awaitsUser(a)) ||
        (threadGrouping.awaitsUser(a) ? threadGrouping.attentionOrder(a, b) : 0));
      const unpinned = ordered.filter(thread => !pinnedIds.has(thread.thread_id));
      addGroup(group.key, group.label, unpinned, board, "", ordered);
      if (!unpinned.length) board.lastElementChild?.remove();
    }
    $("overview").append(board);
  } else if (archived) {
    if (groups.closed.length) addGroup("closed", "Closed", groups.closed, $("overview"), "Conversations removed from your workspace. Reopen one whenever you need it.");
  } else if ($("view").value === "later") {
    if (groups.later.length) addGroup("later", "Later", groups.later, $("overview"), "Set aside for another time. Pick up from the last checkpoint when you’re ready.");
  } else {
    const pinned = threads.filter(thread => thread.pinned && thread.attention === "now");
    const pinnedIds = new Set(pinned.map(thread => thread.thread_id));
    const attentionThreads = groups.priority.filter(thread => !pinnedIds.has(thread.thread_id));
    if (pinned.length) addGroup("pinned", "Pinned", pinned, $("overview"), "", []);
    if (groups.priority.length) {
      const attention = node("section", null, "attention-panel");
      attention.setAttribute("aria-label", "Attention queue");
      // Pinned owns the overview card; all attention items remain in navigation.
      addGroup("priority", "Attention", attentionThreads, attention, "", groups.priority);
      if (attentionThreads.length) $("overview").append(attention);
    }
    const board = node("div", null, groupBy === "phase" ? "phase-board" : "organization-board");
    board.setAttribute("aria-label", "Work stages");
    const organized = groupBy === "phase" ? groups.phases : threadOrganization.groupThreads(groups.phases.flatMap(group => group.threads), groupBy);
    for (const group of organized) {
      const unpinned = group.threads.filter(thread => !pinnedIds.has(thread.thread_id));
      addGroup(group.key, group.label, unpinned, board, group.detail || "", group.threads);
    }
    $("overview").append(board);
    if (groupBy === "phase") {
      // Keep each view's place through polling and updates while a thread is open.
      board.onscroll = () => {
        if (board.isConnected && !$("welcome").hidden && board.clientWidth) state.phaseScroll.set(viewId, board.scrollLeft);
      };
      board.scrollLeft = state.phaseScroll.get(viewId) || 0;
    }
  }
  if (!threads.length && !upcoming.length) {
    $("sessions").append(node("p", filtered ? "No matching threads" : "Nothing here yet", "empty"));
    const empty = node("div", null, "empty-state");
    const emptyProject = !query && !$("phase-filter").value && !$("repository").value && $("view").value === "active" && !projectThreads.length
      && state.organization.projects.find(project => project.id === $("project").value);
    const title = emptyProject ? "No open threads in " + emptyProject.name : filtered ? "No matching threads" : archived ? "No closed threads yet" : $("view").value === "later" ? "Nothing set aside" : "Start with an idea";
    const copy = emptyProject ? "Start a new session here, or browse existing threads and choose Add to group." : filtered ? "Try another search or clear your filters." : archived ? "Closed threads stay here. Reopen one whenever you want to continue." : $("view").value === "later" ? "Move a thread to Later to keep its context out of your current workspace." : "Open a session and give your next idea a place to begin.";
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
  for (const {el, top, left} of scrollPositions) { el.scrollTop = top; el.scrollLeft = left; }
  state.overviewMotion.play(motionBefore);
  state.dispatchDock?.flush();
  if (focused) {
    const candidates = [...focusArea.querySelectorAll("[data-thread]")].filter(el => el.dataset.thread === focused && el.dataset.action === focusAction);
    const target = candidates.find(el => el.closest("[data-group]")?.dataset.group === focusGroup) || candidates[0];
    target?.focus({preventScroll: true});
  }
  renderStatus();
  if (state.overviewLayout) renderOverviewLayout(threads, upcoming, workingThreads);
  globalThis.threadHover?.refresh();
}
function renderOverviewLayout(threads, upcoming = [], workingThreads = threads) {
  const host = $("atlas-overview");
  if (!host) return;
  const atlas = state.overviewLayout?.mode === "atlas";
  const workingSet = state.overviewLayout?.mode === "working-set", workingHost = $("working-set-overview");
  // Scheduled prompts remain available above either overview representation.
  $("overview").hidden = (atlas || workingSet) && !upcoming.length;
  $("overview").classList.toggle("atlas-schedules-only", atlas || workingSet);
  host.hidden = !atlas;
  if (workingHost) workingHost.hidden = !workingSet;
  if (workingSet && workingHost && !$("welcome").hidden) {
    state.workingSet ||= WorkingSet.create({document, window, storage: state.overviewPreferences,
      isActive: () => state.overviewLayout?.mode === "working-set" && !$("welcome").hidden,
      renderCard: (thread, needsReply) => threadCard({...thread, attention_target_thread_id: null, can_handle_response: false}, false, needsReply),
      onOpen: id => action(() => select(id)),
      onGroupChange: id => action(() => savedViews.openGroup(id)),
      onDismiss: thread => action(async () => {
        await api(threadPath(thread.thread_id), {handled: true, handled_checkpoint_id: thread.checkpoint?.id || 0,
          handled_completion_id: thread.turn_completion?.id || 0});
        await refreshList();
      }),
      onRead: thread => action(async () => {
        await api(threadPath(thread.thread_id), {seen: true, seen_checkpoint_id: thread.checkpoint?.id || 0,
          seen_completion_id: thread.turn_completion?.id || 0});
        await refreshList();
      })});
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
    return inProject(context, state.folders?.selected?.cwd) && threadViews.matches(
      {...context, attention: "later", title: [thread?.title, item.message, item.cwd].filter(Boolean).join(" ")},
      {show: "later", phase: $("phase-filter").value, project: $("project").value,
        repository: $("repository").value, search: query});
  }}) || [];
}
function agentThreadTree(threads) {
  // Filter first: a hidden, closed, or differently placed creator must never
  // swallow a matching child. Keep the original records and counts untouched.
  const visible = new Map(threads.map(thread => [thread.thread_id, thread]));
  const parents = new Map(), children = new Map();
  for (const thread of threads) {
    // Group explicit orchestration creations only; provenance alone also
    // includes dispatch and older creations whose intent was never recorded.
    if (thread.creation_mode !== "orchestration") continue;
    const parent = visible.get(thread.created_by_thread_id);
    if (!parent || parent.attention !== thread.attention) continue;
    const seen = new Set([thread.thread_id]);
    let ancestor = parent;
    while (ancestor && !seen.has(ancestor.thread_id)) {
      seen.add(ancestor.thread_id);
      ancestor = ancestor.creation_mode === "orchestration" ? visible.get(ancestor.created_by_thread_id) : null;
    }
    if (ancestor) continue; // Defensive against malformed historical cycles.
    parents.set(thread.thread_id, parent.thread_id);
    if (!children.has(parent.thread_id)) children.set(parent.thread_id, []);
    children.get(parent.thread_id).push(thread);
  }
  for (const items of children.values()) items.sort((a, b) =>
    (a.created_at || 0) - (b.created_at || 0) || a.thread_id.localeCompare(b.thread_id));
  const requiresInput = thread => threadGrouping.awaitsUser(thread) && (
    ["input", "action", "failed"].includes(thread.response_state) || thread.snooze_due ||
    ["blocked", "review"].includes(thread.attention_reason) ||
    (!thread.checkpoint_stale && thread.checkpoint?.next_actor === "user" && thread.checkpoint.next_action));
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
  const needsInput = children.filter(tree.requiresInput).length;
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
    const item = node("li"), status = threadGrouping.status(child);
    const button = node("button", null, "agent-row" + (status.key === "running" ? " working" : "") + (tree.requiresInput(child) ? " needs-attention" : "") + (child.unread_result ? " response-unread" : ""));
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
  // Retain only a few recently viewed transcripts and their rendered nodes.
  while (cache.size > 4) cache.delete([...cache.keys()].find(key => key !== state.selected));
  return entry;
}
function retainConversation() {
  if (!state.selected || !state.detail || isSessionDraft()) return;
  const entry = conversationEntry(state.selected);
  entry.detail = state.detail;
  // A streamed update may be waiting for its paint. Do not retain older DOM
  // beside the newer model and later mistake it for an up-to-date transcript.
  entry.nodes = state.timelineRender?.id === state.selected ? null : [...$("timeline").childNodes];
  entry.scroll = state.timelineScroll?.position() ?? $("timeline").scrollTop;
  entry.following = state.timelineScroll?.following ?? true;
  // Keep lightweight reading positions even after the bounded transcript cache evicts a tab.
  (state.conversationPositions ||= new Map()).set(state.selected, {scroll: entry.scroll, following: entry.following});
}
function sameConversation(first, second) {
  if (first && JSON.stringify([first.work_thread.forked_from, first.work_thread.forked_from_turn_id, first.work_thread.forked_sessions]) !==
    JSON.stringify([second.work_thread.forked_from, second.work_thread.forked_from_turn_id, second.work_thread.forked_sessions])) return false;
  return !!first && JSON.stringify([first.thread.historyUnavailable, first.thread.turns, first.coordinationMessages, first.dispatchRequests, first.work_thread.browser_session, first.work_thread.client]) ===
    JSON.stringify([second.thread.historyUnavailable, second.thread.turns, second.coordinationMessages, second.dispatchRequests, second.work_thread.browser_session, second.work_thread.client]);
}
async function select(id, {rollUp = false, dock = false, focus = true, stayInProject = false, refreshInventory = true, tabId = null} = {}) {
  const previousTab = state.threadTabs?.selected;
  state.projectScratchpad?.hideTab();
  state.navigationRevision = (state.navigationRevision || 0) + 1;
  state.mobileChat?.close();
  if (id === "new-session") return newSession();
  if ((!tabId || tabId === id) && !state.paneMode && !state.conversationWindow) {
    const known = state.sessions?.find(thread => thread.thread_id === id) || state.conversations?.get(id)?.detail?.work_thread;
    const related = known?.orchestration_workspaces?.includes(state.projectCwd);
    if (stayInProject && !related && (known?.project_cwd || known?.cwd) && (known.project_cwd || known.cwd) !== state.projectCwd) return;
    const cwd = known?.project_cwd || known?.cwd;
    if (!related && cwd && cwd !== state.folders?.selected?.cwd) await openProjectDirectory(cwd, {preserveRollUp: rollUp});
  }
  $("conversation").classList.remove("session-draft");
  if (!rollUp) state.rollUp?.stop();
  if (dock && globalThis.agentCoordPanes?.active) globalThis.agentCoordPanes.leave();
  if (dock) state.dispatchWorkspace?.hide();
  if (globalThis.agentCoordPanes?.active) { await globalThis.agentCoordPanes.openThread(id); return; }
  if (state.paneMode && state.selected && state.selected !== id) { await window.parent.agentCoordPanes.openThread(id); return; }
  savedViews?.remember();
  setNavigation(false);
  if (state.selected) state.drafts.set(state.selected, $("message").value);
  retainConversation();
  state.selected = id;
  state.scratchpad?.select(id);
  if (dock) state.threadTabs?.suspend();
  else if (!tabId || !state.threadTabs?.selectConversation?.(tabId, id))
    state.threadTabs?.open(state.sessions?.find(thread => thread.thread_id === id)
      || state.conversations?.get(id)?.detail?.work_thread || {thread_id: id, title: "Conversation"});
  state.dispatchDock?.hide();
  if (dock) {
    setChatExpanded(false);
    state.dispatchDock.show(id, state.folders.selected.cwd);
    state.dispatchDock.remember(state.folders.selected.cwd, {id, hidden: false});
    state.dispatchWorkspace?.show(id, state.folders.selected.cwd);
  }
  $("message").value = state.drafts.get(id) || "";
  const cached = conversationEntry(id);
  notifications?.syncFocus();
  state.detail = cached.detail;
  state.detailRefreshing = !!cached.detail;
  state.titleEdit = null;
  state.attachments?.highlight(false);
  renderStatus();
  if (navigation) navigation.remember();
  else history.replaceState(null, "", "#" + encodeURIComponent(id));
  $("welcome").hidden = true;
  $("conversation").hidden = false;
  state.agentTreeController?.bind({preservePanel: !!tabId && tabId === previousTab, dock});
  if (typeof layoutContext === "function") layoutContext();
  $("requests").replaceChildren();
  $("thread-context").hidden = true;
  renderTitle();
  $("page-location").textContent = dock ? "Dispatcher" : "Thread";
  delete $("requests").dataset.signature;
  if (cached.detail) {
    renderThread();
    if (cached.nodes) {
      if (state.timelineThread !== id) {
        $("timeline").replaceChildren(...cached.nodes);
        state.timelineScroll?.afterRender(cached.following);
      }
      state.timelineThread = id;
    } else renderTimeline();
    if (state.timelineScroll) {
      if (cached.following) state.timelineScroll.latest();
      else state.timelineScroll.restore(cached.scroll);
    } else $("timeline").scrollTop = cached.scroll;
    renderQueuedMessages(); renderStatus();
  } else {
    $("timeline").replaceChildren(node("p", "Opening session…", "empty"));
    state.timelineThread = null;
    state.timelineScroll?.reset();
  }
  const restorePosition = !cached.detail && state.conversationPositions?.get(id);
  renderList();
  const fresh = await refreshDetail({markSeen: false});
  if (state.selected !== id) return;
  if (restorePosition && !restorePosition.following) {
    if (state.timelineScroll) state.timelineScroll.restore(restorePosition.scroll);
    else $("timeline").scrollTop = restorePosition.scroll;
  }
  // Streamed updates can invalidate the snapshot without changing the provider.
  // Load its controls even when another detail refresh is already scheduled.
  if (state.detail?.work_thread.browser_session) loadSessionModels();
  if (fresh === false) return;
  if (state.paneMode) return;
  const displayed = state.detail.work_thread;
  if ((!tabId || tabId === id) && !state.conversationWindow && !displayed.orchestration_workspaces?.includes(state.projectCwd)
      && displayed.project_cwd && state.folders && displayed.project_cwd !== state.folders.selected?.cwd) {
    if (stayInProject) { goHome({restoreDispatcher: false}); return; }
    await openProjectDirectory(displayed.project_cwd, {preserveRollUp: rollUp});
    return select(id, {rollUp});
  }
  if (!dock) await api(threadPath(id), {seen: true, seen_checkpoint_id: displayed.checkpoint?.id || 0,
    seen_completion_id: displayed.turn_completion?.id || 0});
  if (refreshInventory) await refreshList();
  if (state.selected !== id) return;
  if (state.detail?.work_thread.browser_session) {
    if (focus) (state.mobileChat?.matches && !dock ? $("timeline") : $("message")).focus({preventScroll: true});
  } else $("session-name").focus({preventScroll: true});
}
async function refreshDetail({markSeen = true} = {}) {
  state.schedules?.refresh({background: true}).catch(showError);
  const id = state.selected;
  if (!id || isSessionDraft()) return;
  const entry = conversationEntry(id);
  if (entry.pending) return entry.pending;
  entry.pending = (async () => {
    const revision = entry.revision;
    const attentionRevision = state.attentionMutations?.get(id)?.revision || 0;
    const work = await api(threadPath(id));
    const detail = work.browser_session ? await api(sessionPath(id)) : {session: {cwd: work.cwd}, thread: {turns: []}, requests: [], running: false,
      coordinationMessages: (await api(threadPath(id) + "/messages")).data};
    detail.work_thread ||= work;
    detail.close_when_done = work.close_when_done;
    detail.work_thread = reconcileAttentionSnapshot(detail.work_thread, attentionRevision);
    const changed = revision !== entry.revision;
    // A background snapshot must not erase newer streamed messages or approvals.
    if (changed && entry.detail) {
      if (id === state.selected) scheduleDetail();
      return false;
    }
    const same = sameConversation(entry.detail, detail);
    entry.detail = detail;
    if (!same) entry.nodes = null;
    if (id !== state.selected || state.conversations.get(id) !== entry) return true;
    state.detail = detail;
    state.detailRefreshing = changed;
    if (state.dispatchDock?.active === id && (!detail.work_thread.dispatcher || detail.work_thread.attention === "archived")) {
      for (const key of ["sessions", "viewThreads"]) if (state[key]) state[key] = state[key].map(t => t.thread_id === id ? detail.work_thread : t);
      goHome(); return true;
    }
    renderThread();
    if (!same || state.timelineThread !== id) renderTimeline();
    renderRequests(); renderQueuedMessages(); renderStatus();
    // Opening a tab sends its own receipt. Also acknowledge replies rendered
    // while the user stays in the conversation, using only this snapshot's IDs.
    if (markSeen && !state.paneMode && detail.work_thread.unread_result && !document.hidden && document.hasFocus() &&
        !$("conversation").hidden && (!state.dispatchDock?.active || state.dispatchDock.expanded)) {
      await api(threadPath(id), {seen: true, seen_checkpoint_id: detail.work_thread.checkpoint?.id || 0,
        seen_completion_id: detail.work_thread.turn_completion?.id || 0});
      await refreshList();
    }
    if (revision !== entry.revision) scheduleDetail();
    return !changed;
  })().finally(() => { entry.pending = null; });
  return entry.pending;
}
async function refreshThread() {
  if (state.paneMode) state.schedules?.refresh({background: true}).catch(showError);
  const id = state.selected;
  if (!id || !state.detail || isSessionDraft()) return;
  const revision = state.attentionMutations?.get(id)?.revision || 0;
  const thread = await api(threadPath(id));
  if (id !== state.selected || !state.detail) return;
  const previous = state.detail.work_thread;
  if (!state.closeWhenDoneSaving?.has(id)) state.detail.close_when_done = thread.close_when_done;
  const forksChanged = JSON.stringify([previous.forked_from, previous.forked_from_turn_id, previous.forked_sessions]) !==
    JSON.stringify([thread.forked_from, thread.forked_from_turn_id, thread.forked_sessions]);
  state.detail.work_thread = reconcileAttentionSnapshot(thread, revision); renderThread(); renderStatus();
  if (forksChanged) { conversationEntry(id).nodes = null; renderTimeline(); }
}
function renderThread() {
  const detail = state.detail, work = detail.work_thread, checkpoint = work.checkpoint;
  state.threadTabs?.update([work]);
  state.agentTreeController?.update(work);
  $("thread-context").hidden = false;
  renderTitle();
  $("page-location").textContent = state.dispatchDock?.active ? "Dispatcher" : "Thread";
  $("park").textContent = work.attention === "later" ? "Move to Now" : "Move to Later";
  $("snooze-thread-button").hidden = work.attention === "archived";
  $("snooze-thread-button").textContent = work.attention === "later" ? "Change return time…" : "Move to Later…";
  $("park").hidden = work.attention !== "later";
  $("resume-snooze").hidden = !(work.snoozed || work.snooze_due);
  $("resume-snooze").textContent = work.snooze_due ? "Dismiss reminder" : "Move to Now";
  if (work.snoozed) $("park").hidden = true;
  $("snooze-status").textContent = snoozeLabel(work);
  for (const id of ["snooze-thread-button", "resume-snooze"]) $(id).disabled = state.updatingThreads.has(work.thread_id);
  $("handle-response").hidden = !work.can_handle_response;
  $("handle-response").disabled = state.updatingThreads.has(work.thread_id);
  $("organize-thread").textContent = work.project_id ? "Change group" : "Add to group";
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
  globalThis.agentCoordContextLifecycle?.render($("checkpoint"), work, {sameThread, opened});
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
  if (work.forked_from || work.forked_sessions?.length) $("checkpoint").append(forkLinks(work));
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
  const providerName = session?.client === "muse" ? "Muse Code" : session?.client === "kimi" ? "Kimi" : session?.client === "antigravity" ? "Antigravity" : session?.client === "claude" ? "Claude Code" : "Codex";
  $("session-model").textContent = "Model · " + providerName + " · " + (session?.model || "Default");
  $("session-effort").textContent = "Reasoning · " + (session?.effort || "Model default");
  $("session-yolo").hidden = !session?.yolo;
  $("edit-permissions").disabled = !browserSession || state.detailRefreshing || !!state.detail?.running || state.detail?.work_thread?.attention === "archived" || sessionBusy() || state.closing.has(state.selected);
  const feedback = state.selected === "new-session" ? state.newSessionDraft?.commandFeedback : state.commandFeedback.get(state.selected);
  $("command-feedback").textContent = feedback || "";
  $("command-feedback").hidden = !browserSession || !feedback;
  renderModelPicker();
}
function openSessionPermissions() {
  const detail = state.detail, work = detail?.work_thread;
  if (!work?.browser_session || state.detailRefreshing || detail.running || work.attention === "archived" || sessionBusy() || state.closing.has(state.selected)) return;
  const dialog = $("permissions-dialog");
  dialog.dataset.threadId = state.selected;
  $("permissions-thread").textContent = work.title || detail.session?.name || "This session";
  $("edit-yolo").checked = !!detail.session?.yolo;
  $("permissions-description").textContent = detail.session?.client === "claude"
    ? "With YOLO off, Claude Code uses its configured permissions and asks before restricted tool actions. Changes apply to the next turn."
    : detail.session?.client === "muse"
      ? "Muse workspace sandbox with approval for protected actions"
    : detail.session?.client === "kimi"
    ? "With YOLO off, Kimi uses manual tool approvals. Changes apply to the next turn."
    : detail.session?.client === "antigravity"
    ? "With YOLO off, Antigravity uses its native permissions and tool approval prompts. Changes apply to the next turn."
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
  if (draft) { Object.assign(draft.session, body); draft.commandFeedback = null; }
  if (id === state.selected && state.detail) { state.detail.session = session; renderStatus(); }
  dialog.close();
  await refreshList();
}
function renderStatus() {
  state.sidecars?.showThread(state.detail?.work_thread, $("thread-sidecars"));
  const sidecarButton = $("attach-sidecar");
  if (sidecarButton) sidecarButton.hidden = !state.detail?.work_thread?.browser_session || state.detail?.work_thread?.attention === "archived";
  state.schedules?.renderThread($("thread-schedules"), state.selected);
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
  $("dispatcher-permissions").hidden = !docked;
  $("dispatcher-permissions").disabled = $("edit-permissions").disabled;
  $("dispatcher-permissions").textContent = state.detail?.session?.yolo ? "Permissions · Full access" : "Permissions";
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
  $("message").placeholder = composerLabel + " or drop images…";
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
  $("composer").hidden = !work?.browser_session;
  $("send").disabled = !state.detail || state.detailRefreshing || !work?.browser_session || (!dispatcher && running && !state.detail.activeTurn) || archived || sessionBusy() || !!state.attachments?.pending() || state.closing.has(state.selected);
  $("send").textContent = dispatcher ? "Send ↑" : running ? (!["codex", "claude", "muse"].includes(work?.client) ? "Queue ↑" : "Steer ↑") : "Send ↑";
  $("queue").hidden = dispatcher || !running || !["codex", "claude", "muse"].includes(work?.client);
  $("queue").disabled = $("send").disabled;
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
  $("move-thread").disabled = !!threadMoveUnavailable(work) || state.detailRefreshing;
  $("move-thread").title = threadMoveUnavailable(work);
  $("composer-hint").textContent = archived ? "Reopen this thread to continue." : running ? (!["codex", "claude", "muse"].includes(work?.client) ? "Enter to queue after this turn · Shift + Enter for a new line" : "Enter to steer · Tab to queue after this turn · Shift + Enter for a new line") : "Enter to send · Shift + Enter for a new line · /cd · /model · /help";
  if (!archived && !running && work?.client === "codex") $("composer-hint").textContent += " · $ skills";
  if (dispatcher && !archived) $("composer-hint").textContent = "Requests route to conversations that answer you directly.";
  state.attachments?.render();
  state.slashCommands?.render();
  state.mentions?.render();
  state.resizeComposer?.();
  if (state.paneMode) globalThis.agentCoordPane?.updateStatus();
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
  if (!visible) { element.textContent = ""; return; }
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
  return JSON.stringify(item, null, 2);
}
function forkRelations(work) {
  const relations = [];
  if (work?.forked_from) relations.push({label: "Forked from", thread: work.forked_from, turnId: work.forked_from_turn_id});
  for (const thread of work?.forked_sessions || []) relations.push({label: "Forked session", thread, turnId: thread.forked_from_turn_id});
  return relations;
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
function timelineDisclosures() {
  const entry = conversationEntry(state.selected);
  if (!entry.disclosures) {
    try { entry.disclosures = new Set(JSON.parse(sessionStorage.getItem("timeline-disclosures:" + state.selected) || "[]")); }
    catch { entry.disclosures = new Set(); }
  }
  return entry.disclosures;
}
function rememberDisclosure(detail, open, thread = state.selected) {
  if (detail.open) open.add(detail.dataset.id); else open.delete(detail.dataset.id);
  try { sessionStorage.setItem("timeline-disclosures:" + thread, JSON.stringify([...open])); }
  catch { /* Expansion still survives updates when storage is unavailable. */ }
}
function renderTimeline() {
  if (state.timelineRender) { clearTimeout(state.timelineRender.timer); state.timelineRender = null; }
  const el = $("timeline");
  const atBottom = state.timelineScroll?.beforeRender() ?? (el.scrollHeight - el.scrollTop - el.clientHeight < 100);
  const open = timelineDisclosures();
  if (state.timelineThread === state.selected) {
    for (const detail of el.querySelectorAll("details[data-id]")) {
      if (detail.open) open.add(detail.dataset.id); else open.delete(detail.dataset.id);
    }
    try { sessionStorage.setItem("timeline-disclosures:" + state.selected, JSON.stringify([...open])); }
    catch { /* Expansion still survives updates when storage is unavailable. */ }
  }
  state.timelineThread = state.selected;
  const fragment = document.createDocumentFragment();
  if (state.detail?.thread.historyUnavailable) {
    const notice = node("p", "Conversation history couldn’t be fully loaded. Reload to try again.", "empty");
    notice.setAttribute("role", "status");
    fragment.append(notice);
  }
  const messages = state.detail?.coordinationMessages || [];
  const sourceTurns = state.detail?.thread.turns || [];
  const turns = globalThis.DispatchUI && (state.detail?.work_thread?.dispatcher || state.detail?.dispatchRequests?.length)
    ? DispatchUI.turns(sourceTurns, state.detail.dispatchRequests || []) : sourceTurns;
  const entries = messages.length ? AgentMessages.entries(turns, messages) : turns.map(turn => ({turn}));
  const pendingForks = new Set(forkRelations(state.detail?.work_thread));
  const appendForks = (turnId, remaining = false) => {
    for (const relation of pendingForks) if (remaining || (relation.turnId || null) === turnId) {
      fragment.append(forkMarker(relation)); pendingForks.delete(relation);
    }
  };
  appendForks(null);
  const workGroups = new Map();
  const conversation = conversationEntry(state.selected);
  const previousTools = conversation.toolNodes || new Map(), toolNodes = new Map();
  const disclosureListeners = conversation.disclosureListeners ||= new WeakSet();
  for (const entry of entries) {
    if (entry.message) {
      fragment.append(AgentMessages.card(document, entry.message, {open: openAgentMessage, expanded: open.has("agent-message:" + entry.message.id)}));
      continue;
    }
    const turn = entry.turn;
    for (const item of entry.item ? [entry.item] : entry.end ? [] : turn.items || []) {
      if (item.type === "dispatchReceipt") {
        fragment.append(DispatchUI.card(document, item.receipt, {open: id => select(id).catch(showError),
          dismiss: id => action(async () => { await api(sessionPath(state.selected) + "/dispatch", {id, action: "dismiss"}); await refreshDetail(); })}));
        continue;
      }
      if (messages.length && AgentMessages.isWakePrompt(item, turn, messages)) continue;
      const text = itemText(item);
      if (item.type === "userMessage" || item.type === "agentMessage") {
        const message = node("article", null, "message " + (item.type === "userMessage" ? "user" : "agent"));
        message.dataset.searchTurn = turn.id || "search-turn-" + turns.indexOf(turn);
        message.dataset.searchItem = item.id || "search-item-" + (turn.items || []).indexOf(item);
        message.tabIndex = -1;
        const body = node("div", null, item.type === "agentMessage" ? "text markdown" : "text");
        if (item.type === "agentMessage") body.innerHTML = messageMarkdown.render(text);
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
        message.setAttribute("aria-label", item.type === "userMessage" ? "You" : (state.detail?.work_thread?.client === "muse" ? "Muse Code" : state.detail?.work_thread?.client === "kimi" ? "Kimi" : state.detail?.work_thread?.client === "antigravity" ? "Antigravity" : state.detail?.work_thread?.client === "claude" ? "Claude" : "Codex"));
        message.append(body);
        fragment.append(message);
      } else {
        // Streaming changes one item at a time. Reuse unchanged tool rows,
        // including their disclosure state, instead of churning the full history.
        const key = JSON.stringify([turn.id ?? turns.indexOf(turn), item.id ?? (turn.items || []).indexOf(item)]);
        const signature = JSON.stringify(item), cached = previousTools.get(key);
        const detail = cached?.signature === signature ? cached.node : node("details", null, "tool");
        toolNodes.set(key, {signature, node: detail});
        detail.dataset.id = item.id; detail.dataset.status = item.status || ""; detail.open = open.has(item.id);
        if (detail !== cached?.node) {
          const labels = {fileChange: "File changes", reasoning: "Thinking", mcpToolCall: "Tool", plan: "Plan", webSearch: "Web search"};
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
            fragment.append(group);
          }
          group.append(detail);
        } else fragment.append(detail);
      }
    }
    if (!entry.item && turn.error) fragment.append(node("p", turn.error.message || "This turn failed.", "empty"));
    if (!entry.item && turn.status === "interrupted") fragment.append(node("p", "Turn stopped.", "empty"));
    if (!entry.item) appendForks(turn.id);
  }
  // Retain navigation even when the provider cannot return the boundary turn.
  appendForks(null, true);
  el.replaceChildren(fragment);
  conversation.toolNodes = toolNodes;
  const disclosureThread = state.selected;
  for (const detail of el.querySelectorAll("details[data-id]")) {
    if (!disclosureListeners.has(detail)) {
      detail.addEventListener("toggle", () => rememberDisclosure(detail, open, disclosureThread));
      disclosureListeners.add(detail);
    }
  }
  if (!el.childNodes.length) el.append(node("p", state.detail?.work_thread.browser_session ? "This is the beginning of your session. What would you like to work on?" : "This conversation is in a terminal session. Its checkpoint and related links are saved here." + (state.detail?.work_thread.client === "codex" ? " Close that session to continue it in the browser." : " Continue the conversation in Claude Code."), "empty"));
  if (state.timelineScroll) state.timelineScroll.afterRender(atBottom);
  else if (atBottom) el.scrollTop = el.scrollHeight;
  if (state.messageTarget && state.messageTarget.thread === state.selected) focusAgentMessage(state.messageTarget.id);
  if (state.searchTarget?.thread === state.selected) focusSearchMessage(state.searchTarget);
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
  if (state.detailRefreshing) { area.replaceChildren(); delete area.dataset.signature; return; }
  // Preserve partially entered answers while unrelated events stream.
  const signature = JSON.stringify((state.detail?.requests || []).map(r => r.key));
  if (area.dataset.signature === signature) return;
  area.dataset.signature = signature; area.replaceChildren();
  for (const request of state.detail?.requests || []) {
    const box = node("section", null, "request"), params = request.params;
    const actions = node("div", null, "actions");
    if (request.method === "item/tool/requestUserInput") {
      box.append(node("h3", (state.detail?.work_thread?.client === "muse" ? "Muse Code" : state.detail?.work_thread?.client === "kimi" ? "Kimi" : state.detail?.work_thread?.client === "antigravity" ? "Antigravity" : state.detail?.work_thread?.client === "claude" ? "Claude" : "Codex") + " has a question"));
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
          const options = node("datalist"); options.id = "options-" + request.key + "-" + fields.length;
          for (const option of question.options) options.append(node("option", option.label));
          input.setAttribute("list", options.id); label.append(options);
        }
        label.append(input); box.append(label); fields.push([question.id, input]);
      }
      actions.append(requestButton("Send answer", "accept", request, () => ({answers: Object.fromEntries(fields.map(([id, input]) => [id, {answers: input.multiple ? Array.from(input.selectedOptions, option => option.value) : [input.value]}]))})));
      if (params.allowCancel) actions.append(requestButton("Cancel", "cancel", request));
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
      box.append(node("h3", (state.detail?.work_thread?.client === "muse" ? "Muse Code" : state.detail?.work_thread?.client === "kimi" ? "Kimi" : state.detail?.work_thread?.client === "antigravity" ? "Antigravity" : state.detail?.work_thread?.client === "claude" ? "Claude" : "Codex") + " is requesting approval"));
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
function scheduleTimelineRender() {
  const pending = state.timelineRender;
  if (pending?.id === state.selected && pending.detail === state.detail) return;
  if (pending) clearTimeout(pending.timer);
  const render = {id: state.selected, detail: state.detail};
  state.timelineRender = render;
  // Bound both large SSE replay batches and individual provider chunks. A
  // timer also flushes hidden panes, where animation frames may be suspended.
  render.timer = setTimeout(() => {
    if (state.timelineRender !== render) return;
    state.timelineRender = null;
    if (state.selected !== render.id || state.detail !== render.detail) return;
    renderTimeline(); renderStatus();
  }, 50);
}
function applyEvent(event) {
  if (event.method === "thread/closedWhenDone") { threadClosedWhenDone(event.params.threadId).catch(showError); return; }
  const cached = state.conversations?.get(event.params.threadId);
  if (cached) cached.revision++;
  if (event.method === "thread/usage/updated") {
    if (cached?.detail?.work_thread) cached.detail.work_thread.usage = event.params.usage;
    if (state.detail?.work_thread?.thread_id === event.params.threadId) {
      state.detail.work_thread.usage = event.params.usage;
      renderStatus();
    }
    return;
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
  if (!state.detail || event.params.threadId !== state.selected) return;
  const {method, params} = event;
  if (method === "coordination/messages") { refreshAgentMessages().catch(showError); return; }
  if (event.requestKey) { state.detail.requests.push({key: event.requestKey, method, params}); renderRequests(); renderStatus(); if (state.detailRefreshing) scheduleDetail(); return; }
  if (method === "turn/started") {
    state.detail.running = true; state.detail.activeTurn = params.turn.id;
    const turns = state.detail.thread.turns ||= [];
    const turn = turns.find(t => t.id === params.turn.id);
    if (turn) Object.assign(turn, {...params.turn, items: turn.items});
    else turns.push({...params.turn, items: params.turn.items || []});
    renderTimeline();
  }
  if (method === "turn/completed") {
    state.detail.running = false;
    const turn = state.detail.thread.turns?.find(t => t.id === params.turn?.id);
    if (turn) Object.assign(turn, {...params.turn, items: params.turn.items?.length ? params.turn.items : turn.items});
    renderTimeline(); scheduleDetail();
  }
  if (method === "serverRequest/resolved" || method === "browser/requests") scheduleDetail();
  if (method === "browser/changed") scheduleDetail();
  if (!method.startsWith("item/") || !params.turnId) { renderStatus(); return; }
  const turns = state.detail.thread.turns ||= [];
  let turn = turns.find(t => t.id === params.turnId);
  if (!turn) { turn = {id: params.turnId, items: [], status: "inProgress", startedAt: params.turnStartedAt}; turns.push(turn); }
  if (turn.startedAt == null && params.turnStartedAt != null) turn.startedAt = params.turnStartedAt;
  if (params.item) {
    const index = turn.items.findIndex(i => i.id === params.item.id);
    if (index < 0) turn.items.push(params.item); else turn.items[index] = params.item;
  } else if (params.itemId && typeof params.delta === "string") {
    let item = turn.items.find(i => i.id === params.itemId);
    if (!item && method === "item/agentMessage/delta") { item = {id: params.itemId, type: "agentMessage", text: ""}; turn.items.push(item); }
    if (item && method === "item/agentMessage/delta") item.text = (item.text || "") + params.delta;
    if (item && method === "item/commandExecution/outputDelta") item.aggregatedOutput = (item.aggregatedOutput || "") + params.delta;
  }
  scheduleTimelineRender();
}
let listTimer, detailTimer;
function scheduleList() { clearTimeout(listTimer); listTimer = setTimeout(() => refreshList({background: true}).catch(showError), 150); }
function scheduleDetail() { clearTimeout(detailTimer); detailTimer = setTimeout(() => refreshDetail().catch(showError), 100); }
function connect() {
  const events = new EventSource(base + "events?completion_after=" + state.config.completionCursor);
  events.onopen = () => { for (const entry of state.conversations?.values() || []) entry.revision++; $("connection").replaceChildren(node("span", "Connected locally", "sr-only")); $("connection").title = "Connected locally"; $("connection").dataset.state = "connected"; scheduleList(); if (state.selected) scheduleDetail(); };
  events.onerror = () => { $("connection").textContent = "Reconnecting…"; $("connection").title = "Reconnecting…"; $("connection").dataset.state = "reconnecting"; };
  events.onmessage = message => {
    const batch = JSON.parse(message.data);
    globalThis.agentCoordSpeech?.receive(batch.completions || []);
    globalThis.agentCoordPanes?.receive(batch);
    // Completions apply to every thread, including when this tab is hidden.
    for (const notification of [...(batch.completions || []), ...(batch.approvals || [])]) {
      const entry = state.conversations?.get(notification.thread_id);
      if (entry) entry.revision++;
    }
    notifications.receive(batch.completions || []);
    notifications.receive(batch.approvals || []);
    notifications.receive(batch.snoozes || []);
    // A notification can arrive after the request event was missed. Reconcile
    // the pending controls as well as notifying, even without an item event.
    if (batch.completions?.length || batch.snoozes?.length || batch.approvals?.length) { scheduleList(); if (state.selected) scheduleDetail(); }
    if (batch.reset) { for (const entry of state.conversations?.values() || []) entry.revision++; scheduleDetail(); }
    for (const event of batch.events) {
      if (event.method === "bridge/disconnected") { for (const entry of state.conversations?.values() || []) entry.revision++; showError(new Error("Codex disconnected. Restart Ribbon Field to reopen saved sessions.")); if (state.detail?.work_thread?.client === "codex" && state.detail) { state.detail.running = false; state.detailRefreshing = true; renderRequests(); } renderStatus(); }
      else applyEvent(event);
      if (!["thread/usage/updated", "thread/tokenUsage/updated"].includes(event.method) && !event.method.endsWith("/delta") && !event.method.endsWith("/outputDelta")) scheduleList();
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
    await api(threadPath(id), values);
    savedViews?.showGroup(values.project_id, thread.project_cwd || thread.cwd);
    $("organization-dialog").close();
    if (state.selected === id) await refreshThread();
    await refreshList();
  }, $("save-organization"));
};
function isSessionDraft() { return state.selected === "new-session" && !!state.newSessionDraft; }
function composerDraftId(id = state.selected) {
  return id === "new-session" ? state.newSessionDraft?.draftId || id : id;
}
function sessionBusy() {
  return !!state.busy || !!state.transferLocks?.has(state.selected) || !!state.sending?.has(isSessionDraft() ? state.newSessionDraft : state.selected);
}
function modelValue(client, model) { return JSON.stringify([client, model || ""]); }
function sessionModelClients() {
  return isSessionDraft() && !state.newSessionDraft.createdId ?
    ["codex", "claude", "antigravity", "kimi", "muse"] : [state.detail?.session?.client || "codex"];
}
function selectDraftModel({client, model}) {
  if (!isSessionDraft() || state.newSessionDraft.createdId || state.busy || state.detailRefreshing) return;
  Object.assign(state.newSessionDraft.session, {client, model: model || null, effort: null});
  state.newSessionDraft.work_thread.client = client;
  renderStatus();
}
function renderModelPicker() {
  const session = state.detail?.session, picker = $("model-picker");
  if (!session) { picker.disabled = true; globalThis.modelPicker?.sync(); renderEffortPicker(); return; }
  const draft = isSessionDraft(), client = session.client || "codex";
  const clients = sessionModelClients();
  const catalogs = state.modelCatalogs ||= new Map();
  const signature = JSON.stringify([clients, session.model, [...catalogs]]);
  if (picker.dataset.signature !== signature) {
    picker.dataset.signature = signature;
    picker.replaceChildren();
    for (const provider of clients) {
      const group = node("optgroup"); group.label = provider === "muse" ? "Muse Code" : provider === "kimi" ? "Kimi Code" : provider === "antigravity" ? "Google Antigravity" : provider === "claude" ? "Claude Code" : "Codex";
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
  picker.disabled = sessionBusy() || state.detailRefreshing || !!state.detail.running || state.detail.work_thread?.attention === "archived" || !!state.newSessionDraft?.createdId && draft;
  const errors = clients.filter(provider => state.modelErrors?.has(provider));
  $("model-picker-status").textContent = errors.length ? errors.map(provider => (provider === "muse" ? "Muse Code" : provider === "kimi" ? "Kimi" : provider === "antigravity" ? "Antigravity" : provider === "claude" ? "Claude Code" : "Codex") + " models unavailable").join(" · ") : "";
  if (["antigravity", "kimi", "muse"].includes(client) && state.modelErrors?.has(client)) $("model-picker-status").textContent = state.modelErrors.get(client);
  globalThis.modelPicker?.sync();
  renderEffortPicker();
}
function renderEffortPicker() {
  const session = state.detail?.session, picker = $("effort-picker");
  const models = state.modelCatalogs?.get(session?.client || "codex") || [];
  const model = models.find(item => item.model === session?.model);
  const efforts = model?.supportedReasoningEfforts || [];
  const signature = JSON.stringify([session?.effort, efforts]);
  if (picker.dataset.signature !== signature) {
    picker.dataset.signature = signature;
    picker.replaceChildren(new Option("Model default", ""));
    for (const effort of efforts) picker.append(new Option(effort.reasoningEffort, effort.reasoningEffort));
    if (session?.effort && !efforts.some(item => item.reasoningEffort === session.effort)) {
      picker.append(new Option(session.effort, session.effort));
    }
  }
  picker.value = session?.effort || "";
  picker.disabled = $("model-picker").disabled || !efforts.length;
  $("effort-trigger").title = !session?.model ? "Choose a model to set reasoning effort" : "Reasoning effort for the next message";
  globalThis.effortPicker?.sync();
}
async function changeSessionEffort() {
  const id = state.selected, draft = isSessionDraft(), effort = $("effort-picker").value || null;
  if ($("effort-picker").disabled || sessionBusy() || state.detail?.running || draft && state.newSessionDraft.createdId) return;
  state.busy = true;
  renderStatus();
  try {
    if (draft) state.newSessionDraft.session.effort = effort;
    else {
      const session = await api(sessionPath(id), {effort});
      if (state.selected === id) state.detail.session = session;
    }
  } finally { state.busy = false; renderStatus(); }
}
$("effort-picker").onchange = () => action(changeSessionEffort);
$("effort-picker").onfocus = () => { loadSessionModels(); };
async function loadModelCatalog(client) {
  const catalogs = state.modelCatalogs ||= new Map(), loading = state.modelLoading ||= new Map();
  const errors = state.modelErrors ||= new Map();
  if (catalogs.has(client)) return catalogs.get(client);
  if (!loading.has(client)) {
    loading.set(client, api("models?client=" + encodeURIComponent(client)).then(result => {
      catalogs.set(client, result.data); errors.delete(client);
      return result.data;
    }).catch(error => { errors.set(client, error.message); throw error; })
      .finally(() => { loading.delete(client); renderModelPicker(); }));
  }
  return loading.get(client);
}
async function loadSessionModels() {
  const clients = sessionModelClients();
  // Share completed and in-flight requests with slash autocomplete.
  await Promise.all(clients.map(client => loadModelCatalog(client).catch(() => {})));
}
async function changeSessionModel() {
  const id = state.selected, [client, model] = JSON.parse($("model-picker").value);
  const draft = isSessionDraft();
  if (sessionBusy() || state.detailRefreshing || state.detail?.running || draft && state.newSessionDraft.createdId) return;
  if (draft) { selectDraftModel({client, model}); return; }
  state.busy = true;
  try {
    const session = await api(sessionPath(id), {model});
    if (state.selected === id) state.detail.session = session;
  } finally { state.busy = false; renderStatus(); }
}
$("model-picker").onchange = () => action(changeSessionModel);
$("model-picker").onfocus = () => { loadSessionModels(); };
async function newSession() {
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
  state.newSessionDraft ||= {
    draftId: "new-session:" + (state.draftSequence = (state.draftSequence || 0) + 1),
    session: {...body, client: "codex", model: null, effort: null, yolo: false},
    work_thread: {thread_id: "new-session", title: "New session", ...body, project_cwd: body.cwd, client: "codex", browser_session: true, attention: "now", links: []},
    thread: {turns: []}, requests: [], queuedMessages: [], running: false,
  };
  state.selected = "new-session";
  state.scratchpad?.select(null);
  state.detail = state.newSessionDraft;
  state.threadTabs?.open(state.newSessionDraft.work_thread);
  state.detailRefreshing = false;
  state.titleEdit = null;
  $("message").value = state.drafts.get("new-session") || "";
  $("welcome").hidden = true;
  $("conversation").hidden = false;
  $("conversation").classList.add("session-draft");
  state.agentTreeController?.bind();
  if (typeof layoutContext === "function") layoutContext();
  $("page-location").textContent = "New session";
  state.timelineThread = null;
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
async function sendSessionDraft(text, images, mentions = []) {
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
    if (command === "/yolo" || parts.length === 2)
      draft.session.yolo = command === "/yolo" || parts[1].toLowerCase() === "yolo";
    draft.commandFeedback = draft.session.yolo
      ? "Permissions: yolo — full machine access without approval prompts. Use /permissions default to turn it off."
      : "Permissions: default — approval prompts enabled. Use /yolo to enable full machine access.";
    input.value = ""; state.drafts.delete("new-session");
    state.mentions?.sent(draft.draftId || "new-session", text, mentions);
    renderStatus();
    return;
  }
  const cwd = state.projectCwd || draft.session.cwd, draftId = draft.draftId || "new-session";
  if (!draft.createdId) {
    const result = await api("sessions", {...draft.session});
    draft.createdId = result.session.thread_id;
    if (draft.session.project_id) savedViews?.showGroup(draft.session.project_id, draft.session.cwd);
  }
  const body = {message: text, windowId: state.sourceWindowId};
  if (mentions.length) body.mentions = mentions;
  if (images.length) body.images = images.map(({name, url}) => ({name, url}));
  const result = await api(sessionPath(draft.createdId) + "/messages", body);
  state.attachments?.sent(draftId, images);
  state.mentions?.sent(draftId, text, mentions);
  state.mentions?.move(draftId, draft.createdId);
  state.attachments?.move(draftId, draft.createdId);
  if (result.command) state.commandFeedback.set(draft.createdId, result.command.message);
  // Preserve edits typed while creation or sending was in flight.
  const current = state.newSessionDraft === draft;
  const saved = state.projectDrafts?.get(cwd);
  const remaining = current ? (isSessionDraft() ? $("message").value : state.drafts.get("new-session"))
    : saved?.detail === draft ? saved.text : "";
  if (remaining && remaining !== text) state.drafts.set(draft.createdId, remaining);
  if (saved?.detail === draft) state.projectDrafts.delete(cwd);
  if (current) {
    state.drafts.delete("new-session");
    state.newSessionDraft = null;
  }
  state.threadTabs?.replace("new-session", {thread_id: draft.createdId, title: "Conversation"}, {cwd});
  const destination = state.projectDestinations?.get(cwd);
  if (destination?.tab === "new-session") destination.tab = draft.createdId;
  if (current && state.selected === "new-session") {
    $("message").value = "";
    restoreFocus = !!result.command && hadComposerFocus && state.navigationRevision === navigationRevision;
    const selecting = select(draft.createdId, {focus: !result.command});
    focusRevision = state.navigationRevision;
    await selecting;
  }
  await refreshList();
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
  const button = $("expand-chat"), label = expanded ? "Collapse chat" : "Expand chat";
  button.setAttribute("aria-pressed", String(expanded));
  button.setAttribute("aria-label", label);
  button.title = expanded ? label + " (Esc)" : label;
}
$("expand-chat").onclick = () => setChatExpanded(!document.body.classList.contains("chat-expanded"));
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
  state.dispatchDock?.hide();
  state.selected = null; state.detail = null; state.titleEdit = null;
  state.scratchpad?.select(null);
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
  if (!$("settings-frame").getAttribute("src")) $("settings-frame").src = "/settings?embedded=1";
  $("settings-dialog").showModal();
};
window.addEventListener("message", event => {
  if (event.origin === location.origin && event.source === $("settings-frame").contentWindow && event.data?.type === "ribbon-field-close-settings") $("settings-dialog").close();
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
  let openThreads = [], closedThreads = [], familyThreads = [], pending, defaultPending = false;
  const currentTab = () => state.threadTabs?.items.find(item => item.thread_id === state.threadTabs.selected);
  const controller = state.agentTreeController = {
    mode: "context", root: null, tree: AgentTree.graph([]),
    hasAgents(root = this.root) {
      if (!root || !this.tree.nodes.has(root)) return false;
      return this.tree.family(root).some(id => id !== root && this.tree.nodes.get(id)?.browser_session !== false);
    },
    inventory(open, closed) {
      openThreads = open;
      if (closed) closedThreads = closed;
      this.update();
      if (this.mode === "agents" && state.selected && !isSessionDraft()) this.load().catch(showError);
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
      if (!state.selected || isSessionDraft() || state.selected === "new-session") {
        this.root = null;
        $("panel-agents").hidden = true;
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
      $("conversation").classList.toggle("has-agent-tree", hasAgents);
      if (!hasAgents && this.mode === "agents") {
        if ($("panel-agents") === document.activeElement) $("panel-context").focus();
        this.setMode("context", false);
      }
      this.applyDefault();
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
      defaultPending = !preservePanel && !dock && !state.paneMode && !draft;
      if (!preservePanel || draft) {
        if (typeof readSavedContextCollapsed === "function") contextCollapsed = readSavedContextCollapsed();
        this.setMode("context", false);
      } else layoutContext();
      this.update();
      // Discover closed ancestors and children even when overview filters hide them.
      if (!dock && !state.paneMode && !draft) this.load().catch(showError);
    },
    applyDefault() {
      if (!defaultPending || !state.selected || isSessionDraft() || !this.hasAgents(this.root)) return;
      defaultPending = false;
      this.setMode("agents");
      setContextCollapsed(false, {persist: false});
      if (state.mobileChat?.matches) state.mobileChat.open("context");
      renderStatus();
    },
    cancelDefault() { defaultPending = false; },
    setMode(mode, remember = true) {
      if (mode === "agents" && !this.hasAgents(this.root)) mode = "context";
      if (remember) this.cancelDefault();
      this.mode = mode;
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
    controller.cancelDefault();
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
  const context = $("thread-context"), panel = $("context-panel"), button = $("toggle-context");
  const draft = isSessionDraft() || state.selected === "new-session" || $("conversation").classList.contains("session-draft");
  const collapsed = draft || (contextCollapsed ?? compactContext.matches);
  const containsFocus = panel.contains(document.activeElement);
  panel.hidden = collapsed;
  context.open = !collapsed;
  $("conversation").classList.toggle("context-collapsed", collapsed);
  $("conversation").classList.toggle("agents-visible", !collapsed && state.agentTreeController?.mode === "agents");
  const label = (collapsed ? "Show " : "Hide ") + (state.agentTreeController?.mode === "agents" ? "agent tree" : "thread context");
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", label);
  button.title = label;
  if (collapsed && containsFocus) button.focus({preventScroll: true});
}
function setContextCollapsed(collapsed, {persist = true} = {}) {
  state.agentTreeController?.cancelDefault();
  contextCollapsed = collapsed;
  if (persist) {
    try { localStorage.setItem("agent-coord.context-collapsed", String(collapsed)); } catch { /* Preferences are optional. */ }
  }
  layoutContext();
}
$("toggle-context").onclick = () => setContextCollapsed(!$("context-panel").hidden);
$("thread-context").addEventListener("toggle", () => {
  if (state.mobileChat?.matches || state.agentTreeController?.mode === "agents") return;
  // Clicking the panel heading also collapses the whole column.
  const collapsed = !$("thread-context").open;
  if (collapsed !== $("context-panel").hidden) setContextCollapsed(collapsed);
});
compactContext.addEventListener("change", layoutContext);
layoutContext();
if (typeof AgentTree !== "undefined") setupAgentTree();
state.scratchpad = new ThreadScratchpad({panel: $("thread-scratchpad"), input: $("scratchpad-text"), status: $("scratchpad-status"), api});
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
async function sendMessage(mode = "steer") {
  if (state.detail?.running && ["antigravity", "kimi"].includes(state.detail?.work_thread?.client)) mode = "queue";
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
  if (state.busy || state.sending?.has(sendKey) || state.detailRefreshing || !detail?.work_thread?.browser_session || detail.work_thread.attention === "archived" || state.closing?.has(state.selected) ||
      (!dispatcher && detail.running && !detail.activeTurn) || state.attachments?.pending(draftId) || (!text.trim() && !images.length)) return;
  const mentions = state.mentions?.snapshot(draftId, text) || [];
  const body = {message: text};
  if (mentions.length) body.mentions = mentions;
  if (state.sourceWindowId) body.windowId = state.sourceWindowId;
  if (images.length) body.images = images.map(({name, url}) => ({name, url}));
  if (!dispatcher && detail.running && mode !== "queue") body.expectedTurnId = detail.activeTurn;
  (state.sending ||= new Set()).add(sendKey); renderStatus();
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
      await sendSessionDraft(text, images, mentions); return;
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
    const result = await api(sessionPath(id) + (!dispatcher && mode === "queue" ? "/queue" : "/messages"), body);
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
    if (id === state.selected) {
      if ($("message").value === text) { $("message").value = ""; state.drafts.delete(id); }
      else state.drafts.set(id, $("message").value);
      await refreshDetail();
    } else if (state.drafts.get(id) === text) state.drafts.delete(id);
    await refreshList();
    if (!result.command) await state.rollUp?.responded(ticket, {pending: !!state.detail?.requests?.length});
  }
  catch (error) {
    if (dispatchReceived) {
      console.warn("Dispatcher accepted request before UI refresh failed", error);
      throw new Error("Your request was received, but the view could not refresh. Do not send it again.");
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
  if (event.isComposing || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
  if (state.slashCommands?.keydown(event)) return;
  if (state.mentions?.keydown(event)) return;
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
    ? panes().records.get(id).frame.contentDocument.getElementById("message")
    : state.selected === id ? $("message") : null;
  return {
    empty: () => state.threadTabs.items.length === 0,
    capture(id, {validate = true} = {}) {
      const shell = panes(), record = shell?.records.get(id);
      const tab = state.threadTabs.items.find(item => item.thread_id === id) || record?.thread
        || [...state.threadTabs.projects.values()].flat().find(item => item.thread_id === id)
        || (state.drafts.has(id) || state.attachments.drafts.has(id) || state.mentions?.drafts.has(id) ? {thread_id: id} : null);
      if (!tab || id === "new-session") return null;
      if (validate && (state.transferLocks?.has(id) || record?.api?.canTransfer?.() === false || state.busy || state.sending?.size || state.attachments.pending(id) ||
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
      if (state.busy || state.sending?.size || document.querySelector("dialog[open]") ||
          panes()?.focusedRecord?.api?.canTransfer?.() === false)
        throw new Error("Finish the current action in the destination window before moving a tab there.");
      const id = payload.thread.thread_id;
      staged.set(transferId, {id, before: this.capture(id, {validate: false}), selected: state.selected,
        wasOpen: [...state.threadTabs.projects.values()].some(items => items.some(item => item.thread_id === id)),
        paneView: panes()?.active ? panes().viewId : null});
      this.lock(id, true);
      try {
        await this.restore(payload);
        return true;
      } catch (error) {
        await this.finish(transferId, false);
        throw error;
      }
    },
    async finish(transferId, committed) {
      const snapshot = staged.get(transferId);
      if (!snapshot) return !committed;
      staged.delete(transferId);
      const {id, before, wasOpen, selected, paneView} = snapshot;
      this.lock(id, false);
      if (!committed) {
        if (before) {
          await this.restore(before, {rollback: true});
          if (!wasOpen) await this.release(id, state.threadTabs.cwd, undefined, {retainDraft: true});
        }
        else await this.release(id);
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
          JSON.stringify([existing.draft.text, existing.draft.images, existing.draft.mentions]) !==
          JSON.stringify([draft.text, draft.images, draft.mentions]))
        throw new Error("This window already has a different draft for this conversation.");
      if (panes()?.active || state.dispatchDock?.active) goHome({restoreDispatcher: false});
      state.drafts.set(id, draft.text);
      state.attachments.drafts.set(id, draft.images || []);
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
    discard(id) {
      state.drafts.delete(id); state.attachments.drafts.delete(id); state.mentions?.drafts.delete(id);
    },
    async release(id, cwd = state.projectCwd, expected, {retainDraft = false} = {}) {
      if (expected) {
        // Unrelated sends/dialogs may start in another tab while the target
        // loads. Only an actual draft change can invalidate this commit.
        const current = this.capture(id, {validate: false});
        const content = draft => JSON.stringify([draft?.text, draft?.images || [], draft?.mentions || []]);
        if (!current || content(current.draft) !== content(expected.draft))
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
      if (!retainDraft) this.discard(id);
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
async function boot() {
  state.resizeComposer = setupComposerSizing($("message"));
  state.timelineScroll = new ConversationScroll($("timeline"), $("jump-to-latest"));
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
  state.config = await api("config");
  state.schedules = new ScheduledPromptsUI({document, api, openThread: select, onError: showError,
    onChange: () => renderList(),
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
    onChange: () => { state.drafts.set(state.selected, $("message").value); state.resizeComposer?.(); globalThis.agentCoordPane?.saveDraft(); }});
  state.slashCommands = new ChatSlashCommands({input: $("message"), menu: $("slash-commands"),
    getThread: () => state.selected,
    getSession: () => ({...state.detail?.session, cwd: state.detail?.session?.cwd || state.detail?.work_thread?.cwd || state.config?.cwd}),
    catalogs: (state.modelCatalogs ||= new Map()),
    getModelClients: sessionModelClients,
    onSelectModel: selectDraftModel,
    loadModels: loadModelCatalog,
    loadSkills: async session => (await api("skills", {client: session.client || "codex", cwd: session.cwd})).data,
    canComplete: () => !!state.detail?.work_thread?.browser_session && !$("composer").hidden && !sessionBusy() &&
      state.detail.work_thread.attention !== "archived" && !state.closing.has(state.selected),
    onReplace: (start, end, text) => state.mentions?.draft().edit(start, end, text),
    onChange: () => { state.mentions?.render(); state.drafts.set(state.selected, $("message").value); state.resizeComposer?.(); globalThis.agentCoordPane?.saveDraft(); }});
  state.attachments = new ChatImageAttachments({document, getThread: () => composerDraftId(),
    canAttach: () => !!state.detail?.work_thread?.browser_session && state.detail.work_thread.attention !== "archived" && !state.closing.has(state.selected) && !state.transferLocks?.has(state.selected),
    onChange: renderStatus, onError: showError});
  state.attachments.bind();
  const turnTimer = setInterval(() => { if (!document.hidden) renderTurnElapsed(); }, 1000);
  window.addEventListener("pagehide", () => clearInterval(turnTimer), {once: true});
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
    claimSnooze: async snooze_id => (await api("notifications/claim", {snooze_id})).claimed,
    selected: () => window.agentCoordPanes?.active ? window.agentCoordPanes.selected :
      state.dispatchDock?.active && !state.dispatchDock.expanded ? null : state.selected,
    openThread: select,
    onError: showError,
  });
  if (state.config.remote) notifications.start().catch(showError);
  let windowStorage, preferences;
  try { windowStorage = sessionStorage; preferences = localStorage; } catch { /* Views work without browser storage. */ }
  state.overviewPreferences = preferences;
  state.overviewLayout = new OverviewLayout({document, window, storage: preferences,
    canNavigate: () => !state.busy && !document.querySelector("dialog[open]"),
    onChange: (_mode, {navigate}) => { state.listSignature = ""; if (navigate) goHome({restoreDispatcher: false}); else renderList(); }});
  const desktop = window.agentCoordDesktop;
  state.conversationWindow = desktop?.conversationWindow === true;
  const publishDesktopProjects = snapshot => api("desktop-projects", {
    windowId: state.sourceWindowId, projects: snapshot.projects, active: snapshot.active,
  });
  state.folders = new WorkingFolders({storage: desktop && !state.conversationWindow ? preferences : windowStorage, preferences,
    key: (state.conversationWindow ? desktop.windowId : desktop?.folderSlot) || "browser", scope: state.config.workspaceRoot || "", api,
    onChange: desktop ? snapshot => publishDesktopProjects(snapshot).catch(showError) : null});
  const projectQuery = new URLSearchParams(location.search);
  // Browsers copy sessionStorage into a new window. Explicitly opening a
  // project there must not duplicate the originating window's project tabs.
  if (projectQuery.get("new_project") === "1") state.folders.reset();
  else await state.folders.restore(state.config.workspaceRoot).catch(showError);
  const initialDirectory = projectQuery.get("directory");
  if (initialDirectory) await state.folders.open(initialDirectory).catch(showError);
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
  savedViews = new SavedViews({document, api, storage: windowStorage, preferences,
    onRefresh: refreshList,
    notify: (...args) => state.threadActions.notify(...args),
    scope: (state.config.workspaceRoot || "") + ":" + (desktop?.folderSlot || "browser"),
    onSwitch: async () => { goHome(); await refreshList(); window.agentCoordPanes?.restoreView(); }, onError: showError});
  state.projectCwd = state.folders.selected?.cwd || "";
  state.sidecars = new SidecarsUI({document, api, getThreads: () => state.sessions, openThread: select, onError: showError});
  window.addEventListener("pagehide", () => clearInterval(state.sidecars.timer), {once: true});
  const sidecarButton = document.createElement("button");
  sidecarButton.id = "attach-sidecar"; sidecarButton.type = "button"; sidecarButton.className = "quiet";
  sidecarButton.textContent = "Attach sidecar…"; sidecarButton.hidden = true;
  sidecarButton.onclick = () => action(() => state.sidecars.open(state.detail?.work_thread));
  document.querySelector(".session-actions").append(sidecarButton);
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
    storage: desktop && !state.conversationWindow ? preferences : windowStorage, fallbackStorage: desktop && !state.conversationWindow ? windowStorage : null,
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
  await refreshList(); connect();
  savedViews.restoreScroll();
  navigation = new agentCoordNavigation.Router({api, history, location, windowId: state.sourceWindowId, onError: showError,
    blocked: () => Boolean(document.querySelector("dialog[open]") || window.agentCoordPanes?.focusedRecord?.frame?.contentDocument?.querySelector("dialog[open]")),
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
        await select(route.id);
        if (route.item && state.selected === route.id && !focusSearchMessage({thread: route.id, turn: route.turn, item: route.item})) {
          throw new Error("The matching message is no longer available in this conversation's saved history.");
        }
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
  window.addEventListener("popstate", event => navigation.back(event.state?.agentCoord));
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
  window.agentCoordDesktop?.navigationReady();
  if (state.config.remote) await notifications.ready();
  // Checkpoints written by terminal agents arrive through the shared database.
  const metadataTimer = setInterval(() => {
    if (!document.hidden) { refreshList({background: true}).catch(showError); refreshThread().catch(showError); window.agentCoordPanes?.refresh(); }
  }, 5000);
  window.addEventListener("pagehide", () => clearInterval(metadataTimer), {once: true});
  window.addEventListener("pagehide", () => savedViews.remember());
}
boot().catch(showError);
