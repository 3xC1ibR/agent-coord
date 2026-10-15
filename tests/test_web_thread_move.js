"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const elements = new Map(), calls = [];
  const thread = {thread_id: "one", project_cwd: "/B", cwd: "/B", browser_session: true, client: "codex", attention: "now"};
  const c = {state: {selected: "one", folders: {projects: [{cwd: "/B"}, {cwd: "/A"}]}, detail: {work_thread: thread},
      threadTabs: {async close(id) { calls.push(["closeTab", id]); }}},
    $: id => { if (!elements.has(id)) elements.set(id, {hidden: true, replaceChildren(...items) {this.items = items;},
      showModal() {this.open = true;}, close() {this.open = false;}, focus() {this.focused = true;}}); return elements.get(id); },
    Option: function(label, value) {this.label = label; this.value = value;},
    threadPath: id => "threads/" + id,
    async api(path, body) {calls.push([path, body]); return {...thread, cwd: "/A", project_cwd: "/A"};},
    async openProjectDirectory(cwd) {calls.push(["openProject", cwd]);},
    async select(id) {calls.push(["select", id]);},
    async refreshList() {calls.push(["refreshList"]);}, action: fn => fn(), calls, thread,
  };
  vm.createContext(c);
  const helperStart = source.indexOf("function threadMoveUnavailable(");
  // The helper may live with renderStatus or with the move dialog functions.
  const helperEnd = source.indexOf("\n}", helperStart) + 2;
  vm.runInContext(source.slice(helperStart, helperEnd), c);
  let block = source.slice(source.indexOf("let movingThread ="), source.indexOf('$("organize-thread").onclick'));
  if (block.includes("function threadMoveUnavailable(")) block = block.replace(source.slice(helperStart, helperEnd), "");
  vm.runInContext(block, c);
  return c;
}

test("move offers other open projects and moves both values before navigating", async () => {
  const c = setup();
  c.openThreadMove(c.thread);
  assert.equal(c.$("move-thread-dialog").open, true);
  assert.deepEqual(c.$("move-thread-projects").items.map(item => item.value), ["/A"]);
  c.$("move-thread-directory").value = " /A ";
  await c.submitThreadMove();
  assert.equal(c.calls[0][0], "threads/one/move");
  assert.equal(c.calls[0][1].cwd, "/A");
  assert.deepEqual(c.calls.slice(1), [["closeTab", "one"], ["openProject", "/A"], ["select", "one"]]);
  assert.equal(c.$("move-thread-dialog").open, false);
});

test("failed move stays in original project with an inline error and retry", async () => {
  const c = setup();
  c.openThreadMove(c.thread);
  c.$("move-thread-directory").value = "/missing";
  c.api = async () => {throw new Error("Choose an existing project directory.");};
  await c.submitThreadMove();
  assert.equal(c.$("move-thread-dialog").open, true);
  assert.match(c.$("move-thread-error").textContent, /existing project/);
  assert.deepEqual(c.calls, []);
});

test("moving a card preserves navigation if the user is viewing another thread", async () => {
  const c = setup();
  c.openThreadMove(c.thread);
  c.state.selected = "two";
  c.$("move-thread-directory").value = "/A";
  await c.submitThreadMove();
  assert.deepEqual(c.calls.slice(1), [["refreshList"]]);
});

test("running, closed, terminal and fixed-workspace threads cannot open the move dialog", () => {
  const c = setup();
  for (const change of [{turn_active: true}, {status: "needs input"}, {attention: "archived"},
      {browser_session: false}, {client: "kimi"}, {client: "muse"}]) {
    c.openThreadMove({...c.thread, ...change});
    assert.equal(c.$("move-thread-dialog").open, undefined);
  }
  assert.equal(c.threadMoveUnavailable({...c.thread, client: "claude"}), "");
});

test("agent-initiated moves update inventory and follow the currently viewed thread", async () => {
  const c = setup();
  c.state.sessions = [c.thread];
  c.state.viewThreads = [c.thread];
  c.state.conversations = new Map([["one", {revision: 1, detail: c.state.detail}]]);
  c.showError = error => {throw error;};
  vm.runInContext(source.slice(source.indexOf("function applyEvent("), source.indexOf("let listTimer,")), c);
  c.applyEvent({method: "browser/changed", params: {threadId: "one", projectCwd: "/A"}});
  assert.equal(c.state.sessions[0].project_cwd, "/A");
  assert.equal(c.state.detail.work_thread.cwd, "/A");
  assert.deepEqual(c.calls, [["select", "one"]]);
});

test("a move notification for another thread updates inventory without navigating", () => {
  const c = setup();
  c.state.sessions = [c.thread];
  c.state.selected = "two";
  vm.runInContext(source.slice(source.indexOf("function applyEvent("), source.indexOf("let listTimer,")), c);
  c.applyEvent({method: "browser/changed", params: {threadId: "one", projectCwd: "/A"}});
  assert.equal(c.state.sessions[0].project_cwd, "/A");
  assert.deepEqual(c.calls, []);
});
