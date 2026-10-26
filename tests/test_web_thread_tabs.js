"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const {ThreadTabs} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-tabs.js");
const thread = id => ({thread_id: id, title: "Thread " + id, attention: "now", response_state: "working"});

// Exercise the actual startup wiring with a new session store on every launch,
// as happens when iPadOS discards a home-screen app's browser session.
function launchBrowser(preferences, session, desktop, remote = true) {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const context = {state: {config: {workspaceRoot: "", remote}}, localStorage: preferences, sessionStorage: session,
    window: {agentCoordDesktop: desktop}, document: undefined, $() {}, api: async (_route, body) => ({cwd: body.path}),
    showError() {}, openProjectScratchpad() {}, ThreadTabs,
    WorkingFolders: require("../plugins/agent-coord/scripts/agent_coord/web/folders.js").WorkingFolders};
  const storageSetup = source.slice(source.indexOf("  let windowStorage, preferences;"), source.indexOf("  state.overviewPreferences ="));
  const folderSetup = source.slice(source.indexOf("  const desktop = window.agentCoordDesktop;", source.indexOf("  let windowStorage, preferences;")), source.indexOf("  const projectQuery ="));
  const tabSetup = source.slice(source.indexOf("  state.threadTabs = new ThreadTabs("), source.indexOf("  state.threadTabs.setProject("));
  vm.runInNewContext(storageSetup + folderSetup + tabSetup, context);
  return context.state;
}

const browserStorage = () => {
  const values = new Map();
  return {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)};
};

test("web app relaunch retains projects, ordered tabs, and selected conversation after session storage is lost", async () => {
  for (const remote of [true, false]) {
    const preferences = browserStorage();
    const first = launchBrowser(preferences, browserStorage(), undefined, remote);
    await first.folders.open("/work"); await first.folders.open("/other"); first.folders.activate("/work");
    first.threadTabs.setProject("/work");
    first.threadTabs.open(thread("a")); first.threadTabs.open(thread("b")); first.threadTabs.move("b", 0);
    const reopened = launchBrowser(preferences, browserStorage(), undefined, remote);
    await reopened.folders.restore(); reopened.threadTabs.setProject(reopened.folders.selected.cwd);
    assert.deepEqual([...reopened.folders.snapshot().projects], ["/work", "/other"]);
    assert.equal(reopened.folders.selected.cwd, "/work");
    assert.deepEqual(reopened.threadTabs.items.map(item => item.thread_id), ["b", "a"]);
    assert.equal(reopened.threadTabs.destination(), "b");
    reopened.threadTabs.open(thread("a"));
    await reopened.threadTabs.close("b");
    const closed = launchBrowser(preferences, browserStorage(), undefined, remote);
    closed.threadTabs.setProject("/work");
    assert.deepEqual(closed.threadTabs.items.map(item => item.thread_id), ["a"]);
    assert.equal(closed.threadTabs.destination(), "a");
  }
});

test("web app migrates existing session tabs and projects before the next cold launch", async () => {
  const preferences = browserStorage(), session = browserStorage();
  session.setItem("agent-coord.folder.browser:", JSON.stringify({projects: [{cwd: "/work"}], active: "/work"}));
  session.setItem("agent-coord.thread-tabs.:browser:/work", JSON.stringify({items: [thread("legacy")], selected: "legacy"}));
  const migrated = launchBrowser(preferences, session);
  await migrated.folders.restore(); migrated.threadTabs.setProject(migrated.folders.selected.cwd); migrated.threadTabs.update([]);
  const reopened = launchBrowser(preferences, browserStorage());
  reopened.threadTabs.setProject(reopened.folders.selected.cwd);
  assert.deepEqual(reopened.threadTabs.items.map(item => item.thread_id), ["legacy"]);
  assert.equal(reopened.threadTabs.destination(), "legacy");
});

test("native conversation windows keep temporary tabs isolated from persistent main windows", async () => {
  const preferences = browserStorage(), session = browserStorage();
  const desktop = {windowId: "conversation-1", conversationWindow: true};
  const first = launchBrowser(preferences, session, desktop);
  await first.folders.open("/work"); first.threadTabs.setProject("/work"); first.threadTabs.open(thread("temporary"));
  const main = launchBrowser(preferences, session, {windowId: "main", folderSlot: "native-0"});
  main.threadTabs.setProject("/work");
  assert.deepEqual(main.threadTabs.items, []);
  const reopened = launchBrowser(preferences, browserStorage(), desktop);
  assert.equal(reopened.folders.selected, null);
  reopened.threadTabs.setProject("/work"); assert.deepEqual(reopened.threadTabs.items, []);
});

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

test("agent-tree selection keeps the original tab identity and persists its displayed conversation", () => {
  const {tabs, ids, storage} = setup();
  tabs.open(thread("orchestrator")); tabs.setTree("orchestrator", true);
  assert.equal(tabs.selectConversation("orchestrator", "child"), true);
  assert.deepEqual(ids(), ["orchestrator"]);
  assert.equal(tabs.selected, "orchestrator");
  assert.equal(tabs.items[0].title, "Thread orchestrator");
  assert.equal(tabs.conversationFor("orchestrator"), "child");
  tabs.update([{...thread("child"), title: "Renamed child"}]);
  assert.equal(tabs.items[0].title, "Thread orchestrator");
  const restored = setup(storage).tabs;
  assert.equal(restored.destination(), "orchestrator");
  assert.equal(restored.conversationFor("orchestrator"), "child");
  assert.equal(restored.items[0].tree_root, "orchestrator");
  assert.equal(restored.items[0].tree_open, true);
  assert.equal(restored.selectConversation("missing", "child"), false);
  assert.equal(restored.conversationFor("missing"), "missing");
});

test("a child can have its own tab and closing the family tab keeps that child tab", async () => {
  const {tabs, ids, visits} = setup();
  tabs.open(thread("root")); tabs.open(thread("child"));
  tabs.selectConversation("root", "child");
  await tabs.close("root");
  assert.deepEqual(ids(), ["child"]);
  assert.equal(tabs.selected, "child");
  assert.deepEqual(visits, ["child"]);
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
      state: {threadTabs: {destination: () => null}},
      ...destination, projectQuery: new URLSearchParams(destination.scratchpad ? "scratchpad=1" : ""),
      navigation: {open: async () => calls.push("link")}, select: async id => calls.push(id),
      openProjectScratchpad: () => calls.push("scratchpad"), restoreProjectTab: async () => calls.push("restore"), showError() {}};
    await vm.runInNewContext("(async () => {" + startup + "})()", context);
    assert.deepEqual(calls, [destination.initialNavigation ? "link" : destination.initialThread || destination.initialDispatcher
      || (destination.scratchpad ? "scratchpad" : "restore")]);
  }
});

test("reload of a selected child restores the family tab instead of creating a child tab", async () => {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const startup = source.slice(source.indexOf("  if (initialNavigation) await navigation.open"), source.indexOf("  state.dispatchReady = true;"));
  const calls = [], context = {initialNavigation: null, initialThread: "child", initialDispatcher: null, initialMessage: null,
    state: {threadTabs: {destination: () => "root", conversationFor: () => "child"}},
    projectQuery: new URLSearchParams(), select: async (id, options) => calls.push([id, options.tabId])};
  await vm.runInNewContext("(async () => {" + startup + "})()", context);
  assert.deepEqual(calls, [["child", "root"]]);
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
