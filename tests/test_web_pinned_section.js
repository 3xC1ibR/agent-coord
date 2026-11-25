"use strict";
const {test} = require("node:test"), assert = require("node:assert/strict");
const WorkingSet = require("../plugins/agent-coord/scripts/agent_coord/web/working-set.js");
function thread(id, extra = {}) { return {thread_id: id, title: id, attention: "now", work_phase: "implementation", response_state: "working", ...extra}; }
function overview() {
  const vm = require("node:vm"), fs = require("node:fs");
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const controls = {};
  function element(tag, text, className = "") {
    return {tag, textContent: text, className, children: [], dataset: {}, value: "", hidden: false, scrollLeft: 0,
      get isConnected() { return this.root || Boolean(this.parent?.isConnected); },
      get clientWidth() { return this.isConnected && !controls.welcome.hidden ? 346 : 0; },
      classList: {toggle() {}},
      setAttribute() {},
      append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } },
      replaceChildren(...items) { for (const item of this.children) item.parent = null; this.children = []; this.append(...items); },
    };
  }
  const c = {conversationSearch: null, node: element, $: id => controls[id] ||= Object.assign(element("div"), {root: true}),
    document: {activeElement: null}, savedViews: {activeId: "all", render() {}},
    state: {sessions: [], selected: null, pinning: new Set(), updatingThreads: new Set(),
      phaseScroll: new Map(), organization: {projects: []}, listSignature: ""},
    threadGrouping: require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js"),
    threadOrganization: require("../plugins/agent-coord/scripts/agent_coord/web/organization.js"),
    threadViews: require("../plugins/agent-coord/scripts/agent_coord/web/views.js").threadViews,
    WorkingSet,
    groupHeading: label => element("h2", label),
    threadCard: thread => Object.assign(element("button"), {dataset: {thread: thread.thread_id}}),
    renderStatus() {}, renderClosedToggle() {},
  };
  c.$("welcome"); c.$("view").value = "active"; c.$("group-by").value = "phase";
  c.threadsInProject = threads => threads;
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function renderList("), source.indexOf("function conversationEntry(")), c);
  c.renderList();
  return c;
}

const sidebarAttentionIds = c => c.$("sessions").children
  .find(el => el.className === "thread-group priority")?.children
  .map(el => el.dataset.thread).filter(Boolean) || [];

test("pinned replies appear once in List Pinned while attention badges and sidebar navigation remain available", () => {
  const c = overview();
  c.state.sessions = [thread("pin", {pinned: true}),
    thread("pinned-reply", {pinned: true, response_state: "reply", unread_result: true, needs_attention: true, attention_since: 100}),
    thread("reply", {response_state: "reply", unread_result: true, needs_attention: true, attention_since: 90})];
  for (const view of ["active", "attention"]) {
    c.$("view").value = view;
    c.renderList();
    assert.equal(c.$("overview").children.length, 0, "Cards overview board is retired");
    const list = WorkingSet.listSections(c.state.sessions, {now: 120, workspace: "/work"});
    assert.deepEqual(list.sections.pinned.map(t => t.thread_id), ["pinned-reply", "pin"]);
    assert.deepEqual(list.sections.attention.map(t => t.thread_id), ["reply"]);
    assert.deepEqual(sidebarAttentionIds(c), ["pinned-reply", "reply"]);
    assert.match(c.$("results-count").textContent, view === "active" ? /^3 threads/ : /^2 threads/);
    assert.equal(c.threadViews.badges(c.state.sessions, {}).attention, 2);
    assert.ok(!c.$("sessions").children.some(el => el.className.includes("pinned")));
  }
  c.state.sessions.forEach(item => { item.pinned = false; });
  c.renderList();
  const unpinnedList = WorkingSet.listSections(c.state.sessions, {now: 120, workspace: "/work"});
  assert.deepEqual(unpinnedList.sections.pinned, []);
  assert.deepEqual(unpinnedList.sections.attention.map(t => t.thread_id), ["pinned-reply", "reply"]);
});

test("List Attention hides pinned items into Pinned without losing sidebar attention or badges", () => {
  for (const response_state of ["input", "action", "failed", "reply"]) {
    const c = overview();
    c.state.sessions = [thread("pinned", {pinned: true, response_state, unread_result: true, needs_attention: true, attention_since: 100})];
    c.renderList();
    const list = WorkingSet.listSections(c.state.sessions, {now: 120, workspace: "/work"});
    assert.deepEqual(list.sections.pinned.map(t => t.thread_id), ["pinned"]);
    assert.deepEqual(list.sections.attention, []);
    assert.deepEqual(sidebarAttentionIds(c), ["pinned"]);
    assert.equal(c.threadViews.badges(c.state.sessions, {}).attention, 1);
    assert.equal(c.state.sessions[0].needs_attention, true);
  }
});

test("pinning moves threads to List Pinned, preserves sidebar navigation, and unpinning restores them", () => {
  for (const phase of ["new", "investigation", "planning", "orchestrating", "implementation", "validation", "deployment", "finished"]) {
    const c = overview();
    c.state.sessions = [thread("pin", {pinned: true, work_phase: phase, checkpoint: {id: 1}}),
      thread("other", {work_phase: phase, checkpoint: {id: 2}})];
    const sidebarCards = () => c.$("sessions").children.flatMap(el => el.children)
      .map(el => el.dataset.thread).filter(Boolean);
    c.renderList();
    const pinnedList = WorkingSet.listSections(c.state.sessions, {now: 120, workspace: "/work"});
    assert.deepEqual(pinnedList.sections.pinned.map(t => t.thread_id), ["pin"]);
    assert.ok(sidebarCards().includes("pin"));
    assert.match(c.$("results-count").textContent, /^2 threads/);
    c.state.sessions[0].pinned = false;
    c.renderList();
    const restoredList = WorkingSet.listSections(c.state.sessions, {now: 120, workspace: "/work"});
    assert.deepEqual(restoredList.sections.pinned, []);
    assert.deepEqual(sidebarCards().sort(), ["other", "pin"]);
  }
});

test("Pinned obeys search and placement filters; snoozed pins stay in Later", () => {
  const c = overview();
  c.state.sessions = [thread("keep", {pinned: true}), thread("sleep", {pinned: true, attention: "later", snoozed: true})];
  c.renderList();
  const list = WorkingSet.listSections(c.state.sessions, {now: 120, workspace: "/work"});
  assert.deepEqual(list.sections.pinned.map(t => t.thread_id), ["keep"]);
  c.$("search").value = "missing"; c.renderList();
  assert.match(c.$("results-count").textContent, /^0 threads/);
  c.$("search").value = ""; c.$("view").value = "later"; c.renderList();
  assert.equal(c.$("sessions").children[0].dataset.group, "later");
  assert.match(c.$("results-count").textContent, /^1 thread/);
});

test("sidebar attention order stays stakes-first with pins breaking ties within the same severity rank", () => {
  const c = overview();
  c.state.sessions = [
    thread("done-newest", {response_state: "update", attention_reason: "done", needs_attention: true, attention_since: 500}),
    thread("reply-unpinned", {response_state: "reply", attention_reason: "reply", needs_attention: true, attention_since: 400}),
    thread("reply-pinned", {pinned: true, response_state: "reply", attention_reason: "reply", needs_attention: true, attention_since: 100}),
    thread("review-item", {response_state: "action", attention_reason: "review", needs_attention: true, attention_since: 200}),
    thread("blocked-oldest", {response_state: "failed", attention_reason: "blocked", needs_attention: true, attention_since: 50}),
  ];
  c.renderList();
  assert.deepEqual(sidebarAttentionIds(c), [
    "blocked-oldest", "review-item", "reply-pinned", "reply-unpinned", "done-newest",
  ]);
});
