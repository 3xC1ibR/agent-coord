"use strict";

class TurnNotifications {
  constructor({button, claim, selected, openThread, onError}, env = globalThis) {
    Object.assign(this, {button, claim, selected, openThread, onError, env});
    this.owner = env.crypto?.randomUUID?.() || Math.random().toString(36).slice(2);
    this.memory = new Map();
    this.queue = Promise.resolve();
    this.closed = false;
    this.preferenceKey = "agent-coord.notifications.enabled";
    this.focusKey = "agent-coord.notifications.focus";
    this.changed = () => { this.syncFocus(); this.render(); };
    this.pagehide = () => this.destroy();
    this.button.onclick = () => this.toggle().catch(onError);
    for (const event of ["focus", "blur", "storage"]) env.addEventListener(event, this.changed);
    env.document.addEventListener("visibilitychange", this.changed);
    env.addEventListener("pagehide", this.pagehide);
    // A crashed or closed tab must not suppress future alerts indefinitely.
    this.timer = env.setInterval(this.changed, 15000);
    this.changed();
  }

  read(key) {
    try { return this.env.localStorage.getItem(key); }
    catch { return this.memory.get(key) ?? null; }
  }

  write(key, value) {
    this.memory.set(key, value);
    try { this.env.localStorage.setItem(key, value); } catch { /* Session-only fallback. */ }
  }

  supported() { return typeof this.env.Notification === "function" && this.env.isSecureContext !== false; }
  enabled() { return this.supported() && this.env.Notification.permission === "granted" && this.read(this.preferenceKey) === "true"; }

  render() {
    const supported = this.supported(), denied = supported && this.env.Notification.permission === "denied";
    const enabled = this.enabled();
    this.button.disabled = !supported || denied;
    this.button.textContent = !supported ? "Notifications unavailable" : denied ? "Notifications blocked" : enabled ? "Notifications on" : "Enable notifications";
    this.button.setAttribute("aria-pressed", String(enabled));
    this.button.title = !supported ? "Desktop notifications are not available in this browser." : denied ?
      "Allow notifications for this site in your browser settings, then reload." : enabled ?
      "Turn off desktop alerts for completed turns." : "Get turn-finished alerts while a UI tab is open.";
  }

  async toggle() {
    if (!this.supported()) return;
    if (this.enabled()) this.write(this.preferenceKey, "false");
    else {
      let permission = this.env.Notification.permission;
      // Called directly from the button's click; never ask during page load.
      if (permission === "default") permission = await this.env.Notification.requestPermission();
      this.write(this.preferenceKey, String(permission === "granted"));
    }
    this.render();
  }

  focusedThread() {
    return this.env.document.visibilityState === "visible" && this.env.document.hasFocus() ? this.selected() : null;
  }

  savedFocus() {
    try { return JSON.parse(this.read(this.focusKey) || "null"); } catch { return null; }
  }

  syncFocus() {
    if (this.closed) return;
    const thread = this.focusedThread();
    if (thread) this.write(this.focusKey, JSON.stringify({owner: this.owner, thread, updated: Date.now()}));
    else if (this.savedFocus()?.owner === this.owner) this.write(this.focusKey, "null");
  }

  viewing(thread) {
    if (this.focusedThread() === thread) return true;
    const focus = this.savedFocus();
    return focus?.thread === thread && Date.now() - focus.updated < 45000;
  }

  receive(events) {
    this.queue = this.queue.then(async () => {
      for (const event of events) {
        try { await this.deliver(event); } catch (error) { this.onError(error); }
      }
    });
    return this.queue;
  }

  async deliver(event) {
    if (this.closed || !this.enabled() || !["completed", "failed"].includes(event.status)) return;
    // The server claim is atomic across tabs and persists across reconnects.
    // Consume focused-thread events too, so they cannot pop up in another tab.
    const quiet = this.viewing(event.thread_id);
    if (!await this.claim(event.id)) return;
    if (this.closed || !this.enabled() || quiet || this.viewing(event.thread_id)) return;
    const label = event.status === "failed" ? "Turn failed" : "Turn finished";
    const notification = new this.env.Notification(`${event.project_name || event.repository_name || "Agent Coord"} · ${label}`, {
      body: event.title, tag: "agent-coord-turn-" + event.id,
    });
    notification.onclick = async () => {
      this.env.focus();
      notification.close();
      try { await this.openThread(event.thread_id); } catch (error) { this.onError(error); }
    };
  }

  destroy() {
    if (this.closed) return;
    if (this.savedFocus()?.owner === this.owner) this.write(this.focusKey, "null");
    this.closed = true;
    this.env.clearInterval(this.timer);
    for (const event of ["focus", "blur", "storage"]) this.env.removeEventListener(event, this.changed);
    this.env.document.removeEventListener("visibilitychange", this.changed);
    this.env.removeEventListener("pagehide", this.pagehide);
  }
}

if (typeof module !== "undefined" && module.exports) module.exports = TurnNotifications;
else globalThis.TurnNotifications = TurnNotifications;
