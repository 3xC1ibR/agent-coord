"use strict";

class PhoneNotifications {
  constructor({button, api, openThread, onError, listen = true}, env = globalThis) {
    Object.assign(this, {button, api, openThread, onError, env});
    // Conversation pages receive navigation events; only Settings mounts controls.
    this.button ||= env.document.createElement("button");
    button = this.button;
    this.busy = false;
    this.info = {};
    this.subscription = null;
    this.navigationReady = false;
    this.pendingThread = null;
    this.receivedDestinations = new Set();
    this.button.onclick = () => this.toggle().catch(onError);
    this.testButton = env.document.createElement("button");
    this.testButton.type = "button";
    this.testButton.className = button.className;
    this.testButton.textContent = "Send test notification";
    this.testButton.hidden = true;
    this.testButton.onclick = () => this.test().catch(onError);
    this.message = env.document.createElement("small");
    this.message.setAttribute("role", "status");
    button.after(this.testButton, this.message);
    this.messageHandler = event => {
      if (event.data?.type !== "agent-coord-open-notification") return;
      try {
        const url = new URL(event.data.url, env.location.origin);
        if (url.origin === env.location.origin && url.pathname === "/" && !url.search &&
            (!url.hash || /^#[a-zA-Z0-9_.~-]{1,200}$/.test(url.hash))) {
          // Queue until saved views and the initial route have finished loading,
          // so startup cannot overwrite a notification selection.
          const duplicate = event.data.id && this.receivedDestinations.has(event.data.id);
          if (!duplicate && url.hash) this.pendingThread = url.hash.slice(1);
          if (event.data.id) {
            this.receivedDestinations.add(event.data.id);
            if (this.receivedDestinations.size > 32) this.receivedDestinations.delete(this.receivedDestinations.values().next().value);
          }
          event.ports?.[0]?.postMessage({type: "agent-coord-notification-received"});
          if (this.navigationReady) this.openPendingThread();
        }
      } catch { /* Ignore malformed worker messages. */ }
    };
    if (listen) env.navigator.serviceWorker?.addEventListener("message", this.messageHandler);
    this.recoverHandler = () => {
      if (this.navigationReady && !env.document.hidden) this.recoverDestination();
    };
    this.hideHandler = event => { if (!event.persisted) this.destroy(); };
    if (listen) {
      env.navigator.serviceWorker?.addEventListener("controllerchange", this.recoverHandler);
      env.addEventListener("pageshow", this.recoverHandler);
      env.document.addEventListener("visibilitychange", this.recoverHandler);
    }
    env.addEventListener("pagehide", this.hideHandler);
  }

  ready() {
    this.navigationReady = true;
    this.recoverDestination();
    return this.openPendingThread();
  }

  async recoverDestination() {
    try {
      const worker = this.env.navigator.serviceWorker;
      const active = worker?.controller || (await worker?.ready)?.active;
      active?.postMessage({type: "agent-coord-notification-ready"});
    } catch { /* A later foreground/controller change can retry recovery. */ }
  }

  async openPendingThread() {
    const thread = this.pendingThread;
    this.pendingThread = null;
    if (thread) {
      // Only a notification click queues a pending thread.
      this.env.agentCoordActivity?.markIntent("notification");
      try { await this.openThread(thread); } catch (error) { this.onError(error); }
    }
  }

  supported() {
    return this.env.isSecureContext && this.env.navigator.serviceWorker && this.env.PushManager && this.env.Notification;
  }

  async start() {
    if (!this.supported()) {
      this.button.disabled = true;
      this.button.textContent = "Phone notifications unavailable";
      this.message.textContent = "On iPhone, add Ribbon Field to your Home Screen, open it there, then enable notifications.";
      return;
    }
    this.button.disabled = true;
    try {
      this.info = await this.api("push/status");
      if (this.info.available) {
        await this.env.navigator.serviceWorker.register("/push-worker.js", {scope: "/", updateViaCache: "none"});
        this.registration = await this.env.navigator.serviceWorker.ready;
        this.subscription = await this.registration.pushManager.getSubscription();
        // Permission revocation should also remove the server's subscription.
        if (this.info.enabled && (this.env.Notification.permission !== "granted" || !this.subscription)) {
          this.info = await this.api("push/unsubscribe", {});
        }
      }
      this.render();
    } catch (error) {
      this.button.textContent = "Phone notifications unavailable";
      this.message.textContent = "Could not set up notifications. Reconnect to your Mac and reload.";
      throw error;
    }
  }

  render() {
    const enabled = !!this.info.enabled && !!this.subscription;
    const denied = this.env.Notification?.permission === "denied";
    this.button.disabled = this.busy || !this.info.available || (denied && !this.info.enabled);
    this.button.textContent = enabled ? "Phone notifications on" : denied ? "Phone notifications blocked" : "Enable phone notifications";
    this.button.setAttribute("aria-pressed", String(enabled));
    this.button.title = enabled ? "Turn off background alerts on this device." : "Get background alerts for finished turns, failures, approvals, and reminders.";
    this.testButton.hidden = !enabled;
    this.testButton.disabled = this.busy;
    this.message.textContent = this.info.error || (denied ? "Allow Ribbon Field notifications in iPhone Settings, then reload." : "");
  }

  async toggle() {
    if (this.busy || !this.registration) return;
    this.busy = true;
    this.render();
    try {
      if (this.info.enabled) {
        // Server first: a network error must not leave an invisible active grant.
        this.info = await this.api("push/unsubscribe", {});
        await this.subscription?.unsubscribe();
        this.subscription = null;
      } else {
        // First awaited call follows the user's tap, as required by iOS.
        const permission = await this.env.Notification.requestPermission();
        if (permission !== "granted") return;
        const key = this.info.publicKey;
        const binary = this.env.atob(key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - key.length % 4) % 4));
        const publicKey = Uint8Array.from(binary, character => character.charCodeAt(0));
        // Recreate stale/expired subscriptions when explicitly enabling.
        const previous = await this.registration.pushManager.getSubscription();
        if (previous) await previous.unsubscribe();
        this.subscription = await this.registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: publicKey});
        this.info = await this.api("push/subscribe", {subscription: this.subscription.toJSON()});
      }
    } finally {
      this.busy = false;
      this.render();
    }
  }

  async test() {
    if (this.busy) return;
    this.busy = true;
    this.render();
    try {
      await this.api("push/test", {});
      this.message.textContent = "Test queued. Look for a Ribbon Field notification; Focus settings may silence it.";
    } finally {
      this.busy = false;
      this.testButton.disabled = false;
      this.button.disabled = false;
    }
  }

  // The background sender owns remote alerts; never claim desktop events.
  syncFocus() {}
  receive() { return Promise.resolve(); }
  destroy() {
    this.env.navigator.serviceWorker?.removeEventListener("message", this.messageHandler);
    this.env.navigator.serviceWorker?.removeEventListener("controllerchange", this.recoverHandler);
    this.env.removeEventListener("pageshow", this.recoverHandler);
    this.env.document.removeEventListener("visibilitychange", this.recoverHandler);
    this.env.removeEventListener("pagehide", this.hideHandler);
  }
}

if (typeof module !== "undefined" && module.exports) module.exports = PhoneNotifications;
else globalThis.PhoneNotifications = PhoneNotifications;
