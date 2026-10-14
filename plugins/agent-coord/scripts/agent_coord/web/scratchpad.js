"use strict";

class ThreadScratchpad {
  constructor({panel, input, status, api, view = window, path, draftPrefix = "agent-coord.scratchpad-draft:", onRender}) {
    Object.assign(this, {panel, input, status, api, view, draftPrefix, onRender});
    if (path) this.path = entry => path(entry.id);
    this.entries = new Map();
    this.selected = null;
    input.addEventListener("input", () => {
      const entry = this.entries.get(this.selected);
      if (!entry?.loaded) return;
      entry.text = input.value;
      entry.error = "";
      this.remember(entry);
      this.schedule(entry, 500);
      this.render();
    });
    input.addEventListener("blur", () => this.flush(this.entries.get(this.selected)));
    view.addEventListener("pagehide", () => this.flushAll(true));
    view.document.addEventListener("visibilitychange", () => {
      if (view.document.visibilityState === "hidden") this.flushAll(true);
    });
    view.addEventListener("online", () => {
      const entry = this.entries.get(this.selected);
      if (entry && !entry.loaded) this.load(entry);
      this.flushAll();
    });
  }

  key(entry) { return this.draftPrefix + entry.id; }
  remember(entry) {
    try {
      if (entry.text === entry.saved && !entry.saving) this.view.sessionStorage.removeItem(this.key(entry));
      else this.view.sessionStorage.setItem(this.key(entry), entry.text);
    } catch { /* Saving to the server still works without browser storage. */ }
  }

  select(id) {
    if (id === this.selected) return;
    this.flush(this.entries.get(this.selected));
    this.selected = id;
    this.panel.hidden = !id;
    if (!id) return;
    let entry = this.entries.get(id);
    if (!entry) {
      let draft = null;
      entry = {id, text: "", saved: "", loaded: false, saving: false, loading: false, error: ""};
      try { draft = this.view.sessionStorage.getItem(this.key(entry)); } catch { /* Optional recovery. */ }
      entry.draft = draft;
      this.entries.set(id, entry);
    }
    if (!entry.saving && (!entry.loaded || entry.text === entry.saved)) this.load(entry);
    this.render();
  }

  path(entry) { return "threads/" + encodeURIComponent(entry.id) + "/scratchpad"; }
  schedule(entry, delay) {
    this.view.clearTimeout(entry.timer);
    entry.timer = this.view.setTimeout(() => entry.loaded ? this.flush(entry) : this.load(entry), delay);
  }

  async load(entry) {
    if (entry.loading) return;
    entry.loading = true;
    entry.loaded = false;
    entry.error = "";
    this.render();
    try {
      const result = await this.api(this.path(entry));
      entry.saved = result.text;
      entry.text = entry.draft ?? result.text;
      entry.draft = null;
      entry.loaded = true;
      this.remember(entry);
      if (entry.text !== entry.saved) this.schedule(entry, 500);
    } catch {
      entry.error = "Couldn’t load. Retrying…";
      this.schedule(entry, 2500);
    } finally {
      entry.loading = false;
      this.render();
    }
  }

  async flush(entry, keepalive = false) {
    if (!entry?.loaded || entry.saving) return;
    this.view.clearTimeout(entry.timer);
    if (entry.text === entry.saved) return;
    const text = entry.text;
    entry.saving = true;
    entry.error = "";
    this.render();
    try {
      await this.api(this.path(entry), {text}, {keepalive});
      entry.saved = text;
    } catch {
      entry.saved = null; // The server may have accepted a request whose response was lost.
      entry.error = "Couldn’t save. Retrying…";
    } finally {
      entry.saving = false;
      this.remember(entry);
      // Serialize saves per thread so an older request cannot overwrite later typing.
      if (entry.text !== entry.saved) this.schedule(entry, entry.error ? 2500 : 0);
      this.render();
    }
  }

  flushAll(keepalive = false) {
    for (const entry of this.entries.values()) this.flush(entry, keepalive);
  }

  render() {
    const entry = this.entries.get(this.selected);
    if (!entry) return;
    this.input.disabled = !entry.loaded;
    const text = entry.loaded ? entry.text : (entry.draft ?? entry.text);
    if (this.input.value !== text) this.input.value = text;
    this.status.textContent = entry.error || (!entry.loaded ? "Loading…" :
      entry.saving || entry.text !== entry.saved ? "Saving…" : "Saved");
    this.onRender?.(entry);
  }
}

if (typeof module !== "undefined") module.exports = {ThreadScratchpad};
