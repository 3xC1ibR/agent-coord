/* A window-local attention pass. Polling updates the queue, never navigation. */
(function (root) {
  "use strict";
  const eligible = thread => thread.attention === "now" && thread.needs_attention;
  const token = thread => thread.attention_key || JSON.stringify([
    thread.turn_key, thread.turn_completion?.id, thread.checkpoint?.id, thread.response_state,
  ]);
  class RollUp {
    constructor({load, open, selected, changed = () => {}}) {
      Object.assign(this, {load, open, selected, changed});
      this.active = false; this.busy = false; this.epoch = 0;
      this.queue = []; this.skipped = new Set(); this.answered = new Map();
      this.current = null; this.done = false;
    }
    sync(threads) {
      if (!this.active) return;
      const candidates = new Map(threads.filter(thread => eligible(thread) && !this.skipped.has(thread.thread_id) &&
        this.answered.get(thread.thread_id) !== token(thread)).map(thread => [thread.thread_id, thread]));
      const retained = new Set();
      this.queue = this.queue.filter(thread => candidates.has(thread.thread_id) &&
        token(candidates.get(thread.thread_id)) === token(thread)).map(thread => {
        retained.add(thread.thread_id); return candidates.get(thread.thread_id);
      });
      const added = [...candidates.values()].filter(thread => !retained.has(thread.thread_id));
      added.sort((a, b) => (a.attention_since ?? a.turn_completion?.completed_at ?? a.checkpoint?.created_at ?? Infinity) -
        (b.attention_since ?? b.turn_completion?.completed_at ?? b.checkpoint?.created_at ?? Infinity) ||
        a.thread_id.localeCompare(b.thread_id));
      this.queue.push(...added);
      this.changed();
    }
    ticket(id = this.selected()) {
      return this.active && this.current?.thread_id === id ? {id, token: token(this.current), epoch: this.epoch} : null;
    }
    stop(done = false) {
      if (!this.active && !this.busy && !this.done && !done) return;
      this.epoch++; this.active = false; this.busy = false; this.done = done;
      this.current = null; this.queue = []; this.changed();
    }
    async start() {
      if (this.active || this.busy) return;
      this.epoch++; this.active = true; this.done = false;
      this.queue = []; this.skipped.clear(); this.answered.clear();
      await this.advance();
    }
    async advance() {
      if (!this.active || this.busy) return;
      const epoch = this.epoch;
      this.busy = true; this.changed();
      try {
        const threads = await this.load();
        if (!this.active || epoch !== this.epoch) return;
        this.sync(threads);
        const next = this.queue[0];
        if (!next) { this.stop(true); return; }
        this.current = next;
        await this.open(next.thread_id);
      } finally {
        if (epoch === this.epoch) { this.busy = false; this.changed(); }
      }
    }
    async skip() {
      if (!this.active || this.busy) return;
      if (this.current) this.skipped.add(this.current.thread_id);
      await this.advance();
    }
    async responded(ticket, {pending = false} = {}) {
      if (!ticket || !this.active || this.busy || ticket.epoch !== this.epoch ||
          ticket.id !== this.selected() || ticket.id !== this.current?.thread_id || pending) return;
      this.answered.set(ticket.id, ticket.token);
      // A fast new response belongs at the end, behind the rest of this pass.
      this.queue = this.queue.filter(thread => thread.thread_id !== ticket.id);
      await this.advance();
    }
  }
  function setupRollUp({document, state, api, select, action, markHandled, panes = () => root.agentCoordPanes}) {
    const $ = id => document.getElementById(id);
    const roll = new RollUp({
      load: async () => (await api("threads?archived=false")).data,
      selected: () => state.selected,
      open: async id => {
        panes()?.leave();
        await select(id, {rollUp: true});
        if (state.selected !== id || !roll.active) return;
        const request = $("requests").querySelector("input, select, textarea, button:not(:disabled)");
        if (request) request.focus();
        else if (!$("composer").hidden && !$("message").disabled) $("message").focus();
        else $("roll-up-skip").focus();
      },
      changed: () => {
        if ($("roll-up-start").disabled !== !!state.busy) $("roll-up-start").disabled = !!state.busy;
        if (!roll.active && !roll.done && $("roll-up-bar").hidden) return;
        $("roll-up-bar").hidden = !roll.active && !roll.done;
        $("roll-up-label").textContent = roll.active ? "Roll up · " + roll.queue.length + " waiting" :
          roll.skipped.size ? "Pass complete · " + roll.skipped.size + " skipped" : "All caught up";
        $("roll-up-start").setAttribute("aria-pressed", String(roll.active));
        $("roll-up-skip").hidden = !roll.active;
        $("roll-up-skip").disabled = roll.busy || !!state.busy;
        $("roll-up-handle").hidden = !roll.active || !state.detail?.work_thread?.can_handle_response;
        $("roll-up-handle").disabled = roll.busy || !!state.busy || state.updatingThreads.has(state.selected);
        $("roll-up-exit").textContent = roll.active ? "Exit" : "Dismiss";
      },
    });
    state.rollUp = roll;
    const toggle = () => {
      if (document.querySelector("dialog[open]") || panes()?.focusedRecord?.frame?.contentDocument?.querySelector("dialog[open]") || state.busy) return;
      if (roll.active) roll.stop();
      else action(() => roll.start());
    };
    $("roll-up-start").onclick = toggle;
    $("roll-up-exit").onclick = () => roll.stop();
    $("roll-up-skip").onclick = () => action(() => roll.skip());
    $("roll-up-handle").onclick = () => action(() => markHandled(state.detail.work_thread));
    document.addEventListener("keydown", event => {
      if (event.defaultPrevented || event.isComposing || document.querySelector("dialog[open]")) return;
      if (event.altKey && (event.metaKey || event.ctrlKey) && !event.shiftKey &&
          (event.code === "KeyR" || event.key.toLowerCase() === "r")) {
        event.preventDefault(); toggle();
      } else if (event.key === "Escape" && (roll.active || roll.done)) {
        if (document.querySelector(".filter-menu[open], .view-menu[open]")) return;
        event.preventDefault(); roll.stop();
      }
    });
    return roll;
  }
  if (typeof module !== "undefined" && module.exports) module.exports = {RollUp, setupRollUp};
  root.RollUp = RollUp;
  root.setupRollUp = setupRollUp;
})(typeof globalThis !== "undefined" ? globalThis : this);
