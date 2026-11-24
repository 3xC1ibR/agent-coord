"use strict";

// POST in submission order within a conversation. Different conversations
// can make independent progress, including while the originating tab is closed.
class ConversationSubmissionQueue {
  constructor() { this.lanes = new Map(); }

  reserve(id, requestId) {
    let lane = this.lanes.get(id);
    if (!lane) this.lanes.set(id, lane = {entries: new Map(), tail: Promise.resolve()});
    let entry = lane.entries.get(requestId);
    if (entry) return entry;
    entry = {before: lane.tail};
    const resolved = new Promise(resolve => { entry.resolve = resolve; });
    lane.tail = entry.before.then(() => resolved);
    lane.entries.set(requestId, entry);
    return entry;
  }

  confirm(id, requestId) {
    const lane = this.lanes.get(id), entry = lane?.entries.get(requestId);
    if (!entry) return false;
    lane.entries.delete(requestId);
    entry.resolve();
    if (!lane.entries.size) this.lanes.delete(id);
    return true;
  }

  submit(id, requestId, deliver) {
    const entry = this.reserve(id, requestId);
    if (entry.attempt) return entry.attempt;
    const attempt = entry.before.then(deliver);
    entry.attempt = attempt;
    attempt.then(() => this.confirm(id, requestId), error => {
      entry.attempt = null;
      if (!error?.submissionUnconfirmed) this.confirm(id, requestId);
    });
    return attempt;
  }
}

function hydrateConversationControls(id, detail) {
  if (!("recent_turns" in detail) && !("agent_tree" in detail)) return;
  if (Array.isArray(detail.agent_tree?.data)) state.agentTreeController?.hydrate(detail.agent_tree.data);
  if (detail.scratchpad) state.scratchpad?.select(id, {snapshot: detail.scratchpad});
  if (Array.isArray(detail.asks)) state.asks?.hydrateThread?.(id, detail.asks);
  if (detail.sidecars) state.sidecars?.hydrate(id, detail.sidecars, $("thread-sidecars"));
  if (Array.isArray(detail.schedules) && state.schedules) {
    state.schedules.update([...state.schedules.items.filter(item => item.target_thread_id !== id), ...detail.schedules]);
  }
  if (Array.isArray(detail.models)) (state.modelCatalogs ||= new Map()).set(detail.session?.client, detail.models);
}

// A browser can copy sessionStorage into an opened or duplicated tab. Hold a
// document lease on the stored ID and allocate a new one if its owner is live.
async function composerWindowScope(view) {
  if (view.agentCoordDesktop?.windowId) return view.agentCoordDesktop.windowId;
  const storageKey = "ribbon-field-composer-window";
  const allocate = () => "browser-" + view.crypto.randomUUID();
  let previous;
  try { previous = view.sessionStorage.getItem(storageKey); } catch { /* Restricted storage. */ }
  let id = previous || allocate();
  if (view.navigator.locks) {
    const occupy = scope => new Promise((resolve, reject) => {
      view.navigator.locks.request(storageKey + ":" + scope, {ifAvailable: true}, lock => {
        resolve(!!lock);
        if (lock) return new Promise(release => view.addEventListener("pagehide", release, {once: true}));
      }).catch(reject);
    });
    while (!await occupy(id)) id = allocate();
  } else {
    // Storage events also work on WebKit versions without Web Locks. Probe
    // only a previously stored ID; a fresh window never waits for a timeout.
    const probe = allocate(), key = storageKey + "-probe";
    let occupied = false;
    const receive = event => {
      if (event.key !== key || !event.newValue) return;
      const [operation, scope, sender] = event.newValue.split("|");
      if (scope !== id) return;
      if (operation === "held" && sender === probe) occupied = true;
      if (operation === "probe" && sender !== probe)
        view.localStorage.setItem(key, ["held", id, sender].join("|"));
    };
    view.addEventListener("storage", receive);
    if (previous) {
      try {
        view.localStorage.setItem(key, ["probe", id, probe].join("|"));
        await new Promise(resolve => view.setTimeout(resolve, 40));
        if (occupied) id = allocate();
      } catch {
        if (view.performance.getEntriesByType("navigation")[0]?.type !== "reload") id = allocate();
      }
    }
  }
  try { view.sessionStorage.setItem(storageKey, id); } catch { /* Document-scoped storage. */ }
  return id;
}

// Preserve local edit order even when a large attachment waits for the first paint.
function writeComposerRecord(id, write, field = "draft") {
  const writes = state.composerWrites ||= new Map();
  const previous = writes.get(id) || Promise.resolve();
  const current = previous.catch(() => {}).then(write);
  writes.set(id, current);
  const release = () => { if (writes.get(id) === current) writes.delete(id); };
  const failures = state.composerWriteFailures ||= new Map();
  const key = JSON.stringify([id, field]);
  current.then(() => { failures.delete(key); release(); }, error => { failures.set(key, error); release(); });
  return current;
}

async function drainComposerWrites() {
  while (state.composerWrites?.size) await Promise.all(state.composerWrites.values());
  const failed = state.composerWriteFailures?.values().next().value;
  if (failed) throw failed;
  return true;
}

function afterComposerPaint() {
  return new Promise(resolve => {
    const timer = setTimeout(resolve, 100);
    if (!document.hidden) requestAnimationFrame(() => setTimeout(() => { clearTimeout(timer); resolve(); }, 0));
  });
}

function persistComposer(id = composerDraftId(), fields = "text") {
  if (!id || !state.durableDrafts || state.clearingSubmission === id) return;
  const versions = state.draftVersions ||= new Map();
  versions.set(id, (versions.get(id) || 0) + 1);
  const patch = fields === "images" ? {images: state.attachments.snapshot(id)} :
    {text: $("message").value, mentions: state.mentions?.snapshot(id, $("message").value) || []};
  writeComposerRecord(id, () => state.durableDrafts.write(id, patch), fields).catch(showError);
}

function persistAttachmentChanges() {
  const snapshots = state.attachmentSnapshots ||= new Map();
  for (const [id, images] of state.attachments.drafts) {
    if (!state.attachments.pending(id) && snapshots.get(id) !== images) {
      snapshots.set(id, images);
      persistComposer(id, "images");
    }
  }
  for (const id of snapshots.keys()) if (!state.attachments.drafts.has(id)) {
    snapshots.delete(id);
    persistComposer(id, "images");
  }
}

function updateOutgoing(id) {
  const entry = state.conversations?.get(id);
  if (entry && state.selected !== id) entry.nodes = null;
  const detail = entry?.detail || (state.selected === id ? state.detail : null);
  if (state.selected === id && state.timelineThread === id)
    state.outgoing.update(document, $("timeline"), id, detail);
  const pane = globalThis.agentCoordPanes?.records.get(id)?.api;
  if (pane?.timeline) state.outgoing.update(document, pane.timeline, id, detail);
}

function discoverComposerSubmissions(id) {
  if (!state.durableDrafts) return Promise.resolve();
  const discoveries = state.outboxDiscoveries ||= new Map();
  if (discoveries.has(id)) return discoveries.get(id);
  const discovery = state.durableDrafts.pending(id).then(pending => {
    for (const row of pending) {
      const echo = state.outgoing.list(id).find(item => item.submissionId === row.requestId) ||
        state.outgoing.add(id, {text: row.text, images: row.images.length, submissionId: row.requestId});
      if (echo.acceptance || echo.transferReceipt) {
        state.durableDrafts.acknowledge(id, row.requestId).catch(showError);
        continue;
      }
      // An echo can precede discovery when a storage read fails. Reserve every
      // journaled request in insertion order, including those already visible.
      state.submissionQueue.reserve(id, row.requestId);
      if (echo.awaitingDiscovery) continue;
      echo.status = "uncertain";
      if (state.config.ordinarySubmissionReceipts === 1 || echo.discoveryFailed) {
        echo.retry = () => deliverComposerSubmission(id, row, echo).catch(showError);
        echo.retry();
      }
    }
    updateOutgoing(id);
  }).catch(error => { discoveries.delete(id); throw error; });
  discoveries.set(id, discovery);
  return discovery;
}

async function restoreLocalDraft(id) {
  if (!state.durableDrafts) return;
  const version = state.draftVersions?.get(id);
  const discovery = discoverComposerSubmissions(id);
  await state.composerWrites?.get(id);
  const [draft] = await Promise.all([state.durableDrafts.read(id), discovery]);
  if (draft && id === composerDraftId() && !state.transferLocks?.has(id) && state.draftVersions?.get(id) === version) {
    $("message").value = draft.text;
    state.drafts.set(state.selected, draft.text);
    state.attachments.drafts.set(id, draft.images);
    (state.attachmentSnapshots ||= new Map()).set(id, draft.images);
    state.mentions?.restore(id, draft.text, draft.mentions);
    renderStatus();
  }
  return id === composerDraftId();
}

function submissionOutcomeUnknown(error) {
  return !error.status || error.status >= 500 || error.code === "coordinator_offline" ||
    error.code === "submission_outcome_unknown" || error.status === 400 &&
    (error.message.startsWith("Your submission may have been received, but its receipt could not be saved.") ||
     error.message.startsWith("This submission has an unconfirmed outcome."));
}

async function deliverComposerSubmission(id, row, echo, staged) {
  return state.submissionQueue.submit(id, row.requestId, async () => {
    if (echo.deliveryResult) return echo.deliveryResult;
    let posted = false, accepted = false;
    try {
      await staged;
      if (echo.acceptance || echo.transferReceipt) {
        await state.durableDrafts?.acknowledge(id, row.requestId);
        return {acceptedMessage: echo.acceptance, transferSubmission: echo.transferReceipt};
      }
      posted = true;
      const result = await api(row.path, row.body);
      const transfer = result.transferSubmission;
      if (transfer && (transfer.threadId !== id || transfer.requestId !== row.requestId))
        throw new Error("Message receipt could not be confirmed.");
      accepted = true;
      echo.deliveryResult = result;
      if (transfer) state.outgoing.transfers(id, [transfer], state.conversations?.get(id)?.detail);
      else {
        if (result.acceptedMessage) state.outgoing.confirm(id, [result.acceptedMessage]);
        if (result.queued && result.id) echo.queueId = result.id;
        state.outgoing.accepted(echo);
      }
      await state.durableDrafts?.acknowledge(id, row.requestId);
      return result;
    } catch (error) {
      if (echo.acceptance || echo.transferReceipt) {
        await state.durableDrafts?.acknowledge(id, row.requestId);
        return {acceptedMessage: echo.acceptance, transferSubmission: echo.transferReceipt};
      }
      if (accepted) throw error;
      if (posted && submissionOutcomeUnknown(error)) {
        error.submissionUnconfirmed = true;
        echo.status = "uncertain";
        if (state.config?.ordinarySubmissionReceipts === 1)
          echo.retry = () => deliverComposerSubmission(id, row, echo).catch(showError);
      } else {
        state.outgoing.rejected(echo, error.message || String(error));
        const restored = !posted || !state.durableDrafts || await state.durableDrafts.recover(id, row.requestId);
        if (!restored) await state.durableDrafts?.acknowledge(id, row.requestId);
        if (restored && !state.drafts.get(id) && !(id === state.selected && $("message").value)) {
          state.drafts.set(id, row.text);
          state.attachments.drafts.set(id, [...row.images, ...state.attachments.items(id)]);
          state.mentions?.restore(id, row.text, row.mentions);
          if (id === state.selected) $("message").value = row.text;
          globalThis.agentCoordPanes?.records.get(id)?.api?.restoreDraft({text: row.text, images: row.images, mentions: row.mentions});
        }
      }
      throw error;
    } finally {
      updateOutgoing(id);
      if (id === state.selected) renderStatus();
    }
  });
}

function submitComposerMessage({id, requestId = crypto.randomUUID(), text, images, mentions, body, path, detail, clear, onStage}) {
  const discovery = discoverComposerSubmissions(id);
  const versions = state.draftVersions ||= new Map();
  versions.set(id, (versions.get(id) || 0) + 1);
  body.requestId = requestId;
  if (state.config?.ordinarySubmissionReceipts === 1) body.client_message_id = requestId;
  state.activity?.submission(id, body);
  const row = {requestId, text, images, mentions, body, path};
  const echo = state.outgoing.add(id, {text, images: images.length, submissionId: requestId, detail});
  echo.awaitingDiscovery = true;
  const large = images.some(image => image.url?.length > 256 * 1024);
  const firstPaint = large ? afterComposerPaint() : undefined;
  const staged = writeComposerRecord(id, async () => {
    if (large) await firstPaint;
    return state.durableDrafts?.stage(id, requestId, row);
  }, requestId);
  onStage?.(staged);
  staged?.catch(() => {}); // The queue observes the failure when this write reaches its turn.
  state.clearingSubmission = id;
  try { clear(); } finally { state.clearingSubmission = null; }
  updateOutgoing(id);
  const deliver = () => discoverComposerSubmissions(id).then(() => deliverComposerSubmission(id, row, echo, staged));
  return discovery.then(() => {
    echo.awaitingDiscovery = false;
    return deliverComposerSubmission(id, row, echo, staged);
  }, error => {
    echo.awaitingDiscovery = false; echo.discoveryFailed = true;
    echo.status = "uncertain";
    echo.retry = () => deliver().catch(showError);
    updateOutgoing(id);
    throw error;
  });
}
