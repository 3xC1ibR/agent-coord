"use strict";

// A sent message appears on Enter, before the backend answers. It reads
// "Sending…" until acceptance and stays until the conversation shows the
// provider's own copy; a rejected message stays visibly marked as not sent.
class OutgoingMessages {
  constructor({itemText}) {
    this.itemText = itemText;
    this.threads = new Map();
    this.sequence = 0;
  }

  list(id) { return this.threads.get(id) || []; }

  // Dispatcher requests settle by their durable request ID; other messages
  // settle when a user message that was not already present carries the text.
  add(id, {text, images = 0, requestId = null, detail = null}) {
    const known = new Set();
    for (const turn of detail?.thread?.turns || []) {
      for (const item of turn.items || []) if (item.type === "userMessage") known.add(item.id);
    }
    const entry = {key: "outgoing-" + ++this.sequence, text, images, match: text.trim().slice(0, 200),
      requestId, known, status: "sending", error: ""};
    this.threads.set(id, [...this.list(id).filter(item => item.status !== "failed"), entry]);
    return entry;
  }

  accepted(entry) { entry.status = "sent"; }

  rejected(entry, message) { entry.status = "failed"; entry.error = message; }

  // The provider's copy also settles a message whose acceptance reply is still
  // in flight: its stream event can arrive first.
  settle(id, detail) {
    const entries = this.list(id);
    if (!entries.length || !detail) return;
    const remaining = entries.filter(entry => {
      if (entry.status === "failed") return true;
      if (entry.requestId) return !(detail.dispatchRequests || []).some(request => request.request_id === entry.requestId);
      return !(detail.thread?.turns || []).some(turn => (turn.items || []).some(item => item.type === "userMessage" &&
        !entry.known.has(item.id) && (!entry.match || this.itemText(item).includes(entry.match))));
    });
    if (remaining.length) this.threads.set(id, remaining); else this.threads.delete(id);
  }

  node(document, entry) {
    const message = document.createElement("article");
    message.className = "message user outgoing";
    message.dataset.outgoing = entry.key;
    message.dataset.status = entry.status;
    message.setAttribute("aria-label", "You");
    const body = document.createElement("div");
    body.className = "text";
    body.textContent = entry.text;
    if (entry.images) {
      const images = document.createElement("p");
      images.className = "outgoing-images";
      images.textContent = entry.images === 1 ? "1 image" : entry.images + " images";
      body.append(images);
    }
    const status = document.createElement("span");
    status.className = "message-time outgoing-status";
    status.setAttribute("role", "status");
    status.textContent = entry.status === "failed" ? "Not sent · " + entry.error : entry.status === "sent" ? "Sent" : "Sending…";
    message.append(body, status);
    return message;
  }

  append(document, container, id) {
    for (const entry of this.list(id)) container.append(this.node(document, entry));
  }

  // Show a change in place, without rebuilding the conversation.
  update(document, timeline, id, detail) {
    this.settle(id, detail);
    for (const old of timeline.querySelectorAll("article[data-outgoing]")) old.remove();
    if (this.list(id).length) timeline.querySelector(":scope > p.empty:only-child")?.remove();
    this.append(document, timeline, id);
  }
}

if (typeof module !== "undefined") module.exports = OutgoingMessages;
