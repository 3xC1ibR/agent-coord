"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {groupThreads, awaitsUser} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function thread(id, extra = {}) {
  return {thread_id: id, title: id, attention: "now", pinned: false,
    response_state: "working", checkpoint: {phase: "implementation"}, ...extra};
}
const ids = threads => threads.map(t => t.thread_id);

test("pins stay together across phases and placement without changing saved thread state", () => {
  const input = [thread("working", {pinned: true}),
    thread("later", {pinned: true, attention: "later", response_state: "reply"}),
    thread("done", {pinned: true, response_state: "completed", checkpoint: {phase: "finished"}}),
    thread("ordinary")];
  const before = JSON.stringify(input), groups = groupThreads(input);
  assert.deepEqual(ids(groups.pinned), ["working", "later", "done"]);
  assert.deepEqual(ids(groups.phases.flatMap(g => g.threads)), ["ordinary"]);
  assert.deepEqual(groups.completed, []);
  assert.deepEqual(groups.later, []);
  assert.equal(JSON.stringify(input), before);
});

test("pinned replies, approvals and failures appear only in Pinned", () => {
  for (const response_state of ["reply", "input", "failed"]) {
    const item = thread("reply", {pinned: true, response_state, unread: true});
    assert.equal(awaitsUser(item), true);
    assert.deepEqual(groupThreads([item]).priority, []);
    assert.deepEqual(ids(groupThreads([item]).pinned), ["reply"]);
    item.pinned = false;
    assert.deepEqual(ids(groupThreads([item]).priority), ["reply"]);
    assert.deepEqual(groupThreads([item]).pinned, []);
    item.pinned = true;
    item.attention = "later";
    assert.equal(awaitsUser(item), false);
    assert.deepEqual(groupThreads([item]).priority, []);
    assert.deepEqual(ids(groupThreads([item]).pinned), ["reply"]);
    assert.equal(item.unread, true);
  }
});

test("pinning never duplicates a thread across overview groups", () => {
  const input = [thread("pin", {pinned: true, response_state: "reply"}),
    thread("reply", {response_state: "reply"}), thread("work"),
    thread("later", {attention: "later"}), thread("done", {response_state: "completed"})];
  const groups = groupThreads(input);
  const visible = [...groups.pinned, ...groups.priority, ...groups.completed,
    ...groups.phases.flatMap(g => g.threads), ...groups.later];
  assert.equal(visible.length, input.length);
  assert.deepEqual(new Set(ids(visible)), new Set(ids(input)));
});

test("unpinning restores each thread to its normal group", () => {
  const input = [thread("working", {pinned: true}),
    thread("later", {pinned: true, attention: "later"}),
    thread("done", {pinned: true, response_state: "completed"}),
    thread("reply", {pinned: true, response_state: "reply"})];
  for (const item of input) item.pinned = false;
  const groups = groupThreads(input);
  assert.deepEqual(groups.pinned, []);
  assert.deepEqual(ids(groups.phases.flatMap(g => g.threads)), ["working"]);
  assert.deepEqual(ids(groups.later), ["later"]);
  assert.deepEqual(ids(groups.completed), ["done"]);
  assert.deepEqual(ids(groups.priority), ["reply"]);
});

test("closed pins leave the top section and reappear when reopened", () => {
  const item = thread("closed", {pinned: true, attention: "archived", response_state: "reply"});
  const groups = groupThreads([item]);
  assert.deepEqual(groups.pinned, []);
  assert.deepEqual(groups.priority, []);
  assert.deepEqual(ids(groups.phases.flatMap(g => g.threads)), ["closed"]);
  item.attention = "now";
  assert.deepEqual(ids(groupThreads([item]).pinned), ["closed"]);
});

function setup(item = thread("one")) {
  const c = {state: {sessions: [item], selected: null, detail: null, pinning: new Set(), listSignature: ""},
    threadPath: id => "threads/" + id, renderList() {}, refreshList: async () => { c.refreshed = true; }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("async function toggleThreadPinned("), source.indexOf("function threadCard(")), c);
  return c;
}

test("pin and unpin target the card without opening or marking it read", async () => {
  const item = thread("card");
  const c = setup(item);
  c.state.selected = "another";
  c.state.detail = {work_thread: thread("another")};
  for (const pinned of [true, false]) {
    c.api = async (path, body) => {
      assert.equal(path, "threads/card");
      assert.equal(JSON.stringify(body), JSON.stringify({pinned}));
      return {...item, pinned};
    };
    await c.toggleThreadPinned(c.state.sessions[0]);
    assert.equal(c.state.sessions[0].pinned, pinned);
    assert.equal(c.state.selected, "another");
    assert.equal(c.state.detail.work_thread.thread_id, "another");
    assert.equal(c.state.pinning.size, 0);
    assert.equal(c.refreshed, true);
  }
});

test("duplicate pin controls cannot submit a second request while saving", async () => {
  const item = thread("one"), c = setup(item);
  let finish, requests = 0;
  c.api = () => { requests++; return new Promise(resolve => { finish = resolve; }); };
  const saving = c.toggleThreadPinned(item);
  assert.equal(c.state.pinning.has("one"), true);
  await c.toggleThreadPinned(item);
  assert.equal(requests, 1);
  finish({...item, pinned: true});
  await saving;
  assert.equal(c.state.pinning.size, 0);
});

test("failed pin requests keep existing placement and allow retry", async () => {
  const item = thread("one", {pinned: true}), c = setup(item);
  c.api = async () => { throw new Error("Connection lost"); };
  await assert.rejects(c.toggleThreadPinned(item), /Connection lost/);
  assert.equal(c.state.sessions[0].pinned, true);
  assert.equal(c.state.pinning.size, 0);
  assert.equal(c.state.selected, null);
});
