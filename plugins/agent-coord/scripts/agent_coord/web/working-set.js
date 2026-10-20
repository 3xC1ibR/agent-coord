"use strict";

// Live version of mockup 30. Every thread owns one state, independently of its
// parent's phase, read receipt, or position. Placement is never mutated here.
const WorkingSet = (() => {
  const grouping = typeof module !== "undefined" ? require("./thread-groups.js") : threadGrouping;
  const LEVELS = ["Running + Needs my reply", "Replies to read", "Idle"];
  const PHASES = ["new", "investigation", "planning", "orchestrating", "implementation", "validation", "deployment", "finished"];
  const GROUPINGS = ["group", "domain", "workspace"], ALL = "overview:all", CAP = 12;
  const parked = t => t.attention === "later" || t.attention === "archived" || t.snoozed;
  const running = t => t.attention !== "archived" && grouping.status(t).key === "running";
  function needsReply(t) {
    return !parked(t) && (t.status === "needs input" || t.response_state === "input" ||
      !t.checkpoint_stale && t.checkpoint?.next_actor === "user" && !!t.checkpoint.next_action);
  }
  function levelOf(t) { return running(t) || needsReply(t) ? 0 : !parked(t) && t.unread_result === true ? 1 : 2; }
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
    const all = {key: ALL, name: "All groups", aggregate: true, levels: [[], [], []]};
    for (const t of threads) {
      const descriptor = groupFor(t, byId, dimension, workspace);
      if (!map.has(descriptor.key)) map.set(descriptor.key, {...descriptor, levels: [[], [], []]});
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
  function create({document, window, storage, renderCard, onOpen, onRead, isActive = () => true}) {
    const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
    let host, viewport, vtrack, minimap, tracks = [], panes = [], groups = [], source = [], byId = new Map();
    let workspace = "all", context = "", positionKey = "", dimension = "group", level = 1, pageKey = ALL, pageIdx = 0, order = [], signature = "";
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
      try { value ||= JSON.parse(storage?.getItem(key) || "null"); } catch { /* Optional preference. */ }
      dimension = GROUPINGS.includes(value?.dimension) ? value.dimension : "group";
      level = Number.isInteger(value?.level) && value.level >= 0 && value.level < 3 ? value.level : ["later", "archived"].includes(placement) ? 2 : 1;
      pageKey = typeof value?.pageKey === "string" ? value.pageKey : ALL;
      order = []; scroll.clear(); expanded.clear();
    }
    function rememberScroll() {
      for (const track of tracks) for (const page of track.children) scroll.set(page.dataset.level + ":" + page.dataset.key, page.scrollTop);
    }
    function section(page, g, items, title, key, count = false) {
      if (!items.length) return;
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
        if (g.aggregate) {
          const descriptor = groupFor(t, byId, dimension, workspace);
          const link = button(descriptor.name, "quiet ws-group-link", () => go(groups.findIndex(g => g.key === descriptor.key), level));
          link.dataset.action = "group"; links.append(link);
        }
        if (onRead && t.unread_result && t.attention !== "archived") {
          const read = button("Mark read", "quiet ws-read", () => onRead(t), "Mark response read · " + t.title);
          read.dataset.thread = t.thread_id; read.dataset.action = "read"; links.append(read);
        }
        if (links.children.length) wrap.append(links); cards.append(wrap);
      }
      section.append(cards);
      if (items.length > CAP) section.append(button(expanded.has(expansionKey) ? "Show recent" : "Show all", "quiet ws-more", () => {
        expanded.has(expansionKey) ? expanded.delete(expansionKey) : expanded.add(expansionKey); paint();
      }));
      page.append(section);
    }
    function makePage(g, i, n) {
      const page = el("section", "ws-page"); page.dataset.key = g.key; page.dataset.level = String(n); page.dataset.index = String(i);
      const title = el("h2", "ws-page-title", g.name); title.tabIndex = -1;
      const heading = el("div", "ws-level-title"); heading.append(el("h3", "", LEVELS[n]));
      const items = g.levels[n]; if (n === 1 && items.length) heading.append(el("span", "", String(items.length)));
      page.append(title, heading);
      if (!items.length) {
        page.append(el("p", "ws-empty", ["Nothing running or waiting for your reply", "No unread replies", "No idle threads"][n] + (g.aggregate ? " across these groups." : " in this group.")));
        const links = el("div", "ws-empty-links");
        g.levels.forEach((list, other) => { if (other !== n && list.length) links.append(button(LEVELS[other], "quiet", () => go(i, other))); }); page.append(links);
      }
      if (n === 0) {
        section(page, g, items.filter(needsReply), "Needs my reply", "asks", true);
        section(page, g, items.filter(t => !needsReply(t)), "Running", "running");
      } else if (n === 1) section(page, g, items, "", "replies");
      else {
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
      const nav = el("div", "ws-group-nav"), select = el("select"); select.setAttribute("aria-label", "Choose group");
      const prev = button("←", "quiet", () => go(pageIdx - 1, level), "Previous group"); prev.disabled = pageIdx === 0;
      const next = button("→", "quiet", () => go(pageIdx + 1, level), "Next group"); next.disabled = pageIdx === groups.length - 1;
      groups.forEach((g, i) => { const option = el("option", "", g.name); option.value = String(i); option.selected = i === pageIdx; select.append(option); });
      select.onchange = () => go(Number(select.value), level); nav.append(prev, select, next); minimap.append(nav);
      const body = el("div", "ws-map-body"), labels = el("div", "ws-labels"), matrix = el("div", "ws-matrix");
      LEVELS.forEach((label, n) => {
        const b = button(label, "ws-level", () => go(pageIdx, n)); b.setAttribute("aria-pressed", String(n === level)); labels.append(b);
        const cells = el("div", "ws-cells");
        groups.forEach((g, i) => {
          const count = g.levels[n].length, badge = n === 0 ? g.levels[0].filter(needsReply).length : n === 1 ? count : 0;
          const cell = button(badge ? String(badge) : count ? "·" : "", "ws-cell", () => go(i, n));
          cell.dataset.group = g.key; cell.dataset.level = String(n);
          cell.classList.toggle("ws-here", i === pageIdx); cell.classList.toggle("ws-selected", i === pageIdx && n === level);
          cell.classList.toggle("ws-needs", n === 0 && badge > 0);
          cell.title = g.name + " · " + label + (badge ? " · " + badge + (n === 0 ? " awaiting your reply" : " unread") : !count ? " · Empty" : "");
          cell.setAttribute("aria-label", cell.title); cell.setAttribute("aria-current", String(i === pageIdx && n === level)); cells.append(cell);
        }); matrix.append(cells);
      });
      body.append(labels, matrix); minimap.append(body);
      const foot = el("div", "ws-map-foot"), label = el("label", "", "Group by "), dimensionSelect = el("select");
      dimensionSelect.setAttribute("aria-label", "Working Set grouping");
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
      const nextIdx = Math.max(0, Math.min(groups.length - 1, i)), nextLevel = Math.max(0, Math.min(2, n));
      if (pageIdx === nextIdx && level === nextLevel) return;
      pageIdx = nextIdx; level = nextLevel; pageKey = groups[pageIdx].key; save(); paint();
    }
    function changeGrouping(value) {
      if (!GROUPINGS.includes(value) || value === dimension) return;
      rememberScroll(); dimension = value; pageKey = ALL; pageIdx = 0; order = []; scroll.clear();
      groups = derive(source, {dimension, workspace}); order = groups.slice(1).map(g => g.key); save(); paint(false);
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
        touch = event.touches.length === 1 ? {x: t.clientX, y: t.clientY, page, top: !page || page.scrollTop <= 0,
          bottom: !page || page.scrollTop + page.clientHeight >= page.scrollHeight - 1, scroll: page?.scrollTop || 0} : null;
      }, {passive: true});
      viewport.addEventListener("touchend", event => {
        if (!touch || !active()) return;
        const t = event.changedTouches[0], dx = t.clientX - touch.x, dy = t.clientY - touch.y;
        if (Math.abs(dx) > 60 && Math.abs(dx) > 1.4 * Math.abs(dy)) go(pageIdx - Math.sign(dx), level);
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
        const nextContext = JSON.stringify([options.workspace || "all", options.view || "all", options.placement || "active"]);
        const nextSignature = JSON.stringify([threads, nextContext, options.revision]);
        if (signature === nextSignature) { layout(); return; }
        const changedContext = context !== nextContext;
        if (changedContext) { rememberScroll(); context = nextContext; restore("agent-coord.working-set:v1:" + context, options.placement); }
        workspace = options.workspace || "all"; source = threads; byId = new Map(threads.map(t => [t.thread_id, t]));
        groups = derive(threads, {dimension, workspace, order}); order = groups.slice(1).map(g => g.key);
        pageIdx = Math.max(0, groups.findIndex(g => g.key === pageKey)); pageKey = groups[pageIdx].key;
        signature = nextSignature; save(); paint(!changedContext);
      },
      destroy() { document.removeEventListener("keydown", keydown); window.clearTimeout(wheelTimer); observer?.disconnect(); host?.replaceChildren(); },
    };
  }
  return {create, derive, levelOf, needsReply, groupFor};
})();
if (typeof module !== "undefined") module.exports = WorkingSet;
