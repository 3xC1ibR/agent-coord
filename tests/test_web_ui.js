"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {awaitsUser, groupThreads, status, phase, reason} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const thread = (id, work_phase = "investigation", extra = {}) => ({thread_id: id, attention: "now", status: "idle",
  work_phase, created_at: 1, response_state: "available", ...extra});
const ids = items => items.map(t => t.thread_id);
const lane = (groups, key) => groups.phases.find(g => g.key === key);

test("all seven lifecycle columns have a fixed order, including empty stages", () => {
  const expected = ["Getting started", "Investigating", "Planning", "Implementing", "Validating", "Deploying", "Done"];
  assert.deepEqual(groupThreads([]).phases.map(g => g.label), expected);
  const groups = groupThreads([thread("deploy", "deployment"), thread("findings"), thread("done", "finished")]);
  assert.deepEqual(groups.phases.map(g => g.label), expected);
  assert.deepEqual(ids(lane(groups, "investigation").threads), ["findings"]);
  assert.deepEqual(ids(lane(groups, "finished").threads), ["done"]);
});

test("blocked deployment outranks requested review, healthy rollout, findings, and routine success", () => {
  const items = [thread("success", "finished", {attention_reason: "done", response_state: "update", pinned: true}),
    thread("findings", "investigation", {attention_reason: "findings", response_state: "update"}),
    thread("healthy", "deployment", {attention_reason: "update", response_state: "update"}),
    thread("review", "validation", {attention_reason: "review", response_state: "input"}),
    thread("credentials", "deployment", {attention_reason: "blocked", response_state: "input"})];
  const groups = groupThreads(items);
  assert.deepEqual(ids(groups.priority), ["credentials", "review", "healthy", "findings", "success"]);
  assert.equal(lane(groups, "deployment").attention, 2);
  assert.equal(lane(groups, "finished").attention, 1);
  assert.equal(groups.phases.flatMap(g => g.threads).length, 0);
  assert.equal(reason(items[0]).label, "✓ Done");
});

test("reading findings and success restores the same stage while required actions remain", () => {
  const items = [thread("findings", "investigation", {response_state: "update", needs_attention: true}),
    thread("success", "finished", {response_state: "update", needs_attention: true}),
    thread("blocker", "deployment", {response_state: "input", needs_attention: true})];
  for (const t of items.slice(0, 2)) { t.response_state = "available"; t.needs_attention = false; t.unread = false; }
  items[2].unread = false;
  const groups = groupThreads(items);
  assert.deepEqual(ids(groups.priority), ["blocker"]);
  assert.deepEqual(ids(lane(groups, "investigation").threads), ["findings"]);
  assert.deepEqual(ids(lane(groups, "finished").threads), ["success"]);
  assert.equal(phase({work_phase: "investigation", checkpoint: {phase: "finished"}}), "investigation");
  assert.equal(phase({checkpoint: {phase: "discussion"}}), "investigation");
});

test("stage peers and attention peers keep stable order through polling and metadata changes", () => {
  const older = thread("older", "implementation", {created_at: 1, updated_at: 900});
  const newer = thread("newer", "implementation", {created_at: 2, updated_at: 800});
  for (const input of [[newer, older], [older, newer]]) {
    assert.deepEqual(ids(lane(groupThreads(input), "implementation").threads), ["older", "newer"]);
    older.updated_at++;
  }
  for (const t of [older, newer]) { t.response_state = "reply"; t.attention_since = 5; }
  assert.deepEqual(ids(groupThreads([newer, older]).priority), ["older", "newer"]);
  newer.pinned = true;
  assert.deepEqual(ids(groupThreads([newer, older]).priority), ["older", "newer"]);
});

test("Later and Closed stay out of the current board and queue, preserving all context", () => {
  const now = thread("now"), later = thread("later", "deployment", {attention: "later", response_state: "input", pinned: true, unread: true});
  const closed = thread("closed", "finished", {attention: "archived", pinned: true});
  const input = [now, later, closed], before = JSON.stringify(input), groups = groupThreads(input);
  assert.deepEqual(groups.priority, []);
  assert.deepEqual(ids(groups.later), ["later"]);
  assert.deepEqual(ids(groups.closed), ["closed"]);
  assert.deepEqual(ids(groups.phases.flatMap(g => g.threads)), ["now"]);
  assert.equal(JSON.stringify(input), before);
  later.attention = "now";
  assert.equal(awaitsUser(later), true);
  assert.equal(later.unread, true);
});

test("unclassified replies remain visible after reading; running and interrupted turns are separate", () => {
  const reply = thread("unknown", "new", {response_state: "reply", unread: false});
  const working = thread("work", "implementation", {response_state: "working", unread: true});
  const stopped = thread("stopped", "implementation", {response_state: "interrupted"});
  assert.deepEqual(ids(groupThreads([reply, working, stopped]).priority), ["unknown"]);
  assert.equal(status(working).key, "running");
  assert.equal(status(stopped).label, "Stopped");
});

test("placement, pin, phase, and response transitions never drop or duplicate a thread", () => {
  for (const attention of ["now", "later", "archived"]) for (const response_state of ["working", "input", "update", "available", "reply"]) {
    const moving = thread("moving", "implementation", {attention, response_state, pinned: true});
    const groups = groupThreads([moving, thread("unknown", "future-phase")]);
    const shown = [...groups.priority, ...groups.phases.flatMap(g => g.threads), ...groups.later, ...groups.closed];
    assert.equal(shown.length, 2);
    assert.equal(new Set(ids(shown)).size, 2);
  }
});
