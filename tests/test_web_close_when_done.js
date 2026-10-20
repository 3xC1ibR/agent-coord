"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const button = {setAttribute(key, value) { this[key] = value; }};
  const c = {state: {selected: "one", detail: {running: true, work_thread: {thread_id: "one", browser_session: true,
    attention: "now"}}, sessions: [{thread_id: "one"}, {thread_id: "two"}], closing: new Set()},
    $: () => button, threadPath: id => "threads/" + id, button, showError() {}, renderStatus() {},
    refreshList: async () => {}, renderList() {}, goHome() { c.home = true; c.state.selected = null; }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function renderCloseWhenDone("), source.indexOf("function renderTurnElapsed(")), c);
  vm.runInContext(source.slice(source.indexOf("async function toggleCloseWhenDone("), source.indexOf('$("close-when-done").onclick')), c);
  return c;
}

test("control offers an accessible toggle while working and can cancel while waiting for a result", () => {
  const c = setup();
  c.renderCloseWhenDone();
  assert.equal(c.button.textContent, "Close when done");
  assert.equal(c.button.disabled, false);
  assert.equal(c.button["aria-pressed"], "false");
  c.state.detail.close_when_done = {status: "queued", success_only: true};
  c.state.detail.running = false;
  c.renderCloseWhenDone();
  assert.equal(c.button.textContent, "Close when done ✓");
  assert.equal(c.button["aria-pressed"], "true");
  assert.equal(c.button.disabled, false);
});

test("control stays unavailable for idle, closed, terminal, refreshing, or closing conversations", () => {
  const c = setup();
  c.state.detail.running = false; c.renderCloseWhenDone();
  assert.equal(c.button.disabled, true);
  c.state.detail.running = true; c.state.detailRefreshing = true; c.renderCloseWhenDone();
  assert.equal(c.button.disabled, true);
  c.state.detailRefreshing = false; c.state.closing.add("one"); c.renderCloseWhenDone();
  assert.equal(c.button.disabled, true);
  c.state.closing.clear(); c.state.detail.work_thread.attention = "archived"; c.renderCloseWhenDone();
  assert.equal(c.button.hidden, true);
  c.state.detail.work_thread.attention = "now"; c.state.detail.work_thread.browser_session = false; c.renderCloseWhenDone();
  assert.equal(c.button.hidden, true);
});

test("kept-open explanation is visible when a successful result could not be confirmed", () => {
  const c = setup(); c.state.detail.running = false;
  c.state.detail.close_when_done = {status: "cancelled", success_only: true, error: "The task has a required next step."};
  c.renderCloseWhenDone();
  assert.equal(c.button.textContent, "Kept open");
  assert.match(c.button.title, /required next step/);
});

test("toggle saves and cancels on the captured conversation without adding a chat message", async () => {
  const c = setup(), calls = [];
  c.api = async (path, body) => { calls.push({path, enabled: body.enabled});
    return {status: body.enabled ? "queued" : "cancelled", success_only: true}; };
  await c.toggleCloseWhenDone();
  assert.equal(c.state.detail.close_when_done.status, "queued");
  await c.toggleCloseWhenDone();
  assert.deepEqual(calls, [{path: "threads/one/close-when-done", enabled: true},
    {path: "threads/one/close-when-done", enabled: false}]);
});

test("duplicate clicks are suppressed and navigation cannot apply a result to another thread", async () => {
  const c = setup(); let finish, count = 0;
  c.api = () => { count++; return new Promise(resolve => { finish = resolve; }); };
  const first = c.toggleCloseWhenDone();
  await c.toggleCloseWhenDone();
  assert.equal(count, 1);
  c.state.selected = "two"; c.state.detail = {work_thread: {thread_id: "two"}};
  finish({status: "queued", success_only: true}); await first;
  assert.equal(c.state.detail.close_when_done, undefined);
  assert.equal(c.state.closeWhenDoneSaving.size, 0);
});

test("failed requests preserve the saved toggle and release its busy state", async () => {
  const c = setup();
  c.api = async () => { throw new Error("Unavailable"); };
  await assert.rejects(c.toggleCloseWhenDone(), /Unavailable/);
  assert.equal(c.state.detail.close_when_done, undefined);
  assert.equal(c.state.closeWhenDoneSaving.size, 0);
});

test("automatic closure removes its tab without navigating away from another selected conversation", async () => {
  const c = setup(), removed = [];
  c.state.selected = "two";
  c.state.threadTabs = {close: async (id, options) => { assert.equal(options.force, true); removed.push(id); }};
  c.api = async path => { assert.equal(path, "threads/one"); return {attention: "archived"}; };
  await c.threadClosedWhenDone("one");
  assert.deepEqual(removed, ["one"]);
  assert.equal(c.state.selected, "two");
  assert.equal(c.home, undefined);
  assert.deepEqual(Array.from(c.state.sessions, t => t.thread_id), ["two"]);
});

test("a reopened conversation survives a delayed automatic-close event", async () => {
  const c = setup();
  c.api = async () => ({attention: "now"});
  c.state.threadTabs = {close: async () => assert.fail("Reopened thread must keep its tab")};
  await c.threadClosedWhenDone("one");
  assert.equal(c.state.sessions.length, 2);
  assert.equal(c.state.selected, "one");
});
