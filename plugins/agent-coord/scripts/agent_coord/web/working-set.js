"use strict";

// Focus keeps unhandled work in Attention; Replies to read is also a shortcut
// to unread responses. Placement is never mutated here.
const WorkingSet = (() => {
  const grouping = typeof module !== "undefined" ? require("./thread-groups.js") : threadGrouping;
  const LEVELS = ["Attention", "Replies to read", "Idle"];
  const PHASES = ["new", "investigation", "planning", "orchestrating", "implementation", "validation", "deployment", "finished"];
  const GROUPINGS = ["group", "domain", "workspace"], ALL = "overview:all", CAP = 12;
  const parked = t => t.attention === "archived" || t.snoozed || t.attention === "later" && !grouping.awaitsUser(t);
  const running = t => t.attention !== "archived" && grouping.status(t).key === "running";
  function needsReply(t) {
    return !parked(t) && !grouping.acknowledgedRequest(t) && (t.status === "needs input" || t.response_state === "input" ||
      !t.checkpoint_stale && t.checkpoint?.next_actor === "user" && !!t.checkpoint.next_action);
  }
  const handoff = t => t.response_classification?.status === "classified" ? t.response_classification.choice : null;
  function columnOf(t) {
    if (running(t)) return "working";
    if (grouping.awaitsUser(t) && (t.snooze_due || t.attention_items?.length)) return "attention";
    if (parked(t) || grouping.acknowledgedRequest(t)) return null;
    const choice = handoff(t), pending = t.unhandled_response ?? t.unread_result;
    if (t.status === "needs input" || t.response_state === "failed") return "blocked";
    if (needsReply(t)) return choice === "review" || t.attention_reason === "review" ? "attention" : "blocked";
    if (pending && (choice === "blocked" || t.attention_reason === "blocked")) return "blocked";
    if (pending && (choice === "review" || t.attention_reason === "review")) return "attention";
    if (t.unhandled_response === true) return "working";
    // Older snapshots may have only an unread marker.
    if (t.unhandled_response == null && t.unread_result === true) return choice === "update" ? "working" : "replies";
    if (grouping.awaitsUser(t)) return "attention";
    return null;
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
      const level = levelOf(t);
      map.get(descriptor.key).levels[level].push(t); all.levels[level].push(t);
      if (level === 0 && t.unhandled_response === true && t.unread_result === true && !running(t) && !parked(t)) {
        map.get(descriptor.key).levels[1].push(t); all.levels[1].push(t);
      }
    }
    for (const g of [all, ...map.values()]) {
      g.touch = 0;
      for (const list of g.levels) {
        list.sort((a, b) => (b.attention_since || touchOf(b)) - (a.attention_since || touchOf(a)) || a.thread_id.localeCompare(b.thread_id));
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
    function section(page, g, items, title, key, empty = "") {
      if (!items.length && !empty) return;
      const section = el("section", "ws-section"); section.dataset.section = key;
      if (title) {
        const heading = el("header", "ws-section-title"); heading.append(el("h4", "", title)); section.append(heading);
      }
      const cards = el("div", "thread-cards"), expansionKey = g.key + ":" + key;
      for (const t of items.slice(0, expanded.has(expansionKey) ? Infinity : CAP)) {
        const wrap = el("div", "ws-card"), card = renderCard(t, needsReply(t));
        wrap.dataset.cardThread = t.thread_id;
        if (t.attention === "later") wrap.classList.add("ws-parked");
        wrap.append(card);
        const links = el("div", "ws-card-links");
        if (t.pinned) links.append(el("span", "ws-pin", "Pinned"));
        if (onDismiss && (t.can_handle_response ?? t.unhandled_response) && !running(t)) {
          const dismiss = button("Dismiss", "quiet ws-dismiss", event => { event?.stopPropagation(); onDismiss(t); }, "Dismiss response · " + t.title);
          dismiss.dataset.thread = t.thread_id; dismiss.dataset.action = "dismiss";
          const actions = card.querySelector(".card-actions") || el("div", "card-actions");
          const end = el("div", "card-end-actions"); end.append(dismiss); actions.append(end);
          if (!card.contains(actions)) card.append(actions);
        }
        if (onRead && t.unread_result && !running(t) && t.attention !== "archived") {
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
      const title = el("h2", "ws-page-title sr-only", g.name); title.tabIndex = -1; // Group tabs already show the visible name.
      const heading = el("div", "ws-level-title"); heading.append(el("h3", "", LEVELS[n]));
      const items = g.levels[n];
      page.append(title, heading);
      if (!items.length) {
        page.append(el("p", "ws-empty", ["Nothing needs attention", "No replies to read", "No idle threads"][n] + (g.aggregate ? " across these groups." : " in this group.")));
        const links = el("div", "ws-empty-links");
        g.levels.forEach((list, other) => { if (other !== n && list.length) links.append(button(LEVELS[other], "quiet", () => go(i, other))); }); page.append(links);
      }
      if (n === 0) {
        section(page, g, items, "", "attention");
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
          const items = g.levels[n], count = items.length;
          const unread = items.filter(t => t.unread_result || t.unread).length;
          const blocked = items.filter(t => columnOf(t) === "blocked").length;
          const attention = items.filter(t => columnOf(t) === "attention").length;
          const cell = button("", "ws-cell", () => go(i, n));
          cell.append(el("span", "ws-cell-count", count ? String(count) : ""));
          if (unread) {
            const dot = el("span", "ws-cell-unread"); dot.setAttribute("aria-hidden", "true"); cell.append(dot);
          }
          if (blocked || attention) {
            const marker = el("span", "ws-cell-action", "!"); marker.setAttribute("aria-hidden", "true"); cell.append(marker);
          }
          cell.dataset.group = g.key; cell.dataset.level = String(n);
          cell.classList.toggle("ws-here", i === pageIdx); cell.classList.toggle("ws-selected", i === pageIdx && n === level);
          const details = [count ? count + (count === 1 ? " conversation" : " conversations") : "Empty"];
          if (unread) details.push(unread + " unread");
          if (blocked) details.push(blocked + " blocked");
          if (attention) details.push(attention + " needing attention");
          cell.title = [g.name, label, ...details].join(" · ");
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
    function stepLevel(direction) {
      const next = level + direction;
      go(pageIdx, next === 1 && groups[pageIdx]?.levels[1].length === 0 ? next + direction : next);
    }
    function keydown(event) {
      if (!active() || event.defaultPrevented || event.isComposing || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey ||
        event.target?.closest?.("input, textarea, select, button, a, summary, [contenteditable], [role=menu], [role=tablist]") ||
        document.querySelector("dialog[open], .thread-action-menu, .overview-layout-menu") || document.body.classList.contains("navigation-open")) return;
      const actions = {ArrowUp: () => stepLevel(-1), ArrowDown: () => stepLevel(1), ArrowLeft: () => go(pageIdx - 1, level), ArrowRight: () => go(pageIdx + 1, level), g: () => changeGrouping(GROUPINGS[(GROUPINGS.indexOf(dimension) + 1) % GROUPINGS.length])};
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
        if (Math.abs(dx) > 60 && Math.abs(dx) > 1.4 * Math.abs(dy)) {
          go(pageIdx - Math.sign(dx), level);
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
  // List: one row per thread in Attention, Working and Idle, grouped like Focus.
  // Opening a reply reads it, and reading moves it out of Attention; requests stay
  // until they are resolved. Rows are ordered by the user's own last message.
  const DAY = 86400, IDLE_CAP = 50;
  const SECTIONS = [["attention", "Attention"], ["working", "Working"], ["idle", "Idle"]];
  const REQUEST_LABELS = {approval: "Approval needed", question: "Question", reminder: "Reminder", failed: "Failed", "needs-you": "Needs you"};
  const PROVIDERS = {codex: "OpenAI · Codex", claude: "Anthropic · Claude Code", muse: "Meta · Muse Code", kimi: "Moonshot · Kimi Code", antigravity: "Google · Antigravity"};
  // Until the payload carries the user's last message, the latest turn start stands in for it.
  const engagedAt = t => t.last_user_message_at || t.turn_started_at || t.created_at || 0;
  const arrivedAt = t => t.attention_since || t.turn_completion?.completed_at || t.checkpoint?.created_at || t.updated_at || t.created_at || 0;
  const creatorOf = (t, byId) => t.creation_mode === "orchestration" && byId.get(t.created_by_thread_id) || null;
  function rootOf(t, byId) {
    const seen = new Set([t.thread_id]);
    for (let creator = creatorOf(t, byId); creator && !seen.has(creator.thread_id); creator = creatorOf(t, byId)) { seen.add(creator.thread_id); t = creator; }
    return t;
  }
  function requestOf(t) {
    if (t.status === "needs input") return "approval";
    if (t.open_ask) return "question";
    if (t.snooze_due) return "reminder";
    if (t.response_state === "failed" && !running(t)) return "failed";
    const checkpoint = t.checkpoint;
    if (!t.checkpoint_stale && checkpoint?.next_actor === "user" && checkpoint.next_action && (checkpoint.id || 0) > (t.handled_checkpoint_id || 0)) return "needs-you";
    return null;
  }
  const interrupts = request => ["approval", "question", "reminder", "failed"].includes(request);
  function unreadNow(t, request, now) {
    // Unread replies and agent requests leave Attention once read, or after a day.
    const unread = request === "needs-you" ? t.unread || t.unread_result : !request && t.unread_result;
    return !!unread && now - arrivedAt(t) < DAY;
  }
  function placementOf(t, byId, now, workingRoots = new Set()) {
    if (t.attention === "archived") return null;
    const request = requestOf(t);
    if (request === "approval" || request === "question") return "attention";
    // An orchestrator owns its agents' replies and failures; only their requests reach you.
    if (creatorOf(t, byId)) return request === "needs-you" && unreadNow(t, request, now) ? "attention" : "nested";
    if (request === "reminder") return "attention";
    if (t.attention === "later" || t.snoozed) return "later";
    if (request === "failed") return "attention";
    if (running(t)) return "working";
    if (unreadNow(t, request, now)) return "attention";
    return workingRoots.has(t.thread_id) ? "working" : "idle";
  }
  function listSections(threads, {dimension = "group", workspace = "all", pageKey = ALL, view = "active", now = Date.now() / 1000} = {}) {
    const byId = new Map(threads.map(t => [t.thread_id, t])), agents = new Map(), workingRoots = new Set();
    for (const t of threads) {
      if (!creatorOf(t, byId)) continue;
      const root = rootOf(t, byId);
      if (!agents.has(root.thread_id)) agents.set(root.thread_id, []);
      agents.get(root.thread_id).push(t);
      if (running(t)) workingRoots.add(root.thread_id);
    }
    const parked = ["later", "archived"].includes(view);
    const rows = [];
    for (const t of threads) {
      const place = parked ? (creatorOf(t, byId) ? "nested" : "idle") : placementOf(t, byId, now, workingRoots);
      if (place && place !== "nested" && place !== "later") rows.push({thread: t, place, group: groupFor(t, byId, dimension, workspace)});
    }
    const groups = new Map();
    for (const {thread, place, group} of rows) {
      if (!groups.has(group.key)) groups.set(group.key, {...group, attention: 0, touch: 0});
      const entry = groups.get(group.key);
      entry.attention += place === "attention"; entry.touch = Math.max(entry.touch, engagedAt(thread));
    }
    const ordered = [...groups.values()].sort((a, b) => b.touch - a.touch || a.name.localeCompare(b.name));
    const all = {key: ALL, name: "All groups", attention: rows.filter(r => r.place === "attention").length};
    const selected = pageKey === ALL || groups.has(pageKey) ? pageKey : ALL;
    const sections = {attention: [], working: [], idle: [], empty: []};
    for (const {thread, place, group} of rows) {
      if (selected !== ALL && group.key !== selected) continue;
      const empty = place === "idle" && !thread.checkpoint && !thread.turn_completion && !agents.has(thread.thread_id);
      sections[empty ? "empty" : place].push(thread);
    }
    const urgency = t => Number(interrupts(requestOf(t)));
    sections.attention.sort((a, b) => urgency(b) - urgency(a) || arrivedAt(b) - arrivedAt(a) || a.thread_id.localeCompare(b.thread_id));
    for (const key of ["working", "idle", "empty"])
      sections[key].sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || engagedAt(b) - engagedAt(a) || a.thread_id.localeCompare(b.thread_id));
    // Agents waiting on you lead their orchestrator's list, then the ones still working.
    const agentOrder = t => ["approval", "question", "needs-you"].includes(requestOf(t)) ? 2 : running(t) ? 1 : 0;
    for (const list of agents.values()) list.sort((a, b) => agentOrder(b) - agentOrder(a) || arrivedAt(b) - arrivedAt(a) || a.thread_id.localeCompare(b.thread_id));
    return {groups: [all, ...ordered], selected, sections, agents, byId, workingRoots};
  }
  function ago(at, now) {
    const s = Math.max(0, now - at);
    return !at ? "" : s < 3600 ? Math.max(1, Math.round(s / 60)) + "m" : s < DAY ? Math.round(s / 3600) + "h" : Math.round(s / DAY) + "d";
  }
  function createList({document, storage, onOpen, onRead, onClose, onMenu, onGroupChange, now = () => Date.now() / 1000}) {
    const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
    const button = (text, cls, fn, label) => {
      const b = el("button", cls, text); b.type = "button";
      b.onclick = event => { event?.stopPropagation?.(); fn(event); };
      if (label) { b.setAttribute("aria-label", label); b.title = label; }
      return b;
    };
    let host, source = [], options = {}, context = "", key = "", dimension = "group", pageKey = ALL, selectedGroup, signature = "";
    const expanded = new Set(), shown = new Set();
    function restore(nextKey) {
      key = nextKey; expanded.clear(); shown.clear();
      let saved = null;
      try { saved = JSON.parse(storage?.getItem(key) || "null"); } catch { /* Optional preference. */ }
      dimension = GROUPINGS.includes(saved?.dimension) ? saved.dimension : "group";
      pageKey = typeof saved?.pageKey === "string" ? saved.pageKey : ALL;
    }
    function save() {
      try { storage?.setItem(key, JSON.stringify({dimension, pageKey})); } catch { /* Selection also works without storage. */ }
    }
    function choose(next) {
      if (next === pageKey) return;
      pageKey = next; save(); paint();
      if (dimension === "group") onGroupChange?.(next.startsWith("group:") ? next.slice(6) : null);
    }
    function row(t, data, {agent = false} = {}) {
      const time = now(), request = requestOf(t), place = agent ? "nested" : placementOf(t, data.byId, time, data.workingRoots);
      const unread = !!(t.unread_result || request === "needs-you" && t.unread);
      const li = el("li", "tl-row");
      li.dataset.thread = t.thread_id;
      li.classList.toggle("tl-unread", unread);
      li.classList.toggle("tl-request", !!request && (interrupts(request) || unread));
      li.classList.toggle("tl-reply", unread && !request);
      li.classList.toggle("tl-working", running(t) || !agent && place === "working");
      li.classList.toggle("tl-agent", agent);
      li.classList.toggle("tl-failed", request === "failed");
      li.append(el("span", "tl-mark"));
      const logo = globalThis.providerLogo?.(t.client, PROVIDERS[t.client]) || el("span", "provider-logo");
      const mark = el("span", "tl-logo"); mark.append(logo); li.append(mark);
      const root = creatorOf(t, data.byId) && !agent ? rootOf(t, data.byId) : null;
      const open = button("", "tl-open", () => onOpen?.(t.thread_id));
      open.dataset.thread = t.thread_id; open.dataset.action = "open";
      if (root) open.append(el("span", "tl-crumb", root.title + " › "));
      open.append(document.createTextNode(t.title));
      open.title = (root ? root.title + " › " : "") + t.title;
      li.append(open);
      const summary = el("span", "tl-summary");
      const children = agent ? [] : data.agents.get(t.thread_id) || [];
      if (children.length) {
        const working = children.filter(running).length, waiting = children.filter(c => ["approval", "question", "needs-you"].includes(requestOf(c))).length;
        const failed = children.filter(c => requestOf(c) === "failed").length, isOpen = expanded.has(t.thread_id);
        const toggle = button("", "tl-agents", () => { isOpen ? expanded.delete(t.thread_id) : expanded.add(t.thread_id); paint(); });
        toggle.dataset.thread = t.thread_id; toggle.dataset.action = "agents";
        toggle.setAttribute("aria-expanded", String(isOpen));
        toggle.append(children.length + (children.length === 1 ? " agent" : " agents"));
        if (working) toggle.append(" · " + working + " working");
        if (waiting) toggle.append(" · ", el("span", "tl-warm", waiting + " need you"));
        if (failed) toggle.append(" · ", el("span", "tl-warm", failed + " failed"));
        toggle.setAttribute("aria-label", "Agents created by " + t.title + ": " + toggle.textContent);
        summary.append(toggle);
      }
      if (request) summary.append(el("span", "tl-inline-label", REQUEST_LABELS[request]));
      const asked = ["approval", "question", "needs-you"].includes(request) ? t.open_ask?.question || t.checkpoint?.next_action : "";
      const text = asked || t.checkpoint?.summary || t.original_request || "";
      // An automatic title is often the opening request; repeating it adds nothing.
      const plain = value => value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
      if (text && !plain(text).startsWith(plain(t.title).slice(0, 40))) summary.append(el("span", "tl-text", text));
      li.append(summary);
      li.append(el("span", "tl-label", request ? REQUEST_LABELS[request] : ""));
      li.append(el("span", "tl-phase", grouping.phaseLabel(t)));
      const at = place === "attention" ? arrivedAt(t) : engagedAt(t), stamp = el("time", "tl-time", ago(at, time));
      if (at) stamp.title = (place === "attention" ? "Arrived " : "Last active ") + new Date(at * 1000).toLocaleString();
      li.append(stamp);
      const actions = el("span", "tl-actions");
      if (unread && onRead) {
        const read = button("Mark read", "tl-action", () => onRead(t), "Mark read · " + t.title);
        read.dataset.thread = t.thread_id; read.dataset.action = "read"; actions.append(read);
      }
      if (onMenu) {
        const more = button("…", "tl-action tl-icon", event => onMenu(t, more, event), "Thread options · " + t.title);
        more.dataset.thread = t.thread_id; more.dataset.action = "more"; more.setAttribute("aria-haspopup", "menu"); actions.append(more);
      }
      if (onClose && t.attention !== "archived") {
        const close = button("×", "tl-action tl-icon", () => onClose(t), (["running", "needs input"].includes(t.status) ? "Stop and close " : "Close ") + t.title);
        close.dataset.thread = t.thread_id; close.dataset.action = "close"; actions.append(close);
      }
      li.append(actions);
      li.onclick = event => { if (!event.target?.closest?.("button:not(.tl-open)")) onOpen?.(t.thread_id); };
      return li;
    }
    function section(data, name, label, items, empty) {
      const node = el("section", "tl-section"); node.dataset.section = name; node.setAttribute("aria-label", label);
      const heading = el("h3", "tl-heading", label); heading.append(el("span", "tl-count", String(items.length))); node.append(heading);
      if (!items.length) { node.append(el("p", "tl-empty", empty)); return node; }
      const list = el("ul", "tl-rows"), all = name !== "idle" || shown.has(name);
      for (const t of all ? items : items.slice(0, IDLE_CAP)) {
        list.append(row(t, data));
        if (expanded.has(t.thread_id)) for (const agent of data.agents.get(t.thread_id) || []) list.append(row(agent, data, {agent: true}));
      }
      node.append(list);
      if (items.length > IDLE_CAP && name === "idle")
        node.append(button(all ? "Show recent" : "Show all " + items.length, "quiet tl-more", () => { all ? shown.delete(name) : shown.add(name); paint(); }));
      return node;
    }
    function paint() {
      if (!host) return;
      const focused = host.contains(document.activeElement) ? document.activeElement : null;
      const focusThread = focused?.dataset?.thread, focusAction = focused?.dataset?.action;
      const parked = ["later", "archived"].includes(options.placement);
      const data = listSections(source, {dimension, workspace: options.workspace || "all", pageKey, view: options.placement, now: now()});
      if (data.selected !== pageKey) pageKey = data.selected;
      const bar = el("div", "tl-bar"), tabs = el("div", "tl-groups"); tabs.setAttribute("aria-label", "Groups");
      for (const g of data.groups) {
        const tab = button(g.name, "tl-group", () => choose(g.key));
        tab.setAttribute("aria-pressed", String(g.key === data.selected));
        tab.dataset.group = g.key;
        if (g.attention) tab.append(el("span", "tl-count", String(g.attention)));
        tabs.append(tab);
      }
      const label = el("label", "tl-grouping", "Group by "), select = el("select");
      for (const value of GROUPINGS) { const o = el("option", "", value); o.value = value; o.selected = value === dimension; select.append(o); }
      select.onchange = () => {
        dimension = select.value; pageKey = ALL; save(); paint();
        if (selectedGroup != null) onGroupChange?.(null);
      };
      label.append(select); bar.append(tabs, label);
      const parts = [bar];
      if (parked) {
        const title = options.placement === "later" ? "Later" : "Closed";
        parts.push(section(data, "parked", title, [...data.sections.idle, ...data.sections.empty], options.placement === "later" ? "Nothing set aside." : "No closed threads."));
      } else {
        const empty = {attention: "Nothing needs you right now.", working: "No agents are working.", idle: "No idle threads."};
        for (const [name, title] of SECTIONS) parts.push(section(data, name, title, data.sections[name], empty[name]));
        const blank = data.sections.empty;
        if (blank.length) {
          if (shown.has("empty")) parts.push(section(data, "empty", "Empty", blank, ""));
          parts.push(button(shown.has("empty") ? "Hide empty threads" : blank.length + (blank.length === 1 ? " empty thread" : " empty threads"), "quiet tl-more",
            () => { shown.has("empty") ? shown.delete("empty") : shown.add("empty"); paint(); }));
        }
      }
      // WebKit resets a scrolled ancestor to the top when this content is replaced
      // by new nodes, so background updates would undo the reader's scrolling.
      const scrolled = [];
      for (let el = host; el; el = el.parentElement) if (el.scrollTop || el.scrollLeft) scrolled.push([el, el.scrollTop, el.scrollLeft]);
      host.replaceChildren(...parts);
      for (const [el, top, left] of scrolled) { el.scrollTop = top; el.scrollLeft = left; }
      if (focusThread) [...host.querySelectorAll("[data-thread]")].find(n => n.dataset.thread === focusThread && n.dataset.action === focusAction)?.focus({preventScroll: true});
    }
    return {
      render(threads, target, next = {}) {
        if (host !== target) { host = target; host.classList.add("thread-list"); }
        const controlled = next.selectedGroup !== undefined;
        const nextContext = JSON.stringify([next.workspace || "all", controlled ? "groups" : next.view || "all", next.placement || "active"]);
        const nextSignature = JSON.stringify([nextContext, next.selectedGroup, next.revision]);
        // The app's list revision changes whenever thread data changes, and once a minute for relative times.
        if (signature === nextSignature && next.revision !== undefined) return;
        if (context !== nextContext) { context = nextContext; restore("agent-coord.thread-list:v1:" + context); }
        if (controlled && selectedGroup !== next.selectedGroup) {
          if (next.selectedGroup != null) dimension = "group";
          if (dimension === "group") pageKey = next.selectedGroup == null ? ALL : "group:" + next.selectedGroup;
        }
        selectedGroup = next.selectedGroup; source = threads; options = next; signature = nextSignature;
        paint();
      },
    };
  }
  return {create, derive, levelOf, columnOf, needsReply, groupFor, createList, listSections, placementOf, requestOf};
})();
if (typeof module !== "undefined") module.exports = WorkingSet;
