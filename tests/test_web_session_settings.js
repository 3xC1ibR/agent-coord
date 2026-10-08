"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const elements = new Map();
  const context = {$: id => { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); },
    state: {selected: "one", sessions: [{thread_id: "one"}], detail: {session: {model: "test-model", effort: "high", yolo: 1}, work_thread: {thread_id: "one", browser_session: true, attention: "now", title: "One"}, running: false}, commandFeedback: new Map(), drafts: new Map(), closing: new Set()},
    renderModelPicker() {}, renderStatus() {}, refreshDetail: async () => {}, refreshList: async () => {}, sessionPath: id => "sessions/" + id,
    threadPath: id => "threads/" + id,
    select: async id => { context.state.selected = id; },
    goHome: () => { context.state.selected = null; context.state.detail = null; }};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), context);
  vm.runInContext(source.slice(source.indexOf("function renderSessionSettings("), source.indexOf("function renderStatus(")), context);
  vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), context);
  vm.runInContext(source.slice(source.indexOf("async function toggleThreadClosed("), source.indexOf('$("close-thread").onclick')), context);
  vm.runInContext(source.slice(source.indexOf("async function forkThread("), source.indexOf("async function boot(")), context);
  return context;
}

test("status line shows effective settings and YOLO only for that session", () => {
  const c = setup();
  c.renderSessionSettings();
  assert.equal(c.$("session-model").textContent, "Model · test-model");
  assert.equal(c.$("session-effort").textContent, "Reasoning · high");
  assert.equal(c.$("session-yolo").hidden, false);
  assert.equal(c.$("edit-permissions").disabled, false);
  c.state.detail.session = {model: "another", effort: "low", yolo: 0};
  c.renderSessionSettings();
  assert.equal(c.$("session-model").textContent, "Model · another");
  assert.equal(c.$("session-yolo").hidden, true);
  c.state.detail.work_thread.browser_session = false;
  c.renderSessionSettings();
  assert.equal(c.$("session-settings").hidden, true);
});

test("permissions editor updates YOLO for an existing idle thread", async () => {
  const c = setup();
  const dialog = c.$("permissions-dialog");
  dialog.dataset = {};
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; };
  c.state.detail.session.yolo = 0;
  c.openSessionPermissions();
  assert.equal(dialog.open, true);
  assert.equal(dialog.dataset.threadId, "one");
  assert.equal(c.$("edit-yolo").checked, false);
  assert.equal(c.$("permissions-thread").textContent, "One");
  c.$("edit-yolo").checked = true;
  c.api = async (path, body) => {
    assert.equal(path, "sessions/one");
    assert.equal(body.yolo, true);
    return {...c.state.detail.session, yolo: 1};
  };
  await c.saveSessionPermissions();
  assert.equal(c.state.detail.session.yolo, 1);
  assert.equal(dialog.open, false);
  c.renderSessionSettings();
  assert.equal(c.$("session-yolo").hidden, false);
});

test("permissions editor waits for a turn and keeps the choice on save failure", async () => {
  const c = setup();
  const dialog = c.$("permissions-dialog");
  dialog.dataset = {};
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; };
  c.state.detail.running = true;
  c.renderSessionSettings();
  assert.equal(c.$("edit-permissions").disabled, true);
  c.openSessionPermissions();
  assert.equal(dialog.open, undefined);
  c.state.detail.running = false;
  c.openSessionPermissions();
  c.$("edit-yolo").checked = false;
  c.api = async () => { throw new Error("Could not save"); };
  await assert.rejects(c.saveSessionPermissions(), /Could not save/);
  assert.equal(dialog.open, true);
  assert.equal(c.$("edit-yolo").checked, false);
  assert.equal(c.state.detail.session.yolo, 1);
});

test("slash command feedback appears without fabricating conversation items", async () => {
  const c = setup();
  c.$("message").value = "/effort high";
  c.api = async (path, body) => {
    assert.equal(path, "sessions/one/messages");
    assert.equal(body.message, "/effort high");
    return {command: {message: "Next message: test-model · high reasoning."}};
  };
  await c.sendMessage();
  c.renderSessionSettings();
  assert.match(c.$("command-feedback").textContent, /high reasoning/);
  assert.equal(c.$("message").value, "");
  assert.equal(c.state.busy, false);
  c.state.selected = "two";
  c.renderSessionSettings();
  assert.equal(c.$("command-feedback").hidden, true);
});

test("failed commands keep the draft and running sessions cannot change settings", async () => {
  const c = setup();
  c.$("message").value = "/effort invalid";
  c.api = async () => { throw new Error("Unsupported effort"); };
  await assert.rejects(c.sendMessage(), /Unsupported/);
  assert.equal(c.$("message").value, "/effort invalid");
  assert.equal(c.state.busy, false);
  c.state.detail.running = true;
  c.state.detail.activeTurn = "turn-one";
  c.api = async () => { throw new Error("Wait for the running turn to finish before changing model or effort."); };
  await assert.rejects(c.sendMessage(), /Wait for the running turn/);
  assert.equal(c.$("message").value, "/effort invalid");
});

test("switching threads during a command preserves the new thread draft", async () => {
  const c = setup();
  c.$("message").value = "/model";
  c.api = async () => {
    c.state.selected = "two"; c.$("message").value = "draft two";
    return {command: {message: "Available models"}};
  };
  await c.sendMessage();
  assert.equal(c.$("message").value, "draft two");
  assert.equal(c.state.commandFeedback.get("one"), "Available models");
});

test("fork command uses the thread endpoint, clears the command and opens the fork", async () => {
  const c = setup();
  c.$("message").value = "  /FORK "; c.state.drafts.set("one", "  /FORK ");
  c.state.commandFeedback.set("one", "Old feedback");
  c.state.rollUp = {ticket() {}, responded() { assert.fail("A thread command must not count as a reply"); }};
  c.api = async (path, body) => {
    assert.equal(path, "threads/one/fork");
    assert.deepEqual(Object.keys(body), []);
    return {thread_id: "child"};
  };
  c.select = async id => {
    assert.equal(c.$("message").value, "", "Clear the command before navigation saves the source draft");
    c.state.selected = id;
  };
  await c.sendMessage();
  assert.equal(c.state.selected, "child");
  assert.equal(c.state.drafts.has("one"), false);
  assert.equal(c.state.commandFeedback.has("one"), false);
  assert.equal(c.state.forking, false);
  assert.equal(c.state.busy, false);
});

test("close commands run immediately for both providers even when queueing", async () => {
  for (const client of ["codex", "claude"]) {
    const c = setup();
    c.state.detail.work_thread.client = client;
    c.state.detail.running = true; c.state.detail.activeTurn = "turn";
    c.$("message").value = "/close";
    c.api = async (path, body) => {
      assert.equal(path, "threads/one/close");
      assert.deepEqual(Object.keys(body), []);
    };
    await c.sendMessage("queue");
    assert.equal(c.state.selected, null);
    assert.equal(c.state.sessions.length, 0);
    assert.equal(c.$("view").value, "active");
    assert.equal(c.$("message").value, "");
    assert.equal(c.state.closing.size, 0);
    assert.equal(c.state.busy, false);
  }
});

test("thread command errors preserve drafts and never fall through to model prompts", async () => {
  for (const command of ["/fork", "/close"]) {
    const c = setup();
    c.$("message").value = command; c.state.drafts.set("one", command);
    let calls = 0;
    c.api = async path => {
      assert.equal(path, "threads/one" + command); calls++;
      throw new Error(command === "/fork" ? "Fork requires an open, idle Codex thread" : "The turn is still stopping");
    };
    await assert.rejects(c.sendMessage(), /Fork requires|still stopping/);
    assert.equal(calls, 1);
    assert.equal(c.$("message").value, command);
    assert.equal(c.state.drafts.get("one"), command);
    assert.equal(c.state.selected, "one");
    assert.equal(c.state.busy, false);
    assert.equal(c.state.closing.size, 0);
  }
});

test("thread commands reject extra arguments and attachments without taking action", async () => {
  for (const command of ["/fork", "/close"]) {
    const c = setup();
    c.api = async () => assert.fail("Invalid command must not invoke an action or send a message");
    c.$("message").value = command + " extra";
    await assert.rejects(c.sendMessage(), /without arguments/);
    assert.equal(c.$("message").value, command + " extra");
    c.$("message").value = command;
    c.state.attachments = {snapshot: () => [{name: "image.png", url: "image"}], pending: () => false};
    await assert.rejects(c.sendMessage(), /Remove attached images/);
    assert.equal(c.$("message").value, command);
  }
});

test("navigation during thread commands preserves the new thread and draft", async () => {
  for (const command of ["/fork", "/close"]) {
    const c = setup();
    c.$("message").value = command; c.state.drafts.set("one", command);
    c.api = async path => {
      assert.equal(path, "threads/one" + command);
      c.state.selected = "two"; c.$("message").value = "Draft two";
      return {thread_id: "child"};
    };
    await c.sendMessage();
    assert.equal(c.state.selected, "two");
    assert.equal(c.$("message").value, "Draft two");
    assert.equal(c.state.drafts.has("one"), false);
  }
});

test("fork preserves text entered while the command runs", async () => {
  const c = setup();
  c.$("message").value = "/fork";
  c.api = async () => { c.$("message").value = "New source draft"; return {thread_id: "child"}; };
  c.select = async () => assert.equal(c.$("message").value, "New source draft");
  await c.sendMessage();
});
