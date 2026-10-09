/* Durable agent-to-agent messages, rendered separately from provider prose. */
(function (root) {
  "use strict";
  const WAKE_PROMPT = "Check and handle your unread agent-coord messages.";
  function timestamp(value) {
    if (value == null || value === "") return NaN;
    return typeof value === "number" ? (value > 1e12 ? value / 1000 : value) : Date.parse(value) / 1000;
  }
  function entries(turns, messages) {
    const pending = [...messages].sort((a, b) => a.created_at - b.created_at || a.id - b.id);
    const result = [], used = new Set();
    const take = predicate => {
      for (const message of pending) if (!used.has(message.id) && predicate(message)) {
        used.add(message.id); result.push({message});
      }
    };
    for (const turn of turns) {
      const started = timestamp(turn.startedAt ?? turn.started_at), ended = timestamp(turn.completedAt);
      const anchored = pending.filter(message => message.turn_id === turn.id);
      const before = Number.isFinite(started) ? started : Math.max(-Infinity,
        ...anchored.filter(message => message.direction === "received").map(message => message.created_at));
      take(message => message.created_at <= before);
      const items = turn.items || [];
      let previous = started;
      for (const [index, item] of items.entries()) {
        let stamp = timestamp(item.timelineAt);
        if (!Number.isFinite(stamp)) {
          stamp = index === items.length - 1 && item.type === "agentMessage" && Number.isFinite(ended) ? ended : previous;
        }
        if (Number.isFinite(stamp)) {
          previous = stamp;
          take(message => message.created_at <= stamp);
        }
        result.push({turn, item});
      }
      const through = Math.max(Number.isFinite(ended) ? ended : -Infinity, ...anchored.map(message => message.created_at));
      take(message => message.created_at <= through);
      result.push({turn, end: true});
    }
    take(() => true);
    return result;
  }
  function isWakePrompt(item, turn, messages) {
    return item.type === "userMessage" && (item.content || []).map(part => part.text || "").join("\n") === WAKE_PROMPT &&
      messages.some(message => message.started_turn && message.wake_turn_id === turn.id);
  }
  function card(document, message, {open, expanded = false} = {}) {
    const el = (tag, text, className) => {
      const element = document.createElement(tag);
      if (text != null) element.textContent = text;
      if (className) element.className = className;
      return element;
    };
    const outgoing = message.direction === "sent", peer = message.counterpart;
    const created = outgoing && Boolean(message.creation_request_id);
    const prefix = created ? "Created agent " : "Message " + (outgoing ? "to " : "from ");
    const article = el("article", null, "agent-message " + message.direction);
    article.id = "agent-message-" + message.id;
    article.dataset.messageId = String(message.id);
    article.tabIndex = -1;
    article.setAttribute("aria-label", prefix + peer.title);
    const heading = el("div", null, "agent-message-heading");
    const icon = el("span", outgoing ? "↗" : "↙", "agent-message-icon");
    icon.setAttribute("aria-hidden", "true");
    const label = el("span", prefix, "agent-message-label");
    const link = el(peer.available ? "a" : "span", peer.title, "agent-message-peer");
    if (peer.available) {
      link.href = "/?message=" + encodeURIComponent(message.id) + "#" + encodeURIComponent(peer.session_id);
      link.title = "Open matching message in " + peer.title;
      link.onclick = event => {
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); open?.(peer.session_id, message.id);
      };
    } else link.title = "This conversation is outside the current workspace.";
    label.append(link);
    const time = el("time", new Date(message.created_at * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}), "agent-message-time");
    time.dateTime = new Date(message.created_at * 1000).toISOString();
    heading.append(icon, label, time); article.append(heading);
    const text = message.body || "";
    if (text.length > 240 || text.split("\n").length > 4) {
      const details = el("details", null, "agent-message-content");
      details.dataset.id = "agent-message:" + message.id;
      details.open = expanded;
      const preview = text.replace(/\s+/g, " ").slice(0, 180) + "…";
      const summary = el("summary");
      const updateSummary = () => { summary.textContent = details.open ? "Collapse message" : preview; };
      updateSummary();
      details.ontoggle = updateSummary;
      details.append(summary, el("div", text, "agent-message-body"));
      article.append(details);
    } else article.append(el("div", text, "agent-message-body"));
    const footer = el("div", null, "agent-message-status");
    footer.append(el("span", message.status));
    if (message.reply_required && !message.reply_id && !message.closed_at && !message.suppressed_at) footer.append(el("span", "Reply requested"));
    if (message.classification === "informational") footer.append(el("span", "Informational"));
    if (message.started_turn) footer.append(el("span", "Started this turn", "agent-message-cause"));
    if (message.wake_outcome === "failed" && !message.wake_turn_id && !message.delivered_at && !message.closed_at) footer.append(el("span", "Wake failed · Message retained"));
    article.append(footer);
    return article;
  }
  root.AgentMessages = {entries, card, isWakePrompt};
  if (typeof module !== "undefined" && module.exports) module.exports = root.AgentMessages;
})(globalThis);
