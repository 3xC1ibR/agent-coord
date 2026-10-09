(function (root) {
  "use strict";
  const prefix = "[Ribbon Field dispatcher context]\n";
  function turns(source, receipts = []) {
    const routed = new Map(receipts.filter(r => r.target_id === r.dispatcher_id && r.turn_id).map(r => [r.turn_id, r]));
    const hidden = new Set();
    const result = source.map(turn => ({...turn, items: (turn.items || []).flatMap(item => {
      if (item.type !== "userMessage") return [item];
      const content = (item.content || []).filter(c => !(c.type === "text" && c.text?.startsWith(prefix)));
      if (routed.has(turn.id) && !hidden.has(turn.id)) { hidden.add(turn.id); return []; }
      return content.length ? [{...item, content}] : [];
    })}));
    for (const receipt of receipts) result.push({id: "dispatch-" + receipt.id, status: "completed", startedAt: receipt.created_at,
      items: [{id: "dispatch-input-" + receipt.id, type: "userMessage", content: [
        {type: "text", text: receipt.body.message}, ...(receipt.body.images || []).map(image => ({type: "image", url: image.url}))]},
      {id: "dispatch-receipt-" + receipt.id, type: "dispatchReceipt", receipt}]});
    const stamp = turn => typeof turn.startedAt === "number" ? turn.startedAt : Date.parse(turn.startedAt || turn.started_at) / 1000;
    return result.map((turn, index) => ({turn, index})).sort((a, b) => {
      const first = stamp(a.turn), second = stamp(b.turn);
      return Number.isFinite(first) && Number.isFinite(second) ? first - second || a.index - b.index : a.index - b.index;
    }).map(entry => entry.turn);
  }
  function card(document, receipt, {open, dismiss} = {}) {
    const article = document.createElement("article"); article.className = "dispatch-receipt";
    const labels = {pending: "Finding a conversation…", routing: "Finding a conversation…", creating: "Creating a conversation…",
      queued: "Queued", sending: "Sending…", accepted: "Sent", paused: "Delivery paused", cancelled: "Cancelled",
      failed: "Routing failed", uncertain: "Delivery needs review", dismissed: "Dismissed"};
    const label = document.createElement("span");
    label.textContent = (receipt.target_id === receipt.dispatcher_id && receipt.state === "accepted") ? "Passed to dispatcher" : labels[receipt.state] || receipt.state;
    article.append(label);
    if (receipt.target_id && receipt.target_id !== receipt.dispatcher_id) {
      const link = document.createElement("a"); link.textContent = receipt.target_title || "Open conversation";
      link.href = "/#" + encodeURIComponent(receipt.target_id);
      link.onclick = event => {
        if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); open?.(receipt.target_id);
      };
      article.append(link);
    }
    if (receipt.error) { const error = document.createElement("span"); error.textContent = receipt.error; article.append(error); }
    if (["failed", "uncertain"].includes(receipt.state)) {
      const button = document.createElement("button"); button.type = "button"; button.className = "quiet";
      button.textContent = "Dismiss"; button.title = "Acknowledge this receipt without sending the request again";
      button.onclick = () => dismiss?.(receipt.id); article.append(button);
    }
    return article;
  }
  function requestKey(storage, thread, body, uuid = () => root.crypto.randomUUID()) {
    // Store only a fingerprint and key, never prompt or image content.
    let hash = 2166136261; const text = JSON.stringify(body);
    for (let i = 0; i < text.length; i++) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
    const signature = text.length + ":" + (hash >>> 0), key = "dispatch-request:" + thread;
    let previous;
    try { previous = JSON.parse(storage.getItem(key)); } catch (_) {}
    if (previous?.signature === signature) return previous.id;
    const id = uuid(); storage.setItem(key, JSON.stringify({signature, id})); return id;
  }
  root.DispatchUI = {turns, card, requestKey};
  if (typeof module !== "undefined") module.exports = root.DispatchUI;
})(globalThis);
