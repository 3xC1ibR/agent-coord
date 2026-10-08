"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {RollUp, setupRollUp} = require("../plugins/agent-coord/scripts/agent_coord/web/roll-up.js");
const thread = (id, since, extra = {}) => ({thread_id: id, attention: "now", needs_attention: true,
  attention_since: since, attention_key: id + ":1", ...extra});
function setup(threads) {
  let selected = null;
  const opened = [], data = {threads};
  const roll = new RollUp({load: async () => data.threads, selected: () => selected,
    open: async id => { selected = id; opened.push(id); }});
  return {roll, data, opened, selected: id => { selected = id; }};
}

test("oldest waiting request wins across projects, excluding quiet, Later and Closed", async () => {
  const {roll, opened} = setup([thread("new", 20, {created_at: 1}), thread("old", 10, {project_id: "elsewhere", created_at: 99}),
    thread("later", 1, {attention: "later"}), thread("closed", 1, {attention: "archived"}),
    thread("info", 1, {needs_attention: false})]);
  await roll.start();
  assert.deepEqual(opened, ["old"]);
  assert.equal(roll.queue.length, 2);
  await roll.responded(roll.ticket());
  assert.deepEqual(opened, ["old", "new"]);
  await roll.responded(roll.ticket());
  assert.equal(roll.active, false);
  assert.equal(roll.done, true);
  assert.deepEqual(opened, ["old", "new"]);
});

test("refreshes never navigate; arrivals append, and a fast reply goes behind waiting work", async () => {
  const {roll, data, opened} = setup([thread("one", 1), thread("two", 2)]);
  await roll.start();
  const ticket = roll.ticket();
  data.threads.push(thread("arrival", 0));
  roll.sync(data.threads);
  assert.deepEqual(opened, ["one"]);
  assert.deepEqual(roll.queue.map(t => t.thread_id), ["one", "two", "arrival"]);
  data.threads[0] = thread("one", 5, {attention_key: "one:2"});
  await roll.responded(ticket);
  assert.deepEqual(roll.queue.map(t => t.thread_id), ["two", "arrival", "one"]);
});

test("skip excludes a thread for the entire pass without changing its attention", async () => {
  const {roll, data, opened} = setup([thread("one", 1), thread("two", 2)]);
  await roll.start(); await roll.skip();
  data.threads[0].attention_key = "one:2";
  roll.sync(data.threads);
  assert.deepEqual(roll.queue.map(t => t.thread_id), ["two"]);
  assert.equal(data.threads[0].needs_attention, true);
  await roll.skip();
  assert.equal(roll.done, true);
  await roll.start();
  assert.deepEqual(opened, ["one", "two", "one"]);
});

test("multiple unanswered requests keep the same conversation open", async () => {
  const {roll, opened} = setup([thread("one", 1), thread("two", 2)]);
  await roll.start();
  await roll.responded(roll.ticket(), {pending: true});
  assert.deepEqual(opened, ["one"]);
  await roll.responded(roll.ticket());
  assert.deepEqual(opened, ["one", "two"]);
});

test("external handling removes candidates without moving the current conversation", async () => {
  const {roll, data, opened} = setup([thread("one", 1), thread("two", 2), thread("three", 3)]);
  await roll.start(); data.threads[1].needs_attention = false; roll.sync(data.threads);
  assert.deepEqual(opened, ["one"]);
  await roll.responded(roll.ticket());
  assert.deepEqual(opened, ["one", "three"]);
});

test("a new response observed between polls rejoins at the tail", async () => {
  const {roll, data, opened} = setup([thread("one", 1), thread("two", 2), thread("three", 3)]);
  await roll.start();
  data.threads[1] = thread("two", 4, {attention_key: "two:2"});
  roll.sync(data.threads);
  assert.deepEqual(roll.queue.map(t => t.thread_id), ["one", "three", "two"]);
  assert.deepEqual(opened, ["one"]);
});

test("exiting during a load prevents delayed navigation and new passes ignore old submissions", async () => {
  const {roll, opened} = setup([thread("one", 1)]);
  let finish;
  roll.load = () => new Promise(resolve => { finish = resolve; });
  const start = roll.start(); roll.stop(); finish([thread("one", 1)]); await start;
  assert.deepEqual(opened, []);
  roll.load = async () => [thread("one", 1), thread("two", 2)];
  await roll.start(); const ticket = roll.ticket(); roll.stop(); await roll.start();
  await roll.responded(ticket);
  assert.deepEqual(opened, ["one", "one"]);
});

test("manual navigation and double submission cannot advance the wrong thread", async () => {
  const {roll, selected, opened} = setup([thread("one", 1), thread("two", 2)]);
  await roll.start(); const ticket = roll.ticket();
  selected("elsewhere"); await roll.responded(ticket);
  assert.deepEqual(opened, ["one"]);
  selected("one"); await Promise.all([roll.responded(ticket), roll.responded(ticket)]);
  assert.deepEqual(opened, ["one", "two"]);
});

test("load failures retain mode and allow retry, while empty passes complete", async () => {
  const {roll, opened} = setup([]);
  roll.load = async () => { throw new Error("Offline"); };
  await assert.rejects(roll.start(), /Offline/);
  assert.equal(roll.active, true); assert.equal(roll.busy, false);
  roll.load = async () => [];
  await roll.advance();
  assert.equal(roll.done, true); assert.deepEqual(opened, []);
});

test("each window owns an independent pass", async () => {
  const one = setup([thread("one", 1), thread("two", 2)]), two = setup([thread("one", 1), thread("two", 2)]);
  await one.roll.start(); await two.roll.start(); await one.roll.skip();
  assert.deepEqual(one.opened, ["one", "two"]); assert.deepEqual(two.opened, ["one"]);
});

test("ordinary navigation and pane status events do not redraw an inactive mode", () => {
  const {roll} = setup([]);
  let changes = 0;
  roll.changed = () => changes++;
  roll.stop(); roll.stop();
  assert.equal(changes, 0);
});

test("controls handle empty passes, skipped work, keyboard exit, and modal precedence", async () => {
  const nodes = new Map(), handlers = {}, opened = [];
  let dialog = false, menu = false;
  const document = {getElementById: id => {
    if (!nodes.has(id)) nodes.set(id, {hidden: false, setAttribute() {}, focus() {}, querySelector: () => null});
    return nodes.get(id);
  }, querySelector: selector => selector === "dialog[open]" ? dialog : menu,
  addEventListener: (name, fn) => { handlers[name] = fn; }};
  let items = [], operation;
  const state = {updatingThreads: new Set()};
  const roll = setupRollUp({document, state, api: async () => ({data: items}), panes: () => null,
    select: async id => { state.selected = id; opened.push(id); }, action: fn => { operation = fn(); }});
  nodes.get("roll-up-start").onclick(); await operation;
  assert.equal(nodes.get("roll-up-label").textContent, "All caught up");
  nodes.get("roll-up-exit").onclick();
  assert.equal(nodes.get("roll-up-bar").hidden, true);
  items = [thread("one", 1), thread("two", 2)];
  const key = {key: "®", code: "KeyR", altKey: true, metaKey: true, preventDefault() {}};
  dialog = true; handlers.keydown(key); assert.equal(roll.active, false);
  dialog = false; handlers.keydown(key); await operation;
  assert.deepEqual(opened, ["one"]);
  menu = true; handlers.keydown({key: "Escape", preventDefault() {}}); assert.equal(roll.active, true);
  menu = false;
  nodes.get("roll-up-skip").onclick(); await operation;
  nodes.get("roll-up-skip").onclick(); await operation;
  assert.equal(nodes.get("roll-up-label").textContent, "Pass complete · 2 skipped");
  handlers.keydown({key: "Escape", preventDefault() {}});
  assert.equal(nodes.get("roll-up-bar").hidden, true);
});

function appFixture() {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const elements = new Map(), advanced = [];
  const c = {state: {selected: "one", drafts: new Map(), commandFeedback: new Map(), updatingThreads: new Set(),
    sessions: [], detail: {work_thread: {thread_id: "one", browser_session: true, can_handle_response: true}, requests: []},
    rollUp: {ticket: id => ({id}), responded: async (ticket, options) => advanced.push({ticket, options})}},
    $: id => { if (!elements.has(id)) elements.set(id, {value: "A reply"}); return elements.get(id); },
    renderStatus() {}, renderList() {}, renderThread() {}, refreshDetail: async () => {}, refreshList: async () => {},
    sessionPath: id => "sessions/" + id, threadPath: id => "threads/" + id,
    api: async () => ({}), node: () => ({}), action: fn => fn()};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), c);
  for (const [start, end] of [["async function sendMessage(", '$("composer").onsubmit'],
    ["function requestButton(", "function renderRequests("],
    ["async function updateThreadAttention(", "function threadCard("]]) {
    vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end)), c);
  }
  return {c, advanced};
}

test("accepted messages advance, slash commands and failed sends do not", async () => {
  for (const outcome of ["accepted", "command", "failed"]) {
    const {c, advanced} = appFixture();
    c.api = async () => {
      if (outcome === "failed") throw new Error("Failed send");
      return outcome === "command" ? {command: {message: "Help"}} : {};
    };
    if (outcome === "failed") {
      await assert.rejects(c.sendMessage(), /Failed send/);
      assert.equal(c.$("message").value, "A reply");
    } else await c.sendMessage();
    assert.equal(advanced.length, outcome === "accepted" ? 1 : 0, outcome);
  }
});

test("response hooks pass remaining approvals to the queue and never send to a newly selected thread", async () => {
  const {c, advanced} = appFixture();
  const button = c.requestButton("Approve", "accept", {key: "approval"});
  c.state.detail.requests = [{key: "another"}];
  c.api = async path => { assert.equal(path, "sessions/one/requests/approval"); return {}; };
  await button.onclick();
  assert.equal(advanced[0].options.pending, true);
  c.state.selected = "two";
  await button.onclick();
  assert.equal(advanced.at(-1).ticket.id, "one");
});

test("Handled advances only after a successful acknowledgement", async () => {
  const {c, advanced} = appFixture();
  const thread = c.state.detail.work_thread;
  c.api = async () => { throw new Error("Offline"); };
  await assert.rejects(c.markThreadHandled(thread), /Offline/);
  assert.equal(advanced.length, 0);
  c.api = async () => ({...thread, needs_attention: false});
  await c.markThreadHandled(thread);
  assert.equal(advanced.length, 1);
});
