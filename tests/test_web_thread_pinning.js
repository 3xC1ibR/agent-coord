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

test("pins order within their stage without hiding attention or Later", () => {
  const input = [thread("ordinary"), thread("pin", {pinned: true}),
    thread("reply", {pinned: true, response_state: "reply"}),
    thread("later", {pinned: true, attention: "later"}),
    thread("closed", {pinned: true, attention: "archived"})];
  const before = JSON.stringify(input), groups = groupThreads(input);
  assert.deepEqual(ids(groups.phases.flatMap(g => g.threads)), ["pin", "ordinary"]);
  assert.deepEqual(ids(groups.priority), ["reply"]);
  assert.deepEqual(ids(groups.later), ["later"]);
  assert.deepEqual(ids(groups.closed), ["closed"]);
  assert.equal(JSON.stringify(input), before);
});

test("unpinning restores stable order within the same stage", () => {
  const first = thread("first", {created_at: 1}), second = thread("second", {created_at: 2, pinned: true});
  const cards = () => ids(groupThreads([first, second]).phases.flatMap(g => g.threads));
  assert.deepEqual(cards(), ["second", "first"]);
  second.pinned = false;
  assert.deepEqual(cards(), ["first", "second"]);
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
