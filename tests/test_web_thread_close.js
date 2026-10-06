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
