"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const grouping = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const {threadViews} = require("../plugins/agent-coord/scripts/agent_coord/web/views.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const controls = new Map();
  const c = {Date, state: {sessions: [], detail: null, updatingThreads: new Set()},
    threadPath: id => "threads/" + id, renderList() {}, renderThread() {}, refreshList: async () => {},
    $: id => {
      if (!controls.has(id)) controls.set(id, {value: "", reset() {}, focus() {}, showModal() { this.open = true; }, close() { this.open = false; }});
      return controls.get(id);
    }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("async function updateThreadAttention("), source.indexOf("function threadCard(")), c);
  vm.runInContext(source.slice(source.indexOf('$("snooze-thread-button").onclick'), source.indexOf('$("handle-response").onclick')), c);
  return c;
}

test("duration and tomorrow choices use local time, including calendar rollovers", () => {
  const c = setup(), now = new Date(2026, 11, 31, 23, 30);
  assert.equal(c.snoozeTime("1", "", now), now.getTime() / 1000 + 3600);
  assert.equal(c.snoozeTime("3", "", now), now.getTime() / 1000 + 10800);
  assert.equal(c.snoozeTime("tomorrow", "", now), new Date(2027, 0, 1, 9).getTime() / 1000);
  assert.equal(c.snoozeTime("custom", "2027-01-02T10:30", now), new Date(2027, 0, 2, 10, 30).getTime() / 1000);
  for (const custom of ["", "invalid", "2026-01-01T10:30"]) assert.throws(() => c.snoozeTime("custom", custom, now), /future/);
});

test("snooze dialog uses its original thread even if selection changes and preserves read markers", async () => {
  const c = setup(), calls = [], item = {thread_id: "one", title: "Investigation", seen_completion_id: 8};
  c.api = async (path, body) => { calls.push({path, body: JSON.parse(JSON.stringify(body))}); return {...item, attention: "later", snoozed: true, ...body}; };
  c.state.sessions = [item]; c.openThreadSnooze(item);
  assert.equal(c.$("snooze-thread").textContent, "Investigation");
  assert.equal(c.$("snooze-dialog").open, true);
  c.state.selected = "another";
  c.$("snooze-duration").value = "1";
  await c.$("snooze-form").onsubmit({preventDefault() {}});
  assert.equal(calls[0].path, "threads/one");
  assert.deepEqual(Object.keys(calls[0].body), ["snoozed_until"]);
  assert.equal(c.state.sessions[0].seen_completion_id, 8);
  assert.equal(c.state.sessions[0].attention, "later");
  assert.equal(c.$("snooze-dialog").open, false);
});

test("invalid custom time and server failures stay in the dialog with recoverable errors", async () => {
  const c = setup(); c.openThreadSnooze({thread_id: "one"});
  c.$("snooze-duration").value = "custom"; c.$("snooze-duration").onchange();
  assert.equal(c.$("snooze-custom-label").hidden, false);
  assert.equal(c.$("snooze-custom").required, true);
  c.api = async () => assert.fail("Invalid date must not submit");
  await c.$("snooze-form").onsubmit({preventDefault() {}});
  assert.match(c.$("snooze-error").textContent, /future/);
  c.$("snooze-duration").value = "1";
  c.api = async () => { throw new Error("Connection lost"); };
  await c.$("snooze-form").onsubmit({preventDefault() {}});
  assert.equal(c.$("snooze-error").textContent, "Connection lost");
  assert.equal(c.$("snooze-error").hidden, false);
  assert.equal(c.$("snooze-dialog").open, true);
  assert.equal(c.$("save-snooze").disabled, false);
});

test("Resume acknowledges only the displayed snooze and leaves read/handled receipts untouched", async () => {
  const c = setup(), item = {thread_id: "one", snoozed_until: 1200};
  c.api = async (path, body) => {
    assert.equal(path, "threads/one");
    assert.deepEqual(JSON.parse(JSON.stringify(body)), {resume_snooze: 1200});
    return {...item, snoozed_until: null};
  };
  await c.resumeThreadSnooze(item);
});

test("snoozed threads live only in Later; due reminders enter Attention even when already read", () => {
  const parked = {thread_id: "one", attention: "later", snoozed: true, snoozed_until: 1200,
    work_phase: "implementation", needs_attention: false, response_state: "available", unread: false};
  let groups = grouping.groupThreads([parked]);
  assert.deepEqual(groups.later, [parked]);
  assert.equal(groups.phases.flatMap(g => g.threads).length, 0);
  assert.equal(threadViews.badges([parked], {}).attention, 0);
  const due = {...parked, attention: "now", snoozed: false, snooze_due: true, needs_attention: true, attention_reason: "snooze"};
  groups = grouping.groupThreads([due]);
  assert.deepEqual(groups.priority, [due]);
  assert.equal(grouping.reason(due).label, "Snooze ended");
  assert.equal(grouping.status({...due, response_state: "working", status: "running"}).label, "Working");
  assert.equal(grouping.cardAge(due, 999999), "fresh");
  assert.equal(threadViews.badges([due], {}).attention, 1);
  const resumed = {...due, snooze_due: false, snoozed_until: null, needs_attention: false, attention_reason: null};
  assert.deepEqual(grouping.groupThreads([resumed]).phases.find(g => g.key === "implementation").threads, [resumed]);
});

test("snoozing advances roll-up only after the change succeeds", async () => {
  const c = setup(), calls = [];
  c.state.rollUp = {ticket: id => id, responded: async ticket => calls.push(ticket)};
  c.api = async () => { throw new Error("Offline"); };
  await assert.rejects(c.snoozeThread({thread_id: "one"}, 1200), /Offline/);
  assert.deepEqual(calls, []);
  c.api = async () => ({thread_id: "one", attention: "later"});
  await c.snoozeThread({thread_id: "one"}, 1200);
  assert.deepEqual(calls, ["one"]);
});
