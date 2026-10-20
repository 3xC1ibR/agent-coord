"use strict";

class OverviewLayout {
  constructor({document, window, storage, onChange, canNavigate = () => true}) {
    Object.assign(this, {document, window, storage, onChange, canNavigate});
    this.key = "agent-coord.overview-layout";
    this.preference = "cards";
    try { const saved = storage?.getItem(this.key); if (["atlas", "working-set"].includes(saved)) this.preference = saved; } catch { /* Optional preference. */ }
    this.narrow = window.matchMedia("(max-width: 720px)");
    this.narrow.addEventListener("change", () => { this.close(); this.onChange?.(this.mode, {navigate: false}); });
    document.addEventListener("pointerdown", event => {
      if (this.menu && !this.menu.contains(event.target) && !this.anchor?.contains(event.target)) this.close();
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && this.menu) { event.preventDefault(); event.stopPropagation(); this.close(true); }
    });
    window.addEventListener("resize", () => this.close());
    window.addEventListener("scroll", () => this.close(), {capture: true, passive: true});
  }
  get mode() { return this.narrow.matches && this.preference === "atlas" ? "cards" : this.preference; }
  choose(mode) {
    if (!["cards", "atlas", "working-set"].includes(mode) || mode === "atlas" && this.narrow.matches || !this.canNavigate()) return false;
    this.preference = mode;
    try { this.storage?.setItem(this.key, mode); } catch { /* Switching still works without storage. */ }
    this.close(true);
    this.onChange?.(mode, {navigate: true});
    return true;
  }
  close(focus = false) {
    if (!this.menu) return;
    this.menu.remove(); this.menu = null;
    this.anchor?.setAttribute("aria-expanded", "false");
    if (focus && this.anchor?.isConnected) this.anchor.focus({preventScroll: true});
    this.anchor = null;
  }
  open(anchor, event = {}) {
    event.preventDefault?.(); event.stopPropagation?.();
    if (!this.canNavigate()) return;
    if (this.menu && this.anchor === anchor) { this.close(true); return; }
    this.close(); this.anchor = anchor;
    const doc = this.document, panel = doc.createElement("div");
    panel.className = "overview-layout-menu"; panel.setAttribute("role", "menu"); panel.setAttribute("aria-label", "Overview layout");
    for (const [mode, title] of [["cards", "Cards"], ["atlas", "Atlas"], ["working-set", "Working Set"]]) {
      const button = doc.createElement("button"); button.type = "button";
      button.dataset.layout = mode; button.setAttribute("role", "menuitemradio"); button.setAttribute("aria-checked", String(this.mode === mode));
      button.disabled = mode === "atlas" && this.narrow.matches;
      if (button.disabled) button.title = "Atlas is available on larger screens";
      const check = doc.createElement("span"); check.className = "layout-check"; check.setAttribute("aria-hidden", "true"); check.textContent = this.mode === mode ? "✓" : "";
      button.append(check, title);
      button.onclick = () => this.choose(mode);
      panel.append(button);
    }
    panel.onkeydown = event => {
      const buttons = [...panel.children].filter(button => !button.disabled), index = buttons.indexOf(doc.activeElement);
      const next = event.key === "ArrowDown" ? (index + 1) % buttons.length : event.key === "ArrowUp" ? (index + buttons.length - 1) % buttons.length
        : event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : null;
      if (next !== null) { event.preventDefault(); buttons[next]?.focus(); }
      if (event.key === "Tab") this.close();
    };
    doc.body.append(panel); this.menu = panel; anchor.setAttribute("aria-expanded", "true");
    const rect = anchor.getBoundingClientRect(), size = panel.getBoundingClientRect();
    panel.style.left = Math.max(8, Math.min(rect.left, this.window.innerWidth - size.width - 8)) + "px";
    panel.style.top = Math.max(8, Math.min(rect.bottom + 4, this.window.innerHeight - size.height - 8)) + "px";
    [...panel.children].find(button => button.dataset.layout === this.mode)?.focus();
  }
}
if (typeof module !== "undefined") module.exports = {OverviewLayout};
