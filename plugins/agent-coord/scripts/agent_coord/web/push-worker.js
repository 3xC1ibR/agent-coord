"use strict";

// Notifications only: no fetch handler or conversation/offline cache.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

function notificationURL(value) {
  try {
    const url = new URL(typeof value === "string" ? value : "/", self.location.origin);
    if (url.origin === self.location.origin && url.pathname === "/" && !url.search &&
        (!url.hash || /^#[a-zA-Z0-9_.~-]{1,200}$/.test(url.hash))) return url.href;
  } catch { /* Invalid URLs must not prevent a visible notification. */ }
  return self.location.origin + "/";
}

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data?.json() || {}; } catch { /* Always show a visible fallback. */ }
  const text = (value, fallback, limit) => typeof value === "string" && value.trim() ? value.slice(0, limit) : fallback;
  event.waitUntil(self.registration.showNotification(text(data?.title, "Ribbon Field", 240), {
    body: text(data?.body, "Ribbon Field has an update.", 240),
    tag: typeof data.tag === "string" ? data.tag.slice(0, 100) : "agent-coord-update",
    icon: "/app-icons/icon-192.png",
    data: {url: notificationURL(data.url)},
  }));
});

function sendDestination(client, url) {
  // A newly launched iPhone client may exist before its page has installed a
  // message listener. Require receipt; otherwise use navigation's startup URL.
  return new Promise(resolve => {
    const channel = new MessageChannel();
    const finish = received => {
      clearTimeout(timer);
      channel.port1.close(); channel.port2.close();
      resolve(received);
    };
    const timer = setTimeout(() => finish(false), 1500);
    channel.port1.onmessage = event => {
      if (event.data?.type === "agent-coord-notification-received") finish(true);
    };
    try { client.postMessage({type: "agent-coord-open-notification", url}, [channel.port2]); }
    catch { finish(false); }
  });
}

async function openDestination(client, url) {
  // Failure to focus a waking WebKit client must not prevent navigation.
  try { await client.focus(); } catch { /* Try delivery anyway. */ }
  if (await sendDestination(client, url)) return true;
  try { return Boolean(await client.navigate(url)); } catch { return false; }
}

self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil((async () => {
    const url = notificationURL(event.notification.data?.url);
    const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    const client = windows.find(item => {
      const current = new URL(item.url);
      return current.origin === self.location.origin && current.pathname === "/";
    });
    if (client && await openDestination(client, url)) return;
    const opened = await self.clients.openWindow(url);
    // Some Home Screen launches restore the start URL instead of the requested
    // hash. Deliver to the returned client too, once it can accept navigation.
    if (opened) await openDestination(opened, url);
  })());
});
