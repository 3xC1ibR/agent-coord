"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const Tree = require("../plugins/agent-coord/scripts/agent_coord/web/agent-tree.js");
const thread = (id, parent, extra = {}) => ({thread_id: id, title: id, browser_session: true, created_at: 100,
  attention: "now", status: "idle", response_state: "available", work_phase: "planning",
  ...(parent ? {creation_mode: "orchestration", created_by_thread_id: parent} : {}), ...extra});

test("the tree spans state, placement and group boundaries without inventing dispatch or fork edges", () => {
  const records = [thread("root"), thread("child", "root", {attention: "later", project_id: "different"}),
    thread("closed", "child", {attention: "archived"}), thread("dispatch", "root", {creation_mode: null}),
    thread("fork", null, {forked_from: {thread_id: "root"}}), thread("unrelated", null, {project_id: "different"})];
  const tree = Tree.graph(records);
  assert.deepEqual(tree.family("root"), ["root", "child", "closed"]);
  assert.equal(tree.root("closed"), "root");
  assert.equal(tree.root("dispatch"), "dispatch");
  assert.equal(tree.root("fork"), "fork");
});
test("missing parents, self links, and historical cycles cannot trap tree navigation", () => {
  const tree = Tree.graph([thread("a", "b"), thread("b", "a"), thread("c", "b"),
    thread("missing", "unavailable"), thread("self", "self")]);
  assert.deepEqual(tree.family(tree.root("c")).sort(), ["a", "b", "c"]);
  assert.equal(tree.root("missing"), "missing");
  assert.equal(tree.root("self"), "self");
});
test("full cards have non-overlapping positions, first-level columns and connected deeper descendants", () => {
  const records = [thread("root"), thread("research", "root"), thread("writer", "root"), thread("coder", "root"),
    thread("test1", "coder"), thread("test2", "coder"), thread("nested", "test1")];
  const tree = Tree.graph(records), layout = Tree.layout(tree, "root");
  assert.equal(layout.nodes.length, records.length); assert.equal(layout.edges.length, records.length - 1);
  for (const a of layout.nodes) for (const b of layout.nodes) if (a !== b) {
    assert.ok(a.x + Tree.CARD_W <= b.x || b.x + Tree.CARD_W <= a.x || a.y + Tree.CARD_H <= b.y || b.y + Tree.CARD_H <= a.y,
      a.id + " overlaps " + b.id);
  }
  for (const edge of layout.edges) assert.ok(!/NaN|undefined/.test(edge.path));
  const after = Tree.layout(Tree.graph(records.map(t => ({...t, response_state: "working"}))), "root");
  assert.deepEqual(after, layout, "status changes preserve geometry");
  const collapsed = Tree.layout(tree, "root", new Set(["coder"]));
  assert.deepEqual(collapsed.nodes.map(n => n.id).sort(), ["coder", "research", "root", "writer"]);
});
test("current running state supersedes a historical failure; stale asks do not require intervention", () => {
  assert.equal(Tree.status(thread("running", null, {response_state: "working", turn_completion: {status: "failed"}})).key, "running");
  assert.equal(Tree.status(thread("ask", null, {checkpoint: {next_actor: "user", next_action: "Approve"}})).key, "waiting");
  assert.equal(Tree.status(thread("stale", null, {checkpoint_stale: true, checkpoint: {next_actor: "user", next_action: "Approve"}})).key, "idle");
  assert.equal(Tree.duration(thread("idle")), "");
});

function setup() {
  const doc = {activeElement: null};
  class Element {
    constructor(tag) { Object.assign(this, {tagName: tag, children: [], dataset: {}, attrs: {}, events: {}, style: {}, className: "",
      clientWidth: 900, clientHeight: 650}); }
    get classList() { const read = () => new Set(this.className.split(/\s+/).filter(Boolean)); return {
      contains: n => read().has(n),
      add: n => { const s = read(); s.add(n); this.className = [...s].join(" "); },
      remove: n => { const s = read(); s.delete(n); this.className = [...s].join(" "); },
      toggle: (n, value) => { const s = read(); if (value ?? !s.has(n)) s.add(n); else s.delete(n); this.className = [...s].join(" "); }}; }
    append(...nodes) { for (const n of nodes) { n.remove?.(); this.children.push(n); n.parent = this; } }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(n => n !== this); this.parent = null; }
    replaceChildren(...nodes) { for (const n of this.children) n.parent = null; this.children = []; this.append(...nodes); }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    removeAttribute(k) { delete this.attrs[k]; }
    matches(s) {
      if (s.includes(",")) return s.split(",").some(x => this.matches(x.trim()));
      if (s.startsWith(".")) return this.classList.contains(s.slice(1));
      if (s.startsWith("[role=")) return this.attrs.role === "treeitem";
      return this.tagName === s;
    }
    closest(s) { return this.matches(s) ? this : this.parent?.closest(s); }
    querySelector(s) { for (const n of this.children) { if (n.matches(s)) return n; const found = n.querySelector(s); if (found) return found; } return null; }
    contains(n) { return this === n || this.children.some(c => c.contains(n)); }
    addEventListener(k, f) { this.events[k] = f; }
    focus() { doc.activeElement = this; }
  }
  doc.createElement = tag => new Element(tag); doc.createElementNS = (_, tag) => new Element(tag);
  const timers = new Map(); let timerId = 0;
  const window = {setTimeout(fn) { timers.set(++timerId, fn); return timerId; }, clearTimeout(id) { timers.delete(id); }};
  const selected = [], panel = new Tree.Panel({document: doc, window, host: doc.createElement("div"), onSelect: id => selected.push(id)});
  return {panel, selected, doc, timers, window};
}
test("selection updates reuse card elements and camera; folding and keyboard navigation preserve the full root", () => {
  const {panel, selected, doc} = setup();
  const records = [thread("root"), thread("child", "root"), thread("grandchild", "child")];
  panel.update(records, "root", "root");
  const child = panel.cardEls.get("child");
  panel.zoom(1.25); const camera = {...panel.view};
  child.onclick({target: child}); assert.deepEqual(selected, ["child"]);
  panel.update(records, "root", "child");
  assert.equal(panel.root, "root"); assert.equal(panel.cardEls.get("child"), child);
  assert.equal(child.attrs["aria-selected"], "true"); assert.equal(panel.view.x, camera.x);
  panel.fold("child"); assert.equal(panel.cardEls.has("grandchild"), false);
  panel.update(records, "root", "child"); assert.equal(panel.cardEls.has("grandchild"), false);
  let prevented = false;
  panel.keydown({key: "ArrowRight", target: child, preventDefault() { prevented = true; }});
  assert.equal(prevented, true); assert.equal(panel.cardEls.has("grandchild"), true);
  panel.keydown({key: "ArrowRight", target: child, preventDefault() {}});
  assert.equal(doc.activeElement.dataset.agentId, "grandchild");
  panel.keydown({key: "Enter", target: doc.activeElement, preventDefault() {}});
  assert.equal(selected.at(-1), "grandchild");
});
test("attention control reveals a folded failed descendant before selecting it", () => {
  const {panel, selected} = setup();
  const records = [thread("root"), thread("child", "root"), thread("failed", "child", {response_state: "failed"})];
  panel.update(records, "root", "root"); panel.fold("child"); panel.findAttention();
  assert.equal(panel.cardEls.has("failed"), true); assert.equal(selected.at(-1), "failed");
  assert.equal(panel.root, "root");
});

test("live messages travel in both directions and between siblings without changing selection or camera", () => {
  const {panel, timers} = setup();
  panel.update([thread("root"), thread("left", "root"), thread("right", "root")], "root", "left");
  panel.zoom(1.25); const camera = {...panel.view};
  panel.message({messageId: 1, threadId: "root", recipientId: "left"});
  const route = panel.messages.get(1).group.children[0].attrs.d;
  assert.match(route, /^M[\d.]+,180 C/); // Starts at the parent's bottom edge.
  panel.message({messageId: 2, threadId: "left", recipientId: "root"});
  assert.match(panel.messages.get(2).group.children[0].attrs.d, /^M[\d.]+,244 C/); // Child's top edge.
  panel.message({messageId: 3, threadId: "left", recipientId: "right"});
  assert.match(panel.messages.get(3).group.children[0].attrs.d, /^M260,320 C/);
  assert.equal(panel.messageStatus.textContent, "left → right: message sent");
  assert.equal(panel.selected, "left"); assert.deepEqual(panel.view, camera);
  panel.message({messageId: 3, threadId: "left", recipientId: "right"});
  assert.equal(panel.messages.size, 3);
  for (const finish of [...timers.values()]) finish();
  assert.equal(panel.messages.size, 0); assert.equal(panel.messageSvg.children.length, 0); assert.equal(timers.size, 0);
});

test("collapsed branches receive messages at the visible ancestor and unrelated families do not animate", () => {
  const {panel, timers} = setup();
  panel.update([thread("root"), thread("left", "root"), thread("leaf", "left"), thread("right", "root"), thread("other")], "root", "root");
  panel.fold("left");
  const camera = {...panel.view};
  panel.message({messageId: 1, threadId: "leaf", recipientId: "right"});
  assert.equal(panel.messages.size, 1); assert.equal(panel.cardEls.has("leaf"), false);
  assert.deepEqual(panel.view, camera);
  panel.message({messageId: 2, threadId: "leaf", recipientId: "left"});
  panel.message({messageId: 3, threadId: "root", recipientId: "other"});
  panel.message({messageId: 4, threadId: "missing", recipientId: "right"});
  assert.equal(panel.messages.size, 1);
  panel.fold("left"); // Layout changes invalidate paths and release timers.
  assert.equal(panel.messages.size, 0); assert.equal(timers.size, 0);
});

test("reduced motion shows a static directional cue and bursts keep a bounded overlay", () => {
  const {panel, window, timers} = setup();
  window.matchMedia = () => ({matches: true});
  panel.update([thread("root"), thread("child", "root")], "root", "root");
  for (let messageId = 1; messageId <= 30; messageId++) panel.message({messageId, threadId: "root", recipientId: "child"});
  assert.equal(panel.messages.size, 16); assert.equal(timers.size, 16);
  assert.equal(panel.messageSvg.children.length, 16);
  assert.equal(panel.messages.get(30).group.children.some(el => el.tagName === "circle"), false);
  panel.clearMessages(); assert.equal(timers.size, 0); assert.equal(panel.messageSvg.children.length, 0);
});

function setupController(records, selected = "root", response, {compact = true} = {}) {
  const fs = require("node:fs"), vm = require("node:vm");
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const elements = new Map(), storage = new Map();
  const c = {state: {selected, sessions: records}, errors: [], requests: [], storage,
    compactContext: {matches: compact, addEventListener() {}},
    document: {activeElement: null}, window: {}, localStorage: {getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, String(v))},
    $: id => {
      if (!elements.has(id)) {
        const classes = new Set(), props = new Map(), attrs = new Map();
        elements.set(id, {hidden: false, open: false, classes, props, attrs,
          classList: {
            add: n => classes.add(n),
            toggle: (n, v) => { if (v ?? !classes.has(n)) classes.add(n); else classes.delete(n); },
            remove: n => classes.delete(n),
            contains: n => classes.has(n),
          },
          style: {setProperty: (k, v) => props.set(k, v), getPropertyValue: k => props.get(k)},
          setAttribute: (k, v) => attrs.set(k, String(v)),
          getAttribute: k => attrs.get(k),
          addEventListener() {},
          contains: () => false,
          setPointerCapture() {},
          getBoundingClientRect: () => ({left: 0, right: 1200, width: 1200}),
          focus() {}});
      }
      return elements.get(id);
    },
    get collapsed() { return c.$("context-panel").hidden; },
    AgentTree: {...Tree, Panel: class { update() {} fit() {} clearMessages() {} message(params) { c.messages.push(params); } }}, messages: [],
    isSessionDraft: () => c.state.selected === "new-session", threadPath: id => "threads/" + id,
    renderStatus() {}, renderList() {}, showError: error => c.errors.push(error), action: fn => fn(),
    api: path => {
      c.requests.push(path);
      if (path.includes("new-session")) return Promise.reject(new Error("Work thread not found."));
      const loaded = response ? response(path) : Promise.resolve({root_thread_id: Tree.graph(records).root(selected), data: records});
      return loaded.then(result => {
        const tree = Tree.graph(result.data);
        return {...result, data: result.data.map(t => ({...t, agent_parent_id: tree.parents.get(t.thread_id) || null}))};
      });
    }};
  c.state.threadTabs = {selected, items: [{thread_id: selected}],
    setTree(root, open) { const item = this.items.find(t => t.thread_id === this.selected);
      if (item) Object.assign(item, {tree_root: root, tree_open: open}); }};
  vm.runInNewContext(source.slice(source.indexOf("function setupAgentTree()"), source.indexOf("\nif (typeof AgentTree !== \"undefined\") setupAgentTree();")) + "\nsetupAgentTree();", c);
  c.controller = c.state.agentTreeController;
  c.controller.inventory(records);
  return c;
}

test("only a visible Agents panel handles live send events", async () => {
  const c = setupController([thread("root"), thread("child", "root")]);
  const send = {messageId: 1, threadId: "root", recipientId: "child"};
  c.controller.message(send); assert.equal(c.messages.length, 0);
  c.controller.bind(); await c.controller.load();
  c.controller.message(send); assert.equal(c.messages.length, 1);
  c.document.hidden = true; c.controller.message(send); assert.equal(c.messages.length, 1);
  c.document.hidden = false; c.controller.setMode("context");
  c.controller.message(send); assert.equal(c.messages.length, 1);
});

test("opening either a parent or leaf conversation defaults to its full tree and opens the panel", async () => {
  const records = [thread("root"), thread("child", "root")];
  for (const selected of ["root", "child"]) {
    const c = setupController(records, selected);
    c.controller.bind(); await c.controller.load();
    assert.equal(c.$("panel-agents").hidden, false);
    assert.equal(c.controller.mode, "agents");
    assert.equal(c.controller.root, "root");
    assert.equal(c.collapsed, false);
    assert.equal(c.state.threadTabs.items[0].tree_open, true);
  }
});

test("family discovery includes an archived ancestor missing from overview inventory", async () => {
  const child = thread("child", "root");
  const c = setupController([child], "child", () => Promise.resolve({root_thread_id: "root",
    data: [thread("root", null, {attention: "archived"}), child]}));
  c.controller.bind(); await c.controller.load();
  assert.equal(c.$("panel-agents").hidden, false);
  assert.equal(c.controller.mode, "agents");
  assert.equal(c.controller.root, "root");
  assert.equal(c.collapsed, false);
});

test("a standalone conversation keeps Context without forcing the panel open", async () => {
  const c = setupController([thread("root")]);
  c.controller.bind(); await c.controller.load();
  assert.equal(c.$("panel-agents").hidden, true);
  assert.equal(c.controller.mode, "context");
  assert.equal(c.collapsed, true);
  assert.equal(c.state.threadTabs.items[0].tree_root, undefined);
  await c.controller.open("root");
  assert.equal(c.$("panel-agents").hidden, true);
  assert.equal(c.controller.mode, "context");
  let focused = 0, clicked = 0;
  c.$("panel-agents").focus = () => { focused++; };
  c.$("panel-agents").click = () => { clicked++; };
  c.$("panel-context").onkeydown({key: "ArrowLeft", preventDefault() {}});
  assert.equal(focused, 0);
  assert.equal(clicked, 0);
});

test("switching conversations and changing child lists hides or restores the Agents tab without leaving a hidden tab selected", async () => {
  const parent = thread("root"), child = thread("child", "root"), solo = thread("solo");
  let family = [parent, child];
  const c = setupController([parent, child, solo], "root", path => Promise.resolve(
    path.includes("solo") ? {root_thread_id: "solo", data: [solo]} : {root_thread_id: "root", data: family}));
  c.controller.bind(); await c.controller.load();
  assert.equal(c.$("panel-agents").hidden, false);
  assert.equal(c.controller.mode, "agents");

  // Switching to a standalone conversation hides Agents and selects Context, even with preservePanel.
  c.state.selected = "solo"; c.state.threadTabs.selected = "solo"; c.state.threadTabs.items.push({thread_id: "solo"});
  c.controller.bind({preservePanel: true}); await c.controller.load();
  assert.equal(c.$("panel-agents").hidden, true);
  assert.equal(c.controller.mode, "context");

  // Switching back to a conversation with app agents restores the Agents tab.
  c.state.selected = "root"; c.state.threadTabs.selected = "root";
  c.controller.bind(); await c.controller.load();
  assert.equal(c.$("panel-agents").hidden, false);
  assert.equal(c.controller.mode, "agents");

  // If the child list changes so no app agents remain, Agents hides and cannot remain selected.
  family = [parent];
  c.controller.inventory([parent, solo], []);
  await c.controller.load();
  assert.equal(c.$("panel-agents").hidden, true);
  assert.equal(c.controller.mode, "context");

  // When a child app agent appears, the Agents tab is restored.
  family = [parent, child];
  c.controller.inventory([parent, child, solo], []);
  assert.equal(c.$("panel-agents").hidden, false);
});

test("late family discovery respects explicit Context and close choices", async () => {
  for (const choice of ["panel-context", "close-context-panel"]) {
    let resolve;
    const c = setupController([thread("root")], "root", () => new Promise(yes => { resolve = yes; }));
    c.controller.bind(); const loaded = c.controller.load();
    c.$(choice).onclick();
    resolve({root_thread_id: "root", data: [thread("root"), thread("child", "root")]});
    await loaded;
    assert.equal(c.$("panel-agents").hidden, false);
    assert.equal(c.controller.mode, "context");
    assert.equal(c.collapsed, true);
  }
});

test("same-tab child navigation keeps panel choices and does not reopen the mobile sheet", async () => {
  const c = setupController([thread("root"), thread("child", "root")]);
  let opened = 0;
  c.state.mobileChat = {matches: true, open() { opened++; }, layoutContext() {}};
  c.controller.bind(); await c.controller.load();
  assert.equal(opened, 1);
  c.state.selected = "child";
  c.controller.bind({preservePanel: true}); await c.controller.load();
  assert.equal(c.controller.mode, "agents");
  assert.equal(opened, 1);
  c.controller.setMode("context");
  c.controller.bind({preservePanel: true}); await c.controller.load();
  assert.equal(c.controller.mode, "context");
});

test("late responses from a previous conversation cannot open its tree in a standalone chat", async () => {
  const replies = new Map();
  const c = setupController([thread("root"), thread("solo")], "root", path => new Promise(resolve => replies.set(path, resolve)));
  c.controller.bind(); const previous = c.controller.load();
  c.state.selected = "solo"; c.state.threadTabs.selected = "solo"; c.state.threadTabs.items.push({thread_id: "solo"});
  c.controller.bind(); const current = c.controller.load();
  replies.get("threads/solo/agent-tree")({root_thread_id: "solo", data: [thread("solo")]}); await current;
  replies.get("threads/root/agent-tree")({root_thread_id: "root", data: [thread("root"), thread("child", "root")]}); await previous;
  assert.equal(c.controller.root, "solo");
  assert.equal(c.$("panel-agents").hidden, true);
  assert.equal(c.controller.mode, "context");
  assert.equal(c.collapsed, true);
  assert.equal(c.state.threadTabs.items[1].tree_root, undefined);
});

test("creating a new draft session or switching conversations with an outstanding tree request immediately clears the tree and never requests new-session", async () => {
  const replies = new Map();
  const c = setupController([thread("root"), thread("child", "root")], "root", path => new Promise(resolve => replies.set(path, resolve)));
  c.controller.bind();
  assert.equal(c.controller.mode, "agents");
  assert.equal(c.$("panel-agents").hidden, false);
  const previous = c.controller.load();

  // Switch to a new draft session while the previous conversation's tree request is still in flight.
  c.state.selected = "new-session";
  c.state.detail = {work_thread: {thread_id: "new-session", title: "New session", browser_session: true}};
  c.state.threadTabs.selected = "new-session";
  c.state.threadTabs.items.push(c.state.detail.work_thread);
  c.$("conversation").classList.add("session-draft");
  c.controller.bind();
  assert.equal(c.controller.mode, "context");
  assert.equal(c.controller.root, null);
  assert.equal(c.$("panel-agents").hidden, true);
  assert.equal(c.$("context-panel").hidden, true);
  assert.equal(c.$("conversation").classList.contains("context-collapsed"), true);
  assert.equal(c.$("conversation").classList.contains("has-agent-tree"), false);
  assert.equal(c.$("conversation").classList.contains("agents-visible"), false);

  // Inventory refreshes while on the draft must not keep the old tree or query threads/new-session/agent-tree.
  c.controller.inventory([thread("root"), thread("child", "root")], []);
  assert.equal(c.requests.some(path => path.includes("new-session")), false);
  assert.deepEqual(c.errors, []);

  // When the delayed response from the previous conversation arrives, the draft remains clean.
  replies.get("threads/root/agent-tree")({root_thread_id: "root", data: [thread("root"), thread("child", "root")]});
  await previous;
  assert.equal(c.controller.mode, "context");
  assert.equal(c.controller.root, null);
  assert.equal(c.$("panel-agents").hidden, true);
  assert.equal(c.$("context-panel").hidden, true);
  assert.deepEqual(c.errors, []);
});

test("new draft sessions hide and collapse the Context panel across desktop and compact layouts, then restore appropriate Context access after the first message", async () => {
  const fs = require("node:fs");
  const agentTreeCss = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/agent-tree.css"), "utf8");
  // Desktop flex rules in agent-tree.css must exclude .session-draft so they never override .session-draft .context-panel { display: none }.
  assert.match(agentTreeCss, /\.conversation:not\(\.context-collapsed\):not\(\.session-draft\)\s+\.context-panel:not\(\.agents-mode\)\s*\{\s*display:\s*flex;/);
  assert.match(agentTreeCss, /\.conversation:not\(\.context-collapsed\):not\(\.session-draft\)\s+\.agent-panel-resize\s*\{\s*display:\s*block;/);

  // 1. Desktop (compact = false): transitioning from an existing chat with Context or Agents open to a new draft hides and collapses Context, then sending the first message restores Context.
  const desktop = setupController([thread("root"), thread("child", "root"), thread("created")], "root", undefined, {compact: false});
  desktop.controller.bind(); await desktop.controller.load();
  assert.equal(desktop.$("context-panel").hidden, false);
  assert.equal(desktop.$("conversation").classList.contains("context-collapsed"), false);

  // Enter new session draft.
  desktop.state.selected = "new-session";
  desktop.$("conversation").classList.add("session-draft");
  desktop.controller.bind();
  assert.equal(desktop.$("context-panel").hidden, true);
  assert.equal(desktop.$("conversation").classList.contains("context-collapsed"), true);
  assert.equal(desktop.$("conversation").classList.contains("agents-visible"), false);

  // First message transitions draft -> created thread: Context panel becomes available on desktop.
  desktop.state.selected = "created";
  desktop.$("conversation").classList.remove("session-draft");
  desktop.controller.bind();
  assert.equal(desktop.$("context-panel").hidden, false);
  assert.equal(desktop.$("conversation").classList.contains("context-collapsed"), false);

  // 2. Compact layout (<= 1100px, compact = true): automatic Agents opening in a parent chat does not persist a false uncollapsed preference into a new draft or a standalone conversation.
  const compact = setupController([thread("root"), thread("child", "root"), thread("solo")], "root", path => Promise.resolve(
    path.includes("solo") ? {root_thread_id: "solo", data: [thread("solo")]} : {root_thread_id: "root", data: [thread("root"), thread("child", "root")]}), {compact: true});
  compact.controller.bind(); await compact.controller.load();
  assert.equal(compact.controller.mode, "agents");
  assert.equal(compact.$("context-panel").hidden, false);
  assert.equal(compact.storage.get("agent-coord.context-collapsed"), undefined);

  // Opening a new draft from the Agents view hides the panel in compact layout too.
  compact.state.selected = "new-session";
  compact.$("conversation").classList.add("session-draft");
  compact.controller.bind();
  assert.equal(compact.$("context-panel").hidden, true);
  assert.equal(compact.$("conversation").classList.contains("context-collapsed"), true);

  // Navigating to a standalone conversation in compact layout keeps Context collapsed by default unless the user explicitly opened it.
  compact.state.selected = "solo";
  compact.$("conversation").classList.remove("session-draft");
  compact.controller.bind(); await compact.controller.load();
  assert.equal(compact.$("context-panel").hidden, true);
  assert.equal(compact.$("conversation").classList.contains("context-collapsed"), true);

  // Explicit user toggle persists across conversations, while still staying hidden on a new draft.
  compact.$("toggle-context").onclick();
  assert.equal(compact.storage.get("agent-coord.context-collapsed"), "false");
  assert.equal(compact.$("context-panel").hidden, false);
  compact.state.selected = "new-session";
  compact.$("conversation").classList.add("session-draft");
  compact.controller.bind();
  assert.equal(compact.$("context-panel").hidden, true);
  assert.equal(compact.$("conversation").classList.contains("context-collapsed"), true);
  compact.state.selected = "solo";
  compact.$("conversation").classList.remove("session-draft");
  compact.controller.bind();
  assert.equal(compact.$("context-panel").hidden, false);
});

test("both Context and Agents side panel modes support pointer and keyboard resizing with persisted widths", async () => {
  const c = setupController([thread("root"), thread("child", "root")]);
  const grip = c.$("agent-panel-resize"), conv = c.$("conversation");
  assert.equal(c.controller.mode, "context");
  assert.equal(grip.getAttribute("aria-label"), "Resize thread context panel");
  assert.equal(grip.getAttribute("aria-valuemin"), "220");
  assert.equal(grip.getAttribute("aria-valuemax"), "560");
  assert.equal(grip.getAttribute("aria-valuenow"), "340");

  // Pointer drag in Context mode updates --context-panel-width and persists to localStorage.
  grip.onpointerdown({pointerId: 1, preventDefault() {}});
  grip.onpointermove({clientX: 780});
  grip.onpointerup();
  assert.equal(conv.style.getPropertyValue("--context-panel-width"), "420px");
  assert.equal(grip.getAttribute("aria-valuenow"), "420");
  assert.equal(c.storage.get("agent-coord.context-panel-width"), "420");

  // Keyboard resize in Context mode clamps to sensible bounds.
  grip.onkeydown({key: "Home", preventDefault() {}});
  assert.equal(conv.style.getPropertyValue("--context-panel-width"), "220px");
  grip.onkeydown({key: "End", preventDefault() {}});
  assert.equal(conv.style.getPropertyValue("--context-panel-width"), "560px");

  // Switching to Agents mode switches the grip's ARIA range and resizes --agent-panel-width independently.
  c.controller.bind(); await c.controller.load();
  assert.equal(c.controller.mode, "agents");
  assert.equal(grip.getAttribute("aria-label"), "Resize agent tree panel");
  assert.equal(grip.getAttribute("aria-valuemin"), "30");
  assert.equal(grip.getAttribute("aria-valuemax"), "70");
  grip.onkeydown({key: "ArrowLeft", preventDefault() {}});
  assert.equal(conv.style.getPropertyValue("--agent-panel-width"), "57%");
  assert.equal(c.storage.get("agent-coord.agent-panel-width"), "57");
});
