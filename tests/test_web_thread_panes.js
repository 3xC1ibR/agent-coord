"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {paneLayout, ThreadPanes} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-panes.js");

test("layouts fit readable panes, use tall primary pane for three, and tab overflow", () => {
  assert.deepEqual(paneLayout.plan(0, 1600, 900), []);
  assert.deepEqual(paneLayout.plan(2, 1000, 800), [1, 1]);
  assert.deepEqual(paneLayout.plan(3, 1000, 800), [1, 2]);
  assert.deepEqual(paneLayout.plan(4, 1000, 800), [2, 2]);
  assert.deepEqual(paneLayout.plan(6, 1600, 900), [2, 2, 2]);
  const layout = paneLayout.arrange(["a", "b", "c", "a", "d"], 400, 400);
  assert.deepEqual(layout, {columns: [1], groups: [["a", "b", "c", "d"]]});
  for (const width of [320, 720, 1000, 1600, 3000]) {
    for (const height of [240, 600, 900, 2000]) {
      const ids = Array.from({length: 29}, (_, i) => String(i));
      const result = paneLayout.arrange(ids, width, height);
      assert.deepEqual(result.groups.flat().sort(), ids.sort());
      assert.ok(result.groups.length <= 6);
      assert.ok(result.groups.every(group => group.length > 0));
    }
  }
});

test("pane shortcuts leave typing, IME and ordinary navigation alone", () => {
  const modifiers = {ctrlKey: true, altKey: true};
  assert.equal(paneLayout.shortcut({...modifiers, metaKey: true, code: "KeyT", key: "†"}), "tile");
  assert.equal(paneLayout.shortcut({...modifiers, key: "ArrowRight"}), "next");
  assert.equal(paneLayout.shortcut({...modifiers, key: "Enter"}), "maximize");
  assert.equal(paneLayout.shortcut({key: "ArrowRight"}), null);
  assert.equal(paneLayout.shortcut({...modifiers, key: "Enter", isComposing: true}), null);
  assert.equal(paneLayout.shortcut({...modifiers, key: "Enter", shiftKey: true}), null);
});

function setup() {
  const classes = () => { const set = new Set(); return {add: c => set.add(c), contains: c => set.has(c),
    remove: c => set.delete(c), toggle(c, on) { if (on) set.add(c); else set.delete(c); }}; };
  const element = () => ({style: {}, hidden: false, children: [], dataset: {}, classList: classes(),
    append(...children) { this.children.push(...children); }, replaceChildren() { this.children = []; },
    setAttribute() {}, querySelectorAll() { return []; },
    getBoundingClientRect: () => ({width: 1000, height: 800, left: 0, top: 0})});
  const doc = {createElement: element, hasFocus: () => true};
  const focus = [], exits = [];
  const panes = new ThreadPanes({document: doc, container: element(), onFocus: id => focus.push(id), onExit: () => exits.push(true)});
  // Layout lifecycle is tested independently of browser geometry, which is
  // exercised by the native smoke test against real conversation documents.
  panes.render = () => {}; panes.position = () => {};
  return {panes, focus, exits, doc, element};
}
const thread = id => ({thread_id: id, title: "Thread " + id, attention: "now"});

function shellFixture() {
  const vm = require("node:vm"), fs = require("node:fs");
  const {doc, element} = setup(), controls = new Map(), listeners = {};
  doc.body = element(); doc.querySelector = () => null;
  doc.getElementById = id => {
    if (!controls.has(id)) controls.set(id, {...element(), value: "", scrollTop: 0});
    return controls.get(id);
  };
  doc.addEventListener = (name, callback) => { listeners[name] = callback; };
  const state = {selected: null, drafts: new Map(), attachments: {drafts: new Map(), pending: () => false,
    items(id) { return this.drafts.get(id) || []; }}};
  const window = {addEventListener() {}, dispatchEvent() {}};
  const context = vm.createContext({window, document: doc, location: {search: ""}, Event, URLSearchParams, setTimeout});
  vm.runInContext(fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/thread-panes.js"), "utf8"), context);
  vm.runInContext("ThreadPanes.prototype.render = function() {}; ThreadPanes.prototype.position = function() {};", context);
  const visits = [], errors = [];
  let view = "first", threads = [thread("a"), thread("b")], shell;
  const select = async id => {
    assert.equal(shell.active, false);
    visits.push(id); state.selected = id;
    doc.getElementById("message").value = state.drafts.get(id) || "";
  };
  shell = context.setupPaneShell({state, document: doc, api() {}, select,
    goHome() { shell.leave(); visits.push(null); state.selected = null; }, refreshList: async () => {},
    getView: () => view, getThreads: () => threads, showError: error => errors.push(error), notifications: () => null});
  return {shell, state, doc, controls, visits, errors, listeners,
    setView: value => { view = value; }, setThreads: value => { threads = value; }};
}

test("tiling shortcut toggles overview while retaining resized layout, selected tab and drafts", async () => {
  const {shell, state, visits, listeners, setThreads} = shellFixture();
  assert.equal(await shell.toggleCurrent(), true);
  const layout = shell.layout, record = shell.records.get("b");
  layout.widths = [1.4, .6]; layout.heights = {0: [1.2, .8]}; shell.focus("b", false);
  let draft = {text: "Keep this", images: [{id: "image"}], scroll: 220};
  state.drafts.set("b", draft.text); state.attachments.drafts.set("b", draft.images);
  record.api = {exportDraft: () => draft, restoreDraft: value => { draft = value; }, setActive() {}, focus() {}};
  let prevented = false;
  listeners.keydown({key: "t", code: "KeyT", ctrlKey: true, altKey: true, metaKey: true,
    preventDefault() { prevented = true; }});
  assert.equal(prevented, true); assert.equal(shell.active, false); assert.deepEqual(visits, [null]);
  setThreads([thread("a"), thread("b"), thread("new")]);
  await shell.toggleCurrent();
  assert.equal(shell.layout, layout); assert.equal(shell.selected, "b");
  assert.deepEqual(layout.widths, [1.4, .6]); assert.equal(layout.heights[0][0], 1.2);
  assert.equal(draft.text, "Keep this"); assert.equal(draft.images[0].id, "image"); assert.equal(draft.scroll, 220);
  assert.equal(shell.layout.order.includes("new"), false);
  assert.equal(shell.tileCurrent(), true);
  assert.notEqual(shell.layout, layout); assert.equal(shell.layout.order.includes("new"), true);
});

test("toggle returns to the original single conversation and resyncs its edited draft on reentry", async () => {
  const {shell, state, doc, visits} = shellFixture();
  state.selected = "a"; doc.getElementById("message").value = "Original";
  doc.getElementById("timeline").scrollTop = 80;
  shell.tileCurrent();
  const layout = shell.layout, record = shell.records.get("a"), source = {};
  let draft = {text: "Edited in pane", images: [], scroll: 260};
  record.frame = {contentWindow: source};
  record.api = {exportDraft: () => draft, restoreDraft: value => { draft = value; }, setActive() {}, focus() {}};
  shell.draft(source, draft); shell.focus("b", false);
  await shell.toggleCurrent();
  assert.deepEqual(visits, ["a"]); assert.equal(state.selected, "a");
  assert.equal(doc.getElementById("message").value, "Edited in pane");
  assert.equal(doc.getElementById("timeline").scrollTop, 80);
  doc.getElementById("message").value = "Edited outside panes";
  await shell.toggleCurrent();
  assert.equal(shell.layout, layout); assert.equal(shell.selected, "b");
  assert.equal(draft.text, "Edited outside panes"); assert.equal(draft.scroll, 260);
});

test("toggle respects modal/send guards and view restoration returns to that view's overview", async () => {
  const {shell, state, doc, visits, errors, setView} = shellFixture();
  state.selected = "a"; shell.tileCurrent();
  doc.querySelector = () => ({});
  assert.equal(await shell.toggleCurrent(), false); assert.equal(shell.active, true);
  doc.querySelector = () => null; state.busy = true;
  assert.equal(await shell.toggleCurrent(), false); assert.equal(errors.length, 1);
  state.busy = false;
  shell.leave(); state.selected = null; setView("second"); await shell.toggleCurrent();
  assert.equal(shell.viewId, "second");
  shell.leave(); setView("first"); shell.restoreView();
  await shell.toggleCurrent();
  assert.deepEqual(visits, [null]);
});

test("retile reuses conversations, preserves selected thread and excludes closed threads", () => {
  const {panes} = setup();
  panes.tile("view", [thread("a"), thread("b"), thread("c"), {...thread("closed"), attention: "archived"}]);
  const record = panes.records.get("a");
  record.api = {setActive() {}, focus() {}, draft: "unfinished"};
  panes.focus("b");
  panes.tile("view", [thread("c"), thread("b"), thread("a"), thread("d")]);
  assert.equal(panes.records.get("a"), record);
  assert.equal(record.api.draft, "unfinished");
  assert.equal(panes.selected, "b");
  assert.deepEqual(panes.layout.groups.flat(), ["a", "b", "c", "d"]);
  assert.equal(panes.records.has("closed"), false);
});

test("view layouts are independent and closing panes keeps conversation state", () => {
  const {panes, exits} = setup();
  panes.tile("first", [thread("a"), thread("b")]);
  panes.focus("b"); panes.maximize();
  const first = panes.layout;
  panes.tile("second", [thread("c")]);
  assert.equal(panes.selected, "c");
  panes.show("first");
  assert.equal(panes.layout, first); assert.equal(panes.selected, "b");
  assert.equal(panes.layout.maximized, 1);
  let prevented = false;
  panes.keydown({key: "Escape", preventDefault: () => { prevented = true; }});
  assert.equal(panes.layout.maximized, null); assert.equal(prevented, true);
  panes.close(1);
  assert.equal(panes.records.has("b"), true);
  assert.equal(panes.selected, "a");
  assert.deepEqual(panes.layout.columns, [1]);
  panes.close(0);
  assert.equal(panes.active, false); assert.equal(exits.length, 1);
  assert.equal(panes.show("first"), false);
  assert.equal(panes.show("second"), true);
});

test("overflow tabs switch without replacing records and keyboard focus follows panes", () => {
  const {panes} = setup();
  const threads = Array.from({length: 8}, (_, i) => thread(String(i)));
  panes.tile("view", threads);
  assert.deepEqual(panes.layout.groups, [["0", "4"], ["1", "5"], ["2", "6"], ["3", "7"]]);
  panes.focus("4"); assert.equal(panes.layout.tabs[0], "4");
  const groups = structuredClone(panes.layout.groups);
  panes.tile("view", threads.slice().reverse());
  assert.deepEqual(panes.layout.groups, groups);
  assert.equal(panes.layout.tabs[0], "4");
  panes.cycle(1); assert.equal(panes.selected, "1");
  panes.cycle(-1); assert.equal(panes.selected, "4");
  panes.close(0); assert.deepEqual(panes.layout.groups[0], ["0"]);
  panes.open(thread("extra")); assert.equal(panes.selected, "extra");
});

test("only owned conversation frames may attach or claim focus", () => {
  const {panes} = setup();
  panes.tile("view", [thread("a"), thread("b")]);
  const source = {}, calls = [];
  const record = panes.records.get("a"); record.frame = {contentWindow: source}; record.initialDraft = {text: "draft"};
  const api = {restoreDraft: draft => calls.push(draft), setActive: active => calls.push(active)};
  assert.equal(panes.attach({}, api), false);
  assert.equal(panes.attach(source, api), true);
  assert.deepEqual(calls.slice(0, 2), [{text: "draft"}, true]);
  assert.equal(record.initialDraft, null);
  panes.focus("b", false); panes.focused({}); assert.equal(panes.selected, "b");
  record.frame.hidden = true; panes.focused(source); assert.equal(panes.selected, "b");
  record.frame.hidden = false; panes.focused(source); assert.equal(panes.selected, "a");
});

test("background conversations keep unread results until actual focus and consume shared events", async () => {
  const vm = require("node:vm"), fs = require("node:fs");
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/thread-panes.js"), "utf8");
  const requests = [], events = [], listeners = {};
  let focused = false, pane, refreshed = 0;
  const state = {selected: "a", detail: {work_thread: {thread_id: "a", checkpoint: {id: 2}, turn_completion: {id: 3}}},
    attachments: {items: () => [], drafts: new Map()}, drafts: new Map()};
  const controls = {message: {value: "Draft A"}, timeline: {scrollTop: 0, scrollHeight: 123}, composer: {hidden: false}};
  const host = {from: () => ({}), attach: (_source, api) => { pane = api; api.setActive(true); },
    requestList() {}, draft() {}, changed() {}, focused() {}, keydown() {}};
  const window = {parent: {agentCoordPanes: host, dispatchEvent() {}}};
  const document = {body: {classList: {add() {}}}, hasFocus: () => focused,
    getElementById: id => controls[id], querySelector: () => null,
    addEventListener: (type, callback) => { listeners[type] = callback; }};
  const c = vm.createContext({window, document, location: {hash: "#a"}, Event,
    setTimeout, console, EventSource: class { constructor() { throw new Error("Pane opened a duplicate event stream"); } }});
  vm.runInContext(source, c);
  await c.setupPaneConversation({state, document, api: async (path, body) => requests.push({path, body}),
    select: async () => {}, refreshDetail: async () => {}, refreshThread: async () => {},
    applyEvent: event => events.push(event), scheduleDetail: () => refreshed++, renderStatus() {}, showError: error => { throw error; }});
  assert.equal(requests.length, 0);
  focused = true; pane.setActive(true); await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 1); assert.equal(requests[0].body.seen_completion_id, 3);
  pane.setActive(true); assert.equal(requests.length, 1);
  focused = false; state.detail.work_thread.turn_completion.id = 4; pane.setActive(true);
  assert.equal(requests.length, 1);
  pane.receive({events: [{method: "item/agentMessage/delta", params: {threadId: "a"}}], completions: [{thread_id: "a"}]});
  assert.equal(events.length, 1); assert.equal(refreshed, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(pane.exportDraft())), {text: "Draft A", mentions: [], images: [], scroll: 123});
  pane.restoreDraft({text: "New", images: [], scroll: null});
  assert.equal(controls.timeline.scrollTop, 123, "new panes retain the transcript's initial scroll position");
  pane.restoreDraft({text: "Existing", images: [], scroll: 0});
  assert.equal(controls.timeline.scrollTop, 0, "an explicitly saved position at the top is restored");
});

test("explicit overview links leave old tiles; Back restores tiling only for a tiled history entry", async () => {
  const vm = require("node:vm"), fs = require("node:fs");
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const panes = {active: true, selected: "draft-thread", restoreView() { this.active = true; }};
  const state = {selected: null, sourceWindowId: "window"};
  const views = {activeId: "all", filters: {}, read() { return {filters: this.filters}; }, sync() {},
    async overview(filters) { this.activeId = "all"; this.filters = filters; panes.restoreView(); },
    async activate(id) { this.activeId = id; panes.restoreView(); }};
  const c = vm.createContext({window: {agentCoordPanes: panes}, state, savedViews: views,
    history: {}, location: {}, document: {querySelector: () => null}, showError() {},
    api: async () => ({data: []}), goHome: () => { panes.active = false; state.selected = null; },
    select: async id => { if (panes.active) panes.selected = id; else state.selected = id; },
    agentCoordNavigation: {Router: function (options) { return options; }}});
  vm.runInContext(source.slice(source.indexOf("  navigation = new agentCoordNavigation.Router("), source.indexOf("  window.agentCoordNavigate =")), c);
  const before = c.navigation.capture();
  assert.equal(before.tiled, true); assert.equal(before.thread, "draft-thread");
  await c.navigation.apply({kind: "overview", filters: {project: "billing"}});
  assert.equal(panes.active, false); assert.equal(c.navigation.capture().thread, null);
  assert.equal(c.navigation.capture().filters.project, "billing");
  await c.navigation.restore(before);
  assert.equal(panes.active, true); assert.equal(panes.selected, "draft-thread");
  await c.navigation.restore({viewId: "all", filters: {}, thread: "single-thread", tiled: false});
  assert.equal(panes.active, false); assert.equal(state.selected, "single-thread");
  await c.navigation.apply({kind: "view", id: "other"});
  assert.equal(panes.active, false); assert.equal(views.activeId, "other");
});
