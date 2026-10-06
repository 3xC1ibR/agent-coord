"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {awaitsUser, groupThreads, status} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");

function thread(id, phase, responseState = null, extra = {}) {
  return {thread_id: id, attention: "now", status: "idle", unread: false,
    response_state: responseState, checkpoint: {phase}, ...extra};
}

test("investigation replies remain Your turn after opening, until the next prompt", () => {
  const reply = thread("investigation", "investigation", "reply", {unread: true});
  assert.equal(awaitsUser(reply), true);
  reply.unread = false;
  assert.equal(awaitsUser(reply), true);
  assert.deepEqual(groupThreads([reply]).priority.map(t => t.thread_id), ["investigation"]);
  assert.deepEqual(status(reply), {key: "waiting", label: "Your turn"});
  reply.response_state = "working";
  assert.equal(awaitsUser(reply), false);
  assert.equal(groupThreads([reply]).phases[0].label, "Investigating");
});

test("new completed tasks have their own section and do not require a reply", () => {
  const seen = thread("seen", "finished", "completed");
  const fresh = thread("fresh", "finished", "completed", {unread: true});
  const reply = thread("reply", "investigation", "reply");
  const groups = groupThreads([seen, fresh, reply]);
  assert.deepEqual(groups.priority.map(t => t.thread_id), ["reply"]);
  assert.deepEqual(groups.completed.map(t => t.thread_id), ["fresh", "seen"]);
  assert.equal(groups.phases.length, 0);
  assert.equal(awaitsUser(fresh), false);
  assert.deepEqual(status(fresh), {key: "completed", label: "Completed"});
  fresh.unread = false;
  assert.equal(groupThreads([fresh]).completed.length, 1);
});

test("unread progress, missing checkpoints, and stale phases do not hide a reply", () => {
  const working = thread("working", "finished", "working", {unread: true, checkpoint_stale: true});
  const progress = thread("progress", "investigation", null, {unread: true});
  const noCheckpoint = thread("missing", null, "reply", {checkpoint: null});
  const stale = thread("stale", "finished", "reply", {checkpoint_stale: true});
  const groups = groupThreads([working, progress, noCheckpoint, stale]);
  assert.deepEqual(groups.priority.map(t => t.thread_id), ["missing", "stale"]);
  assert.equal(groups.completed.length, 0);
  assert.equal(status(working).label, "Working");
});

test("approvals and failures require attention; an interrupted turn is not a completed task", () => {
  const approval = thread("approval", "implementation", "input", {checkpoint_stale: true});
  const failed = thread("failed", "finished", "failed");
  const stopped = thread("stopped", "implementation", "interrupted");
  const groups = groupThreads([approval, failed, stopped]);
  assert.deepEqual(groups.priority.map(t => t.thread_id), ["approval", "failed"]);
  assert.equal(groups.completed.length, 0);
  assert.equal(status(approval).label, "Needs input");
  assert.equal(status(failed).label, "Failed");
  assert.equal(status(stopped).label, "Stopped");
});

test("grouping preserves Later placement and excludes archived conversations from priority", () => {
  const parkedReply = thread("parked-reply", "discussion", "reply", {attention: "later"});
  const later = thread("later", "planning", null, {attention: "later"});
  const parkedResult = thread("parked-result", "finished", "completed", {attention: "later", unread: true});
  const archived = thread("archived", "finished", "reply", {attention: "archived", unread: true});
  const input = [parkedReply, later, parkedResult, archived], before = JSON.stringify(input);
  const groups = groupThreads(input);
  assert.deepEqual(groups.priority.map(t => t.thread_id), ["parked-reply"]);
  assert.deepEqual(groups.later.map(t => t.thread_id), ["later", "parked-result"]);
  assert.deepEqual(groups.phases.flatMap(g => g.threads).map(t => t.thread_id), ["archived"]);
  assert.equal(groups.completed.length, 0);
  assert.equal(JSON.stringify(input), before);
});

test("phase groups follow the workflow and retain recent-first order within a phase", () => {
  const groups = groupThreads([
    thread("deploy", "deployment"), thread("newer", "implementation"),
    thread("discuss", "discussion"), thread("older", "implementation"),
    thread("validate", "validation"), thread("investigate", "investigation"),
    thread("plan", "planning"), thread("done", "finished", "completed"),
  ]);
  assert.deepEqual(groups.phases.map(g => g.label), ["Discussing", "Investigating", "Planning", "Implementing", "Validating", "Deploying"]);
  assert.deepEqual(groups.phases.find(g => g.key === "implementation").threads.map(t => t.thread_id), ["newer", "older"]);
  assert.deepEqual(groups.completed.map(t => t.thread_id), ["done"]);
});

test("changes of phase and response state never duplicate or drop a thread", () => {
  const input = [thread("moving", "implementation", "working"), thread("missing", null, null, {checkpoint: null}), thread("future", "unknown-phase")];
  assert.deepEqual(groupThreads(input).phases.map(g => g.key), ["implementation", "new"]);
  input[0].response_state = "reply";
  assert.equal(groupThreads(input).priority.length, 1);
  input[0].response_state = "completed";
  input[0].checkpoint.phase = "finished";
  const groups = groupThreads(input);
  const ids = [...groups.priority, ...groups.completed, ...groups.phases.flatMap(g => g.threads), ...groups.later].map(t => t.thread_id);
  assert.equal(new Set(ids).size, input.length);
  assert.equal(ids.length, input.length);
});
