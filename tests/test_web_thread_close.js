"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const grouping = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const elements = new Map();
  const c = {$: id => { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); },
    state: {selected: "one", detail: {work_thread: {browser_session: true, attention: "now"}}, sessions: [{thread_id: "one"}], closing: new Set()},
    threadGrouping: grouping, renderSessionSettings() {}, refreshDetail: async () => {}, refreshList: async () => {},
    threadPath: id => "threads/" + id, goHome() { c.state.selected = null; c.state.detail = null; c.home = true; }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), c);
  vm.runInContext(source.slice(source.indexOf("function renderStatus("), source.indexOf("function itemText(")), c);
  vm.runInContext(source.slice(source.indexOf("async function toggleThreadClosed("), source.indexOf('$("close-thread").onclick')), c);
  return c;
}

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

test("Undo does not overwrite a thread already reopened elsewhere", async () => {
  const c = setup(); let undo;
  c.state.threadActions = {captureClose() {}, closed() {}, notify: (key, text, callback) => { undo = callback; }};
  c.api = async () => ({});
  await c.toggleThreadClosed({thread_id: "one", attention: "later"});
  c.api = async (path, body) => { assert.equal(body, undefined); return {attention: "now"}; };
  await undo();
  assert.equal(c.state.closing.size, 0);
});
