"use strict";

// The shell owns layout and navigation. Each conversation keeps its own document,
// draft, scroll position and approval UI, while sharing the shell's event stream.
const paneLayout = (() => {
  function plan(count, width, height) {
    if (!count) return [];
    const columns = Math.max(1, Math.min(3, Math.floor(width / 360)));
    const rows = Math.max(1, Math.min(2, Math.floor(height / 300)));
    const slots = Math.min(count, columns * rows, 6);
    if (slots === 2 && columns >= 2) return [1, 1];
    let best = null;
    for (let cols = 1; cols <= Math.min(columns, slots); cols++) {
      if (Math.ceil(slots / cols) > rows) continue;
      const sizes = Array.from({length: cols}, (_, i) => Math.floor(slots / cols) + (i >= cols - slots % cols ? 1 : 0));
      const score = sizes.reduce((sum, n) => sum + n * Math.abs(Math.log((width / cols) / (height / n) / 1.35)), 0);
      if (!best || score < best.score) best = {sizes, score};
    }
    return best?.sizes || [1];
  }
  function arrange(ids, width, height) {
    const unique = [...new Set(ids)];
    const columns = plan(unique.length, width, height);
    const groups = Array.from({length: columns.reduce((a, b) => a + b, 0)}, () => []);
    unique.forEach((id, i) => groups[i % groups.length].push(id));
    return {columns, groups};
  }
  function shortcut(event) {
    if (event.isComposing || event.shiftKey || !event.ctrlKey || !event.altKey) return null;
    const key = event.code || event.key;
    if (event.metaKey) return key === "KeyT" || key.toLowerCase() === "t" ? "tile" : null;
    return ({ArrowLeft: "previous", ArrowRight: "next", Enter: "maximize"})[key] || null;
  }
  return {plan, arrange, shortcut};
})();

class ThreadPanes {
  constructor({document, container, onFocus, onExit, onTile, onError, getDraft, onDraft, search = ""}) {
    Object.assign(this, {document, container, onFocus, onExit, onTile, onError, getDraft, onDraft, search});
    this.layouts = new Map(); this.records = new Map(); this.active = false; this.viewId = null;
    this.chrome = this.el("div", "pane-layout");
    this.deck = this.el("div", "pane-deck");
    container.append(this.chrome, this.deck);
    const Resize = document.defaultView?.ResizeObserver;
    if (Resize) { this.observer = new Resize(() => this.position()); this.observer.observe(container); }
    document.defaultView?.addEventListener("resize", () => this.position());
  }
  el(tag, className, text) {
    const element = this.document.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = text;
    return element;
  }
  button(text, label, fn) {
    const button = this.el("button", "quiet", text); button.type = "button";
    button.title = label; button.setAttribute("aria-label", label); button.onclick = fn;
    return button;
  }
  get layout() { return this.layouts.get(this.viewId); }
  get selected() { return this.active ? this.layout?.selected || null : null; }
  get focusedRecord() { return this.records.get(this.selected); }
  tile(viewId, threads) {
    const open = threads.filter(t => t.attention !== "archived");
    if (!open.length) return false;
    const previous = this.layouts.get(viewId);
    const available = new Map(open.map(t => [t.thread_id, t]));
    // Re-tiling keeps established order even when activity changes list sorting.
    const old = (previous?.order || []).filter(id => available.has(id));
    const ids = [...new Set([...old, ...available.keys()])];
    for (const thread of open) this.record(thread);
    this.active = true; this.viewId = viewId; this.container.hidden = false;
    const rect = this.container.getBoundingClientRect();
    const layout = paneLayout.arrange(ids, rect.width, rect.height);
    layout.order = ids;
    layout.tabs = layout.groups.map(group => previous?.tabs.find(id => group.includes(id)) || group[0]);
    layout.selected = ids.includes(previous?.selected) ? previous.selected : ids[0];
    layout.tabs[layout.groups.findIndex(group => group.includes(layout.selected))] = layout.selected;
    layout.maximized = null;
    this.layouts.set(viewId, layout); this.render();
    this.onFocus?.(layout.selected); return true;
  }
  show(viewId) {
    if (!this.layouts.get(viewId)?.groups.length) return false;
    this.viewId = viewId; this.active = true; this.container.hidden = false;
    this.render(); this.onFocus?.(this.selected); return true;
  }
  hide() {
    this.active = false; this.container.hidden = true;
    for (const record of this.records.values()) record.api?.setActive(false);
  }
  record(thread) {
    let record = this.records.get(thread.thread_id);
    if (record) { record.thread = thread; return record; }
    record = {thread, frame: null, api: null, initialDraft: this.getDraft?.(thread.thread_id)};
    this.records.set(thread.thread_id, record); return record;
  }
  ensureFrame(record) {
    if (record.frame) return;
    const frame = record.frame = this.el("iframe", "conversation-pane-frame");
    frame.title = "Conversation · " + record.thread.title;
    const query = new URLSearchParams(this.search); query.set("pane", "1");
    frame.src = "/?" + query + "#" + encodeURIComponent(record.thread.thread_id);
    frame.hidden = true;
    // Frames stay in the deck for their lifetime: moving an iframe between DOM
    // parents reloads it and loses the draft/selection/scroll state.
    this.deck.append(frame);
  }
  attach(source, api) {
    const record = [...this.records.values()].find(r => r.frame?.contentWindow === source);
    if (!record) return false;
    record.api = api;
    if (record.initialDraft) { api.restoreDraft(record.initialDraft); record.initialDraft = null; }
    api.setActive(this.active && this.selected === record.thread.thread_id && this.document.hasFocus());
    this.position(); return true;
  }
  from(source) { return [...this.records.values()].find(r => r.frame?.contentWindow === source); }
  focused(source) {
    const record = this.from(source);
    if (record && this.active && !record.frame.hidden) this.focus(record.thread.thread_id, false);
  }
  changed(source, thread) {
    const record = this.from(source);
    if (!record || thread.thread_id !== record.thread.thread_id) return;
    record.thread = thread;
    for (const button of this.chrome.querySelectorAll("[data-pane-thread]")) {
      if (button.dataset.paneThread !== thread.thread_id) continue;
      button.textContent = thread.title;
      button.title = thread.title + (thread.unread_result ? " · New response" : "");
      button.dataset.status = thread.response_state || "";
    }
  }
  receive(batch) { for (const record of this.records.values()) record.api?.receive(batch); }
  refresh() { for (const record of this.records.values()) record.api?.refresh(); }
  draft(source, draft) {
    const record = this.from(source);
    if (record) this.onDraft?.(record.thread.thread_id, draft);
  }
  exportDraft(id) { return this.records.get(id)?.api?.exportDraft(); }
  focus(id, moveFocus = true) {
    if (!this.active) return false;
    const index = this.layout.groups.findIndex(group => group.includes(id));
    if (index < 0) return false;
    const changed = this.layout.tabs[index] !== id;
    this.layout.tabs[index] = id; this.layout.selected = id;
    if (this.layout.maximized != null) this.layout.maximized = index;
    if (changed) this.render(); else this.position();
    for (const record of this.records.values()) record.api?.setActive(record.thread.thread_id === id);
    for (const pane of this.chrome.querySelectorAll(".thread-pane")) pane.classList.toggle("pane-focused", Number(pane.dataset.slot) === index);
    if (moveFocus) this.records.get(id)?.api?.focus();
    this.onFocus?.(id); return true;
  }
  open(thread) {
    if (this.focus(thread.thread_id)) return;
    this.record(thread);
    if (!this.layout.order.includes(thread.thread_id)) this.layout.order.push(thread.thread_id);
    const index = Math.max(0, this.layout.groups.findIndex(group => group.includes(this.selected)));
    this.layout.groups[index].push(thread.thread_id); this.focus(thread.thread_id);
  }
  cycle(direction) {
    if (!this.active) return;
    const ids = this.layout.tabs, index = ids.indexOf(this.selected);
    this.focus(ids[(index + direction + ids.length) % ids.length]);
  }
  maximize(index = this.layout?.groups.findIndex(group => group.includes(this.selected))) {
    if (!this.active || index < 0) return;
    this.layout.maximized = this.layout.maximized === index ? null : index;
    this.render(); this.focus(this.layout.tabs[index]);
  }
  close(index) {
    const layout = this.layout, id = layout.tabs[index];
    const group = layout.groups[index]; group.splice(group.indexOf(id), 1);
    if (group.length) layout.tabs[index] = group[0];
    else {
      let offset = 0;
      const column = layout.columns.findIndex(count => { const found = index < offset + count; offset += count; return found; });
      layout.columns[column]--; layout.columns = layout.columns.filter(Boolean);
      layout.groups.splice(index, 1); layout.tabs.splice(index, 1);
      layout.widths = null; layout.heights = null;
    }
    layout.maximized = null;
    if (!layout.groups.length) { this.hide(); this.onExit?.(); return; }
    layout.selected = layout.tabs[Math.min(index, layout.tabs.length - 1)];
    // Keep the conversation mounted so closing a pane never discards a draft.
    this.render(); this.focus(layout.selected);
  }
  keydown(event) {
    if (event.target?.closest?.("dialog[open]")) return false;
    if ((event.key === "Escape" || event.key === "Esc" || event.code === "Escape") && this.active && this.layout.maximized != null &&
        !event.target?.closest?.("#rename-form")) {
      event.preventDefault(); this.layout.maximized = null; this.render(); this.focus(this.selected); return true;
    }
    if (event.defaultPrevented) return false;
    const command = paneLayout.shortcut(event);
    if (command === "tile") { event.preventDefault(); this.onTile?.(); return true; }
    if (!this.active) return false;
    if (command === "previous" || command === "next") { event.preventDefault(); this.cycle(command === "next" ? 1 : -1); return true; }
    if (command === "maximize") {
      event.preventDefault(); this.maximize(); return true;
    }
    return false;
  }
  render() {
    const layout = this.layout;
    this.chrome.replaceChildren(); this.slots = [];
    this.chrome.classList.toggle("pane-maximized", layout.maximized != null);
    let index = 0;
    layout.columns.forEach((count, column) => {
      if (column) this.chrome.append(this.splitter(this.chrome, column, "horizontal"));
      const col = this.el("div", "pane-column");
      col.style.flexGrow = String(layout.widths?.[column] || 1);
      this.chrome.append(col);
      for (let row = 0; row < count; row++, index++) {
        const slot = index;
        if (row) col.append(this.splitter(col, row, "vertical", column));
        const pane = this.el("section", "thread-pane"); pane.dataset.slot = String(slot);
        pane.style.flexGrow = String(layout.heights?.[column]?.[row] || 1);
        pane.hidden = layout.maximized != null && layout.maximized !== slot;
        if (!pane.hidden && layout.maximized != null) col.classList.add("maximized-column");
        pane.setAttribute("aria-label", "Conversation pane " + (slot + 1));
        const header = this.el("div", "pane-header"), tabs = this.el("div", "pane-tabs");
        tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "Threads in pane " + (slot + 1));
        for (const id of layout.groups[slot]) {
          const thread = this.records.get(id).thread;
          const tab = this.button(thread.title, thread.title, () => this.focus(id));
          tab.dataset.paneThread = id; tab.dataset.status = thread.response_state || "";
          tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", String(layout.tabs[slot] === id));
          tab.tabIndex = layout.tabs[slot] === id ? 0 : -1;
          tab.onkeydown = event => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || event.ctrlKey || event.altKey || event.metaKey) return;
            event.preventDefault(); const group = layout.groups[slot], current = group.indexOf(id);
            const next = event.key === "Home" ? 0 : event.key === "End" ? group.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + group.length) % group.length;
            this.focus(group[next], false);
            this.chrome.querySelector('[data-pane-thread="' + CSS.escape(group[next]) + '"]')?.focus();
          };
          tabs.append(tab);
        }
        header.append(tabs, this.button(layout.maximized === slot ? "↙" : "⤢", layout.maximized === slot ? "Restore panes" : "Maximize pane (Ctrl+Alt+Enter)", () => this.maximize(slot)),
          this.button("×", "Close pane — keep thread and draft", () => this.close(slot)));
        const body = this.el("div", "pane-content");
        pane.append(header, body); col.append(pane); this.slots.push({pane, body, id: layout.tabs[slot]});
      }
    });
    this.position();
  }
  position() {
    if (!this.active || !this.slots) return;
    const origin = this.container.getBoundingClientRect(), visible = new Set();
    for (const {pane, body, id} of this.slots) {
      if (pane.hidden) continue;
      const record = this.records.get(id); this.ensureFrame(record);
      const rect = body.getBoundingClientRect(); visible.add(id);
      Object.assign(record.frame.style, {left: rect.left - origin.left + "px", top: rect.top - origin.top + "px", width: rect.width + "px", height: rect.height + "px"});
      pane.classList.toggle("pane-focused", id === this.selected);
    }
    for (const [id, record] of this.records) {
      if (record.frame) record.frame.hidden = !visible.has(id);
      record.api?.setActive(visible.has(id) && id === this.selected && this.document.hasFocus());
    }
  }
  splitter(parent, index, direction, column) {
    const horizontal = direction === "horizontal";
    const divider = this.el("div", "pane-divider " + direction);
    divider.tabIndex = 0; divider.setAttribute("role", "separator");
    divider.setAttribute("aria-orientation", horizontal ? "vertical" : "horizontal");
    divider.setAttribute("aria-label", horizontal ? "Resize pane columns" : "Resize pane rows");
    divider.setAttribute("aria-valuemin", "15"); divider.setAttribute("aria-valuemax", "85"); divider.setAttribute("aria-valuenow", "50");
    const resize = ratio => {
      const siblings = [...parent.children].filter(el => !el.classList.contains("pane-divider"));
      const a = siblings[index - 1], b = siblings[index]; if (!a || !b) return;
      const sum = Number(a.style.flexGrow) + Number(b.style.flexGrow);
      ratio = Math.max(.15, Math.min(.85, ratio));
      a.style.flexGrow = String(sum * ratio); b.style.flexGrow = String(sum * (1 - ratio));
      divider.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
      const values = siblings.map(el => Number(el.style.flexGrow));
      if (horizontal) this.layout.widths = values;
      else { this.layout.heights ||= {}; this.layout.heights[column] = values; }
      this.position();
    };
    divider.onkeydown = event => {
      const keys = horizontal ? ["ArrowLeft", "ArrowRight"] : ["ArrowUp", "ArrowDown"];
      if (!keys.includes(event.key)) return;
      event.preventDefault(); resize(Number(divider.getAttribute("aria-valuenow")) / 100 + (event.key === keys[0] ? -.05 : .05));
    };
    divider.onpointerdown = event => {
      if (event.button !== 0) return;
      event.preventDefault(); divider.setPointerCapture(event.pointerId);
      const a = divider.previousElementSibling.getBoundingClientRect(), b = divider.nextElementSibling.getBoundingClientRect();
      this.container.classList.add("pane-resizing");
      divider.onpointermove = move => resize(horizontal ? (move.clientX - a.left) / (b.right - a.left) : (move.clientY - a.top) / (b.bottom - a.top));
      const end = () => { divider.onpointermove = null; this.container.classList.remove("pane-resizing"); };
      divider.onpointerup = divider.onpointercancel = divider.onlostpointercapture = end;
    };
    return divider;
  }
}

function setupPaneShell({state, document, api, select, goHome, refreshList, getView, getThreads, showError, notifications}) {
  const $ = id => document.getElementById(id), container = $("thread-panes");
  const shell = new ThreadPanes({document, container, search: location.search,
    getDraft: id => ({text: state.selected === id ? $("message").value : state.drafts.get(id) || "",
      images: state.attachments.items(id), scroll: state.selected === id ? (state.timelineScroll?.position() ?? $("timeline").scrollTop) : null}),
    onDraft: (id, draft) => {
      if (!shell.active && state.selected === id) return;
      state.drafts.set(id, draft.text); state.attachments.drafts.set(id, draft.images);
      if (shell.active && state.selected === id) $("message").value = draft.text;
    },
    onFocus: () => { notifications()?.syncFocus(); window.dispatchEvent(new Event("agent-coord-pane-state")); },
    onExit: () => goHome(), onTile: () => shell.toggleCurrent(), onError: showError});
  window.agentCoordPanes = shell;
  shell.requestList = () => refreshList().catch(showError);
  shell.openThread = async (id, {messageId} = {}) => {
    if (!shell.active) return select(id);
    try {
      shell.open(await api("threads/" + encodeURIComponent(id)));
      if (messageId != null) {
        const record = shell.records.get(id);
        if (record.api) record.api.focusMessage?.(messageId);
        else record.messageTarget = messageId;
      }
    } catch (error) { showError(error); }
  };
  const show = () => {
    document.body.classList.remove("chat-expanded"); document.body.classList.add("panes-visible");
    $("leave-tiles").hidden = false; $("tile-threads").textContent = "Retile threads";
    $("page-location").textContent = "Tiled threads";
  };
  const returnTarget = () => ({thread: state.selected, scroll: state.timelineScroll?.position() ?? $("timeline").scrollTop});
  function canChangeLayout() {
    if (document.querySelector("dialog[open]") || shell.focusedRecord?.frame?.contentDocument?.querySelector("dialog[open]")) return false;
    if (state.busy || state.attachments.pending()) { showError(new Error("Wait for the current send or image attachment to finish before tiling.")); return false; }
    return true;
  }
  function syncDrafts() {
    for (const id of shell.layout.order) {
      const record = shell.records.get(id), draft = shell.getDraft(id);
      if (record.api) draft.scroll = record.api.exportDraft().scroll;
      if (record.api) record.api.restoreDraft(draft);
      else record.initialDraft = draft;
    }
  }
  function tile() {
    if (!canChangeLayout()) return false;
    const threads = getThreads();
    if (!threads.some(t => t.attention !== "archived")) { showError(new Error("This view has no open threads to tile.")); return false; }
    const wasActive = shell.active;
    const previous = wasActive ? shell.layout.returnTarget : returnTarget();
    show(); container.hidden = false;
    shell.tile(getView(), threads);
    shell.layout.returnTarget = previous;
    if (!wasActive) syncDrafts();
    return true;
  }
  shell.tileCurrent = tile;
  shell.restoreView = () => {
    const previous = returnTarget();
    if (shell.show(getView())) { shell.layout.returnTarget = previous; show(); shell.position(); }
  };
  shell.leave = () => {
    shell.hide(); document.body.classList.remove("panes-visible");
    $("leave-tiles").hidden = true; $("tile-threads").textContent = "Tile threads";
  };
  let toggling = false;
  shell.toggleCurrent = async () => {
    if (toggling || !canChangeLayout()) return false;
    toggling = true;
    try {
      if (shell.active) {
        const previous = shell.layout.returnTarget;
        shell.leave();
        if (previous?.thread) {
          await select(previous.thread);
          if (state.selected === previous.thread) {
            if (state.timelineScroll) state.timelineScroll.restore(previous.scroll);
            else $("timeline").scrollTop = previous.scroll;
          }
        } else goHome();
      } else if (shell.layouts.get(getView())?.groups.length) {
        const previous = returnTarget();
        show(); shell.show(getView());
        shell.layout.returnTarget = previous;
        syncDrafts();
        shell.focus(shell.selected);
      } else if (!tile()) return false;
      return true;
    } catch (error) { showError(error); return false; }
    finally { toggling = false; }
  };
  $("tile-threads").onclick = tile;
  $("leave-tiles").onclick = () => goHome();
  document.addEventListener("keydown", event => {
    if (!document.querySelector("dialog[open]")) shell.keydown(event);
  });
  window.addEventListener("focus", () => shell.position());
  window.addEventListener("blur", () => {
    // Moving focus into a child document also blurs window; document.hasFocus
    // distinguishes that from leaving the application entirely.
    setTimeout(() => { if (!document.hasFocus()) for (const record of shell.records.values()) record.api?.setActive(false); }, 0);
  });
  return shell;
}

async function setupPaneConversation({state, document, api, select, refreshDetail, refreshThread, applyEvent, scheduleDetail, renderStatus, showError}) {
  const host = window.parent.agentCoordPanes;
  if (!host?.from(window)) throw new Error("Open this conversation from Tile threads in the workspace.");
  const $ = id => document.getElementById(id);
  document.body.classList.add("pane-document");
  let active = false, seen = "", restoredScroll = null;
  const exportDraft = () => ({text: $("message").value, images: state.attachments.items(), scroll: state.timelineScroll?.position() ?? $("timeline").scrollTop});
  const markSeen = async () => {
    const work = state.detail?.work_thread;
    if (!active || !document.hasFocus() || !work) return;
    const signature = JSON.stringify([work.thread_id, work.checkpoint?.id, work.turn_completion?.id]);
    if (signature === seen) return;
    seen = signature;
    try { await api("threads/" + encodeURIComponent(work.thread_id), {seen: true,
      seen_checkpoint_id: work.checkpoint?.id || 0, seen_completion_id: work.turn_completion?.id || 0});
      host.requestList();
    } catch (error) { seen = ""; showError(error); }
  };
  const pane = window.agentCoordPane = {
    exportDraft,
    focusMessage: id => window.agentCoordFocusMessage?.(id),
    saveDraft: () => {
      if (!state.selected) return;
      host.draft(window, exportDraft());
    },
    updateStatus: () => {
      if (state.detail?.work_thread) host.changed(window, state.detail.work_thread);
      window.parent.dispatchEvent(new Event("agent-coord-pane-state"));
    },
    restoreDraft: draft => {
      $("message").value = draft.text || ""; state.drafts.set(state.selected, draft.text || "");
      state.attachments.drafts.set(state.selected, draft.images || []);
      if (Number.isFinite(draft.scroll)) {
        restoredScroll = draft.scroll;
        if (state.timelineScroll) state.timelineScroll.restore(draft.scroll);
        else $("timeline").scrollTop = draft.scroll;
      }
      renderStatus(); pane.saveDraft();
    },
    setActive: value => { active = value; markSeen(); },
    focus: () => { if (!document.querySelector("dialog[open]")) ($("message").disabled || $("composer").hidden ? $("session-name") : $("message")).focus(); },
    receive: batch => {
      if (batch.reset || batch.completions?.some(event => event.thread_id === state.selected)) scheduleDetail();
      for (const event of batch.events || []) {
        if (event.method === "bridge/disconnected") { showError(new Error("Codex disconnected. Restart Ribbon Field to reopen saved sessions.")); if (state.detail && state.detail.work_thread?.client !== "claude") state.detail.running = false; renderStatus(); }
        else applyEvent(event);
      }
    },
    refresh: () => refreshThread().then(markSeen).catch(showError),
  };
  state.attachments.onChange = () => { renderStatus(); pane.saveDraft(); };
  document.addEventListener("focusin", () => host.focused(window));
  document.addEventListener("pointerdown", () => host.focused(window), true);
  document.addEventListener("input", () => pane.saveDraft());
  document.addEventListener("click", event => {
    const link = event.target.closest("a[href]");
    if (!link?.getAttribute("href")?.startsWith("agentcoord:") || event.button !== 0) return;
    event.preventDefault(); window.parent.agentCoordNavigate?.(link.getAttribute("href"));
  });
  document.addEventListener("keydown", event => {
    if (!document.querySelector("dialog[open]")) host.keydown(event);
  }, true);
  const id = decodeURIComponent(location.hash.slice(1));
  await select(id);
  // Booting background panes must not consume unread results or steal focus.
  host.attach(window, pane);
  window.parent.agentCoordDesktop?.bindPane?.(document);
  await refreshDetail();
  // The first transcript can render before the frame has its final size. Start
  // at the latest message unless this conversation already had a saved position.
  if (state.timelineScroll) {
    if (restoredScroll !== null) state.timelineScroll.restore(restoredScroll);
    else state.timelineScroll.latest();
  } else $("timeline").scrollTop = restoredScroll ?? $("timeline").scrollHeight;
  const record = host.from(window);
  if (record?.messageTarget != null) {
    pane.focusMessage(record.messageTarget);
    delete record.messageTarget;
  }
}

if (typeof module !== "undefined" && module.exports) module.exports = {paneLayout, ThreadPanes, setupPaneShell, setupPaneConversation};
