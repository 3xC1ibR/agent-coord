"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// Tiling copies the listed conversations' composer drafts into panes. Only a
// send or image read for one of those conversations can be duplicated or lost.
const thread = id => ({thread_id: id, title: "Thread " + id, attention: "now"});

function shellFixture() {
  const element = () => ({style: {}, hidden: false, children: [], dataset: {}, value: "", scrollTop: 0,
    classList: {add() {}, remove() {}, toggle() {}, contains: () => false},
    append(...children) { this.children.push(...children); }, replaceChildren() { this.children = []; },
    setAttribute() {}, querySelectorAll: () => [], getBoundingClientRect: () => ({width: 1000, height: 800, left: 0, top: 0})});
  const controls = new Map();
  const doc = {createElement: element, hasFocus: () => true, body: element(), querySelector: () => null,
    addEventListener() {}, getElementById: id => controls.get(id) || controls.set(id, element()).get(id)};
  const pending = new Set();
  const state = {selected: "a", drafts: new Map(), sending: new Set(),
    attachments: {drafts: new Map(), pending: id => pending.has(id), items(id) { return this.drafts.get(id) || []; }}};
  const context = vm.createContext({window: {addEventListener() {}, dispatchEvent() {}}, document: doc,
    location: {search: ""}, Event, URLSearchParams, setTimeout,
    threadViews: require("../plugins/agent-coord/scripts/agent_coord/web/views.js").threadViews});
  vm.runInContext(fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/thread-panes.js"), "utf8"), context);
  vm.runInContext("ThreadPanes.prototype.render = function() {}; ThreadPanes.prototype.position = function() {};", context);
  const errors = [], drafts = [];
  let threads = [thread("a"), thread("b")], shell;
  shell = context.setupPaneShell({state, document: doc, api: async path => thread(decodeURIComponent(path.split("/").at(-1))),
    select: async id => { state.selected = id; }, goHome() { shell.leave(); }, refreshList: async () => {},
    getView: () => "view", getThreads: () => threads, showError: error => errors.push(error.message), notifications: () => null});
  const getDraft = shell.getDraft;
  shell.getDraft = id => { drafts.push(id); return getDraft(id); };
  return {shell, state, pending, errors, drafts, setThreads: value => { threads = value; }};
}

test("a send or image for another conversation does not block tiling", async () => {
  const {shell, state, pending, errors} = shellFixture();
  state.sending.add("elsewhere"); pending.add("elsewhere");
  assert.equal(shell.tileCurrent(), true);
  assert.equal(shell.active, true);
  assert.deepEqual(errors, []);
});

test("a tiled conversation's unaccepted send or unread image blocks copying its draft", async () => {
  for (const block of ["send", "image"]) {
    const {shell, state, pending, errors, drafts} = shellFixture();
    if (block === "send") state.sending.add("b"); else pending.add("b");
    assert.equal(shell.tileCurrent(), false);
    assert.equal(shell.active, false);
    assert.deepEqual(drafts, []);
    assert.match(errors[0], /this conversation's send or image attachment/);
    // Once the send is accepted or the image is read, the same layout proceeds.
    state.sending.clear(); pending.clear();
    assert.equal(shell.tileCurrent(), true);
    assert.deepEqual([...new Set(drafts)].sort(), ["a", "b"]);
  }
});

test("a new conversation still being created blocks tiling", () => {
  const {shell, state, errors} = shellFixture();
  state.sending.add({draftId: "draft"});
  assert.equal(shell.tileCurrent(), false);
  assert.equal(errors.length, 1);
});

test("leaving tiles is not blocked by a send in this window", async () => {
  const {shell, state, errors} = shellFixture();
  assert.equal(shell.tileCurrent(), true);
  state.sending.add("a");
  assert.equal(await shell.toggleCurrent(), true);
  assert.equal(shell.active, false);
  assert.deepEqual(errors, []);
});

test("retiling and dragging check only the conversations they bring into panes", () => {
  const {shell, state, errors, setThreads} = shellFixture();
  assert.equal(shell.tileCurrent(), true);
  state.sending.add("c");
  assert.equal(shell.canDrag(thread("b")), true);
  assert.equal(shell.canDrag(thread("c")), false);
  setThreads([thread("a"), thread("b"), thread("c")]);
  assert.equal(shell.tileCurrent(), false);
  assert.equal(errors.length, 2);
});
