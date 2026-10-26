"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {actionMessage, reconcileChildren, commit} = require("../plugins/agent-coord/scripts/agent_coord/web/openui.js");

test("UI responses preserve action and submitted values as a literal message", () => {
  const event = {type: "to_assistant", message: "/close", formState: {trip: {destination: {value: "Paris"}}}};
  const text = actionMessage(event);
  assert.ok(text.startsWith("OpenUI response:\n"));
  assert.deepEqual(JSON.parse(text.split("\n").slice(1).join("\n")), event);
  assert.throws(() => actionMessage(null, "x"), /Invalid/);
  assert.throws(() => actionMessage([], "x"), /Invalid/);
  assert.throws(() => actionMessage({text: "x".repeat(32768)}, "x"), /too large/);
});

class Element {
  constructor(name) { this.name = name; this.dataset = {}; this.childNodes = []; this.parentNode = null; this.moves = 0; }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() { return this.parentNode?.childNodes[this.parentNode.childNodes.indexOf(this) + 1] || null; }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1); this.parentNode = null;
  }
  insertBefore(child, cursor) {
    child.moves++;
    child.remove();
    this.childNodes.splice(cursor ? this.childNodes.indexOf(cursor) : this.childNodes.length, 0, child);
    child.parentNode = this;
  }
  querySelectorAll() { return []; }
}

test("refresh replaces surrounding rows without detaching an interactive message", () => {
  const timeline = new Element("timeline"), before = new Element("old-tool"), frame = new Element("interactive"), after = new Element("after");
  for (const item of [before, frame, after]) timeline.insertBefore(item, null);
  frame.moves = 0;
  const freshTool = new Element("fresh-tool"), newEnd = new Element("new-end");
  reconcileChildren(timeline, [freshTool, frame, newEnd]);
  assert.deepEqual(timeline.childNodes.map(item => item.name), ["fresh-tool", "interactive", "new-end"]);
  assert.equal(frame.moves, 0);
  assert.equal(before.parentNode, null);
  assert.equal(after.parentNode, null);
});

test("identical messages survive polling while streaming state changes", () => {
  const timeline = new Element("timeline");
  const card = () => {
    const value = new Element("card"); value.dataset = {openuiKey: "thread/turn/item", openuiText: "same", openuiStreaming: "true"}; return value;
  };
  const first = card(); commit(timeline, {childNodes: [first]}, {});
  first.moves = 0;
  const second = card(); second.dataset.openuiStreaming = "false";
  commit(timeline, {childNodes: [second]}, {});
  assert.equal(timeline.firstChild, first);
  assert.equal(first.dataset.openuiStreaming, "false");
  assert.equal(first.moves, 0);
  const third = card(); third.dataset.openuiText = "changed";
  commit(timeline, {childNodes: [third]}, {});
  assert.equal(timeline.firstChild, third);
});

test("late timestamp metadata updates without detaching the cached interactive message", () => {
  const timeline = new Element("timeline"), first = new Element("card"), next = new Element("card");
  for (const card of [first, next]) card.dataset = {openuiKey: "timestamp-thread/item", openuiText: "same"};
  const newTime = {outerHTML: '<time datetime="2026-10-26T16:00:00Z">Oct 26, 11:00 AM</time>'};
  first.time = {outerHTML: '<span>Time unavailable</span>', replaceWith(value) { first.time = value; }};
  first.querySelector = () => first.time;
  next.querySelector = () => newTime;
  commit(timeline, {childNodes: [first]}, {});
  first.moves = 0;
  commit(timeline, {childNodes: [next]}, {});
  assert.equal(timeline.firstChild, first);
  assert.equal(first.time, newTime);
  assert.equal(first.moves, 0);
});

function sender({running = false, archived = false, dispatcher = false, refreshFails = false} = {}) {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const calls = [];
  const state = {selected: "thread-a", detail: {running, work_thread: {browser_session: true, dispatcher, attention: archived ? "archived" : "now"}}};
  const context = {state, console: {warn() {}}, api: async (...args) => calls.push(args), sessionPath: id => "/sessions/" + id,
    refreshDetail: async () => { if (refreshFails) throw Error("offline"); }, refreshList: async () => {},
    DispatchUI: {requestKey: () => "request-id"}, sessionStorage: {}};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("async function sendOpenUIResponse("), source.indexOf("function focusSearchMessage(")), context);
  return {context, calls, state, send: (thread, text) => context.sendOpenUIResponse(thread, text)};
}

test("actions route to their originating thread and queue during a running turn", async () => {
  for (const running of [false, true]) {
    const {send, calls} = sender({running});
    await send("thread-a", "OpenUI response:\n{}");
    assert.equal(calls[0][0], "/sessions/thread-a/" + (running ? "queue" : "messages"));
    assert.equal(calls[0][1].message, "OpenUI response:\n{}");
    assert.equal(calls.length, 1);
  }
  const {send, calls} = sender({dispatcher: true, running: true});
  await send("thread-a", "OpenUI response:\n{}");
  assert.equal(calls[0][0], "/sessions/thread-a/messages");
  assert.equal(calls[0][1].askDispatcher, true);
});

test("closed and switched threads reject an action without sending it", async () => {
  for (const config of [{archived: true}, {}]) {
    const {send, calls} = sender(config);
    await assert.rejects(send(config.archived ? "thread-a" : "thread-b", "response"), /Reopen/);
    assert.equal(calls.length, 0);
  }
});

test("accepted responses remain successful if history refresh fails", async () => {
  const {send, calls} = sender({refreshFails: true});
  await send("thread-a", "response");
  assert.equal(calls.length, 1);
});

test("cached-tab restoration rebuilds detached interactive previews", () => {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const context = {state: {selected: "thread", detail: {}, conversations: new Map()}, isSessionDraft: () => false,
    $: () => ({childNodes: [{dataset: {openuiKey: "thread/turn/item"}}], scrollTop: 30})};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function conversationEntry("), source.indexOf("function sameConversation(")), context);
  vm.runInContext("retainConversation()", context);
  assert.equal(context.state.conversations.get("thread").nodes, null);
  assert.equal(context.state.conversations.get("thread").scroll, 30);
});
