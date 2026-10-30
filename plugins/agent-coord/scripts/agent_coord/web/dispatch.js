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
    const input = Object.fromEntries(Object.entries(body).filter(([key]) =>
      !["requestId", "windowId", "activity", "expectedTurnId"].includes(key)));
    let hash = 2166136261; const text = JSON.stringify(input);
    for (let i = 0; i < text.length; i++) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
    const signature = text.length + ":" + (hash >>> 0), key = "dispatch-request:" + thread;
    let previous;
    try { previous = JSON.parse(storage.getItem(key)); } catch (_) {}
    if (previous?.signature === signature) return previous.id;
    const id = uuid(); storage.setItem(key, JSON.stringify({signature, id})); return id;
  }
  function chooseDispatcher(threads, cwd, preferred) {
    if (!cwd) return null;
    const candidates = threads.filter(t => t.dispatcher && t.browser_session && t.attention !== "archived" &&
      (t.project_cwd || t.cwd) === cwd);
    return candidates.find(t => t.thread_id === preferred) || candidates.sort((a, b) =>
      (b.last_work_at || b.updated_at || 0) - (a.last_work_at || a.updated_at || 0) || a.thread_id.localeCompare(b.thread_id))[0] || null;
  }
  function handoffs(detail) {
    return [...(detail.dispatchRequests || []).map(receipt => ({key: "request:" + receipt.id,
      target: receipt.target_id !== receipt.dispatcher_id ? receipt.target_id : null,
      title: receipt.target_title, state: receipt.state, createdAt: receipt.created_at, receipt})),
    ...(detail.coordinationMessages || []).filter(m => m.direction === "sent" && m.classification === "action_required" &&
      !m.suppressed_at && !m.closed_at).map(message => ({key: "message:" + message.id,
      target: message.counterpart.session_id, title: message.counterpart.title,
      state: "accepted", createdAt: message.created_at, message}))]
      .sort((a, b) => a.createdAt - b.createdAt || a.key.localeCompare(b.key));
  }
  class HandoffTracker {
    constructor() { this.threads = new Map(); }
    update(id, entries) {
      const seen = this.threads.get(id), accepted = entries.filter(e => e.state === "accepted" && e.target);
      const fresh = seen ? accepted.filter(e => !seen.has(e.key)) : [];
      this.threads.set(id, new Set([...(seen || []), ...accepted.map(e => e.key)]));
      return fresh;
    }
  }
  class Dock {
    constructor({document, view, storage, open, dismiss, onExpand}) {
      Object.assign(this, {document, view, storage, open, dismiss, onExpand});
      this.$ = id => document.getElementById(id);
      this.home = document.createComment("conversation home");
      this.$("conversation").before(this.home);
      this.tracker = new HandoffTracker(); this.pending = new Map(); this.motions = new Set();
      this.preferences = new Map();
      this.reduced = view.matchMedia("(prefers-reduced-motion: reduce)");
      this.$("dispatcher-expand").onclick = () => this.expand(!this.expanded);
      this.reduced.addEventListener("change", () => this.stopMotion());
      view.addEventListener("resize", () => this.stopMotion(), {passive: true});
      view.addEventListener("scroll", () => this.stopMotion(), {capture: true, passive: true});
    }
    preference(cwd) {
      if (!this.preferences.has(cwd)) {
        let saved;
        try { saved = JSON.parse(this.storage?.getItem("ribbon-field.dispatcher:" + cwd)); } catch { /* Optional preference. */ }
        this.preferences.set(cwd, saved && typeof saved === "object" ? saved : {});
      }
      return this.preferences.get(cwd);
    }
    remember(cwd, values) {
      const preference = {...this.preference(cwd), ...values};
      this.preferences.set(cwd, preference);
      try { this.storage?.setItem("ribbon-field.dispatcher:" + cwd, JSON.stringify(preference)); } catch { /* Works without storage. */ }
    }
    show(id, cwd) {
      this.active = id; this.cwd = cwd; this.signature = null; this.latestFallback = null; this.initialized = false;
      this.expanded = undefined;
      this.tracker.threads.delete(id);
      this.$("dispatcher-body").append(this.$("conversation"));
      this.$("dispatcher-dock").hidden = false;
      this.document.body.classList.add("dispatcher-docked");
      this.$("dispatcher-project").textContent = cwd.split("/").filter(Boolean).pop() || cwd;
      this.$("dispatcher-project").title = cwd;
      this.$("dispatcher-receipt").replaceChildren();
      this.expand(false);
    }
    hide() {
      this.stopMotion(); this.pending.clear(); this.active = null;
      this.home.after(this.$("conversation"));
      this.$("dispatcher-dock").hidden = true;
      this.document.body.classList.remove("dispatcher-docked");
    }
    expand(value) {
      if (this.expanded === !!value) return;
      this.expanded = !!value;
      this.$("dispatcher-dock").classList.toggle("expanded", this.expanded);
      this.$("dispatcher-expand").setAttribute("aria-expanded", String(this.expanded));
      this.$("dispatcher-expand").textContent = this.expanded ? "Collapse conversation ⌄" : "Conversation ⌃";
      const mobile = this.$("dispatcher-mobile-conversation");
      mobile?.setAttribute("aria-expanded", String(this.expanded));
      if (mobile) mobile.textContent = this.expanded ? "Close chat" : "Chat";
      this.onExpand?.(this.expanded);
    }
    update(detail) {
      const id = detail?.work_thread?.thread_id;
      if (id !== this.active) return;
      const entries = handoffs(detail);
      for (const entry of this.tracker.update(id, entries)) this.pending.set(entry.key, {...entry, observedAt: Date.now()});
      const latest = entries.at(-1), signature = JSON.stringify(latest);
      if (signature !== this.signature) {
        this.signature = signature;
        const area = this.$("dispatcher-receipt"); area.replaceChildren();
        if (latest?.receipt) area.append(card(this.document, latest.receipt, {open: this.open, dismiss: this.dismiss}));
        else if (latest) {
          const label = this.document.createElement("span"); label.textContent = "Sent to ";
          const link = this.document.createElement("a"); link.textContent = latest.title || "Open conversation";
          link.href = "/#" + encodeURIComponent(latest.target);
          link.onclick = event => {
            if (event.button || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
            event.preventDefault(); this.open(latest.target);
          };
          area.append(label, link);
        } else {
          area.textContent = "Send a request. Your agents answer in their own conversations.";
        }
      }
      const fallback = (detail.dispatchRequests || []).filter(r => r.target_id === id).at(-1);
      if (fallback && this.latestFallback !== fallback.id) {
        if (this.initialized || ["pending", "routing", "queued", "sending"].includes(fallback.state) || detail.running) this.expand(true);
        this.latestFallback = fallback.id;
      }
      if (detail.requests?.length) this.expand(true);
      this.initialized = true;
      this.$("dispatcher-expand").classList.toggle("has-reply", !!detail.work_thread.unread_result);
      this.$("dispatcher-mobile-conversation")?.classList.toggle("has-reply", !!detail.work_thread.unread_result);
    }
    stopMotion() {
      for (const stop of this.motions) stop();
      this.motions.clear();
    }
    flush() {
      if (!this.active || this.document.hidden) { this.pending.clear(); return; }
      const overview = this.$("overview"), viewport = this.$("welcome").getBoundingClientRect();
      const source = this.$("composer").getBoundingClientRect();
      for (const [key, entry] of this.pending) {
        const target = [...overview.querySelectorAll("[data-card-thread]")].find(el => el.dataset.cardThread === entry.target);
        if (!target && Date.now() - entry.observedAt < 10000) continue; // New agents arrive on the next list snapshot.
        this.pending.delete(key);
        if (!target) continue;
        const rect = target.getBoundingClientRect(), board = target.closest(".phase-board")?.getBoundingClientRect();
        const left = Math.max(viewport.left, board?.left ?? 0), right = Math.min(viewport.right, board?.right ?? this.view.innerWidth);
        if (!rect.width || rect.bottom <= viewport.top || rect.top >= viewport.bottom || rect.left < left || rect.right > right) continue;
        if (this.reduced.matches || !target.animate) continue;
        const pulse = target.animate([{outline: "2px solid transparent", outlineOffset: "3px"},
          {outline: "2px solid var(--accent)", outlineOffset: "3px"}, {outline: "2px solid transparent", outlineOffset: "3px"}],
          {duration: 950, delay: 300});
        const dot = this.document.createElement("span"); dot.className = "dispatcher-flight";
        dot.textContent = "↗"; dot.setAttribute("aria-hidden", "true");
        const x = Math.min(source.right - 32, right - 16), y = source.top + 18;
        Object.assign(dot.style, {left: x + "px", top: y + "px"}); this.document.body.append(dot);
        const dx = rect.left + rect.width / 2 - x, dy = Math.max(rect.top + 16, viewport.top + 16) - y;
        const flight = dot.animate([{transform: "translate(-50%, -50%) scale(.7)", opacity: 0},
          {transform: "translate(-50%, -50%) scale(1)", opacity: 1, offset: .12},
          {transform: `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px)) scale(.65)`, opacity: 0}],
          {duration: 550, easing: "cubic-bezier(.22,.65,.3,1)"});
        const stop = () => { flight.cancel(); pulse.cancel(); dot.remove(); this.motions.delete(stop); };
        this.motions.add(stop); pulse.onfinish = stop;
      }
    }
  }
  root.DispatchUI = {turns, card, requestKey, chooseDispatcher, handoffs, HandoffTracker, Dock};
  if (typeof module !== "undefined") module.exports = root.DispatchUI;
})(globalThis);
