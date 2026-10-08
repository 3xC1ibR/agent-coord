"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const grouping = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const {threadViews} = require("../plugins/agent-coord/scripts/agent_coord/web/views.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function response(extra = {}) {
  return {thread_id: "one", title: "Recommendation", attention: "now", response_state: "reply",
    checkpoint: {id: 12, phase: "finished"}, turn_completion: {id: 8},
    can_handle_response: true, needs_attention: true, unhandled_response: true, ...extra};
}
function setup(item = response()) {
  const c = {state: {sessions: [item], detail: null, selected: null, updatingThreads: new Set()},
    threadPath: id => "threads/" + id, renderList() {}, renderThread() {}, refreshList: async () => {}};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("async function updateThreadAttention("), source.indexOf("function threadCard(")), c);
  return c;
}

test("handling a card acknowledges only its displayed response and leaves navigation alone", async () => {
  const item = response({pinned: true}), c = setup(item);
  c.state.selected = "other"; c.state.detail = {work_thread: {thread_id: "other"}};
  c.api = async (path, body) => {
    assert.equal(path, "threads/one");
    assert.deepEqual(JSON.parse(JSON.stringify(body)), {handled: true, handled_checkpoint_id: 12, handled_completion_id: 8});
    return {...item, response_state: "available", unhandled_response: false, needs_attention: false, can_handle_response: false};
  };
  await c.markThreadHandled(item);
  assert.equal(c.state.selected, "other");
  assert.equal(c.state.detail.work_thread.thread_id, "other");
  assert.equal(c.state.sessions[0].pinned, true);
  assert.equal(c.state.sessions[0].attention, "now");
  assert.deepEqual(grouping.groupThreads(c.state.sessions).phases.flatMap(g => g.threads).map(t => t.thread_id), ["one"]);
});

test("failed handling keeps the response pending and releases its controls", async () => {
  const item = response(), c = setup(item);
  c.api = async () => { throw new Error("Connection lost"); };
  await assert.rejects(c.markThreadHandled(item), /Connection lost/);
  assert.equal(c.state.sessions[0], item);
  assert.equal(grouping.awaitsUser(item), true);
  assert.equal(c.state.updatingThreads.size, 0);
});

test("repeated clicks submit once, and a newer response returned by the API remains pending", async () => {
  const item = response(), c = setup(item);
  let finish, requests = 0;
  c.api = () => { requests++; return new Promise(resolve => { finish = resolve; }); };
  const saving = c.markThreadHandled(item);
  await c.markThreadHandled(item);
  assert.equal(requests, 1);
  finish(response({turn_completion: {id: 9}}));
  await saving;
  assert.equal(c.state.sessions[0].turn_completion.id, 9);
  assert.equal(grouping.awaitsUser(c.state.sessions[0]), true);
});

test("required input cannot be dismissed with the generic handled action", async () => {
  const c = setup(); c.api = () => assert.fail("must not submit");
  await c.markThreadHandled(response({response_state: "input", can_handle_response: false}));
});

test("parking defers attention while preserving the response, and returning restores it", async () => {
  const item = response(), c = setup(item), calls = [];
  c.api = async (path, body) => { calls.push(JSON.parse(JSON.stringify(body))); return {...item, ...body}; };
  await c.toggleThreadLater(item);
  assert.equal(grouping.awaitsUser(c.state.sessions[0]), false);
  assert.equal(c.state.sessions[0].unhandled_response, true);
  await c.toggleThreadLater(c.state.sessions[0]);
  assert.equal(grouping.awaitsUser(c.state.sessions[0]), true);
  assert.deepEqual(calls, [{attention: "later"}, {attention: "now"}]);
});

test("handled responses leave attention and remain in their work stage", () => {
  const pending = response({unread: false}), handled = response({thread_id: "handled", response_state: "available",
    can_handle_response: false, needs_attention: false, unhandled_response: false, unread: true});
  const input = [pending, handled];
  assert.equal(threadViews.badges(input, {}).attention, 1);
  assert.deepEqual(input.filter(t => threadViews.matches(t, {show: "completed"})), [pending, handled]);
  assert.deepEqual(grouping.groupThreads(input).phases.find(g => g.key === "finished").threads, [handled]);
});

test("Planning, Validating and Deploying remain separate columns beneath attention", () => {
  const input = ["planning", "validation", "deployment"].map((phase, i) => response({thread_id: phase,
    response_state: "working", needs_attention: false, checkpoint: {phase}}));
  input.push(response());
  const groups = grouping.groupThreads(input);
  assert.deepEqual(groups.phases.filter(g => g.threads.length).map(g => g.label), ["Planning", "Validating", "Deploying"]);
  assert.deepEqual(groups.priority.map(t => t.thread_id), ["one"]);
});

test("updates share the attention queue and read responses remain in their stage", () => {
  const unread = response({thread_id: "update", response_state: "update", needs_attention: true, attention_reason: "update"});
  const read = response({thread_id: "read", response_state: "available", needs_attention: false});
  const groups = grouping.groupThreads([unread, read]);
  assert.deepEqual(groups.priority, [unread]);
  assert.deepEqual(groups.phases.find(g => g.key === "finished").threads, [read]);
  assert.equal(threadViews.badges([unread], {}).attention, 1);
  assert.equal(threadViews.matches(unread, {show: "attention"}), true);
});
