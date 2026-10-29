"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const slice = (from, to) => source.slice(source.indexOf(from), source.indexOf(to));

function reader() {
  const calls = [];
  const element = () => ({dataset: {}, childNodes: [], classList: {add() {}, remove() {}, toggle() {}},
    replaceChildren() {}, append() {}, focus() {}, setAttribute() {}});
  const c = {state: {selected: "one", detail: null, sessions: [{thread_id: "one", browser_session: true}], config: {}, closing: new Set()},
    calls, AbortController, setTimeout, clearTimeout, $: () => element(), node: element,
    isSessionDraft: () => false, renderThread() {}, renderTimeline() {}, renderRequests() {}, renderQueuedMessages() {}, renderStatus() {},
    loadSessionModels() {}, scheduleDetail() {}, showError() {}, sessionPath: id => "sessions/" + id, threadPath: id => "threads/" + id,
    api: async (path, body, options = {}) => {
      calls.push({path, view: options.headers?.["X-Agent-Coord-History-View"] || null});
      if (path.startsWith("threads/")) return {thread_id: "one", browser_session: true, attention: "now"};
      return {session: {cwd: "/project"}, running: false, requests: [], work_thread: {thread_id: "one", browser_session: true, attention: "now"},
        thread: {turns: []}};
    }};
  vm.createContext(c);
  vm.runInContext(slice("function reconcileAttentionSnapshot(", "async function markThreadHandled("), c);
  vm.runInContext(slice("function conversationEntry(", "function renderThread("), c);
  return c;
}
const views = c => c.calls.filter(call => call.path === "sessions/one").map(call => call.view);

test("opening a conversation asks for its recent view", async () => {
  const c = reader();
  await c.refreshDetail();
  assert.deepEqual(views(c), ["recent"]);
});

test("a conversation that loaded its complete history keeps reading it", async () => {
  const c = reader();
  c.conversationEntry("one").completeHistory = true;
  await c.refreshDetail();
  assert.deepEqual(views(c), [null]);
});

test("a search result or message link into the conversation reads the complete history", async () => {
  for (const target of ["searchTarget", "messageTarget"]) {
    const c = reader();
    c.state[target] = {thread: "one", id: "7", turn: "t0", item: "a0"};
    await c.refreshDetail();
    c.state[target] = null;
    await c.refreshDetail();
    assert.deepEqual(views(c), [null, null], target);
    assert.equal(c.conversationEntry("one").allTurns, true);
  }
});

function router({recentFirst, found}) {
  const c = {state: {selected: null, detail: null}, entries: new Map(), reads: [], focusCalls: 0};
  Object.assign(c, {
    conversationEntry: id => { if (!c.entries.has(id)) c.entries.set(id, {}); return c.entries.get(id); },
    select: async id => { c.state.selected = id; c.state.detail = {thread: recentFirst ? {view: "recent", turns: []} : {turns: []}}; },
    refreshDetail: async () => { c.reads.push(c.state.searchTarget); c.state.detail = {thread: {turns: []}}; },
    focusSearchMessage: () => { c.focusCalls += 1; return found(c); }});
  vm.createContext(c);
  vm.runInContext(slice("async function openSearchTarget(", "function focusAgentMessage("), c);
  return c;
}
const route = {kind: "thread", id: "one", turn: "t0", item: "a0"};

test("a search result sets its target before the conversation opens", async () => {
  const c = router({recentFirst: false, found: () => true});
  let seen;
  c.select = async id => { seen = c.state.searchTarget; c.state.selected = id; c.state.detail = {thread: {turns: []}}; };
  await c.openSearchTarget(route);
  assert.deepEqual({...seen}, {thread: "one", turn: "t0", item: "a0"});
  assert.equal(c.reads.length, 0);
});

test("a search result found only after a recent read reads the complete history once", async () => {
  const c = router({recentFirst: true, found: c => c.state.detail.thread.view !== "recent"});
  await c.openSearchTarget(route);
  assert.deepEqual(c.reads.map(read => ({...read})), [{thread: "one", turn: "t0", item: "a0"}]);
  assert.equal(c.focusCalls, 2);
});

test("a search result missing from the complete history still reports it", async () => {
  const c = router({recentFirst: false, found: () => false});
  await assert.rejects(c.openSearchTarget(route), /no longer available/);
  assert.equal(c.reads.length, 0);
});
