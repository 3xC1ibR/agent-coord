"use strict";

class SendWorkPalette {
  constructor({document, view = window, api, workspace, threads, openThread, storage}) {
    Object.assign(this, {document, view, api, workspace, threads, openThread, storage});
    this.$ = id => document.getElementById("send-work-" + id);
    this.dialog = this.$("dialog"); this.input = this.$("input"); this.list = this.$("results");
    this.error = this.$("error"); this.hint = this.$("status"); this.notice = this.$("notice");
    this.generation = 0; this.rows = []; this.active = 0;
    this.draft = this.defaultSettings(); this.catalogs = new Map(); this.modelLoading = new Map();
    this.settings = document.createElement("div"); this.settings.id = "send-work-settings";
    this.settings.hidden = true;
    this.menu = document.createElement("div"); this.menu.id = "send-work-commands";
    this.menu.className = "slash-commands"; this.menu.hidden = true; this.menu.setAttribute("role", "listbox");
    this.dialog.insertBefore(this.settings, this.list); this.dialog.insertBefore(this.menu, this.list);
    this.slash = new ChatSlashCommands({input: this.input, menu: this.menu,
      getThread: () => "send-work:" + this.cwd, getSession: () => this.draft,
      catalogs: this.catalogs, loadModels: client => this.loadModels(client),
      getModelClients: () => ["codex", "claude", "antigravity", "kimi", "muse"],
      getCommands: () => this.commands(), canComplete: () => this.dialog.open && !this.sending && !this.configuring,
      onSelectModel: choice => { this.modelChoice = choice; },
      onChange: () => this.changed(),
      onComplete: ({query, command}) => {
        if (query.kind !== "command" || command.name === "/new") this.configure();
      }});
    this.$("open").onclick = () => this.toggle();
    this.$("close").onclick = () => this.close();
    this.$("unlock").onclick = () => { this.locked = null; this.changed(); this.input.focus(); };
    this.$("submit").onclick = () => this.send();
    this.input.addEventListener("input", () => this.changed());
    this.input.addEventListener("keydown", event => {
      if (event.isComposing || this.sending || this.configuring) return;
      if (this.slash.keydown(event)) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (this.locked?.kind === "new" || !this.rows.length) return;
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
  defaultSettings() { return {client: "codex", model: null, effort: null, yolo: false}; }
  commands() {
    return [{name: "/new", arguments: "", description: "Prepare a new conversation without opening it"},
      ...(this.locked?.kind === "new" ? [
        {name: "/model", arguments: "[model-id] [effort]", description: "Choose the new agent's model"},
        {name: "/effort", arguments: "[level]", description: "Choose reasoning effort"},
        {name: "/permissions", arguments: "[default|yolo]", description: "Choose the new agent's permissions"},
      ] : [])];
  }
  async loadModels(client) {
    if (this.catalogs.has(client)) return this.catalogs.get(client);
    if (!this.modelLoading.has(client)) {
      this.modelLoading.set(client, this.api("models?client=" + encodeURIComponent(client)).then(result => {
        this.catalogs.set(client, result.data); return result.data;
      }).finally(() => this.modelLoading.delete(client)));
    }
    return this.modelLoading.get(client);
  }
  async configure() {
    if (this.configuring || this.sending) return;
    const match = /^\s*\/(\S+)(?:[ \t]+([^\r\n]*))?(?:\r?\n|$)/.exec(this.input.value);
    if (!match) return;
    const command = match[1].toLowerCase(), argument = (match[2] || "").trim();
    const rest = this.input.value.slice(match[0].length);
    this.cancelSuggestions(); this.error.hidden = true;
    this.configuring = true; this.render();
    try {
      if (command === "new") {
        this.locked = this.choices([]).find(row => row.kind === "new");
        this.input.value = [argument, rest].filter(Boolean).join("\n");
      } else {
        if (this.locked?.kind !== "new") throw new Error("Use /new before configuring an agent.");
        if (command === "permissions") {
          if (!argument) { this.configuring = false; this.offerCommand("permissions"); return; }
          if (!["default", "yolo"].includes(argument.toLowerCase())) throw new Error("Use /permissions default or /permissions yolo.");
          this.draft.yolo = argument.toLowerCase() === "yolo";
        } else if (command === "model" || command === "effort") {
          if (command === "effort" && !this.draft.model) throw new Error("Choose a model with /model before setting effort.");
          if (!argument) { this.configuring = false; this.offerCommand(command); return; }
          const parts = argument.split(/\s+/);
          if (parts.length > (command === "model" ? 2 : 1)) throw new Error("Use /model <model-id> [effort] or /effort <level>.");
          let client = this.draft.client, model = this.draft.model, effort = parts[0];
          if (command === "model") {
            model = parts[0]; effort = parts[1] || null;
            if (this.modelChoice?.model === model) client = this.modelChoice.client;
            else {
              const clients = [client, ...["codex", "claude", "antigravity", "kimi", "muse"].filter(c => c !== client)];
              await Promise.allSettled(clients.map(c => this.loadModels(c)));
              client = clients.find(c => this.catalogs.get(c)?.some(m => m.model === model));
              if (!client) throw new Error("Choose an available model from /model.");
            }
          }
          if (!model) throw new Error("Choose a model with /model before setting effort.");
          const selected = (await this.loadModels(client)).find(m => m.model === model);
          if (!selected) throw new Error("Choose an available model from /model.");
          if (effort && !selected.supportedReasoningEfforts?.some(e => e.reasoningEffort === effort)) {
            throw new Error("Reasoning effort is not supported by this model.");
          }
          Object.assign(this.draft, {client, model, effort});
        } else throw new Error("Use /new, /model, /effort, or /permissions in this popup.");
        this.input.value = rest;
      }
      this.modelChoice = null; this.input.setSelectionRange(0, 0);
      this.slash.dismissed = null; this.slash.close(); this.changed();
    } catch (error) {
      this.error.textContent = error.message; this.error.hidden = false;
    } finally {
      this.configuring = false; this.render();
      if (this.dialog.open) this.input.focus({preventScroll: true});
      this.slash.render();
    }
  }
  offerCommand(name) {
    if (this.sending || this.configuring) return;
    // Keep a typed request underneath the temporary command line.
    const rest = /^\s*\/(?:model|effort|permissions)\b[^\r\n]*(?:\r?\n|$)/i.test(this.input.value) ?
      this.input.value.replace(/^\s*\/[^\r\n]*(?:\r?\n|$)/, "") : this.input.value;
    this.input.value = "/" + name + " " + (rest ? "\n" + rest : "");
    this.input.focus(); this.input.setSelectionRange(name.length + 2, name.length + 2);
    this.slash.dismissed = null; this.changed(); this.slash.render();
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
    if (this.cwd !== cwd) { this.input.value = ""; this.locked = null; this.draft = this.defaultSettings(); }
    this.cwd = cwd;
    this.dialog.setAttribute("aria-label", "Send work in " + cwd);
    this.input.title = "Send work in " + cwd;
    this.dialog.showModal(); this.changed(); this.input.focus({preventScroll: true});
    return true;
  }
  close() {
    if (this.sending || this.configuring) return;
    this.slash.close(); this.cancelSuggestions(); this.dialog.close(); this.restoreFocus();
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
    this.slash.render();
    const message = this.input.value.trim();
    if (message.length < 2 || this.locked || message.startsWith("/")) return;
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
    this.settings.hidden = this.locked?.kind !== "new";
    this.list.hidden = this.locked?.kind === "new";
    this.settings.replaceChildren();
    if (!this.settings.hidden) {
      const labels = {codex: "Codex", claude: "Claude Code", antigravity: "Google Antigravity", kimi: "Kimi Code", muse: "Muse Code"};
      for (const [command, label] of [["model", labels[this.draft.client] + " · " + (this.draft.model || "Default model")],
        ["effort", this.draft.effort || "Model default effort"], ["permissions", this.draft.yolo ? "Full access · no approvals" : "Default permissions"]]) {
        const button = this.document.createElement("button"); button.type = "button"; button.className = "quiet";
        button.textContent = label; button.title = "/" + command; button.disabled = !!(this.sending || this.configuring);
        button.onclick = () => this.offerCommand(command); this.settings.append(button);
      }
    }
    if (this.menu.hidden) {
      this.input.setAttribute("aria-controls", this.list.id);
      this.input.setAttribute("aria-expanded", String(!this.list.hidden));
      if (this.list.hidden) this.input.removeAttribute("aria-activedescendant");
    }
    this.$("submit").disabled = this.sending || this.configuring || !this.input.value.trim();
    this.$("submit").textContent = this.sending ? "Sending…" : "Send";
    this.input.disabled = !!(this.sending || this.configuring);
  }
  async send() {
    if (this.sending || this.configuring) return;
    if (this.input.value.trimStart().startsWith("/")) { await this.configure(); return; }
    const row = this.locked || this.rows[this.active];
    if (this.sending || !row || !this.input.value.trim()) return;
    this.locked = row; this.cancelSuggestions(); this.sending = true; this.error.hidden = true; this.render();
    const destination = row.kind === "thread" ? {kind: "thread", id: row.id} : {kind: row.kind};
    const body = {cwd: this.cwd, message: this.input.value, destination};
    if (row.kind === "new") body.settings = {...this.draft};
    try {
      const requestId = DispatchUI.requestKey(this.storage, "send-work:" + this.cwd, body);
      const result = await this.api("send-work/send", {...body, requestId});
      this.storage.removeItem("dispatch-request:send-work:" + this.cwd);
      this.input.value = ""; this.locked = null; this.draft = this.defaultSettings(); this.sending = false; this.render(); this.close();
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
        if (!palette.menu.hidden && palette.slash.keydown(event)) { event.stopImmediatePropagation(); return; }
        event.preventDefault(); event.stopImmediatePropagation(); palette.close(); return;
      }
      if (!event.shiftKey || event.altKey || !(event.metaKey || event.ctrlKey) || event.key?.toLowerCase() !== "k" || !palette?.canToggle()) return;
      event.preventDefault(); event.stopImmediatePropagation(); if (!event.repeat) palette.toggle();
    }, true);
  }
}
if (typeof module !== "undefined") module.exports = {SendWorkPalette};
