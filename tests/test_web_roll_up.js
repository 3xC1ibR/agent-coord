"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {RollUp, setupRollUp} = require("../plugins/agent-coord/scripts/agent_coord/web/roll-up.js");
const {inProject} = require("../plugins/agent-coord/scripts/agent_coord/web/folders.js");
const thread = (id, since, extra = {}) => ({thread_id: id, attention: "now", needs_attention: true,
  attention_since: since, attention_key: id + ":1", ...extra});
function setup(threads) {
  let selected = null;
  const opened = [], data = {threads};
  const roll = new RollUp({load: async () => data.threads, selected: () => selected,
    open: async id => { selected = id; opened.push(id); }});
  return {roll, data, opened, selected: id => { selected = id; }};
}

test("a grouped escalation opens its child and advances using the parent receipt", async () => {
  const {roll, data, opened} = setup([thread("parent", 1, {attention_target_thread_id: "child"}), thread("other", 2)]);
  await roll.start();
  assert.deepEqual(opened, ["child"]);
  const ticket = roll.ticket();
  assert.equal(ticket.id, "child");
  data.threads[0] = thread("parent", 1, {attention_target_thread_id: "second-child", attention_key: "parent:2"});
  await roll.responded(ticket);
  assert.deepEqual(opened, ["child", "other"]);
  await roll.responded(roll.ticket());
  assert.deepEqual(opened, ["child", "other", "second-child"]);
});

test("oldest waiting request wins across projects, excluding quiet, Later and Closed", async () => {
  const {roll, opened} = setup([thread("new", 20, {created_at: 1}), thread("old", 10, {project_id: "elsewhere", created_at: 99}),
    thread("later", 1, {attention: "later"}), thread("closed", 1, {attention: "archived"}),
    thread("info", 1, {needs_attention: false})]);
  await roll.start();
  assert.deepEqual(opened, ["old"]);
  assert.equal(roll.queue.length, 2);
  await roll.responded(roll.ticket());
  assert.deepEqual(opened, ["old", "new"]);
  await roll.responded(roll.ticket());
  assert.equal(roll.active, false);
  assert.equal(roll.done, true);
  assert.deepEqual(opened, ["old", "new"]);
});

test("refreshes never navigate; arrivals append, and a fast reply goes behind waiting work", async () => {
  const {roll, data, opened} = setup([thread("one", 1), thread("two", 2)]);
  await roll.start();
  const ticket = roll.ticket();
  data.threads.push(thread("arrival", 0));
  roll.sync(data.threads);
  assert.deepEqual(opened, ["one"]);
  assert.deepEqual(roll.queue.map(t => t.thread_id), ["one", "two", "arrival"]);
  data.threads[0] = thread("one", 5, {attention_key: "one:2"});
  await roll.responded(ticket);
  assert.deepEqual(roll.queue.map(t => t.thread_id), ["two", "arrival", "one"]);
});

test("skip excludes a thread for the entire pass without changing its attention", async () => {
  const {roll, data, opened} = setup([thread("one", 1), thread("two", 2)]);
  await roll.start(); await roll.skip();
  data.threads[0].attention_key = "one:2";
  roll.sync(data.threads);
  assert.deepEqual(roll.queue.map(t => t.thread_id), ["two"]);
  assert.equal(data.threads[0].needs_attention, true);
  await roll.skip();
  assert.equal(roll.done, true);
  await roll.start();
  assert.deepEqual(opened, ["one", "two", "one"]);
});

test("multiple unanswered requests keep the same conversation open", async () => {
  const {roll, opened} = setup([thread("one", 1), thread("two", 2)]);
  await roll.start();
  await roll.responded(roll.ticket(), {pending: true});
  assert.deepEqual(opened, ["one"]);
  await roll.responded(roll.ticket());
  assert.deepEqual(opened, ["one", "two"]);
});

test("external handling removes candidates without moving the current conversation", async () => {
  const {roll, data, opened} = setup([thread("one", 1), thread("two", 2), thread("three", 3)]);
  await roll.start(); data.threads[1].needs_attention = false; roll.sync(data.threads);
  assert.deepEqual(opened, ["one"]);
  await roll.responded(roll.ticket());
  assert.deepEqual(opened, ["one", "three"]);
});

test("a new response observed between polls rejoins at the tail", async () => {
  const {roll, data, opened} = setup([thread("one", 1), thread("two", 2), thread("three", 3)]);
  await roll.start();
  data.threads[1] = thread("two", 4, {attention_key: "two:2"});
  roll.sync(data.threads);
  assert.deepEqual(roll.queue.map(t => t.thread_id), ["one", "three", "two"]);
  assert.deepEqual(opened, ["one"]);
});

test("exiting during a load prevents delayed navigation and new passes ignore old submissions", async () => {
  const {roll, opened} = setup([thread("one", 1)]);
  let finish;
  roll.load = () => new Promise(resolve => { finish = resolve; });
  const start = roll.start(); roll.stop(); finish([thread("one", 1)]); await start;
  assert.deepEqual(opened, []);
  roll.load = async () => [thread("one", 1), thread("two", 2)];
  await roll.start(); const ticket = roll.ticket(); roll.stop(); await roll.start();
  await roll.responded(ticket);
  assert.deepEqual(opened, ["one", "one"]);
});

test("manual navigation and double submission cannot advance the wrong thread", async () => {
  const {roll, selected, opened} = setup([thread("one", 1), thread("two", 2)]);
  await roll.start(); const ticket = roll.ticket();
  selected("elsewhere"); await roll.responded(ticket);
  assert.deepEqual(opened, ["one"]);
  selected("one"); await Promise.all([roll.responded(ticket), roll.responded(ticket)]);
  assert.deepEqual(opened, ["one", "two"]);
});

test("load failures retain mode and allow retry, while empty passes complete", async () => {
  const {roll, opened} = setup([]);
  roll.load = async () => { throw new Error("Offline"); };
  await assert.rejects(roll.start(), /Offline/);
  assert.equal(roll.active, true); assert.equal(roll.busy, false);
  roll.load = async () => [];
  await roll.advance();
  assert.equal(roll.done, true); assert.deepEqual(opened, []);
});

test("each window owns an independent pass", async () => {
  const one = setup([thread("one", 1), thread("two", 2)]), two = setup([thread("one", 1), thread("two", 2)]);
  await one.roll.start(); await two.roll.start(); await one.roll.skip();
  assert.deepEqual(one.opened, ["one", "two"]); assert.deepEqual(two.opened, ["one"]);
});

test("ordinary navigation and pane status events do not redraw an inactive mode", () => {
  const {roll} = setup([]);
  let changes = 0;
  roll.changed = () => changes++;
  roll.stop(); roll.stop();
  assert.equal(changes, 0);
});

test("controls handle empty passes, skipped work, keyboard exit, and modal precedence", async () => {
  const nodes = new Map(), handlers = {}, opened = [];
  let dialog = false, menu = false;
  const document = {getElementById: id => {
    if (!nodes.has(id)) nodes.set(id, {hidden: false, setAttribute() {}, focus() {}, querySelector: () => null});
    return nodes.get(id);
  }, querySelector: selector => selector === "dialog[open]" ? dialog : menu,
  addEventListener: (name, fn) => { handlers[name] = fn; }};
  let items = [], operation;
  const state = {updatingThreads: new Set()};
  const roll = setupRollUp({document, state, api: async () => ({data: items}), panes: () => null,
    select: async id => { state.selected = id; opened.push(id); }, action: fn => { operation = fn(); }});
  nodes.get("roll-up-start").onclick(); await operation;
  assert.equal(nodes.get("roll-up-label").textContent, "All caught up");
  nodes.get("roll-up-exit").onclick();
  assert.equal(nodes.get("roll-up-bar").hidden, true);
  items = [thread("one", 1), thread("two", 2)];
  const key = {key: "®", code: "KeyR", altKey: true, metaKey: true, preventDefault() {}};
  dialog = true; handlers.keydown(key); assert.equal(roll.active, false);
  dialog = false; handlers.keydown(key); await operation;
  assert.deepEqual(opened, ["one"]);
  menu = true; handlers.keydown({key: "Escape", preventDefault() {}}); assert.equal(roll.active, true);
  menu = false;
  nodes.get("roll-up-skip").onclick(); await operation;
  nodes.get("roll-up-skip").onclick(); await operation;
  assert.equal(nodes.get("roll-up-label").textContent, "Pass complete · 2 skipped");
  handlers.keydown({key: "Escape", preventDefault() {}});
  assert.equal(nodes.get("roll-up-bar").hidden, true);
});

function appFixture() {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const elements = new Map(), advanced = [];
  const c = {listRequest: 0, state: {selected: "one", drafts: new Map(), commandFeedback: new Map(), updatingThreads: new Set(),
    sessions: [], detail: {work_thread: {thread_id: "one", browser_session: true, can_handle_response: true}, requests: []},
    rollUp: {ticket: id => ({id}), responded: async (ticket, options) => advanced.push({ticket, options})}},
    $: id => { if (!elements.has(id)) elements.set(id, {value: "A reply"}); return elements.get(id); },
    renderStatus() {}, renderList() {}, renderThread() {}, refreshDetail: async () => {}, refreshList: async () => {},
    sessionPath: id => "sessions/" + id, threadPath: id => "threads/" + id,
    api: async () => ({}), node: () => ({}), action: fn => fn()};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), c);
  for (const [start, end] of [["async function sendMessage(", '$("composer").onsubmit'],
    ["function requestButton(", "function renderRequests("],
    ["async function updateThreadAttention(", "function threadCard("]]) {
    vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end)), c);
  }
  return {c, advanced};
}

test("accepted messages advance, slash commands and failed sends do not", async () => {
  for (const outcome of ["accepted", "command", "failed"]) {
    const {c, advanced} = appFixture();
    c.api = async () => {
      if (outcome === "failed") throw new Error("Failed send");
      return outcome === "command" ? {command: {message: "Help"}} : {};
    };
    if (outcome === "failed") {
      await assert.rejects(c.sendMessage(), /Failed send/);
      assert.equal(c.$("message").value, "A reply");
    } else await c.sendMessage();
    assert.equal(advanced.length, outcome === "accepted" ? 1 : 0, outcome);
  }
});

test("response hooks pass remaining approvals to the queue and never send to a newly selected thread", async () => {
  const {c, advanced} = appFixture();
  const button = c.requestButton("Approve", "accept", {key: "approval"});
  c.state.detail.requests = [{key: "another"}];
  c.api = async path => { assert.equal(path, "sessions/one/requests/approval"); return {}; };
  await button.onclick();
  assert.equal(advanced[0].options.pending, true);
  c.state.selected = "two";
  await button.onclick();
  assert.equal(advanced.at(-1).ticket.id, "one");
});

test("Handled advances only after a successful acknowledgement", async () => {
  const {c, advanced} = appFixture();
  const thread = c.state.detail.work_thread;
  c.api = async () => { throw new Error("Offline"); };
  await assert.rejects(c.markThreadHandled(thread), /Offline/);
  assert.equal(advanced.length, 0);
  c.api = async () => ({...thread, needs_attention: false});
  await c.markThreadHandled(thread);
  assert.equal(advanced.length, 1);
});

function projectAppFixture({known = true} = {}) {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const items = [thread("second", 20, {project_cwd: "/second"}), thread("first", 10, {project_cwd: "/first"}),
    thread("third", 30, {project_cwd: "/third"}), thread("later", 1, {project_cwd: "/first", attention: "later"}),
    thread("quiet", 2, {project_cwd: "/first", needs_attention: false})];
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {value: "", hidden: false, dataset: {}, options: [],
      classList: {remove() {}}, setAttribute() {}, focus() {}, querySelector: () => null,
      replaceChildren(...options) { this.options = options; }, append(option) { this.options.push(option); }});
    return elements.get(id);
  };
  const c = {listRequest: 0, state: {projectCwd: "/home", selected: null, drafts: new Map(), updatingThreads: new Set(),
    sessions: known ? items.slice() : [], viewThreads: [],
    folders: {selected: {cwd: "/home"}, adopt(folder) { this.selected = folder; }}}, $, items,
    document: {getElementById: $, querySelector: () => null, addEventListener() {}}, setupRollUp,
    Option: function (text, value) { Object.assign(this, {text, value}); },
    inProject,
    savedViews: {remember() {}, restoreScroll() {}, sync() {}, setProject: async () => {}},
    navigation: {remember() {}}, notifications: {syncFocus() {}}, threadOrganization: {NONE: "__none__"},
    renderFolders() {}, renderList() {}, renderStatus() {}, renderSessionSettings() {}, renderTitle() {}, setNavigation() {}, setChatExpanded() {},
    retainConversation() {}, restoreProjectTab: async () => {}, conversationEntry: () => ({detail: null}), node: () => ({}),
    overlayPendingAttention: item => item, threadPath: id => "threads/" + id, action: fn => fn(), markThreadHandled() {},
    refreshDetail: async () => { c.state.detail = {work_thread: items.find(item => item.thread_id === c.state.selected)}; },
    api: async (path, body) => {
      if (path.startsWith("threads?")) return {data: items.slice()};
      if (path === "organization") return {repositories: [], projects: []};
      if (path === "views") return {data: []};
      if (path.startsWith("threads/") && body?.seen) return {};
      throw new Error("Unexpected API call: " + path);
    }};
  vm.createContext(c);
  for (const [start, end] of [["async function refreshList(", "function currentProjectDispatcher("],
    ["function threadsInProject(", "async function restoreProjectTab("],
    ["async function openProjectDirectory(", "function renderFolders("],
    ["async function select(", "async function refreshDetail("],
    ["function goHome(", "function openProjectScratchpad("],
    ["  setupRollUp(", '  window.addEventListener("agent-coord-pane-state"']]) {
    const from = source.indexOf(start), to = source.indexOf(end, from);
    assert.ok(from >= 0 && to > from, "Load actual app integration: " + start);
    vm.runInContext(source.slice(from, to), c);
  }
  return c;
}
const queued = c => Array.from(c.state.rollUp.queue, item => item.thread_id);

test("app Roll up retains all projects and queue order through refresh and cross-project advance", async () => {
  const c = projectAppFixture(), roll = c.state.rollUp;
  await roll.start();
  assert.equal(c.state.selected, "first");
  assert.equal(c.state.projectCwd, "/first");
  assert.equal(roll.active, true);
  assert.equal(c.$("roll-up-bar").hidden, false);
  assert.deepEqual(queued(c), ["first", "second", "third"]);
  c.items.push(thread("arrival", 5, {project_cwd: "/arrival"}));
  await c.refreshList();
  assert.equal(c.state.selected, "first", "background refresh must not navigate");
  assert.deepEqual(queued(c), ["first", "second", "third", "arrival"], "arrivals append after retained work");
  c.$("message").value = "Unsent draft";
  await roll.responded(roll.ticket());
  assert.equal(c.state.selected, "second");
  assert.equal(c.state.projectCwd, "/second");
  assert.equal(c.state.drafts.get("first"), "Unsent draft");
  assert.equal(roll.active, true);
  assert.deepEqual(queued(c), ["second", "third", "arrival"]);
  await roll.skip();
  assert.equal(c.state.selected, "third");
  assert.equal(c.state.projectCwd, "/third");
  assert.deepEqual(queued(c), ["third", "arrival"]);
});

test("app Roll up preserves its pass when a thread's project is learned from fresh detail", async () => {
  const c = projectAppFixture({known: false});
  await c.state.rollUp.start();
  assert.equal(c.state.selected, "first");
  assert.equal(c.state.projectCwd, "/first");
  assert.equal(c.state.rollUp.active, true);
  assert.deepEqual(queued(c), ["first", "second", "third"]);
});

test("manual project or conversation navigation still stops the app Roll up pass", async () => {
  for (const destination of ["project", "conversation"]) {
    const c = projectAppFixture();
    await c.state.rollUp.start();
    c.$("message").value = "Keep my draft";
    if (destination === "project") {
      c.state.folders.adopt({cwd: "/manual"});
      await c.switchProject();
      assert.equal(c.state.projectCwd, "/manual");
      assert.equal(c.state.selected, null);
    } else {
      await c.select("second");
      assert.equal(c.state.projectCwd, "/second");
      assert.equal(c.state.selected, "second");
    }
    assert.equal(c.state.rollUp.active, false, destination);
    assert.deepEqual(queued(c), [], destination);
    assert.equal(c.$("roll-up-bar").hidden, true, destination);
    assert.equal(c.state.drafts.get("first"), "Keep my draft", destination);
  }
});

test("opening a cached conversation in another project paints before that project's thread list loads", async () => {
  const c = projectAppFixture();
  const listed = [];
  let finishList;
  const api = c.api;
  c.api = (path, body) => {
    if (!path.startsWith("threads?")) return api(path, body);
    listed.push(path);
    return new Promise(resolve => { finishList = () => resolve({data: c.items.slice()}); });
  };
  const painted = [], order = [], frames = [];
  c.conversationEntry = id => ({detail: id === "second" ? {work_thread: c.items.find(item => item.thread_id === id)} : null,
    nodes: id === "second" ? [] : null});
  c.renderThread = () => { painted.push(c.state.selected); order.push("conversation"); };
  c.renderList = () => order.push("overview");
  c.requestAnimationFrame = callback => frames.push(callback);
  c.setTimeout = callback => callback();
  c.renderQueuedMessages = () => {};
  c.$("timeline").replaceChildren = () => {};
  c.refreshDetail = async () => true;
  const opening = c.select("second", {refreshInventory: false});
  for (let turn = 0; turn < 5 && !painted.length; turn++) await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(painted, ["second"], "the cached conversation is shown while the list read is still pending");
  assert.deepEqual(order, ["conversation"], "the overview is not redrawn before the conversation's frame");
  frames.splice(0).forEach(callback => callback());
  assert.deepEqual(order, ["conversation", "overview"]);
  assert.equal(c.state.projectCwd, "/second");
  assert.equal(listed.length, 1);
  finishList();
  await opening;
});
