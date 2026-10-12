(function (root) {
  "use strict";
  const routed = new Set(["queued", "sending", "accepted"]);
  function describe(thread, handoff) {
    const state = thread.response_state, checkpoint = !thread.checkpoint_stale && thread.checkpoint;
    const completion = thread.turn_completion;
    let kind, label;
    if (["input", "action", "failed", "interrupted", "reply"].includes(state)) {
      kind = "needs";
      label = ({input: "Needs your answer", action: "Needs your review", failed: "Failed", interrupted: "Interrupted", reply: "Response to check"})[state];
    } else if (state === "working" || thread.status === "running") { kind = "running"; label = "Working"; }
    else if (handoff && routed.has(handoff.state) &&
        (handoff.state !== "accepted" || !completion || completion.completed_at < handoff.createdAt)) {
      kind = "running"; label = handoff.state === "queued" ? "Queued" : "Starting";
    } else if (completion || ["available", "update"].includes(state)) {
      kind = "results";
      label = ({findings: "Findings", done: "Completed", update: "Update"})[thread.response_classification?.choice] || "Result";
    } else { kind = "running"; label = "Waiting"; }
    return {thread, kind, label, unread: !!thread.unread_result,
      summary: kind === "needs" && checkpoint?.next_actor === "user" && checkpoint.next_action
        ? checkpoint.next_action : checkpoint?.summary || (kind === "running" ? "Waiting for the next update." : "Open the conversation to read the response."),
      time: completion?.completed_at || thread.last_work_at || 0};
  }
  class Model {
    constructor(saved = {}) {
      const strings = value => Array.isArray(value) ? value.filter(v => typeof v === "string") : [];
      this.ids = new Set(strings(saved.ids)); this.seen = new Set(strings(saved.seen));
      this.visible = strings(saved.visible); this.manual = new Set(strings(saved.manual));
      this.dismissed = new Set(strings(saved.dismissed)); this.pending = new Set(strings(saved.pending));
      this.items = []; this.handoffs = new Map(); this.receipts = [];
    }
    update(threads, entries, {capacity = 1, protectedIds = [], autoOpen = true} = {}) {
      for (const entry of entries) {
        if (!entry.target || !routed.has(entry.state)) continue;
        this.ids.add(entry.target); this.handoffs.set(entry.target, entry);
        const key = entry.key + ":" + entry.target;
        if (!this.seen.has(key)) {
          this.seen.add(key); this.pending.add(entry.target); this.dismissed.delete(entry.target);
        }
      }
      this.receipts = entries.filter(e => ["pending", "routing", "creating", "failed", "uncertain", "paused"].includes(e.state));
      const available = new Map(threads.filter(t => this.ids.has(t.thread_id) && t.attention !== "archived").map(t => [t.thread_id, t]));
      this.items = [...available.values()].map(t => describe(t, this.handoffs.get(t.thread_id)));
      const byId = new Map(this.items.map(item => [item.thread.thread_id, item]));
      // While a conversation is being read or edited, background activity must
      // not rearrange its neighbours. New work is still available in the rail.
      if (!protectedIds.length) {
        this.visible = this.visible.filter(id => byId.has(id) && (byId.get(id).kind !== "results" || this.manual.has(id)));
        this.visible = this.visible.slice(0, capacity);
        for (const id of this.pending) {
          const item = byId.get(id);
          if (!item) continue; // Receipt can precede the next inventory snapshot.
          if (autoOpen && item.kind !== "results" && !this.dismissed.has(id) && !this.visible.includes(id) && this.visible.length < capacity) this.visible.push(id);
          this.pending.delete(id);
        }
      }
      return this.items;
    }
    open(id, capacity = 1) {
      this.ids.add(id); this.dismissed.delete(id); this.manual.add(id); this.pending.delete(id);
      if (!this.visible.includes(id)) {
        if (this.visible.length >= capacity) this.visible.shift();
        this.visible.push(id);
      }
    }
    remove(id) {
      this.visible = this.visible.filter(value => value !== id);
      this.manual.delete(id); this.pending.delete(id); this.dismissed.add(id);
    }
    snapshot() { return {ids: [...this.ids], seen: [...this.seen], visible: this.visible,
      manual: [...this.manual], dismissed: [...this.dismissed], pending: [...this.pending]}; }
  }
  class Workspace {
    constructor({document, view, storage, panes, fetchThread, onError, openDispatcher, dismissThread}) {
      Object.assign(this, {document, view, storage, panes, fetchThread, onError, openDispatcher, dismissThread});
      this.$ = id => document.getElementById(id); this.models = new Map(); this.threads = [];
      this.entries = []; this.active = false; this.resultLimit = 8;
      this.dismissing = new Set();
      panes.onResize = () => this.sync();
      this.$("dispatcher-options-toggle").onclick = () => this.options(!this.optionsOpen);
      this.$("dispatcher-projects-toggle").onclick = () => this.projects(!this.projectsOpen);
      this.$("dispatcher-mobile-conversation").onclick = () => this.$("dispatcher-expand").click();
      document.addEventListener("keydown", event => {
        if (!this.active || event.key !== "Escape" || event.defaultPrevented ||
            !this.$("dispatcher-options-toggle").offsetHeight || document.querySelector("dialog[open]")) return;
        if (this.optionsOpen && this.$("composer").contains(document.activeElement)) {
          this.options(false); this.$("dispatcher-options-toggle").focus(); event.preventDefault();
        } else if (this.projectsOpen) {
          this.projects(false); this.$("dispatcher-projects-toggle").focus(); event.preventDefault();
        }
      });
      view.addEventListener("resize", () => this.sync());
      document.addEventListener("focusin", () => {
        if (this.active) view.setTimeout(() => this.sync(), 0);
      });
    }
    options(value) {
      this.optionsOpen = !!value;
      this.$("composer").classList.toggle("dispatcher-options-open", this.optionsOpen);
      this.$("dispatcher-options-toggle").setAttribute("aria-expanded", String(this.optionsOpen));
    }
    projects(value) {
      this.projectsOpen = !!value;
      this.document.body.classList.toggle("dispatcher-projects-open", this.projectsOpen);
      this.$("dispatcher-projects-toggle").setAttribute("aria-expanded", String(this.projectsOpen));
    }
    get mobile() { return this.view.matchMedia("(max-width: 800px)").matches; }
    get capacity() {
      if (this.mobile) return 1;
      const rect = this.panes.container.getBoundingClientRect();
      // Keep overflow in the rail instead of squeezing more conversations in.
      return Math.max(1, Math.min(3, Math.floor(rect.width / 360))) * Math.max(1, Math.min(2, Math.floor(rect.height / 300)));
    }
    show(id, cwd) {
      this.options(false); this.projects(false);
      this.active = true; this.id = id; this.cwd = cwd; this.entries = []; this.signature = null;
      this.activation = {}; this.receiptsReady = false;
      this.restored = new Set(); this.geometry = null;
      this.key = "ribbon-field.dispatcher-workspace:" + id;
      if (!this.models.has(id)) {
        let saved; try { saved = JSON.parse(this.storage?.getItem(this.key)); } catch { /* Optional view state. */ }
        this.models.set(id, new Model(saved || {}));
      }
      this.model = this.models.get(id);
      this.document.body.classList.add("dispatcher-workspace");
      this.$("dispatcher-workspace").hidden = false; this.$("dispatcher-empty").hidden = false;
      this.panes.container.hidden = false;
      this.panes.workspaceClose = id => this.remove(id);
      this.sync();
    }
    hide() {
      if (!this.active) return;
      this.options(false); this.projects(false);
      this.document.body.classList.remove("dispatcher-has-activity");
      this.save(); this.active = false; this.panes.workspaceClose = null;
      this.document.body.classList.remove("dispatcher-workspace");
      this.$("dispatcher-workspace").hidden = true; this.$("dispatcher-empty").hidden = true;
    }
    save() {
      try { this.storage?.setItem(this.key, JSON.stringify(this.model.snapshot())); } catch { /* Storage is optional. */ }
    }
    update(threads, detail) {
      this.threads = threads || this.threads;
      if (this.id && detail?.work_thread?.thread_id === this.id) {
        this.entries = root.DispatchUI.handoffs(detail); this.receiptsReady = true;
      }
      this.sync();
    }
    protectedIds() {
      return this.model.visible.filter(id => {
        const record = this.panes.records.get(id), draft = record?.api?.exportDraft();
        return this.document.activeElement === record?.frame || draft?.text?.trim() || draft?.images?.length ||
          record?.frame?.contentDocument?.querySelector("dialog[open]");
      });
    }
    sync() {
      if (!this.active || this.syncing) return;
      this.syncing = true;
      try {
        const threads = this.threads.filter(t => (t.project_cwd || t.cwd) === this.cwd && t.thread_id !== this.id);
        const rect = this.panes.container.getBoundingClientRect();
        const geometry = this.mobile ? "mobile" : [Math.max(1, Math.min(3, Math.floor(rect.width / 360))), Math.max(1, Math.min(2, Math.floor(rect.height / 300)))].join(":");
        const resized = this.geometry !== geometry, protectedIds = this.protectedIds();
        if (resized && this.model.visible.length > this.capacity) {
          const selected = this.panes.selected;
          const kept = this.model.visible.slice(0, this.capacity);
          if (selected && this.model.visible.includes(selected) && !kept.includes(selected)) kept[kept.length - 1] = selected;
          this.model.visible = kept;
        }
        this.geometry = geometry;
        // Retained tiles can have a new queued assignment and an older result.
        // Wait for dispatcher receipts before deciding whether to retire them.
        const items = this.model.update(threads, this.entries, {capacity: this.capacity, autoOpen: !this.mobile,
          protectedIds: this.receiptsReady ? protectedIds : ["loading"]});
        const visible = this.model.visible.map(id => threads.find(t => t.thread_id === id)).filter(Boolean);
        const ids = visible.map(t => t.thread_id), current = this.panes.viewId === "dispatcher:" + this.id ? this.panes.layout?.order || [] : [];
        if (resized || this.panes.viewId !== "dispatcher:" + this.id || JSON.stringify(ids) !== JSON.stringify(current) || !this.panes.active) {
          if (visible.length) {
            this.panes.container.hidden = false;
            this.panes.tile("dispatcher:" + this.id, visible);
          } else {
            this.panes.viewId = "dispatcher:" + this.id;
            this.panes.layouts.set(this.panes.viewId, {order: [], columns: [], groups: [], tabs: [], selected: null, maximized: null});
            this.panes.active = true; this.panes.render(); this.panes.onFocus?.(null);
          }
        }
        this.document.body.classList.toggle("panes-visible", !!visible.length);
        this.$("dispatcher-empty").hidden = !!visible.length;
        // A hidden empty canvas still needs a width for capacity calculation.
        this.panes.container.hidden = false;
        for (const id of ids) {
          if (this.restored.has(id)) continue;
          const record = this.panes.records.get(id), draft = this.panes.getDraft(id);
          if (record.api) { draft.scroll = record.api.exportDraft().scroll; record.api.restoreDraft(draft); }
          else record.initialDraft = draft;
          this.restored.add(id);
        }
        for (const item of items) {
          const record = this.panes.records.get(item.thread.thread_id);
          if (record?.frame) this.panes.changed(record.frame.contentWindow, item.thread);
        }
        this.document.body.classList.toggle("dispatcher-has-activity", !!items.length || !!this.model.receipts.length);
        this.render(items); this.save();
      } finally { this.syncing = false; }
    }
    async open(id) {
      const dispatcher = this.id, activation = this.activation;
      try {
        let thread = this.threads.find(t => t.thread_id === id);
        if (!thread) thread = await this.fetchThread(id);
        if (!this.active || dispatcher !== this.id || activation !== this.activation) return;
        if (thread.dispatcher && id === this.id) { this.openDispatcher(); return; }
        if (thread.attention === "archived") throw new Error("This conversation is closed. Reopen it from the overview or send a new request through the dispatcher.");
        if ((thread.project_cwd || thread.cwd) !== this.cwd) throw new Error("Open this conversation from its project overview.");
        this.threads = [...this.threads.filter(t => t.thread_id !== id), thread];
        this.model.open(id, this.capacity); this.sync(); this.panes.focus(id);
      } catch (error) { this.onError(error); }
    }
    remove(id) {
      this.model.remove(id); this.sync();
      if (!this.model.visible.length) {
        const target = this.mobile && this.$("dispatcher-workspace").querySelector('[data-dispatch-thread="' + root.CSS.escape(id) + '"]');
        (target || this.$("message")).focus({preventScroll: true});
      }
    }
    async dismiss(thread) {
      const id = thread.thread_id;
      if (!thread.can_handle_response || !this.dismissThread || this.dismissing.has(id)) return;
      this.dismissing.add(id); this.sync();
      try { await this.dismissThread(thread); }
      catch (error) { this.onError(error); }
      finally { this.dismissing.delete(id); this.sync(); }
    }
    render(items) {
      const signature = JSON.stringify([items, this.model.receipts, this.model.visible, this.resultLimit, [...this.dismissing]]);
      if (signature === this.signature) return;
      this.signature = signature;
      const focused = this.document.activeElement?.dataset?.dispatchThread;
      const focusedAction = this.document.activeElement?.dataset?.dispatchAction;
      for (const [kind, sectionId] of [["needs", "dispatcher-attention"], ["running", "dispatcher-running"], ["results", "dispatcher-results"]]) {
        const section = this.$(sectionId), list = section.querySelector(".dispatch-items");
        const entries = items.filter(item => item.kind === kind).sort((a, b) => Number(b.unread) - Number(a.unread) || b.time - a.time);
        const receipts = kind === "needs" ? this.model.receipts.filter(e => ["failed", "uncertain", "paused"].includes(e.state))
          : kind === "running" ? this.model.receipts.filter(e => ["pending", "routing", "creating"].includes(e.state)) : [];
        section.hidden = !entries.length && !receipts.length;
        section.querySelector("small").textContent = String(entries.length + receipts.length);
        list.replaceChildren();
        const displayed = kind === "results" ? entries.slice(0, entries.filter(item => item.unread).length + this.resultLimit) : entries;
        for (const item of displayed) {
          const button = this.document.createElement("button"); button.type = "button";
          button.className = "dispatch-work-item" + (item.unread ? " is-unread" : "");
          button.dataset.kind = kind; button.dataset.dispatchThread = item.thread.thread_id;
          button.setAttribute("aria-current", String(this.model.visible.includes(item.thread.thread_id)));
          button.onclick = () => this.open(item.thread.thread_id);
          for (const [text, cls] of [[item.thread.title, "title"], [item.label + (item.unread ? " · Unread" : ""), "state"], [item.summary, "summary"]]) {
            const span = this.document.createElement("span"); span.className = "dispatch-item-" + cls; span.textContent = text; button.append(span);
          }
          const card = this.document.createElement("div"); card.className = "dispatch-work-card";
          card.append(button);
          if (item.thread.can_handle_response && this.dismissThread) {
            const dismiss = this.document.createElement("button"); dismiss.type = "button";
            dismiss.className = "quiet dispatch-dismiss"; dismiss.textContent = "Dismiss";
            dismiss.dataset.dispatchThread = item.thread.thread_id; dismiss.dataset.dispatchAction = "dismiss";
            dismiss.setAttribute("aria-label", "Dismiss · " + item.thread.title);
            dismiss.disabled = this.dismissing.has(item.thread.thread_id);
            dismiss.onclick = () => this.dismiss(item.thread);
            card.append(dismiss);
          }
          list.append(card);
        }
        if (displayed.length < entries.length) {
          const more = this.document.createElement("button"); more.type = "button"; more.className = "quiet";
          more.textContent = "Earlier results (" + (entries.length - displayed.length) + ")";
          more.onclick = () => { this.resultLimit += 8; this.render(items); }; list.append(more);
        }
        for (const entry of receipts) {
          const button = this.document.createElement("button"); button.type = "button"; button.className = "dispatch-work-item";
          button.dataset.kind = kind;
          button.textContent = ({failed: "Routing failed", uncertain: "Delivery needs review", paused: "Delivery paused", creating: "Creating a conversation…"})[entry.state] || "Finding a conversation…";
          button.onclick = () => this.openDispatcher(); list.append(button);
        }
      }
      this.$("dispatcher-rail-empty").hidden = !!items.length || !!this.model.receipts.length;
      if (focused) {
        const selector = '[data-dispatch-thread="' + root.CSS.escape(focused) + '"]';
        const action = focusedAction && this.$("dispatcher-workspace").querySelector(selector + '[data-dispatch-action="dismiss"]');
        (action && !action.disabled ? action : this.$("dispatcher-workspace").querySelector(selector))?.focus({preventScroll: true});
      }
    }
  }
  root.DispatcherWorkspace = {Model, Workspace, describe};
  if (typeof module !== "undefined") module.exports = root.DispatcherWorkspace;
})(globalThis);
