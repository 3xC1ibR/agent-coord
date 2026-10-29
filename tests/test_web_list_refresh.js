"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const grouping = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

const thread = (extra = {}) => ({thread_id: "dispatcher", attention: "now", work_phase: "investigation",
  status: "idle", response_state: "reply", needs_attention: true, ...extra});
function setup() {
  const controls = {view: {value: "active"}, repository: {dataset: {signature: "[]"}},
    project: {dataset: {signature: "[]"}}};
  const reads = [], rendered = [];
  const c = {listRequest: 0, state: {sessions: [thread()]}, $: id => controls[id],
    overlayPendingAttention: item => item, threadsInProject: items => items,
    savedViews: {sync() {}}, renderFolders() {},
    renderList() { rendered.push(grouping.groupThreads(c.state.sessions)); },
    api(path) {
      if (path.startsWith("threads?")) return new Promise((resolve, reject) => reads.push({path, resolve, reject}));
      return Promise.resolve(path === "organization" ? {repositories: [], projects: []} : {data: []});
    }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("async function refreshList("), source.indexOf("function currentProjectDispatcher(")), c);
  return {c, reads, rendered, controls};
}

test("slow background refreshes publish running state despite repeated polling and events", async () => {
  const {c, reads, rendered} = setup();
  const first = c.refreshList({background: true});
  const polls = Array.from({length: 9}, () => c.refreshList({background: true}));
  reads[0].resolve({data: [thread({status: "running", response_state: "working", needs_attention: false})]});
  await first;
  assert.equal(c.state.sessions[0].response_state, "working");
  assert.equal(reads.length, 1, "background events share one inventory request");
  await Promise.all(polls);
  assert.equal(rendered.length, 1);
  assert.equal(rendered[0].priority.length, 0);
  assert.equal(rendered[0].phases.find(phase => phase.key === "investigation").threads[0].thread_id, "dispatcher");

  const next = c.refreshList({background: true});
  reads[1].resolve({data: [thread({status: "needs input", response_state: "input", attention_reason: "blocked"})]});
  await next;
  assert.equal(rendered[1].priority[0].thread_id, "dispatcher", "a real approval still needs attention");
});

test("explicit refresh after a user action supersedes an earlier background snapshot", async () => {
  const {c, reads} = setup();
  const old = c.refreshList({background: true});
  const action = c.refreshList();
  const polling = c.refreshList({background: true});
  assert.equal(reads.length, 2);
  reads[1].resolve({data: [thread({attention: "later", needs_attention: false})]});
  await Promise.all([action, polling]);
  reads[0].resolve({data: [thread()]});
  await old;
  assert.equal(c.state.sessions[0].attention, "later");
});

test("a view change starts a fresh read and old completion cannot clear its pending request", async () => {
  const {c, reads, controls} = setup();
  const old = c.refreshList({background: true});
  controls.view.value = "archived";
  const closed = c.refreshList({background: true});
  reads[0].resolve({data: [thread()]});
  await old;
  const polling = c.refreshList({background: true});
  assert.equal(reads.length, 3, "the new view fetches open badges and closed cards once");
  reads[1].resolve({data: []});
  reads[2].resolve({data: [thread({attention: "archived"})]});
  await Promise.all([closed, polling]);
  assert.equal(c.state.sessions[0].attention, "archived");
  assert.equal(c.state.viewThreads.length, 0);
});

test("mutation invalidation rejects a stale snapshot even when the view is unchanged", async () => {
  const {c, reads} = setup();
  const old = c.refreshList({background: true});
  c.listRequest++;
  const updated = c.refreshList({background: true});
  reads[0].resolve({data: [thread()]});
  await old;
  const polling = c.refreshList({background: true});
  assert.equal(reads.length, 2);
  reads[1].resolve({data: [thread({snoozed: true, attention: "later", needs_attention: false})]});
  await Promise.all([updated, polling]);
  assert.equal(c.state.sessions[0].snoozed, true);
});

test("closing during inventory reads cannot resurrect a confirmed closed card", async () => {
  const {c, reads} = setup();
  Object.assign(c, {renderStatus() {}, threadPath: id => "threads/" + id, showError(error) { throw error; }});
  Object.assign(c.state, {selected: null, closing: new Set()});
  vm.runInContext(source.slice(source.indexOf("async function toggleThreadClosed("), source.indexOf('$("close-thread").onclick')), c);
  const read = c.api;
  let finishClose;
  c.api = (path, body) => path.endsWith("/close") ? new Promise(resolve => { finishClose = resolve; }) : read(path, body);
  const before = c.refreshList({background: true});
  const closing = c.toggleThreadClosed(thread());
  const during = c.refreshList({background: true});
  finishClose({attention: "archived"});
  await closing;
  assert.equal(c.state.sessions.length, 0);
  // Both snapshots still contain the thread, but predate the confirmed close.
  reads[0].resolve({data: [thread()]});
  reads[1].resolve({data: [thread()]});
  await Promise.all([before, during]);
  assert.equal(c.state.sessions.length, 0);
  assert.equal(reads.length, 3, "confirmation refresh runs independently");
  reads[2].resolve({data: [thread({thread_id: "other"})]});
  await c.state.listRefresh.promise;
  assert.equal(c.state.sessions[0].thread_id, "other", "post-close refresh is not invalidated");
});

test("a failed shared refresh releases the request so the next poll can recover", async () => {
  const {c, reads} = setup();
  const first = c.refreshList({background: true}), second = c.refreshList({background: true});
  const failed = Promise.all([assert.rejects(first, /Offline/), assert.rejects(second, /Offline/)]);
  assert.equal(reads.length, 1);
  reads[0].reject(new Error("Offline"));
  await failed;
  const retry = c.refreshList({background: true});
  reads[1].resolve({data: [thread({response_state: "input", attention_reason: "review",
    checkpoint: {next_actor: "user", next_action: "Review the result."}})]});
  await retry;
  assert.equal(grouping.groupThreads(c.state.sessions).priority.length, 1);
});

test("event-triggered refreshes join background polling", async () => {
  const {c, reads} = setup();
  let callback;
  c.setTimeout = fn => { callback = fn; };
  c.clearTimeout = () => {};
  c.Date = Date;
  c.showError = error => { throw error; };
  vm.runInContext(source.slice(source.indexOf("let listTimer, detailTimer;"), source.indexOf("function connect(")), c);
  const polling = c.refreshList({background: true});
  c.scheduleList(); callback();
  assert.equal(reads.length, 1);
  reads[0].resolve({data: []});
  await polling;
});

test("an event burst schedules one list read per interval and is never postponed", async () => {
  const {c, reads} = setup();
  const timers = [];
  let now = 10000;
  c.Date = {now: () => now};
  c.setTimeout = (fn, wait) => { timers.push({fn, wait}); return timers.length; };
  c.clearTimeout = () => assert.fail("a scheduled refresh must not be postponed");
  c.showError = error => { throw error; };
  vm.runInContext(source.slice(source.indexOf("let listTimer, detailTimer;"), source.indexOf("function connect(")), c);
  for (let event = 0; event < 20; event++) c.scheduleList();
  assert.equal(timers.length, 1);
  assert.equal(timers[0].wait, 150);
  timers[0].fn();
  assert.equal(reads.length, 1);
  reads[0].resolve({data: []});
  now += 200;
  for (let event = 0; event < 20; event++) c.scheduleList();
  assert.equal(timers.length, 2);
  assert.equal(timers[1].wait, 1800, "the next read waits for the rest of the interval");
});

test("pane refreshes preserve background polling and explicit action semantics", async () => {
  const {c, reads} = setup(), requests = [];
  c.state.paneMode = true;
  c.window = {parent: {agentCoordPanes: {requestList: options => requests.push(options.background)}}};
  await c.refreshList({background: true});
  await c.refreshList();
  assert.deepEqual(requests, [true, false]);
  assert.equal(reads.length, 0);
});

function countCompanions(c) {
  const counts = {asks: 0, schedules: 0};
  c.showError = error => { throw error; };
  c.state.asks = {refresh: async () => { counts.asks += 1; }};
  c.state.schedules = {refresh: async () => { counts.schedules += 1; }};
  return counts;
}

test("per-item provider events do not refresh the thread list", () => {
  const {c} = setup();
  vm.runInContext(source.slice(source.indexOf("// Per-item provider events change"), source.indexOf("function newProject(")), c);
  for (const method of ["item/started", "item/completed", "item/reasoning/summaryTextDelta", "item/agentMessage/delta",
    "item/commandExecution/outputDelta", "hook/started", "hook/completed", "turn/diff/updated", "turn/plan/updated",
    "thread/usage/updated", "thread/tokenUsage/updated", "account/rateLimits/updated", "deprecationNotice",
    "mcpServer/startupStatus/updated", "warning"]) {
    assert.equal(c.changesThreadList({method}), false, method);
  }
  for (const method of ["turn/started", "turn/completed", "thread/started", "thread/name/updated", "thread/closed",
    "thread/classified", "browser/changed", "coordination/messages", "coordination/messageSent", "serverRequest/resolved"]) {
    assert.equal(c.changesThreadList({method}), true, method);
  }
  assert.equal(c.changesThreadList({method: "item/commandExecution/requestApproval", requestKey: "request"}), true,
    "provider requests change attention");
});

test("background refreshes read decisions and schedules at most every 15 seconds", async () => {
  const {c, reads} = setup();
  const counts = countCompanions(c);
  let now = 100000;
  c.Date = {now: () => now};
  const first = c.refreshList({background: true});
  c.refreshList({background: true});
  c.refreshList({background: true});
  assert.equal(reads.length, 1);
  assert.deepEqual(counts, {asks: 1, schedules: 1}, "joined refreshes issue no companion requests");
  reads[0].resolve({data: []});
  await first;
  now += 5000;
  const soon = c.refreshList({background: true});
  assert.deepEqual(counts, {asks: 1, schedules: 1});
  reads[1].resolve({data: []});
  await soon;
  now += 10000;
  const later = c.refreshList({background: true});
  assert.deepEqual(counts, {asks: 2, schedules: 2});
  reads[2].resolve({data: []});
  await later;
});

test("explicit refreshes always update decisions and schedules; panes follow the background schedule", async () => {
  const {c, reads} = setup();
  const counts = countCompanions(c);
  let now = 100000;
  c.Date = {now: () => now};
  c.refreshList({background: true});
  c.refreshList();
  assert.equal(reads.length, 2);
  assert.deepEqual(counts, {asks: 2, schedules: 2});
  c.state.paneMode = true;
  c.window = {parent: {agentCoordPanes: {requestList() {}}}};
  await c.refreshList({background: true});
  assert.deepEqual(counts, {asks: 2, schedules: 2});
  await c.refreshList();
  assert.deepEqual(counts, {asks: 3, schedules: 3});
  now += 15000;
  await c.refreshList({background: true});
  assert.deepEqual(counts, {asks: 4, schedules: 4});
});

test("an unchanged list is validated, not reprocessed, and companions keep their own schedule", async () => {
  const {c, reads, rendered} = setup();
  let now = 100000;
  c.Date = {now: () => now};
  const companions = [];
  const api = c.api;
  c.api = (path, body, options) => { if (!path.startsWith("threads?")) companions.push(path); return api(path, body, options); };
  const first = c.refreshList({background: true});
  const listed = {data: [thread()]};
  reads[0].resolve(listed);
  await first;
  assert.deepEqual(companions, ["organization", "views"]);
  assert.equal(rendered.length, 1);
  now += 10000;
  const unchanged = c.refreshList({background: true});
  reads[1].resolve(listed);  // api() resolves a 304 to the previously returned object.
  await unchanged;
  assert.equal(rendered.length, 1, "an unchanged list is not redrawn");
  assert.deepEqual(companions, ["organization", "views"], "groups and views wait for their own interval");
  now += 30000;
  const relative = c.refreshList({background: true});
  reads[2].resolve(listed);
  await relative;
  assert.equal(rendered.length, 2, "relative times still advance");
  now += 30000;
  const changed = c.refreshList({background: true});
  reads[3].resolve({data: [thread({title: "Renamed"})]});
  await changed;
  assert.equal(rendered.length, 3);
  assert.deepEqual(companions, ["organization", "views", "organization", "views"]);
});

test("a validated read sends its entity tag and resolves a 304 to the previous value", async () => {
  const requests = [];
  const responses = [
    {status: 200, ok: true, headers: {get: name => name === "ETag" ? '"one"' : "application/json"}, json: async () => ({data: [1]})},
    {status: 304, ok: false, headers: {get: () => null}, json: async () => { throw new Error("no body"); }},
    {status: 200, ok: true, headers: {get: name => name === "ETag" ? '"two"' : "application/json"}, json: async () => ({data: [2]})},
  ];
  const c = {listRequest: 0, state: {config: {token: "t"}}, base: "/api/browser/", location: {protocol: "http:"}, console,
    setTimeout, fetch: async (url, options) => { requests.push(options.headers?.["If-None-Match"] || null); return responses.shift(); }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("async function api("), source.indexOf("async function openMentionFile(")), c);
  const entity = {};
  const first = await c.api("threads?archived=false", undefined, {entity});
  assert.deepEqual(first, {data: [1]});
  const second = await c.api("threads?archived=false", undefined, {entity});
  assert.equal(second, first, "an unchanged resource resolves to the same object");
  const third = await c.api("threads?archived=false", undefined, {entity});
  assert.deepEqual(third, {data: [2]});
  assert.deepEqual(requests, [null, '"one"', '"one"']);
  assert.equal(entity.tag, '"two"');
});
