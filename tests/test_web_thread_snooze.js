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
  const c = {Date, listRequest: 0, state: {sessions: [], detail: null, updatingThreads: new Set()},
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

test("Later defaults to no reminder and removes an existing timer without consuming the response", async () => {
  const c = setup(), thread = {thread_id: "one", attention: "later", snoozed_until: 9000, unhandled_response: true};
  c.openThreadSnooze(thread);
  assert.equal(c.$("snooze-duration").value, "none");
  c.api = async (path, body) => {
    assert.deepEqual(JSON.parse(JSON.stringify(body)), {attention: "later"});
    return {...thread, snoozed_until: null, snoozed: false};
  };
  c.state.sessions = [thread];
  await c.$("snooze-form").onsubmit({preventDefault() {}});
  assert.equal(c.state.sessions[0].snoozed_until, null);
  assert.equal(c.state.sessions[0].unhandled_response, true);
});

test("editing a future reminder shows its existing local return time", () => {
  const c = setup(), deadline = Date.now() / 1000 + 3600;
  c.openThreadSnooze({thread_id: "one", attention: "later", snoozed_until: deadline});
  assert.equal(c.$("snooze-duration").value, "custom");
  assert.equal(c.$("snooze-custom-label").hidden, false);
  assert.ok(Math.abs(new Date(c.$("snooze-custom").value).getTime() / 1000 - deadline) < 60);
});

test("new results from ordinary Later surface in Attention without entering a work column", () => {
  const thread = {thread_id: "one", attention: "later", needs_attention: true, response_state: "reply"};
  const groups = grouping.groupThreads([thread]);
  assert.deepEqual(groups.priority, [thread]);
  assert.deepEqual(groups.later, [thread]);
  assert.equal(groups.phases.flatMap(group => group.threads).length, 0);
  assert.equal(threadViews.matches(thread, {show: "active"}), true);
  assert.equal(threadViews.matches(thread, {show: "attention"}), true);
  assert.equal(threadViews.badges([thread], {}).attention, 1);
  assert.equal(threadViews.matches({...thread, needs_attention: false}, {show: "active"}), false);
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
  assert.equal(c.$("snooze-error").textContent, "Connection lost Try again.");
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
  assert.equal(grouping.reason(due).label, "Reminder");
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

function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }
test("confirming snooze closes immediately, polling overlays pending placement and duplicate saves are ignored", async () => {
  const c = setup(), save = deferred(), original = {thread_id: "one", title: "Demo", attention: "now", needs_attention: true, seen_completion_id: 8};
  c.state.sessions = [original]; c.state.viewThreads = [original]; c.api = () => save.promise;
  c.openThreadSnooze(original); assert.equal(c.state.sessions[0].attention, "now");
  c.$("snooze-duration").value = "1";
  const pending = c.$("snooze-form").onsubmit({preventDefault() {}});
  assert.equal(c.$("snooze-dialog").open, false);
  assert.equal(c.state.sessions[0].attention, "later"); assert.equal(c.state.sessions[0].snoozed, true);
  assert.equal(c.overlayPendingAttention({...original, seen_completion_id: 9}).seen_completion_id, 9);
  assert.equal(c.overlayPendingAttention(original).attention, "later");
  assert.equal(await c.updateThreadAttention(original, {snoozed_until: 9000}), false);
  const revision = c.state.attentionMutations.get("one").revision;
  c.state.sessions[0].seen_completion_id = 10;
  const deadline = c.state.sessions[0].snoozed_until;
  save.resolve({...original, snoozed_until: deadline, attention: "later", snoozed: true, needs_attention: false}); await pending;
  assert.equal(c.state.sessions[0].seen_completion_id, 10);
  assert.equal(c.state.sessions[0].snoozed_until, deadline);
  assert.equal(c.reconcileAttentionSnapshot(original, revision).attention, "later");
});
test("failed snooze restores placement without undoing concurrent read or pin changes and permits retry", async () => {
  const c = setup(), save = deferred(), original = {thread_id: "one", attention: "now", needs_attention: true, snoozed: false, snoozed_until: null, seen_completion_id: 8};
  c.state.sessions = [original]; c.api = () => save.promise;
  c.openThreadSnooze(original); c.$("snooze-duration").value = "1";
  const pending = c.$("snooze-form").onsubmit({preventDefault() {}});
  c.state.sessions[0].pinned = true; c.state.sessions[0].seen_completion_id = 9;
  const revision = c.state.attentionMutations.get("one").revision;
  save.reject(new Error("Offline")); await pending;
  assert.equal(c.state.sessions[0].attention, "now"); assert.equal(c.state.sessions[0].needs_attention, true);
  assert.equal(c.state.sessions[0].seen_completion_id, 9); assert.equal(c.state.sessions[0].pinned, true);
  assert.equal(c.$("snooze-dialog").open, true); assert.match(c.$("snooze-error").textContent, /Offline.*Try again/);
  assert.equal(c.reconcileAttentionSnapshot({thread_id: "one", attention: "later"}, revision).attention, "now");
  c.api = async (_, body) => ({...original, ...body, attention: "later", snoozed: true});
  await c.$("snooze-form").onsubmit({preventDefault() {}}); assert.equal(c.state.sessions[0].attention, "later");
});

test("list polling cannot resurrect a snoozed card before or after reconciliation", async () => {
 const c=setup(), save=deferred(), reads=[], original={thread_id:"one", attention:"now", snoozed:false, needs_attention:true};
 c.state.sessions=[original]; c.state.viewThreads=[original];
 c.$("view").value="active";
 for(const id of ["repository","project"]) c.$(id).dataset={signature:"[]"};
 c.savedViews={sync(){}}; c.renderFolders=()=>{};
 c.api=(path,body)=>{
  if(body) return save.promise;
  if(path.startsWith("threads?")){const read=deferred(); reads.push(read); return read.promise;}
  if(path==="organization") return Promise.resolve({repositories:[],projects:[]});
  return Promise.resolve({data:[]});
 };
 vm.runInContext(source.slice(source.indexOf("async function refreshList("),source.indexOf("function sessionContext(")),c);
 const before=c.refreshList(); const pending=c.snoozeThread(original,Date.now()/1000+3600);
 reads[0].resolve({data:[original]}); await before; assert.equal(c.state.sessions[0].attention,"later");
 const during=c.refreshList(); reads[1].resolve({data:[{...original,seen_completion_id:12}]}); await during;
 assert.equal(c.state.sessions[0].attention,"later"); assert.equal(c.state.sessions[0].seen_completion_id,12);
 const stale=c.refreshList(); save.resolve({...original,attention:"later",snoozed:true,snoozed_until:Date.now()/1000+3600}); await pending;
 reads[2].resolve({data:[original]}); await stale; assert.equal(c.state.sessions[0].attention,"later");
});
test("a late failure cannot replace a newly opened snooze picker", async()=>{
 const c=setup(), save=deferred(), errors=[]; c.showError=error=>errors.push(error.message);
 c.state.sessions=[{thread_id:"one",attention:"now"}]; c.api=()=>save.promise;
 c.openThreadSnooze(c.state.sessions[0]); c.$("snooze-duration").value="1";
 const pending=c.$("snooze-form").onsubmit({preventDefault(){}});
 c.openThreadSnooze({thread_id:"two",title:"Other"}); save.reject(new Error("Offline")); await pending;
 assert.equal(c.state.snoozingThread.thread_id,"two"); assert.equal(c.$("snooze-thread").textContent,"Other");
 assert.equal(c.$("snooze-dialog").open,true); assert.deepEqual(errors,["Offline"]);
});
test("reopening the same pending snooze enables changing it after reconciliation", async()=>{
 const c=setup(), save=deferred(), original={thread_id:"one",attention:"now"}; c.state.sessions=[original]; c.api=()=>save.promise;
 c.openThreadSnooze(original); c.$("snooze-duration").value="1";
 const pending=c.$("snooze-form").onsubmit({preventDefault(){}});
 c.openThreadSnooze(c.state.sessions[0]); assert.equal(c.$("save-snooze").disabled,true);
 save.resolve({...original,attention:"later",snoozed:true,snoozed_until:9000}); await pending;
 assert.equal(c.$("save-snooze").disabled,false); assert.equal(c.state.snoozingThread.snoozed_until,9000);
});
