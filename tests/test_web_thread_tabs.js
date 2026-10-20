"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const {ThreadTabs} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-tabs.js");
const thread = id => ({thread_id: id, title: "Thread " + id, attention: "now", response_state: "working"});

function setup(storage = new Map()) {
  const visits = [], errors = [];
  const tabs = new ThreadTabs({storage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    onSelect: id => { visits.push(id); if (id) tabs.open(thread(id)); else tabs.overview(); }, onError: error => errors.push(error)});
  tabs.setProject("/work");
  return {tabs, visits, errors, storage, ids: () => tabs.items.map(item => item.thread_id)};
}

test("overview and repeated opens preserve the working set and user order", async () => {
  const {tabs, visits, ids} = setup();
  tabs.open(thread("a")); tabs.open(thread("b")); tabs.open(thread("c"));
  tabs.move("c", 0);
  await tabs.activate(null);
  assert.equal(tabs.selected, null);
  assert.deepEqual(ids(), ["c", "a", "b"]);
  await tabs.activate("a"); await tabs.activate("a");
  assert.deepEqual(visits, [null, "a"]);
  assert.deepEqual(ids(), ["c", "a", "b"]);
});

test("project scratchpad starts closed and persists explicit opens and closes", async () => {
  const {tabs, ids, visits, storage} = setup();
  tabs.onScratchpad = () => tabs.scratchpad();
  tabs.open(thread("one"));
  const focused = [];
  tabs.reveal = id => focused.push(id);
  const arrow = {key: "ArrowRight", preventDefault() {}};
  tabs.keydown(arrow, null);
  assert.deepEqual(focused, ["one"]);
  assert.equal(tabs.scratchpadOpen, false);
  await tabs.activate("project-scratchpad");
  assert.equal(tabs.selected, "project-scratchpad");
  assert.equal(tabs.scratchpadOpen, true);
  assert.deepEqual(ids(), ["one"]);
  const reopened = setup(storage); reopened.tabs.onScratchpad = () => {};
  assert.equal(reopened.tabs.scratchpadOpen, true);
  assert.equal(reopened.tabs.destination(), "project-scratchpad");
  focused.length = 0; tabs.keydown(arrow, null);
  assert.deepEqual(focused, ["project-scratchpad"]);
  await tabs.close("project-scratchpad");
  assert.equal(tabs.selected, "one");
  assert.equal(tabs.scratchpadOpen, false);
  assert.deepEqual(visits, ["one"]);
  const closed = setup(storage); closed.tabs.onScratchpad = () => {};
  assert.equal(closed.tabs.scratchpadOpen, false);
  assert.equal(closed.tabs.destination(), "one");
});

test("scratchpad closes in the background without navigation and respects the navigation guard", async () => {
  const {tabs, visits} = setup(); tabs.onScratchpad = () => tabs.scratchpad();
  tabs.scratchpad(); tabs.open(thread("one"));
  tabs.canNavigate = () => false;
  await tabs.close("project-scratchpad"); assert.equal(tabs.scratchpadOpen, true);
  tabs.canNavigate = () => true;
  await tabs.close("project-scratchpad");
  assert.equal(tabs.selected, "one"); assert.deepEqual(visits, []);
  tabs.scratchpad(); await tabs.close("one");
  await tabs.close("project-scratchpad");
  assert.equal(tabs.selected, null); assert.deepEqual(visits, [null]);
});

test("scratchpad visibility stays with its project and defaults closed for older saved tabs", async () => {
  const {tabs, storage} = setup(); tabs.onScratchpad = () => tabs.scratchpad();
  tabs.scratchpad(); tabs.setProject("/other");
  assert.equal(tabs.scratchpadOpen, false);
  tabs.scratchpad(); await tabs.close("project-scratchpad");
  tabs.setProject("/work"); assert.equal(tabs.scratchpadOpen, true);
  tabs.setProject("/other"); assert.equal(tabs.scratchpadOpen, false);
  storage.set("agent-coord.thread-tabs.:/work", JSON.stringify({items: [], selected: "project-scratchpad"}));
  const legacy = setup(storage); legacy.tabs.onScratchpad = () => {};
  assert.equal(legacy.tabs.scratchpadOpen, false); assert.equal(legacy.tabs.destination(), null);
});

test("closing tabs only navigates, preserves thread lifecycle, and returns to overview after the last", async () => {
  const {tabs, visits, ids} = setup(), a = thread("a"), b = thread("b"), c = thread("c");
  tabs.open(a); tabs.open(b); tabs.open(c); tabs.open(b);
  await tabs.close("a");
  assert.deepEqual(visits, []);
  assert.equal(tabs.selected, "b");
  await tabs.close("b");
  assert.deepEqual(ids(), ["c"]); assert.equal(tabs.selected, "c");
  await tabs.close("c");
  assert.deepEqual(visits, ["c", null]); assert.deepEqual(ids(), []);
  assert.deepEqual([a, b, c], [thread("a"), thread("b"), thread("c")]);
  await tabs.close(null); assert.deepEqual(visits, ["c", null]);
});

test("project changes and reload retain tab order without automatically opening conversations", () => {
  const {tabs, storage, ids, visits} = setup();
  tabs.open(thread("a")); tabs.open(thread("b")); tabs.move("b", 0);
  tabs.setProject("/other"); tabs.open(thread("other"));
  tabs.setProject("/work"); assert.deepEqual(ids(), ["b", "a"]); assert.equal(tabs.selected, null);
  const restored = setup(storage);
  assert.deepEqual(restored.ids(), ["b", "a"]); assert.deepEqual(visits, []);
  assert.equal(restored.tabs.selected, null);
});

test("inventory updates and filters do not remove, reorder, or open tabs", () => {
  const {tabs, ids} = setup();
  tabs.open(thread("a")); tabs.open(thread("b")); tabs.overview();
  tabs.update([{...thread("a"), title: "Renamed", attention: "later"}, thread("extra")]);
  assert.deepEqual(ids(), ["a", "b"]); assert.equal(tabs.selected, null);
  assert.equal(tabs.items[0].title, "Renamed");
  tabs.update([]); assert.deepEqual(ids(), ["a", "b"]);
});

test("a submitted draft keeps its tab position and cannot duplicate the created thread", () => {
  const {tabs, ids, storage} = setup();
  tabs.open(thread("a")); tabs.open(thread("new-session")); tabs.open(thread("b"));
  tabs.open(thread("new-session"));
  assert.deepEqual(setup(storage).ids(), ["a", "b"]);
  tabs.replace("new-session", thread("created"));
  assert.deepEqual(ids(), ["a", "created", "b"]); assert.equal(tabs.selected, "created");
});

test("navigation guards protect drafts during sending and failures remain visible", async () => {
  const {tabs, ids, visits, errors} = setup(); tabs.open(thread("a")); tabs.open(thread("b"));
  tabs.canNavigate = () => false;
  await tabs.close("b"); await tabs.activate("a");
  assert.deepEqual(ids(), ["a", "b"]); assert.deepEqual(visits, []);
  tabs.canNavigate = () => true;
  tabs.onSelect = async () => { throw new Error("Unavailable"); };
  await tabs.activate("a"); assert.equal(errors[0].message, "Unavailable");
});

test("keyboard reordering cannot move Overview and ignores composition", () => {
  const {tabs, ids} = setup(); tabs.open(thread("a")); tabs.open(thread("b"));
  const event = {key: "ArrowLeft", altKey: true, shiftKey: true, preventDefault() {}};
  tabs.keydown({...event, isComposing: true}, "b"); assert.deepEqual(ids(), ["a", "b"]);
  tabs.keydown(event, "b"); assert.deepEqual(ids(), ["b", "a"]);
  tabs.keydown(event, null); assert.deepEqual(ids(), ["b", "a"]);
});

test("unavailable or malformed storage does not prevent opening conversations", () => {
  for (const stored of ["null", "{}", "invalid", '[null,{}, {"thread_id":"a"},{"thread_id":"a"}]']) {
    const {tabs} = setup(new Map([["agent-coord.thread-tabs.:/work", stored]]));
    tabs.open(thread("b")); assert.equal(tabs.selected, "b");
    assert.equal(new Set(tabs.items.map(item => item.thread_id)).size, tabs.items.length);
  }
});

test("status refreshes retain the dragged DOM tab until drop or cancellation", () => {
  const element = () => ({children: [], dataset: {}, scrollLeft: 0, classList: {add() {}},
    append(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
    setAttribute() {}, contains: () => false, querySelectorAll: () => []});
  const container = element(), document = {createElement: element};
  const tabs = new ThreadTabs({container, document}); tabs.setProject("/work");
  tabs.open(thread("a")); tabs.open(thread("b"));
  const source = container.children[1], ends = [];
  tabs.onDragEnd = () => ends.push(true);
  source.ondragstart({dataTransfer: {setData() {}}});
  tabs.update([{...thread("a"), title: "Streaming update"}]);
  assert.equal(container.children[1], source, "streaming must not detach the native drag source");
  source.ondragend();
  assert.deepEqual(ends, [true]); assert.equal(tabs.dragging, null);
  assert.equal(container.children[1].children[0].textContent, "Streaming update");
  let prevented = false;
  tabs.onDragStart = () => false;
  container.children[1].ondragstart({preventDefault() { prevented = true; }});
  assert.equal(prevented, true); assert.equal(tabs.dragging, null);
});

test("conversation tabs keep action dots through reading and refresh when blockers are handled", () => {
  const element = () => ({children: [], dataset: {}, attributes: {}, scrollLeft: 0, classList: {add() {}},
    append(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
    setAttribute(key, value) { this.attributes[key] = value; }, contains: () => false, querySelectorAll: () => []});
  const container = element(), tabs = new ThreadTabs({container, document: {createElement: element}});
  tabs.setProject("/work");
  const approval = {...thread("approval"), response_state: "input", needs_attention: true, unread_result: true};
  tabs.open(approval);
  const tab = () => container.children[1].children[0];
  assert.equal(tab().dataset.needsAction, "true");
  assert.match(tab().attributes["aria-label"], /Action required/);
  tabs.update([{...approval, unread_result: false}]);
  assert.equal(tab().dataset.needsAction, "true", "reading must not clear a pending approval");
  tabs.update([{...approval, response_state: "working", needs_attention: false}]);
  assert.equal(tab().dataset.needsAction, "false"); assert.equal(tab().dataset.status, "working");
  const blocker = {...approval, response_state: "action", attention_reason: "blocked"};
  tabs.update([blocker]); assert.equal(tab().dataset.needsAction, "true");
  tabs.update([{...blocker, needs_attention: false}]); assert.equal(tab().dataset.needsAction, "false");
  assert.doesNotMatch(tab().attributes["aria-label"], /Action required/);
});

test("restart preserves tab order and selection through the initial inventory refresh", async () => {
  const {tabs, storage} = setup();
  tabs.open(thread("a")); tabs.open(thread("b")); tabs.move("b", 0); tabs.open(thread("a"));
  const reopened = setup(storage);
  reopened.tabs.update([{...thread("a"), title: "Renamed"}]);
  assert.deepEqual(reopened.ids(), ["b", "a"]);
  assert.equal(reopened.tabs.destination(), "a");
  await reopened.tabs.activate(reopened.tabs.destination());
  assert.equal(reopened.tabs.selected, "a");
  await reopened.tabs.close("a");
  assert.equal(setup(storage).tabs.destination(), "b");
  await reopened.tabs.close("b");
  assert.equal(setup(storage).tabs.destination(), null);
  assert.deepEqual(setup(storage).ids(), []);
});

test("Overview, Scratchpad and temporary destinations survive restart safely", () => {
  const {tabs, storage} = setup();
  tabs.onScratchpad = () => {};
  tabs.open(thread("a")); tabs.overview();
  assert.equal(setup(storage).tabs.destination(), null);
  tabs.scratchpad();
  const reopened = setup(storage); reopened.tabs.onScratchpad = () => {};
  assert.equal(reopened.tabs.destination(), "project-scratchpad");
  tabs.open(thread("new-session"));
  assert.equal(setup(storage).tabs.destination(), null);
  tabs.replace("new-session", thread("created"));
  assert.equal(setup(storage).tabs.destination(), "created");
  tabs.suspend();
  assert.equal(setup(storage).tabs.destination(), null);
});

test("each window and project keeps its own saved selection", () => {
  const values = new Map(), storage = {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value)};
  const windowTabs = scope => new ThreadTabs({storage, scope});
  const first = windowTabs("native-0"), second = windowTabs("native-1");
  first.setProject("/work"); first.open(thread("a"));
  first.overview({remember: false}); first.setProject("/other"); first.open(thread("b"));
  second.setProject("/work"); second.open(thread("c"));
  const reopened = windowTabs("native-0");
  reopened.setProject("/work"); assert.equal(reopened.destination(), "a");
  reopened.setProject("/other"); assert.equal(reopened.destination(), "b");
  const other = windowTabs("native-1"); other.setProject("/work");
  assert.equal(other.destination(), "c");
});

test("legacy session tabs migrate once without reviving a closed persistent tab set", () => {
  const key = "agent-coord.thread-tabs.:/work", values = new Map();
  const storage = {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value)};
  const fallbackStorage = {getItem: () => JSON.stringify([thread("legacy")])};
  const tabs = new ThreadTabs({storage, fallbackStorage}); tabs.setProject("/work"); tabs.update([]);
  assert.deepEqual(JSON.parse(values.get(key)).items, [{thread_id: "legacy", title: "Thread legacy"}]);
  values.set(key, JSON.stringify({items: [], selected: null}));
  const reopened = new ThreadTabs({storage, fallbackStorage}); reopened.setProject("/work");
  assert.deepEqual(reopened.items, []);
  values.set(key, JSON.stringify({items: [thread("a")], selected: "missing"}));
  const stale = new ThreadTabs({storage}); stale.setProject("/work");
  assert.equal(stale.destination(), null);
});

test("tabs expose active, blocked, unread-response, and idle indicators", () => {
  const element = () => ({children: [], dataset: {}, scrollLeft: 0, classList: {add() {}},
    append(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
    setAttribute() {}, contains: () => false, querySelectorAll: () => []});
  const container = element(), document = {createElement: element};
  const tabs = new ThreadTabs({container, document}); tabs.setProject("/work");
  tabs.open({...thread("run"), response_state: "working", unread_result: true});
  tabs.open({...thread("blocked"), response_state: "input"});
  tabs.open({...thread("failed"), response_state: "failed"});
  tabs.open({...thread("answered"), response_state: "reply", unread_result: true});
  tabs.open({...thread("calm"), response_state: "available"});
  const state = id => {
    const wrapper = container.children.find(child => child.children[0].dataset.threadTab === id);
    return wrapper && {status: wrapper.children[0].dataset.status, unread: wrapper.children[0].dataset.unread};
  };
  assert.deepEqual(state("run"), {status: "working", unread: "true"});
  assert.deepEqual(state("blocked"), {status: "input", unread: "false"});
  assert.deepEqual(state("failed"), {status: "failed", unread: "false"});
  assert.deepEqual(state("answered"), {status: "reply", unread: "true"});
  assert.deepEqual(state("calm"), {status: "available", unread: "false"});
  tabs.update([{...thread("answered"), response_state: "reply", unread_result: false}]);
  assert.deepEqual(state("answered"), {status: "reply", unread: "false"});
});

test("startup restores the saved tab only when no explicit destination was requested", async () => {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const startup = source.slice(source.indexOf("  if (initialNavigation) await navigation.open"), source.indexOf("  state.dispatchReady = true;"));
  for (const destination of [{}, {initialNavigation: "agentcoord://overview"}, {initialThread: "linked"},
    {initialDispatcher: "dispatcher"}, {scratchpad: "1"}]) {
    const calls = [];
    const context = {initialNavigation: null, initialThread: "", initialDispatcher: null, initialMessage: null,
      ...destination, projectQuery: new URLSearchParams(destination.scratchpad ? "scratchpad=1" : ""),
      navigation: {open: async () => calls.push("link")}, select: async id => calls.push(id),
      openProjectScratchpad: () => calls.push("scratchpad"), restoreProjectTab: async () => calls.push("restore"), showError() {}};
    await vm.runInNewContext("(async () => {" + startup + "})()", context);
    assert.deepEqual(calls, [destination.initialNavigation ? "link" : destination.initialThread || destination.initialDispatcher
      || (destination.scratchpad ? "scratchpad" : "restore")]);
  }
});

test("a background draft completion updates only its original project's tabs and saved destination", () => {
  const {tabs, storage} = setup();
  tabs.open(thread("new-session"));
  tabs.setProject("/other"); tabs.open(thread("new-session"));
  tabs.replace("new-session", thread("created"), {cwd: "/work"});
  assert.equal(tabs.cwd, "/other");
  assert.equal(tabs.selected, "new-session");
  assert.deepEqual(tabs.items.map(item => item.thread_id), ["new-session"]);
  assert.deepEqual(tabs.projects.get("/work").map(item => item.thread_id), ["created"]);
  const reopened = setup(storage);
  assert.equal(reopened.tabs.destination(), "created");
  assert.deepEqual(reopened.ids(), ["created"]);
});

test("Overview offers one dropdown/context menu without navigating until a layout is selected", () => {
  const element = () => ({children: [], dataset: {}, attributes: {}, scrollLeft: 0, classList: {add() {}},
    append(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
    setAttribute(k, v) { this.attributes[k] = v; }, contains: () => false, querySelectorAll: () => []});
  const container = element(), opened = [], visits = [];
  const tabs = new ThreadTabs({container, document: {createElement: element}, onOverviewMenu: (anchor, event) => opened.push({anchor,event}), onSelect: id => visits.push(id)});
  tabs.setProject('/work');
  const overview = container.children[0], [tab, arrow] = overview.children;
  assert.equal(tab.textContent,'Overview'); assert.equal(arrow.attributes['aria-haspopup'],'menu');
  arrow.onclick({type:'click'}); overview.oncontextmenu({type:'contextmenu'});
  tab.onkeydown({key:'F10',shiftKey:true}); tab.onkeydown({key:'ArrowDown',altKey:true});
  assert.equal(opened.length,4); assert(opened.every(call => call.anchor === arrow)); assert.deepEqual(visits,[]);
  tab.onclick(); assert.deepEqual(visits,[null]);
});
