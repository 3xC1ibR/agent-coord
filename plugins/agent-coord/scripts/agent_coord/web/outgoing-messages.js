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
async function claimComposerWindowHandoff(database, id, generation) {
  if (!database || !Number.isSafeInteger(generation) || generation < 1) return false;
  return new Promise(resolve => {
    const open = database.open("ribbon-field-composer-windows", 1);
    open.onupgradeneeded = () => open.result.createObjectStore("generations");
    open.onerror = open.onblocked = () => resolve(false);
    open.onsuccess = () => {
      const db = open.result, transaction = db.transaction("generations", "readwrite");
      const store = transaction.objectStore("generations"), prior = store.get(id);
      let claimed = false;
      prior.onsuccess = () => {
        if ((prior.result || 0) + 1 !== generation) return;
        store.put(generation, id); claimed = true;
      };
      transaction.oncomplete = () => { db.close(); resolve(claimed); };
      transaction.onerror = transaction.onabort = () => { db.close(); resolve(false); };
    };
  });
}

async function composerWindowScope(view, claim = claimComposerWindowHandoff) {
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
        if (lock) return new Promise(release => {
          const departing = event => {
            if (event?.persisted) return;
            view.removeEventListener?.("pagehide", departing);
            release();
          };
          view.addEventListener("pagehide", departing);
        });
      }).catch(reject);
    });
    while (!await occupy(id)) id = allocate();
  } else {
    // An active tab never exposes a reusable ID for a clone. The departing
    // document grants its own successor a one-use handoff during navigation.
    // A throttled background tab cannot accidentally grant ownership by silence.
    const handoff = storageKey + "-handoff";
    try {
      const ticket = JSON.parse(view.sessionStorage.getItem(handoff) || "null");
      let generation = 0;
      if (previous && ticket?.id === previous && await claim(view.indexedDB, previous, ticket.generation))
        generation = ticket.generation;
      else id = allocate();
      view.sessionStorage.removeItem(handoff);
      view.addEventListener("pagehide", event => {
        if (!event?.persisted) view.sessionStorage.setItem(handoff, JSON.stringify({id, generation: generation + 1}));
      });
      view.addEventListener("pageshow", () => view.sessionStorage.removeItem(handoff));
    } catch { id = allocate(); }
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
  await globalThis.ChatImageAttachments?.drain?.();
  let repaired = false;
  while (state.composerWrites?.size || !repaired && state.composerRepairs?.size) {
    if (!state.composerWrites?.size) {
      repaired = true;
      await Promise.allSettled([...state.composerRepairs.keys()].map(saveRepairedComposer));
      continue;
    }
    for (const proceed of state.composerPaints || []) proceed();
    await Promise.allSettled(state.composerWrites.values());
  }
  const failed = state.composerWriteFailures?.values().next().value;
  if (failed) throw failed;
  return true;
}

function afterComposerPaint() {
  return new Promise(resolve => {
    const paints = state.composerPaints ||= new Set();
    function finish() { clearTimeout(timer); paints.delete(finish); resolve(); }
    const timer = setTimeout(finish, 100);
    paints.add(finish);
    if (!document.hidden) requestAnimationFrame(() => setTimeout(finish, 0));
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
  for (const id of state.attachments.drafts.keys()) {
    const images = state.attachments.snapshot(id), previous = snapshots.get(id) || [];
    if (previous.length !== images.length || images.some((image, index) => image !== previous[index])) {
      snapshots.set(id, images);
      persistComposer(id, "images");
    }
  }
  for (const id of snapshots.keys()) if (!state.attachments.drafts.has(id)) {
    snapshots.delete(id);
    persistComposer(id, "images");
  }
}

// A rejected local stage has no provider effect. Persist whichever draft the
// user can now edit, including edits made while the journal was unavailable.
function repairComposerStage(id, row, echo, error) {
  echo.localFailure = true;
  state.outgoing.rejected(echo, error.message || String(error));
  const {pane, selected, field, savedProject, current} = composerRecoveryState(id);
  if (!current.text && !current.images.length) {
    current.text = row.text; current.images = row.images; current.mentions = row.mentions;
    if (savedProject && !selected) savedProject.text = current.text;
    else state.drafts.set(selected ? state.selected : id, current.text);
    if (field) field.value = current.text;
    state.attachments?.drafts.set(id, current.images);
    state.mentions?.restore(id, current.text, current.mentions);
    pane?.restoreDraft(current);
  }
  const repairs = state.composerRepairs ||= new Map();
  if (!repairs.has(id)) repairs.set(id, new Set());
  repairs.get(id).add(row.requestId);
  updateOutgoing(id);
  return saveRepairedComposer(id);
}

function composerRecoveryState(id) {
  const pane = globalThis.agentCoordPanes?.records.get(id)?.api;
  const selected = id === composerDraftId(), field = selected ? $("message") : null;
  const savedProject = id.startsWith("new-session:") && state.projectDrafts?.get(id.slice("new-session:".length));
  const text = field?.value ?? savedProject?.text ?? state.drafts.get(id) ?? "";
  const current = pane?.exportDraft() || {text, images: state.attachments?.snapshot(id) || [],
    mentions: state.mentions?.snapshot(id, text) || []};
  return {pane, selected, field, savedProject, current};
}

async function saveRepairedComposer(id) {
  const repairs = state.composerRepairs.get(id);
  if (!repairs) return;
  const requestIds = [...repairs], {current} = composerRecoveryState(id);
  const full = {text: current.text, images: current.images, mentions: current.mentions};
  await writeComposerRecord(id, () => state.durableDrafts.write(id, full));
  for (const key of requestIds) {
    state.composerWriteFailures?.delete(JSON.stringify([id, key]));
    repairs.delete(key);
  }
  if (!repairs.size) state.composerRepairs.delete(id);
}

function updateOutgoing(id) {
  const entry = state.conversations?.get(id);
  if (entry && state.selected !== id) entry.nodes = null;
  const detail = entry?.detail || (state.selected === id ? state.detail : null);
  if (state.selected === id && (state.timelineThread === id || !state.detail && state.detailRefreshing))
    state.outgoing.update(document, $("timeline"), id, detail);
  const pane = globalThis.agentCoordPanes?.records.get(id)?.api;
  if (pane?.timeline) state.outgoing.update(document, pane.timeline, id, detail);
}

function discoverComposerSubmissions(id) {
  if (!state.durableDrafts) return Promise.resolve();
  const discoveries = state.outboxDiscoveries ||= new Map();
  if (discoveries.has(id)) return discoveries.get(id);
  const discovery = state.durableDrafts.pending(id).then(pending => {
    const live = state.discoveringSubmissions?.get(id);
    for (const row of pending) {
      if (live?.has(row.requestId)) continue;
      if (row.path === "sessions" && row.body?.creationIntent) { restoreCreationIntent(id, row); continue; }
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
      if (!row.path || state.config.ordinarySubmissionReceipts === 1 && row.body?.client_message_id === row.requestId || echo.discoveryFailed) {
        echo.retry = () => deliverComposerSubmission(id, row, echo).catch(showError);
        echo.retry();
      } else allowUnconfirmedRelease(id, row, echo);
    }
    // Local Enter events can precede the journal commit as well as its read.
    // Put the entire live suffix behind restored rows in its original order.
    for (const {row, echo, staged} of live?.values() || []) {
      state.submissionQueue.reserve(id, row.requestId);
      if (echo.discoveryFailed && !echo.awaitingDiscovery) deliverComposerSubmission(id, row, echo, staged).catch(showError);
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

function allowUnconfirmedRelease(id, row, echo) {
  echo.release = async () => {
    await state.durableDrafts?.acknowledge(id, row.requestId);
    state.submissionQueue.confirm(id, row.requestId);
    echo.status = "unverified"; echo.release = null;
    updateOutgoing(id);
  };
}

async function bindComposerSubmission(id, row) {
  const entry = conversationEntry(id);
  const verified = entry.detail && !entry.accessDenied && entry.validatedRevision === entry.revision &&
    entry.detail.revisionEpoch === state.config?.conversationRevisionEpoch;
  const fresh = verified || await (entry.pending || refreshDetail({threadId: id, markSeen: false, background: true}));
  const detail = entry.detail, work = detail?.work_thread;
  if (fresh === false || !work || work.thread_id !== id || entry.accessDenied)
    throw new Error("Waiting for current conversation details.");
  if (!work.browser_session || work.attention === "archived")
    throw Object.assign(new Error("This conversation does not accept messages."), {status: 400});
  const reviewing = detail.reviews?.some(review => review.active);
  const ordered = state.outgoing.pendingTransferCounts?.has(id) && ["codex", "claude"].includes(work.client);
  const queue = !work.dispatcher && !reviewing && detail.running && ["antigravity", "kimi"].includes(work.client);
  const body = {...row.body}, path = sessionPath(id) + (queue ? "/queue" : "/messages");
  if (detail.running || reviewing) delete body.review;
  if (work.dispatcher) body.askDispatcher = false;
  else if (!queue && !reviewing && !ordered && detail.running) {
    if (detail.activeTurn) body.expectedTurnId = detail.activeTurn;
    else if (!detail.acceptsUntargetedInput) throw new Error("Waiting for the current turn to accept input.");
  }
  if (!state.durableDrafts) return {...row, path, body};
  return writeComposerRecord(id, () => state.durableDrafts.resolveSubmission(id, row.requestId, path, body), "route:" + row.requestId);
}

async function deliverComposerSubmission(id, row, echo, staged) {
  const live = state.discoveringSubmissions?.get(id);
  live?.delete(row.requestId);
  if (live && !live.size) state.discoveringSubmissions.delete(id);
  return state.submissionQueue.submit(id, row.requestId, async () => {
    if (echo.deliveryResult) return echo.deliveryResult;
    let posted = false, accepted = false, journaled = false;
    try {
      await staged;
      journaled = true;
      if (!row.path) {
        const bound = await bindComposerSubmission(id, row);
        if (!bound) throw new Error("The original submission could not be located.");
        row = bound;
      }
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
        if (result.command || result.review) {
          state.outgoing.remove(id, echo);
          if (result.command) state.commandFeedback?.set(id, result.command.message);
          if (result.review) state.reviewUI?.sent(id);
        }
      }
      await state.durableDrafts?.acknowledge(id, row.requestId);
      return result;
    } catch (error) {
      if (echo.localFailure) throw error;
      if (echo.acceptance || echo.transferReceipt) {
        await state.durableDrafts?.acknowledge(id, row.requestId);
        return {acceptedMessage: echo.acceptance, transferSubmission: echo.transferReceipt};
      }
      if (accepted) throw error;
      const awaitingRoute = journaled && !posted && !row.path &&
        (!error.status || error.status >= 500 || error.status === 429);
      if (awaitingRoute || posted && submissionOutcomeUnknown(error)) {
        error.submissionUnconfirmed = true;
        echo.awaitingRoute = awaitingRoute;
        echo.status = "uncertain";
        if (awaitingRoute || state.config?.ordinarySubmissionReceipts === 1)
          echo.retry = () => deliverComposerSubmission(id, row, echo).catch(showError);
        else allowUnconfirmedRelease(id, row, echo);
      } else {
        state.outgoing.rejected(echo, error.message || String(error));
        const restored = !journaled || !state.durableDrafts || await state.durableDrafts.recover(id, row.requestId);
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
  const discovering = state.discoveringSubmissions ||= new Map();
  let live = discovering.get(id);
  if (!live) discovering.set(id, live = new Map());
  live.set(requestId, {row, echo, staged});
  onStage?.(staged);
  // Local recovery must run even if an earlier provider submission is still
  // unconfirmed and this row has not reached the head of its network lane.
  staged.catch(error => repairComposerStage(id, row, echo, error)).catch(showError);
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
