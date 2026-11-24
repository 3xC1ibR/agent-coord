"use strict";

// POST in submission order within a conversation. Different conversations
// can make independent progress, including while the originating tab is closed.
class ConversationSubmissionQueue {
  constructor() { this.tails = new Map(); this.active = new Map(); }

  submit(id, requestId, deliver) {
    const pending = this.active.get(requestId);
    if (pending) return pending;
    const previous = this.tails.get(id) || Promise.resolve();
    const delivery = previous.catch(() => {}).then(deliver);
    this.active.set(requestId, delivery);
    this.tails.set(id, delivery);
    const release = () => {
      this.active.delete(requestId);
      if (this.tails.get(id) === delivery) this.tails.delete(id);
    };
    delivery.then(release, release);
    return delivery;
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

function persistComposer(id = composerDraftId(), fields = "text") {
  if (!id || !state.durableDrafts) return;
  const versions = state.draftVersions ||= new Map();
  versions.set(id, (versions.get(id) || 0) + 1);
  const patch = fields === "images" ? {images: state.attachments.snapshot(id)} :
    {text: $("message").value, mentions: state.mentions?.snapshot(id, $("message").value) || []};
  state.durableDrafts.write(id, patch).catch(showError);
}

function updateOutgoing(id) {
  const detail = state.conversations?.get(id)?.detail || state.detail;
  if (state.selected === id && state.timelineThread === id)
    state.outgoing.update(document, $("timeline"), id, detail);
  const pane = globalThis.agentCoordPanes?.records.get(id)?.api;
  if (pane?.timeline) state.outgoing.update(document, pane.timeline, id, detail);
}

async function restoreLocalDraft(id) {
  if (!state.durableDrafts) return;
  const version = state.draftVersions?.get(id);
  const [draft, pending] = await Promise.all([state.durableDrafts.read(id), state.durableDrafts.pending(id)]);
  if (draft && id === composerDraftId() && state.draftVersions?.get(id) === version) {
    $("message").value = draft.text;
    state.drafts.set(state.selected, draft.text);
    state.attachments.drafts.set(id, draft.images);
    state.mentions?.restore(id, draft.text, draft.mentions);
    renderStatus();
  }
  for (const row of pending) {
    if (state.outgoing.list(id).some(echo => echo.submissionId === row.requestId)) continue;
    const echo = state.outgoing.add(id, {text: row.text, images: row.images.length, submissionId: row.requestId});
    echo.status = "uncertain";
    if (state.config.ordinarySubmissionReceipts === 1) {
      echo.retry = () => deliverComposerSubmission(id, row, echo).catch(showError);
      echo.retry();
    }
  }
  updateOutgoing(id);
  return id === composerDraftId();
}

async function deliverComposerSubmission(id, row, echo, staged) {
  return state.submissionQueue.submit(id, row.requestId, async () => {
    let posted = false, accepted = false;
    try {
      await staged;
      posted = true;
      const result = await api(row.path, row.body);
      const transfer = result.transferSubmission;
      if (transfer && (transfer.threadId !== id || transfer.requestId !== row.requestId))
        throw new Error("Message receipt could not be confirmed.");
      accepted = true;
      if (transfer) state.outgoing.transfers(id, [transfer], state.conversations?.get(id)?.detail);
      else {
        if (result.acceptedMessage) state.outgoing.confirm(id, [result.acceptedMessage]);
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
      if (posted && (!error.status || error.status >= 500 || error.code === "coordinator_offline")) {
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

function submitComposerMessage({id, text, images, mentions, body, path, detail, clear}) {
  const versions = state.draftVersions ||= new Map();
  versions.set(id, (versions.get(id) || 0) + 1);
  const requestId = crypto.randomUUID();
  Object.assign(body, {requestId, client_message_id: requestId});
  const row = {requestId, text, images, mentions, body, path};
  const echo = state.outgoing.add(id, {text, images: images.length, submissionId: requestId, detail});
  const staged = state.durableDrafts?.stage(id, requestId, row);
  staged?.catch(() => {}); // The queue observes the failure when this write reaches its turn.
  clear();
  updateOutgoing(id);
  return deliverComposerSubmission(id, row, echo, staged);
}
