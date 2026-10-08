/* Desktop-only command palette; injected before bridge.js. */
(() => {
  "use strict";
  const normalize = value => String(value || "").normalize("NFKD").replace(/\p{M}/gu, "").toLocaleLowerCase();
  const styles = `
    #desktop-command-dialog { width: min(640px, calc(100vw - 48px)); max-height: calc(100vh - 96px);
      margin: 64px auto auto; padding: 0; color: var(--ink, #293c34); background: var(--surface, #fffefa);
      border: 1px solid var(--line, #dddfd4); border-radius: 12px; box-shadow: 0 24px 80px #142b3240; overflow: hidden; }
    #desktop-command-dialog::backdrop { background: #162b2366; backdrop-filter: blur(3px); }
    #desktop-command-dialog .palette-heading { display: flex; align-items: center; gap: 12px;
      padding: 16px 20px 8px; }
    #desktop-command-dialog h2 { margin: 0; font: 12px -apple-system, BlinkMacSystemFont, sans-serif;
      letter-spacing: 0; color: var(--muted, #657060); }
    #desktop-command-dialog .palette-close { margin-left: auto; padding: 3px 7px; font-size: 11px; }
    #desktop-command-input { display: block; width: calc(100% - 40px); margin: 0 20px 16px; padding: 10px 0;
      border: 0; border-radius: 0; outline: none; box-shadow: none; background: transparent; font-size: 19px; }
    #desktop-command-input:focus-visible { box-shadow: 0 2px var(--accent, #2f624b); }
    #desktop-command-results { margin: 0; padding: 6px; list-style: none; overflow-y: auto;
      max-height: min(390px, calc(100vh - 285px)); border-block: 1px solid var(--line, #dddfd4); }
    #desktop-command-results [role=option] { display: flex; align-items: center; gap: 14px;
      padding: 12px 14px; border-radius: 6px; cursor: pointer; }
    #desktop-command-results [role=option][aria-selected=true] { background: var(--tint, #eaf0e5); }
    #desktop-command-results .palette-copy { flex: 1; min-width: 0; }
    #desktop-command-results .palette-label { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    #desktop-command-results .palette-detail { display: block; margin-top: 3px; color: var(--muted, #657060);
      font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    #desktop-command-results kbd { flex-shrink: 0; font: 11px var(--mono, monospace); color: var(--muted, #657060); }
    #desktop-command-status { margin: 0; padding: 10px 20px 4px; color: var(--muted, #657060); font-size: 11px; }
    #desktop-command-dialog .palette-help { margin: 0; padding: 6px 20px 14px; color: var(--muted, #657060);
      font: 11px var(--mono, monospace); }
  `;

  class DesktopCommandPalette {
    constructor({document, actions, loadThreads, selectThread, onError}) {
      Object.assign(this, {document, actions, loadThreads, selectThread, onError});
      this.entries = []; this.results = []; this.active = 0; this.generation = 0;
      const element = (tag, id, text) => {
        const node = document.createElement(tag);
        if (id) node.id = id;
        if (text) node.textContent = text;
        return node;
      };
      const style = element("style", "desktop-command-style", styles);
      document.head.append(style);
      this.dialog = element("dialog", "desktop-command-dialog");
      this.dialog.setAttribute("aria-labelledby", "desktop-command-title");
      const heading = element("div"); heading.className = "palette-heading";
      const title = element("h2", "desktop-command-title", "Commands and threads");
      const close = element("button", null, "Esc");
      close.type = "button"; close.className = "palette-close";
      close.setAttribute("aria-label", "Close command palette");
      close.onclick = () => this.close();
      heading.append(title, close);
      this.input = element("input", "desktop-command-input");
      this.input.type = "text"; this.input.placeholder = "Search commands or jump to a thread…";
      this.input.autocomplete = "off"; this.input.spellcheck = false;
      for (const [name, value] of Object.entries({role: "combobox", "aria-label": "Search commands and threads",
        "aria-autocomplete": "list", "aria-controls": "desktop-command-results", "aria-expanded": "false"})) {
        this.input.setAttribute(name, value);
      }
      this.list = element("ul", "desktop-command-results");
      this.list.setAttribute("role", "listbox"); this.list.setAttribute("aria-label", "Commands and threads");
      this.status = element("p", "desktop-command-status"); this.status.setAttribute("role", "status");
      const help = element("p", null, "↑ ↓ Navigate    ↵ Select    Esc Close"); help.className = "palette-help";
      this.dialog.append(heading, this.input, this.list, this.status, help);
      document.body.append(this.dialog);
      this.input.addEventListener("input", () => this.render(true));
      this.input.addEventListener("keydown", event => {
        if (event.isComposing) return;
        if (["ArrowDown", "ArrowUp"].includes(event.key)) {
          event.preventDefault();
          if (this.results.length) this.setActive((this.active + (event.key === "ArrowDown" ? 1 : -1) + this.results.length) % this.results.length);
        } else if (event.key === "Enter") {
          event.preventDefault(); this.activate(this.active);
        } else if (event.key === "Escape") {
          event.preventDefault(); this.close();
        }
      });
      this.dialog.addEventListener("cancel", event => { event.preventDefault(); this.close(); });
      this.dialog.addEventListener("click", event => {
        if (event.target !== this.dialog) return;
        const rect = this.dialog.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) this.close();
      });
    }

    toggle() {
      if (this.dialog.open) { this.close(); return true; }
      if (this.document.querySelector("dialog[open]")) return false;
      this.returnFocus = this.document.activeElement;
      this.entries = this.actions().map(entry => ({...entry, kind: "Action"}));
      this.input.value = ""; this.loading = true; this.failure = false;
      const controller = this.controller = new AbortController();
      const generation = ++this.generation;
      this.dialog.showModal();
      this.input.setAttribute("aria-expanded", "true");
      this.render(true); this.input.focus();
      Promise.resolve().then(() => this.loadThreads(controller.signal)).then(threads => {
        if (!this.dialog.open || generation !== this.generation) return;
        const seen = new Set();
        for (const thread of threads) {
          if (typeof thread.thread_id !== "string" || !thread.thread_id || seen.has(thread.thread_id)) continue;
          seen.add(thread.thread_id);
          this.entries.push({id: "thread:" + thread.thread_id, kind: "Thread", label: thread.title || "Untitled thread",
            detail: [thread.project_name, thread.repository_name, thread.cwd].filter(Boolean).join(" · "),
            run: () => this.selectThread(thread.thread_id)});
        }
        this.loading = false; this.render();
      }).catch(() => {
        if (!this.dialog.open || generation !== this.generation) return;
        this.loading = false; this.failure = true; this.render();
      });
      return true;
    }

    close(restoreFocus = true) {
      if (!this.dialog.open) return;
      ++this.generation; this.controller?.abort();
      this.dialog.close(); this.input.setAttribute("aria-expanded", "false");
      if (restoreFocus && this.returnFocus?.isConnected && !this.returnFocus.disabled) this.returnFocus.focus({preventScroll: true});
      this.returnFocus = null;
    }

    render(reset = false) {
      const selected = reset ? null : this.results[this.active]?.id;
      const query = normalize(this.input.value).trim(), words = query.split(/\s+/).filter(Boolean);
      const matches = this.entries.filter(entry => words.every(word =>
        normalize(entry.label + " " + (entry.detail || "") + " " + (entry.keywords || "")).includes(word)));
      if (query) matches.sort((a, b) => Number(!normalize(a.label).startsWith(query)) - Number(!normalize(b.label).startsWith(query)));
      this.results = matches.slice(0, 60);
      this.list.replaceChildren();
      for (const [index, entry] of this.results.entries()) {
        const row = this.document.createElement("li"); row.id = "desktop-command-option-" + index;
        row.setAttribute("role", "option"); row.setAttribute("aria-selected", "false");
        row.dataset.entry = entry.id;
        const copy = this.document.createElement("span"); copy.className = "palette-copy";
        const label = this.document.createElement("span"); label.className = "palette-label"; label.textContent = entry.label;
        const detail = this.document.createElement("span"); detail.className = "palette-detail";
        detail.textContent = entry.kind + (entry.detail ? " · " + entry.detail : "");
        copy.append(label, detail); row.append(copy);
        if (entry.shortcut) { const key = this.document.createElement("kbd"); key.textContent = entry.shortcut; row.append(key); }
        row.addEventListener("mousemove", () => this.setActive(index, false));
        row.addEventListener("mousedown", event => event.preventDefault());
        row.addEventListener("click", () => this.activate(index));
        this.list.append(row);
      }
      this.setActive(Math.max(0, this.results.findIndex(entry => entry.id === selected)), false);
      const count = matches.length > 60 ? "Showing 60 matches — keep typing to narrow the list." :
        matches.length ? matches.length + " match" + (matches.length === 1 ? "" : "es") : "No matching commands or threads.";
      this.status.textContent = this.loading ? "Loading threads…" : this.failure ? "Threads could not load. Commands are still available; reopen to retry." : count;
    }

    setActive(index, scroll = true) {
      this.active = index;
      const rows = Array.from(this.list.children);
      for (const [i, row] of rows.entries()) row.setAttribute("aria-selected", String(i === index));
      if (rows[index]) {
        this.input.setAttribute("aria-activedescendant", rows[index].id);
        if (scroll) rows[index].scrollIntoView({block: "nearest"});
      } else this.input.removeAttribute("aria-activedescendant");
    }

    activate(index) {
      if (!this.dialog.open || !this.results[index]) return;
      const entry = this.results[index];
      this.close(false);
      Promise.resolve().then(() => entry.run()).then(result => {
        if (result === false) throw new Error("This command is no longer available.");
      }).catch(this.onError);
    }
  }
  globalThis.DesktopCommandPalette = DesktopCommandPalette;
  if (typeof module !== "undefined") module.exports = DesktopCommandPalette;
})();
