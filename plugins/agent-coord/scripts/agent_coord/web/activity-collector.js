/* One recorder per top-level window. Pane documents borrow its submission context. */
(function (root) {
  "use strict";
  // Where a deliberate navigation began, matched from the most specific container.
  const SURFACES = [
    ["#thread-hover", "hover-card"], [".asks-dialog", "decisions"], ["#roll-up-bar", "roll-up"],
    ["#sessions, #sidebar", "sidebar"], ["#conversation-results", "search"],
    ["#working-set-overview", "working-set"], ["#atlas-overview", "atlas"], ["#overview", "cards"],
    ["#conversation-tabs", "tabs"], ["#dispatcher-workspace, #dispatcher-dock, #dispatcher-empty, #send-work-dock", "dispatcher"],
    ["#agent-tree-panel", "agent-tree"], ["#thread-panes", "panes"], ["#conversation", "conversation"],
    ["#project-navigation, .views-bar", "projects"],
  ];
  function surfaceOf(event) {
    if (event.type === "keydown" && (event.metaKey || event.ctrlKey || event.altKey)) return "shortcut";
    for (const [selector, surface] of SURFACES) if (event.target?.closest?.(selector)) return surface;
    return "other";
  }
  class VisitRecorder {
    constructor({windowId, send, now = () => Date.now() / 1000, uuid = () => root.crypto.randomUUID(), limit = 80}) {
      Object.assign(this, {windowId, send, now, uuid, limit});
      this.enabled = false; this.foreground = false; this.thread = null;
      this.current = null; this.queue = []; this.pending = null; this.intent = 0;
      this.intentSurface = null; this.navigationSurface = null;
    }
    markIntent(surface = null) { this.intent = this.now() + 1; this.intentSurface = surface; }
    source() {
      const deliberate = this.intent > this.now(); this.intent = 0;
      // Each deliberate selection replaces the surface its visit will report.
      if (deliberate) this.navigationSurface = this.intentSurface;
      return deliberate ? "navigation" : "programmatic";
    }
    select(thread, source = "programmatic") {
      thread = thread && thread !== "new-session" ? thread : null;
      if (thread !== this.thread) this.end();
      this.thread = thread;
      this.start(source);
    }
    interact(thread, source = "focus") { this.select(thread, source); }
    setForeground(value) {
      this.foreground = !!value;
      if (!this.foreground) this.end();
    }
    setEnabled(value) {
      const previous = this.enabled;
      if (!value) this.end();
      this.enabled = !!value;
      if (this.enabled && !previous) this.start("programmatic");
    }
    start(source) {
      if (!this.enabled || !this.foreground || !this.thread) return;
      if (this.current) {
        const deliberate = source === "navigation" || source === "focus";
        const observing = this.current.source === "restore" || this.current.source === "programmatic";
        if (!deliberate || !observing) return;
        this.end();
      }
      this.current = {visit_id: this.uuid(), thread_id: this.thread, source};
      if (source === "navigation") {
        if (this.navigationSurface) this.current.surface = this.navigationSurface;
        this.navigationSurface = null;
      }
      this.enqueue("visit_started", this.current);
    }
    end() {
      if (!this.current) return;
      this.enqueue("visit_ended", this.current); this.current = null;
    }
    enqueue(kind, visit) {
      this.queue.push({...visit, kind, window_id: this.windowId, event_id: this.uuid(), occurred_at: this.now()});
      if (this.queue.length > this.limit) this.queue.splice(0, this.queue.length - this.limit);
    }
    submission(thread, body) {
      // The same body keeps the same event ID through transport retries.
      const deliberate = this.current?.source === "navigation" || this.current?.source === "focus";
      body.activity ||= {event_id: this.uuid(), visit_id: deliberate && this.current?.thread_id === thread ? this.current.visit_id : null,
        window_id: this.windowId};
      return body;
    }
    clear() { this.queue = []; this.current = null; this.intentSurface = this.navigationSurface = null; }
    async flush({keepalive = false} = {}) {
      if (this.pending || !this.queue.length) return this.pending;
      const batch = this.queue.slice(0, 40), ids = new Set(batch.map(event => event.event_id));
      this.pending = Promise.resolve().then(() => this.send(batch, {keepalive})).then(() => {
        this.queue = this.queue.filter(event => !ids.has(event.event_id));
      }).catch(() => { /* Bounded retry on the next tick; no content or keystrokes are retained. */ });
      try { await this.pending; } finally { this.pending = null; }
    }
  }
  function bind({window: view, document: doc, state, api}) {
    if (state.paneMode) return view.parent.agentCoordActivity;
    const recorder = new VisitRecorder({windowId: state.sourceWindowId,
      send: (events, options) => api("activity/events", {events}, options)});
    view.agentCoordActivity = recorder;
    const foreground = () => !doc.hidden && doc.hasFocus() && !doc.querySelector("dialog[open]");
    const selected = () => view.agentCoordPanes?.active ? view.agentCoordPanes.selected : state.selected;
    let wasAway = false, stopped = false;
    recorder.setForeground(foreground());
    const interact = event => {
      if (!event.isTrusted) return;
      recorder.markIntent(surfaceOf(event));
      // Let navigation handlers choose their destination before observing it.
      const inConversation = !!event.target.closest?.("#conversation, #thread-panes");
      const inSingleConversation = !!event.target.closest?.("#conversation");
      queueMicrotask(() => {
        recorder.setForeground(foreground());
        if (inConversation && !(inSingleConversation && view.agentCoordPanes?.active)) recorder.interact(selected(), "focus");
      });
    };
    doc.addEventListener("pointerdown", interact, true);
    doc.addEventListener("keydown", interact, true);
    recorder.windowBlurred = () => view.setTimeout(() => {
      // Focusing a child iframe blurs window, but the top document still has focus.
      if (doc.hidden || !doc.hasFocus()) wasAway = true;
      if (!foreground()) { recorder.setForeground(false); recorder.flush(); }
    }, 0);
    recorder.windowFocused = () => {
      recorder.setForeground(foreground());
      if (wasAway && foreground()) { wasAway = false; recorder.interact(selected(), "focus"); }
      else recorder.select(selected(), "restore");
    };
    view.addEventListener("blur", recorder.windowBlurred);
    view.addEventListener("focus", recorder.windowFocused);
    doc.addEventListener("visibilitychange", () => {
      if (doc.hidden) { wasAway = true; recorder.setForeground(false); recorder.flush(); }
      else {
        recorder.setForeground(foreground());
        if (wasAway && foreground()) { wasAway = false; recorder.interact(selected(), "focus"); }
        else recorder.select(selected(), "restore");
      }
    });
    const refreshSettings = () => api("activity/settings").then(settings => recorder.setEnabled(settings.enabled)).catch(() => {});
    refreshSettings();
    let ticks = 0;
    const timer = view.setInterval(() => {
      recorder.setForeground(foreground());
      recorder.flush();
      if (++ticks % 15 === 0) refreshSettings();
    }, 2000);
    const channel = typeof view.BroadcastChannel === "function" ? new view.BroadcastChannel("ribbon-field-activity") : null;
    if (channel) channel.onmessage = ({data}) => {
      if (data?.type === "clear") recorder.clear();
      if (typeof data?.enabled === "boolean") recorder.setEnabled(data.enabled);
    };
    view.addEventListener("pagehide", () => {
      recorder.end(); recorder.flush({keepalive: true});
      view.clearInterval(timer); channel?.close(); stopped = true;
    });
    view.addEventListener("pageshow", event => { if (event.persisted && stopped) view.location.reload(); });
    return recorder;
  }
  const exports = {VisitRecorder, bind, surfaceOf, SURFACES};
  if (typeof module !== "undefined" && module.exports) module.exports = exports;
  else root.ActivityCollector = exports;
})(typeof globalThis !== "undefined" ? globalThis : this);
