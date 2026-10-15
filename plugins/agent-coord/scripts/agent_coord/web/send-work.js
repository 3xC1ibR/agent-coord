"use strict";

class SendWorkPalette {
  constructor({document, view = window, api, workspace, threads, openThread, storage}) {
    Object.assign(this, {document, view, api, workspace, threads, openThread, storage});
    this.$ = id => document.getElementById("send-work-" + id);
    this.dialog = this.$("dialog"); this.input = this.$("input"); this.list = this.$("results");
    this.error = this.$("error"); this.hint = this.$("status"); this.notice = this.$("notice");
    this.generation = 0; this.rows = []; this.active = 0;
    this.$("open").onclick = () => this.toggle();
    this.$("close").onclick = () => this.close();
    this.$("unlock").onclick = () => { this.locked = null; this.changed(); this.input.focus(); };
    this.$("submit").onclick = () => this.send();
    this.input.addEventListener("input", () => this.changed());
    this.input.addEventListener("keydown", event => {
      if (event.isComposing || this.sending) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        this.active = (this.active + (event.key === "ArrowDown" ? 1 : -1) + this.rows.length) % this.rows.length;
        this.locked = this.rows[this.active]; this.render();
      } else if (event.key === "Tab" && !event.shiftKey && !this.locked && this.rows.length) {
        event.preventDefault(); this.locked = this.rows[this.active]; this.render();
      } else if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault(); this.send();
      }
    });
    this.dialog.addEventListener("cancel", event => { event.preventDefault(); this.close(); });
    this.dialog.addEventListener("close", () => this.restoreFocus());
    this.dialog.addEventListener("click", event => {
      if (event.target !== this.dialog) return;
      const rect = this.dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) this.close();
    });
  }
  get available() { return Boolean(this.workspace()); }
  canToggle() {
    const other = this.document.querySelector("dialog[open]") ||
      this.view.agentCoordPanes?.focusedRecord?.frame?.contentDocument?.querySelector("dialog[open]");
    return this.available && (!other || other === this.dialog);
  }
  toggle() {
    if (this.dialog.open) { this.close(); return true; }
    if (!this.canToggle()) return false;
    let element = this.document.activeElement;
    while (element?.contentDocument?.activeElement) element = element.contentDocument.activeElement;
    this.returnFocus = {element, start: element?.selectionStart, end: element?.selectionEnd,
      direction: element?.selectionDirection, top: element?.scrollTop, left: element?.scrollLeft};
    const cwd = this.workspace();
    if (this.cwd !== cwd) { this.input.value = ""; this.locked = null; }
    this.cwd = cwd;
    this.dialog.setAttribute("aria-label", "Send work in " + cwd);
    this.input.title = "Send work in " + cwd;
    this.dialog.showModal(); this.changed(); this.input.focus({preventScroll: true});
    return true;
  }
  close() {
    if (this.sending) return;
    this.cancelSuggestions(); this.dialog.close(); this.restoreFocus();
  }
  restoreFocus() {
    const previous = this.returnFocus; this.returnFocus = null;
    if (!previous?.element?.isConnected) return;
    previous.element.focus({preventScroll: true});
    if (typeof previous.start === "number") previous.element.setSelectionRange(previous.start, previous.end, previous.direction);
    previous.element.scrollTop = previous.top; previous.element.scrollLeft = previous.left;
  }
  cancelSuggestions() {
    ++this.generation; this.view.clearTimeout(this.timer); this.controller?.abort();
  }
  static matches(threads, cwd, query) {
    const words = query.toLocaleLowerCase().replace(/\bredeploy\b/g, "deploy").match(/[\p{L}\p{N}]+/gu) || [];
    return threads.filter(t => t.browser_session && !t.dispatcher && (t.cwd || t.project_cwd) === cwd)
      .map(t => {
        const title = (t.title || "").toLocaleLowerCase(), summary = t.checkpoint?.summary || "";
        const text = (summary + " " + (t.original_request || "")).toLocaleLowerCase();
        const score = words.reduce((sum, w) => sum + (title.includes(w) ? 4 : 0) + (text.includes(w) ? 1 : 0), 0);
        return {id: t.thread_id, kind: "thread", title: t.title || "Untitled conversation", summary,
          running: t.turn_active || t.browser_session?.running, closed: t.attention === "archived", score, updated: t.updated_at || 0};
      }).filter(t => !words.length || t.score > 0)
      .sort((a, b) => b.score - a.score || b.updated - a.updated).slice(0, 6);
  }
  choices(candidates) {
    return [...candidates, {id: "new", kind: "new", title: "New conversation", summary: "Start in this project"},
      {id: "dispatcher", kind: "dispatcher", title: "Ask dispatcher", summary: "Let it work out where this belongs"}];
  }
  changed() {
    this.cancelSuggestions(); this.error.hidden = true;
    this.input.style.height = "auto";
    this.input.style.height = Math.min(180, this.input.scrollHeight) + "px";
    this.rows = this.choices(SendWorkPalette.matches(this.threads(), this.cwd, this.input.value));
    if (this.locked && !this.rows.some(r => r.id === this.locked.id)) this.rows.unshift(this.locked);
    this.active = this.locked ? this.rows.findIndex(r => r.id === this.locked.id) : 0;
    this.hint.textContent = ""; this.hint.hidden = true; this.render();
    const message = this.input.value.trim();
    if (message.length < 2 || this.locked) return;
    const generation = this.generation;
    this.timer = this.view.setTimeout(async () => {
      const controller = this.controller = new AbortController();
      try {
        const result = await this.api("send-work/suggest", {cwd: this.cwd, message}, {signal: controller.signal});
        if (!this.dialog.open || generation !== this.generation || this.locked) return;
        const merged = new Map();
        for (const row of [...result.candidates.map(c => ({...c, kind: "thread"})), ...this.rows.filter(r => r.kind === "thread")]) {
          if (!merged.has(row.id)) merged.set(row.id, row);
        }
        this.rows = this.choices([...merged.values()].slice(0, 8)); this.active = 0; this.render();
      } catch (error) {
        if (generation === this.generation && error.name !== "AbortError") {
          this.hint.textContent = "Showing local matches. You can still choose a destination.";
          this.hint.hidden = false;
        }
      }
    }, 450);
  }
  render() {
    this.list.replaceChildren();
    this.rows.forEach((row, index) => {
      const item = this.document.createElement("li"); item.id = "send-work-option-" + index;
      item.setAttribute("role", "option"); item.setAttribute("aria-selected", String(index === this.active));
      const title = this.document.createElement("strong"); title.textContent = row.title;
      const detail = this.document.createElement("span");
      detail.textContent = [row.running ? "Running · request will queue" : row.closed ? "Closed · will reopen" : "", row.summary].filter(Boolean).join(" · ");
      item.append(title, detail);
      item.onmousedown = event => event.preventDefault();
      item.onclick = () => { if (this.sending) return; this.active = index; this.locked = row; this.render(); this.input.focus(); };
      this.list.append(item);
    });
    this.input.setAttribute("aria-activedescendant", "send-work-option-" + this.active);
    this.list.children[this.active]?.scrollIntoView?.({block: "nearest"});
    this.$("target").hidden = !this.locked;
    this.$("target-name").textContent = this.locked?.title || "";
    this.$("submit").disabled = this.sending || !this.input.value.trim();
    this.$("submit").textContent = this.sending ? "Sending…" : "Send";
    this.input.disabled = !!this.sending;
  }
  async send() {
    const row = this.locked || this.rows[this.active];
    if (this.sending || !row || !this.input.value.trim()) return;
    this.locked = row; this.cancelSuggestions(); this.sending = true; this.error.hidden = true; this.render();
    const destination = row.kind === "thread" ? {kind: "thread", id: row.id} : {kind: row.kind};
    const body = {cwd: this.cwd, message: this.input.value, destination};
    try {
      const requestId = DispatchUI.requestKey(this.storage, "send-work:" + this.cwd, body);
      const result = await this.api("send-work/send", {...body, requestId});
      this.storage.removeItem("dispatch-request:send-work:" + this.cwd);
      this.input.value = ""; this.locked = null; this.sending = false; this.render(); this.close();
      this.receipt = result.receipt; this.showReceipt(); this.pollReceipt();
    } catch (error) {
      this.error.textContent = error.message; this.error.hidden = false;
    } finally { this.sending = false; this.render(); }
  }
  showReceipt() {
    const receipt = this.receipt;
    this.notice.replaceChildren(); this.notice.hidden = false;
    const labels = {pending: "Preparing request…", routing: "Preparing request…", creating: "Creating conversation…", queued: "Queued", sending: "Sending…", accepted: "Sent", failed: "Request failed", uncertain: "Delivery needs review", paused: "Delivery paused", cancelled: "Cancelled"};
    const text = this.document.createElement("span");
    text.textContent = labels[receipt.state] || receipt.state; this.notice.append(text);
    if (receipt.target_id) {
      const link = this.document.createElement("a"); link.textContent = receipt.target_title || "Open conversation";
      link.href = "/#" + encodeURIComponent(receipt.target_id);
      link.onclick = event => { if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); this.openThread(receipt.target_id); };
      this.notice.append(link);
    }
    if (receipt.error) { const error = this.document.createElement("span"); error.textContent = receipt.error; this.notice.append(error); }
    const close = this.document.createElement("button"); close.type = "button"; close.className = "quiet";
    close.textContent = "Dismiss"; close.onclick = () => { this.notice.hidden = true; this.view.clearTimeout(this.poll); };
    this.notice.append(close);
  }
  pollReceipt() {
    this.view.clearTimeout(this.poll);
    const receipt = this.receipt;
    if (!["pending", "routing", "creating", "queued", "sending"].includes(receipt.state)) return;
    this.poll = this.view.setTimeout(async () => {
      try {
        const result = await this.api("send-work/status", {id: receipt.id, cwd: receipt.cwd});
        if (this.receipt.id !== receipt.id || this.notice.hidden) return;
        this.receipt = result; this.showReceipt(); this.pollReceipt();
      } catch (_) {
        if (this.receipt.id === receipt.id && !this.notice.hidden) {
          this.receipt = {...receipt, state: "uncertain", error: "Could not confirm delivery. Open the destination to check before resending."}; this.showReceipt();
        }
      }
    }, 1000);
  }
  static bindShortcut(document, getPalette) {
    // Pane buttons open the same top-level palette as the keyboard shortcut.
    for (const id of ["send-work-mobile-open", "send-work-mobile-chat"]) {
      const button = document.getElementById(id);
      if (button) button.onclick = () => getPalette()?.toggle();
    }
    document.addEventListener("keydown", event => {
      if (event.defaultPrevented || event.isComposing) return;
      const palette = getPalette();
      if (event.key === "Escape" && palette?.dialog.open) {
        event.preventDefault(); event.stopImmediatePropagation(); palette.close(); return;
      }
      if (!event.shiftKey || event.altKey || !(event.metaKey || event.ctrlKey) || event.key?.toLowerCase() !== "k" || !palette?.canToggle()) return;
      event.preventDefault(); event.stopImmediatePropagation(); if (!event.repeat) palette.toggle();
    }, true);
  }
}
if (typeof module !== "undefined") module.exports = {SendWorkPalette};
