/* OpenUI is loaded only inside isolated frames, and only for agent fences. */
(function (root) {
  "use strict";
  const cards = new Map(), records = new Map();
  const MAX_SOURCE = 256 * 1024, MAX_ACTION = 32 * 1024;
  function actionMessage(event) {
    if (!event || typeof event !== "object" || Array.isArray(event)) throw new Error("Invalid OpenUI response.");
    const payload = JSON.stringify(event);
    if (payload.length > MAX_ACTION) throw new Error("This OpenUI response is too large to send.");
    return "OpenUI response:\n" + payload;
  }
  // Retain mounted frames. Detaching an iframe during a polling refresh reloads
  // its document and discards in-progress form input.
  function reconcileChildren(container, desired) {
    const keep = new Set(desired);
    for (const child of [...container.childNodes]) if (!keep.has(child)) child.remove();
    let cursor = container.firstChild;
    for (const child of desired) {
      if (child === cursor) cursor = cursor.nextSibling;
      else if (child.parentNode === container && container.moveBefore) container.moveBefore(child, cursor);
      else container.insertBefore(child, cursor);
    }
  }
  function commit(container, fragment, options) {
    const live = new Set(), nextCards = new Map();
    const desired = [...fragment.childNodes].map(child => {
      const key = child.dataset?.openuiKey;
      if (!key) return child;
      const old = cards.get(key);
      const card = old?.dataset.openuiText === child.dataset.openuiText ? old : child;
      if (card !== child) {
        const previousTime = card.querySelector?.(".message-time"), nextTime = child.querySelector?.(".message-time");
        if (previousTime && nextTime && previousTime.outerHTML !== nextTime.outerHTML) previousTime.replaceWith(nextTime);
      }
      card.dataset.openuiStreaming = child.dataset.openuiStreaming;
      nextCards.set(key, card);
      [...card.querySelectorAll(".openui-block")].forEach((block, index) => {
        const id = key + ":" + (index + 1);
        live.add(id);
        const source = block.querySelector("code").textContent;
        let record = records.get(id);
        if (record?.block !== block) {
          const savedState = record?.source === source ? record.state : null;
          dispose(record);
          record = mount(block, id, source, savedState, options);
          records.set(id, record);
        }
        record.options = options;
        record.streaming = card.dataset.openuiStreaming === "true";
        const disabled = !!options.disabled || record.streaming;
        if (record.disabled !== disabled || record.theme !== options.theme) {
          record.disabled = disabled; record.theme = options.theme;
          update(record);
        }
      });
      return card;
    });
    for (const [id, record] of records) if (!live.has(id)) { dispose(record); records.delete(id); }
    cards.clear(); for (const [key, card] of nextCards) cards.set(key, card);
    reconcileChildren(container, desired);
  }
  function update(record) {
    record.port?.postMessage({type: "render", response: record.source, state: record.state,
      disabled: record.disabled, theme: record.theme || "light"});
  }
  function availability(thread, disabled) {
    for (const record of records.values()) if (record.options.thread === thread) {
      const value = !!disabled || record.streaming;
      if (record.disabled !== value) { record.disabled = value; update(record); }
    }
  }
  function dispose(record) { if (record) { clearTimeout(record.timer); record.port?.close(); record.frame?.remove(); } }
  function mount(block, id, source, state, options) {
    const doc = block.ownerDocument, preview = block.querySelector(".openui-preview");
    const status = doc.createElement("p"); status.className = "openui-status"; status.setAttribute("role", "status");
    status.textContent = "Loading interactive UI…";
    preview.append(status);
    const record = {block, source, state, options, status, disabled: !!options.disabled, theme: options.theme, ready: false};
    const fail = message => {
      status.textContent = message; block.querySelector("details").open = true;
      if (record.frame) record.frame.style.display = "none";
    };
    if (source.length > MAX_SOURCE) { fail("This OpenUI block is too large to preview."); return record; }
    const frame = record.frame = doc.createElement("iframe");
    frame.title = "Interactive agent response";
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.src = "/openui-frame.html";
    frame.onload = () => {
      record.port?.close();
      const channel = new MessageChannel(); record.port = channel.port1;
      channel.port1.onmessage = async ({data}) => {
        if (!data || records.get(id) !== record) return;
        if (data.type === "ready") { record.ready = true; clearTimeout(record.timer); frame.style.display = ""; status.textContent = ""; update(record); }
        if (data.type === "height" && Number.isFinite(data.height)) frame.style.height = Math.max(80, Math.min(1400, data.height)) + "px";
        if (data.type === "state") record.state = data.state;
        if (data.type === "error") fail(String(data.message).slice(0, 300));
        if (data.type === "action") {
          if (record.pending || record.disabled) { record.port.postMessage({type: "settled"}); return; }
          record.pending = true; status.textContent = "Sending to thread…";
          try {
            await record.options.onAction(actionMessage(data.event));
            status.textContent = "Sent to thread.";
          } catch (error) { status.textContent = error.message || "Could not send. Please try again."; }
          finally { record.pending = false; record.port?.postMessage({type: "settled"}); }
        }
      };
      channel.port1.start();
      frame.contentWindow.postMessage({type: "rf-openui-init"}, "*", [channel.port2]);
    };
    record.timer = setTimeout(() => { if (!record.ready) fail("OpenUI could not load. Its source is available below."); }, 15000);
    preview.prepend(frame);
    return record;
  }
  const api = {commit, actionMessage, reconcileChildren, availability};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else {
    root.OpenUIThread = api;
    new MutationObserver(() => {
      const theme = document.documentElement.dataset.theme || "light";
      for (const record of records.values()) if (record.theme !== theme) { record.theme = theme; update(record); }
    }).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
  }
})(globalThis);
