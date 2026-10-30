"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const elements = new Map();
  const context = {
    $: id => { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); },
    state: {busy: false, selected: "one", sessions: [], closing: new Set(), commandFeedback: new Map(), drafts: new Map(),
      detail: {running: true, activeTurn: "turn-one", work_thread: {client: "codex", browser_session: true, attention: "now"}}},
    renderSessionSettings() {}, threadGrouping: {status: () => ({label: "Idle"})},
    refreshDetail: async () => {}, refreshList: async () => {}, sessionPath: id => "sessions/" + id, showError() {},
  };
  const storage = new Map();
  context.DispatchUI = require("../plugins/agent-coord/scripts/agent_coord/web/dispatch.js");
  context.sessionStorage = {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), context);
  vm.runInContext(source.slice(source.indexOf("function renderStatus("), source.indexOf("function itemText(")), context);
  vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), context);
  vm.runInContext(source.slice(source.indexOf("function composerKeydown("), source.indexOf('$("message").onkeydown')), context);
  context.$("message").value = "Focus on the UI first";
  return context;
}

test("running composer offers steering alongside Stop, then returns to Send", () => {
  const c = setup();
  c.renderStatus();
  assert.equal(c.$("send").disabled, false);
  assert.match(c.$("send").title, /Steer/);
  assert.equal(c.$("stop").hidden, false);
  assert.equal(c.$("queue").hidden, false);
  assert.equal(c.$("queue").disabled, false);
  assert.match(c.$("message").placeholder, /Tab to queue/);
  assert.match(c.$("message").placeholder, /steer/i);
  c.state.detail.running = false;
  c.renderStatus();
  assert.match(c.$("send").title, /Send/);
  assert.equal(c.$("stop").hidden, true);
  assert.equal(c.$("queue").hidden, true);
});

test("Tab queues a draft without steering, while Enter submits normally", async () => {
  const c = setup();
  c.renderStatus();
  let operation, prevented = 0, submits = 0;
  c.action = callback => { operation = callback(); };
  c.$("composer").requestSubmit = () => { submits++; };
  c.api = async (path, body) => {
    assert.equal(path, "sessions/one/queue");
    assert.equal(body.message, "Focus on the UI first");
    assert.equal("expectedTurnId" in body, false);
    return {queued: true, id: "queued-one"};
  };
  c.composerKeydown({key: "Tab", preventDefault() { prevented++; }});
  await operation;
  assert.equal(prevented, 1);
  assert.equal(c.$("message").value, "");
  assert.equal(c.state.detail.running, true);
  assert.equal(submits, 0);
  c.$("message").value = "Steer now";
  c.composerKeydown({key: "Enter", preventDefault() { prevented++; }});
  assert.equal(submits, 1);
  assert.equal(prevented, 2);
});

test("empty Tab, idle Tab, modified keys and IME retain normal keyboard behavior", () => {
  for (const mode of ["empty", "idle", "shiftKey", "ctrlKey", "altKey", "metaKey", "isComposing"]) {
    const c = setup();
    c.renderStatus();
    if (mode === "empty") c.$("message").value = "  ";
    if (mode === "idle") c.state.detail.running = false;
    c.action = () => assert.fail("Unexpected submission: " + mode);
    c.composerKeydown({key: "Tab", [mode]: true, preventDefault() { assert.fail("Intercepted Tab: " + mode); }});
  }
});

test("busy Tab does not submit twice and queue failures preserve the draft", async () => {
  const c = setup();
  c.state.busy = true; c.renderStatus();
  c.action = () => assert.fail("Duplicate submission");
  c.composerKeydown({key: "Tab", preventDefault() {}});
  c.state.busy = false;
  c.api = async () => { throw new Error("Queue unavailable"); };
  await assert.rejects(c.sendMessage("queue"), /Queue unavailable/);
  assert.equal(c.$("message").value, "Focus on the UI first");
  assert.equal(c.state.busy, false);
});

test("steering targets the visible active turn and clears only the sent draft", async () => {
  const c = setup();
  c.api = async (path, body) => {
    assert.equal(path, "sessions/one/messages");
    assert.equal(body.message, "Focus on the UI first");
    assert.equal(body.expectedTurnId, "turn-one");
    assert.equal(c.$("send").disabled, true);
    return {turnId: "turn-one"};
  };
  await c.sendMessage();
  assert.equal(c.$("message").value, "");
  assert.equal(c.state.busy, false);
  assert.equal(c.state.detail.running, true);
});

test("failed steering preserves the draft and permits retry", async () => {
  const c = setup();
  c.api = async () => { throw new Error("The active turn changed"); };
  await assert.rejects(c.sendMessage(), /active turn changed/);
  assert.equal(c.$("message").value, "Focus on the UI first");
  assert.equal(c.state.busy, false);
  assert.equal(c.$("send").disabled, false);
});

test("typing a new draft while steering is in flight preserves it", async () => {
  const c = setup();
  c.api = async () => { c.$("message").value = "Also check accessibility"; return {turnId: "turn-one"}; };
  await c.sendMessage();
  assert.equal(c.$("message").value, "Also check accessibility");
  assert.equal(c.state.drafts.get("one"), "Also check accessibility");
});

test("switching threads during steering preserves drafts in both threads", async () => {
  const c = setup();
  c.api = async () => {
    c.state.drafts.set("one", "A newer draft for one");
    c.state.selected = "two";
    c.$("message").value = "Draft for two";
    return {turnId: "turn-one"};
  };
  await c.sendMessage();
  assert.equal(c.$("message").value, "Draft for two");
  assert.equal(c.state.drafts.get("one"), "A newer draft for one");
});

test("idle messages omit the steering precondition", async () => {
  const c = setup();
  c.state.detail.running = false;
  c.api = async (path, body) => { assert.equal("expectedTurnId" in body, false); return {turn: {id: "new"}}; };
  await c.sendMessage();
});

test("closed threads and turns without an ID cannot submit steering", async () => {
  for (const mode of ["archived", "closing", "starting", "busy", "terminal"]) {
    const c = setup();
    if (mode === "archived") c.state.detail.work_thread.attention = "archived";
    if (mode === "closing") c.state.closing.add("one");
    if (mode === "starting") c.state.detail.activeTurn = null;
    if (mode === "busy") c.state.busy = true;
    if (mode === "terminal") c.state.detail.work_thread.browser_session = false;
    c.api = async () => assert.fail("Must not send: " + mode);
    c.renderStatus();
    assert.equal(c.$("send").disabled, true, mode);
    await c.sendMessage();
    assert.equal(c.$("message").value, "Focus on the UI first");
  }
});

test("accepted sends release the composer before detail and inventory refreshes finish", async () => {
  const c = setup();
  let accept, finishDetail, finishList, posts = 0;
  c.api = () => { posts++; return new Promise(resolve => { accept = resolve; }); };
  c.refreshDetail = () => new Promise(resolve => { finishDetail = resolve; });
  c.refreshList = () => new Promise(resolve => { finishList = resolve; });
  const sending = c.sendMessage();
  assert.equal(c.$("send").disabled, true);
  await c.sendMessage(); assert.equal(posts, 1);
  c.$("message").value = "Next draft";
  accept({turnId: "turn-one"}); await sending;
  assert.equal(c.$("send").disabled, false);
  assert.equal(c.state.sending.size, 0);
  assert.equal(c.$("message").value, "Next draft");
  assert.equal(c.state.drafts.get("one"), "Next draft");
  assert.ok(finishDetail); assert.ok(finishList);
  finishDetail(); finishList();
});

test("refresh failures after acceptance do not reject the send or restore sent text", async () => {
  const c = setup(), errors = [];
  c.api = async () => ({turnId: "turn-one"});
  c.refreshDetail = async () => { throw new Error("Detail unavailable"); };
  c.refreshList = async () => { throw new Error("Inventory unavailable"); };
  c.showError = error => errors.push(error.message);
  await c.sendMessage(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(c.$("message").value, "");
  assert.equal(c.$("send").disabled, false);
  assert.equal(errors.length, 2);
  assert.ok(errors.every(message => message.startsWith("Your message was received")));
});
