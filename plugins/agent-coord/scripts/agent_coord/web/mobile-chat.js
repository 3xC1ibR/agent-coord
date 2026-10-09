"use strict";

// Move the existing controls into a native dialog so their state and handlers
// remain shared with desktop. Only the transcript scrolls in the mobile frame.
class MobileChat {
  constructor({document: doc = document, view = window, onBack, onLayout, onContextLayout}) {
    this.doc = doc;
    this.view = view;
    this.onLayout = onLayout;
    this.onContextLayout = onContextLayout;
    this.$ = id => doc.getElementById(id);
    this.media = view.matchMedia("(max-width: 720px), (max-width: 1100px) and (hover: none) and (pointer: coarse)");
    this.dialog = this.$("mobile-thread-sheet");
    this.trigger = this.$("mobile-chat-actions");
    this.panel = this.$("context-panel");
    this.actions = doc.querySelector(".session-actions");
    this.info = doc.querySelector(".session-statusbar");
    this.homes = [this.actions, this.panel, this.info].map(element => {
      const marker = doc.createComment("mobile chat control position");
      element.before(marker);
      return {element, marker};
    });
    this.$("mobile-chat-back").onclick = onBack;
    this.trigger.onclick = () => this.open("actions");
    this.$("mobile-show-context").onclick = () => this.open("context");
    this.$("mobile-sheet-back").onclick = () => this.open("actions");
    this.dialog.addEventListener("close", () => {
      this.trigger.setAttribute("aria-expanded", String(this.dialog.open));
      this.layoutContext();
    });
    this.dialog.addEventListener("click", event => {
      if (event.target !== this.dialog) return;
      const bounds = this.dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) this.close();
    });
    // Close before the original handler opens a confirmation or edit dialog.
    this.dialog.addEventListener("click", event => {
      const button = event.target.closest("button");
      if (button && !button.disabled && (this.actions.contains(button) || this.panel.contains(button) || this.info.contains(button))) this.close();
    }, {capture: true});
    this.media.addEventListener("change", () => this.sync());
    view.visualViewport?.addEventListener("resize", () => this.viewport());
    view.visualViewport?.addEventListener("scroll", () => this.viewport());
    this.sync();
  }

  get matches() { return this.media.matches; }

  sync() {
    this.close();
    this.doc.body.classList.toggle("mobile-chat", this.matches);
    if (this.matches) {
      this.$("mobile-sheet-actions").append(this.actions);
      this.$("mobile-sheet-context").append(this.panel);
      this.$("mobile-sheet-info").append(this.info);
      this.layoutContext();
    } else {
      for (const {element, marker} of this.homes) marker.after(element);
      this.onContextLayout();
    }
    this.viewport();
  }

  viewport() {
    const viewport = this.view.visualViewport;
    const style = this.doc.body.style;
    if (!this.matches || !viewport) {
      style.removeProperty("--mobile-viewport-height");
      style.removeProperty("--mobile-viewport-top");
    } else if (viewport.scale === 1) {
      // iOS changes the visual viewport, rather than 100dvh, for its keyboard.
      style.setProperty("--mobile-viewport-height", viewport.height + "px");
      style.setProperty("--mobile-viewport-top", viewport.offsetTop + "px");
    }
    if (this.frame) this.view.cancelAnimationFrame(this.frame);
    this.frame = this.view.requestAnimationFrame(() => { this.frame = null; this.onLayout(); });
  }

  open(mode) {
    if (!this.matches) return;
    this.mode = mode;
    const context = mode === "context";
    this.$("mobile-sheet-title").textContent = context ? "Thread context" : "Thread actions";
    this.$("mobile-sheet-actions").hidden = context;
    this.$("mobile-show-context").hidden = context;
    this.$("mobile-sheet-info").hidden = context;
    this.$("mobile-sheet-context").hidden = !context;
    this.$("mobile-sheet-back").hidden = !context;
    // Prepare context before showModal chooses its initial focus target.
    this.panel.hidden = !context;
    if (context) this.$("thread-context").open = true;
    if (!this.dialog.open) this.dialog.showModal();
    this.trigger.setAttribute("aria-expanded", "true");
    this.layoutContext();
    if (context) this.$("mobile-sheet-back").focus({preventScroll: true});
  }

  close() { if (this.dialog.open) this.dialog.close(); }

  layoutContext() {
    if (!this.matches) return;
    this.panel.hidden = !this.dialog.open || this.mode !== "context";
    this.$("conversation").classList.add("context-collapsed");
  }

  status(label, key) {
    const status = this.$("mobile-chat-status");
    status.setAttribute("aria-label", label);
    status.title = label;
    status.dataset.status = key;
  }
}

if (typeof module !== "undefined" && module.exports) module.exports = {MobileChat};
