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

const leaves = node => node.children ? node.children.flatMap(leaves) : [node.slot];
function assertLayout(layout) {
  const ids = layout.groups.flat();
  assert.equal(new Set(ids).size, ids.length, "each conversation belongs to exactly one group");
  assert.ok(layout.groups.every(group => group.length));
  layout.tabs.forEach((id, slot) => assert.ok(layout.groups[slot].includes(id)));
  assert.deepEqual(leaves(paneLayout.tree(layout)).sort((a, b) => a - b), layout.groups.map((_, i) => i));
  assert.ok(ids.includes(layout.selected));
}
function singleGroup() {
  return {order: ["a", "b", "c"], columns: [1], groups: [["a", "b", "c"]], tabs: ["a"], selected: "a", maximized: null};
}

test("edge drops split the target locally in all four directions and nest across axes", () => {
  for (const edge of ["left", "right", "top", "bottom"]) {
    const layout = singleGroup();
    assert.equal(paneLayout.move(layout, "b", 0, edge), true);
    assertLayout(layout);
    assert.equal(layout.tree.axis, ["left", "right"].includes(edge) ? "horizontal" : "vertical");
    assert.deepEqual(leaves(layout.tree), ["left", "top"].includes(edge) ? [1, 0] : [0, 1]);
    assert.deepEqual(layout.groups, [["a", "c"], ["b"]]);
    const otherAxis = layout.tree.axis === "horizontal" ? "bottom" : "right";
    assert.equal(paneLayout.move(layout, "c", 0, otherAxis), true);
    assertLayout(layout);
    const nested = layout.tree.children.find(node => node.children);
    assert.ok(nested); assert.notEqual(nested.axis, layout.tree.axis);
    assert.deepEqual(leaves(nested), [0, 2]);
  }
});

test("moving tabs between groups collapses empty splits and reordering respects insertion positions", () => {
  const layout = singleGroup();
  paneLayout.move(layout, "b", 0, "right");
  paneLayout.move(layout, "c", 0, "bottom");
  paneLayout.move(layout, "b", 0, "center", 0);
  assertLayout(layout); assert.deepEqual(layout.groups, [["b", "a"], ["c"]]);
  assert.equal(layout.tree.axis, "vertical");
  paneLayout.move(layout, "c", 0, "center", 1);
  assertLayout(layout); assert.deepEqual(layout.groups, [["b", "c", "a"]]);
  assert.deepEqual(layout.tree, {slot: 0});
  paneLayout.move(layout, "b", 0, "center", 2);
  assert.deepEqual(layout.groups, [["c", "b", "a"]]);
  paneLayout.move(layout, "a", 0, "center", 0);
  assert.deepEqual(layout.groups, [["a", "c", "b"]]); assertLayout(layout);
});

test("a sole tab cannot split itself, and invalid drops never change the layout", () => {
  const layout = {...singleGroup(), order: ["a"], groups: [["a"]], tabs: ["a"]};
  const before = structuredClone(layout);
  for (const edge of ["left", "right", "top", "bottom"]) assert.equal(paneLayout.move(layout, "a", 0, edge), false);
  assert.equal(paneLayout.move(layout, "a", 99, "right"), false);
  assert.equal(paneLayout.move(layout, "a", 0, "invalid"), false);
  assert.deepEqual(layout, before);
});

test("drop preview zones distinguish edges from the center at the pane's actual bounds", () => {
  const rect = {left: 100, top: 80, width: 800, height: 400};
  for (const [x, y, edge] of [[101, 280, "left"], [899, 280, "right"], [500, 81, "top"],
    [500, 479, "bottom"], [500, 280, "center"], [400, 200, "center"]]) {
    assert.equal(paneLayout.dropZone(rect, x, y), edge);
  }
});

test("repeated moves and splits preserve every conversation and valid tree references", () => {
  const layout = singleGroup(), edges = ["center", "left", "bottom", "right", "top"];
  for (let i = 0; i < 200; i++) {
    paneLayout.move(layout, ["a", "b", "c"][i % 3], (i * 7) % layout.groups.length, edges[i % edges.length], i % 4);
    assertLayout(layout); assert.deepEqual(layout.groups.flat().sort(), ["a", "b", "c"]);
  }
});

function setup() {
  const classes = () => { const set = new Set(); return {add: (...cs) => cs.forEach(c => set.add(c)), contains: c => set.has(c),
    remove: (...cs) => cs.forEach(c => set.delete(c)), toggle(c, on) { if (on) set.add(c); else set.delete(c); }}; };
  const element = () => ({style: {}, hidden: false, children: [], dataset: {}, classList: classes(),
    append(...children) { this.children.push(...children); }, replaceChildren() { this.children = []; },
    attributes: {}, setAttribute(name, value) { this.attributes[name] = value; }, querySelectorAll() { return []; },
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
  const threadViews = require("../plugins/agent-coord/scripts/agent_coord/web/views.js").threadViews;
  const context = vm.createContext({window, document: doc, location: {search: ""}, Event, URLSearchParams, setTimeout, threadViews});
  vm.runInContext(fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/thread-panes.js"), "utf8"), context);
  vm.runInContext("ThreadPanes.prototype.render = function() {}; ThreadPanes.prototype.position = function() {};", context);
  const visits = [], errors = [];
  let view = "first", threads = [thread("a"), thread("b")], shell;
  const select = async id => {
    assert.equal(shell.active, false);
    visits.push(id); state.selected = id;
    state.threadTabs.open(thread(id));
    doc.getElementById("message").value = state.drafts.get(id) || "";
  };
  const {ThreadTabs} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-tabs.js");
  state.threadTabs = new ThreadTabs({onSelect: async id => {
    if (!await shell.selectTab(id)) {
      shell.leave();
      if (id) await select(id);
      else { state.selected = null; state.threadTabs.overview(); }
    }
  }});
  state.threadTabs.setProject("/work");
  shell = context.setupPaneShell({state, document: doc, api: async path => thread(decodeURIComponent(path.split("/").at(-1))), select,
    goHome() { shell.leave(); visits.push(null); state.selected = null; state.threadTabs.overview(); }, refreshList: async () => {},
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
  // Tiling would copy an unaccepted send's draft into its pane.
  state.sending = new Set(["a"]);
  assert.equal(shell.tileCurrent(), false); assert.equal(shell.active, false); assert.equal(errors.length, 1);
  state.sending.clear();
  state.selected = "a"; shell.tileCurrent();
  doc.querySelector = () => ({});
  assert.equal(await shell.toggleCurrent(), false); assert.equal(shell.active, true);
  doc.querySelector = () => null; state.busy = true;
  assert.equal(await shell.toggleCurrent(), false); assert.equal(errors.length, 2);
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

test("manual tab moves reuse live documents, drafts and scroll, including after closing a nested group", () => {
  const {panes} = setup(); panes.tile("view", [thread("a"), thread("b"), thread("c")]);
  const record = panes.records.get("b"), draft = {text: "Unsent", images: [{id: "image"}], mentions: [{id: "ref"}], scroll: 260};
  record.frame = {contentWindow: {}, hidden: false};
  record.api = {setActive() {}, focus() {}, exportDraft: () => draft};
  const frame = record.frame, api = record.api;
  panes.moveThread(thread("b"), 0, "bottom");
  assertLayout(panes.layout);
  assert.equal(record.frame, frame); assert.equal(record.api, api); assert.equal(panes.exportDraft("b"), draft);
  panes.moveThread(thread("b"), 0, "center");
  assertLayout(panes.layout); assert.equal(panes.selected, "b");
  panes.close(0); assertLayout(panes.layout);
  assert.equal(panes.records.get("b"), record); assert.equal(panes.exportDraft("b"), draft);
  assert.equal(panes.layout.order.includes("b"), false);
});

test("dragging from conversation tabs seeds just the working set and retains the original draft", () => {
  const {shell, state, doc, setThreads} = shellFixture();
  for (const id of ["a", "b", "c"]) state.threadTabs.open(thread(id));
  state.selected = "a"; doc.getElementById("message").value = "Keep this draft";
  doc.getElementById("timeline").scrollTop = 123;
  setThreads([thread("unopened")]);
  shell.onDrop(thread("b"), 0, "right");
  assert.equal(shell.active, true); assertLayout(shell.layout);
  assert.deepEqual(Array.from(shell.layout.order), ["a", "b", "c"]);
  assert.deepEqual(Array.from(shell.layout.groups[0]), ["a", "c"]);
  assert.equal(shell.records.get("a").initialDraft.text, "Keep this draft");
  assert.equal(shell.records.get("a").initialDraft.scroll, 123);
  assert.equal(shell.selected, "b");
  assert.equal(shell.viewId, "first");
  assert.equal(state.threadTabs.selected, "b");
  assert.equal(doc.body.classList.contains("conversation-split"), true);
  assert.equal(doc.getElementById("leave-tiles").hidden, true);
  assert.equal(doc.getElementById("page-location").textContent, "Conversation");
});

test("normal tab navigation stays in the split and returns from Overview without retiling", async () => {
  const {shell, state, doc, visits} = shellFixture();
  for (const id of ["a", "b", "c"]) state.threadTabs.open(thread(id));
  state.selected = "a";
  shell.onDrop(thread("b"), 0, "right");
  const layout = shell.layout, record = shell.records.get("b");
  await state.threadTabs.activate("c");
  assert.equal(shell.active, true); assert.equal(shell.layout, layout);
  assert.equal(shell.selected, "c"); assert.equal(state.threadTabs.selected, "c");
  assert.deepEqual(visits, []);
  await state.threadTabs.activate(null);
  assert.equal(shell.active, false); assert.equal(state.threadTabs.selected, null);
  assert.equal(doc.body.classList.contains("conversation-split"), false);
  await state.threadTabs.activate("b");
  assert.equal(shell.active, true); assert.equal(shell.layout, layout);
  assert.equal(shell.records.get("b"), record); assert.equal(state.threadTabs.selected, "b");
  assert.equal(doc.getElementById("page-location").textContent, "Conversation");
});

test("closing the last split collapses to a normal tab and preserves draft, images, mentions and scroll", async () => {
  const {shell, state, doc, visits} = shellFixture();
  for (const id of ["a", "b"]) state.threadTabs.open(thread(id));
  state.selected = "a";
  shell.onDrop(thread("b"), 0, "right");
  const draft = {text: "Unsent draft", images: [{id: "image"}], mentions: [{id: "reference"}], scroll: 272};
  let mentions;
  state.mentions = {restore: (...args) => { mentions = args; }};
  shell.records.get("a").api = {exportDraft: () => draft, setActive() {}, focus() {}};
  shell.close(shell.layout.groups.findIndex(group => group.includes("b")));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(shell.active, false); assert.equal(shell.layouts.has("first"), false);
  assert.equal(doc.body.classList.contains("conversation-split"), false);
  assert.deepEqual(visits, ["a"]); assert.equal(state.threadTabs.selected, "a");
  assert.deepEqual(state.threadTabs.items.map(t => t.thread_id), ["a"]);
  assert.equal(doc.getElementById("message").value, draft.text);
  assert.equal(doc.getElementById("timeline").scrollTop, 272);
  assert.deepEqual(state.attachments.drafts.get("a"), draft.images);
  assert.deepEqual(mentions, ["a", draft.text, draft.mentions]);
});

test("merging split groups returns to normal tabs without losing any open conversation", async () => {
  const {shell, state, visits} = shellFixture();
  for (const id of ["a", "b", "c"]) state.threadTabs.open(thread(id));
  state.selected = "a";
  shell.onDrop(thread("b"), 0, "right");
  shell.moveThread(thread("b"), 0, "center");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(shell.active, false); assert.equal(state.threadTabs.selected, "b");
  assert.deepEqual(state.threadTabs.items.map(t => t.thread_id), ["a", "c", "b"]);
  assert.deepEqual(visits, ["b"]);
});

test("splitting eight tabs moves only the dragged tab and renders one standard strip per group", () => {
  const {shell, state} = shellFixture();
  const ids = ["a", "b", "c", "d", "e", "f", "g", "h"];
  for (const id of ids) state.threadTabs.open(thread(id));
  state.selected = "c";
  shell.onDrop(thread("g"), 0, "right");
  assert.deepEqual(Array.from(shell.layout.groups[0]), ["a", "b", "c", "d", "e", "f", "h"]);
  assert.deepEqual(Array.from(shell.layout.groups[1]), ["g"]);
  assert.equal(shell.layout.tabs[0], "c", "the original group keeps its selection");
  shell.slots = [];
  const panes = shell.layout.groups.map((_, slot) => shell.renderPane(slot));
  const strips = panes.map(pane => pane.children[0].children[0]);
  assert.equal(strips.length, 2);
  for (const strip of strips) {
    assert.equal(strip.className, "pane-tabs conversation-tabs");
    assert.equal(strip.attributes.role, "tablist");
    assert.ok(strip.children.every(wrapper => wrapper.className.startsWith("conversation-tab")));
    for (const wrapper of strip.children) {
      assert.equal(wrapper.children[0].className, "conversation-tab-select");
      if (wrapper.children[0].dataset.paneThread) {
        assert.equal(wrapper.children[1].className, "conversation-tab-close");
        assert.equal(wrapper.children[0].draggable, true);
      }
    }
  }
  assert.equal(strips[0].children[0].children[0].textContent, "Overview");
  const renderedIds = strips.flatMap(strip => strip.children.map(wrapper => wrapper.children[0].dataset.paneThread).filter(Boolean));
  assert.deepEqual(Array.from(renderedIds).sort(), ids);
  assert.equal(new Set(renderedIds).size, 8, "each conversation occurs in just one group");
  assert.equal(panes[0].children[0].children.length, 2, "the group header has its tab strip and maximize control, without an extra close-pane button");
});

test("group tab controls focus and close the requested tab without touching neighboring tabs", async () => {
  const {shell, state} = shellFixture();
  for (const id of ["a", "b", "c", "d"]) state.threadTabs.open(thread(id));
  state.selected = "a";
  shell.onDrop(thread("d"), 0, "right");
  shell.slots = [];
  const strip = shell.renderPane(0).children[0].children[0];
  const b = strip.children.find(wrapper => wrapper.children[0].dataset.paneThread === "b");
  b.children[0].onclick();
  assert.equal(shell.selected, "b");
  b.children[1].onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(Array.from(shell.layout.groups[0]), ["a", "c"]);
  assert.deepEqual(Array.from(shell.layout.groups[1]), ["d"]);
  assert.equal(shell.selected, "c", "closing a tab selects its neighbor in the same group");
  assert.equal(state.threadTabs.items.some(item => item.thread_id === "b"), false);
});

test("Shift-click closes a split conversation tab without focusing it", async () => {
  const {shell, state} = shellFixture();
  for (const id of ["a", "b", "c", "d"]) state.threadTabs.open(thread(id));
  state.selected = "a";
  shell.onDrop(thread("d"), 0, "right");
  shell.slots = [];
  const strip = shell.renderPane(0).children[0].children[0];
  const b = strip.children.find(wrapper => wrapper.children[0].dataset.paneThread === "b");
  const selected = shell.selected;
  b.children[0].onclick({shiftKey: true});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(Array.from(shell.layout.groups[0]), ["a", "c"]);
  assert.equal(shell.selected, selected);
  assert.equal(state.threadTabs.items.some(item => item.thread_id === "b"), false);
});

test("pane tabs expose status and unread-response indicators that follow thread updates", () => {
  const {shell, state} = shellFixture();
  state.threadTabs.open({...thread("run"), response_state: "working", unread_result: true});
  state.threadTabs.open({...thread("blocked"), response_state: "input"});
  state.threadTabs.open({...thread("answered"), response_state: "reply", unread_result: true});
  state.threadTabs.open(thread("calm"));
  state.selected = "run";
  shell.onDrop(thread("calm"), 0, "right");
  shell.slots = [];
  const strip = shell.renderPane(0).children[0].children[0];
  const tabOf = id => strip.children.find(wrapper => wrapper.children[0].dataset.paneThread === id)?.children[0];
  const indicator = id => { const tab = tabOf(id); return tab && {status: tab.dataset.status, unread: tab.dataset.unread}; };
  assert.deepEqual(indicator("run"), {status: "working", unread: "true"});
  assert.deepEqual(indicator("blocked"), {status: "input", unread: "false"});
  assert.equal(tabOf("blocked").dataset.needsAction, "true");
  assert.deepEqual(indicator("answered"), {status: "reply", unread: "true"});
  // A later read marks the response seen without touching the other tabs.
  const record = shell.records.get("answered"), source = {};
  record.frame = {contentWindow: source};
  shell.chrome = {querySelectorAll: () => [tabOf("answered")]};
  shell.changed(source, {...record.thread, unread_result: false});
  assert.deepEqual(indicator("answered"), {status: "reply", unread: "false"});
  assert.deepEqual(indicator("run"), {status: "working", unread: "true"});
  const blocked = shell.records.get("blocked"), blockedSource = {};
  blocked.frame = {contentWindow: blockedSource}; shell.chrome = {querySelectorAll: () => [tabOf("blocked")]};
  shell.changed(blockedSource, {...blocked.thread, needs_attention: false, unhandled_response: false, can_handle_response: false});
  assert.equal(tabOf("blocked").dataset.needsAction, "false");
});

test("moving and merging groups preserves their edited order in the remaining normal strip", async () => {
  const {shell, state} = shellFixture();
  for (const id of ["a", "b", "c", "d"]) state.threadTabs.open(thread(id));
  shell.onDrop(thread("d"), 0, "right");
  shell.moveThread(thread("c"), 0, "center", 0);
  shell.moveThread(thread("b"), 1, "center", 0);
  assert.deepEqual(Array.from(shell.layout.groups[0]), ["c", "a"]);
  assert.deepEqual(Array.from(shell.layout.groups[1]), ["b", "d"]);
  shell.moveThread(thread("b"), 0, "center", 1);
  shell.moveThread(thread("d"), 0, "center", 2);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(shell.active, false);
  assert.deepEqual(state.threadTabs.items.map(t => t.thread_id), ["c", "b", "d", "a"]);
});

test("dropping directly on a group tab inserts the conversation and collapses the empty group", async () => {
  const {shell, state} = shellFixture();
  for (const id of ["a", "b", "c"]) state.threadTabs.open(thread(id));
  shell.onDrop(thread("c"), 0, "right");
  shell.slots = [];
  const strip = shell.renderPane(0).children[0].children[0];
  const target = strip.children.find(wrapper => wrapper.children[0].dataset.paneThread === "b").children[0];
  shell.dragging = thread("c");
  let accepted = false;
  const event = {clientX: 0, dataTransfer: {}, preventDefault() { accepted = true; }, stopPropagation() {}};
  target.ondragover(event);
  assert.equal(accepted, true);
  assert.equal(event.dataTransfer.dropEffect, "move");
  target.ondrop(event);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(shell.active, false);
  assert.deepEqual(state.threadTabs.items.map(t => t.thread_id), ["a", "c", "b"]);
});

test("closing a background tab updates the split without changing focus", async () => {
  const {shell, state} = shellFixture();
  for (const id of ["a", "b", "c"]) state.threadTabs.open(thread(id));
  shell.onDrop(thread("b"), 0, "right");
  await state.threadTabs.close("c");
  assert.equal(shell.active, true); assert.equal(shell.selected, "b");
  assert.equal(state.threadTabs.selected, "b");
  assert.deepEqual(Array.from(shell.layout.order), ["a", "b"]);
  assert.equal(shell.layout.groups.length, 2);
});

test("project view restoration retains the tab split presentation", () => {
  const {shell, state, doc, setView} = shellFixture();
  for (const id of ["a", "b"]) state.threadTabs.open(thread(id));
  shell.onDrop(thread("b"), 0, "right");
  const layout = shell.layout;
  shell.leave(); setView("second"); shell.tileCurrent();
  assert.equal(doc.body.classList.contains("conversation-split"), false);
  assert.equal(doc.getElementById("page-location").textContent, "Tiled threads");
  shell.leave(); setView("first"); shell.restoreView();
  assert.equal(shell.layout, layout); assert.equal(shell.active, true);
  assert.equal(doc.body.classList.contains("conversation-split"), true);
  assert.equal(doc.getElementById("page-location").textContent, "Conversation");
});

test("Escape removes drag shields without changing conversation layout or focus", () => {
  const {panes} = setup(); panes.tile("view", [thread("a"), thread("b")]);
  const before = structuredClone(panes.layout), removed = [];
  panes.dragging = thread("b"); panes.dropSurfaces = [{dataset: {edge: "left"}, remove() { removed.push(true); }}];
  panes.keydown({key: "Escape", preventDefault() {}});
  assert.equal(panes.dragging, null); assert.deepEqual(removed, [true]);
  assert.deepEqual(panes.layout, before);
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

test("explicitly opening a loading pane focuses it once after its draft is restored", () => {
  const {panes} = setup();
  panes.tile("dispatcher", [thread("a")]);
  const source = {}, record = panes.records.get("a"), calls = [];
  const draft = {text: "Follow up on these findings", images: [], scroll: 120};
  record.frame = {contentWindow: source, hidden: false}; record.initialDraft = draft;
  const api = {restoreDraft: value => calls.push(["draft", value]),
    setActive() {}, focus: () => calls.push(["focus"])};

  assert.equal(panes.focus("a"), true);
  assert.deepEqual(calls, [], "opening must wait for the conversation API to attach");
  assert.equal(panes.attach(source, api), true);
  assert.deepEqual(calls, [["draft", draft], ["focus"]]);
  panes.attach(source, api);
  assert.deepEqual(calls, [["draft", draft], ["focus"]], "reattaching must not repeat the original focus request");
});

test("automatic tiles never steal focus when their conversation frames attach", () => {
  const {panes} = setup(), focused = [];
  panes.tile("dispatcher", [thread("a"), thread("b")]);
  for (const id of ["a", "b"]) {
    const source = {};
    panes.records.get(id).frame = {contentWindow: source, hidden: false};
    assert.equal(panes.attach(source, {setActive() {}, focus: () => focused.push(id)}), true);
  }
  assert.equal(panes.selected, "a");
  assert.deepEqual(focused, [], "even the selected automatic tile leaves focus with the dispatcher input");
});

test("leaving a loading pane cancels its deferred focus when it attaches", async t => {
  for (const destination of ["another pane", "overview"]) {
    await t.test(destination, () => {
      const {panes} = setup();
      panes.tile("dispatcher", [thread("a"), thread("b")]);
      const source = {}, record = panes.records.get("a");
      record.frame = {contentWindow: source, hidden: false};
      let focused = 0;
      const api = {setActive() {}, focus: () => focused++};
      panes.focus("a");
      if (destination === "another pane") panes.focus("b", false);
      else panes.hide();

      assert.equal(panes.attach(source, api), true);
      assert.equal(focused, 0, "a late-loading conversation must not pull focus back from the destination");
      panes.show("dispatcher"); panes.focus("a", false);
      panes.attach(source, api);
      assert.equal(focused, 0, "the abandoned focus request must not survive a later return");
    });
  }
});

test("workspace pane dismissal delegates visibility without closing the session or losing its draft", () => {
  const {panes, exits} = setup(), removed = [];
  const a = thread("a"), b = thread("b");
  panes.tile("dispatcher", [a, b]);
  const record = panes.records.get("a");
  const draft = {text: "Keep this follow-up", mentions: [{id: "reference"}], images: [{id: "image"}], scroll: 260};
  record.frame = {contentWindow: {}, hidden: false};
  record.api = {setActive() {}, focus() {}, exportDraft: () => draft};
  const frame = record.frame, api = record.api;
  panes.focus("b", false);
  panes.workspaceClose = id => {
    removed.push(id);
    panes.tile("dispatcher", [b]);
  };

  panes.close(0);
  assert.deepEqual(removed, ["a"], "dismissal targets the requested pane, not the currently focused conversation");
  assert.deepEqual(panes.layout.order, ["b"]);
  assert.equal(panes.active, true); assert.deepEqual(exits, []);
  assert.equal(a.attention, "now");
  assert.equal(panes.records.get("a"), record);
  assert.equal(record.frame, frame); assert.equal(record.api, api);
  assert.deepEqual(panes.exportDraft("a"), draft);

  panes.tile("dispatcher", [a, b]);
  assert.equal(panes.records.get("a"), record);
  assert.equal(record.frame, frame);
  assert.deepEqual(panes.exportDraft("a"), draft, "reopening the tile preserves its text, images, references, and scroll position");
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
  const threadViews = require("../plugins/agent-coord/scripts/agent_coord/web/views.js").threadViews;
  const c = vm.createContext({window, document, location: {hash: "#a"}, Event, threadViews,
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
  pane.receive({events: [], approvals: [{thread_id: "other", request_key: "unrelated"}]});
  assert.equal(refreshed, 1, "another conversation's approval does not refresh this pane");
  pane.receive({events: [], approvals: [{thread_id: "a", request_key: "approval"}]});
  assert.equal(refreshed, 2, "approval notifications recover controls even without their item event");
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
  const state = {selected: null, sourceWindowId: "window", organization: {repositories: [], projects: []}};
  const views = {activeId: "all", filters: {}, current() { return {}; }, read() { return {filters: this.filters}; }, sync() {},
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
