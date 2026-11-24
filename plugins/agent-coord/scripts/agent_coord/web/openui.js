/* OpenUI is loaded only inside isolated frames, and only for agent fences. */
(function (root) {
  "use strict";
  const containers = new Map(), records = new Map();
  let containerSequence = 0;
  // Six active panes retain their live cards. Evict the least recently parked
  // document when inactive frames would otherwise grow with every page read.
  const parked = new Map(), maximumParked = 32;
  function evictParked() {
    while (parked.size > maximumParked) {
      const [card, owner] = parked.entries().next().value;
      parked.delete(card);
      if (owner.cards.get(card.dataset.openuiKey) === card) owner.cards.delete(card.dataset.openuiKey);
      for (const [id, record] of records) if (card.contains(record.block)) {
        dispose(record); records.delete(id);
      }
      card.remove();
    }
  }
  function context(container) {
    let value = containers.get(container);
    if (!value) {
      value = {id: ++containerSequence, cards: new Map(), parking: null};
      containers.set(container, value);
    }
    return value;
  }
  function retain(container, card) {
    const owner = context(container);
    if (container.moveBefore) {
      if (!owner.parking) {
        owner.parking = container.ownerDocument.createElement("div");
        owner.parking.hidden = true;
        container.ownerDocument.body.append(owner.parking);
      }
      owner.parking.moveBefore(card, null);
    } else {
      // Older WebKit reloads any reparented browsing context. Keep the card
      // connected at its original DOM position until the conversation evicts it.
      card.hidden = true;
      card.dataset.openuiParked = "true";
    }
    parked.delete(card); parked.set(card, owner);
    evictParked();
  }
  function park(container) {
    for (const card of [...container.children]) if (card.dataset?.openuiKey && !card.dataset.openuiParked) retain(container, card);
  }
  function release(thread, container = null) {
    for (const [host, owner] of containers) {
      if (container && host !== container) continue;
      for (const [key, card] of owner.cards) {
        if (thread && card.dataset.openuiThread !== thread) continue;
        owner.cards.delete(key); parked.delete(card);
        card.remove();
      }
    }
    for (const [id, record] of records) if ((!thread || record.options.thread === thread) && (!container || record.container === container)) {
      dispose(record); records.delete(id);
    }
  }
  function releaseContainer(container) {
    release(null, container);
    containers.get(container)?.parking?.remove();
    containers.delete(container);
  }
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
    for (const child of [...container.childNodes]) if (!keep.has(child) && !child.dataset?.openuiParked) child.remove();
    if (!container.moveBefore) {
      // Work backwards from each retained frame, moving ordinary rows around
      // its fixed position. Hidden cards do not participate in transcript order.
      let next = null;
      for (let index = desired.length - 1; index >= 0; index--) {
        const child = desired[index];
        let sibling = child.nextSibling;
        while (sibling?.dataset?.openuiParked) sibling = sibling.nextSibling;
        if (!(child.parentNode === container && (child.dataset?.openuiKey || child.contains(container.ownerDocument?.activeElement))) &&
            (child.parentNode !== container || sibling !== next)) container.insertBefore(child, next);
        next = child;
      }
      return;
    }
    let cursor = container.firstChild;
    for (const child of desired) {
      if (child === cursor) cursor = cursor.nextSibling;
      else if (child.isConnected && container.isConnected) container.moveBefore(child, cursor);
      else container.insertBefore(child, cursor);
    }
  }
  function unpark(card) {
    parked.delete(card);
    if (card.dataset?.openuiParked) { delete card.dataset.openuiParked; card.hidden = false; }
  }
  function restore(container, nodes) {
    const cards = context(container).cards;
    for (const element of nodes) if (element.dataset?.openuiKey && cards.get(element.dataset.openuiKey) !== element) return false;
    for (const element of nodes) unpark(element);
    reconcileChildren(container, nodes);
    return true;
  }
  function commit(container, fragment, options) {
    const owner = context(container), cards = owner.cards, live = new Set(), nextCards = new Map();
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
      card.dataset.openuiThread = options.thread || "";
      unpark(card);
      nextCards.set(key, card);
      [...card.querySelectorAll(".openui-block")].forEach((block, index) => {
        const id = owner.id + ":" + key + ":" + (index + 1);
        live.add(id);
        const source = block.querySelector("code").textContent;
        let record = records.get(id);
        if (record?.block !== block) {
          const savedState = record?.source === source ? record.state : null;
          dispose(record);
          record = mount(block, id, source, savedState, options);
          record.container = container;
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
    for (const [key, card] of cards) {
      if (!nextCards.has(key) && card.parentNode === container && !card.dataset.openuiParked) retain(container, card);
    }
    for (const [id, record] of records) if (record.container === container && !live.has(id) &&
        nextCards.has(record.block.closest?.("[data-openui-key]")?.dataset.openuiKey)) {
      dispose(record); records.delete(id);
    }
    for (const [key, card] of nextCards) cards.set(key, card);
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
  function dispose(record) { if (record) { clearTimeout(record.timer); record.port?.close(); record.frame?.remove(); record.status?.remove(); } }
  function mount(block, id, source, state, options) {
    const doc = block.ownerDocument, preview = block.querySelector(".openui-preview");
    const status = doc.createElement("p"); status.className = "openui-status"; status.setAttribute("role", "status");
    status.textContent = "Loading interactive UI…";
    preview.append(status);
    const record = {block, source, state, options, status, disabled: !!options.disabled, theme: options.theme, ready: false};
    const fail = message => {
      record.failed = true;
      status.textContent = message; block.querySelector("details").open = true;
      if (record.frame) record.frame.style.display = "none";
    };
    if (source.length > MAX_SOURCE) { fail("This OpenUI block is too large to preview."); return record; }
    const frame = record.frame = doc.createElement("iframe");
    frame.title = "Interactive agent response";
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.src = new URL("/openui-frame.html", doc.location.origin).href;
    frame.onload = () => {
      record.port?.close();
      const channel = new MessageChannel(); record.port = channel.port1;
      channel.port1.onmessage = async ({data}) => {
        if (!data || records.get(id) !== record) return;
        if (data.type === "ready") { record.ready = true; clearTimeout(record.timer); frame.style.display = ""; status.textContent = ""; update(record); }
        if (data.type === "rendered" && record.failed) { record.failed = false; frame.style.display = ""; status.textContent = ""; }
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
  const api = {commit, restore, actionMessage, reconcileChildren, availability, park, release, releaseContainer};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else {
    root.OpenUIThread = api;
    new MutationObserver(() => {
      const theme = document.documentElement.dataset.theme || "light";
      for (const record of records.values()) if (record.theme !== theme) { record.theme = theme; update(record); }
    }).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
  }
})(globalThis);
