"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const grouping = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const elements = new Map();
  const c = {
    $: id => {
      if (!elements.has(id)) elements.set(id, {
        value: "", focus() { c.focused = id; }, select() { c.selectedText = id; },
        setCustomValidity(message) { this.validationMessage = message; },
        reportValidity() { this.reported = true; },
      });
      return elements.get(id);
    },
    state: {selected: "one", titleEdit: null, sessions: [], closing: new Set(),
      detail: {session: {}, work_thread: {thread_id: "one", title: "Original title", browser_session: true, attention: "now", response_state: "reply"}}},
    threadPath: id => "threads/" + id, threadGrouping: grouping,
    renderSessionSettings() {}, refreshThread: async () => {}, refreshList: async () => {},
  };
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function renderTitle("), source.indexOf("function renderSessionSettings(")), c);
  vm.runInContext(source.slice(source.indexOf("function renderStatus("), source.indexOf("function itemText(")), c);
  return c;
}

test("title editing preserves a draft through metadata refresh and supports cancellation", () => {
  const c = setup();
  c.startTitleEdit();
  assert.equal(c.$("rename-name").value, "Original title");
  assert.equal(c.focused, "rename-name");
  assert.equal(c.selectedText, "rename-name");
  c.$("rename-name").value = "Unsaved draft";
  c.state.detail.work_thread.title = "Updated elsewhere";
  c.renderTitle();
  assert.equal(c.$("rename-name").value, "Unsaved draft");
  c.cancelTitleEdit();
  assert.equal(c.state.titleEdit, null);
  assert.equal(c.$("rename-form").hidden, true);
  assert.equal(c.$("session-name").textContent, "Updated elsewhere");
  assert.equal(c.focused, "session-name");
});

test("saving an inline title persists trimmed text and returns focus to the title", async () => {
  const c = setup();
  c.startTitleEdit();
  c.$("rename-name").value = "  New title  ";
  c.api = async (path, body) => {
    assert.equal(path, "threads/one");
    assert.equal(body.title, "New title");
    c.refreshThread = async () => { c.state.detail.work_thread.title = body.title; };
  };
  await c.saveTitleEdit();
  assert.equal(c.state.titleEdit, null);
  assert.equal(c.$("rename-form").hidden, true);
  assert.equal(c.$("session-name").textContent, "New title");
  assert.equal(c.focused, "session-name");
});

test("blank titles stay editable without making a request", async () => {
  const c = setup();
  c.startTitleEdit();
  c.$("rename-name").value = "   ";
  c.api = async () => assert.fail("Blank titles must not be saved");
  await c.saveTitleEdit();
  assert.equal(c.$("rename-form").hidden, false);
  assert.ok(c.$("rename-name").validationMessage);
  assert.equal(c.$("rename-name").reported, true);
});

test("failed renames retain the draft and allow retrying", async () => {
  const c = setup();
  c.startTitleEdit();
  c.$("rename-name").value = "Keep this draft";
  c.api = async () => { throw new Error("Connection lost"); };
  await assert.rejects(c.saveTitleEdit(), /Connection lost/);
  assert.equal(c.$("rename-name").value, "Keep this draft");
  assert.equal(c.$("rename-name").disabled, false);
  assert.equal(c.$("save-rename").disabled, false);
  assert.equal(c.$("cancel-rename").disabled, false);
  assert.equal(c.$("rename-form").hidden, false);
});

test("a pending rename cannot be submitted twice or overwrite another thread editor", async () => {
  const c = setup();
  c.startTitleEdit();
  c.$("rename-name").value = "Saved on one";
  let resolve, requests = 0;
  c.api = () => { requests++; return new Promise(done => { resolve = done; }); };
  const saving = c.saveTitleEdit();
  await c.saveTitleEdit();
  assert.equal(requests, 1);
  c.state.selected = "two";
  c.state.detail.work_thread = {thread_id: "two", title: "Second thread"};
  c.state.titleEdit = null;
  c.startTitleEdit();
  c.$("rename-name").value = "Second thread draft";
  c.refreshThread = async () => assert.fail("Must not refresh the newly selected thread");
  resolve();
  await saving;
  assert.equal(c.state.titleEdit.id, "two");
  assert.equal(c.$("rename-name").value, "Second thread draft");
  assert.equal(c.$("rename-form").hidden, false);
  assert.equal(c.focused, "rename-name");
});

test("the header omits Your turn while preserving actionable and running states", () => {
  const c = setup();
  c.renderStatus();
  assert.equal(c.$("status").textContent, "");
  assert.equal(c.$("status").hidden, true);
  assert.equal(grouping.awaitsUser(c.state.detail.work_thread), true);
  c.state.detail.requests = [{}];
  c.renderStatus();
  assert.equal(c.$("status").textContent, "Needs input");
  assert.equal(c.$("status").hidden, false);
  c.state.detail.requests = [];
  c.state.detail.running = true;
  c.renderStatus();
  assert.equal(c.$("status").textContent, "Working");
});
