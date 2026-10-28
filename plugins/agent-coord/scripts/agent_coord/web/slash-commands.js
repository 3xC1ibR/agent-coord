"use strict";
(() => {
  const commands = [
    {name: "/cd", arguments: "[directory]", description: "Show or change the working directory"},
    {name: "/model", arguments: "[model-id] [effort]", description: "List models or choose one for the next message"},
    {name: "/effort", arguments: "[level]", description: "Show or change reasoning effort"},
    {name: "/permissions", arguments: "[default|yolo]", description: "Show or change session permissions"},
    {name: "/yolo", arguments: "", description: "Enable full machine access without approval prompts"},
    {name: "/fork", arguments: "", description: "Fork this Codex or Claude conversation into a new thread"},
    {name: "/close", arguments: "", description: "Stop and close this thread, keeping its history"},
    {name: "/help", arguments: "", description: "Show available commands and usage"},
    {name: "/fast", arguments: "[on|off|status]", description: "Toggle Codex Fast mode for subsequent messages", codexOnly: true},
    {name: "/ultrafast", arguments: "[on|off|status]", description: "Toggle Codex Ultrafast mode for subsequent messages", codexOnly: true},
  ];

  class ChatSlashCommands {
    constructor({input, menu, getThread, getSession = () => ({}), loadModels = async () => [], loadSkills = async () => [],
      catalogs = new Map(), getModelClients = () => [getSession()?.client || "codex"],
      onSelectModel = () => {}, getCommands = () => commands, onComplete = () => {}, canComplete, onReplace = () => {}, onChange}) {
      Object.assign(this, {input, menu, getThread, getSession, loadModels, loadSkills, getModelClients,
        onSelectModel, getCommands, onComplete, canComplete, onReplace, onChange});
      this.catalogs = catalogs;
      this.skillCatalogs = new Map();
      this.skillLoading = new Set();
      this.skillErrors = new Map();
      this.loading = new Set();
      this.modelErrors = new Set();
      this.matches = [];
      this.index = 0;
      input.addEventListener("input", event => {
        this.dismissed = null;
        this.skillErrors.clear();
        this.modelErrors.clear();
        if (event.isComposing) this.close();
        else this.render();
      });
      input.addEventListener("compositionstart", () => { this.composing = true; this.close(); });
      input.addEventListener("compositionend", () => { this.composing = false; this.render(); });
      input.addEventListener("focus", () => { this.dismissed = null; this.skillErrors.clear(); this.modelErrors.clear(); this.render(); });
      input.addEventListener("blur", () => this.close());
      input.addEventListener("click", () => this.render());
      input.addEventListener("keyup", () => this.render());
      input.ownerDocument.addEventListener("selectionchange", () => this.render());
      // Keep the caret in the composer when choosing a suggestion with a pointer.
      menu.addEventListener("mousedown", event => event.preventDefault());
      menu.addEventListener("pointerdown", event => event.preventDefault());
    }

    query() {
      const input = this.input;
      if (this.composing || !this.canComplete() || input.disabled || input.ownerDocument.activeElement !== input ||
          input.selectionStart !== input.selectionEnd) return null;
      const caret = input.selectionStart;
      const skill = /(?:^|[\s(\[{])\$([\w:.-]*)$/.exec(input.value.slice(0, caret));
      if (skill) return {kind: "skill", prefix: "$" + skill[1].toLowerCase(), start: caret - skill[1].length - 1,
        end: caret + /^[\w:.-]*/.exec(input.value.slice(caret))[0].length};
      const command = /^([ \t]*)(\/[^\s]*)/.exec(input.value);
      if (!command || caret <= command[1].length) return null;
      if (caret <= command[0].length) return {kind: "command",
        prefix: input.value.slice(command[1].length, caret).toLowerCase(), start: command[1].length, end: command[0].length};
      const name = command[2].toLowerCase();
      if (!this.getCommands().some(command => command.name === name)) return null;
      if (["/fast", "/ultrafast"].includes(name) && (this.getSession()?.client || "codex") !== "codex") return null;
      if (!["/model", "/effort", "/permissions", "/fast", "/ultrafast"].includes(name) || /[\r\n]/.test(input.value.slice(0, caret))) return null;
      // Track token boundaries so editing an argument keeps its neighbors intact.
      const tokens = [...input.value.matchAll(/"[^"\r\n]*(?:"|$)|'[^'\r\n]*(?:'|$)|[^\s]+/g)];
      let index = tokens.findIndex(token => caret >= token.index && caret <= token.index + token[0].length);
      if (index < 0) index = tokens.filter(token => token.index < caret).length;
      const token = tokens[index], start = token && token.index <= caret ? token.index : caret;
      const end = start === token?.index ? start + token[0].length : caret;
      const unquote = value => value.replace(/^["']|["']$/g, "");
      const kind = ["/fast", "/ultrafast"].includes(name) && index === 1 ? "speed" :
        name === "/permissions" && index === 1 ? "permissions" :
        name === "/model" && index === 1 ? "model" :
        (name === "/effort" && index === 1) || (name === "/model" && index === 2) ? "effort" : null;
      if (!kind) return null;
      return {kind, start, end, prefix: unquote(input.value.slice(start, caret)).toLowerCase(),
        model: name === "/model" && index === 2 ? unquote(tokens[1][0]) : this.getSession()?.model};
    }

    models(client = this.getSession()?.client || "codex") {
      if (!this.catalogs.has(client) && !this.loading.has(client) && !this.modelErrors.has(client)) {
        this.loading.add(client);
        Promise.resolve().then(() => this.loadModels(client)).then(models => {
          this.loading.delete(client);
          this.catalogs.set(client, models);
          // Re-evaluate the current draft and provider, not the one that started the request.
          this.render();
        }, () => {
          // Leave manual commands usable; another input/focus can retry the request.
          this.loading.delete(client);
          this.modelErrors.add(client);
          this.render();
        });
      }
      return this.catalogs.get(client) || [];
    }

    suggestions(query) {
      if (query.kind === "skill") return this.skills().map(skill => ({name: "$" + skill.name,
        arguments: skill.scope || "", description: skill.description || ""}));
      if (query.kind === "command") return this.getCommands().filter(command => !command.codexOnly || (this.getSession()?.client || "codex") === "codex");
      if (query.kind === "speed") return [
        {name: "on", arguments: "", description: "Enable this speed mode"},
        {name: "off", arguments: "", description: "Use standard speed"},
        {name: "status", arguments: "", description: "Show the current speed mode"},
      ];
      if (query.kind === "permissions") return [
        {name: "default", arguments: "", description: "Configured permissions with approval prompts"},
        {name: "yolo", arguments: "", description: "Full machine access without approval prompts"},
      ];
      if (query.kind === "model") {
        const clients = this.getModelClients();
        const labels = {codex: "Codex", claude: "Claude Code", antigravity: "Google Antigravity", kimi: "Kimi Code", muse: "Muse Code"};
        return clients.flatMap(client => this.models(client).map(model => ({name: model.model, client,
          group: clients.length > 1 ? labels[client] || client : null,
          arguments: [clients.length > 1 ? labels[client] || client : "", model.displayName || ""].filter(Boolean).join(" · "),
          description: model.description || ""})));
      }
      if (!query.model) return [];
      const models = this.models();
      const model = models.find(model => model.model === query.model || model.resolvedModel === query.model);
      return (model?.supportedReasoningEfforts || []).map(effort => ({name: effort.reasoningEffort,
        arguments: "", description: effort.description || ""}));
    }

    skills() {
      const session = {...this.getSession()}, key = this.skillKey(session);
      if (!this.skillCatalogs.has(key) && !this.skillLoading.has(key) && !this.skillErrors.has(key)) {
        this.skillLoading.add(key);
        Promise.resolve().then(() => this.loadSkills(session)).then(skills => {
          this.skillLoading.delete(key);
          this.skillCatalogs.set(key, skills);
          this.render();
        }, () => {
          this.skillLoading.delete(key);
          this.skillErrors.set(key, "Skills unavailable. Type to retry.");
          this.render();
        });
      }
      return this.skillCatalogs.get(key) || [];
    }

    skillKey(session = this.getSession() || {}) {
      return JSON.stringify([session.client || "codex", session.cwd || ""]);
    }

    pending(query) {
      return query?.kind === "skill" ? this.skillLoading.has(this.skillKey()) :
        query?.kind === "model" ? this.getModelClients().some(client => this.loading.has(client)) :
        query?.kind === "effort" && this.loading.has(this.getSession()?.client || "codex");
    }

    close() {
      this.menu.hidden = true;
      if (this.input.getAttribute("aria-controls") === this.menu.id) {
        this.input.setAttribute("aria-expanded", "false");
        this.input.removeAttribute("aria-activedescendant");
      }
    }

    render(force = false) {
      const query = this.query();
      const key = JSON.stringify([this.getThread(), this.input.value, this.input.selectionStart]);
      if (!query || this.dismissed === key) { this.close(); return; }
      const matches = this.suggestions(query).filter(command => query.kind === "skill" ?
        command.name.slice(1).toLowerCase().includes(query.prefix.slice(1)) : command.name.toLowerCase().startsWith(query.prefix));
      if (!matches.length) {
        this.matches = []; this.options = [];
        const status = this.pending(query) ? (query.kind === "skill" ? "Loading skills…" : "Loading models…") :
          query.kind === "skill" ? this.skillErrors.get(this.skillKey()) : null;
        if (!status) { this.close(); return; }
        const message = this.input.ownerDocument.createElement("div");
        message.className = "slash-command"; message.setAttribute("role", "status"); message.textContent = status;
        this.menu.replaceChildren(message); this.menu.hidden = false;
        this.input.setAttribute("aria-controls", this.menu.id);
        this.input.setAttribute("aria-expanded", "true");
        this.input.removeAttribute("aria-activedescendant");
        this.signature = null;
        return;
      }
      const signature = JSON.stringify(matches);
      if (!force && !this.menu.hidden && key === this.key && signature === this.signature) return;
      if (key !== this.key || signature !== this.signature) this.index = 0;
      this.signature = signature;
      this.key = key;
      this.matches = matches;
      this.menu.hidden = false;
      this.input.setAttribute("aria-controls", this.menu.id);
      this.menu.setAttribute("aria-label", query.kind === "model" ? "Models" : query.kind === "effort" ? "Reasoning effort" :
        query.kind === "permissions" ? "Permissions" : query.kind === "speed" ? "Codex speed" : query.kind === "skill" ? "Skills" : "Slash commands");
      this.input.setAttribute("aria-expanded", "true");
      this.menu.replaceChildren();
      const doc = this.input.ownerDocument;
      this.options = [];
      let group = null, groupName = null;
      for (const [index, command] of matches.entries()) {
        if (command.group && command.group !== groupName) {
          groupName = command.group;
          group = doc.createElement("div");
          group.setAttribute("role", "group");
          group.setAttribute("aria-label", groupName);
          this.menu.append(group);
        }
        const option = doc.createElement("div");
        option.id = this.menu.id + "-" + index;
        option.className = "slash-command";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", String(index === this.index));
        const name = doc.createElement("strong"); name.textContent = command.name;
        const usage = doc.createElement("span"); usage.textContent = command.arguments;
        const description = doc.createElement("small"); description.textContent = command.description;
        option.append(name, usage, description);
        option.addEventListener("click", () => this.choose(index, true));
        (command.group ? group : this.menu).append(option);
        this.options.push(option);
      }
      this.input.setAttribute("aria-activedescendant", this.menu.id + "-" + this.index);
    }

    choose(index, apply = false) {
      const query = this.query(), command = this.matches[index];
      if (!query || !command || this.menu.hidden) return;
      const suffix = this.input.value.slice(query.end);
      // Replace only the active token; retain arguments already entered.
      const completion = command.name + (suffix ? "" : " ");
      this.onReplace(query.start, query.end, completion);
      this.input.setRangeText(completion, query.start, query.end, "end");
      const next = this.query();
      this.dismissed = next && next.kind !== query.kind ? null :
        JSON.stringify([this.getThread(), this.input.value, this.input.selectionStart]);
      this.close();
      if (query.kind === "model") this.onSelectModel({client: command.client, model: command.name});
      this.onChange();
      const complete = query.kind !== "skill" && (query.kind !== "command" || !command.arguments);
      this.onComplete({query, command, apply: apply && complete});
      this.render();
    }

    keydown(event) {
      if (event.isComposing || event.keyCode === 229 || this.composing || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return false;
      this.render();
      if (event.key === "Escape" && this.query()) {
        this.dismissed = JSON.stringify([this.getThread(), this.input.value, this.input.selectionStart]);
        this.close();
        event.preventDefault();
        return true;
      }
      if (this.menu.hidden) return false;
      if (!["ArrowDown", "ArrowUp", "Enter", "Tab"].includes(event.key)) return false;
      event.preventDefault();
      // A cold catalog must not let Enter submit the unfinished token as a prompt.
      if (!this.matches.length) return true;
      if (event.key === "Enter" || event.key === "Tab") this.choose(this.index, event.key === "Enter");
      else {
        this.index = (this.index + (event.key === "ArrowDown" ? 1 : -1) + this.matches.length) % this.matches.length;
        this.render(true);
        this.options[this.index].scrollIntoView({block: "nearest"});
      }
      return true;
    }
  }
  globalThis.ChatSlashCommands = ChatSlashCommands;
})();
