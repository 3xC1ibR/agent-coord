"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  let now = 100000, focused = true;
  const calls = {list: 0, detail: 0, metadata: 0, panes: 0};
  const timers = new Map();
  let sequence = 0;
  const context = {state: {}, Date: {now: () => now},
    document: {hidden: false, hasFocus: () => focused},
    window: {agentCoordPanes: {refresh: () => calls.panes++}},
    refreshList: async () => { calls.list++; }, refreshDetail: async () => { calls.detail++; },
    refreshThread: async () => { calls.metadata++; }, showError: assert.fail,
    setTimeout(callback) { timers.set(++sequence, callback); return sequence; },
    clearTimeout(id) { timers.delete(id); }};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("let listTimer, detailTimer;"), source.indexOf("function connect(")), context);
  return {context, calls, advance: value => { now += value; }, focus: value => { focused = value; },
    fire() { const pending = [...timers.values()]; timers.clear(); for (const callback of pending) callback(); }};
}

test("inactive windows share a slower budget across event bursts and metadata polls", () => {
  const h = setup(); h.focus(false);
  h.context.refreshBackgroundView();
  for (let i = 0; i < 6; i++) {
    h.advance(2000);
    h.context.scheduleList(); h.context.scheduleDetail(); h.fire();
    h.context.refreshBackgroundView();
  }
  assert.deepEqual(h.calls, {list: 1, detail: 0, metadata: 1, panes: 1});
  h.advance(3000);
  h.context.scheduleList(); h.context.scheduleDetail(); h.fire();
  assert.deepEqual(h.calls, {list: 2, detail: 1, metadata: 1, panes: 1});
});

test("hidden windows defer background requests and immediately refresh when focused", () => {
  const h = setup(); h.focus(false); h.context.document.hidden = true;
  h.context.refreshBackgroundView();
  h.context.scheduleList(); h.context.scheduleDetail(); h.fire();
  assert.deepEqual(h.calls, {list: 0, detail: 0, metadata: 0, panes: 0});
  h.context.document.hidden = false; h.focus(true);
  h.context.refreshBackgroundView();
  assert.deepEqual(h.calls, {list: 1, detail: 1, metadata: 0, panes: 1});
});

test("focused windows refresh without waiting for the inactive-window interval", () => {
  const h = setup(); h.focus(false); h.context.refreshBackgroundView();
  h.advance(10); h.focus(true); h.context.refreshBackgroundView();
  assert.equal(h.calls.list, 2); assert.equal(h.calls.metadata, 2);
  h.context.scheduleDetail(); h.fire();
  assert.equal(h.calls.detail, 1);
});

test("a deferred conversation refresh survives metadata polling and runs when due", () => {
  const h = setup(); h.focus(false); h.context.refreshBackgroundView();
  h.advance(100); h.context.scheduleDetail(); h.fire();
  h.advance(5000); h.context.refreshBackgroundView();
  assert.equal(h.calls.detail, 0);
  h.advance(10000); h.context.refreshBackgroundView();
  assert.equal(h.calls.detail, 1); assert.equal(h.calls.metadata, 1);
  h.advance(15000); h.context.refreshBackgroundView();
  assert.equal(h.calls.detail, 1); assert.equal(h.calls.metadata, 2);
});

test("returning to a window reconciles its conversation even without a received event", () => {
  const h = setup(); h.focus(false); h.context.refreshBackgroundView();
  h.advance(10); h.focus(true); h.context.refreshBackgroundView({reconcile: true});
  assert.equal(h.calls.list, 2); assert.equal(h.calls.detail, 1);
});

test("a parent-driven pane refresh reconciles a deferred conversation snapshot", async () => {
  const h = setup(); h.focus(false); h.context.state.paneMode = true;
  h.context.refreshBackgroundView();
  h.advance(100); h.context.scheduleDetail(); h.fire();
  assert.equal(h.calls.detail, 0);
  vm.runInContext(source.slice(source.indexOf("async function refreshThread("), source.indexOf("function renderThread(")), h.context);
  await h.context.refreshThread();
  assert.equal(h.calls.detail, 1);
  assert.equal(h.context.state.backgroundDetailPending, false);
});

test("focusing a pane reconciles its snapshot without fetching a separate inventory", () => {
  const h = setup(); h.context.state.paneMode = true;
  h.context.refreshBackgroundView({reconcile: true});
  assert.equal(h.calls.detail, 1); assert.equal(h.calls.list, 0);
});
