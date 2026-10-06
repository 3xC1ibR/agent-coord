"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const elements = new Map();
  const context = {$: id => { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); },
    state: {selected: "one", detail: {session: {model: "test-model", effort: "high", yolo: 1}, work_thread: {browser_session: true, attention: "now", title: "One"}, running: false}, commandFeedback: new Map(), drafts: new Map(), closing: new Set()},
    renderStatus() {}, refreshDetail: async () => {}, refreshList: async () => {}, sessionPath: id => "sessions/" + id};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function renderSessionSettings("), source.indexOf("function renderStatus(")), context);
  vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), context);
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
