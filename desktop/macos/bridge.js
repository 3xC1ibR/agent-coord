/* Loaded only by the native wrapper, before the existing web UI. */
(() => {
  "use strict";
  const native = window.webkit?.messageHandlers?.desktop;
  if (!native || window.top !== window) return;
  const send = body => native.postMessage(body);
  const preferences = __AGENT_COORD_PREFERENCES__;
  const savedKey = key => key.startsWith("agent-coord.") || key === "agent-coord-group-by";
  // The backend uses a fresh private port on every launch. Preserve UI choices
  // in macOS preferences instead of tying them to that temporary HTTP origin.
  try {
    const setItem = Storage.prototype.setItem, removeItem = Storage.prototype.removeItem;
    for (const [key, value] of Object.entries(preferences)) {
      if (savedKey(key) && key !== "agent-coord.notifications.focus") setItem.call(localStorage, key, value);
    }
    Storage.prototype.setItem = function (key, value) {
      setItem.call(this, key, value);
      if (this === localStorage && savedKey(String(key)) && key !== "agent-coord.notifications.focus") {
        send({action: "preference", key: String(key), value: String(value)}).catch(() => {});
      }
    };
    Storage.prototype.removeItem = function (key) {
      removeItem.call(this, key);
      if (this === localStorage && savedKey(String(key))) send({action: "preference", key: String(key), value: null}).catch(() => {});
    };
  } catch { /* Storage restrictions must not prevent the UI from loading. */ }

  let permission = "default";
  const pending = new Map();
  class DesktopNotification extends EventTarget {
    static get permission() { return permission; }
    static get agentCoordNative() { return true; }
    static async requestPermission(callback) {
      permission = await send({action: "requestPermission"});
      if (callback) callback(permission);
      window.dispatchEvent(new Event("focus"));
      return permission;
    }
    constructor(title, options = {}) {
      super();
      this.title = String(title);
      this.body = String(options.body || "");
      this.onclick = null;
      this.id = crypto.randomUUID();
      if (permission !== "granted") return;
      pending.set(this.id, this);
      // Keep only a bounded set of live click callbacks.
      if (pending.size > 128) pending.delete(pending.keys().next().value);
      send({action: "notify", id: this.id, title: this.title, body: this.body}).catch(() => pending.delete(this.id));
    }
    close() {
      pending.delete(this.id);
      send({action: "closeNotification", id: this.id}).catch(() => {});
    }
  }
  window.Notification = DesktopNotification;
  window.__agentCoordNotificationClick = id => {
    const item = pending.get(id);
    if (!item) return;
    const event = new Event("click");
    item.dispatchEvent(event);
    if (typeof item.onclick === "function") item.onclick(event);
  };
  send({action: "permission"}).then(value => {
    permission = value;
    window.dispatchEvent(new Event("focus"));
  }).catch(() => {});
})();
