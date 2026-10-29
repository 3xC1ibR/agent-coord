"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function fixture(id, browser = true) {
  return {session: {cwd: "/project", client: "codex"}, running: false, requests: [], queuedMessages: [],
    work_thread: {thread_id: id, browser_session: browser, title: id, attention: "now", checkpoint: {id: 1}, turn_completion: {id: 1}},
    thread: {turns: [{id: "turn", status: "completed", items: [{id: "answer", type: "agentMessage", text: "Saved"}]}]}};
}

// Each read waits for the test to answer it, so request overlap is observable.
function setup() {
  const elements = new Map(), requests = [];
  const element = () => ({textContent: "", value: "", dataset: {}, hidden: false, childNodes: [], scrollTop: 0,
    classList: {add() {}, remove() {}, toggle() {}}, replaceChildren(...nodes) { this.childNodes = nodes; },
    append(...nodes) { this.childNodes.push(...nodes); }, focus() {}, setAttribute() {}});
  const c = {state: {selected: null, detail: null, drafts: new Map(), sessions: [], config: {}, closing: new Set()}, requests,
    base: "/api/browser/", AbortController, setTimeout, clearTimeout,
    $: id => elements.get(id) || elements.set(id, element()).get(id), node: element,
    notifications: {syncFocus() {}, receive() {}}, savedViews: {remember() {}}, navigation: {remember() {}},
    setNavigation() {}, setChatExpanded() {}, isSessionDraft: () => false,
    renderTitle() {}, renderList() {}, renderThread() {}, renderRequests() {}, renderQueuedMessages() {}, renderStatus() {},
    renderTimeline() {}, refreshList: async () => {}, loadSessionModels() {}, scheduleDetail() {}, showError() {},
    sessionPath: id => "sessions/" + id, threadPath: id => "threads/" + id, window: {addEventListener() {}},
    api(path, body) {
      if (body) return Promise.resolve({});
      const request = {path, ...deferred()};
      requests.push(request);
      return request.promise;
    }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function reconcileAttentionSnapshot("), source.indexOf("async function markThreadHandled(")), c);
  vm.runInContext(source.slice(source.indexOf("function conversationEntry("), source.indexOf("function renderThread(")), c);
  return c;
}
const settle = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
const answer = (c, path, value) => c.requests.find(request => request.path === path).resolve(structuredClone(value));

test("a known app conversation reads its thread and session together", async () => {
  const c = setup(), detail = fixture("one");
  c.state.sessions = [detail.work_thread];
  const opening = c.select("one", {refreshInventory: false, focus: false});
  await settle();
  // Both reads are in flight before either answers: open time is the slower read, not the sum.
  assert.deepEqual(c.requests.map(request => request.path), ["sessions/one", "threads/one"]);
  answer(c, "sessions/one", detail);
  answer(c, "threads/one", detail.work_thread);
  await settle();
  c.requests.filter(request => request.path === "threads/one").at(-1).resolve({});
  await opening;
  assert.equal(c.state.detail.work_thread.thread_id, "one");
  assert.equal(c.state.detailRefreshing, false);
});

test("an unknown conversation reads its session only after learning it is an app conversation", async () => {
  const c = setup(), detail = fixture("one");
  const opening = c.select("one", {refreshInventory: false, focus: false});
  await settle();
  assert.deepEqual(c.requests.map(request => request.path), ["threads/one"]);
  answer(c, "threads/one", detail.work_thread);
  await settle();
  assert.deepEqual(c.requests.map(request => request.path), ["threads/one", "sessions/one"]);
  answer(c, "sessions/one", detail);
  await settle();
  c.requests.filter(request => request.path === "threads/one").at(-1).resolve({});
  await opening;
  assert.equal(c.state.detail.thread.turns.length, 1);
});

test("a stale app-conversation hint uses the coordination timeline without an unhandled failure", async () => {
  const c = setup(), stale = fixture("one"), current = fixture("one", false);
  c.state.sessions = [stale.work_thread];
  const unhandled = [];
  const record = reason => unhandled.push(reason);
  process.on("unhandledRejection", record);
  try {
    const opening = c.select("one", {refreshInventory: false, focus: false});
    await settle();
    c.requests.find(request => request.path === "sessions/one").reject(new Error("Browser session not found in this workspace."));
    answer(c, "threads/one", current.work_thread);
    await settle();
    answer(c, "threads/one/messages", {data: [{id: 7}]});
    await settle();
    c.requests.filter(request => request.path === "threads/one").at(-1).resolve({});
    await opening;
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(c.state.detail.coordinationMessages, [{id: 7}]);
    assert.deepEqual(unhandled, []);
  } finally { process.off("unhandledRejection", record); }
});
