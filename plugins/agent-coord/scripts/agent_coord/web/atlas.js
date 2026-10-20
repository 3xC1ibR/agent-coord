"use strict";

// The Atlas renderer uses the same filtered inventory as Cards. Geometry and
// gestures are adapted from the approved Atlas design, without mockup fixtures.
const LiveAtlas = (() => {
const geometry = typeof module !== "undefined" ? require("./atlas-layout.js") : AtlasLayout;
const grouping = typeof module !== "undefined" ? require("./thread-groups.js") : threadGrouping;
function fieldFromThreads(threads, {workspace = "all", now = Date.now()} = {}) {
  const ms = value => Number.isFinite(value) ? value * 1000 : 0;
  const ids = new Set(threads.map(t => t.thread_id));
  const agents = threads.map(t => {
    const cp = t.checkpoint || {}, status = grouping.status(t), needs = grouping.awaitsUser(t);
    const phase = grouping.phase(t), next = !t.checkpoint_stale ? {actor: cp.next_actor || "nobody", action: cp.next_action || ""} : {actor: "nobody", action: ""};
    const createdAt = ms(t.created_at), updatedAt = ms(t.updated_at) || createdAt;
    return {id: t.thread_id, title: t.title || "Untitled conversation", client: t.client || "", source: t,
      parentId: t.creation_mode === "orchestration" && ids.has(t.created_by_thread_id) ? t.created_by_thread_id : null,
      phase: phase === "new" ? "starting" : phase,
      status: status.key === "running" ? "working" : status.key === "failed" ? "failed" : needs ? "waiting" : phase === "finished" ? "done" : "idle",
      summary: cp.summary || "", activity: t.last_tool?.label || t.session?.last_tool?.label || null,
      next, ask: needs ? {kind: grouping.reason(t).key, question: next.actor === "user" && next.action || cp.summary || status.label, askedAt: ms(t.attention_since) || updatedAt} : null,
      domain: t.domain || t.scope_cluster || null, domains: [t.domain || t.scope_cluster].filter(Boolean),
      domainSource: t.domain ? t.domain_source || "agent" : t.scope_cluster ? "derived" : "missing",
      group: t.project_name || null, groupId: t.project_id || null, workspace: t.project_cwd || t.cwd || "", attention: t.attention,
      createdAt, updatedAt, history: cp.created_at ? [{phase: cp.phase, at: ms(cp.created_at)}] : [],
      userTurns: 0, pinned: !!t.pinned,
    };
  });
  const byId = new Map(agents.map(a => [a.id, a]));
  // Inventory provenance can outlive parents or contain old cycles. Never let
  // those records trap navigation or place one thread more than once.
  for (const a of agents) {
    const seen = new Set([a.id]); let p = byId.get(a.parentId);
    while (p && !seen.has(p.id)) { seen.add(p.id); p = byId.get(p.parentId); }
    if (p) a.parentId = null;
  }
  const children = id => agents.filter(a => a.parentId === id);
  const descendants = id => { const out = []; for (const a of children(id)) out.push(a, ...descendants(a.id)); return out; };
  const needsUser = a => !!a.ask || a.next?.actor === "user" && !!a.next.action;
  const relativeTime = timestamp => { const minutes = Math.max(0, (now - timestamp) / 60000); return minutes < 1 ? "just now" : minutes < 60 ? Math.round(minutes) + "m ago" : minutes < 1440 ? Math.round(minutes / 60) + "h ago" : Math.round(minutes / 1440) + "d ago"; };
  return {agents, now, workspace, scale: "real", byId: id => byId.get(id) || null, parent: id => byId.get(byId.get(id)?.parentId) || null,
    children, descendants, roots: () => agents.filter(a => !a.parentId), domainsOf: a => a.domains, needsUser,
    asks: () => agents.filter(needsUser).sort((a, b) => grouping.attentionOrder(a.source, b.source)),
    lastActivity: a => a.updatedAt, relativeTime, duration: ms => relativeTime(now - ms).replace(" ago", ""),
    statusLabel: a => grouping.status(a.source).label, impact: () => []};
}
function drag(element, {onStart, onMove, onEnd, threshold = 4} = {}) {
  let start, last, moved;
  element.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    start = last = {x: event.clientX, y: event.clientY}; moved = false;
    element.setPointerCapture?.(event.pointerId);
  });
  element.addEventListener("pointermove", event => {
    if (!start || !event.buttons) return;
    const dx = event.clientX - start.x, dy = event.clientY - start.y;
    if (!moved && Math.hypot(dx, dy) < threshold) return;
    if (!moved) { moved = true; onStart?.(event); }
    onMove?.({dx, dy, mx: event.clientX - last.x, my: event.clientY - last.y, event});
    last = {x: event.clientX, y: event.clientY};
  });
  const finish = event => { if (!start) return; const dragged = moved; start = null; onEnd?.({dragged, event}); };
  element.addEventListener("pointerup", finish); element.addEventListener("pointercancel", event => { start = null; });
}
function create({document, window, storage, onOpen, onInspect, providerLogo}) {
  const AtlasLayout = geometry, Mockup = {drag};
  const localStorage = storage;
  const requestAnimationFrame = callback => window.requestAnimationFrame(callback);
  const cancelAnimationFrame = id => window.cancelAnimationFrame(id);
  const matchMedia = query => window.matchMedia(query), performance = window.performance;
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const AgentField = {el, DOMAINS: [], DOMAIN_LABELS: {}, PROVIDER_NAMES: {}, providerLogo: client => providerLogo?.(client) || el("span", "atlas-provider", client)};
  function savedGrouping() { try { return localStorage?.getItem("agent-coord.atlas-grouping") === "group" ? "group" : "domain"; } catch { return "domain"; } }
  const $ = AgentField.el, NS = "http://www.w3.org/2000/svg";
  const svgEl = (tag, attrs = {}, parent) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v); if (parent) parent.append(e); return e; };
  const MIN = 60000;
  const scenario = "real";
  const layoutKey = workspace => "agent-coord.atlas:v1:" + JSON.stringify([workspace || "all", state.grouping]);
  let storeKey;
  const cameras = new Map();
  let stageRoot;
  const clusterPos = new Map();
  let radial = AtlasLayout.create(), lastDraw = new Map();
  const persist = () => { try { localStorage.setItem(storeKey, JSON.stringify({clusters: Object.fromEntries(clusterPos), radial: radial.snapshot()})); } catch { /* private mode */ } };
  const CANON = {frontend: "web", ui: "web", webui: "web", web: "web", api: "backend", server: "backend", services: "backend", ios: "mobile", phone: "mobile", mobileapp: "mobile", documentation: "docs", writing: "docs", ci: "infra", devops: "infra", tooling: "infra", shipping: "release", deploy: "release", analysis: "research", investigation: "research"};
  const norm = label => label.toLowerCase().replace(/[^a-z0-9/]+/g, "");
  const clusterKey = label => { if (!label) return "∅"; const n = norm(label); return CANON[n] || n; };
  const LABEL_W = 175; // label boxes in screen pixels at the cluster's own zoom level
  const state = {grouping: savedGrouping(), phasePreview: null, historyExpanded: false, level: 0, focus: null, hub: null, view: null, anim: null, hovered: null, selected: null, highlight: null, panel: "right", labels: "auto", targetScale: null, detailScale: null, fitted: true, fitKey: null};
  let canvas, svg, panel, toolbar, controls, legend, empty, clusters = new Map(), nodeEls = new Map(), api, currentField = null, drawnScale = 1;
  const W = () => canvas.clientWidth || 1000, H = () => canvas.clientHeight || 700;
  const viewScale = () => W() / (state.view?.w || 1600);
  const layoutScale = () => state.targetScale || viewScale();
  const px = () => 1 / layoutScale();
  // Use completion time, not heartbeat/tool recency, and fade the whole mark.
  const finishedAt = a => [...(a.history || [])].reverse().find(h => h.phase === "finished")?.at || a.updatedAt;
  const olderFinished = (field, a) => a._band === "finished" && field.now - finishedAt(a) >= AtlasLayout.finishedDensityAge && !a._pinned;
  // Keep an ancestor on the map while any descendant is still visible. Focusing
  // a family reveals its finished members too, so edges never end at empty space.
  const foldedFinished = (field, a) => olderFinished(field, a) && a.id !== state.selected && a.id !== state.highlight
    && !(state.hub && rootOf(a.id)?.id === rootOf(state.hub)?.id)
    && !(state.historyExpanded && clusterOf(a.id)?.key === state.focus)
    && !field.descendants(a.id).some(child => !olderFinished(field, child) || child.id === state.selected || child.id === state.highlight);
  const opacityFor = (field, a) => a._band === "finished"
    ? Math.max(field.descendants(a.id).some(child => !olderFinished(field, child)) ? .65 : 0, AtlasLayout.completedOpacity(field.now - finishedAt(a))) : 1;
  const fillFor = (field, a) => {
    if (field.needsUser(a)) return "var(--warm)";
    if (a._band === "finished") return "var(--muted)";
    if (a.status === "working") return "var(--accent)";
    return a.status === "failed" ? "var(--danger)" : "var(--muted)";
  };
  const sizeFor = (a, far) => far ? Math.min(8, 2.6 + 1.3 * Math.sqrt(a.userTurns || 0)) : Math.min(10, 4 + 1.8 * Math.sqrt(a.userTurns || 0));

  // Symbols belong to a parent, independently of status, domain, position or
  // ordering. Keep assignments across reloads and workspace switches.
  let familySymbols = new Map();
  const symbolPaths = new Map(), symbolCenters = new Map();
  function symbolPath(index) {
    if (symbolPaths.has(index)) return symbolPaths.get(index);
    // Uneven scoops and broad swells make asymmetric ink-like silhouettes.
    // Low-frequency curves keep the identity readable at small sizes without
    // repeating petals, radial symmetry or a finite palette.
    let seed = Math.imul(index + 1, 2654435761) >>> 0;
    const random = () => { seed += 0x6D2B79F5; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t ^= t + Math.imul(t ^ t >>> 7, 61 | t); return ((t ^ t >>> 14) >>> 0) / 4294967296; };
    const turn = random() * Math.PI * 2, stretch = .66 + random() * .34;
    const shear = (random() - .5) * .42, phase = random() * Math.PI * 2;
    const cuts = Array.from({length: 1 + index % 3}, (_, i) => ({
      angle: turn + i * 2.1 + (random() - .5) * .9,
      width: .28 + random() * .25, depth: .35 + random() * .23,
    }));
    const count = 96;
    const points = Array.from({length: count}, (_, i) => {
      const angle = i / count * Math.PI * 2;
      const scoops = cuts.reduce((sum, cut) => {
        const distance = Math.atan2(Math.sin(angle - cut.angle), Math.cos(angle - cut.angle));
        return sum + cut.depth * Math.exp(-.5 * (distance / cut.width) ** 2);
      }, 0);
      const radius = Math.max(.25, .83 + .16 * Math.cos(2 * angle + phase) + .09 * Math.sin(3 * angle - turn) - scoops);
      const twist = angle + .12 * Math.sin(angle + phase), y = Math.sin(twist) * radius;
      return {x: Math.cos(twist) * radius * stretch + y * shear, y};
    });
    const extent = Math.max(...points.map(p => Math.hypot(p.x, p.y)));
    for (const p of points) { p.x /= extent; p.y /= extent; }
    symbolCenters.set(index, Math.min(.2, ...points.map(p => Math.hypot(p.x, p.y) * .55)));
    const number = n => n.toFixed(5), point = p => `${number(p.x)} ${number(p.y)}`;
    let path = `M${point(points[0])}`;
    for (let i = 0; i < count; i++) {
      const a = points[(i + count - 1) % count], b = points[i], c = points[(i + 1) % count], d = points[(i + 2) % count];
      path += ` C${point({x: b.x + (c.x - a.x) / 6, y: b.y + (c.y - a.y) / 6})} ${point({x: c.x - (d.x - b.x) / 6, y: c.y - (d.y - b.y) / 6})} ${point(c)}`;
    }
    path += " Z"; symbolPaths.set(index, path); return path;
  }
  function assignFamilySymbols(field) {
    const key = `atlas:family-symbols:v1:${field.scale}:${scenario}`;
    try { familySymbols = new Map(JSON.parse(localStorage.getItem(key) || "[]")); } catch { familySymbols = new Map(); }
    const parents = field.agents.filter(a => field.children(a.id).length);
    let next = Math.max(-1, ...familySymbols.values()) + 1;
    for (const a of [...parents].sort((a, b) => a.id.localeCompare(b.id))) if (!familySymbols.has(a.id)) familySymbols.set(a.id, next++);
    try { localStorage.setItem(key, JSON.stringify([...familySymbols])); } catch { /* private mode */ }
  }
  const symbolOwner = a => currentField.children(a.id).length ? a.id : currentField.parent(a.id)?.id;
  function drawSymbol(a, target, radius, panelIcon = false) {
    const owner = symbolOwner(a), index = familySymbols.get(owner), isParent = currentField.children(a.id).length > 0;
    if (index == null) { svgEl("circle", {class: "core", r: radius, style: "fill:" + fillFor(currentField, a)}, target); return; }
    const path = symbolPath(index), attrs = {d: path, "fill-rule": "evenodd", "data-symbol-owner": owner, "data-symbol": index};
    const size = panelIcon && isParent ? radius * 1.25 : radius;
    svgEl("path", {...attrs, class: "core family-core" + (isParent ? " parent-core" : ""), transform: `scale(${size})`, style: "fill:" + fillFor(currentField, a)}, target);
    if (isParent) svgEl("circle", {class: "parent-center", r: size * symbolCenters.get(index)}, target);
    const upstream = isParent && currentField.parent(a.id);
    if (upstream && familySymbols.has(upstream.id)) svgEl("path", {class: "upstream-symbol", d: symbolPath(familySymbols.get(upstream.id)), "fill-rule": "evenodd", transform: `translate(${radius + 3 * (panelIcon ? 1 : px())} ${-radius - 3 * (panelIcon ? 1 : px())}) scale(${radius * .45})`, style: "fill:" + fillFor(currentField, a)}, target);
  }

  const radiusFor = (a, far, k) => {
    const parent = currentField.children(a.id).length > 0, family = symbolOwner(a) != null;
    if (currentField.needsUser(a)) return (parent ? (far ? 9.5 : 11.5) : (far ? 6.5 : 8)) * k;
    const size = far ? (parent ? 8.5 : family ? 5 : a._band === "finished" ? 2.3 : 3.5) : parent ? Math.max(11, sizeFor(a, false)) : family ? Math.max(6, sizeFor(a, false)) : sizeFor(a, false);
    return Math.min(size * k, Math.max(1.5 * k, ((far ? a._overviewSpace : a._space) || Infinity) * .4));
  };

  /* ---------- Clusters ---------- */
  function buildClusters(field) {
    for (const a of field.agents) a.atlasPhase = a.phase;
    const map = new Map();
    const add = (key, a, raw) => { if (!map.has(key)) map.set(key, {key, members: [], raw: new Map(), secondary: new Map()}); const c = map.get(key); c.members.push(a); if (raw) c.raw.set(raw, (c.raw.get(raw) || 0) + 1); return c; };
    const byGroup = state.grouping === "group";
    if (byGroup) {
      for (const a of field.agents) {
        // Keep unassigned children with their root, but honor explicit child groups.
        const owner = a.group ? a : rootOf(a.id), workspace = a.workspace || "";
        const key = owner.group ? "group:" + (owner.groupId || JSON.stringify([workspace, owner.group])) : "ungrouped:" + workspace;
        const c = add(key, a);
        c.label = owner.group || "Ungrouped" + (workspace && (!field.workspace || field.workspace === "all") ? " · " + (workspace.split("/").filter(Boolean).pop() || workspace) : "");
      }
    } else {
      for (const a of field.roots()) { const ds = field.domainsOf(a); const c = add(ds[0] ? clusterKey(ds[0]) : "∅", a, ds[0] || null); if (ds[1]) c.secondary.set(a.id, clusterKey(ds[1])); }
    }
    const odd = c => c.members.length === 1 && c.key !== "∅" && !c.key.includes("/") && !AgentField.DOMAINS.includes(c.key) && scenario !== "none" && c.members.every(a => a.domainSource !== "derived");
    const singles = byGroup ? [] : [...map.values()].filter(odd);
    if (singles.length >= 3) { const other = {key: "other", members: [], raw: new Map(), secondary: new Map(), merged: singles.map(c => c.key)}; for (const c of singles) { map.delete(c.key); other.members.push(...c.members); for (const [r, n] of c.raw) other.raw.set(r, n); } map.set("other", other); }
    for (const c of map.values()) {
      const top = [...c.raw.entries()].sort((x, y) => y[1] - x[1])[0]?.[0];
      const pretty = text => text.includes("/") ? text : text.replace(/\b[a-z]/g, ch => ch.toUpperCase());
      c.derived = !byGroup && c.key !== "∅" && c.members.length > 0 && c.members.every(a => a.domainSource === "derived");
      if (!byGroup) c.label = c.key === "∅" ? "No domain reported" : c.key === "other" ? "Other" : (AgentField.DOMAIN_LABELS[c.key] || (top ? pretty(top) : pretty(c.key)));
      c.aliases = [...new Set([...c.raw.keys()].filter(r => clusterKey(r) === c.key && norm(r) !== norm(c.label) && norm(r) !== c.key))];
      c.source = c.derived ? "from write scope" : c.key === "other" ? c.merged.length + " labels" : c.aliases.length ? "also " + c.aliases.slice(0, 3).join(", ") : "";
      c.roots = [...c.members];
      if (!byGroup) c.members = c.roots.flatMap(a => [a, ...field.descendants(a.id)]);
      c.needs = c.members.filter(field.needsUser).length;
      c.working = c.members.filter(a => a.status === "working").length;
      c.lastActivity = Math.max(...c.members.map(field.lastActivity));
      // Six-hour half-life, with a visible 30% floor after two dormant days.
      const age = Math.max(0, field.now - c.lastActivity);
      c.activity = age >= 48 * 60 * MIN ? .3 : .3 + .7 * Math.pow(2, -age / (6 * 60 * MIN));
      c.overviewRadius = 64 + 28 * Math.sqrt(c.members.length);
      c.positions = radial.place(c.key, c.members, a => rootOf(a.id).id, field.needsUser);
      c.geometry = radial.describe(c.key); c.r = c.geometry.rim;
    }
    return map;
  }
  function placeClusters(map, anchor = null) {
    const seeded = AtlasLayout.seedCenters([...map.values()].map(c => ({key: c.key, r: Math.max(c.r, c.overviewRadius) * 1.39})), clusterPos);
    const repaired = AtlasLayout.separateCenters([...map.values()].map(c => {
      const scale = Math.max(1, c.overviewRadius / c.r);
      return {key: c.key, bounds: Object.fromEntries(Object.entries(AtlasLayout.bounds(c.geometry)).map(([key, value]) => [key, value * scale]))};
    }), seeded, anchor);
    for (const c of map.values()) {
      const p = repaired.get(c.key), previous = clusterPos.get(c.key);
      if (previous) radial.moveHull(c.key, p.x - previous.x, p.y - previous.y);
      clusterPos.set(c.key, p); c.x = p.x; c.y = p.y;
    }
  }
  function placeMembers(c, field) {
    const positions = radial.place(c.key, c.members, a => rootOf(a.id).id, field.needsUser);
    c.geometry = radial.describe(c.key); c.r = c.geometry.rim;
    for (const a of c.members) {
      const p = positions.get(a.id);
      a._x = c.x + p.x; a._y = c.y + p.y;
      a._home = {x: c.x + p.home.x, y: c.y + p.home.y}; a._ang = p.angle; a._band = p.band; a._family = p.family; a._pinned = p.pinned;
    }
    for (const a of c.members) a._space = Math.min(...c.members.filter(b => b !== a && !foldedFinished(field, b)).map(b => Math.hypot(a._x - b._x, a._y - b._y)));
  }

  function contourPath(c, radius = c.r) {
    return AtlasLayout.contour(c.geometry, radius).map((p, i) => `${i ? "L" : "M"}${c.x + p.x} ${c.y + p.y}`).join(" ") + " Z";
  }
  const blobPath = c => contourPath(c);
  const territoryBounds = c => { const b = AtlasLayout.bounds(c.geometry); return {left: c.x + b.left, right: c.x + b.right, top: c.y + b.top, bottom: c.y + b.bottom}; };
  const overviewPath = (c, radius = c.r) => AtlasLayout.contour(c.geometry, radius).map((p, i) => `${i ? "L" : "M"}${c.x + p.x * c.overviewRadius / c.r} ${c.y + p.y * c.overviewRadius / c.r}`).join(" ") + " Z";
  const overviewBounds = c => {
    const b = AtlasLayout.bounds(c.geometry), scale = c.overviewRadius / c.r;
    return {left: c.x + b.left * scale, right: c.x + b.right * scale, top: c.y + b.top * scale, bottom: c.y + b.bottom * scale};
  };
  function domainLabels(list, k) {
    // Full labels may extend beyond a hull; include their footprint when fitting.
    return list.map(c => {
      const rows = 2 + Number(c.needs > 0), width = Math.max(176, c.label.length * 7 + 16) * k;
      return {key: c.key, x: c.x - width / 2, y: overviewBounds(c).top - (rows * 14 + 8) * k,
        w: width, h: rows * 14 * k, scale: k, rows};
    });
  }
  function placeOverviewMembers(c) {
    const scale = c.overviewRadius / c.r;
    for (const a of c.members) {
      // Use the same phase geometry as the contours at both zoom levels.
      a._overview = {x: c.x + (a._x - c.x) * scale, y: c.y + (a._y - c.y) * scale};
    }
    const visible = c.members.filter(a => !foldedFinished(currentField, a));
    for (const a of visible) a._overviewSpace = Math.min(...visible.filter(b => b !== a).map(b => Math.hypot(a._overview.x - b._overview.x, a._overview.y - b._overview.y)));
  }
  function overviewRings(c) {
    const core = c.geometry.bands.find(b => b.name === "needs").outer;
    const work = c.geometry.bands.filter(b => b.name !== "needs" && b.name !== "finished").at(-1)?.outer;
    // Reserve a middle contour even when the domain has no work in progress.
    return [{name: "core", radius: core}, {name: "in-progress", radius: work || (core + c.r) / 2}, {name: "rim", radius: c.r}];
  }
  function phaseRuler(c, k) {
    const x = territoryBounds(c).right + 24 * k;
    let previousY = Infinity;
    const rows = c.geometry.bands.map(band => {
      const tickY = c.y - AtlasLayout.radiusAt(c.geometry, -Math.PI / 2, band.outer);
      const labelY = band.count ? Math.min(tickY, previousY - 15 * k) : tickY;
      if (band.count) previousY = labelY;
      return {band, tickY, labelY};
    });
    const top = Math.min(...rows.map(r => Math.min(r.tickY, r.labelY))) - 10 * k;
    const bottom = c.y + 8 * k;
    return {x, y: top, w: 148 * k, h: bottom - top, rows};
  }
  function layout(field) {
    clusters = buildClusters(field); placeClusters(clusters);
    for (const c of clusters.values()) placeMembers(c, field, clusters);
    persist();
  }

  /* ---------- Camera ---------- */
  function applyView() {
    if (!state.view) return;
    const v = state.view, detail = state.level > 0 && viewScale() >= (state.detailScale || layoutScale()) * .7;
    svg.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
    svg.dataset.labels = state.labels; svg.dataset.detail = detail ? "on" : "off"; svg.dataset.lineage = svg.dataset.detail;
  }
  function setView(box, animate = true) {
    const target = box; cancelAnimationFrame(state.anim); state.targetScale = W() / target.w;
    if (!animate || !state.view || matchMedia("(prefers-reduced-motion: reduce)").matches) { state.view = target; applyView(); return; }
    const from = {...state.view}, t0 = performance.now(), dur = 520;
    const step = now => { const p = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - p, 3); state.view = {x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, w: from.w + (target.w - from.w) * e, h: from.h + (target.h - from.h) * e}; applyView(); if (p < 1) state.anim = requestAnimationFrame(step); };
    state.anim = requestAnimationFrame(step);
  }

  function viewFor(list) {
    if (!list.length) return {x: 0, y: 0, w: 1600, h: 1000};
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const c of list) {
      const b = state.focus === c.key ? territoryBounds(c) : overviewBounds(c);
      x0 = Math.min(x0, b.left); y0 = Math.min(y0, b.top);
      x1 = Math.max(x1, b.right); y1 = Math.max(y1, b.bottom);
      for (const a of c.members.filter(a => a._pinned)) {
        const scale = state.focus === c.key ? 1 : c.overviewRadius / c.r;
        const x = c.x + (a._x - c.x) * scale, y = c.y + (a._y - c.y) * scale;
        x0 = Math.min(x0, x - 12); y0 = Math.min(y0, y - 12); x1 = Math.max(x1, x + 12); y1 = Math.max(y1, y + 12);
      }
    }
    const top = Math.max(76, (toolbar?.offsetHeight || 50) + 24), bottom = Math.max(86, (legend?.offsetHeight || 30) + 56);
    const availableHeight = Math.max(80, H() - top - bottom);
    const base = {x0, y0, x1, y1};
    let scale;
    for (let pass = 0; pass < 8; pass++) {
      scale = Math.min(2.2, Math.max(80, W() - 120) / Math.max(1, x1 - x0), availableHeight / Math.max(1, y1 - y0));
      ({x0, y0, x1, y1} = base);
      const rulerBoxes = list.filter(c => state.focus === c.key).map(c => phaseRuler(c, 1 / scale));
      for (const box of [...domainLabels(list, 1 / scale), ...rulerBoxes]) { x0 = Math.min(x0, box.x); y0 = Math.min(y0, box.y); x1 = Math.max(x1, box.x + box.w); y1 = Math.max(y1, box.y + box.h); }
    }
    scale = Math.min(2.2, Math.max(80, W() - 120) / Math.max(1, x1 - x0), availableHeight / Math.max(1, y1 - y0));
    return {x: (x0 + x1) / 2 - W() / (2 * scale), y: (y0 + y1) / 2 - (top + availableHeight / 2) / scale, w: W() / scale, h: H() / scale};
  }
  const allBounds = () => viewFor([...clusters.values()]);
  function fitAll(animate = true) { state.level = 0; state.focus = null; state.hub = null; state.selected = null; state.hovered = null; fitCurrent(animate); }
  function fitCurrent(animate = true) { drawToolbar(currentField); const box = state.focus && clusters.has(state.focus) ? viewFor([clusters.get(state.focus)]) : allBounds(); state.detailScale = state.level > 0 ? W() / box.w : null; if (!state.level) state.overviewScale = W() / box.w; state.fitted = true; state.fitKey = JSON.stringify(box); setView(box, animate); draw(); }
  function focusCluster(key, animate = true) { const c = clusters.get(key); if (!c) return; if (state.focus !== key) state.historyExpanded = false; state.level = 1; state.focus = key; state.hub = null; state.selected = null; state.hovered = null; fitCurrent(animate); }
  function focusHub(id, animate = true) { const a = rootOf(id), c = clusterOf(id); if (!a || !c) return; if (state.focus !== c.key) state.historyExpanded = false; state.level = 2; state.focus = c.key; state.hub = a.id; state.hovered = null; fitCurrent(animate); }
  function back() { if (state.level === 2) focusCluster(state.focus); else if (state.level === 1) fitAll(); }
  function clusterOf(id) { for (const c of clusters.values()) if (c.members.some(a => a.id === id)) return c; const p = currentField.parent(id); return p ? clusterOf(p.id) : null; }
  function rootOf(id) { let a = currentField.byId(id); while (a && a.parentId && currentField.byId(a.parentId)) a = currentField.byId(a.parentId); return a; }
  function flyTo(id) { const a = currentField.byId(id), c = clusterOf(id); if (!a || !c) return; state.highlight = id; const root = rootOf(id); if (root.id !== id) focusHub(id); else focusCluster(c.key); }
  function toCanvas(dxPx, dyPx) { const k = state.view.w / W(); return {dx: dxPx * k, dy: dyPx * k}; }
  function zoomDetailAt(x, y) {
    if (state.focus) {
      if (viewScale() >= state.detailScale * .65) return false;
      state.level = 0; state.focus = null; state.hub = null; state.selected = null; state.hovered = null;
      return true;
    }
    const nearest = [...clusters.values()].sort((a, b) => Math.hypot(x - a.x, y - a.y) / a.overviewRadius - Math.hypot(x - b.x, y - b.y) / b.overviewRadius)[0];
    if (!nearest || Math.hypot(x - nearest.x, y - nearest.y) > nearest.overviewRadius * 1.4
      || nearest.overviewRadius * viewScale() < 150 || viewScale() < (state.overviewScale || 0) * 1.5) return false;
    state.level = 1; state.focus = nearest.key; state.detailScale = viewScale(); state.historyExpanded = false;
    return true;
  }

  /* ---------- Rendering ---------- */
  function build(stage) {
    stageRoot = stage; stage.classList.add("live-atlas"); stage.replaceChildren(); stage.dataset.panel = state.panel;
    canvas = $("div", "canvas"); stage.append(canvas);
    svg = svgEl("svg", {class: "atlas", "aria-label": "Domain atlas"}, canvas);
    empty = $("p", "canvas-empty"); empty.hidden = true; canvas.append(empty);
    toolbar = $("div", "toolbar"); canvas.append(toolbar);
    legend = $("div", "legend"); canvas.append(legend);
    controls = $("div", "controls"); canvas.append(controls);
    panel = $("aside", "panel"); panel.setAttribute("aria-label", "Agent family and needs you"); stage.append(panel);
    Mockup.drag(svg, {onStart: () => svg.classList.add("dragging"), onMove: ({mx, my}) => { state.fitted = false; const {dx, dy} = toCanvas(mx, my); cancelAnimationFrame(state.anim); state.view = {...state.view, x: state.view.x - dx, y: state.view.y - dy}; applyView(); }, onEnd: ({dragged, event}) => { svg.classList.remove("dragging"); if (!dragged && event.target === svg) back(); }});
    svg.addEventListener("wheel", e => {
      e.preventDefault(); state.fitted = false;
      const k = Math.exp(e.deltaY * 0.0016), r = svg.getBoundingClientRect();
      const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height, v = state.view;
      const nw = Math.max(100, Math.min(Math.max(4000, allBounds().w * 1.2), v.w * k)), nh = v.h * (nw / v.w);
      cancelAnimationFrame(state.anim);
      state.view = {x: v.x + (v.w - nw) * fx, y: v.y + (v.h - nh) * fy, w: nw, h: nh}; state.targetScale = viewScale();
      const changed = zoomDetailAt(v.x + v.w * fx, v.y + v.h * fy);
      if (changed) { drawToolbar(currentField); drawPanel(currentField); }
      if (currentField && (changed || Math.abs(state.targetScale - drawnScale) / drawnScale > .18)) { layout(currentField); drawCanvas(currentField); }
      applyView();
    }, {passive: false});
    resizeObserver?.observe(stage);
  }
  function drawToolbar(field) {
    svg.setAttribute("aria-label", state.grouping === "group" ? "Group atlas" : "Domain atlas");
    toolbar.replaceChildren();
    const clustering = $("label", "clustering", "Cluster by");
    const clusterSel = $("select"); clusterSel.setAttribute("aria-label", "Cluster by");
    for (const [value, label] of [["domain", "Domain"], ["group", "Group"]]) { const option = $("option", null, label); option.value = value; option.selected = value === state.grouping; clusterSel.append(option); }
    clusterSel.onchange = () => {
      persist(); state.grouping = clusterSel.value;
      try { localStorage?.setItem("agent-coord.atlas-grouping", state.grouping); } catch { /* optional preference */ }
      loadLayout(field); layout(field); if (state.view) draw(); else fitAll(false);
    };
    clustering.append(clusterSel); toolbar.append(clustering);
    const crumb = $("span", "crumb");
    if (field.scale === "real") {
      const projectName = field.workspace && field.workspace !== "all" ? field.workspace.replace(/\/$/, "").split("/").pop() || field.workspace : "All projects";
      crumb.append($("b", null, projectName), $("small", null, field.agents.length + (field.agents.length === 1 ? " thread" : " threads")), $("span", "muted", "›"));
    }
    const all = $("button", null, state.grouping === "group" ? "All groups" : "All domains"); all.type = "button"; all.onclick = () => fitAll(); crumb.append(all);
    const c = state.focus && clusters.get(state.focus);
    if (c) { crumb.append($("span", "muted", "›")); if (state.level === 2) { const b = $("button", null, c.label); b.type = "button"; b.onclick = () => focusCluster(c.key); crumb.append(b); } else crumb.append($("b", null, c.label)); if (c.source && state.level === 1) crumb.append($("small", null, c.source)); }
    if (state.level === 2 && field.byId(state.hub)) { const h = field.byId(state.hub); crumb.append($("span", "muted", "›"), $("b", null, h.name || h.title)); }
    toolbar.append(crumb);
    const clusterNoun = state.grouping === "group" ? "group" : "domain";
    toolbar.append($("span", "hint", state.level === 2 ? "Family highlighted · click to inspect · drag outside to pin · back inside to release · Esc goes back" : state.level === 1 ? "Click to inspect · drag around a contour · drag outside to pin" : `Click a ${clusterNoun} to zoom in · drag a ${clusterNoun} to move it · scroll to zoom`));
    legend.replaceChildren();
    const geometry = $("span", null, "Center → rim: checkpoint phases");
    const needs = $("span"); needs.append($("i", "needs-you"), "Needs you");
    const working = $("span"); working.append($("i", "working"), "Running");
    const parent = $("span"), bloom = svgEl("svg", {class: "parent", viewBox: "-1.1 -1.1 2.2 2.2", "aria-hidden": "true"});
    svgEl("path", {d: symbolPath(2), fill: "var(--muted)"}, bloom); svgEl("circle", {class: "parent-center", r: symbolCenters.get(2)}, bloom);
    parent.append(bloom, "Shared form: family · open center: parent");
    const done = $("span", null, "Older finished: included in count · ◆ pinned");
    if (state.level === 0) legend.append($("span", null, "Size: thread count"), $("span", null, "Contours: core · in progress · rim"), needs, working, parent);
    else legend.append(geometry, parent, needs, working, done);
    const selected = field.byId(state.selected), selectedCluster = selected && clusterOf(selected.id);
    if (selected && selectedCluster && state.level > 0) {
      const actions = $("span", "node-actions"), name = $("span", "selected-name", selected.name || selected.title);
      name.title = selected.title; actions.append(name);
      const pin = $("button", "btn small", selected._pinned ? "Return to phase layout" : "Pin freely");
      pin.type = "button";
      pin.title = selected._pinned ? "Restore this thread to its current phase band" : "Keep this position through phase changes; drag anywhere, or drag outside and back inside to restore phase placement";
      pin.onclick = () => { if (selected._pinned) radial.unpin(selectedCluster.key, selected.id); else radial.pin(selectedCluster.key, selected.id); draw(); };
      actions.append(pin); toolbar.append(actions);
    }
    if (c) {
      const older = c.members.filter(a => olderFinished(field, a));
      if (older.length) {
        const history = $("button", "btn small", state.historyExpanded ? "Fold older finished" : `Show ${older.length} older finished`);
        history.type = "button"; history.onclick = () => { state.historyExpanded = !state.historyExpanded; draw(); }; toolbar.append(history);
      }
    }
    controls.replaceChildren();
    const panelSel = $("select"); for (const [v, l] of [["right", "Panel: right"], ["bottom", "Panel: bottom"]]) { const o = $("option", null, l); o.value = v; o.selected = v === state.panel; panelSel.append(o); }
    panelSel.onchange = () => { state.panel = panelSel.value; stageRoot.dataset.panel = state.panel; requestAnimationFrame(() => { layout(field); if (state.level > 0) fitCurrent(false); else setView(allBounds(), false); draw(); }); };
    const labelSel = $("select"); for (const [v, l] of [["auto", "Labels: auto"], ["on", "Labels: on"], ["needs", "Labels: needs you"], ["off", "Labels: off"]]) { const o = $("option", null, l); o.value = v; o.selected = v === state.labels; labelSel.append(o); }
    labelSel.onchange = () => { state.labels = labelSel.value; resolveLabels(field); applyView(); };
    const fit = $("button", "btn small", "Fit"); fit.type = "button"; fit.onclick = () => fitCurrent();
    const reset = $("button", "btn small", "Reset layout"); reset.type = "button"; reset.onclick = () => { clusterPos.clear(); radial = AtlasLayout.create(); lastDraw.clear(); persist(); layout(field); fitAll(); };
    controls.append(panelSel, labelSel, fit, reset);
  }
  function drawPanel(field) {
    panel.replaceChildren();
    const selected = field.byId(state.selected);
    if (selected) {
      const detail = $("section", "atlas-inspector"); detail.setAttribute("aria-label", "Selected thread");
      detail.append($("h2", null, selected.title), $("p", "muted", nodeDoing(selected, field)));
      if (selected.summary) detail.append($("p", null, selected.summary));
      if (selected.next.action) detail.append($("p", null, selected.next.action));
      const open = $("button", "btn", "Open conversation"); open.type = "button"; open.onclick = () => onOpen(selected.id);
      const close = $("button", "btn", "Close preview"); close.type = "button"; close.onclick = () => { state.selected = null; draw(); };
      detail.append(open, close); panel.append(detail);
    }
    if (state.hub) drawFamilyPanel(field);
    const asks = field.asks();
    const head = $("div", "panel-head"); head.append($("i"), "Needs you" + (asks.length ? " · " + asks.length : "")); panel.append(head);
    if (!asks.length) { panel.append($("p", "calm", "Nothing needs you. Threads move inward when they need your attention. Pinned threads keep their chosen positions.")); return; }
    for (const a of asks) {
      const item = $("div", "ask-item" + (state.highlight === a.id ? " located" : "")); item.tabIndex = 0;
      item.onclick = e => { if (e.target.closest("button")) return; if (state.highlight === a.id && state.level > 0) { const el = nodeEls.get(a.id) || nodeEls.get(rootOf(a.id)?.id); api.peek(a.id, el || item); } else flyTo(a.id); };
      item.onkeydown = e => { if (e.key === "Enter") item.click(); };
      const top = $("div", "top"); top.append($("span", "kind", a.ask ? (a.ask.kind === "failure" ? "failed" : a.ask.kind) : "needs you"), AgentField.providerLogo(a.client, AgentField.PROVIDER_NAMES[a.client]), $("b", null, a.name || a.title), $("small", null, field.relativeTime(a.ask?.askedAt || a.updatedAt)));
      item.append(top);
      const parent = a.parentId && field.byId(a.parentId); const c = clusterOf(a.id);
      item.append($("div", "from", (parent ? "from " + (parent.name || parent.title) + " · " : "") + (c ? c.label : "") + (field.impact(a.id).length ? " · blocks " + field.impact(a.id).length : "")));
      item.append($("div", "q", a.ask ? a.ask.question : a.next.action));
      const act = $("div", "act");
      const open = $("button", "btn small", "Open conversation"); open.type = "button";
      open.onclick = e => { e.stopPropagation(); onOpen(a.id); }; act.append(open);
      act.append($("span", "locate", state.highlight === a.id ? "located" : "show on map ↗")); item.append(act);
      panel.append(item);
    }
  }
  function drawFamilyPanel(field) {
    const root = rootOf(state.hub); if (!root) return;
    const section = $("section", "family-panel"), heading = $("div", "family-heading");
    heading.append($("h2", null, "Agent family"));
    const close = $("button", "btn small quiet", "Close"); close.type = "button";
    close.setAttribute("aria-label", "Close agent family"); close.onclick = () => focusCluster(state.focus);
    heading.append(close); section.append(heading);
    const tree = $("ul", "family-tree"); tree.setAttribute("aria-label", "Parent and child agents");
    const branch = (a, list) => {
      const children = field.children(a.id), item = $("li"), button = $("button", "family-member");
      button.type = "button"; button.dataset.agentId = a.id;
      button.setAttribute("aria-current", String(a.id === (state.selected || root.id)));
      const dot = svgEl("svg", {class: "family-symbol", viewBox: "-9 -9 18 18", "aria-hidden": "true"}); drawSymbol(a, dot, 5, true);
      const text = $("span"); text.append($("b", null, a.name || a.title));
      text.append($("small", null, (a.id === root.id ? "Parent" : "Child" + (children.length ? " · also a parent" : "")) + " · " + nodeDoing(a, field)));
      button.append(dot, text); item.append(button); list.append(item);
      button.onclick = () => {
        if (clusterOf(a.id)?.key !== state.focus) focusHub(a.id, false);
        state.selected = a.id; state.highlight = a.id; draw();
        nodeEls.get(a.id)?.focus({preventScroll: true});
        api.peek(a.id, nodeEls.get(a.id) || button);
      };
      if (children.length) { const nested = $("ul"); item.append(nested); for (const child of children) branch(child, nested); }
    };
    branch(root, tree); section.append(tree, $("p", null, "Lines connect parents and children. Select an agent to inspect its work."));
    panel.append(section);
  }
  function drawCanvas(field) {
    state.phasePreview = null;
    svg.replaceChildren(); nodeEls.clear(); drawnScale = layoutScale();
    empty.hidden = field.agents.length > 0;
    empty.textContent = "No threads in this project.";
    const gBlobs = svgEl("g", {class: "blobs"}, svg), gGuides = svgEl("g", {class: "guides work-detail"}, svg), gSectors = svgEl("g", {class: "sectors work-detail"}, svg), gTies = svgEl("g", {class: "ties work-detail"}, svg), gOverview = svgEl("g", {class: "overview-nodes"}, svg), gNodes = svgEl("g", {class: "nodes work-detail"}, svg);
    const k = px();
    for (const c of clusters.values()) {
      const focused = state.focus === c.key;
      const g = svgEl("g", {class: "cluster" + (focused ? " focused" : state.level ? " dim" : "") + (c.needs ? " needs" : ""), "data-domain": c.key, "data-activity": c.activity.toFixed(3), "data-threads": c.members.length, style: `--hull-activity:${c.activity}`}, gBlobs);
      const overview = svgEl("g", {class: "overview-hull", role: "button", tabindex: 0,
        "aria-label": `${c.label}. ${c.members.length} threads. ${c.needs ? c.needs + " need you. " : ""}Zoom into ${state.grouping === "group" ? "group" : "domain"}.`}, g);
      svgEl("path", {class: "overview-hit", d: overviewPath(c)}, overview);
      for (const ring of overviewRings(c)) svgEl("path", {class: "overview-contour hull-surface", "data-band": ring.name, "data-radius": ring.radius, d: overviewPath(c, ring.radius)}, overview);
      const title = svgEl("title", {}, overview);
      title.textContent = `${c.label} · ${c.members.length} threads · last activity ${field.relativeTime(c.lastActivity)}`
        + (c.working ? ` · ${c.working} working` : "") + (c.needs ? ` · ${c.needs} need you` : "") + (c.source ? "\n" + c.source : "");
      const activate = () => focusCluster(c.key);
      overview.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); } });
      overview.addEventListener("pointerdown", e => e.stopPropagation());
      Mockup.drag(overview, {onMove: ({mx, my}) => {
        if (state.level) return;
        state.fitted = false; const {dx, dy} = toCanvas(mx, my);
        c.x += dx; c.y += dy; radial.moveHull(c.key, dx, dy); clusterPos.set(c.key, {x: c.x, y: c.y}); placeMembers(c, field);
        for (const path of overview.querySelectorAll("path")) path.setAttribute("d", overviewPath(c, Number(path.dataset.radius) || c.r));
        placeOverviewMembers(c);
        for (const a of c.members) nodeEls.get(a.id)?.setAttribute("transform", `translate(${a._overview.x} ${a._overview.y})`);
        const caption = [...svg.querySelectorAll(".overview-caption")].find(el => el.getAttribute("data-domain") === c.key);
        if (caption) for (const el of caption.querySelectorAll("text")) { el.setAttribute("x", +el.getAttribute("x") + dx); el.setAttribute("y", +el.getAttribute("y") + dy); }
      }, onEnd: ({dragged}) => { if (dragged) { placeClusters(clusters, c.key); lastDraw.clear(); persist(); draw(); } else activate(); }});
      placeOverviewMembers(c);
      const overviewNodes = svgEl("g", {class: "overview-threads" + (focused ? " focused" : ""), "data-domain": c.key}, gOverview);
      for (const a of c.members) drawNode(a, field, overviewNodes, {far: true, focused: false, hubView: false, faded: state.level > 0 && !focused && !field.needsUser(a)});
      if (!focused) continue;
      // Hidden overview marks must not become endpoints for detail lineage.
      for (const a of c.members) nodeEls.delete(a.id);
      const path = svgEl("path", {class: "blob hull-surface work-detail", d: blobPath(c)}, g);
      path.addEventListener("pointerdown", e => e.stopPropagation());
      path.addEventListener("click", () => { if (state.level === 2) focusCluster(c.key); });
      drawBands(c, gGuides, true);
      for (const a of c.members) {
        const sec = c.secondary.get(a.id) && clusters.get(c.secondary.get(a.id));
        if (sec && !foldedFinished(field, a)) {
          const dx = sec.x - a._x, dy = sec.y - a._y, d = Math.hypot(dx, dy) || 1, ex = a._x + dx / d * 54 * k, ey = a._y + dy / d * 54 * k;
          svgEl("line", {class: "tie", x1: a._x, y1: a._y, x2: ex, y2: ey}, gTies);
          const text = svgEl("text", {class: "tie-label", x: ex + 3 * k, y: ey + 3 * k, style: `font-size:${8.5 * k}px`}, gTies); text.textContent = "also " + sec.label + " →";
        }
        drawNode(a, field, gNodes, {far: false, focused: true, hubView: state.level === 2 && state.hub === a.id, faded: state.level === 2 && rootOf(a.id)?.id !== rootOf(state.hub)?.id && !field.needsUser(a)});
      }
    }
    // Draw captions last so full domain names remain legible beyond their hulls.
    const captions = svgEl("g", {class: "captions"}, svg);
    for (const box of domainLabels([...clusters.values()], k)) {
      const c = clusters.get(box.key), focused = state.focus === c.key, scale = box.scale;
      const group = svgEl("g", {class: "domain-caption overview-caption" + (focused ? " focused" : state.level ? " dim" : ""), "data-domain": c.key}, captions);
      let y = box.y + 11 * scale;
      const line = (text, cls) => {
        const el = svgEl("text", {class: cls, x: c.x, y, "text-anchor": "middle", style: `font-size:${(cls === "cluster-label" ? 13 : 10) * scale}px;stroke-width:${3 * scale}px`}, group);
        el.textContent = text; y += 14 * scale;
      };
      line(c.label, "cluster-label");
      if (box.rows > 1) line(c.members.length + (c.members.length === 1 ? " thread" : " threads"), "cluster-sub");
      if (box.rows > 2) line(`${c.needs} need${c.needs === 1 ? "s" : ""} you`, "cluster-needs");
      if (focused) {
        const detail = svgEl("g", {class: "domain-caption work-detail", "data-domain": c.key}, captions);
        const label = svgEl("text", {class: "cluster-label", x: c.x, y: c.y - AtlasLayout.radiusAt(c.geometry, -Math.PI / 2) + 20 * k,
          "text-anchor": "middle", style: `font-size:${13 * k}px;stroke-width:${3 * k}px`}, detail);
        label.textContent = c.label;
      }
    }
    resolveLabels(field);
    drawLineage(state.hovered || state.selected || state.hub);
  }

  function stateClass(a) { return a.status + (currentField.needsUser(a) ? " needs-you" : ""); }
  function nodeDoing(a, field) {
    if (field.needsUser(a)) return "Needs you · " + field.duration(field.now - (a.ask?.askedAt || a.updatedAt));
    const phase = AtlasLayout.labels[a.atlasPhase || a.phase] || "Getting started";
    if (a._band === "finished") return "Finished · " + field.relativeTime(finishedAt(a));
    if (a.status === "working") return phase + (a.activity ? " · " + a.activity : "");
    if (a.status === "blocked" || a.status === "failed") return phase + " · " + field.statusLabel(a);
    return phase + " · " + field.relativeTime(field.lastActivity(a));
  }
  function drawBands(c, layer, focused) {
    const k = px(), names = AtlasLayout.labels;
    const group = svgEl("g", {class: "band-guides", "data-domain": c.key, "data-rim": c.r, "data-center-x": c.x, "data-center-y": c.y}, layer);
    for (const band of c.geometry.bands) {
      svgEl("path", {class: "phase-guide", d: contourPath(c, band.outer), "data-band": band.name, "data-count": band.count}, group);
    }
    if (focused) {
      const ruler = phaseRuler(c, k);
      const key = svgEl("g", {class: "phase-ruler", "data-x": ruler.x, "data-y": ruler.y, "data-width": ruler.w, "data-height": ruler.h}, group);
      svgEl("line", {class: "phase-ruler-line", x1: ruler.x, x2: ruler.x, y1: ruler.y + 10 * k, y2: c.y}, key);
      for (const {band, tickY, labelY} of ruler.rows) {
        svgEl("line", {class: "phase-ruler-line", "data-boundary": band.name, x1: ruler.x - 4 * k, x2: ruler.x + 5 * k, y1: tickY, y2: tickY}, key);
        if (!band.count) continue;
        svgEl("path", {class: "phase-ruler-line", d: `M${ruler.x + 5 * k} ${tickY} L${ruler.x + 10 * k} ${labelY} L${ruler.x + 13 * k} ${labelY}`}, key);
        const entry = svgEl("g", {class: "phase-entry", tabindex: 0, role: "button", "data-band": band.name,
          "aria-label": `Highlight ${band.count} ${names[band.name]} threads in ${c.label}`}, key);
        svgEl("rect", {class: "phase-entry-hit", x: ruler.x + 12 * k, y: labelY - 8 * k, width: (ruler.w / k - 12) * k, height: 15 * k}, entry);
        const label = svgEl("text", {class: "phase-label", x: ruler.x + 17 * k, y: labelY + 3 * k, style: `font-size:${10 * k}px;stroke-width:${3 * k}px`}, entry);
        label.textContent = names[band.name] + " · " + band.count;
        const preview = () => { state.phasePreview = {cluster: c.key, band: band.name}; applyPhasePreview(); };
        const clear = () => { state.phasePreview = null; applyPhasePreview(); };
        entry.addEventListener("pointerenter", preview);
        entry.addEventListener("pointerleave", () => { if (document.activeElement !== entry) clear(); });
        entry.addEventListener("focus", preview);
        entry.addEventListener("blur", () => { if (!entry.matches(":hover")) clear(); });
        entry.addEventListener("pointerdown", e => e.stopPropagation());
        entry.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); preview(); } });
      }
    }
  }

  const phaseMatches = a => state.phasePreview?.cluster === clusterOf(a.id)?.key && state.phasePreview?.band === a._band;
  function applyPhasePreview() {
    const c = state.phasePreview && clusters.get(state.phasePreview.cluster);
    const members = new Set(c?.members.map(a => a.id));
    for (const [id, el] of nodeEls) {
      el.classList.toggle("phase-match", members.has(id) && phaseMatches(currentField.byId(id)));
      el.classList.toggle("phase-muted", members.has(id) && !phaseMatches(currentField.byId(id)));
    }
    for (const el of svg.querySelectorAll(".phase-entry, .phase-guide")) {
      const match = !!c && el.getAttribute("data-band") === state.phasePreview.band;
      el.classList.toggle(el.classList.contains("phase-entry") ? "active" : "phase-match", match);
    }
    drawLineage(state.hovered || state.selected || state.hub);
  }

  function drawLineage(id) {
    const layer = svg.querySelector(".sectors"); layer.replaceChildren();
    const selectedRoot = rootOf(id)?.id, k = px();
    const activeFamily = selectedRoot && currentField.children(selectedRoot).length ? selectedRoot : null;
    for (const [nodeId, el] of nodeEls) {
      const a = currentField.byId(nodeId), related = a._family === activeFamily;
      el.classList.toggle("family-active", !!activeFamily && related);
      el.classList.toggle("family-muted", !!activeFamily && !related);
    }
    for (const c of clusters.values()) {
      if (state.focus !== c.key) continue;
      for (const a of c.members) {
        if (a._pinned) {
          svgEl("line", {class: "pin-tether", "data-agent-id": a.id, x1: a._home.x, y1: a._home.y, x2: a._x, y2: a._y}, layer);
          svgEl("circle", {class: "home-ghost", "data-agent-id": a.id, cx: a._home.x, cy: a._home.y, r: 4 * px()}, layer);
        }
        if (state.level === 0 || foldedFinished(currentField, a) && !phaseMatches(a)) continue;
        const parent = currentField.parent(a.id);
        if (!parent || !nodeEls.has(parent.id) || !nodeEls.has(a.id)) continue;
        if (foldedFinished(currentField, parent) && !phaseMatches(parent)) continue;
        const active = a._family === activeFamily, dx = a._x - parent._x, dy = a._y - parent._y, distance = Math.hypot(dx, dy);
        if (distance < .01) continue;
        const start = Math.min(distance * .35, radiusFor(parent, state.level === 0, k) + 4 * k), end = Math.min(distance * .35, radiusFor(a, state.level === 0, k) + 4 * k);
        svgEl("line", {class: "lineage" + (active ? " active" : activeFamily || state.focus && state.focus !== c.key ? " quiet" : ""), "data-parent": parent.id, "data-child": a.id,
          x1: parent._x + dx / distance * start, y1: parent._y + dy / distance * start,
          x2: a._x - dx / distance * end, y2: a._y - dy / distance * end,
          opacity: active ? 1 : Math.max(.5, Math.min(opacityFor(currentField, a), opacityFor(currentField, parent)))}, layer);
      }
    }
  }
  const labelLeft = a => {
    const c = clusterOf(a.id), nearRim = c && Math.hypot(a._x - c.x, a._y - c.y) > AtlasLayout.radiusAt(c.geometry, a._ang) * .7;
    let left = nearRim ? Math.cos(a._ang) >= 0 : Math.cos(a._ang) < -.15;
    if (c && state.focus === c.key) {
      const k = px(), ruler = phaseRuler(c, k), gap = radiusFor(a, false, k) + 7 * k;
      if (a._y + 20 * k > ruler.y && a._y - 12 * k < ruler.y + ruler.h) {
        const textX = a._x + (left ? -gap - 220 * k : gap);
        if (textX < ruler.x + ruler.w && textX + 220 * k > ruler.x - 8 * k) left = a._x < ruler.x + ruler.w / 2;
      }
    }
    return left;
  };
  function drawNode(a, field, parent, {far, focused, hubView, faded}) {
    const k = px(), kids = field.children(a.id), c = clusterOf(a.id);
    const x = far ? a._overview.x : a._x, y = far ? a._overview.y : a._y;
    const parentAgent = field.parent(a.id), relationship = (kids.length ? ". Parent agent" : "") + (parentAgent ? ". Child of " + (parentAgent.name || parentAgent.title) : "");
    const g = svgEl("g", {"data-agent-id": a.id, "data-band": a._band, "data-family": a._family, style: `--node-opacity:${opacityFor(field, a)}`, tabindex: 0, role: "button", "aria-label": a.title + relationship + ". " + nodeDoing(a, field) + (a._pinned ? ". Pinned freely" : ""), class: "node " + stateClass(a) + (far && !field.needsUser(a) ? " overview-quiet" : "") + (a._pinned ? " pinned" : "") + (state.highlight === a.id ? " highlight" : "") + (state.selected === a.id ? " peeked" : "") + (faded ? " faded" : ""), transform: `translate(${x} ${y})`}, parent);
    g.classList.toggle("folded-finished", foldedFinished(field, a));
    const previous = !far && lastDraw.get(a.id), now = performance.now();
    let motion = previous?.motion;
    if (previous && !a._pinned && Math.hypot(previous.x - a._x, previous.y - a._y) > 0.01) motion = {from: previous, at: now};
    if (motion && now - motion.at < 650 && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
      const progress = (now - motion.at) / 650, eased = 1 - (1 - progress) ** 3;
      const x = motion.from.x + (a._x - motion.from.x) * eased, y = motion.from.y + (a._y - motion.from.y) * eased;
      g.animate([{transform: `translate(${x}px, ${y}px)`}, {transform: `translate(${a._x}px, ${a._y}px)`}], {duration: 650 * (1 - progress), easing: "cubic-bezier(.2,.7,.2,1)"});
    } else motion = null;
    if (!far) lastDraw.set(a.id, {x: a._x, y: a._y, band: a._band, pinned: a._pinned, motion});
    const r = radiusFor(a, far, k);
    if (a.status === "working" && a._band !== "finished") svgEl("circle", {class: "halo", r: r + 3 * k}, g);
    svgEl("circle", {class: "mark", r: r + 5 * k}, g);
    svgEl("circle", {class: "ring", r: r + 3 * k}, g);
    svgEl("circle", {class: "state", r: r + 2 * k}, g);
    drawSymbol(a, g, r);
    if (a._pinned) { const pin = svgEl("text", {class: "pin-mark", x: r + 2 * k, y: -r, style: `font-size:${9 * k}px`}, g); pin.textContent = "◆"; }
    const doing = nodeDoing(a, field);
    const title = svgEl("title", {}, g); title.textContent = a.title + "\n" + doing + "\n" + "last activity " + field.relativeTime(field.lastActivity(a)) + " · started " + new Date(a.createdAt).toLocaleDateString() + (parentAgent ? "\nFrom " + (parentAgent.name || parentAgent.title) : "") + (kids.length ? "\n" + kids.length + " children" : "") + (a._pinned ? "\nPinned freely: position overrides phase" : "") + (a.domains?.length ? "\nReported domain: " + a.domains.join(", ") : "") + (a.workspace ? "\n" + a.workspace : "");
    const left = far ? Math.cos(a._ang) < 0 : labelLeft(a), lx = (r + 7 * k) * (left ? -1 : 1), anchor = left ? "end" : "start";
    const label = svgEl("text", {class: "node-label" + (hubView ? " hub-label" : ""), x: lx, y: 3.5 * k, "text-anchor": anchor, style: `font-size:${11 * k}px`}, g);
    const name = a.name || a.title; label.textContent = name.slice(0, 28) + (name.length > 28 ? "…" : "");
    const detail = svgEl("text", {class: "node-doing", x: lx, y: 16 * k, "text-anchor": anchor, style: `font-size:${10 * k}px`}, g); detail.textContent = doing.slice(0, 34) + (doing.length > 34 ? "…" : "");
    g.addEventListener("pointerdown", e => e.stopPropagation());
    g.addEventListener("pointerenter", () => { state.hovered = a.id; drawLineage(a.id); });
    g.addEventListener("focus", () => drawLineage(a.id));
    g.addEventListener("blur", () => drawLineage(state.selected || state.hub));
    g.addEventListener("pointerleave", () => { state.hovered = null; drawLineage(state.selected || state.hub); });
    const activate = () => { state.selected = a.id; state.highlight = null;
      const root = rootOf(a.id);
      if (field.children(root.id).length && state.hub !== root.id) focusHub(a.id);
      else if (far) { focusCluster(c.key); state.selected = a.id; draw(); }
      else {
        if (state.hub && root.id !== state.hub) { focusCluster(c.key, false); state.selected = a.id; }
        api.peek(a.id, nodeEls.get(a.id) || g); for (const [id, node] of nodeEls) node.classList.toggle("peeked", id === a.id);
        drawToolbar(field); drawPanel(field); resolveLabels(field); drawLineage(a.id);
      }
    };
    g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); } });
    if (far) { g.addEventListener("click", activate); nodeEls.set(a.id, g); return; }
    let pointerPosition;
    Mockup.drag(g, {onStart: () => { pointerPosition = {x: a._x, y: a._y}; }, onMove: ({mx, my}) => {
      state.fitted = false;
      const {dx, dy} = toCanvas(mx, my); pointerPosition.x += dx; pointerPosition.y += dy;
      radial.drag(c.key, a.id, pointerPosition.x - c.x, pointerPosition.y - c.y);
      placeMembers(c, field);
      for (const member of c.members) {
        const el = nodeEls.get(member.id); if (el) { el.getAnimations().forEach(animation => animation.cancel()); el.setAttribute("transform", `translate(${member._x} ${member._y})`); }
      }
      drawLineage(a.id);
    }, onEnd: ({dragged}) => { if (dragged) { state.selected = a.id; lastDraw.clear(); persist(); draw(); } else activate(); }});
    nodeEls.set(a.id, g);
  }
  function resolveLabels(field) {
    const boxes = [], k = px(), prominent = new Set();
    if (state.focus) {
      const c = clusters.get(state.focus);
      for (const a of [...(c?.members || [])].filter(a => !field.needsUser(a) && a._band !== "finished").sort((a, b) => (b.userTurns || 0) - (a.userTurns || 0) || a.id.localeCompare(b.id)).slice(0, 3)) prominent.add(a.id);
    }
    const ordered = [...field.agents].sort((a, b) => Number(b.id === state.selected) - Number(a.id === state.selected) || Number(field.needsUser(b)) - Number(field.needsUser(a)) || (b.userTurns || 0) - (a.userTurns || 0));
    for (const a of ordered) {
      const el = nodeEls.get(a.id); if (!el || foldedFinished(field, a)) continue;
      const essential = (state.level > 0 && field.needsUser(a)) || a.id === state.selected || a.id === state.highlight;
      const inFamily = state.hub && a._family === rootOf(state.hub)?.id;
      const eligible = essential || inFamily || (state.focus === clusterOf(a.id)?.key && field.children(a.id).length > 0) || prominent.has(a.id);
      el.classList.toggle("auto-label", eligible); el.classList.toggle("label-hidden", false);
      if (!eligible || state.labels !== "auto") continue;
      const left = labelLeft(a), r = radiusFor(a, state.level === 0, k);
      const x = a._x + (left ? -r - 7 * k - LABEL_W * k : r + 7 * k), y = a._y - 10 * k;
      const box = {x, y, w: LABEL_W * k, h: 16 * k};
      const overlap = boxes.some(b => x < b.x + b.w && x + box.w > b.x && y < b.y + b.h && y + box.h > b.y);
      el.classList.toggle("label-hidden", overlap && !essential);
      if (!overlap || essential) boxes.push(box);
    }
  }
  let draw = () => {};
  function loadLayout(field) {
    if (storeKey && state.view) cameras.set(storeKey, Object.fromEntries(["level", "focus", "hub", "view", "targetScale", "detailScale", "overviewScale", "fitted", "fitKey", "historyExpanded"].map(key => [key, state[key]])));
    cancelAnimationFrame(state.anim);
    Object.assign(state, {phasePreview: null, historyExpanded: false, level: 0, focus: null, hub: null, view: null, highlight: null, hovered: null, selected: null, targetScale: null, detailScale: null, fitted: true, fitKey: null});
    storeKey = layoutKey(field.workspace);
    let positions = {}; try { positions = JSON.parse(localStorage.getItem(storeKey) || "{}") || {}; } catch { /* private mode */ }
    clusterPos.clear(); lastDraw.clear();
    try { radial = AtlasLayout.create(positions.radial || {}); } catch { radial = AtlasLayout.create(); }
    for (const [key, value] of Object.entries(positions.clusters || {})) clusterPos.set(key, value);
    if (cameras.has(storeKey)) Object.assign(state, cameras.get(storeKey));
  }
  function render(field, stage, mockupApi) {
    const changed = !currentField || currentField.workspace !== field.workspace;
    if (changed) loadLayout(field);
    api = mockupApi; currentField = field; assignFamilySymbols(field);
    AgentField.DOMAINS = [...new Set(field.agents.flatMap(a => a.domains).map(clusterKey))];
    if (state.selected && !field.byId(state.selected)) state.selected = null;
    if (state.hub && !field.byId(state.hub)) state.hub = null;
    if (!svg || !svg.isConnected) { build(stage); layout(field); state.view = null; setView(allBounds(), false); } else layout(field);
    if (changed && !state.view) { const box = allBounds(); state.overviewScale = W() / box.w; setView(box, false); }
    draw = () => {
      layout(field);
      if (state.focus && !clusters.has(state.focus)) Object.assign(state, {level: 0, focus: null, hub: null, fitted: true, fitKey: null});
      drawToolbar(field);
      if (state.fitted) {
        const box = state.focus && clusters.has(state.focus) ? viewFor([clusters.get(state.focus)]) : allBounds(), key = JSON.stringify(box);
        if (key !== state.fitKey) { state.fitKey = key; state.detailScale = state.level > 0 ? W() / box.w : null; if (!state.level) state.overviewScale = W() / box.w; setView(box, lastDraw.size > 0); }
      }
      drawCanvas(field); drawPanel(field); applyView();
    };
    draw();
  }

  const resize = () => { if (!stageRoot?.isConnected || !stageRoot.clientWidth || stageRoot.hidden || !currentField) return; if (state.fitted) fitCurrent(false); else applyView(); };
  const resizeObserver = window.ResizeObserver ? new window.ResizeObserver(resize) : null;
  const escape = e => { if (e.key === "Escape" && stageRoot?.contains(document.activeElement) && state.level > 0) { e.preventDefault(); e.stopPropagation(); back(); } };
  document.addEventListener("keydown", escape);
  return {
    render(threads, stage, {workspace = "all", now = Date.now()} = {}) {
      const field = fieldFromThreads(threads, {workspace, now});
      render(field, stage, {peek: (id, anchor) => { state.selected = id; drawPanel(field); onInspect?.(id, anchor); }});
    },
    focusThread: flyTo,
    destroy() { resizeObserver?.disconnect(); document.removeEventListener("keydown", escape); cancelAnimationFrame(state.anim); persist(); stageRoot?.replaceChildren(); },
  };
}
return {create, fieldFromThreads};
})();
if (typeof module !== "undefined") module.exports = LiveAtlas;
