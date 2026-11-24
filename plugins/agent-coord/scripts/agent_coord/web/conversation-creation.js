"use strict";

// Reserve a session creation separately from its first message. A process may
// disappear before either response, or after creation but before message intake.
function showCreationIntent(projectId, intent) {
  if (state.selected !== "new-session" || state.newSessionDraft?.draftId !== projectId) return;
  const row = intent.row;
  replaceTimelineContent(state.outgoing.node(document, {
    key: row.requestId, text: row.text, images: row.images.length, status: intent.status,
    error: intent.error, retry: canReplayCreation(row) ? () => continueCreation(projectId, intent).catch(showError) : null,
  }));
}

function canReplayCreation(row) {
  return row.body.creationIntent.receiptCapable && state.config?.sessionCreationReceipts === 1;
}

function restoreCreationIntent(projectId, row) {
  const intents = state.creationIntents ||= new Map();
  let intent = intents.get(projectId);
  if (!intent) {
    intent = {row, status: "uncertain"};
    intents.set(projectId, intent);
    if (canReplayCreation(row)) continueCreation(projectId, intent).catch(showError);
  }
  showCreationIntent(projectId, intent);
}

function startCreation(projectId, settings, text, images, mentions, quiet) {
  const intents = state.creationIntents ||= new Map();
  const existing = intents.get(projectId);
  if (existing) {
    showCreationIntent(projectId, existing);
    throw new Error("The first message is still awaiting session creation. Your follow-up is saved.");
  }
  const creation = {settings, firstMessageId: crypto.randomUUID(), windowId: state.sourceWindowId,
    receiptCapable: state.config?.sessionCreationReceipts === 1, quiet};
  const row = {requestId: crypto.randomUUID(), path: "sessions", text, images, mentions, body: {creationIntent: creation}};
  const intent = {row, status: "sending"};
  const discovery = discoverComposerSubmissions(projectId);
  const paint = images.some(image => image.url?.length > 256 * 1024) ? afterComposerPaint() : undefined;
  const staged = writeComposerRecord(projectId, async () => {
    await discovery;
    const prior = intents.get(projectId);
    if (prior && prior !== intent) {
      showCreationIntent(projectId, prior);
      throw new Error("An earlier session creation is still pending. Your follow-up is saved.");
    }
    intents.set(projectId, intent);
    await paint;
    return state.durableDrafts.stage(projectId, row.requestId, row);
  }, row.requestId);
  const input = $("message");
  const versions = state.draftVersions ||= new Map();
  versions.set(projectId, (versions.get(projectId) || 0) + 1);
  showCreationIntent(projectId, intent);
  state.clearingSubmission = projectId;
  try {
    input.value = ""; state.drafts.delete("new-session");
    state.mentions?.sent(projectId, text, mentions);
    if (images.length) state.attachments?.sent(projectId, images);
  } finally { state.clearingSubmission = null; }
  state.timelineScroll?.latest();
  return staged.then(() => continueCreation(projectId, intent), error => {
    if (intents.get(projectId) === intent) intents.delete(projectId);
    if (state.newSessionDraft?.draftId === projectId && !input.value) {
      state.drafts.set("new-session", text); input.value = text;
      state.attachments?.drafts.set(projectId, images);
      state.mentions?.restore(projectId, text, mentions);
    }
    throw error;
  });
}

function continueCreation(projectId, intent) {
  if (intent.attempt) return intent.attempt;
  const row = intent.row, creation = row.body.creationIntent;
  const operation = async () => {
    const settings = {...creation.settings};
    if (creation.receiptCapable && !canReplayCreation(row)) {
      intent.status = "uncertain"; showCreationIntent(projectId, intent);
      throw new Error("Session creation requires a coordinator that can verify its original request.");
    }
    if (creation.receiptCapable) settings.requestId = row.requestId;
    intent.status = "sending"; showCreationIntent(projectId, intent);
    let created;
    try {
      created = intent.created || await api("sessions", settings);
      if (typeof created.session?.thread_id !== "string" || !created.session.thread_id)
        throw new Error("Session creation could not be confirmed.");
      intent.created = created;
    } catch (error) {
      intent.status = submissionOutcomeUnknown(error) ? "uncertain" : "failed";
      intent.error = error.message;
      if (intent.status === "failed" && await state.durableDrafts.recover(projectId, row.requestId)) {
        state.creationIntents.delete(projectId);
        if (state.newSessionDraft?.draftId === projectId) await restoreLocalDraft(projectId);
      }
      showCreationIntent(projectId, intent);
      throw error;
    }
    const threadId = created.session.thread_id;
    const body = {message: row.text, requestId: creation.firstMessageId};
    if (creation.windowId) body.windowId = creation.windowId;
    if (creation.quiet) body.quiet = true;
    if (row.mentions.length) body.mentions = row.mentions;
    if (row.images.length) body.images = row.images.map(({name, url}) => ({name, url}));
    if (state.config?.ordinarySubmissionReceipts === 1) body.client_message_id = creation.firstMessageId;
    const message = {requestId: creation.firstMessageId, body, path: sessionPath(threadId) + "/messages"};
    const discovered = discoverComposerSubmissions(threadId);
    // Block only the local source and destination transactions. The native
    // durability barrier never waits for session creation or provider intake.
    const previous = state.composerWrites?.get(projectId);
    const promoted = writeComposerRecord(threadId, async () => {
      await previous;
      return state.durableDrafts.promoteCreation(projectId, row.requestId, threadId, message);
    });
    writeComposerRecord(projectId, () => promoted).catch(showError);
    const first = await promoted;
    state.creationIntents.delete(projectId);
    if (!first) return;
    await discovered;
    const echo = state.outgoing.list(threadId).find(item => item.submissionId === first.requestId) ||
      state.outgoing.add(threadId, {text: first.text, images: first.images.length, submissionId: first.requestId, detail: created});
    const delivery = deliverComposerSubmission(threadId, first, echo);
    delivery.catch(() => {});
    await adoptCreatedConversation(projectId, created, creation.settings);
    await delivery;
    if (state.selected === threadId) refreshDetail({background: true}).catch(showError);
    refreshList({background: true}).catch(showError);
  };
  intent.attempt = operation().finally(() => { intent.attempt = null; });
  return intent.attempt;
}

async function adoptCreatedConversation(projectId, created, settings) {
  const id = created.session.thread_id, cwd = settings.cwd;
  const current = state.newSessionDraft?.draftId === projectId;
  const saved = state.projectDrafts?.get(cwd);
  const draft = current ? state.newSessionDraft : saved?.detail;
  if (draft) {
    const entry = conversationEntry(id);
    entry.detail ||= {session: {...settings, ...created.session}, thread: created.thread || {turns: []},
      work_thread: {...draft.work_thread, thread_id: id, title: "Conversation", client: settings.client},
      requests: [], queuedMessages: [], running: false};
    const remaining = current ? state.selected === "new-session" ? $("message").value : state.drafts.get("new-session") : saved.text;
    if (remaining) state.drafts.set(id, remaining);
    state.attachments?.move(projectId, id);
    state.mentions?.move(projectId, id);
    if (saved?.detail === draft) state.projectDrafts.delete(cwd);
    if (current) { state.newSessionDraft = null; state.drafts.delete("new-session"); }
  }
  if (settings.project_id) savedViews?.showGroup(settings.project_id, cwd);
  state.threadTabs?.replace("new-session", {thread_id: id, title: "Conversation"}, {cwd});
  const destination = state.projectDestinations?.get(cwd);
  if (destination?.tab === "new-session") destination.tab = id;
  if (current && state.selected === "new-session") {
    $("message").value = "";
    await select(id, {refreshInventory: false}).catch(showError);
  }
}

async function recoverWorkspaceOutbox() {
  for (const id of await state.durableDrafts?.pendingThreadIds?.() || []) {
    await discoverComposerSubmissions(id);
  }
}
