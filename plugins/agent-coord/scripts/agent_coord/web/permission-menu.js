"use strict";

// Permission choices are supplied by the provider API; this view never maps modes.
class PermissionMenu {
  constructor({document: doc = document, view = window}) {
    this.doc = doc; this.view = view;
    doc.addEventListener("pointerdown", event => {
      if (this.panel && !this.panel.contains(event.target) && !this.trigger.contains(event.target)) this.close();
    });
    doc.addEventListener("focusin", event => {
      if (this.panel && !this.panel.contains(event.target) && !this.trigger.contains(event.target)) this.close();
    });
    view.addEventListener("resize", () => this.position());
    view.visualViewport?.addEventListener("resize", () => this.position());
    view.addEventListener("scroll", () => this.position(), {capture: true, passive: true});
  }
  close(focus = false) {
    const trigger = this.trigger;
    this.panel?.remove(); this.panel = null; this.request = null;
    trigger?.setAttribute("aria-expanded", "false");
    if (focus && trigger?.isConnected) trigger.focus({preventScroll: true});
  }
  sync(id, unavailable) {
    if (this.panel && (id !== this.id || unavailable && !this.saving)) this.close();
  }
  node(tag, text, className) {
    const node = this.doc.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  async open(trigger, {id, load, select}) {
    if (this.panel && this.trigger === trigger) { this.close(true); return; }
    this.close(); this.trigger = trigger; this.id = id; this.saving = false;
    const panel = this.node("div", null, "model-menu permission-menu"); this.panel = panel;
    panel.setAttribute("role", "menu"); panel.setAttribute("aria-label", "Session permissions"); panel.tabIndex = -1;
    trigger.setAttribute("aria-expanded", "true"); this.doc.body.append(panel);
    panel.onkeydown = event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); this.close(true); return; }
      if (event.key === "Tab") { this.close(true); return; }
      const buttons = [...panel.querySelectorAll("button:not(:disabled)")], index = buttons.indexOf(this.doc.activeElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 :
        event.key === "ArrowDown" ? (index + 1) % buttons.length : event.key === "ArrowUp" ? (index - 1 + buttons.length) % buttons.length : null;
      if (next != null) { event.preventDefault(); buttons[next]?.focus({preventScroll: true}); buttons[next]?.scrollIntoView({block: "nearest"}); }
    };
    const request = {}; this.request = request;
    const current = () => this.panel === panel && this.request === request;
    const render = catalog => {
      if (!current()) return;
      panel.replaceChildren();
      const modes = catalog.permission_modes || [], selected = catalog.permission_mode;
      const effective = catalog.permission_mode_effective;
      const effectiveLabel = modes.find(mode => mode.id === effective)?.label || effective;
      const note = this.node("p", "Applies to the next message." + (effective && effective !== selected ? " Current native mode: " + effectiveLabel + "." : ""), "permission-menu-status");
      panel.append(note);
      if (!modes.length) {
        const empty = this.node("p", "No permission choices are available for this provider.", "permission-menu-status");
        empty.setAttribute("role", "status"); panel.append(empty);
      }
      const error = this.node("p", "", "permission-menu-error"); error.hidden = true; error.setAttribute("role", "alert");
      for (const mode of modes) {
        const button = this.node("button", null, "model-option permission-option"); button.type = "button"; button.tabIndex = -1;
        button.dataset.mode = mode.id; button.setAttribute("role", "menuitemradio"); button.setAttribute("aria-checked", String(mode.id === selected));
        button.disabled = mode.available === false;
        const copy = this.node("span", null, "permission-option-copy"); copy.append(this.node("span", mode.label || mode.id, "permission-option-label"));
        const description = mode.available === false && mode.reason ? mode.reason : mode.description;
        if (description) copy.append(this.node("small", description, "permission-option-description"));
        const check = this.node("span", mode.id === selected ? "✓" : "", "model-check"); check.setAttribute("aria-hidden", "true");
        button.append(copy, check);
        button.onclick = async () => {
          if (this.saving || button.disabled) return;
          if (mode.id === selected) { this.close(true); return; }
          this.saving = true; panel.setAttribute("aria-busy", "true");
          for (const option of panel.querySelectorAll("button")) option.disabled = true;
          error.hidden = true;
          try { await select(mode); if (current()) this.close(true); }
          catch (failure) {
            if (current()) {
              render(catalog);
              const feedback = panel.querySelector(".permission-menu-error"); feedback.textContent = failure.message; feedback.hidden = false;
              [...panel.querySelectorAll("button")].find(option => option.dataset.mode === mode.id)?.focus();
              this.position();
            }
          } finally { this.saving = false; panel.removeAttribute("aria-busy"); }
        };
        panel.append(button);
      }
      panel.append(error); this.position();
      const focused = panel.querySelector('button[aria-checked="true"]:not(:disabled)') || panel.querySelector("button:not(:disabled)") || panel;
      focused.focus({preventScroll: true}); if (focused !== panel) focused.scrollIntoView({block: "nearest"});
    };
    const fetch = async () => {
      const loading = this.node("p", "Loading permissions…", "permission-menu-status"); loading.setAttribute("role", "status");
      panel.replaceChildren(loading); this.position(); panel.focus({preventScroll: true});
      try { render(await load()); }
      catch (error) {
        if (!current()) return;
        const message = this.node("p", error.message, "permission-menu-error"); message.setAttribute("role", "alert");
        const retry = this.node("button", "Retry", "model-option"); retry.type = "button"; retry.setAttribute("role", "menuitem"); retry.onclick = fetch;
        panel.replaceChildren(message, retry); this.position(); retry.focus({preventScroll: true});
      }
    };
    await fetch();
  }
  position() {
    if (!this.panel) return;
    const viewport = this.view.visualViewport, style = this.view.getComputedStyle(this.panel);
    const edge = parseFloat(style.getPropertyValue("--chat-menu-edge")) || 12, gap = parseFloat(style.getPropertyValue("--chat-menu-gap")) || 8;
    const width = viewport?.width || this.view.innerWidth, height = viewport?.height || this.view.innerHeight;
    const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
    const rect = this.trigger.getBoundingClientRect();
    this.panel.style.maxHeight = Math.max(0, height - edge * 2) + "px";
    const size = this.panel.getBoundingClientRect();
    this.panel.style.left = Math.max(left + edge, Math.min(rect.right - size.width, left + width - size.width - edge)) + "px";
    this.panel.style.top = Math.max(top + edge, Math.min(rect.top - size.height - gap >= top + edge ? rect.top - size.height - gap : rect.bottom + gap, top + height - size.height - edge)) + "px";
  }
}
if (typeof module !== "undefined" && module.exports) module.exports = {PermissionMenu};
else globalThis.PermissionMenu = PermissionMenu;
