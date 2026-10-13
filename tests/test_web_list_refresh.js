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
  c.showError = error => { throw error; };
  vm.runInContext(source.slice(source.indexOf("let listTimer, detailTimer;"), source.indexOf("function connect(")), c);
  const polling = c.refreshList({background: true});
  c.scheduleList(); callback();
  assert.equal(reads.length, 1);
  reads[0].resolve({data: []});
  await polling;
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
