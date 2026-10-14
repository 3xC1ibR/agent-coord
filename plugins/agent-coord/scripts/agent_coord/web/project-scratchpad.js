"use strict";

class ProjectScratchpad {
  constructor({document, view = window, api}) {
    Object.assign(this, {document, view});
    const $ = id => document.getElementById(id);
    this.page = $("project-scratchpad-page");
    this.dialog = $("project-scratchpad-dialog");
    this.editor = $("project-scratchpad-editor");
    this.input = $("project-scratchpad-text");
    this.projectLabel = $("scratchpad-project-name");
    this.closeButton = $("close-project-scratchpad");
    this.pad = new ThreadScratchpad({panel: this.editor, input: this.input,
      status: $("project-scratchpad-status"), view,
      draftPrefix: "agent-coord.project-scratchpad-draft:",
      path: cwd => "workspaces/scratchpad?path=" + encodeURIComponent(cwd),
      api: (path, body, options) => body === undefined ? api(path) :
        api("workspaces/scratchpad", {path: new URLSearchParams(path.split("?")[1]).get("path"), ...body}, options),
      onRender: () => this.focusWhenReady(),
    });
    this.closeButton.onclick = () => this.close();
    this.dialog.addEventListener("cancel", event => { event.preventDefault(); this.close(); });
    this.dialog.addEventListener("close", () => { if (!this.dialog.open) this.finishClose(); });
  }

  get available() { return Boolean(this.folder?.cwd); }
  get tabActive() { return !this.page.hidden; }
  canToggle() {
    const open = this.document.querySelector("dialog[open]") ||
      this.view.agentCoordPanes?.focusedRecord?.frame?.contentDocument?.querySelector("dialog[open]");
    return this.available && (!open || open === this.dialog);
  }

  setProject(folder) {
    if (this.folder?.cwd === folder?.cwd) return;
    this.hideTab();
    this.folder = folder;
    this.projectLabel.textContent = folder?.name || folder?.cwd?.split("/").filter(Boolean).pop() || "";
    this.projectLabel.title = folder?.cwd || "";
  }

  showTab() {
    if (!this.available) return false;
    this.close(false);
    this.page.hidden = false;
    this.document.body.classList.add("project-scratchpad-active");
    this.openEditor();
    return true;
  }

  hideTab() {
    this.close(false);
    this.page.hidden = true;
    this.document.body.classList.remove("project-scratchpad-active");
    this.pendingFocus = false;
    this.pad.select(null);
  }

  openEditor() {
    this.pendingFocus = true;
    this.pad.select(this.folder.cwd);
    this.focusWhenReady();
  }

  focusWhenReady() {
    if (!this.pendingFocus || this.input.disabled || !(this.tabActive || this.floating)) return;
    this.pendingFocus = false;
    this.input.focus({preventScroll: true});
    this.input.setSelectionRange(this.input.value.length, this.input.value.length);
    this.input.scrollTop = this.input.scrollHeight;
  }

  toggle() {
    if (this.dialog.open) { this.close(); return true; }
    if (!this.canToggle()) return false;
    let element = this.document.activeElement;
    while (element?.contentDocument?.activeElement) element = element.contentDocument.activeElement;
    this.returnFocus = {element, start: element?.selectionStart, end: element?.selectionEnd,
      direction: element?.selectionDirection, scrollTop: element?.scrollTop, scrollLeft: element?.scrollLeft};
    this.floating = true;
    this.dialog.append(this.editor);
    this.closeButton.hidden = false;
    this.editor.hidden = false;
    this.dialog.showModal();
    this.openEditor();
    return true;
  }

  close(restore = true) {
    if (!this.floating) return;
    this.dialog.close();
    this.finishClose(restore);
  }

  finishClose(restore = true) {
    if (!this.floating) return;
    this.floating = false;
    this.pendingFocus = false;
    this.pad.flush(this.pad.entries.get(this.pad.selected));
    this.page.append(this.editor);
    this.closeButton.hidden = true;
    if (!this.tabActive) this.pad.select(null);
    const previous = this.returnFocus;
    this.returnFocus = null;
    if (restore && previous?.element?.isConnected) {
      const {element, start, end, direction, scrollTop, scrollLeft} = previous;
      element.focus({preventScroll: true});
      if (typeof start === "number") element.setSelectionRange(start, end, direction);
      element.scrollTop = scrollTop;
      element.scrollLeft = scrollLeft;
    }
  }

  static bindShortcut(document, getScratchpad) {
    document.addEventListener("keydown", event => {
      if (event.defaultPrevented || event.isComposing) return;
      const pad = getScratchpad();
      if (event.key === "Escape" && pad?.dialog.open) {
        event.preventDefault(); event.stopImmediatePropagation(); pad.close(); return;
      }
      if (!event.shiftKey || event.altKey ||
          !(event.metaKey || event.ctrlKey) || (event.code !== "KeyS" && event.key?.toLowerCase() !== "s")) return;
      if (!pad?.canToggle()) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (!event.repeat) pad.toggle();
    }, true);
  }
}

if (typeof module !== "undefined") module.exports = {ProjectScratchpad};
