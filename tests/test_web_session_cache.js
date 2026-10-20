"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {ThreadTabs} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-tabs.js");
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
    scheduleTimelineRender() { c.renderTimeline(); c.renderStatus(); },
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
  vm.runInContext(source.slice(source.indexOf("function reconcileAttentionSnapshot("), source.indexOf("async function markThreadHandled(")), c);
  vm.runInContext(source.slice(source.indexOf("function conversationEntry("), source.indexOf("function renderThread(")), c);
  vm.runInContext(source.slice(source.indexOf("function goHome("), source.indexOf('$("back-home").onclick')), c);
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

test("agent-tree navigation swaps transcripts, drafts and scroll inside the original tab", async () => {
  const c = setup();
  c.state.threadTabs = new ThreadTabs({});
  c.state.threadTabs.setProject("/project");
  c.replies.set("root", fixture("root", "Orchestrator history"));
  c.replies.set("child", fixture("child", "Child history"));
  await c.select("root");
  c.state.threadTabs.setTree("root", true);
  c.$("message").value = "Root draft";
  c.$("timeline").scrollTop = 123;
  const rootNodes = c.$("timeline").childNodes[0];
  await c.select("child", {tabId: "root", focus: false});
  assert.equal(c.$("timeline").childNodes[0].textContent, "Child history");
  assert.equal(c.$("message").value, "");
  assert.equal(c.state.selected, "child");
  assert.equal(c.state.threadTabs.selected, "root");
  assert.deepEqual(c.state.threadTabs.items.map(t => t.thread_id), ["root"]);
  assert.equal(c.state.threadTabs.conversationFor("root"), "child");
  c.$("message").value = "Child draft"; c.$("timeline").scrollTop = 456;
  await c.select("root", {tabId: "root", focus: false});
  assert.equal(c.$("timeline").childNodes[0], rootNodes);
  assert.equal(c.$("message").value, "Root draft");
  assert.equal(c.$("timeline").scrollTop, 123);
  await c.select("child", {tabId: "root", focus: false});
  assert.equal(c.$("message").value, "Child draft");
  assert.equal(c.$("timeline").scrollTop, 456);
});

test("a slow tree selection cannot overwrite a more recently selected sibling", async () => {
  const c = setup();
  c.state.threadTabs = new ThreadTabs({}); c.state.threadTabs.setProject("/project");
  await c.select("root");
  c.gate = {id: "slow", ...deferred()};
  const slow = c.select("slow", {tabId: "root", focus: false});
  await Promise.resolve(); await Promise.resolve();
  await c.select("fast", {tabId: "root", focus: false});
  c.gate.resolve(fixture("slow", "Late response")); await slow;
  assert.equal(c.state.selected, "fast");
  assert.equal(c.state.detail.work_thread.thread_id, "fast");
  assert.equal(c.state.threadTabs.selected, "root");
  assert.equal(c.state.threadTabs.conversationFor("root"), "fast");
});

test("opening loads model controls even when a streamed update invalidates its first snapshot", async () => {
  const c = setup(); let catalogs = 0;
  c.loadSessionModels = () => { catalogs++; };
  const {opening, gate} = await startRefresh(c, "one");
  c.applyEvent({method: "turn/completed", params: {threadId: "one", turn: {id: "new"}}});
  gate.resolve(fixture("one")); await opening;
  assert.equal(c.state.detailRefreshing, true);
  assert.ok(c.scheduled > 0);
  assert.equal(catalogs, 1, "Reasoning choices must not depend on an uninterrupted snapshot");
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

test("an approval notification recovers missing controls without an item event", async () => {
  const c = setup(); await c.select("one");
  vm.runInContext(source.slice(source.indexOf("function connect("), source.indexOf("function newProject(")), c);
  c.connect();
  c.$("message").value = "Keep this draft";
  const request = {key: "approval", method: "item/commandExecution/requestApproval", params: {reason: "Bash"}};
  c.replies.set("one", {...fixture("one"), requests: [request]});
  c.events.onmessage({data: JSON.stringify({approvals: [{thread_id: "one", request_key: "approval"}], events: []})});
  assert.ok(c.scheduled > 0, "notification must recover the authoritative pending request");
  await c.refreshDetail();
  assert.equal(c.state.detail.requests[0].key, "approval");
  assert.equal(c.$("message").value, "Keep this draft");
});

function readStateSetup() {
  const c = setup();
  c.document = {hidden: false, hasFocus: () => true};
  c.state.threadTabs = new ThreadTabs({});
  c.state.threadTabs.setProject("/project");
  const api = c.api;
  c.api = async (path, body) => {
    if (body?.seen) {
      const work = c.replies.get(path.split("/")[1])?.work_thread;
      if (work) work.unread_result = work.turn_completion.id > body.seen_completion_id;
    }
    return api(path, body);
  };
  c.refreshList = async () => c.state.threadTabs.update([...c.replies.values()].map(detail => detail.work_thread));
  c.reply = (id, completion) => {
    const detail = fixture(id, "Reply " + completion, completion);
    detail.work_thread.unread_result = true;
    c.replies.set(id, detail);
  };
  return c;
}

test("a reply displayed in the current tab stays read after switching away", async () => {
  const c = readStateSetup();
  await c.select("one");
  c.reply("one", 2); c.reply("other", 3);
  await c.refreshDetail();
  assert.equal(c.$("timeline").childNodes[0].textContent, "Reply 2");
  assert.equal(c.replies.get("one").work_thread.unread_result, false);
  assert.equal(c.calls.filter(call => call.body?.seen).at(-1).body.seen_completion_id, 2);
  await c.select("two");
  assert.equal(c.state.threadTabs.items.find(item => item.thread_id === "one").unread_result, false);
  assert.equal(c.replies.get("other").work_thread.unread_result, true, "background replies stay unread");
});

test("hidden, unfocused and collapsed conversations do not consume new replies", async () => {
  for (const hide of [c => { c.document.hidden = true; }, c => { c.document.hasFocus = () => false; },
    c => { c.$("conversation").hidden = true; }, c => {
      c.state.dispatchDock = {active: "one", expanded: false}; c.replies.get("one").work_thread.dispatcher = true;
    }]) {
    const c = readStateSetup(); await c.select("one");
    const receipts = c.calls.filter(call => call.body?.seen).length;
    c.reply("one", 2); hide(c);
    await c.refreshDetail();
    assert.equal(c.calls.filter(call => call.body?.seen).length, receipts);
    assert.equal(c.replies.get("one").work_thread.unread_result, true);
  }
});

test("a read receipt cannot consume a reply arriving after the displayed snapshot", async () => {
  const c = readStateSetup(); await c.select("one");
  c.reply("one", 2);
  const api = c.api;
  c.api = async (path, body) => {
    if (body?.seen) {
      c.reply("one", 3);
      c.state.conversations.get("one").revision++;
    }
    return api(path, body);
  };
  await c.refreshDetail();
  assert.equal(c.calls.filter(call => call.body?.seen).at(-1).body.seen_completion_id, 2);
  assert.equal(c.replies.get("one").work_thread.unread_result, true);
  assert.equal(c.state.threadTabs.items.find(item => item.thread_id === "one").unread_result, true);
  assert.ok(c.scheduled > 0, "updates arriving during the receipt get another detail refresh");
});

test("a fresh live snapshot acknowledges a reply after a previously invalidated refresh", async () => {
  const c = readStateSetup(); await c.select("one");
  c.state.detailRefreshing = true;
  c.reply("one", 2);
  await c.refreshDetail();
  assert.equal(c.replies.get("one").work_thread.unread_result, false);
});

test("the transcript cache evicts old conversations while retaining the current selection", async () => {
  const c = setup();
  for (const id of ["one", "two", "three", "four", "five", "six"]) await c.select(id);
  assert.equal(c.state.conversations.size, 4);
  assert.equal(c.state.conversations.has("one"), false);
  assert.equal(c.state.conversations.get("six").detail, c.state.detail);
});

test("tab navigation preserves drafts and reading positions beyond the transcript cache limit", async () => {
  const c = setup();
  const tabs = c.state.threadTabs = new ThreadTabs({onSelect: id => id ? c.select(id) : c.goHome()});
  tabs.setProject("/project");
  for (const [index, id] of ["one", "two", "three", "four", "five", "six"].entries()) {
    await c.select(id);
    c.$("message").value = "Draft " + id;
    c.$("timeline").scrollTop = 100 + index;
  }
  assert.equal(c.state.conversations.has("one"), false);
  assert.equal(tabs.items.length, 6);
  await tabs.activate(null);
  assert.equal(c.$("welcome").hidden, false);
  await tabs.activate("one");
  assert.equal(c.$("message").value, "Draft one");
  assert.equal(c.$("timeline").scrollTop, 100);
  assert.equal(tabs.items.length, 6);
  await tabs.close("one");
  assert.equal(c.state.selected, "two");
  assert.equal(c.$("message").value, "Draft two");
  assert.equal(c.$("timeline").scrollTop, 101);
  assert.equal(c.calls.some(call => call.body?.attention || call.path.endsWith("/stop")), false);
  await c.select("one");
  assert.equal(c.$("message").value, "Draft one", "closing a tab keeps its draft for later reopening");
});

function projectSetup(storage) {
  const c = setup();
  c.state.projectCwd = "/a";
  c.state.folders = {selected: {cwd: "/a"}, adopt(folder) { this.selected = folder; }};
  c.savedViews.setProject = async () => {};
  c.renderFolders = () => {};
  c.sessionContext = () => ({cwd: c.state.projectCwd});
  c.state.threadTabs = new ThreadTabs({storage, onSelect: id => id ? c.select(id) : c.goHome()});
  c.state.threadTabs.setProject("/a");
  c.state.attachments = {drafts: new Map(), highlight() {}};
  c.state.mentions = {drafts: new Map()};
  for (const [id, cwd] of [["one", "/a"], ["two", "/a"], ["three", "/b"]]) {
    const detail = fixture(id); detail.work_thread.project_cwd = cwd;
    c.replies.set(id, detail); c.state.sessions.push(detail.work_thread);
  }
  vm.runInContext(source.slice(source.indexOf("async function switchProject("), source.indexOf("function renderFolders(")), c);
  vm.runInContext(source.slice(source.indexOf("async function newSession("), source.indexOf("async function sendSessionDraft(")), c);
  c.switchTo = async cwd => { c.state.folders.adopt({cwd}); await c.switchProject(); };
  return c;
}

test("project tabs restore independent conversations, drafts and reading positions", async () => {
  const c = projectSetup();
  await c.select("one"); await c.select("two");
  c.$("message").value = "Draft A"; c.$("timeline").scrollTop = 123;
  await c.switchTo("/b");
  assert.equal(c.state.selected, null, "a new project starts at Overview");
  await c.select("three");
  c.$("message").value = "Draft B"; c.$("timeline").scrollTop = 456;
  await c.switchTo("/a");
  assert.equal(c.state.selected, "two"); assert.equal(c.state.threadTabs.selected, "two");
  assert.equal(c.$("message").value, "Draft A"); assert.equal(c.$("timeline").scrollTop, 123);
  assert.deepEqual(c.state.threadTabs.items.map(item => item.thread_id), ["one", "two"]);
  await c.switchTo("/b");
  assert.equal(c.state.selected, "three");
  assert.equal(c.$("message").value, "Draft B"); assert.equal(c.$("timeline").scrollTop, 456);
  await c.switchTo("/b");
  assert.equal(c.state.selected, "three", "clicking the current project keeps its conversation");
});

test("project restoration shows cached history before views, inventory or session requests finish", async () => {
  const c = projectSetup();
  await c.select("one");
  c.$("message").value = "Draft A"; c.$("timeline").scrollTop = 321;
  const original = c.$("timeline").childNodes[0];
  await c.switchTo("/b"); await c.select("three");
  const views = deferred(), inventory = deferred();
  c.savedViews.setProject = () => views.promise;
  c.gate = {id: "one", ...deferred()};
  let refreshes = 0;
  c.refreshList = () => { refreshes++; return inventory.promise; };
  const renders = [];
  c.renderList = () => renders.push(c.state.selected);
  const switching = c.switchTo("/a");
  assert.equal(c.state.selected, "one"); assert.equal(c.state.threadTabs.selected, "one");
  assert.equal(c.$("welcome").hidden, true); assert.equal(c.$("conversation").hidden, false);
  assert.equal(c.$("timeline").childNodes[0], original);
  assert.equal(c.$("message").value, "Draft A"); assert.equal(c.$("timeline").scrollTop, 321);
  assert.ok(renders.length); assert.equal(renders.includes(null), false, "returning to a conversation must not render Overview");
  assert.equal(refreshes, 0);
  views.resolve(); c.gate.resolve(clone(c.replies.get("one")));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(refreshes, 1, "one inventory refresh is sufficient for the restored project");
  assert.equal(c.state.selected, "one"); assert.equal(c.$("welcome").hidden, true);
  inventory.resolve(); await switching;
});

test("rapid project switches do not wait for or return to an earlier restoration", async () => {
  const c = projectSetup();
  await c.select("one"); await c.switchTo("/b"); await c.select("three");
  const views = deferred();
  c.savedViews.setProject = cwd => cwd === "/a" ? views.promise : Promise.resolve();
  c.gate = {id: "one", ...deferred()};
  const earlier = c.switchTo("/a");
  assert.equal(c.state.selected, "one");
  await c.switchTo("/b");
  assert.equal(c.state.selected, "three");
  views.resolve(); c.gate.reject(Object.assign(new Error("Old session unavailable"), {status: 503}));
  await earlier;
  assert.equal(c.state.projectCwd, "/b"); assert.equal(c.state.selected, "three");
  assert.equal(c.$("welcome").hidden, true);
});

test("restart restores the active conversation and each project's saved tab", async () => {
  const values = new Map(), storage = {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value)};
  const first = projectSetup(storage);
  await first.select("one"); await first.select("two"); await first.switchTo("/b"); await first.select("three");
  const reopened = projectSetup(storage);
  await reopened.restoreProjectTab();
  assert.equal(reopened.state.selected, "two");
  await reopened.switchTo("/b");
  assert.equal(reopened.state.selected, "three");
  reopened.goHome();
  const again = projectSetup(storage); await again.switchTo("/b");
  assert.equal(again.state.selected, null, "Overview remains the selected tab");
});

test("project tabs respect Overview and the replacement selected after closing a tab", async () => {
  const c = projectSetup();
  await c.select("one"); await c.select("two");
  await c.state.threadTabs.close("two");
  await c.switchTo("/b"); await c.switchTo("/a");
  assert.equal(c.state.selected, "one");
  await c.state.threadTabs.close("one");
  await c.switchTo("/b"); await c.switchTo("/a");
  assert.equal(c.state.selected, null); assert.equal(c.$("welcome").hidden, false);
  await c.state.threadTabs.activate("two"); await c.state.threadTabs.activate(null);
  await c.switchTo("/b"); await c.switchTo("/a");
  assert.equal(c.state.selected, null, "choosing Overview is remembered even with open tabs");
});

test("missing tabs, unavailable threads and moved threads fall back to Overview", async () => {
  for (const reason of ["closed-tab", "missing", "moved", "moved-during-refresh"]) {
    const c = projectSetup(); await c.select("one"); await c.switchTo("/b");
    if (reason === "closed-tab") c.state.threadTabs.projects.get("/a").splice(0);
    if (reason === "missing") {
      const api = c.api;
      c.api = async (path, body) => {
        if (path === "threads/one" && !body) throw Object.assign(new Error("Gone"), {status: 404});
        return api(path, body);
      };
    }
    if (reason === "moved") c.state.sessions[0] = {...c.state.sessions[0], project_cwd: "/elsewhere"};
    if (reason === "moved-during-refresh") {
      const detail = clone(c.replies.get("one")); detail.work_thread.project_cwd = "/elsewhere";
      c.replies.set("one", detail);
    }
    await c.switchTo("/a");
    assert.equal(c.state.projectCwd, "/a", reason);
    assert.equal(c.state.selected, null, reason); assert.equal(c.$("welcome").hidden, false, reason);
  }
});

test("temporary restoration failures remain visible and leave a usable Overview", async () => {
  const c = projectSetup(); await c.select("one"); await c.switchTo("/b");
  c.api = async () => { throw Object.assign(new Error("Offline"), {status: 503}); };
  await assert.rejects(c.switchTo("/a"), /Offline/);
  assert.equal(c.state.selected, null); assert.equal(c.$("welcome").hidden, false);
});

test("explicit cross-project navigation bypasses remembered conversations", async () => {
  const c = projectSetup(); await c.select("one"); await c.switchTo("/b"); await c.select("three");
  c.calls.length = 0;
  await c.select("two");
  assert.equal(c.state.projectCwd, "/a"); assert.equal(c.state.selected, "two");
  assert.equal(c.calls.some(call => call.path === "threads/one"), false);
  await c.openProjectDirectory("/b");
  assert.equal(c.state.selected, null, "route callers choose their own destination after changing directory");
});

test("project restoration does not overwrite newer navigation while loading the project", async () => {
  const c = projectSetup(); await c.select("one"); await c.switchTo("/b");
  const gate = deferred(); c.savedViews.setProject = () => gate.promise;
  const switching = c.switchTo("/a");
  await c.select("two"); gate.resolve(); await switching;
  assert.equal(c.state.selected, "two");
});

test("choosing Overview while a project loads cancels its remembered conversation", async () => {
  const values = new Map(), storage = {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value)};
  const c = projectSetup(storage);
  await c.select("one"); await c.switchTo("/b");
  const gate = deferred(); c.savedViews.setProject = () => gate.promise;
  const switching = c.switchTo("/a");
  await c.state.threadTabs.activate(null);
  gate.resolve(); await switching;
  assert.equal(c.state.selected, null, "an explicit Overview click cancels pending restoration");
  assert.equal(c.$("welcome").hidden, false);
  await c.switchTo("/b"); await c.switchTo("/a");
  assert.equal(c.state.selected, null, "returning to the project keeps Overview");
  const reopened = projectSetup(storage); await reopened.restoreProjectTab();
  assert.equal(reopened.state.selected, null, "Overview remains selected after reload");
});

test("new-session drafts and attachments remain separate for each project", async () => {
  const c = projectSetup();
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), c);
  await c.newSession();
  c.$("message").value = "New A";
  c.state.attachments.drafts.set(c.composerDraftId(), ["image A"]);
  c.state.mentions.drafts.set(c.composerDraftId(), ["file A"]);
  await c.switchTo("/b"); await c.newSession();
  c.$("message").value = "New B";
  c.state.attachments.drafts.set(c.composerDraftId(), ["image B"]);
  await c.switchTo("/a");
  assert.equal(c.state.selected, "new-session"); assert.equal(c.state.detail.session.cwd, "/a");
  assert.equal(c.$("message").value, "New A");
  assert.deepEqual(c.state.attachments.drafts.get(c.composerDraftId()), ["image A"]);
  assert.deepEqual(c.state.mentions.drafts.get(c.composerDraftId()), ["file A"]);
  await c.switchTo("/b");
  assert.equal(c.state.selected, "new-session"); assert.equal(c.state.detail.session.cwd, "/b");
  assert.equal(c.$("message").value, "New B");
  assert.deepEqual(c.state.attachments.drafts.get(c.composerDraftId()), ["image B"]);
});

test("project switching restores split selection and its full-conversation return target", async () => {
  const c = projectSetup(); await c.select("one");
  const layout = {selected: "two", returnTarget: {thread: "one", scroll: 123}};
  c.agentCoordPanes = {active: true, layout, leave() { this.active = false; },
    restoreView() { this.active = true; this.layout.returnTarget = {thread: null}; c.state.threadTabs.suspend(); }};
  c.state.threadTabs.suspend();
  await c.switchTo("/b"); assert.equal(c.agentCoordPanes.active, false);
  await c.select("three"); await c.switchTo("/a");
  assert.equal(c.agentCoordPanes.active, true); assert.equal(c.agentCoordPanes.layout, layout);
  assert.equal(layout.selected, "two");
  assert.deepEqual(layout.returnTarget, {thread: "one", scroll: 123});
  assert.equal(c.state.threadTabs.selected, undefined);
});

test("tiled projects restore only after their own saved view is selected", async () => {
  const c = projectSetup(); await c.select("one");
  let view = "/a";
  const gate = deferred(), restored = [];
  c.savedViews.setProject = async cwd => { if (cwd === "/a") await gate.promise; view = cwd; };
  c.agentCoordPanes = {active: true, layout: {}, leave() { this.active = false; },
    restoreView() { restored.push(view); this.active = true; }};
  await c.switchTo("/b");
  const switching = c.switchTo("/a");
  assert.deepEqual(restored, []);
  gate.resolve(); await switching;
  assert.deepEqual(restored, ["/a"]);
});
