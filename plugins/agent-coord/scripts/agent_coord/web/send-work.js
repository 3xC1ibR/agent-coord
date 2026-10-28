"use strict";

class SendWorkPalette {
  constructor({document, view = window, api, workspace, threads, openThread, storage}) {
    Object.assign(this, {document, view, api, workspace, threads, openThread, storage});
    this.$ = id => document.getElementById("send-work-" + id);
    this.dialog = this.$("dialog"); this.input = this.$("input"); this.list = this.$("results");
    this.content = this.$("content"); this.dock = this.$("dock"); this.overviewBar = this.$("overview-bar");
    this.overview = document.getElementById("welcome"); this.drafts = new Map();
    try { this.preferences = view.localStorage || storage; this.pinned = this.preferences.getItem("ribbon-field.floating-dispatch") === "true"; } catch { this.pinned = false; }
    this.error = this.$("error"); this.hint = this.$("status"); this.notice = this.$("notice");
    this.noticeTimer = createTransientNotice(this.notice, view, () => {
      this.notice.hidden = true; this.view.clearTimeout(this.poll);
    });
    this.generation = 0; this.rows = []; this.active = 0;
    this.draft = this.defaultSettings(); this.catalogs = new Map(); this.modelLoading = new Map();
    this.settings = document.createElement("div"); this.settings.id = "send-work-settings";
    this.settings.hidden = true;
    this.menu = document.createElement("div"); this.menu.id = "send-work-commands";
    this.menu.className = "slash-commands"; this.menu.hidden = true; this.menu.setAttribute("role", "listbox");
    this.content.insertBefore(this.settings, this.list); this.content.insertBefore(this.menu, this.list);
    this.slash = new ChatSlashCommands({input: this.input, menu: this.menu,
      getThread: () => "send-work:" + this.cwd, getSession: () => ({...this.draft, cwd: this.cwd}),
      catalogs: this.catalogs, loadModels: client => this.loadModels(client),
      loadSkills: async session => (await this.api("skills", {client: session.client, cwd: session.cwd})).data,
      getModelClients: () => ["codex", "claude", "antigravity", "kimi", "muse"],
      getCommands: () => this.commands(), canComplete: () => this.visible && !this.sending && !this.configuring,
      onSelectModel: choice => { this.modelChoice = choice; },
      onChange: () => this.changed(),
      onComplete: ({query, command}) => {
        if (query.kind !== "skill" && (query.kind !== "command" || ["/new", "/yolo"].includes(command.name))) this.configure();
      }});
    this.$("open").onclick = () => this.toggle();
    this.$("close").onclick = () => this.close();
    this.$("unlock").onclick = () => { this.locked = null; this.changed(); this.input.focus(); };
    this.$("submit").onclick = () => this.send();
    this.$("pin").onclick = () => this.pin(!this.pinned);
    this.$("profile-toggle").onclick = () => this.openProfile();
    this.$("profile-close").onclick = () => this.closeProfile();
    this.$("profile-save").onclick = () => this.saveProfile();
    this.$("profile-client").onchange = () => this.profileModels();
    this.$("profile-model").onchange = () => this.profileEfforts();
    this.input.addEventListener("input", () => this.changed());
    this.input.addEventListener("keydown", event => {
      if (event.isComposing || event.keyCode === 229 || this.slash.composing || this.sending || this.configuring) return;
      if (this.slash.keydown(event)) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (this.locked?.kind === "new" || !this.rows.length) return;
        event.preventDefault();
        this.active = (this.active + (event.key === "ArrowDown" ? 1 : -1) + this.rows.length) % this.rows.length;
        this.locked = this.rows[this.active]; this.render();
      } else if (event.key === "Tab" && !event.shiftKey && !this.locked && this.rows.length) {
        event.preventDefault(); this.locked = this.rows[this.active]; this.render();
      } else if (event.key === "Enter" && !event.shiftKey && !this.view.matchMedia?.("(hover: none) and (pointer: coarse)").matches) {
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
    if (view.MutationObserver && this.overview) {
      this.overviewObserver = new view.MutationObserver(() => this.syncOverview());
      this.overviewObserver.observe(this.overview, {attributes: true, attributeFilter: ["hidden"]});
    }
    this.syncOverview();
  }
  get visible() { return this.dialog.open || this.docked; }
  setWorkspace(cwd) {
    if (this.cwd === cwd) return;
    if (this.cwd) this.drafts.set(this.cwd, {message: this.input.value, locked: this.locked, draft: this.draft});
    const saved = this.drafts.get(cwd);
    this.cwd = cwd; this.input.value = saved?.message || ""; this.locked = saved?.locked || null;
    this.draft = saved?.draft || this.defaultSettings();
    this.closeProfile(); this.cancelSuggestions();
    this.$("logo").title = "Dispatcher" + (cwd ? " in " + cwd : "");
    this.dialog.setAttribute("aria-label", "Dispatcher" + (cwd ? " in " + cwd : ""));
    this.input.title = cwd ? "Dispatch work in " + cwd : "Choose a project";
  }
  syncOverview() {
    const cwd = this.workspace();
    const changed = this.cwd !== cwd;
    this.setWorkspace(cwd);
    this.overviewBar.hidden = !this.overview || this.overview.hidden;
    this.$("pin").disabled = !cwd;
    this.$("pin").textContent = this.pinned ? "Hide dispatcher" : "Show dispatcher";
    this.$("pin").setAttribute("aria-pressed", String(this.pinned));
    const docked = this.pinned && !this.overviewBar.hidden && !!cwd;
    if (docked && this.dialog.open) { this.returnFocus = null; this.dialog.close(); }
    const moved = this.docked !== docked;
    if (moved) {
      this.slash.close(); this.cancelSuggestions(); this.closeProfile();
      (docked ? this.dock : this.dialog).append(this.content);
      this.docked = docked;
    }
    this.dock.hidden = !docked;
    this.$("close").hidden = docked;
    if (this.visible && (changed || moved)) this.changed();
  }
  pin(value) {
    this.pinned = value;
    try { this.preferences?.setItem("ribbon-field.floating-dispatch", String(value)); } catch { /* Optional display preference. */ }
    this.syncOverview();
    if (this.docked) this.input.focus({preventScroll: true});
    else this.$("pin").focus({preventScroll: true});
  }
  closeProfile() {
    this.profileGeneration = (this.profileGeneration || 0) + 1;
    this.$("profile").hidden = true;
    this.$("profile-toggle").setAttribute("aria-expanded", "false");
  }
  async openProfile() {
    if (this.sending || this.configuring || this.profileSaving) return;
    if (!this.$("profile").hidden) { this.closeProfile(); return; }
    const cwd = this.cwd, generation = this.profileGeneration = (this.profileGeneration || 0) + 1;
    this.slash.close(); this.$("profile").hidden = false;
    this.$("profile-toggle").setAttribute("aria-expanded", "true");
    this.$("profile-error").hidden = true; this.$("profile-save").disabled = true;
    this.$("profile-client").disabled = true;
    this.$("profile-model").replaceChildren(); this.$("profile-model").disabled = true;
    this.$("profile-effort").replaceChildren(); this.$("profile-effort").disabled = true;
    try {
      const result = await this.api("send-work/dispatcher-settings", {cwd});
      if (cwd !== this.cwd || generation !== this.profileGeneration) return;
      this.profile = {...result.settings};
      this.$("profile-client").value = this.profile.client;
      this.$("profile-permissions").value = this.profile.yolo ? "yolo" : "default";
      await this.profileModels(this.profile.model, this.profile.effort);
    } catch (error) {
      if (cwd === this.cwd && generation === this.profileGeneration) {
        this.$("profile-error").textContent = error.message; this.$("profile-error").hidden = false;
      }
    } finally {
      if (cwd === this.cwd && generation === this.profileGeneration) this.$("profile-client").disabled = false;
    }
  }
  async profileModels(model, effort) {
    const client = this.$("profile-client").value, cwd = this.cwd;
    const generation = this.profileGeneration = (this.profileGeneration || 0) + 1;
    this.$("profile-error").hidden = true; this.$("profile-save").disabled = true;
    this.$("profile-model").disabled = true; this.$("profile-effort").disabled = true;
    try {
      const models = await this.loadModels(client);
      if (cwd !== this.cwd || generation !== this.profileGeneration) return;
      const picker = this.$("profile-model"); picker.replaceChildren();
      for (const item of models) {
        const option = this.document.createElement("option"); option.value = item.model;
        option.textContent = item.displayName || item.model; picker.append(option);
      }
      picker.value = models.find(item => item.model === model)?.model || models.find(item => item.isDefault)?.model || models[0]?.model || "";
      if (!models.length) throw new Error("No models available for this provider.");
      this.profileEfforts(effort); picker.disabled = false;
      this.$("profile-save").disabled = false;
    } catch (error) {
      if (cwd === this.cwd && generation === this.profileGeneration) {
        this.$("profile-error").textContent = error.message; this.$("profile-error").hidden = false;
      }
    } finally {
      if (cwd === this.cwd && generation === this.profileGeneration) this.$("profile-client").disabled = false;
    }
  }
  profileEfforts(effort) {
    const selected = this.catalogs.get(this.$("profile-client").value)?.find(m => m.model === this.$("profile-model").value);
    const picker = this.$("profile-effort"); picker.replaceChildren();
    for (const value of ["", ...(selected?.supportedReasoningEfforts || []).map(e => e.reasoningEffort)]) {
      const option = this.document.createElement("option"); option.value = value;
      option.textContent = value || "Model default"; picker.append(option);
    }
    picker.value = selected?.supportedReasoningEfforts?.some(e => e.reasoningEffort === effort) ? effort : "";
    picker.disabled = !selected?.supportedReasoningEfforts?.length;
  }
  async saveProfile() {
    if (this.profileSaving || this.$("profile-save").disabled) return;
    const cwd = this.cwd, generation = this.profileGeneration;
    const settings = {client: this.$("profile-client").value, model: this.$("profile-model").value,
      effort: this.$("profile-effort").value || null, yolo: this.$("profile-permissions").value === "yolo"};
    this.profileSaving = true; this.$("profile-save").disabled = true;
    this.$("profile-error").hidden = true;
    try {
      await this.api("send-work/dispatcher-settings", {cwd, settings});
      if (cwd !== this.cwd || generation !== this.profileGeneration) return;
      this.closeProfile(); this.$("profile-toggle").focus();
      this.hint.textContent = "Dispatcher settings saved for this project."; this.hint.hidden = false;
    } catch (error) {
      if (cwd === this.cwd && generation === this.profileGeneration) {
        this.$("profile-error").textContent = error.message; this.$("profile-error").hidden = false;
      }
    } finally {
      this.profileSaving = false;
      if (cwd === this.cwd && generation === this.profileGeneration) this.$("profile-save").disabled = false;
    }
  }
  defaultSettings() { return {client: "codex", model: null, effort: null, yolo: false}; }
  commands() {
    return [{name: "/new", arguments: "", description: "Prepare a new conversation without opening it"},
      ...(this.locked?.kind === "new" ? [
        {name: "/model", arguments: "[model-id] [effort]", description: "Choose the new agent's model"},
        {name: "/effort", arguments: "[level]", description: "Choose reasoning effort"},
        {name: "/permissions", arguments: "[default|yolo]", description: "Choose the new agent's permissions"},
        {name: "/yolo", arguments: "", description: "Enable full machine access without approval prompts"},
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
    const cwd = this.cwd, draft = this.draft;
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
        if (command === "yolo") {
          if (argument) throw new Error("Use /yolo without arguments. Use /permissions default to turn it off.");
          this.draft.yolo = true;
        } else if (command === "permissions") {
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
              if (cwd !== this.cwd || draft !== this.draft) return;
              client = clients.find(c => this.catalogs.get(c)?.some(m => m.model === model));
              if (!client) throw new Error("Choose an available model from /model.");
            }
          }
          if (!model) throw new Error("Choose a model with /model before setting effort.");
          const selected = (await this.loadModels(client)).find(m => m.model === model);
          if (cwd !== this.cwd || draft !== this.draft) return;
          if (!selected) throw new Error("Choose an available model from /model.");
          if (effort && !selected.supportedReasoningEfforts?.some(e => e.reasoningEffort === effort)) {
            throw new Error("Reasoning effort is not supported by this model.");
          }
          Object.assign(this.draft, {client, model, effort});
        } else throw new Error("Use /new, /model, /effort, /permissions, or /yolo in this popup.");
        this.input.value = rest;
      }
      this.modelChoice = null; this.input.setSelectionRange(0, 0);
      this.slash.dismissed = null; this.slash.close(); this.changed();
    } catch (error) {
      this.error.textContent = error.message; this.error.hidden = false;
    } finally {
      this.configuring = false; this.render();
      if (this.visible) this.input.focus({preventScroll: true});
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
    if (this.docked) {
      if (!this.canToggle()) return false;
      this.input.focus({preventScroll: true}); return true;
    }
    if (this.dialog.open) { this.close(); return true; }
    if (!this.canToggle()) return false;
    let element = this.document.activeElement;
    while (element?.contentDocument?.activeElement) element = element.contentDocument.activeElement;
    this.returnFocus = {element, start: element?.selectionStart, end: element?.selectionEnd,
      direction: element?.selectionDirection, top: element?.scrollTop, left: element?.scrollLeft};
    this.setWorkspace(this.workspace());
    this.dialog.showModal(); this.changed(); this.input.focus({preventScroll: true});
    return true;
  }
  close() {
    if (this.sending || this.configuring) return;
    this.slash.close(); this.closeProfile(); this.cancelSuggestions();
    if (!this.docked) { this.dialog.close(); this.restoreFocus(); }
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
        if (!this.visible || generation !== this.generation || this.locked) return;
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
    this.settings.setAttribute("aria-label", "New conversation settings");
    this.$("new-settings-label").hidden = this.settings.hidden;
    this.list.hidden = this.locked?.kind === "new" || (this.docked && !this.input.value.trim());
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
    this.$("profile-toggle").disabled = !!(this.sending || this.configuring);
    this.$("submit").setAttribute("aria-label", this.sending ? "Sending…" : "Send");
    this.$("submit").title = this.sending ? "Sending…" : "Send";
    this.input.disabled = !!(this.sending || this.configuring);
  }
  async send() {
    if (this.sending || this.configuring) return;
    if (this.input.value.trimStart().startsWith("/")) { await this.configure(); return; }
    const row = this.locked || this.rows[this.active];
    if (this.sending || !row || !this.input.value.trim()) return;
    this.locked = row; this.cancelSuggestions(); this.sending = true; this.error.hidden = true; this.render();
    const destination = row.kind === "thread" ? {kind: "thread", id: row.id} : {kind: row.kind};
    const cwd = this.cwd, body = {cwd, message: this.input.value, destination};
    if (row.kind === "new") body.settings = {...this.draft};
    try {
      const requestId = DispatchUI.requestKey(this.storage, "send-work:" + cwd, body);
      const result = await this.api("send-work/send", {...body, requestId});
      this.storage.removeItem("dispatch-request:send-work:" + cwd);
      this.drafts.delete(cwd);
      this.sending = false;
      if (this.cwd === cwd) {
        this.input.value = ""; this.locked = null; this.draft = this.defaultSettings(); this.render(); this.close();
        if (this.docked) { this.changed(); this.input.focus({preventScroll: true}); }
      }
      this.receipt = result.receipt; this.showReceipt(); this.pollReceipt();
    } catch (error) {
      if (this.cwd === cwd) { this.error.textContent = error.message; this.error.hidden = false; }
    } finally { this.sending = false; this.render(); }
  }
  showReceipt() {
    const receipt = this.receipt;
    // Polling unchanged progress must not rebuild focused links or extend the
    // popup forever. A meaningful update gets a new reading interval.
    const signature = JSON.stringify([receipt.id, receipt.state, receipt.target_id, receipt.target_title, receipt.error]);
    if (!this.notice.hidden && signature === this.noticeSignature) return;
    this.noticeSignature = signature;
    this.notice.replaceChildren();
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
    close.textContent = "Dismiss"; close.onclick = () => this.noticeTimer.dismiss();
    this.notice.append(close);
    this.noticeTimer.show(receipt.error || ["failed", "uncertain", "paused"].includes(receipt.state) ? 10000 : 6000);
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
      if (event.key === "Escape" && palette?.visible && palette.canToggle() &&
          (palette.dialog.open || palette.content.contains(document.activeElement))) {
        if (!palette.$("profile").hidden) {
          event.preventDefault(); event.stopImmediatePropagation(); palette.closeProfile(); palette.$("profile-toggle").focus(); return;
        }
        if (!palette.menu.hidden && palette.slash.keydown(event)) { event.stopImmediatePropagation(); return; }
        if (palette.docked) return;
        event.preventDefault(); event.stopImmediatePropagation(); palette.close(); return;
      }
      if (!event.shiftKey || event.altKey || !(event.metaKey || event.ctrlKey) || event.key?.toLowerCase() !== "k" || !palette?.canToggle()) return;
      event.preventDefault(); event.stopImmediatePropagation(); if (!event.repeat) palette.toggle();
    }, true);
  }
}
if (typeof module !== "undefined") module.exports = {SendWorkPalette};
