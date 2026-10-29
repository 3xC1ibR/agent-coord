/* Actions on assistant output, without submitting a message or disturbing a selection. */
(function(root) {
  "use strict";
  class ResponseActions {
    constructor({timeline, input, composer, canDiscuss, onHold, onError}) {
      Object.assign(this, {timeline, input, composer, canDiscuss, onHold, onError});
      this.document = timeline.ownerDocument;
      this.view = this.document.defaultView;
      this.button = this.document.createElement("button");
      this.button.type = "button"; this.button.className = "quiet response-discuss";
      this.button.textContent = "Discuss"; this.button.setAttribute("aria-label", "Discuss selected response text");
      this.button.hidden = true;
      timeline.after(this.button);
      this.status = this.document.createElement("span");
      this.status.className = "sr-only"; this.status.setAttribute("role", "status");
      this.button.after(this.status);
      this.button.addEventListener("pointerdown", event => event.preventDefault());
      this.button.addEventListener("click", () => this.discuss());
      timeline.addEventListener("pointerdown", event => {
        if (event.button || event.target.closest("button,a,input,textarea,summary")) return;
        this.pointing = true; this.onHold?.();
      });
      const released = () => { this.pointing = false; this.update(); };
      this.document.addEventListener("pointerup", released);
      this.document.addEventListener("pointercancel", released);
      this.document.addEventListener("selectionchange", () => this.update());
      this.document.addEventListener("keydown", event => {
        if (event.key === "Escape" && !this.button.hidden) {
          this.view.getSelection()?.removeAllRanges(); this.update();
        }
      });
      this.view.addEventListener("resize", () => this.position());
      this.document.addEventListener("scroll", () => this.position(), {capture: true, passive: true});
      this.view.visualViewport?.addEventListener("resize", () => this.position());
      timeline.addEventListener("click", event => {
        const button = event.target.closest("button[data-copy-block]");
        if (button && timeline.contains(button)) this.copy(button);
      });
    }
    // Collect desired nodes without detaching live messages (and their ranges).
    static buffer() { return {childNodes: [], append(...nodes) { this.childNodes.push(...nodes); }}; }
    static commit(container, buffer) {
      const desired = buffer.childNodes, keep = new Set(desired);
      for (const child of [...container.childNodes]) if (!keep.has(child)) child.remove();
      let cursor = container.firstChild;
      for (const child of desired) {
        if (child === cursor) cursor = cursor.nextSibling;
        else container.insertBefore(child, cursor);
      }
    }
    static enhance(body) {
      for (const pre of body.querySelectorAll("pre")) {
        if (!pre.querySelector(":scope > code") || pre.closest(".response-code")) continue;
        const doc = body.ownerDocument, wrap = doc.createElement("div"), actions = doc.createElement("div");
        wrap.className = "response-code"; actions.className = "response-code-actions";
        const button = doc.createElement("button"); button.type = "button"; button.className = "quiet";
        // A generated visual label stays out of native whole-response selections.
        // The accessible name and live copy result remain ordinary DOM attributes/text.
        button.dataset.copyBlock = ""; button.dataset.copyLabel = "Copy"; button.setAttribute("aria-label", "Copy code block");
        actions.append(button); pre.before(wrap); wrap.append(actions, pre);
      }
    }
    selected() {
      const selection = this.view.getSelection();
      if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
      const element = node => node?.nodeType === 1 ? node : node?.parentElement;
      const start = element(selection.anchorNode)?.closest(".message.agent .text");
      const end = element(selection.focusNode)?.closest(".message.agent .text");
      if (!start || start !== end || !this.timeline.contains(start)) return null;
      const text = selection.toString();
      if (!text.trim()) return null;
      return {text, range: selection.getRangeAt(0).cloneRange(), thread: this.thread};
    }
    selecting() {
      const selection = this.view.getSelection();
      return !!selection && !selection.isCollapsed &&
        (this.timeline.contains(selection.anchorNode) || this.timeline.contains(selection.focusNode));
    }
    deferRender(thread, render) {
      if (this.thread !== thread) {
        this.thread = thread; this.pending = null; this.pointing = false; this.selection = null; this.button.hidden = true;
        return false;
      }
      if (this.pointing || this.selecting() || this.copying) { this.pending = render; this.onHold?.(); return true; }
      this.pending = null;
      return false;
    }
    update() {
      const selected = this.selected();
      this.selection = selected;
      this.button.hidden = !selected || !this.canDiscuss();
      if (this.selecting() || this.pointing) this.onHold?.();
      if (!this.button.hidden) this.position();
      if (!this.pointing && !this.selecting() && !this.copying && this.pending) {
        const render = this.pending; this.pending = null;
        this.view.requestAnimationFrame(render);
      }
    }
    position() {
      if (this.button.hidden || !this.selection) return;
      const rect = this.selection.range.getBoundingClientRect(), viewport = this.view.visualViewport;
      const top = viewport?.offsetTop || 0, left = viewport?.offsetLeft || 0;
      const width = viewport?.width || this.view.innerWidth, height = viewport?.height || this.view.innerHeight;
      const gap = Number.parseFloat(this.view.getComputedStyle(this.button).getPropertyValue("--response-action-gap")) || 8;
      const edge = Number.parseFloat(this.view.getComputedStyle(this.button).getPropertyValue("--response-action-edge")) || 12;
      const button = this.button.getBoundingClientRect(), composer = this.composer.getBoundingClientRect();
      const bottom = Math.min(top + height, composer.top > top ? composer.top : top + height);
      this.button.style.left = Math.max(left + edge, Math.min(rect.right - button.width, left + width - button.width - edge)) + "px";
      this.button.style.top = Math.max(top + gap, Math.min(rect.top - button.height - gap, bottom - button.height - gap)) + "px";
    }
    discuss() {
      const selection = this.selection;
      if (!selection || selection.thread !== this.thread || !this.canDiscuss()) return;
      const draft = this.input.value;
      const separator = !draft || draft.endsWith("\n\n") ? "" : draft.endsWith("\n") ? "\n" : "\n\n";
      const quote = selection.text.split("\n").map(line => "> " + line).join("\n");
      const value = draft + separator + quote + "\n\n";
      if (this.input.maxLength > 0 && value.length > this.input.maxLength) {
        this.onError(new Error("The selection is too long for this draft. Select a shorter passage.")); return;
      }
      this.input.value = value;
      this.input.dispatchEvent(new this.view.Event("input", {bubbles: true}));
      this.view.getSelection()?.removeAllRanges();
      this.input.focus({preventScroll: true}); this.input.setSelectionRange(value.length, value.length);
      this.status.textContent = "Selection added to your draft.";
      this.update();
    }
    async copy(button) {
      const code = button.closest(".response-code")?.querySelector("pre > code");
      if (!code || button.disabled) return;
      const text = code.textContent;
      this.copying = true; button.disabled = true;
      try {
        if (this.view.navigator.clipboard?.writeText) {
          try { await this.view.navigator.clipboard.writeText(text); }
          catch { this.copyFallback(text); }
        } else this.copyFallback(text);
        button.dataset.copyLabel = "Copied"; this.status.textContent = "Code block copied.";
        this.view.setTimeout(() => { button.dataset.copyLabel = "Copy"; }, 2000);
      } catch {
        button.dataset.copyLabel = "Try again";
        this.onError(new Error("Couldn’t copy this block. Select its text and use Copy."));
      } finally { button.disabled = false; this.copying = false; this.update(); }
    }
    copyFallback(text) {
      const doc = this.document, selected = this.view.getSelection(), focus = doc.activeElement;
      const ranges = selected ? Array.from({length: selected.rangeCount}, (_, i) => selected.getRangeAt(i).cloneRange()) : [];
      const area = doc.createElement("textarea"); area.className = "response-copy-buffer"; area.value = text;
      area.setAttribute("aria-hidden", "true"); doc.body.append(area);
      try {
        area.focus({preventScroll: true}); area.select();
        if (!doc.execCommand("copy")) throw new Error("Clipboard unavailable");
      } finally {
        area.remove(); focus?.focus({preventScroll: true});
        if (selected) { selected.removeAllRanges(); for (const range of ranges) if (range.startContainer.isConnected) selected.addRange(range); }
      }
    }
  }
  if (typeof module !== "undefined" && module.exports) module.exports = {ResponseActions};
  else root.ResponseActions = ResponseActions;
})(globalThis);
