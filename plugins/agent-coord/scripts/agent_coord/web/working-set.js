"use strict";

// Live version of mockup 30. Every thread owns one state, independently of its
// parent's phase, read receipt, or position. Placement is never mutated here.
const WorkingSet = (() => {
  const grouping = typeof module !== "undefined" ? require("./thread-groups.js") : threadGrouping;
  const LEVELS = ["Attention", "Replies to read", "Idle"];
  const PHASES = ["new", "investigation", "planning", "orchestrating", "implementation", "validation", "deployment", "finished"];
  const GROUPINGS = ["group", "domain", "workspace"], ALL = "overview:all", CAP = 12;
  const parked = t => t.attention === "later" || t.attention === "archived" || t.snoozed;
  const running = t => t.attention !== "archived" && grouping.status(t).key === "running";
  function needsReply(t) {
    return !parked(t) && (t.status === "needs input" || t.response_state === "input" ||
      !t.checkpoint_stale && t.checkpoint?.next_actor === "user" && !!t.checkpoint.next_action);
  }
  const handoff = t => t.response_classification?.status === "classified" ? t.response_classification.choice : null;
  const completed = t => handoff(t) ? handoff(t) === "done" : !t.checkpoint_stale && grouping.phase(t) === "finished";
  function columnOf(t) {
    if (running(t)) return "working";
    if (parked(t)) return null;
    const choice = handoff(t), pending = t.unhandled_response ?? t.unread_result;
    if (t.status === "needs input" || t.response_state === "failed") return "blocked";
    if (needsReply(t)) return choice === "review" || t.attention_reason === "review" ? "attention" : "blocked";
    if (pending && (choice === "blocked" || t.attention_reason === "blocked")) return "blocked";
    if (pending && (choice === "review" || t.attention_reason === "review")) return "attention";
    if (completed(t)) return pending ? "working" : null;
    if (t.unread_result !== true) return null;
    // Execution updates stay beside running work; ordinary answers and unknown
    // classifications can be read separately without implying a delivery.
    return choice === "update" ? "working" : "replies";
  }
  function levelOf(t) { const column = columnOf(t); return column === "replies" ? 1 : column ? 0 : 2; }
  const workspaceOf = t => t.project_cwd || t.cwd || "unknown";
  const folder = path => path.split("/").filter(Boolean).pop() || path;
  const touchOf = t => Math.max(t.turn_completion?.completed_at || 0, t.turn_started_at || 0, t.created_at || 0);
  function groupFor(t, threads, dimension, workspace) {
    if (dimension === "domain") {
      const domain = (t.domain || t.scope_cluster || "").trim().replace(/\s+/g, " ").toLowerCase();
      return {key: "domain:" + domain, name: domain || "No domain"};
    }
    if (dimension === "workspace") return {key: "workspace:" + workspaceOf(t), name: workspaceOf(t)};
    let root = t;
    const seen = new Set([root.thread_id]);
    while (root.creation_mode === "orchestration" && threads.has(root.created_by_thread_id) && !seen.has(root.created_by_thread_id)) {
      root = threads.get(root.created_by_thread_id); seen.add(root.thread_id);
    }
    const owner = t.project_id || t.project_name ? t : root;
    if (owner.project_id || owner.project_name) return {
      key: "group:" + (owner.project_id || JSON.stringify([workspaceOf(owner), owner.project_name])), name: owner.project_name || "Unnamed group",
    };
    return {key: "ungrouped:" + workspaceOf(t), name: "Ungrouped" + (workspace === "all" ? " · " + folder(workspaceOf(t)) : "")};
  }
  function derive(threads, {dimension = "group", workspace = "all", order = []} = {}) {
    const byId = new Map(threads.map(t => [t.thread_id, t])), map = new Map();
    const all = {key: ALL, name: "All groups", aggregate: true, levels: LEVELS.map(() => [])};
    for (const t of threads) {
      const descriptor = groupFor(t, byId, dimension, workspace);
      if (!map.has(descriptor.key)) map.set(descriptor.key, {...descriptor, levels: LEVELS.map(() => [])});
      map.get(descriptor.key).levels[levelOf(t)].push(t); all.levels[levelOf(t)].push(t);
    }
    for (const g of [all, ...map.values()]) {
      g.touch = 0;
      for (const list of g.levels) {
        list.sort((a, b) => Number(!!parked(a)) - Number(!!parked(b)) || Number(!!b.pinned) - Number(!!a.pinned) || touchOf(b) - touchOf(a) || a.thread_id.localeCompare(b.thread_id));
        for (const t of list) g.touch = Math.max(g.touch, touchOf(t));
      }
    }
    const previous = new Set(order), newcomers = [...map.values()].filter(g => !previous.has(g.key))
      .sort((a, b) => b.touch - a.touch || a.name.localeCompare(b.name));
    return [all, ...order.filter(key => map.has(key)).map(key => map.get(key)), ...newcomers];
  }
  function create({document, window, storage, renderCard, onOpen, onRead, onDismiss, onGroupChange, isActive = () => true}) {
    const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
    let host, viewport, vtrack, minimap, tracks = [], panes = [], groups = [], source = [], byId = new Map();
    let workspace = "all", context = "", positionKey = "", dimension = "group", level = 0, pageKey = ALL, pageIdx = 0, order = [], signature = "";
    let selectedGroup;
    const positions = new Map(), scroll = new Map(), expanded = new Set();
    let wheelTimer, touch, observer, horizontal = 0, vertical = 0, cooldown = -Infinity, lastNative = -Infinity;
    const active = () => host && !host.hidden && host.isConnected && isActive();
    const button = (text, cls, fn, label) => {
      const b = el("button", cls, text); b.type = "button"; b.onclick = fn;
      if (label) b.setAttribute("aria-label", label); return b;
    };
    function save() {
      const value = {dimension, level, pageKey}; positions.set(positionKey, value);
      try { storage?.setItem(positionKey, JSON.stringify(value)); } catch { /* Navigation also works without storage. */ }
    }
    function restore(key, placement) {
      if (positionKey === key) return;
      positionKey = key;
      let value = positions.get(key);
      try {
        value ||= JSON.parse(storage?.getItem(key) || "null");
        if (!value) {
          const previous = JSON.parse(storage?.getItem(key.replace(":v3:", ":v2:")) || "null");
          if (previous) value = {...previous, level: previous.level === 1 ? 2 : 0};
          else value = JSON.parse(storage?.getItem(key.replace(":v3:", ":v1:")) || "null");
        }
      } catch { /* Optional preference. */ }
      dimension = GROUPINGS.includes(value?.dimension) ? value.dimension : "group";
      level = Number.isInteger(value?.level) && value.level >= 0 && value.level < LEVELS.length ? value.level : ["later", "archived"].includes(placement) ? 2 : 0;
      pageKey = typeof value?.pageKey === "string" ? value.pageKey : ALL;
      order = []; scroll.clear(); expanded.clear();
    }
    function rememberScroll() {
      for (const track of tracks) for (const page of track.children) scroll.set(page.dataset.level + ":" + page.dataset.key, page.scrollTop);
    }
    function section(page, g, items, title, key, count = false, empty = "") {
      if (!items.length && !empty) return;
      const section = el("section", "ws-section"); section.dataset.section = key;
      if (title) {
        const heading = el("header", "ws-section-title"); heading.append(el("h4", "", title));
        if (count) heading.append(el("span", "", String(items.length))); section.append(heading);
      }
      const cards = el("div", "thread-cards"), expansionKey = g.key + ":" + key;
      for (const t of items.slice(0, expanded.has(expansionKey) ? Infinity : CAP)) {
        const wrap = el("div", "ws-card"), card = renderCard(t, needsReply(t));
        wrap.dataset.cardThread = t.thread_id;
        if (t.attention === "later") wrap.classList.add("ws-parked");
        wrap.append(card);
        const links = el("div", "ws-card-links");
        if (t.pinned) links.append(el("span", "ws-pin", "Pinned"));
        if (t.creation_mode === "orchestration" && t.created_by_thread_id && (byId.has(t.created_by_thread_id) || t.created_by?.available)) {
          const parent = byId.get(t.created_by_thread_id) || t.created_by;
          const link = button("Under " + (parent.title || "parent conversation"), "quiet ws-parent", () => onOpen(t.created_by_thread_id));
          link.dataset.thread = t.created_by_thread_id; link.dataset.action = "creator"; links.append(link);
        }
        if (onDismiss && completed(t) && columnOf(t) === "working" && !running(t)) {
          const dismiss = button("Dismiss", "quiet ws-read", () => onDismiss(t), "Dismiss completed response · " + t.title);
          dismiss.dataset.thread = t.thread_id; dismiss.dataset.action = "dismiss"; links.append(dismiss);
        } else if (onRead && t.unread_result && !running(t) && t.attention !== "archived") {
          const read = button("Mark read", "quiet ws-read", () => onRead(t), "Mark response read · " + t.title);
          read.dataset.thread = t.thread_id; read.dataset.action = "read"; links.append(read);
        }
        if (links.children.length) wrap.append(links); cards.append(wrap);
      }
      section.append(cards);
      if (!items.length) section.append(el("p", "ws-empty", empty));
      if (items.length > CAP) section.append(button(expanded.has(expansionKey) ? "Show recent" : "Show all", "quiet ws-more", () => {
        expanded.has(expansionKey) ? expanded.delete(expansionKey) : expanded.add(expansionKey); paint();
      }));
      page.append(section);
    }
    function makePage(g, i, n) {
      const page = el("section", "ws-page"); page.dataset.key = g.key; page.dataset.level = String(n); page.dataset.index = String(i);
      const title = el("h2", "ws-page-title", g.name); title.tabIndex = -1;
      const heading = el("div", "ws-level-title"); heading.append(el("h3", "", LEVELS[n]));
      const items = g.levels[n]; if (items.length) heading.append(el("span", "", String(items.length)));
      page.append(title, heading);
      if (!items.length) {
        page.append(el("p", "ws-empty", ["Nothing needs attention", "No replies to read", "No idle threads"][n] + (g.aggregate ? " across these groups." : " in this group.")));
        const links = el("div", "ws-empty-links");
        g.levels.forEach((list, other) => { if (other !== n && list.length) links.append(button(LEVELS[other], "quiet", () => go(i, other))); }); page.append(links);
      }
      if (n === 0) {
        const board = el("div", "ws-attention-board"); board.setAttribute("aria-label", "Increasing attention from left to right");
        for (const [key, title, empty] of [["working", "Working / Completed", "No running agents or completed work."],
          ["attention", "Needs your attention", "No reviews or decisions requested."], ["blocked", "Blocked", "No agents waiting for your input."]]) {
          section(board, g, items.filter(t => columnOf(t) === key), title, key, true, empty);
        }
        page.append(board);
      } else if (n === 1) {
        section(page, g, items, "", "replies");
      } else {
        const board = el("div", "ws-phase-board"); board.setAttribute("aria-label", "Idle threads by phase");
        for (const phase of PHASES) {
          const list = items.filter(t => grouping.phase(t) === phase);
          if (list.length) section(board, g, list, grouping.phaseLabel(list[0]), "phase-" + phase);
        }
        page.append(board);
      }
      page.addEventListener("click", event => { if (i !== pageIdx) { event.preventDefault(); event.stopPropagation(); go(i, level); } }, true);
      return page;
    }
    function drawMinimap() {
      const previousScroll = minimap.querySelector(".ws-matrix")?.scrollLeft || 0;
      minimap.replaceChildren();
      const body = el("div", "ws-map-body"), labels = el("div", "ws-labels"), matrix = el("div", "ws-matrix");
      LEVELS.forEach((label, n) => {
        const b = button(label, "ws-level", () => go(pageIdx, n)); b.setAttribute("aria-pressed", String(n === level)); labels.append(b);
        const cells = el("div", "ws-cells");
        groups.forEach((g, i) => {
          const count = g.levels[n].length, badge = n === 0 ? g.levels[0].filter(t => columnOf(t) !== "working").length : n === 1 ? count : 0;
          const cell = button(badge ? String(badge) : count ? "·" : "", "ws-cell", () => go(i, n));
          cell.dataset.group = g.key; cell.dataset.level = String(n);
          cell.classList.toggle("ws-here", i === pageIdx); cell.classList.toggle("ws-selected", i === pageIdx && n === level);
          cell.classList.toggle("ws-needs", n === 0 && badge > 0);
          cell.title = g.name + " · " + label + (badge ? " · " + badge + (n === 1 ? " unread" : " needing attention") : !count ? " · Empty" : "");
          cell.setAttribute("aria-label", cell.title); cell.setAttribute("aria-current", String(i === pageIdx && n === level)); cells.append(cell);
        }); matrix.append(cells);
      });
      body.append(labels, matrix); minimap.append(body);
      const foot = el("div", "ws-map-foot"), label = el("label", "", "Group by "), dimensionSelect = el("select");
      dimensionSelect.setAttribute("aria-label", "Focus grouping");
      for (const value of GROUPINGS) { const o = el("option", "", value); o.value = value; o.selected = value === dimension; dimensionSelect.append(o); }
      dimensionSelect.onchange = () => changeGrouping(dimensionSelect.value); label.append(dimensionSelect);
      foot.append(el("span", "", "← → groups · ↑ ↓ state"), label); minimap.append(foot);
      matrix.scrollLeft = previousScroll;
      const left = pageIdx * 32;
      if (left < matrix.scrollLeft) matrix.scrollLeft = left;
      else if (left + 28 > matrix.scrollLeft + matrix.clientWidth) matrix.scrollLeft = left + 28 - matrix.clientWidth;
    }
    function layout() {
      if (!active()) return;
      const width = viewport.clientWidth, peek = width < 900 ? 12 : 52, gap = width < 900 ? 10 : 22, pageWidth = Math.max(180, width - 2 * (peek + gap));
      vtrack.style.transform = "translateY(" + (-level * 100) + "%)";
      tracks.forEach((track, n) => {
        for (const page of track.children) {
          const i = Number(page.dataset.index), current = i === pageIdx && n === level;
          page.style.left = (peek + gap + i * (pageWidth + gap)) + "px"; page.style.width = pageWidth + "px";
          page.classList.toggle("ws-neighbor", i !== pageIdx); page.setAttribute("aria-hidden", String(!current));
          for (const child of page.children) child.inert = !current;
        }
        track.style.transform = "translateX(" + (-pageIdx * (pageWidth + gap)) + "px)";
        panes[n].inert = n !== level; panes[n].setAttribute("aria-hidden", String(n !== level));
      });
    }
    function paint(remember = true) {
      if (remember) rememberScroll();
      const focused = host.contains(document.activeElement) ? document.activeElement : null;
      const focusThread = focused?.dataset.thread, focusAction = focused?.dataset.action;
      const focusMap = !!focused?.closest?.(".ws-minimap");
      tracks.forEach((track, n) => {
        // Only neighboring pages need DOM; the mini-map retains every group.
        const pages = groups.flatMap((g, i) => Math.abs(i - pageIdx) <= 1 ? [makePage(g, i, n)] : []);
        track.replaceChildren(...pages);
        for (const page of pages) page.scrollTop = scroll.get(n + ":" + page.dataset.key) || 0;
      });
      drawMinimap(); layout();
      if (focusThread) {
        const page = tracks[level].children[pageIdx === 0 ? 0 : 1];
        const target = [...(page?.querySelectorAll("[data-thread]") || [])].find(e => e.dataset.thread === focusThread && e.dataset.action === focusAction);
        (target || page?.querySelector(".ws-page-title"))?.focus({preventScroll: true});
      } else if (focusMap) minimap.querySelector('.ws-level[aria-pressed="true"]')?.focus({preventScroll: true});
    }
    function go(i, n) {
      if (!groups.length) return;
      const nextIdx = Math.max(0, Math.min(groups.length - 1, i)), nextLevel = Math.max(0, Math.min(LEVELS.length - 1, n));
      if (pageIdx === nextIdx && level === nextLevel) return;
      const changedGroup = pageIdx !== nextIdx && dimension === "group";
      pageIdx = nextIdx; level = nextLevel; pageKey = groups[pageIdx].key; save(); paint();
      if (changedGroup) {
        const nextGroup = pageKey.startsWith("group:") ? pageKey.slice(6) : null;
        if (selectedGroup !== undefined) selectedGroup = nextGroup;
        onGroupChange?.(nextGroup);
      }
    }
    function changeGrouping(value) {
      if (!GROUPINGS.includes(value) || value === dimension) return;
      rememberScroll(); dimension = value; pageKey = ALL; pageIdx = 0; order = []; scroll.clear();
      groups = derive(source, {dimension, workspace}); order = groups.slice(1).map(g => g.key); save(); paint(false);
      if (selectedGroup != null) onGroupChange?.(null);
    }
    function keydown(event) {
      if (!active() || event.defaultPrevented || event.isComposing || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey ||
        event.target?.closest?.("input, textarea, select, button, a, summary, [contenteditable], [role=menu], [role=tablist]") ||
        document.querySelector("dialog[open], .thread-action-menu, .overview-layout-menu") || document.body.classList.contains("navigation-open")) return;
      const actions = {ArrowUp: () => go(pageIdx, level - 1), ArrowDown: () => go(pageIdx, level + 1), ArrowLeft: () => go(pageIdx - 1, level), ArrowRight: () => go(pageIdx + 1, level), g: () => changeGrouping(GROUPINGS[(GROUPINGS.indexOf(dimension) + 1) % GROUPINGS.length])};
      if (actions[event.key]) { event.preventDefault(); actions[event.key](); }
    }
    function wheel(event) {
      if (!active() || event.ctrlKey || event.metaKey) return;
      const now = window.performance.now(), unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1;
      window.clearTimeout(wheelTimer); wheelTimer = window.setTimeout(() => { horizontal = vertical = 0; }, 220);
      if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        const board = event.target.closest?.(".ws-attention-board");
        if (board && (event.deltaX > 0 ? board.scrollLeft + board.clientWidth < board.scrollWidth - 1 : board.scrollLeft > 0)) {
          horizontal = 0; return;
        }
        event.preventDefault(); if (now < cooldown) return; horizontal += event.deltaX * unit;
        if (Math.abs(horizontal) > 70) { go(pageIdx + Math.sign(horizontal), level); horizontal = 0; cooldown = now + 420; } return;
      }
      const page = event.target.closest?.(".ws-page"), direction = Math.sign(event.deltaY);
      if (page && (direction > 0 ? page.scrollTop + page.clientHeight < page.scrollHeight - 1 : page.scrollTop > 0)) { lastNative = now; vertical = 0; return; }
      event.preventDefault(); if (now < cooldown || now - lastNative < 320) return; vertical += event.deltaY * unit;
      if (Math.abs(vertical) > 140) { go(pageIdx, level + Math.sign(vertical)); vertical = 0; cooldown = now + 700; }
    }
    function build(target) {
      host = target; host.classList.add("working-set"); viewport = el("div", "ws-viewport"); vtrack = el("div", "ws-vtrack");
      LEVELS.forEach((label, n) => {
        const pane = el("section", "ws-pane"), track = el("div", "ws-htrack"); pane.style.top = (n * 100) + "%";
        pane.setAttribute("aria-label", label); pane.append(track); tracks.push(track); panes.push(pane); vtrack.append(pane);
      });
      viewport.append(vtrack); minimap = el("nav", "ws-minimap"); minimap.setAttribute("aria-label", "Groups and states");
      viewport.addEventListener("wheel", wheel, {passive: false});
      viewport.addEventListener("touchstart", event => {
        const t = event.touches[0], page = event.target.closest?.(".ws-page");
        touch = event.touches.length === 1 ? {x: t.clientX, y: t.clientY, page, board: event.target.closest?.(".ws-attention-board"), top: !page || page.scrollTop <= 0,
          bottom: !page || page.scrollTop + page.clientHeight >= page.scrollHeight - 1, scroll: page?.scrollTop || 0} : null;
      }, {passive: true});
      viewport.addEventListener("touchend", event => {
        if (!touch || !active()) return;
        const t = event.changedTouches[0], dx = t.clientX - touch.x, dy = t.clientY - touch.y;
        if (Math.abs(dx) > 60 && Math.abs(dx) > 1.4 * Math.abs(dy)) {
          if (!touch.board || touch.board.scrollWidth <= touch.board.clientWidth) go(pageIdx - Math.sign(dx), level);
        }
        else if (Math.abs(dy) > 80 && Math.abs(dy) > 1.4 * Math.abs(dx) && (dy > 0 ? touch.top : touch.bottom) && (!touch.page || Math.abs(touch.page.scrollTop - touch.scroll) < 2)) go(pageIdx, level - Math.sign(dy));
        touch = null;
      }, {passive: true});
      viewport.addEventListener("touchcancel", () => { touch = null; });
      host.replaceChildren(viewport, minimap); document.addEventListener("keydown", keydown);
      observer = new window.ResizeObserver(layout); observer.observe(host);
    }
    return {
      render(threads, target, options = {}) {
        if (!host) build(target);
        const controlled = options.selectedGroup !== undefined;
        const nextContext = JSON.stringify([options.workspace || "all", controlled ? "groups" : options.view || "all", options.placement || "active"]);
        const nextSignature = JSON.stringify([threads, nextContext, options.selectedGroup, options.groupName, options.revision]);
        if (signature === nextSignature) { layout(); return; }
        const changedContext = context !== nextContext;
        if (changedContext) { rememberScroll(); context = nextContext; restore("agent-coord.working-set:v3:" + context, options.placement); }
        if (controlled && (changedContext || selectedGroup !== options.selectedGroup)) {
          if (options.selectedGroup != null) dimension = "group";
          if (dimension === "group") pageKey = options.selectedGroup == null ? ALL : "group:" + options.selectedGroup;
        }
        selectedGroup = options.selectedGroup;
        workspace = options.workspace || "all"; source = threads; byId = new Map(threads.map(t => [t.thread_id, t]));
        groups = derive(threads, {dimension, workspace, order}); order = groups.slice(1).map(g => g.key);
        if (controlled && selectedGroup != null && !groups.some(g => g.key === pageKey)) {
          groups.push({key: pageKey, name: options.groupName || "Unnamed group", levels: LEVELS.map(() => [])});
        }
        pageIdx = Math.max(0, groups.findIndex(g => g.key === pageKey)); pageKey = groups[pageIdx].key;
        signature = nextSignature; save(); paint(!changedContext);
      },
      destroy() { document.removeEventListener("keydown", keydown); window.clearTimeout(wheelTimer); observer?.disconnect(); host?.replaceChildren(); },
    };
  }
  return {create, derive, levelOf, columnOf, needsReply, groupFor};
})();
if (typeof module !== "undefined") module.exports = WorkingSet;
