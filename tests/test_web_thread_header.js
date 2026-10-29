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

test("the header omits Your turn, Working, and work phases while preserving actionable status", () => {
  const c = setup();
  c.renderStatus();
  assert.equal(c.$("status").textContent, "");
  assert.equal(c.$("status").hidden, true);
  assert.equal(grouping.awaitsUser(c.state.detail.work_thread), true);
  for (const phase of ["new", "discussion", "investigation", "planning", "orchestrating", "implementation", "validation", "deployment", "finished"]) {
    Object.assign(c.state.detail.work_thread, {response_state: "available", work_phase: phase, checkpoint: {phase}});
    c.renderStatus();
    assert.equal(c.$("status").textContent, "");
    assert.equal(c.$("status").hidden, true);
  }
  Object.assign(c.state.detail.work_thread, {response_state: "action", attention_reason: "review"});
  c.renderStatus();
  assert.equal(c.$("status").textContent, "Review requested");
  assert.equal(c.$("status").hidden, false);
  Object.assign(c.state.detail.work_thread, {response_state: "interrupted"});
  c.renderStatus();
  assert.equal(c.$("status").textContent, "Stopped");
  assert.equal(c.$("status").hidden, false);
  Object.assign(c.state.detail.work_thread, {response_state: "available"});
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

test("elapsed time uses the current turn start across refreshes, steering and unit boundaries", () => {
  const c = setup(), started = 1800000000;
  Object.assign(c.state.detail, {running: true, activeTurn: "current", thread: {turns: [
    {id: "old", status: "inProgress", startedAt: started - 1000},
    {id: "current", status: "inProgress", startedAt: started},
  ]}});
  for (const [elapsed, expected] of [[0, "0s"], [59, "59s"], [60, "1m 0s"], [134, "2m 14s"], [3661, "1h 1m 1s"]]) {
    c.renderTurnElapsed(started + elapsed);
    assert.equal(c.$("turn-elapsed").hidden, false);
    assert.equal(c.$("turn-elapsed").textContent, "Working · " + expected);
  }
  c.state.detail = JSON.parse(JSON.stringify(c.state.detail));
  c.state.detail.thread.turns[1].items = [{type: "userMessage", content: [{text: "Steer this turn"}]}];
  c.renderTurnElapsed(started + 135);
  assert.equal(c.$("turn-elapsed").textContent, "Working · 2m 15s");
  c.state.detail.requests = [{}];
  c.renderTurnElapsed(started + 136);
  assert.equal(c.$("turn-elapsed").textContent, "Elapsed · 2m 16s");
});

test("elapsed time clears at completion, failure, stop and navigation, and resets for the next turn", () => {
  const c = setup(), started = 1800000000;
  Object.assign(c.state.detail, {running: true, activeTurn: "current", thread: {turns: [
    {id: "current", status: "inProgress", startedAt: started},
  ]}});
  c.state.sessions = [{thread_id: "one", status: "running"}]; // Inventory can lag behind streamed completion.
  c.renderTurnElapsed(started + 134);
  c.state.detail.running = false;
  c.renderTurnElapsed(started + 135);
  assert.equal(c.$("turn-elapsed").hidden, true);
  assert.equal(c.$("turn-elapsed").textContent, "");
  c.state.detail.running = true;
  for (const status of ["completed", "failed", "interrupted"]) {
    c.state.detail.thread.turns[0].status = status;
    c.renderTurnElapsed(started + 136);
    assert.equal(c.$("turn-elapsed").hidden, true);
  }
  c.state.detail.activeTurn = "next";
  c.renderTurnElapsed(started + 137);
  assert.equal(c.$("turn-elapsed").hidden, true); // Never borrow the previous turn's time.
  c.state.detail.thread.turns.push({id: "next", status: "inProgress", startedAt: started + 137});
  c.renderTurnElapsed(started + 138);
  assert.equal(c.$("turn-elapsed").textContent, "Working · 1s");
  c.state.detail = null;
  c.renderTurnElapsed(started + 139);
  assert.equal(c.$("turn-elapsed").hidden, true);
});

test("elapsed time handles persisted timestamps and hides missing or invalid timing", () => {
  const c = setup(), started = 1800000000;
  Object.assign(c.state.detail, {running: true, activeTurn: "current", thread: {turns: [
    {id: "current", status: "inProgress"},
  ]}});
  for (const value of [started, started * 1000, new Date(started * 1000).toISOString()]) {
    c.state.detail.thread.turns[0].startedAt = value;
    c.renderTurnElapsed(started + 134);
    assert.equal(c.$("turn-elapsed").textContent, "Working · 2m 14s");
  }
  for (const value of [undefined, null, "", "invalid", NaN, Infinity]) {
    c.state.detail.thread.turns[0].startedAt = value;
    c.renderTurnElapsed(started + 134);
    assert.equal(c.$("turn-elapsed").hidden, true);
  }
  delete c.state.detail.thread.turns[0].startedAt;
  Object.assign(c.state.detail.work_thread, {turn_id: "current", turn_started_at: started});
  c.renderTurnElapsed(started + 134);
  assert.equal(c.$("turn-elapsed").textContent, "Working · 2m 14s");
  c.state.detail.work_thread.turn_id = "old";
  c.renderTurnElapsed(started + 134);
  assert.equal(c.$("turn-elapsed").hidden, true);
  c.state.detail.thread.turns[0].startedAt = started + 140;
  c.renderTurnElapsed(started + 134);
  assert.equal(c.$("turn-elapsed").textContent, "Working · 0s");
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
  c.state.detail.work_thread.can_fork = true;
  c.renderStatus();
  assert.equal(c.$("fork-thread").hidden, false);
  assert.equal(!!c.$("fork-thread").disabled, false);
  c.state.detail.running = true;
  c.renderStatus();
  assert.equal(c.$("fork-thread").disabled, true);
  c.state.detail.work_thread.client = "kimi";
  c.renderStatus();
  assert.equal(c.$("fork-thread").hidden, true);
});

test("fork origin is a navigable text link and clears on thread changes", async () => {
  const c = setup();
  c.state.detail.work_thread.forked_from = {thread_id: "parent", title: "<Original>"};
  c.renderTitle();
  assert.equal(c.$("fork-origin").hidden, false);
  assert.equal(c.$("fork-origin").children[0], "Forked from ");
  const link = c.$("fork-origin").children[1];
  assert.equal(link.textContent, "<Original>");
  assert.equal(link.href, "#parent");
  c.action = fn => fn();
  c.select = async id => { c.navigated = id; };
  link.onclick({preventDefault() {}});
  assert.equal(c.navigated, "parent");
  c.state.detail.work_thread.forked_from.title = "Renamed source";
  c.renderTitle();
  assert.equal(c.$("fork-origin").children[1].textContent, "Renamed source");
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

test("card fork uses its source and opens the result unless the user navigates away", async () => {
  for (const initial of [null, "another"]) {
    for (const navigate of [false, true]) {
      const c = setup();
      c.state.selected = initial;
      c.select = async id => { c.navigated = id; };
      let finish;
      c.api = (path, body) => {
        assert.equal(path, "threads/card/fork");
        assert.equal(Object.keys(body).length, 0);
        return new Promise(resolve => { finish = resolve; });
      };
      vm.runInContext(source.slice(source.indexOf("async function forkThread("), source.indexOf("async function boot(")), c);
      const pending = c.forkThread(undefined, "card");
      if (navigate) c.state.selected = "elsewhere";
      finish({thread_id: "fork"});
      await pending;
      assert.equal(c.navigated, navigate ? undefined : "fork");
      assert.equal(c.state.forking, false);
    }
  }
});

test("the header menu lists visible thread actions and runs the original controls", () => {
  const buttons = [
    {textContent: "Fork", title: "Copy", hidden: false, disabled: true, click() {}},
    {textContent: "Move to Later", hidden: true, disabled: false, click() {}},
    {textContent: "Close thread", title: "", hidden: false, disabled: false, clicks: 0, click() { this.clicks++; }},
  ];
  const c = {document: {querySelectorAll: selector => selector === ".session-actions .thread-menu-action" ? buttons : []}};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function headerMenuItems("), source.indexOf('$("thread-actions").onclick')), c);
  const items = c.headerMenuItems();
  assert.equal(JSON.stringify(items.map(item => [item.label, item.disabled])), JSON.stringify([["Fork", true], ["Close thread", false]]));
  assert.equal(items[0].title, "Copy");
  items[1].run();
  assert.equal(buttons[2].clicks, 1);
});
