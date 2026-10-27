"use strict";

// A full card-and-connector tree. It navigates conversations; it never starts,
// stops, or reparents an agent. Creation provenance is the relationship shown.
const AgentTree = (() => {
  const grouping = typeof module !== "undefined" ? require("./thread-groups.js") : threadGrouping;
  const CARD_W = 232, CARD_H = 152, ROW_GAP = 24, INDENT = 28, COLUMN_GAP = 32;
  function graph(threads) {
    const nodes = new Map(threads.filter(t => t?.thread_id).map(t => [t.thread_id, t]));
    const parents = new Map(), children = new Map();
    for (const t of nodes.values()) {
      const parent = Object.hasOwn(t, "agent_parent_id") ? t.agent_parent_id
        : t.creation_mode === "orchestration" ? t.created_by_thread_id : t.parent?.session_id;
      if (parent && parent !== t.thread_id && nodes.has(parent)) parents.set(t.thread_id, parent);
    }
    // Break corrupt historical cycles deterministically, retaining every node.
    for (const id of [...nodes.keys()].sort()) {
      const seen = new Set([id]);
      let next = parents.get(id);
      while (next && !seen.has(next)) { seen.add(next); next = parents.get(next); }
      if (next) parents.delete(id);
    }
    for (const [id, parent] of parents) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(id);
    }
    for (const ids of children.values()) ids.sort((a, b) =>
      (nodes.get(a).created_at || 0) - (nodes.get(b).created_at || 0) || a.localeCompare(b));
    function root(id) { while (parents.has(id)) id = parents.get(id); return id; }
    function family(id) {
      const result = [], pending = nodes.has(id) ? [id] : [];
      while (pending.length) {
        const next = pending.pop(); result.push(next);
        pending.push(...(children.get(next) || []).slice().reverse());
      }
      return result;
    }
    return {nodes, parents, children, root, family};
  }
  function layout(tree, root, collapsed = new Set()) {
    if (!tree.nodes.has(root)) return {nodes: [], edges: [], width: 0, height: 0};
    const nodes = [], edges = [], positions = new Map();
    const add = (id, x, y, level, parent = null) => {
      const item = {id, x, y, level, parent}; nodes.push(item); positions.set(id, item);
      if (parent) edges.push({from: parent, to: id});
    };
    const first = collapsed.has(root) ? [] : tree.children.get(root) || [];
    // Keep wide sibling groups from becoming a single, endlessly wide row.
    // Each branch retains its own column, with rows sized by the tallest branch.
    const columns = first.length > 3 ? 3 : Math.max(1, first.length);
    let left = 28, rowTop = 28 + CARD_H + 64, bottom = 28 + CARD_H, width = CARD_W + 56;
    for (let index = 0; index < first.length; index++) {
      const id = first[index];
      if (index && index % columns === 0) {
        width = Math.max(width, left - COLUMN_GAP + 28);
        left = 28; rowTop = bottom + 64;
      }
      let y = rowTop, maxDepth = 0;
      const pending = [{id, depth: 0, parent: root}];
      while (pending.length) {
        const item = pending.pop();
        add(item.id, left + item.depth * INDENT, y, item.depth + 2, item.parent);
        maxDepth = Math.max(maxDepth, item.depth);
        y += CARD_H + ROW_GAP;
        if (!collapsed.has(item.id)) for (const child of (tree.children.get(item.id) || []).slice().reverse())
          pending.push({id: child, depth: item.depth + 1, parent: item.id});
      }
      bottom = Math.max(bottom, y - ROW_GAP);
      left += CARD_W + maxDepth * INDENT + COLUMN_GAP;
    }
    if (first.length) width = Math.max(width, left - COLUMN_GAP + 28);
    add(root, (width - CARD_W) / 2, 28, 1);
    nodes.sort((a, b) => a.level === 1 ? -1 : b.level === 1 ? 1 : a.x - b.x || a.y - b.y);
    for (const edge of edges) {
      const a = positions.get(edge.from), b = positions.get(edge.to);
      if (edge.from === root) {
        const x1 = a.x + CARD_W / 2, y1 = a.y + CARD_H, x2 = b.x + CARD_W / 2, y2 = b.y;
        edge.path = first.length > columns
          ? `M${x1},${y1} V${y1 + 16} H14 V${y2 - 32} H${x2} V${y2}`
          : `M${x1},${y1} V${y1 + 32} H${x2} V${y2}`;
      } else {
        const x = b.x - 14, y = b.y + 24;
        edge.path = `M${a.x + 14},${a.y + CARD_H} V${y} H${b.x}`;
        // The vertical child rail stays to the left of its own card.
        edge.path = `M${x},${a.y + CARD_H} V${y} H${b.x}`;
      }
    }
    return {nodes, edges, width, height: bottom + 28};
  }
  function status(t) {
    const value = grouping.status(t);
    if (value.key === "running") return {key: "running", label: "Running"};
    if (t.response_state === "failed" || t.turn_completion?.status === "failed") return {key: "failed", label: "Failed"};
    if (t.response_state === "input" || t.status === "needs input" ||
        !t.checkpoint_stale && t.checkpoint?.next_actor === "user" && t.checkpoint.next_action)
      return {key: "waiting", label: "Needs you"};
    if (t.attention === "archived") return {key: "closed", label: "Closed"};
    if (t.turn_completion?.status === "interrupted") return {key: "idle", label: "Stopped"};
    if (grouping.phase(t) === "finished") return {key: "done", label: "Done"};
    return {key: "idle", label: "Idle"};
  }
  function duration(t, now = Date.now()) {
    if (!t.turn_started_at) return "";
    const end = status(t).key === "running" ? now / 1000 : t.turn_completion?.completed_at;
    if (!end) return "";
    const seconds = Math.max(0, Math.round(end - t.turn_started_at));
    return seconds >= 60 ? Math.floor(seconds / 60) + "m " + seconds % 60 + "s" : seconds + "s";
  }

  class Panel {
    constructor({document, window, host, onSelect, onMode, providerLogo}) {
      Object.assign(this, {document, window, host, onSelect, onMode, providerLogo});
      this.views = new Map(); this.cardEls = new Map(); this.mode = "context";
      this.messages = new Map(); this.lastMessageId = 0;
      const el = this.el = (tag, cls, text) => {
        const n = document.createElement(tag); if (cls) n.className = cls;
        if (text != null) n.textContent = text; return n;
      };
      const button = this.button = (text, label, fn) => {
        const n = el("button", "quiet", text); n.type = "button"; n.setAttribute("aria-label", label); n.title = label; n.onclick = fn; return n;
      };
      this.toolbar = el("div", "agent-tree-toolbar");
      this.summary = el("span", "agent-tree-summary"); this.summary.setAttribute("role", "status");
      this.attention = button("", "Find agent needing attention", () => this.findAttention());
      this.toolbar.append(this.summary, this.attention);
      this.viewport = el("div", "agent-tree-viewport"); this.viewport.tabIndex = 0;
      this.viewport.setAttribute("aria-label", "Agent tree canvas. Drag to pan; use zoom controls to resize.");
      this.scene = el("div", "agent-tree-scene"); this.scene.setAttribute("role", "tree");
      this.scene.setAttribute("aria-label", "Agent conversations");
      this.svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      this.svg.classList.add("agent-tree-connectors"); this.svg.setAttribute("aria-hidden", "true");
      this.messageSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      this.messageSvg.classList.add("agent-tree-messages"); this.messageSvg.setAttribute("aria-hidden", "true");
      this.messageStatus = el("span", "sr-only"); this.messageStatus.setAttribute("role", "status");
      this.toolbar.append(this.messageStatus);
      this.scene.append(this.svg, this.messageSvg); this.viewport.append(this.scene);
      this.controls = el("div", "agent-tree-controls");
      this.zoomLabel = el("span", "agent-tree-zoom");
      this.controls.append(button("−", "Zoom out", () => this.zoom(.8)), this.zoomLabel,
        button("+", "Zoom in", () => this.zoom(1.25)), button("Fit", "Fit full tree", () => this.fit()),
        button("100%", "Actual card size", () => this.zoom(1 / this.view.scale)),
        button("Show all", "Expand every branch", () => { this.view.collapsed.clear(); this.signature = ""; this.render(); this.fit(); }));
      this.empty = el("p", "agent-tree-empty", "Loading agent tree…");
      host.append(this.toolbar, this.viewport, this.controls, this.empty);
      this.viewport.addEventListener("pointerdown", e => {
        if (e.button !== 0 || e.target.closest("button, [role=treeitem]") || !this.view) return;
        this.drag = {x: e.clientX, y: e.clientY, left: this.view.x, top: this.view.y};
        this.viewport.setPointerCapture(e.pointerId); this.viewport.classList.add("panning"); e.preventDefault();
      });
      this.viewport.addEventListener("pointermove", e => {
        if (!this.drag) return;
        Object.assign(this.view, {x: this.drag.left + e.clientX - this.drag.x, y: this.drag.top + e.clientY - this.drag.y, fitted: false});
        this.transform();
      });
      const end = () => { this.drag = null; this.viewport.classList.remove("panning"); };
      this.viewport.addEventListener("pointerup", end); this.viewport.addEventListener("pointercancel", end);
      this.viewport.addEventListener("wheel", e => {
        if (!this.view) return; e.preventDefault();
        if (e.ctrlKey || e.metaKey) this.zoom(Math.exp(-e.deltaY * .008));
        else { this.view.x -= e.deltaX; this.view.y -= e.deltaY; this.view.fitted = false; this.transform(); }
      }, {passive: false});
      this.viewport.addEventListener("keydown", e => this.keydown(e));
      if (window.ResizeObserver) {
        this.observer = new window.ResizeObserver(() => { if (this.view?.fitted) this.fit(this.view.fullFit); });
        this.observer.observe(this.viewport);
      }
    }
    setMode(mode) { this.mode = mode === "agents" ? "agents" : "context"; this.onMode?.(this.mode); }
    update(threads, root, selected) {
      const changedSelection = this.selected !== selected || this.root !== root;
      this.tree = graph(threads); this.root = root; this.selected = selected;
      if (!this.views.has(root)) this.views.set(root, {x: 0, y: 0, scale: 1, collapsed: new Set(), fitted: true, fullFit: false});
      this.view = this.views.get(root);
      // A selected agent must stay visible, even when selected outside the tree.
      if (changedSelection) for (let parent = this.tree.parents.get(selected); parent; parent = this.tree.parents.get(parent)) this.view.collapsed.delete(parent);
      this.render();
      if (changedSelection && this.view.fitted && !this.view.fullFit) this.fit(false);
    }
    render() {
      if (!this.tree || !this.view) return;
      const family = this.tree.family(this.root), needs = family.filter(id => ["waiting", "failed"].includes(status(this.tree.nodes.get(id)).key));
      this.summary.textContent = family.length + (family.length === 1 ? " agent" : " agents");
      this.attention.hidden = !needs.length; this.attention.textContent = needs.length + " need attention";
      this.empty.hidden = !!family.length; this.viewport.hidden = !family.length;
      this.controls.hidden = !family.length; this.toolbar.hidden = !family.length;
      const positioned = layout(this.tree, this.root, this.view.collapsed);
      this.positioned = positioned;
      const signature = JSON.stringify([this.root, positioned.nodes, positioned.edges]);
      if (signature !== this.signature) {
        this.clearMessages();
        this.signature = signature;
        this.svg.replaceChildren();
        this.svg.setAttribute("width", positioned.width); this.svg.setAttribute("height", positioned.height);
        this.messageSvg.setAttribute("width", positioned.width); this.messageSvg.setAttribute("height", positioned.height);
        for (const edge of positioned.edges) {
          const path = this.document.createElementNS("http://www.w3.org/2000/svg", "path");
          path.setAttribute("d", edge.path); this.svg.append(path);
        }
        const visible = new Set(positioned.nodes.map(n => n.id));
        for (const [id, card] of this.cardEls) if (!visible.has(id)) { card.remove(); this.cardEls.delete(id); }
        for (const item of positioned.nodes) {
          let card = this.cardEls.get(item.id);
          if (!card) {
            card = this.el("article", "agent-tree-card"); card.dataset.agentId = item.id; card.setAttribute("role", "treeitem");
            card.onclick = e => { if (!e.target.closest("button")) this.onSelect(item.id); };
            this.cardEls.set(item.id, card);
          }
          card.style.left = item.x + "px"; card.style.top = item.y + "px";
          card.setAttribute("aria-level", item.level);
          const siblings = item.parent ? this.tree.children.get(item.parent) : [item.id];
          card.setAttribute("aria-posinset", siblings.indexOf(item.id) + 1); card.setAttribute("aria-setsize", siblings.length);
          this.scene.append(card);
        }
        this.scene.style.width = positioned.width + "px"; this.scene.style.height = positioned.height + "px";
        if (this.view.fitted) this.fit(this.view.fullFit);
      }
      for (const {id} of positioned.nodes) this.renderCard(id);
      this.transform();
    }
    clearMessages() {
      for (const {timer} of this.messages.values()) this.window.clearTimeout(timer);
      this.messages.clear(); this.messageSvg.replaceChildren();
    }
    message({messageId, threadId, recipientId}) {
      if (!Number.isSafeInteger(messageId) || messageId <= this.lastMessageId) return;
      this.lastMessageId = messageId;
      if (!this.positioned || !this.tree.nodes.has(threadId) || !this.tree.nodes.has(recipientId)) return;
      const visible = id => {
        while (id && !this.cardEls.has(id)) id = this.tree.parents.get(id);
        return this.positioned.nodes.find(n => n.id === id);
      };
      const from = visible(threadId), to = visible(recipientId);
      if (!from || !to || from.id === to.id) return;
      const dx = to.x - from.x, dy = to.y - from.y, horizontal = Math.abs(dx) > Math.abs(dy);
      const direction = Math.sign(horizontal ? dx : dy);
      const x1 = from.x + CARD_W / 2 + (horizontal ? direction * CARD_W / 2 : 0);
      const y1 = from.y + CARD_H / 2 + (horizontal ? 0 : direction * CARD_H / 2);
      const x2 = to.x + CARD_W / 2 - (horizontal ? direction * CARD_W / 2 : 0);
      const y2 = to.y + CARD_H / 2 - (horizontal ? 0 : direction * CARD_H / 2);
      const bend = Math.max(24, Math.abs(horizontal ? x2 - x1 : y2 - y1) / 2);
      const cx = horizontal ? direction * bend : 0, cy = horizontal ? 0 : direction * bend;
      const d = `M${x1},${y1} C${x1 + cx},${y1 + cy} ${x2 - cx},${y2 - cy} ${x2},${y2}`;
      const svg = (tag, attrs) => {
        const element = this.document.createElementNS("http://www.w3.org/2000/svg", tag);
        for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
        return element;
      };
      const group = svg("g", {class: "agent-tree-message"});
      group.append(svg("path", {d, class: "agent-tree-message-route"}), svg("path", {
        d: "M-8,-4 L0,0 L-8,4", class: "agent-tree-message-arrow",
        transform: `translate(${x2},${y2}) rotate(${horizontal ? direction > 0 ? 0 : 180 : direction > 0 ? 90 : -90})`}));
      if (!this.window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
        const dot = svg("circle", {r: 4, cx: x1, cy: y1, class: "agent-tree-message-dot"});
        // animateMotion uses its own origin; the fallback coordinates stay at the sender.
        const motion = svg("animateMotion", {path: d, dur: "1.2s", begin: "indefinite", fill: "freeze"});
        dot.append(motion); group.append(dot); this.messageSvg.append(group);
        if (motion.beginElement) { dot.setAttribute("cx", 0); dot.setAttribute("cy", 0); motion.beginElement(); }
      } else this.messageSvg.append(group);
      const remove = id => {
        const item = this.messages.get(id); if (!item) return;
        this.window.clearTimeout(item.timer); item.group.remove(); this.messages.delete(id);
      };
      if (this.messages.size >= 16) remove(this.messages.keys().next().value);
      const timer = this.window.setTimeout(() => remove(messageId), 1500);
      this.messages.set(messageId, {group, timer});
      this.messageStatus.textContent = (this.tree.nodes.get(threadId).title || "Agent") + " → " +
        (this.tree.nodes.get(recipientId).title || "Agent") + ": message sent";
    }
    renderCard(id) {
      const card = this.cardEls.get(id), t = this.tree.nodes.get(id), value = status(t), kids = this.tree.children.get(id) || [];
      const selected = id === this.selected;
      card.classList.toggle("selected", selected); card.dataset.status = value.key;
      card.tabIndex = selected || !this.cardEls.has(this.selected) && id === this.root ? 0 : -1;
      card.setAttribute("aria-selected", String(selected)); card.setAttribute("aria-label", (t.title || "Agent") + ": " + value.label);
      if (kids.length) card.setAttribute("aria-expanded", String(!this.view.collapsed.has(id)));
      else card.removeAttribute("aria-expanded");
      const summary = (!t.checkpoint_stale && t.checkpoint?.next_actor === "user" && t.checkpoint.next_action)
        || t.checkpoint?.summary || t.original_request || "No update yet.";
      const model = t.model || t.session?.model || t.client || "Agent";
      const footer = [duration(t), !t.browser_session ? "Read only" : "", t.unread_result ? "Unread reply" : ""].filter(Boolean).join(" · ");
      const signature = JSON.stringify([t.title, model, value, summary, footer, kids.length, this.view.collapsed.has(id)]);
      if (signature === card.dataset.content) return;
      card.dataset.content = signature;
      const focused = card.contains(this.document.activeElement), onFold = this.document.activeElement?.classList?.contains("agent-tree-fold");
      const heading = this.el("div", "agent-tree-card-heading"), identity = this.el("div", "agent-tree-identity");
      const logo = this.providerLogo?.(t.client); if (logo) identity.append(logo);
      const copy = this.el("div"); const title = this.el("strong", "", t.title || "Untitled agent"); title.title = t.title || "";
      copy.append(title, this.el("span", "agent-tree-model", model)); identity.append(copy);
      heading.append(identity, this.el("span", "agent-tree-status", value.label));
      const task = this.el("p", "agent-tree-task", summary); task.title = summary;
      const foot = this.el("div", "agent-tree-card-footer"); foot.append(this.el("span", "", footer));
      if (kids.length) {
        const folded = this.view.collapsed.has(id);
        const fold = this.button((folded ? "+" : "") + kids.length + (folded ? " ▸" : " ▾"),
          (folded ? "Expand" : "Collapse") + " children of " + (t.title || "agent"),
          () => this.fold(id));
        fold.classList.add("agent-tree-fold"); fold.setAttribute("aria-expanded", String(!folded)); foot.append(fold);
      }
      card.replaceChildren(heading, task, foot);
      if (focused) (onFold ? card.querySelector(".agent-tree-fold") || card : card).focus({preventScroll: true});
    }
    fold(id) {
      if (this.view.collapsed.has(id)) this.view.collapsed.delete(id); else this.view.collapsed.add(id);
      this.render();
    }
    fit(fullFit = true) {
      if (!this.positioned?.width || !this.viewport.clientWidth || !this.viewport.clientHeight) return;
      const {width, height} = this.positioned, w = this.viewport.clientWidth, h = this.viewport.clientHeight;
      this.view.scale = Math.max(fullFit ? .15 : .7, Math.min(1, (w - 24) / width, (h - 24) / height));
      this.view.x = (w - width * this.view.scale) / 2; this.view.y = Math.max(12, (h - height * this.view.scale) / 2);
      const selected = this.positioned.nodes.find(item => item.id === this.selected);
      if (!fullFit && selected) {
        const s = this.view.scale, left = this.view.x + selected.x * s, top = this.view.y + selected.y * s;
        if (left < 0 || left + CARD_W * s > w) this.view.x = w / 2 - (selected.x + CARD_W / 2) * s;
        if (top < 0 || top + CARD_H * s > h) this.view.y = h / 2 - (selected.y + CARD_H / 2) * s;
      }
      this.view.fitted = true; this.view.fullFit = fullFit; this.transform();
    }
    zoom(factor) {
      if (!this.view) return;
      const before = this.view.scale, after = Math.max(.15, Math.min(2, before * factor));
      const x = this.viewport.clientWidth / 2, y = this.viewport.clientHeight / 2;
      Object.assign(this.view, {scale: after, x: x - (x - this.view.x) * after / before,
        y: y - (y - this.view.y) * after / before, fitted: false});
      this.transform();
    }
    transform() {
      if (!this.view) return;
      this.scene.style.transform = `translate(${this.view.x}px, ${this.view.y}px) scale(${this.view.scale})`;
      this.zoomLabel.textContent = Math.round(this.view.scale * 100) + "%";
    }
    reveal(id, focus = true) {
      const item = this.positioned.nodes.find(n => n.id === id); if (!item) return;
      const w = this.viewport.clientWidth, h = this.viewport.clientHeight, s = this.view.scale;
      Object.assign(this.view, {x: w / 2 - (item.x + CARD_W / 2) * s,
        y: h / 2 - (item.y + CARD_H / 2) * s, fitted: false}); this.transform();
      if (focus) this.cardEls.get(id)?.focus({preventScroll: true});
    }
    findAttention() {
      const ids = this.tree.family(this.root).filter(id => ["waiting", "failed"].includes(status(this.tree.nodes.get(id)).key));
      const id = ids[(ids.indexOf(this.selected) + 1) % ids.length]; if (!id) return;
      for (let p = this.tree.parents.get(id); p; p = this.tree.parents.get(p)) this.view.collapsed.delete(p);
      this.render(); this.reveal(id); this.onSelect(id);
    }
    keydown(e) {
      const card = e.target.closest("[role=treeitem]");
      if (!card || e.target.closest("button")) return;
      const id = card.dataset.agentId, order = this.tree.family(this.root).filter(id => this.cardEls.has(id));
      let target;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.onSelect(id); return; }
      if (e.key === "ArrowDown") target = order[Math.min(order.length - 1, order.indexOf(id) + 1)];
      if (e.key === "ArrowUp") target = order[Math.max(0, order.indexOf(id) - 1)];
      if (e.key === "Home") target = this.root;
      if (e.key === "End") target = order[order.length - 1];
      if (e.key === "ArrowRight") {
        if (this.view.collapsed.has(id)) this.fold(id); else target = this.tree.children.get(id)?.[0];
      }
      if (e.key === "ArrowLeft") {
        if (this.tree.children.has(id) && !this.view.collapsed.has(id)) this.fold(id);
        else target = this.tree.parents.get(id);
      }
      if (target) this.reveal(target);
      if (["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) e.preventDefault();
    }
  }
  return {graph, layout, status, duration, Panel, CARD_W, CARD_H};
})();
if (typeof module !== "undefined") module.exports = AgentTree;
