"use strict";

// Each visible tile owns only its elements and reading position. History,
// accepted messages, drafts, and event delivery belong to the workspace.
class ConversationPane {
  constructor(host, shell, record, workspace) {
    this.host = host;
    this.shell = shell;
    this.id = record.thread.thread_id;
    this.workspace = workspace;
    this.visual = {};
    this.view = Object.assign(Object.create(workspace), {selected: this.id, detail: null,
      timelineThread: null, timelineRender: null, detailRefreshing: false, searchTarget: null, messageTarget: null, responseActions: null});
    this.timeline = node("div", null, "timeline");
    this.timeline.dataset.paneTimeline = "";
    this.timeline.setAttribute("aria-label", record.thread.title);
    this.timeline.tabIndex = -1;
    this.composer = node("form", null, "composer");
    this.composer.dataset.paneComposer = "";
    this.input = node("textarea");
    this.input.rows = 1; this.input.placeholder = "Message agent…";
    this.input.setAttribute("aria-label", "Message " + record.thread.title);
    this.composer.append(this.input);
    this.setupAttachments();
    const send = node("button", "↑", "primary composer-icon");
    send.type = "submit"; send.setAttribute("aria-label", "Send message");
    this.composer.append(send);
    const latest = node("button", "↓", "jump-to-latest");
    latest.type = "button"; latest.setAttribute("aria-label", "Jump to latest"); latest.hidden = true;
    this.controls = node("div", null, "pane-tools");
    const open = node("button", "Open conversation", "quiet"); open.type = "button";
    open.onclick = () => { shell.leave(); select(this.id).catch(showError); };
    this.controls.append(open);
    this.requests = node("div", null, "requests");
    host.dataset.threadId = this.id;
    host.append(this.timeline, latest, this.requests, this.controls, this.composer);
    this.view.timelineScroll = new ConversationScroll(this.timeline, latest);
    this.view.responseActions = new ResponseActions({timeline: this.timeline, input: this.input, composer: this.composer,
      canDiscuss: () => !this.input.disabled && !this.composer.hidden, onHold: () => this.view.timelineScroll.hold(), onError: showError});
    this.input.oninput = () => this.saveDraft();
    this.composer.onsubmit = event => { event.preventDefault(); this.submit(this.input.value).catch(showError); };
    this.input.onkeydown = event => {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
      const quiet = workspace.config?.quietSubmissions === 1 && TurnNotifications.isQuietShortcut(event);
      if (quiet || !event.metaKey && !event.ctrlKey && !event.altKey && !matchMedia("(pointer: coarse)").matches) {
        event.preventDefault(); this.submit(this.input.value, true, quiet).catch(showError);
      }
    };
    host.addEventListener("focusin", () => { if (shell.selected !== this.id) shell.focus(this.id, false); });
    host.addEventListener("pointerdown", () => { if (shell.selected !== this.id) shell.focus(this.id, false); });
    const draft = record.initialDraft || shell.getDraft(this.id);
    this.restoreDraft(draft);
    if (workspace.durableDrafts && !draft?.text && !draft?.images?.length) {
      const revision = this.draftRevision || 0;
      workspace.durableDrafts.read(this.id).then(stored => {
        if (!this.disposed && stored && revision === (this.draftRevision || 0)) this.restoreDraft(stored);
      }).catch(showError);
    }
    this.refresh().catch(showError);
  }

  setupAttachments() {
    const chooser = node("input");
    chooser.type = "file"; chooser.accept = "image/png,image/jpeg,image/webp,image/gif";
    chooser.multiple = true; chooser.hidden = true;
    const attach = this.attachButton = node("button", "+", "quiet composer-icon");
    attach.type = "button"; attach.setAttribute("aria-label", "Attach image");
    attach.onclick = () => chooser.click();
    this.imageList = node("div", null, "image-attachments");
    this.imageList.hidden = true;
    this.images = new ChatImageAttachments({document, getThread: () => this.id,
      canAttach: () => !this.disposed && !this.composer.hidden && !this.input.disabled,
      onError: showError, onChange: () => {
        this.draftRevision = (this.draftRevision || 0) + 1;
        const images = this.images.snapshot();
        if (this.workspace.durableDrafts && !this.images.pending() && state.clearingSubmission !== this.id)
          writeComposerRecord(this.id, () => this.workspace.durableDrafts.write(this.id, {images})).catch(showError);
        if (!this.disposed) { this.showImages(); this.shell.onDraft?.(this.id, this.exportDraft()); }
      }});
    this.images.drafts = this.workspace.attachments.drafts;
    chooser.onchange = () => { this.images.add([...chooser.files]).catch(showError); chooser.value = ""; };
    this.composer.prepend(this.imageList, chooser, attach);
    for (const event of ["dragenter", "dragover", "drop"]) this.host.addEventListener(event, input => {
      if (!Array.from(input.dataTransfer.types).includes("Files")) return;
      input.preventDefault(); input.stopPropagation();
      if (event === "drop") this.images.add(input.dataTransfer.files).catch(showError);
    });
    this.input.addEventListener("paste", event => {
      if (event.clipboardData.files.length) {
        event.preventDefault(); this.images.add(event.clipboardData.files).catch(showError);
      }
    });
  }

  showImages() {
    const items = this.images.items();
    this.attachButton.disabled = this.input.disabled;
    this.imageList.hidden = !items.length;
    if (this.renderedImages?.length === items.length &&
        items.every((item, index) => item === this.renderedImages[index].item && item.url === this.renderedImages[index].url)) return;
    this.renderedImages = items.map(item => ({item, url: item.url}));
    this.imageList.replaceChildren(...items.map(item => {
      const remove = node("button", item.name + " ×", "quiet"); remove.type = "button";
      remove.setAttribute("aria-label", "Remove " + item.name);
      remove.onclick = () => this.images.remove(this.id, item);
      return remove;
    }));
  }

  render() {
    if (this.disposed) return;
    const detail = this.view.detail = conversationEntry(this.id).detail;
    if (!detail) { this.timeline.textContent = "Opening conversation…"; this.input.disabled = true; return; }
    this.composer.hidden = !detail.work_thread.browser_session || detail.work_thread.attention === "archived";
    this.input.disabled = !!this.workspace.transferLocks?.has(this.id);
    this.showImages();
    renderConversationTimeline(this);
    renderConversationRequests(this);
    this.markSeen();
  }

  async refresh() {
    this.render();
    const fresh = await refreshDetail({threadId: this.id, markSeen: false, background: true});
    if (fresh === false && !this.disposed) this.scheduleRefresh();
    this.render();
  }

  receive(batch) {
    const changed = (batch.events || []).filter(event => event.params?.threadId === this.id);
    if (batch.reset || [...(batch.approvals || []), ...(batch.completions || [])].some(row => row.thread_id === this.id) ||
        changed.some(e => ["browser/changed", "browser/requests", "coordination/messages", "turn/completed", "serverRequest/resolved"].includes(e.method) || e.requestKey)) {
      this.scheduleRefresh();
    }
    if (changed.length && !this.paint) this.paint = requestAnimationFrame(() => { this.paint = null; this.render(); });
  }

  scheduleRefresh() {
    if (this.refreshTimer) return;
    this.refreshTimer = setTimeout(() => {
      this.refreshTimer = null; this.refresh().catch(showError);
    }, 100);
  }

  async submit(text, clear = true, quiet = false) {
    const detail = this.view.detail, draft = this.exportDraft();
    if (!detail?.work_thread.browser_session || detail.work_thread.attention === "archived" || this.input.disabled) return;
    const images = clear ? draft.images : [], mentions = clear ? draft.mentions : [];
    if (this.images.pending() || !text.trim() && !images.length) return;
    const body = {message: text, windowId: this.workspace.sourceWindowId};
    if (quiet) body.quiet = true;
    if (mentions.length) body.mentions = mentions;
    if (images.length) body.images = images.map(({name, url}) => ({name, url}));
    if (detail.running && detail.activeTurn) body.expectedTurnId = detail.activeTurn;
    const queue = detail.running && ["antigravity", "kimi"].includes(detail.work_thread.client);
    await submitComposerMessage({id: this.id, text, images, mentions, body, detail,
      path: sessionPath(this.id) + (queue ? "/queue" : "/messages"), clear: () => {
        if (clear) { this.restoreDraft({text: "", images: [], mentions: []}); this.saveDraft(); }
        this.view.timelineScroll.latest();
      }});
    if (!this.disposed) this.refresh().catch(showError);
  }

  markSeen() {
    const work = this.view.detail?.work_thread;
    if (!this.active || !work || document.hidden || !document.hasFocus() || this.view.detail.deferReadAcknowledgment) return;
    const seen = {seen: true, seen_checkpoint_id: work.checkpoint?.id || 0, seen_completion_id: work.turn_completion?.id || 0};
    const signature = JSON.stringify(seen);
    if (this.seen === signature) return;
    if (!work.unread_result && seen.seen_checkpoint_id <= (work.seen_checkpoint_id || 0) &&
        seen.seen_completion_id <= (work.seen_completion_id || 0)) return;
    this.seen = signature;
    api(threadPath(this.id), seen, {headers: {Prefer: "return=minimal"}}).catch(error => { this.seen = null; showError(error); });
  }

  saveDraft() {
    this.draftRevision = (this.draftRevision || 0) + 1;
    const draft = this.exportDraft();
    this.shell.onDraft?.(this.id, draft);
    if (this.workspace.durableDrafts && state.clearingSubmission !== this.id)
      writeComposerRecord(this.id, () => this.workspace.durableDrafts.write(this.id, {text: draft.text, mentions: draft.mentions})).catch(showError);
  }
  exportDraft() {
    return {text: this.input.value, images: this.workspace.attachments.items(this.id),
      mentions: this.workspace.mentions?.snapshot(this.id, this.input.value) || [],
      scroll: this.view.timelineScroll.position(), following: this.view.timelineScroll.following};
  }
  restoreDraft(draft) {
    if (!draft) return;
    this.input.value = draft.text || "";
    this.workspace.attachments.drafts.set(this.id, draft.images || []);
    this.workspace.mentions?.restore(this.id, this.input.value, draft.mentions || []);
    this.workspace.drafts.set(this.id, this.input.value);
    this.showImages();
    if (Number.isFinite(draft.scroll)) this.view.timelineScroll.restore(draft.scroll);
  }
  earlierControl(count) {
    const button = node("button", "Show " + count + " earlier turns", "quiet timeline-earlier");
    button.onclick = () => action(async () => {
      const entry = conversationEntry(this.id), before = entry.detail.thread.beforeSeq;
      if (state.config?.conversationHistoryPages && entry.detail.thread.earlierTurns) {
        const result = await api(sessionPath(this.id) + "?before_seq=" + before + "&limit=40");
        const page = historyPage(result, this.id, before);
        if (this.disposed || entry.detail.thread.beforeSeq !== before) return;
        const scroller = this.view.timelineScroll.scroller(), top = scroller.getBoundingClientRect().top;
        const anchor = [...this.timeline.children].find(row=>row.dataset.viewportKey && !row.hidden && row.getBoundingClientRect().bottom > top);
        if (anchor && this.visual.viewport) this.visual.viewport.anchorKey = anchor.dataset.viewportKey;
        this.view.timelineScroll.hold();
        entry.nodes = null; entry.pageBytes = (entry.pageBytes || 0) + (result.snapshotBytes || 0);
        entry.retainedBytes = (entry.retainedBytes || 0) + (result.snapshotBytes || 0);
        entry.detail.thread = mergeHistoryWindow(entry, page, true);
        entry.historyLoaded = true;
      } else if (entry.detail.thread.earlierTurns) {
        entry.completeHistory = true;
        await refreshDetail({threadId: this.id, markSeen: false});
      }
      this.view.timelineScroll.hold();
      this.visual.allTurns = true; this.render();
    }, button);
    return button;
  }
  outputControl(turn, item) {
    const button = node("button", "Show full output", "quiet tool-complete");
    button.onclick = () => action(async () => {
      const entry = conversationEntry(this.id);
      if (!state.config?.conversationHistoryPages) {
        entry.completeHistory = true;
        await this.refresh(); return;
      }
      const preview = JSON.stringify(item);
      const result = await api(sessionPath(this.id) + "/turns/" + encodeURIComponent(turn.id) + "/full");
      if (this.disposed || result.turn?.id !== turn.id || state.conversations.get(this.id) !== entry) return;
      const current = entry.detail.thread.turns.find(t => t.id === turn.id);
      const index = current?.items.findIndex(i => i.id === item.id);
      const full = result.turn?.items?.find(i => i.id === item.id);
      if (!full || !(index >= 0) || JSON.stringify(current.items[index]) !== preview) return;
      (entry.expandedOutput ||= new Map()).set(JSON.stringify([turn.id, item.id]), {preview, item: full});
      entry.nodes = null; entry.historyLoaded = true;
      entry.expandedBytes = (entry.expandedBytes || 0) + (result.snapshotBytes || 0);
      entry.retainedBytes = (entry.retainedBytes || 0) + (result.snapshotBytes || 0);
      current.items[index] = full; this.view.timelineScroll.hold(); this.render();
    }, button);
    return button;
  }
  focusMessage(id) {
    const element = this.timeline.querySelector("#agent-message-" + CSS.escape(String(id)));
    if (!element) {
      if (this.view.messageTarget?.id === id) return;
      const entry = conversationEntry(this.id);
      this.view.messageTarget = entry.pendingMessageTarget = {thread: this.id, id};
      this.visual.allTurns = true;
      this.refresh().catch(showError).finally(() => {
        if (entry.pendingMessageTarget?.id === id) delete entry.pendingMessageTarget;
      });
      return;
    }
    this.view.messageTarget = null; element.scrollIntoView({block: "center"}); element.focus({preventScroll: true});
  }
  focus() { (this.composer.hidden ? this.timeline : this.input).focus({preventScroll: true}); }
  setActive(active) { this.active = active; this.markSeen(); }
  canTransfer() { return !this.workspace.attachments.pending(this.id); }
  setTransferLock(locked) { this.input.disabled = locked; }
  dispose() {
    this.disposed = true; clearTimeout(this.refreshTimer); cancelAnimationFrame(this.paint);
    this.view.responseActions.dispose();
    this.view.timelineScroll.resize.disconnect(); this.visual.viewport?.dispose();
    globalThis.OpenUIThread?.releaseContainer?.(this.timeline);
    this.host.remove();
  }
}
