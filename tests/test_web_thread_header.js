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
        replaceChildren(...children) { this.children = children; },
        append(...children) { this.children.push(...children); },
        setCustomValidity(message) { this.validationMessage = message; },
        reportValidity() { this.reported = true; },
      });
      return elements.get(id);
    },
    state: {selected: "one", titleEdit: null, sessions: [], closing: new Set(),
      detail: {session: {}, work_thread: {thread_id: "one", title: "Original title", browser_session: true, attention: "now", response_state: "reply"}}},
    threadPath: id => "threads/" + id, threadGrouping: grouping,
    node: (tag, text) => ({tag, textContent: text}),
    renderSessionSettings() {}, refreshThread: async () => {}, refreshList: async () => {},
  };
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), c);
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

test("the header omits Your turn and Working while preserving requests for input", () => {
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
  assert.equal(c.$("status").textContent, "");
  assert.equal(c.$("status").hidden, true);
});

test("fork availability follows backend eligibility and live activity", () => {
  const c = setup();
  Object.assign(c.state.detail.work_thread, {client: "codex", can_fork: true});
  c.renderStatus();
  assert.equal(c.$("fork-thread").hidden, false);
  assert.equal(!!c.$("fork-thread").disabled, false);
  c.state.detail.running = true;
  c.renderStatus();
  assert.equal(c.$("fork-thread").disabled, true);
  c.state.detail.running = false;
  c.state.detail.work_thread.can_fork = false;
  c.renderStatus();
  assert.equal(c.$("fork-thread").disabled, true);
  c.state.detail.work_thread.client = "claude";
  c.renderStatus();
  assert.equal(c.$("fork-thread").hidden, true);
});

test("fork origin is a navigable text link and clears on thread changes", async () => {
  const c = setup();
  c.state.detail.work_thread.forked_from = {thread_id: "parent", title: "<Original>"};
  c.renderTitle();
  const link = c.$("fork-origin").children[1];
  assert.equal(link.textContent, "<Original>");
  assert.equal(link.href, "#parent");
  c.action = fn => fn();
  c.select = async id => { c.navigated = id; };
  link.onclick({preventDefault() {}});
  assert.equal(c.navigated, "parent");
  c.state.detail = null;
  c.renderTitle();
  assert.equal(c.$("fork-origin").hidden, true);
  assert.equal(c.$("fork-origin").children.length, 0);
});

test("fork action prevents duplicate requests and does not steal changed selection", async () => {
  const c = setup();
  Object.assign(c.state.detail.work_thread, {client: "codex", can_fork: true});
  c.action = fn => fn();
  let resolve, requests = 0;
  c.api = (path, body) => {
    assert.equal(path, "threads/one/fork");
    assert.equal(Object.keys(body).length, 0);
    requests++;
    return new Promise(done => { resolve = done; });
  };
  c.select = async id => { c.navigated = id; };
  vm.runInContext(source.slice(source.indexOf('$("fork-thread").onclick'), source.indexOf("async function boot(")), c);
  const pending = c.$("fork-thread").onclick();
  await c.$("fork-thread").onclick();
  assert.equal(requests, 1);
  c.state.selected = "two";
  resolve({thread_id: "fork"});
  await pending;
  assert.equal(c.navigated, undefined);
  assert.equal(c.state.forking, false);
  c.state.selected = "one";
  const next = c.$("fork-thread").onclick();
  resolve({thread_id: "fork-two"});
  await next;
  assert.equal(c.navigated, "fork-two");
});
