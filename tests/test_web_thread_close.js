"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const grouping = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const {ThreadTabs} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-tabs.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const elements = new Map();
  const c = {listRequest: 0, $: id => { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); },
    state: {selected: "one", detail: {work_thread: {browser_session: true, attention: "now"}}, sessions: [{thread_id: "one"}], closing: new Set()},
    threadGrouping: grouping, renderSessionSettings() {}, refreshDetail: async () => {}, refreshList: async () => {},
    renderList() { c.rendered = c.state.sessions.filter(thread => !c.state.pendingCloses?.has(thread.thread_id)); },
    showError(error) { c.error = error; },
    threadPath: id => "threads/" + id, goHome() { c.state.selected = null; c.state.detail = null; c.home = true; }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), c);
  vm.runInContext(source.slice(source.indexOf("function renderStatus("), source.indexOf("function itemText(")), c);
  vm.runInContext(source.slice(source.indexOf("async function toggleThreadClosed("), source.indexOf('$("close-thread").onclick')), c);
  return c;
}

function withTabs(c, ids = ["one"]) {
  const storage = new Map(), visits = [];
  const tabs = new ThreadTabs({
    storage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    canNavigate: () => !c.state.busy,
    onSelect: id => {
      visits.push(id);
      if (id) { c.state.selected = id; tabs.open({thread_id: id}); }
      else c.goHome();
    }, onError: c.showError,
  });
  const goHome = c.goHome;
  c.goHome = () => { goHome(); tabs.overview(); };
  c.state.threadTabs = tabs;
  tabs.setProject("/work");
  for (const id of ids) tabs.open({thread_id: id});
  tabs.open({thread_id: c.state.selected});
  return {tabs, visits, ids: () => tabs.items.map(item => item.thread_id),
    savedIds: () => JSON.parse(storage.get(tabs.baseKey + ":/work")).items.map(item => item.thread_id)};
}

test("closing a middle thread removes its saved tab and selects the next tab to the right", async () => {
  const c = setup(), t = withTabs(c, ["left", "one", "right"]);
  c.api = async path => assert.equal(path, "threads/one/close");
  await c.toggleThreadClosed();
  assert.deepEqual(t.ids(), ["left", "right"]);
  assert.deepEqual(t.savedIds(), ["left", "right"]);
  assert.deepEqual(t.visits, ["right"]);
  assert.equal(c.state.selected, "right");
  assert.equal(c.home, undefined);
});

test("closing the rightmost thread selects the tab to its left even during /close", async () => {
  const c = setup(), t = withTabs(c, ["left", "one"]);
  c.state.busy = true;
  c.api = async () => ({});
  await c.toggleThreadClosed();
  assert.deepEqual(t.ids(), ["left"]);
  assert.deepEqual(t.visits, ["left"]);
  assert.equal(c.state.selected, "left");
  assert.equal(c.home, undefined);
});

test("closing the last thread removes its tab and returns to Overview", async () => {
  const c = setup(), t = withTabs(c);
  c.api = async () => ({});
  await c.toggleThreadClosed();
  assert.deepEqual(t.ids(), []);
  assert.deepEqual(t.savedIds(), []);
  assert.deepEqual(t.visits, [null]);
  assert.equal(c.state.selected, null);
  assert.equal(c.home, true);
});

test("a failed close preserves the tab, its order, and selection", async () => {
  const c = setup(), t = withTabs(c, ["left", "one", "right"]);
  c.api = async () => { throw new Error("Shutdown failed"); };
  await assert.rejects(c.toggleThreadClosed(), /Shutdown failed/);
  assert.deepEqual(t.ids(), ["left", "one", "right"]);
  assert.deepEqual(t.savedIds(), ["left", "one", "right"]);
  assert.deepEqual(t.visits, []);
  assert.equal(t.tabs.selected, "one");
  assert.equal(c.state.selected, "one");
});

test("navigating during shutdown removes the closed tab without moving the new selection", async () => {
  const c = setup(), t = withTabs(c, ["one", "two", "three"]);
  let finishClose;
  c.api = () => new Promise(resolve => { finishClose = resolve; });
  const closing = c.toggleThreadClosed();
  assert.deepEqual(t.ids(), ["one", "two", "three"]);
  await t.tabs.activate("three");
  finishClose({});
  await closing;
  assert.deepEqual(t.ids(), ["two", "three"]);
  assert.deepEqual(t.visits, ["three"]);
  assert.equal(c.state.selected, "three");
});

test("reopening a thread keeps its tab and selection", async () => {
  const c = setup(), t = withTabs(c, ["one", "two"]);
  c.state.detail.work_thread.attention = "archived";
  c.api = async path => assert.equal(path, "threads/one/reopen");
  await c.toggleThreadClosed();
  assert.deepEqual(t.ids(), ["one", "two"]);
  assert.deepEqual(t.visits, []);
  assert.equal(c.state.selected, "one");
});

test("closing removes the selected thread and returns to the open overview", async () => {
  const c = setup();
  c.$("view").value = "completed";
  c.api = async (path, body) => { assert.equal(path, "threads/one/close"); assert.equal(Object.keys(body).length, 0); };
  await c.toggleThreadClosed();
  assert.equal(c.home, true);
  assert.equal(c.$("view").value, "active");
  assert.equal(c.state.sessions.length, 0);
  assert.equal(c.state.closing.size, 0);
});

test("reopening keeps the conversation selected and restores the open view", async () => {
  const c = setup();
  c.state.detail.work_thread.attention = "archived";
  c.api = async path => assert.equal(path, "threads/one/reopen");
  await c.toggleThreadClosed();
  assert.equal(c.state.selected, "one");
  assert.equal(c.$("view").value, "active");
  assert.equal(c.home, undefined);
});

test("a failed close keeps the thread and releases the busy indicator", async () => {
  const c = setup();
  c.api = async () => { throw new Error("Shutdown failed"); };
  await assert.rejects(c.toggleThreadClosed(), /Shutdown failed/);
  assert.equal(c.state.selected, "one");
  assert.equal(c.state.sessions.length, 1);
  assert.equal(c.state.closing.size, 0);
});

test("a slow close immediately hides the card and a failure restores it", async () => {
  const c = setup(); let rejectClose;
  c.state.selected = null;
  c.api = () => new Promise((resolve, reject) => { rejectClose = reject; });
  const closing = c.toggleThreadClosed({thread_id: "one", attention: "now"});
  assert.equal(c.rendered.length, 0, "render before waiting for shutdown");
  assert.equal(c.state.closing.has("one"), true);
  assert.equal(c.listRequest, 1, "invalidate inventory reads started before the click");
  rejectClose(new Error("Still stopping"));
  await assert.rejects(closing, /Still stopping/);
  assert.equal(c.rendered[0].thread_id, "one");
  assert.equal(c.state.closing.size, 0);
  assert.equal(c.listRequest, 2, "invalidate inventory reads started during shutdown");
});

test("a confirmed close updates cards and badges without waiting for an inventory refresh", async () => {
  const c = setup(); let rejectRefresh;
  c.state.selected = null;
  c.state.viewThreads = [...c.state.sessions];
  c.api = async () => ({});
  c.refreshList = () => new Promise((resolve, reject) => { rejectRefresh = reject; });
  let completed = false;
  const closing = c.toggleThreadClosed({thread_id: "one", attention: "now"}).then(() => { completed = true; });
  for (let i = 0; i < 5; i++) await Promise.resolve();
  assert.equal(completed, true);
  assert.equal(c.state.sessions.length, 0);
  assert.equal(c.state.viewThreads.length, 0);
  assert.equal(c.state.closing.size, 0);
  rejectRefresh(new Error("Inventory unavailable"));
  await closing;
  assert.equal(c.error.message, "Inventory unavailable");
  assert.equal(c.state.sessions.length, 0, "refresh failure must not undo a successful close");
});

test("concurrent closes reconcile independently and suppress duplicate requests", async () => {
  const c = setup(), pending = new Map();
  c.state.selected = null;
  c.state.sessions.push({thread_id: "two"}, {thread_id: "three"});
  c.api = path => new Promise((resolve, reject) => { pending.set(path, {resolve, reject}); });
  const one = c.toggleThreadClosed({thread_id: "one", attention: "now"});
  const two = c.toggleThreadClosed({thread_id: "two", attention: "now"});
  await c.toggleThreadClosed({thread_id: "one", attention: "now"});
  assert.equal(pending.size, 2);
  assert.deepEqual(c.rendered.map(t => t.thread_id), ["three"]);
  pending.get("threads/two/close").resolve({}); await two;
  pending.get("threads/one/close").reject(new Error("Offline"));
  await assert.rejects(one, /Offline/);
  assert.deepEqual(c.rendered.map(t => t.thread_id), ["one", "three"]);
  assert.equal(c.state.closing.size, 0);
});

test("navigating during shutdown does not close the newly selected conversation", async () => {
  const c = setup();
  c.api = async () => { c.state.selected = "two"; };
  await c.toggleThreadClosed();
  assert.equal(c.state.selected, "two");
  assert.equal(c.home, undefined);
});

test("overview close targets the card without opening it or touching another selected thread", async () => {
  const c = setup();
  c.state.selected = "another";
  c.api = async path => assert.equal(path, "threads/card-thread/close");
  await c.toggleThreadClosed({thread_id: "card-thread", attention: "now"});
  assert.equal(c.state.selected, "another");
  assert.equal(c.home, undefined);
  c.state.selected = null;
  c.state.detail = null;
  await c.toggleThreadClosed({thread_id: "card-thread", attention: "now"});
  assert.equal(c.state.selected, null);
  assert.equal(c.home, undefined);
});

test("running and pending-input threads offer Stop and close; closed history offers Reopen", () => {
  const c = setup();
  for (const status of ["running", "needs input"]) {
    c.state.detail.work_thread.status = status;
    c.renderStatus();
    assert.equal(c.$("close-thread").textContent, "Stop and close");
    assert.equal(c.$("close-thread").disabled, false);
  }
  c.state.detail.work_thread.attention = "archived";
  c.renderStatus();
  assert.equal(c.$("close-thread").textContent, "Reopen");
  assert.equal(c.$("composer-hint").textContent, "Reopen this thread to continue.");
  assert.equal(grouping.status(c.state.detail.work_thread).label, "Closed");
  c.state.closing.add("one");
  c.renderStatus();
  assert.equal(c.$("close-thread").disabled, true);
});

test("Undo restores Later and its future reminder without restarting an agent", async () => {
  const c = setup(), calls = [], deadline = Date.now() / 1000 + 3600;
  let undo, placement = "later", animated = 0;
  c.state.threadActions = {captureClose: () => "snapshot", closed: value => { assert.equal(value, "snapshot"); animated++; },
    notify: (key, text, callback) => { undo = callback; }};
  c.api = async (path, body) => {
    calls.push({path, body: body && JSON.parse(JSON.stringify(body))});
    if (path.endsWith("/close")) placement = "archived";
    else if (path.endsWith("/reopen")) placement = "now";
    else if (body?.snoozed_until) placement = "later";
    return {attention: placement};
  };
  await c.toggleThreadClosed({thread_id: "one", title: "HPA", status: "running", attention: "later", snoozed_until: deadline});
  assert.equal(animated, 1); assert.equal(placement, "archived");
  await undo(); assert.equal(placement, "later");
  assert.deepEqual(calls.map(call => call.path), ["threads/one/close", "threads/one", "threads/one/reopen", "threads/one"]);
  assert.deepEqual(calls.at(-1).body, {snoozed_until: deadline});
  assert.equal(c.state.closing.size, 0);
});

test("a failed close never offers Undo or animates", async () => {
  const c = setup();
  c.state.threadActions = {captureClose() {}, closed: () => assert.fail("failed close cannot animate"),
    notify: () => assert.fail("failed close cannot offer Undo")};
  c.api = async () => { throw new Error("Offline"); };
  await assert.rejects(c.toggleThreadClosed(), /Offline/);
});

test("external closure explains that Codex work may continue and retains Undo", async () => {
  const c = setup(); let notice, undo;
  c.state.threadActions = {captureClose() {}, closed() {}, notify: (key, text, callback) => { notice = text; undo = callback; }};
  c.api = async () => ({close_notice: "Thread closed in Ribbon Field. Work may continue in Codex."});
  await c.toggleThreadClosed({thread_id: "one", title: "External", status: "running", attention: "now"});
  assert.equal(notice, "Thread closed in Ribbon Field. Work may continue in Codex.");
  assert.equal(typeof undo, "function");
});

test("Undo does not overwrite a thread already reopened elsewhere", async () => {
  const c = setup(); let undo;
  c.state.threadActions = {captureClose() {}, closed() {}, notify: (key, text, callback) => { undo = callback; }};
  c.api = async () => ({});
  await c.toggleThreadClosed({thread_id: "one", attention: "later"});
  c.api = async (path, body) => { assert.equal(body, undefined); return {attention: "now"}; };
  await undo();
  assert.equal(c.state.closing.size, 0);
});
