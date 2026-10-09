"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const clone = value => JSON.parse(JSON.stringify(value));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function fixture(id, text = "Saved answer", checkpoint = 1) {
  return {session: {cwd: "/project", client: "codex"}, running: false, requests: [], queuedMessages: [],
    work_thread: {thread_id: id, browser_session: true, title: id, attention: "now", checkpoint: {id: checkpoint}, turn_completion: {id: checkpoint}},
    thread: {turns: [{id: "turn", status: "completed", items: [{id: "answer", type: "agentMessage", text}]}]}};
}
function setup() {
  const elements = new Map(), replies = new Map(), calls = [];
  const element = (tag = "div", text = "") => ({tag, textContent: text, value: "", dataset: {}, hidden: false,
    childNodes: [], scrollTop: 0, classList: {add() {}, remove() {}, toggle() {}},
    replaceChildren(...nodes) { this.childNodes = nodes; }, append(...nodes) { this.childNodes.push(...nodes); },
    focus() {}, setAttribute() {}});
  const c = {state: {selected: null, detail: null, drafts: new Map(), sessions: [], config: {}, closing: new Set()}, calls, replies, base: "/api/browser/",
    $: id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, node: element,
    notifications: {syncFocus() {}, receive() {}}, savedViews: {remember() {}, restoreScroll() {}}, navigation: {remember() {}},
    setNavigation() {}, setChatExpanded() {}, isSessionDraft: () => c.state.selected === "new-session",
    renderTitle() {}, renderList() {}, renderThread() {}, renderRequests() {}, renderQueuedMessages() {}, renderStatus() {},
    refreshList: async () => {}, loadSessionModels() {}, sessionPath: id => "sessions/" + id, threadPath: id => "threads/" + id,
    scheduleDetail: () => { c.scheduled = (c.scheduled || 0) + 1; }, scheduleList() {}, showError() {},
    window: {addEventListener() {}}, EventSource: function () { c.events = this; },
    renderTimeline() {
      c.renders = (c.renders || 0) + 1;
      c.state.timelineThread = c.state.selected;
      c.$("timeline").replaceChildren(element("article", c.state.detail.thread.turns[0]?.items[0]?.text));
    },
    api: async (path, body) => {
      calls.push({path, body});
      if (body) return {};
      const id = path.split("/")[1];
      const detail = replies.get(id) || fixture(id);
      if (path.startsWith("threads/")) return clone(detail.work_thread);
      if (c.gate?.id === id) return c.gate.promise;
      return clone(detail);
    }};
  c.state.timelineScroll = {following: false, position: () => c.$("timeline").scrollTop,
    reset() {}, afterRender() {}, latest: () => { c.$("timeline").scrollTop = 999; },
    restore: top => { c.$("timeline").scrollTop = top; }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function conversationEntry("), source.indexOf("function renderThread(")), c);
  vm.runInContext(source.slice(source.indexOf("function goHome("), source.indexOf('document.querySelector(".brand")')), c);
  vm.runInContext(source.slice(source.indexOf("function applyEvent("), source.indexOf("let listTimer")), c);
  return c;
}
async function startRefresh(c, id) {
  c.gate = {id, ...deferred()};
  const opening = c.select(id);
  await Promise.resolve();
  await Promise.resolve();
  return {opening, gate: c.gate};
}

test("large conversation reopens immediately with the same nodes, draft and scroll", async () => {
  const c = setup();
  c.replies.set("one", fixture("one", "large transcript ".repeat(100000)));
  await c.select("one");
  c.$("message").value = "unsent draft"; c.$("timeline").scrollTop = 123;
  const original = c.$("timeline").childNodes[0];
  c.goHome();
  const {opening, gate} = await startRefresh(c, "one");
  assert.equal(c.$("timeline").childNodes[0], original);
  assert.equal(c.$("message").value, "unsent draft");
  assert.equal(c.$("timeline").scrollTop, 123);
  assert.equal(c.state.detailRefreshing, true);
  assert.equal(c.renders, 1);
  gate.resolve(clone(c.replies.get("one"))); await opening;
  assert.equal(c.state.detailRefreshing, false);
  assert.equal(c.$("timeline").childNodes[0], original);
  assert.equal(c.renders, 1, "unchanged background history must not rebuild the timeline");
});

test("switching conversations restores each retained transcript", async () => {
  const c = setup();
  await c.select("one"); const first = c.$("timeline").childNodes[0];
  await c.select("two"); const second = c.$("timeline").childNodes[0];
  assert.notEqual(first, second);
  const {opening, gate} = await startRefresh(c, "one");
  assert.equal(c.$("timeline").childNodes[0], first);
  assert.equal(c.renders, 2);
  gate.resolve(fixture("one")); await opening;
  assert.equal(c.renders, 2);
});

test("a draft placeholder cannot be mistaken for the retained conversation's nodes", async () => {
  const c = setup(); await c.select("one"); c.goHome();
  const original = c.$("timeline").childNodes[0];
  // The actual draft creation path invalidates the rendered thread identity.
  c.state.newSessionDraft = null;
  c.state.organization = {repositories: [], projects: []};
  c.threadOrganization = {NONE: "__none__"};
  c.state.drafts.set("new-session", "new draft");
  c.loadSessionModels = async () => {};
  c.state.config = {cwd: "/project"};
  c.sessionContext = () => ({cwd: "/project", project_id: null});
  c.renderModelPicker = () => {};
  vm.runInContext(source.slice(source.indexOf("async function newSession("), source.indexOf("async function sendSessionDraft(")), c);
  await c.newSession();
  assert.equal(c.state.timelineThread, null);
  c.goHome();
  const {opening, gate} = await startRefresh(c, "one");
  assert.equal(c.$("timeline").childNodes[0], original);
  gate.resolve(fixture("one")); await opening;
});

test("updates received in the overview refresh cached history and acknowledge only the fresh result", async () => {
  const c = setup(); await c.select("one"); c.goHome();
  c.applyEvent({method: "turn/completed", params: {threadId: "one", turn: {id: "new"}}});
  const {opening, gate} = await startRefresh(c, "one");
  assert.equal(c.$("timeline").childNodes[0].textContent, "Saved answer");
  const seenBefore = c.calls.filter(call => call.body?.seen).length;
  assert.equal(seenBefore, 1);
  const fresh = fixture("one", "New result", 2); c.replies.set("one", fresh);
  gate.resolve(clone(fresh)); await opening;
  assert.equal(c.$("timeline").childNodes[0].textContent, "New result");
  const seen = c.calls.filter(call => call.body?.seen).at(-1).body;
  assert.equal(seen.seen_checkpoint_id, 2); assert.equal(seen.seen_completion_id, 2);
});

test("a delayed background read cannot overwrite newer streamed text or a new approval", async () => {
  const c = setup(); await c.select("one"); c.goHome();
  const {opening, gate} = await startRefresh(c, "one");
  c.applyEvent({method: "item/agentMessage/delta", params: {threadId: "one", turnId: "turn", itemId: "answer", delta: " update"}});
  c.applyEvent({method: "item/commandExecution/requestApproval", requestKey: "new-approval", params: {threadId: "one"}});
  gate.resolve(fixture("one")); await opening;
  assert.equal(c.state.detail.thread.turns[0].items[0].text, "Saved answer update");
  assert.equal(c.state.detail.requests[0].key, "new-approval");
  assert.ok(c.scheduled > 0);
  assert.equal(c.state.detailRefreshing, true);
  c.gate = null;
  c.replies.set("one", clone(c.state.detail));
  await c.refreshDetail();
  assert.equal(c.state.detailRefreshing, false);
});

test("an old session response does not replace the conversation selected while it was loading", async () => {
  const c = setup(); await c.select("one"); c.goHome();
  const {opening, gate} = await startRefresh(c, "one");
  await c.select("two");
  const selectedNodes = c.$("timeline").childNodes[0];
  gate.resolve(fixture("one", "Updated while hidden")); await opening;
  assert.equal(c.state.selected, "two"); assert.equal(c.state.detail.work_thread.thread_id, "two");
  assert.equal(c.$("timeline").childNodes[0], selectedNodes);
  c.gate = null;
  const {opening: again, gate: next} = await startRefresh(c, "one");
  assert.equal(c.$("timeline").childNodes[0].textContent, "Updated while hidden");
  next.resolve(fixture("one", "Updated while hidden")); await again;
});

test("overlapping refreshes share a request, and failures keep the cached transcript", async () => {
  const c = setup(); await c.select("one"); c.goHome();
  const {opening, gate} = await startRefresh(c, "one");
  const another = c.refreshDetail();
  const failed = assert.rejects(opening, /offline/), failedAgain = assert.rejects(another, /offline/);
  gate.reject(new Error("offline")); await Promise.all([failed, failedAgain]);
  assert.equal(c.$("timeline").childNodes[0].textContent, "Saved answer");
  assert.equal(c.calls.filter(call => call.path === "sessions/one").length, 2);
  assert.equal(c.state.detailRefreshing, true);
  c.gate = null; await c.refreshDetail(); assert.equal(c.state.detailRefreshing, false);
});

test("cached approvals and sending remain unavailable until authoritative refresh", async () => {
  const c = setup();
  c.replies.set("one", {...fixture("one"), requests: [{key: "old", method: "unknown", params: {}}]});
  await c.select("one"); c.goHome();
  vm.runInContext(source.slice(source.indexOf("function renderRequests("), source.indexOf("function applyEvent(")), c);
  vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), c);
  const {opening, gate} = await startRefresh(c, "one");
  c.renderRequests(); assert.equal(c.$("requests").childNodes.length, 0);
  c.$("message").value = "must wait";
  const before = c.calls.length; await c.sendMessage(); assert.equal(c.calls.length, before);
  gate.resolve(fixture("one")); await opening;
  assert.equal(c.state.detailRefreshing, false);
});

test("an SSE reset invalidates an outstanding cached refresh", async () => {
  const c = setup(); await c.select("one"); c.goHome();
  vm.runInContext(source.slice(source.indexOf("function connect("), source.indexOf("function newProject(")), c);
  c.connect();
  const {opening, gate} = await startRefresh(c, "one");
  c.events.onmessage({data: JSON.stringify({reset: true, events: []})});
  gate.resolve(fixture("one", "obsolete")); await opening;
  assert.equal(c.state.detail.thread.turns[0].items[0].text, "Saved answer");
  assert.equal(c.state.detailRefreshing, true);
});

test("a completion notification without an item event invalidates an outstanding snapshot", async () => {
  const c = setup(); await c.select("one"); c.goHome();
  vm.runInContext(source.slice(source.indexOf("function connect("), source.indexOf("function newProject(")), c);
  c.connect();
  const {opening, gate} = await startRefresh(c, "one");
  c.events.onmessage({data: JSON.stringify({completions: [{thread_id: "one"}], events: []})});
  gate.resolve(fixture("one", "obsolete")); await opening;
  assert.equal(c.state.detail.thread.turns[0].items[0].text, "Saved answer");
  assert.ok(c.scheduled > 0);
});

test("the transcript cache evicts old conversations while retaining the current selection", async () => {
  const c = setup();
  for (const id of ["one", "two", "three", "four", "five", "six"]) await c.select(id);
  assert.equal(c.state.conversations.size, 4);
  assert.equal(c.state.conversations.has("one"), false);
  assert.equal(c.state.conversations.get("six").detail, c.state.detail);
});
