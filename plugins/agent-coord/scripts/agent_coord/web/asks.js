"use strict";
/* Workspace entry to the asks view: the sidebar count and the embedded page. */
(() => {
  const copy = typeof module !== "undefined" ? require("./asks-copy.js").AsksCopy : globalThis.AsksCopy;
  class AsksUI {
    constructor({document, api, view = globalThis, navigate, onError, entry, label, badge, shortcut, dialog, frame}) {
      Object.assign(this, {document, api, view, navigate, onError});
      const $ = id => document.getElementById(id);
      this.items = [];
      this.sequence = 0;
      this.entry = entry || $("open-asks-view");
      this.label = label || $("asks-entry-label");
      this.badge = badge || $("asks-count");
      this.shortcut = shortcut || $("open-asks");
      this.dialog = dialog || $("asks-dialog");
      this.frame = frame || $("asks-frame");
      if (this.label) this.label.textContent = copy.viewName;
      this.dialog?.setAttribute("aria-label", copy.viewName);
      this.frame?.setAttribute("title", copy.viewName);
      this.shortcut?.setAttribute("aria-label", copy.viewName);
      for (const control of [this.entry, this.shortcut]) {
        if (control) control.onclick = event => { event?.preventDefault?.(); this.open(); };
      }
      view.addEventListener?.("message", event => this.receive(event));
      this.dialog?.addEventListener?.("close", () => this.closed());
      this.updateBadge();
    }
    async refresh() {
      const sequence = ++this.sequence;
      const data = await this.api("asks");
      if (sequence !== this.sequence) return;
      this.items = data.data || [];
      this.updateBadge();
    }
    updateBadge() {
      // Snoozed asks are out of the open list until their time.
      const waiting = this.items.filter(item => !item.snoozed);
      const count = waiting.length, blocking = waiting.filter(item => item.blocking).length;
      if (this.badge) {
        this.badge.hidden = !count;
        this.badge.textContent = count ? String(count) : "";
        this.badge.classList?.toggle("asks-count-blocking", blocking > 0);
      }
      const title = copy.entryTitle(count, blocking);
      for (const control of [this.entry, this.shortcut]) if (control) control.title = title;
      this.entry?.setAttribute("aria-label", count ? copy.viewName + ": " + title : copy.viewName);
    }
    open() {
      const backdrop = this.document.getElementById("sidebar-backdrop");
      if (backdrop && !backdrop.hidden) backdrop.click();
      this.frame.src = "/asks?embedded=1";
      if (!this.dialog.open) this.dialog.showModal();
    }
    receive(event) {
      if (event.origin !== this.view.location.origin || event.source !== this.frame?.contentWindow) return;
      const {type, thread_id: threadId, url} = event.data || {};
      if (type === "ribbon-field-close-asks") this.dialog.close();
      else if (type === "ribbon-field-asks-thread" && typeof threadId === "string") {
        this.follow("agentcoord://thread/" + encodeURIComponent(threadId));
      } else if (type === "ribbon-field-asks-navigate" && typeof url === "string" && url.startsWith("agentcoord://")) {
        this.follow(url);
      }
    }
    follow(url) {
      this.dialog.close();
      // The click happened inside the embedded page, so name the surface for the activity record here.
      this.view.agentCoordActivity?.markIntent?.("decisions");
      Promise.resolve(this.navigate(url)).catch(this.onError);
    }
    closed() {
      this.frame.removeAttribute("src");
      const visible = this.entry?.getClientRects?.().length;
      (visible ? this.entry : this.document.getElementById("menu-toggle"))?.focus?.({preventScroll: true});
      this.refresh().catch(this.onError);
    }
  }
  if (typeof module !== "undefined") module.exports = {AsksUI};
  globalThis.AsksUI = AsksUI;
})();
