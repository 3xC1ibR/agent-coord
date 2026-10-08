"use strict";

// Notifications only: no fetch handler or conversation/offline cache.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

function notificationURL(value) {
  try {
    const url = new URL(typeof value === "string" ? value : "/", self.location.origin);
    if (url.origin === self.location.origin && url.pathname === "/" && !url.search &&
        (!url.hash || /^#[a-zA-Z0-9-]{1,160}$/.test(url.hash))) return url.href;
  } catch { /* Invalid URLs must not prevent a visible notification. */ }
  return self.location.origin + "/";
}

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data?.json() || {}; } catch { /* Always show a visible fallback. */ }
  const labels = new Set(["Turn finished", "Turn failed", "Approval needed", "Phone notifications are working"]);
  event.waitUntil(self.registration.showNotification("Ribbon Field", {
    body: labels.has(data.body) ? data.body : "Ribbon Field has an update.",
    tag: typeof data.tag === "string" ? data.tag.slice(0, 100) : "agent-coord-update",
    icon: "/app-icons/icon-192.png",
    data: {url: notificationURL(data.url)},
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil((async () => {
    const url = notificationURL(event.notification.data?.url);
    const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    const client = windows.find(item => {
      const current = new URL(item.url);
      return current.origin === self.location.origin && current.pathname === "/" && !current.search;
    });
    if (client) {
      await client.focus();
      client.postMessage({type: "agent-coord-open-notification", url});
    } else await self.clients.openWindow(url);
  })());
});
