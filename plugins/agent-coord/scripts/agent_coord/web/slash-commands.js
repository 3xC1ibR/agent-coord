"use strict";
(() => {
  const commands = [
    {name: "/cd", arguments: "[directory]", description: "Show or change the working directory"},
    {name: "/model", arguments: "[model-id] [effort]", description: "List models or choose one for the next message"},
    {name: "/effort", arguments: "[level]", description: "Show or change reasoning effort"},
    {name: "/permissions", arguments: "[default|yolo]", description: "Show or change session permissions"},
    {name: "/fork", arguments: "", description: "Fork this Codex conversation into a new thread"},
    {name: "/close", arguments: "", description: "Stop and close this thread, keeping its history"},
    {name: "/help", arguments: "", description: "Show available commands and usage"},
  ];

  class ChatSlashCommands {
    constructor({input, menu, getThread, getSession = () => ({}), loadModels = async () => [], canComplete, onChange}) {
      Object.assign(this, {input, menu, getThread, getSession, loadModels, canComplete, onChange});
      this.catalogs = new Map();
      this.loading = new Set();
      this.matches = [];
      this.index = 0;
      input.addEventListener("input", event => {
        this.dismissed = null;
        if (event.isComposing) this.close();
        else this.render();
      });
      input.addEventListener("compositionstart", () => { this.composing = true; this.close(); });
      input.addEventListener("compositionend", () => { this.composing = false; this.render(); });
      input.addEventListener("focus", () => { this.dismissed = null; this.render(); });
      input.addEventListener("blur", () => this.close());
      input.addEventListener("click", () => this.render());
      input.addEventListener("keyup", () => this.render());
      input.ownerDocument.addEventListener("selectionchange", () => this.render());
      // Keep the caret in the composer when choosing a suggestion with a pointer.
      menu.addEventListener("mousedown", event => event.preventDefault());
    }

    query() {
      const input = this.input;
      if (this.composing || !this.canComplete() || input.disabled || input.ownerDocument.activeElement !== input ||
          input.selectionStart !== input.selectionEnd) return null;
      const command = /^([ \t]*)(\/[^\s]*)/.exec(input.value), caret = input.selectionStart;
      if (!command || caret <= command[1].length) return null;
      if (caret <= command[0].length) return {kind: "command",
        prefix: input.value.slice(command[1].length, caret).toLowerCase(), start: command[1].length, end: command[0].length};
      const name = command[2].toLowerCase();
      if (!["/model", "/effort"].includes(name) || /[\r\n]/.test(input.value.slice(0, caret))) return null;
      // Track token boundaries so editing an argument keeps its neighbors intact.
      const tokens = [...input.value.matchAll(/"[^"\r\n]*(?:"|$)|'[^'\r\n]*(?:'|$)|[^\s]+/g)];
      let index = tokens.findIndex(token => caret >= token.index && caret <= token.index + token[0].length);
      if (index < 0) index = tokens.filter(token => token.index < caret).length;
      const token = tokens[index], start = token && token.index <= caret ? token.index : caret;
      const end = start === token?.index ? start + token[0].length : caret;
      const unquote = value => value.replace(/^["']|["']$/g, "");
      const kind = name === "/model" && index === 1 ? "model" :
        (name === "/effort" && index === 1) || (name === "/model" && index === 2) ? "effort" : null;
      if (!kind) return null;
      return {kind, start, end, prefix: unquote(input.value.slice(start, caret)).toLowerCase(),
        model: name === "/model" && index === 2 ? unquote(tokens[1][0]) : this.getSession()?.model};
    }

    models() {
      const client = this.getSession()?.client || "codex";
      if (!this.catalogs.has(client) && !this.loading.has(client)) {
        this.loading.add(client);
        Promise.resolve().then(() => this.loadModels(client)).then(models => {
          this.loading.delete(client);
          this.catalogs.set(client, models);
          // Re-evaluate the current draft and provider, not the one that started the request.
          this.render();
        }, () => {
          // Leave manual commands usable; another input/focus can retry the request.
          this.loading.delete(client);
        });
      }
      return this.catalogs.get(client) || [];
    }

    suggestions(query) {
      if (query.kind === "command") return commands;
      const models = this.models();
      if (query.kind === "model") return models.map(model => ({name: model.model,
        arguments: model.displayName || "", description: model.description || ""}));
      const model = models.find(model => model.model === query.model);
      return (model?.supportedReasoningEfforts || []).map(effort => ({name: effort.reasoningEffort,
        arguments: "", description: effort.description || ""}));
    }

    close() {
      this.menu.hidden = true;
      this.input.setAttribute("aria-expanded", "false");
      this.input.removeAttribute("aria-activedescendant");
    }

    render(force = false) {
      const query = this.query();
      const key = JSON.stringify([this.getThread(), this.input.value, this.input.selectionStart]);
      if (!query || this.dismissed === key) { this.close(); return; }
      const matches = this.suggestions(query).filter(command => command.name.toLowerCase().startsWith(query.prefix));
      if (!matches.length) { this.close(); return; }
      const signature = JSON.stringify(matches);
      if (!force && !this.menu.hidden && key === this.key && signature === this.signature) return;
      if (key !== this.key || signature !== this.signature) this.index = 0;
      this.signature = signature;
      this.key = key;
      this.matches = matches;
      this.menu.hidden = false;
      this.menu.setAttribute("aria-label", query.kind === "model" ? "Models" : query.kind === "effort" ? "Reasoning effort" : "Slash commands");
      this.input.setAttribute("aria-expanded", "true");
      this.menu.replaceChildren();
      const doc = this.input.ownerDocument;
      for (const [index, command] of matches.entries()) {
        const option = doc.createElement("div");
        option.id = this.menu.id + "-" + index;
        option.className = "slash-command";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", String(index === this.index));
        const name = doc.createElement("strong"); name.textContent = command.name;
        const usage = doc.createElement("span"); usage.textContent = command.arguments;
        const description = doc.createElement("small"); description.textContent = command.description;
        option.append(name, usage, description);
        option.addEventListener("click", () => this.choose(index));
        this.menu.append(option);
      }
      this.input.setAttribute("aria-activedescendant", this.menu.id + "-" + this.index);
    }

    choose(index) {
      const query = this.query(), command = this.matches[index];
      if (!query || !command || this.menu.hidden) return;
      const suffix = this.input.value.slice(query.end);
      // Replace only the active token; retain arguments already entered.
      const completion = command.name + (suffix ? "" : " ");
      this.input.setRangeText(completion, query.start, query.end, "end");
      const next = this.query();
      this.dismissed = next && next.kind !== query.kind ? null :
        JSON.stringify([this.getThread(), this.input.value, this.input.selectionStart]);
      this.close();
      this.onChange();
      this.render();
    }

    keydown(event) {
      if (event.isComposing || this.composing || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return false;
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
      if (event.key === "Enter" || event.key === "Tab") this.choose(this.index);
      else {
        this.index = (this.index + (event.key === "ArrowDown" ? 1 : -1) + this.matches.length) % this.matches.length;
        this.render(true);
        this.menu.children[this.index].scrollIntoView({block: "nearest"});
      }
      return true;
    }
  }
  globalThis.ChatSlashCommands = ChatSlashCommands;
})();
