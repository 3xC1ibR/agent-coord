/* Page shell shared by every mockup: navigation strip, faux app chrome, scale
 * and theme controls, the simulation transport, a "signals used" popover, and
 * a thread drawer any mockup can open with api.select(id). */
"use strict";
const Mockup = (() => {
  const MANIFEST = [
    ["01-card-refinements.html", "Card refinements", "conservative"],
    ["02-agent-roster.html", "Agent roster", "conservative"],
    ["03-needs-you-inbox.html", "Needs-you inbox", "moderate"],
    ["04-delegation-tree.html", "Delegation tree", "moderate"],
    ["05-territory-map.html", "Territory map", "moderate"],
    ["06-swimlane-timeline.html", "Swimlane timeline", "moderate"],
    ["07-plan-boards.html", "Plan boards", "radical"],
    ["08-written-briefing.html", "Written briefing", "radical"],
    ["09-mission-control.html", "Mission control", "radical"],
    ["10-focus-mode.html", "Focus mode", "radical"],
    ["11-settling-field.html", "Settling field", "round two"],
    ["12-constellations.html", "Constellations", "round two"],
    ["13-orbits.html", "Orbits", "round two"],
    ["14-vital-signs.html", "Vital signs", "round two"],
    ["15-river.html", "River", "round two"],
    ["16-radar.html", "Radar", "round two"],
    ["17-breathing-grid.html", "Breathing grid", "round two"],
    ["18-atlas.html", "Atlas", "synthesis"],
  ];
  const CURRENT_SIGNALS = {
    title: "checkpoint title (3–6 words, auto/agent/user source)", phase: "checkpoint phase (8 values)", summary: "checkpoint summary (1–2 sentences)",
    next_action: "checkpoint next_action text", next_actor: "checkpoint next_actor (user/agent/external/nobody)", links: "checkpoint links (PR, document, issue, bead, branch)",
    created_by: "created_by_thread_id + creation_mode=orchestration (parent/child)", client: "provider (Codex, Claude Code, Antigravity, Kimi)",
    status: "runtime state (working, idle, needs input, failed, done)", unread: "unread result / new checkpoint", stale: "checkpoint_stale (activity since the checkpoint)",
    write_scope: "begin-work write scope (file globs)", updated_at: "last activity time", original_request: "original request text", pinned: "pinned / Now / Later placement",
    classification: "reply classification (blocked, review, update, findings, done)", checkpoint_history: "checkpoint history entries",
    tool_activity: "latest tool calls in the turn (read, edit, run, search) from the provider stream; costs the model nothing", pending_approval: "a pending permission request from the provider, answerable with approve/deny",
    age: "time since the last tool call or update (runtime)", scope_cluster: "cluster key derived from the write scope when no domain is reported",
    messages: "conversation turns, and how many came from the user (runtime)",
  };
  const PROPOSED_SIGNALS = {
    name: "stable agent identity name, separate from the thread title", role: "role (orchestrator, implementer, reviewer, researcher, releaser, dispatcher)",
    kind: "kind (app conversation, provider-native subagent, terminal delegate)", ask: "structured ask: kind, question, options, free-text, blocking",
    blocks: "what an ask or wait blocks (impact, derived from waiting_on)", domain: "domain (web, backend, mobile, docs, infra, release, research)",
    target: "target page / service / module", plan: "plan steps with states (done, active, todo, blocked, delegated)", activity: "live one-line activity (what the agent is doing right now)",
    waiting_on: "dependency edges: other agents or an external thing", cost: "cost so far (tokens, dollars, minutes)", confidence: "self-reported confidence", eta: "expected remaining time",
    events: "event log (phase changes, asks, spawns, finishes) for timelines and digests",
  };
  const $ = (tag, cls, text) => AgentField.el(tag, cls, text);
  function params() { return new URLSearchParams(location.search); }
  function withParams(href, overrides = {}) {
    const q = params(); for (const [k, v] of Object.entries(overrides)) { if (v == null) q.delete(k); else q.set(k, v); }
    const s = q.toString(); return href + (s ? "?" + s : "");
  }
  function chrome() {
    const wrap = $("div", "app-chrome");
    const tabs = $("div", "app-tabs");
    for (const [name, count, current, dot] of [["agent-coord", 1, false, true], ["rig", 7, true, true], ["stoic", 0, false, false], ["projects", 19, false, true]]) {
      const t = $("span"); if (current) t.setAttribute("aria-current", "true"); t.append(name); if (count) t.append($("small", null, String(count))); if (dot) t.append($("i", "tab-dot")); tabs.append(t);
    }
    const tools = $("div", "app-tools");
    const search = $("div", "search"); search.append("⌕ Search conversations…", $("kbd", null, "/"));
    const seg = $("div", "segmented"); for (const [l, on] of [["Now", true], ["Later", false], ["Closed", false]]) { const s = $("span", null, l); s.setAttribute("aria-pressed", String(on)); seg.append(s); }
    tools.append(search, $("span", "btn quiet", "Filters ⌄"), seg, $("span", "spacer"), $("span", "btn", "Open dispatcher ↗"), $("span", "btn primary", "New session ＋"));
    wrap.append(tabs, tools); return wrap;
  }
  function mount({number, title, spectrum = "moderate", summary = "", signals = {current: [], proposed: []}, render, fullBleed = false, showChrome = true, defaultScale = "small", notes = "", domainScenarios = false}) {
    const q = params();
    const theme = q.get("theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const snapshot = globalThis.AGENT_COORD_SNAPSHOT;
    const scale = ["small", "medium", "large"].includes(q.get("scale")) || (q.get("scale") === "real" && snapshot) ? q.get("scale") : defaultScale;
    const workspace = q.get("workspace") || "all";
    const domains = domainScenarios && Object.hasOwn(AgentField.DOMAIN_SCENARIOS, q.get("domains") || "") ? q.get("domains") : "clean";
    document.documentElement.dataset.theme = theme; document.documentElement.style.colorScheme = theme;
    document.title = "Ribbon Field mockup " + String(number).padStart(2, "0") + " · " + title;
    let field = AgentField.create({scale, domains, workspace});
    const state = {selected: null, playing: false, timer: null, speed: 1200};
    const api = {get field() { return field; }, get selected() { return state.selected; }, get peeked() { return state.peeked; }, select, note, close: closeDrawer, peek, unpeek, scale: () => field.scale, rerender: () => draw()};

    const shell = $("header", "mock-shell");
    const idx = number - 1, prev = MANIFEST[(idx + MANIFEST.length - 1) % MANIFEST.length], next = MANIFEST[(idx + 1) % MANIFEST.length];
    shell.append($("span", "mock-id", String(number).padStart(2, "0") + " / " + MANIFEST.length), $("strong", "mock-title", title));
    const level = $("span", "spectrum", spectrum); level.dataset.level = spectrum; shell.append(level);
    if (summary) { const s = $("span", "mock-summary", summary); s.title = summary; shell.append(s); }
    const sig = $("details"); const sum = $("summary", null, "Signals used"); sig.append(sum);
    const pop = $("div", "signals");
    const section = (heading, keys, dict, cls) => { if (!keys?.length) return; pop.append($("h4", null, heading)); const p = $("div"); for (const k of keys) { const c = $("code", cls, k); c.title = dict[k] || k; p.append(c); } pop.append(p); };
    section("Available today", signals.current, CURRENT_SIGNALS, "");
    section("Proposed checkpoint additions", signals.proposed, PROPOSED_SIGNALS, "proposed");
    if (notes) pop.append($("p", null, notes));
    sig.append(pop); shell.append(sig);
    shell.append($("span", "mock-spacer"));
    const clock = $("span", "mock-id"); shell.append(clock);
    const scaleOptions = [["small", "16 agents"], ["medium", "≈45 agents"], ["large", "≈160 agents"]];
    if (snapshot?.threads?.length) scaleOptions.push(["real", "Real snapshot · " + snapshot.threads.length + " threads"]);
    const scaleSel = $("select"); for (const [v, l] of scaleOptions) { const o = $("option", null, l); o.value = v; o.selected = v === scale; scaleSel.append(o); }
    let workspaceSel = null;
    if (scale === "real" && snapshot) {
      workspaceSel = $("select"); const all = $("option", null, "All workspaces · " + snapshot.threads.length); all.value = "all"; all.selected = workspace === "all"; workspaceSel.append(all);
      for (const w of snapshot.workspaces || []) { const o = $("option", null, w.cwd.replace(/^\/opt\/projects\//, "") + " · " + w.count); o.value = w.cwd; o.selected = w.cwd === workspace; workspaceSel.append(o); }
      workspaceSel.title = "Workspace"; workspaceSel.onchange = () => { location.href = withParams(location.pathname.split("/").pop(), {workspace: workspaceSel.value === "all" ? null : workspaceSel.value}); };
    }
    scaleSel.title = "Data source"; scaleSel.onchange = () => { location.href = withParams(location.pathname.split("/").pop(), {scale: scaleSel.value, workspace: null}); };
    let domainSel = null;
    if (domainScenarios && scale !== "real") {
      domainSel = $("select"); for (const [v, l] of Object.entries(AgentField.DOMAIN_SCENARIOS)) { const o = $("option", null, l); o.value = v; o.selected = v === domains; domainSel.append(o); }
      domainSel.title = "How well the model reports domains"; domainSel.onchange = () => { location.href = withParams(location.pathname.split("/").pop(), {domains: domainSel.value === "clean" ? null : domainSel.value}); };
    }
    const themeBtn = $("button", "btn", theme === "dark" ? "Light" : "Dark"); themeBtn.type = "button"; themeBtn.title = "Toggle theme";
    themeBtn.onclick = () => { location.href = withParams(location.pathname.split("/").pop(), {theme: theme === "dark" ? "light" : "dark"}); };
    const play = $("button", "btn", "▶ Play"); play.type = "button"; play.title = "Advance simulated time (1 min per step)";
    const step = $("button", "btn", "Step"); step.type = "button";
    const reset = $("button", "btn", "Reset"); reset.type = "button";
    const transport = $("span"); transport.style.display = "inline-flex"; transport.style.gap = "4px"; transport.append(play, step, reset);
    const nav = $("nav");
    const link = (href, label, t) => { const a = $("a", "btn", label); a.href = withParams(href); a.title = t; return a; };
    nav.append(link("../index.html", "Index", "All mockups"), link(prev[0], "←", prev[1]), link(next[0], "→", next[1]));
    shell.append(scaleSel); if (workspaceSel) shell.append(workspaceSel); if (domainSel) shell.append(domainSel); shell.append(themeBtn, transport, nav);
    const noteLine = $("p", "mock-note"); shell.append(noteLine);
    document.body.prepend(shell);
    if (showChrome) shell.after(chrome());
    const stage = document.getElementById("stage") || (() => { const s = $("main"); s.id = "stage"; document.body.append(s); return s; })();
    if (fullBleed) stage.classList.add("full-bleed");

    function note(text) { noteLine.textContent = text; noteLine.hidden = !text; }
    note(notes ? "" : ""); noteLine.hidden = true;
    function tickClock() { const d = new Date(field.now); clock.textContent = (field.scale === "real" ? d.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) + " · snapshot" : d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) + " · simulated") + (field.tickCount ? " +" + field.tickCount + "m" : ""); }
    function setPlaying(on) {
      state.playing = on; play.textContent = on ? "❚❚ Pause" : "▶ Play"; play.setAttribute("aria-pressed", String(on));
      clearInterval(state.timer); if (on) state.timer = setInterval(() => field.tick(), state.speed);
    }
    play.onclick = () => setPlaying(!state.playing);
    step.onclick = () => field.tick();
    reset.onclick = () => { setPlaying(false); field = AgentField.create({scale, domains, workspace}); field.subscribe(draw); state.selected = null; closeDrawer(); unpeek(); draw(); };
    document.addEventListener("keydown", e => {
      if (e.target?.matches?.("input, textarea, select, [contenteditable]")) return;
      if (e.key === "[") location.href = withParams(prev[0]); else if (e.key === "]") location.href = withParams(next[0]);
      else if (e.key === "Escape") { if (lens) unpeek(); else closeDrawer(); } else if (e.key === "." ) field.tick();
    });

    /* Lens: a compact peek anchored to a mark. Now, next, recent steps, sub-agents, and the ask, answerable in place. */
    let lens = null, lensAnchor = null, lensPos = null;
    function unpeek() { lens?.remove(); lens = null; lensAnchor = null; lensPos = null; state.peeked = null; }
    function peek(id, anchor, position) {
      if (state.peeked === id && lens) { unpeek(); return; }
      state.peeked = id; lensAnchor = anchor || null; lensPos = position || null; drawLens();
    }
    function placeLens() {
      if (!lens) return;
      const margin = 12, gap = 10, w = lens.offsetWidth, h = lens.offsetHeight;
      let left, top;
      if (lensAnchor?.isConnected && lensAnchor.getBoundingClientRect) {
        const r = lensAnchor.getBoundingClientRect(); left = r.right + gap; top = r.top - 8;
        if (left + w > innerWidth - margin) left = r.left - w - gap;
        if (left < margin) { left = Math.min(Math.max(margin, r.left), innerWidth - w - margin); top = r.bottom + gap; }
      } else if (lensPos) { left = lensPos.x + gap; top = lensPos.y - 8; if (left + w > innerWidth - margin) left = lensPos.x - w - gap; }
      else { left = innerWidth / 2 - w / 2; top = innerHeight / 3; }
      lens.style.left = Math.max(margin, Math.min(left, innerWidth - w - margin)) + "px";
      lens.style.top = Math.max(margin, Math.min(top, innerHeight - h - margin)) + "px";
    }
    function drawLens() {
      const a = state.peeked && field.byId(state.peeked);
      if (!a) { unpeek(); return; }
      if (!lens) { lens = $("aside", "lens"); lens.setAttribute("role", "dialog"); lens.setAttribute("aria-label", "Agent peek"); document.body.append(lens); }
      lens.replaceChildren();
      const head = $("div", "lens-head");
      head.append(AgentField.statusDot(a), AgentField.providerLogo(a.client, AgentField.PROVIDER_NAMES[a.client]));
      const t = $("div", "lens-title"); if (a.name) t.append($("b", null, a.name + " · ")); t.append(a.title); head.append(t);
      const close = $("button", "btn quiet small", "×"); close.type = "button"; close.setAttribute("aria-label", "Close"); close.onclick = unpeek; head.append(close);
      lens.append(head);
      const meta = $("div", "lens-meta"); meta.append($("span", null, field.statusLabel(a)), AgentField.phaseSteps(a), $("span", null, AgentField.PHASE_LABELS[a.phase]));
      if (a.parentId && field.byId(a.parentId)) { const p = $("button", "btn quiet small", "↑ " + (field.byId(a.parentId).name || field.byId(a.parentId).title)); p.type = "button"; p.title = "Created by " + field.byId(a.parentId).title; p.onclick = () => peek(a.parentId, lensAnchor, lensPos); meta.append(p); }
      lens.append(meta);
      if (a.ask) lens.append(askBox(a, field, null));
      const now = $("div", "lens-row"); now.append($("b", null, "Now"));
      const nowText = a.status === "working" ? (a.activity || field.toolLabel(a)) : a.status === "done" ? "Finished " + field.relativeTime(a.updatedAt) : a.status === "blocked" ? field.statusLabel(a) : a.status === "waiting" || a.status === "failed" ? "Paused until you answer" : "Idle since " + field.relativeTime(field.lastActivity(a));
      now.append($("span", null, nowText)); lens.append(now);
      if (a.status === "working" && field.toolLabel(a)) { const tool = $("div", "lens-row lens-tool"); tool.append($("b", null, "Tool"), $("span", "mono", field.toolLabel(a) + " · " + field.relativeTime(a.lastTool.at))); lens.append(tool); }
      if (a.turns != null) { const msgs = $("div", "lens-row"); msgs.append($("b", null, "Msgs"), $("span", "muted", a.turns + " messages" + (a.userTurns ? " · " + a.userTurns + " from you" : ""))); lens.append(msgs); }
      if (!a.ask && a.next?.action && a.status !== "done") { const nx = $("div", "lens-row"); nx.append($("b", null, {user: "You", agent: "Next", external: "Waits"}[a.next.actor] || "Next"), $("span", null, a.next.action)); lens.append(nx); }
      const steps = field.recentSteps(a);
      if (steps.length) { const ul = $("ul", "lens-steps"); for (const s of steps) ul.append($("li", s.state, s.label)); lens.append(ul); }
      const kids = field.children(a.id);
      if (kids.length) {
        const row = $("div", "lens-kids");
        const open = state.lensKidsOpen ||= new Set();
        const toggle = $("button", "btn quiet small", (open.has(a.id) ? "▾ " : "▸ ") + kids.length + (kids.length === 1 ? " agent" : " agents") + (field.descendantsNeedingUser(a.id).length ? " · " + field.descendantsNeedingUser(a.id).length + " need you" : ""));
        toggle.type = "button"; toggle.onclick = () => { open.has(a.id) ? open.delete(a.id) : open.add(a.id); drawLens(); };
        row.append(toggle);
        const dots = $("span", "lens-dots"); for (const k of kids) { const d = AgentField.statusDot(k); d.title = k.title + " · " + field.statusLabel(k); dots.append(d); } row.append(dots);
        lens.append(row);
        if (open.has(a.id)) { const list = $("ul", "lens-kid-list"); for (const k of kids) { const li = $("li"); const b = $("button", "btn quiet small"); b.type = "button"; b.append(AgentField.statusDot(k), " ", k.title); b.onclick = () => peek(k.id, lensAnchor, lensPos); li.append(b, $("span", "muted", k.status === "working" ? (k.activity || field.toolLabel(k) || "Working") : k.status === "done" ? "Done · " + (field.recentSteps(k)[0]?.label || "") : field.statusLabel(k))); list.append(li); } lens.append(list); }
      }
      const foot = $("div", "lens-foot"); foot.append($("span", "muted mono", field.relativeTime(field.lastActivity(a))));
      const openBtn = $("button", "btn quiet small", "Open thread"); openBtn.type = "button"; openBtn.onclick = () => { unpeek(); select(a.id); }; foot.append(openBtn);
      lens.append(foot);
      placeLens();
    }
    document.addEventListener("pointerdown", e => { if (lens && !lens.contains(e.target) && !(lensAnchor?.contains?.(e.target))) unpeek(); }, true);
    window.addEventListener("resize", placeLens);

    /* Thread drawer */
    let drawer = null;
    function closeDrawer() { drawer?.remove(); drawer = null; state.selected = null; }
    function select(id) { state.selected = id; drawDrawer(); }
    function drawDrawer() {
      const a = state.selected && field.byId(state.selected);
      if (!a) { closeDrawer(); return; }
      if (!drawer) { drawer = $("aside", "drawer"); drawer.setAttribute("aria-label", "Thread"); document.body.append(drawer); }
      drawer.replaceChildren();
      const close = $("button", "btn quiet drawer-close", "×"); close.type = "button"; close.setAttribute("aria-label", "Close"); close.onclick = closeDrawer; drawer.append(close);
      const head = $("div", "drawer-head"); head.append($("h2", null, a.title)); drawer.append(head);
      const ident = $("div", "drawer-identity");
      ident.append(AgentField.providerLogo(a.client, AgentField.PROVIDER_NAMES[a.client]));
      if (a.name) ident.append($("span", "chip", a.name));
      ident.append($("span", "chip", AgentField.ROLE_LABELS[a.role]));
      if (a.kind !== "app") ident.append($("span", "chip mono", a.kind));
      ident.append($("span", "chip mono", AgentField.DOMAIN_LABELS[a.domain] + (a.target ? " · " + a.target : "")));
      drawer.append(ident);
      const st = $("div"); st.style.display = "flex"; st.style.alignItems = "center"; st.style.gap = "8px";
      st.append(AgentField.statusDot(a), $("span", null, field.statusLabel(a)), AgentField.phaseSteps(a), $("span", "muted", AgentField.PHASE_LABELS[a.phase]), $("span", "muted mono", field.relativeTime(a.updatedAt)));
      drawer.append(st);
      if (a.parentId && field.byId(a.parentId)) { const p = $("button", "btn quiet small", "↑ Created by " + field.byId(a.parentId).title); p.type = "button"; p.onclick = () => select(a.parentId); drawer.append(p); }
      if (a.ask) drawer.append(askBox(a, field, select));
      drawer.append($("h3", null, "Where things stand"), $("p", null, a.summary));
      if (a.stale) drawer.append($("p", "muted", "New activity since this checkpoint."));
      if (a.activity) drawer.append($("h3", null, "Right now"), $("p", null, a.activity));
      if (a.next?.action && !a.ask) drawer.append($("h3", null, {user: "Your next step", agent: "Agent’s next step", external: "Waiting on"}[a.next.actor] || "Next"), $("p", null, a.next.action));
      const waits = field.agentWaits(a), ext = field.externalWaits(a), blocks = field.blocks(a.id);
      if (waits.length || ext.length) { drawer.append($("h3", null, "Waiting on")); const ul = $("div", "kids"); for (const w of waits) { const b = $("button", "btn quiet small", w.title + " · " + field.statusLabel(w)); b.type = "button"; b.onclick = () => select(w.id); ul.append(b); } for (const e of ext) ul.append($("span", "muted", e)); drawer.append(ul); }
      if (blocks.length) { drawer.append($("h3", null, "Blocks")); const ul = $("div", "kids"); for (const id of blocks) { const b = field.byId(id); const btn = $("button", "btn quiet small", b.title); btn.type = "button"; btn.onclick = () => select(id); ul.append(btn); } drawer.append(ul); }
      if (a.plan.length) { drawer.append($("h3", null, "Plan · " + field.progress(a).done + " of " + a.plan.length)); const ul = $("ul", "plan"); for (const s of a.plan) { const li = $("li", s.state, s.label); if (s.agentId && field.byId(s.agentId)) { const b = $("button", "btn quiet small", "→ " + field.byId(s.agentId).title); b.type = "button"; b.onclick = () => select(s.agentId); li.append(b); } ul.append(li); } drawer.append(ul); }
      const kids = field.children(a.id);
      if (kids.length) { drawer.append($("h3", null, "Agents · " + kids.length)); const ul = $("div", "kids"); for (const k of kids) { const b = $("button", "btn quiet small"); b.type = "button"; b.append(AgentField.statusDot(k), " ", k.title, " · ", field.statusLabel(k)); b.onclick = () => select(k.id); ul.append(b); } drawer.append(ul); }
      if (a.scope.length) { drawer.append($("h3", null, "Scope")); const sc = $("div", "scope"); for (const s of a.scope) sc.append($("code", null, s)); drawer.append(sc); }
      if (a.links.length) { drawer.append($("h3", null, "Links")); const ul = $("div", "kids"); for (const l of a.links) ul.append($("span", null, l.kind.replaceAll("_", " ") + " · " + l.label + " · " + l.target)); drawer.append(ul); }
      drawer.append($("h3", null, a.cost.tokens ? "Cost" : "Run"), $("p", "muted", a.cost.tokens ? field.duration(a.cost.minutes * 60000) + " · " + Math.round(a.cost.tokens / 1000) + "k tokens · $" + a.cost.usd.toFixed(2) + " · confidence " + Math.round(a.confidence * 100) + "%" : field.duration(a.cost.minutes * 60000) + " since created · " + (a.turns || 0) + " turns" + (a.workspace ? " · " + a.workspace : "")));
      drawer.append($("h3", null, "Checkpoint history · " + a.history.length)); const h = $("div", "history");
      for (const e of [...a.history].reverse()) { const art = $("article"); art.append($("small", null, (e.actor === "user" ? "You · " : "") + AgentField.PHASE_LABELS[e.phase] + " · " + field.relativeTime(e.at)), $("span", null, e.summary)); h.append(art); }
      drawer.append(h);
    }
    function draw() { tickClock(); render(field, stage, api); if (state.selected) drawDrawer(); if (state.peeked) drawLens(); }
    field.subscribe(draw);
    draw();
    return api;
  }
  /* A reusable structured-ask block with inline answers. */
  function askBox(agent, field, onOpen) {
    const box = $("div", "ask");
    box.append($("span", "ask-kind", (agent.ask.kind === "failure" ? "failed · " : "") + agent.ask.kind + (agent.ask.blocking ? " · blocking" : "")));
    box.append($("div", "ask-question", agent.ask.question));
    const impact = field.impact(agent.id);
    const meta = [];
    if (impact.length) meta.push("Blocks " + impact.length + (impact.length === 1 ? " agent" : " agents") + ": " + impact.map(id => field.byId(id)?.title).filter(Boolean).slice(0, 2).join(", ") + (impact.length > 2 ? "…" : ""));
    meta.push("Asked " + field.relativeTime(agent.ask.askedAt));
    box.append($("span", "ask-impact", meta.join(" · ")));
    if (agent.ask.options.length) {
      const opts = $("div", "ask-options");
      agent.ask.options.forEach((o, i) => { const b = $("button", "btn small" + (i === 0 ? " primary" : ""), o.label); b.type = "button"; b.onclick = e => { e.stopPropagation(); field.answer(agent.id, o.id); }; opts.append(b); });
      box.append(opts);
    }
    if (agent.ask.freeText) {
      const row = $("div", "ask-answer"); const input = $("input"); input.placeholder = "Type an answer…"; input.onclick = e => e.stopPropagation();
      const send = $("button", "btn small primary", "Answer"); send.type = "button"; send.onclick = e => { e.stopPropagation(); if (input.value.trim()) field.answer(agent.id, input.value.trim()); };
      input.onkeydown = e => { if (e.key === "Enter") send.click(); };
      row.append(input, send); box.append(row);
    }
    if (onOpen) { const open = $("button", "btn quiet small", "Open thread"); open.type = "button"; open.onclick = e => { e.stopPropagation(); onOpen(agent.id); }; box.append(open); }
    return box;
  }
  /* Drag helper for direct manipulation. onMove gets client-space deltas; onEnd says whether it was a drag or a click. */
  function drag(element, {onStart, onMove, onEnd, threshold = 4} = {}) {
    let start = null, moved = false, last = null;
    element.addEventListener("pointerdown", e => {
      if (e.button !== 0) return;
      start = {x: e.clientX, y: e.clientY}; last = start; moved = false;
      element.setPointerCapture?.(e.pointerId);
    });
    element.addEventListener("pointermove", e => {
      if (!start) return;
      const dx = e.clientX - start.x, dy = e.clientY - start.y;
      if (!moved && Math.hypot(dx, dy) < threshold) return;
      if (!moved) { moved = true; element.classList.add("dragging"); onStart?.(e); }
      onMove?.({dx, dy, mx: e.clientX - last.x, my: e.clientY - last.y, event: e}); last = {x: e.clientX, y: e.clientY};
    });
    const finish = e => { if (!start) return; const was = moved; start = null; moved = false; element.classList.remove("dragging"); onEnd?.({dragged: was, event: e}); };
    element.addEventListener("pointerup", finish); element.addEventListener("pointercancel", finish);
    return () => { start = null; };
  }
  return {mount, askBox, drag, MANIFEST, CURRENT_SIGNALS, PROPOSED_SIGNALS, withParams};
})();
