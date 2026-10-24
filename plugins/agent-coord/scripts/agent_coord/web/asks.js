"use strict";
(() => {
  function formatAge(seconds) {
    if (!Number.isFinite(seconds) || seconds < 60) return "just now";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h";
    return Math.floor(seconds / 86400) + "d";
  }
  class AsksUI {
    constructor({document, api, openThread, onError, button, badge}) {
      Object.assign(this, {document, api, openThread, onError});
      this.items = [];
      this.sequence = 0;
      this.pending = new Set();
      this.rows = new Map();
      this.renderedIds = null;
      this.button = button || document.getElementById("open-asks");
      this.badge = badge || document.getElementById("asks-count");
      this.dialog = this.node("dialog", "", "asks-dialog");
      this.dialog.setAttribute("aria-labelledby", "asks-heading");
      this.document.body.append(this.dialog);
      if (this.button) this.button.onclick = () => this.open().catch(onError);
      this.dialog.addEventListener?.("close", () => clearInterval(this.timer));
    }
    node(tag, text, className) {
      const element = this.document.createElement(tag);
      if (text) element.textContent = text;
      if (className) element.className = className;
      return element;
    }
    control(text, callback, className = "quiet") {
      const button = this.node("button", text, className); button.type = "button";
      button.onclick = () => Promise.resolve().then(callback).catch(this.onError);
      return button;
    }
    async refresh() {
      const sequence = ++this.sequence;
      const data = await this.api("asks");
      if (sequence !== this.sequence) return;
      this.items = data.data || [];
      this.updateBadge();
      if (this.dialog.open) this.render();
    }
    updateBadge() {
      const count = this.items.length, blocking = this.items.filter(item => item.blocking).length;
      if (this.badge) {
        this.badge.hidden = !count;
        this.badge.textContent = count ? String(count) : "";
        this.badge.classList?.toggle("asks-count-blocking", blocking > 0);
      }
      if (this.button) {
        this.button.title = count ? count + (count === 1 ? " decision" : " decisions") + " waiting for you" + (blocking ? " · " + blocking + " blocking" : "") : "Decisions";
      }
    }
    async open() {
      await this.refresh();
      this.render();
      if (!this.dialog.open) this.dialog.showModal();
      clearInterval(this.timer);
      this.timer = setInterval(() => { if (!this.document.hidden) this.refresh().catch(this.onError); }, 5000);
    }
    close() { clearInterval(this.timer); this.dialog.close(); }
    render() {
      const dialog = this.dialog;
      const ids = this.items.map(ask => ask.id).join(",");
      for (const ask of this.items) {
        if (!this.rows.has(ask.id)) this.rows.set(ask.id, this.row(ask));
        const age = Array.from(this.rows.get(ask.id).querySelectorAll("span"))
          .find(node => node.className === "ask-age");
        if (age) age.textContent = formatAge(ask.age_seconds);
      }
      // Reuse answer forms across polls so typing, focus, and selection survive.
      if (this.renderedIds === ids) return;
      this.renderedIds = ids;
      const active = this.document.activeElement;
      const selection = active?.selectionStart;
      const selectionEnd = active?.selectionEnd;
      for (const id of this.rows.keys()) if (!this.items.some(ask => ask.id === id)) this.rows.delete(id);
      dialog.replaceChildren();
      const head = this.node("div", "", "asks-head");
      const heading = this.node("h2", "Decisions"); heading.id = "asks-heading";
      head.append(heading, this.control("Close", () => this.close()));
      dialog.append(head);
      if (!this.items.length) {
        dialog.append(this.node("p", "No open decisions. Agents will ask here when they need you.", "asks-empty"));
        return;
      }
      dialog.append(this.node("p", "Blocking decisions stop an agent until you answer. Non-blocking ones keep working on the stated default.", "asks-intro"));
      const list = this.node("div", "", "asks-list");
      for (const ask of this.items) list.append(this.rows.get(ask.id));
      dialog.append(list);
      if (active && dialog.contains?.(active)) {
        active.focus();
        if (selection != null) active.setSelectionRange?.(selection, selectionEnd);
      }
    }
    row(ask) {
      const row = this.node("article", "", "ask-row" + (ask.blocking ? " ask-blocking" : ""));
      row.dataset.id = String(ask.id);
      const meta = this.node("div", "", "ask-meta");
      meta.append(this.node("span", ask.blocking ? "Blocking" : "Non-blocking", "ask-kind"));
      const thread = this.control(ask.thread_title, () => { this.close(); return this.openThread(ask.session_id); }, "ask-thread");
      thread.title = "Open this thread";
      meta.append(thread);
      if (ask.project_name) meta.append(this.node("span", ask.project_name, "ask-project"));
      meta.append(this.node("span", formatAge(ask.age_seconds), "ask-age"));
      row.append(meta, this.node("p", ask.question, "ask-question"));
      if (ask.context) row.append(this.node("p", ask.context, "ask-context"));
      const answers = this.node("div", "", "ask-answers");
      for (const option of ask.options) answers.append(this.control(option, () => this.answer(ask, {option}, row), "ask-option"));
      const form = this.node("form", "", "ask-free");
      const input = this.node("input"); input.type = "text"; input.maxLength = 4000;
      input.placeholder = ask.options.length ? "Or answer in your own words…" : "Your answer…";
      input.setAttribute("aria-label", "Answer: " + ask.question);
      const send = this.node("button", "Answer", "primary"); send.type = "submit";
      form.append(input, send);
      form.onsubmit = event => {
        event?.preventDefault?.();
        const text = input.value.trim();
        if (!text) return;
        return this.answer(ask, {answer: text}, row).catch(this.onError);
      };
      answers.append(form);
      row.append(answers);
      return row;
    }
    async answer(ask, body, row) {
      if (this.pending.has(ask.id)) return;
      this.pending.add(ask.id);
      for (const button of row.querySelectorAll?.("button, input") || []) button.disabled = true;
      try {
        await this.api("asks/" + encodeURIComponent(ask.id), body);
        this.items = this.items.filter(item => item.id !== ask.id);
        this.updateBadge();
        this.render();
      } catch (error) {
        for (const button of row.querySelectorAll?.("button, input") || []) button.disabled = false;
        throw error;
      } finally { this.pending.delete(ask.id); }
    }
  }
  if (typeof module !== "undefined") module.exports = {AsksUI, formatAge};
  globalThis.AsksUI = AsksUI;
})();
