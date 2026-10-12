"use strict";

// Notifications only: no fetch handler or conversation/offline cache. Retain
// just the latest tapped URL briefly so a cold Home Screen launch can claim it.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

let pendingDestination = null;
const destinationLifetime = 5 * 60 * 1000;

function destinationStore(update) {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("agent-coord-notification-navigation", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("destination");
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const db = request.result;
      const transaction = db.transaction("destination", update ? "readwrite" : "readonly");
      const store = transaction.objectStore("destination");
      const read = store.get("pending");
      let result;
      read.onsuccess = () => {
        result = update ? update(read.result) : read.result;
        if (update) {
          if (result) store.put(result, "pending");
          else store.delete("pending");
        }
      };
      transaction.oncomplete = () => { db.close(); resolve(result); };
      transaction.onabort = transaction.onerror = () => { db.close(); reject(transaction.error); };
    };
  });
}

async function forgetDestination(id) {
  if (pendingDestination?.id === id) pendingDestination = null;
  try { await destinationStore(current => current?.id === id ? null : current); }
  catch { /* Direct message/navigation still works if storage is unavailable. */ }
}

async function readDestination() {
  let destination = pendingDestination;
  try { destination = await destinationStore() || destination; } catch { /* Use memory. */ }
  if (destination && Date.now() - destination.createdAt > destinationLifetime) {
    await forgetDestination(destination.id);
    return null;
  }
  return destination;
}

function appClient(client) {
  try {
    const url = new URL(client.url);
    return url.origin === self.location.origin && url.pathname === "/";
  } catch { return false; }
}

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

function sendDestination(client, destination) {
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
    try { client.postMessage({type: "agent-coord-open-notification", url: destination.url, id: destination.id}, [channel.port2]); }
    catch { finish(false); }
  });
}

async function deliverDestination(client, destination) {
  if (!await sendDestination(client, destination)) return false;
  await forgetDestination(destination.id);
  return true;
}

async function openDestination(client, destination) {
  // Failure to focus a waking WebKit client must not prevent navigation.
  try { await client.focus(); } catch { /* Try delivery anyway. */ }
  if (await deliverDestination(client, destination)) return true;
  // Keep the saved destination even when navigate succeeds: WebKit may restore
  // the start URL or report success before the new document is ready.
  try { return Boolean(await client.navigate(destination.url)); } catch { return false; }
}

self.addEventListener("message", event => {
  if (event.data?.type !== "agent-coord-notification-ready" || !appClient(event.source)) return;
  event.waitUntil((async () => {
    const destination = await readDestination();
    if (destination) await deliverDestination(event.source, destination);
  })());
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil((async () => {
    const url = notificationURL(event.notification.data?.url);
    const destination = {id: crypto.randomUUID(), url, createdAt: Date.now()};
    pendingDestination = destination;
    try { await destinationStore(() => destination); } catch { /* Use direct delivery. */ }
    const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    const client = windows.find(appClient);
    if (client && await openDestination(client, destination)) return;
    const opened = await self.clients.openWindow(url);
    // Some Home Screen launches restore the start URL instead of the requested
    // hash. Deliver to the returned client too, once it can accept navigation.
    if (opened) await openDestination(opened, destination);
  })());
});
