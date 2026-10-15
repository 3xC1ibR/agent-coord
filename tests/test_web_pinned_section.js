"use strict";
const {test} = require("node:test"), assert = require("node:assert/strict");
function thread(id, extra = {}) { return {thread_id: id, title: id, attention: "now", work_phase: "implementation", response_state: "working", ...extra}; }
function overview() {
  const vm = require("node:vm"), fs = require("node:fs");
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const controls = {};
  function element(tag, text, className = "") {
    return {tag, textContent: text, className, children: [], dataset: {}, value: "", hidden: false, scrollLeft: 0,
      get isConnected() { return this.root || Boolean(this.parent?.isConnected); },
      get clientWidth() { return this.isConnected && !controls.welcome.hidden ? 346 : 0; },
      setAttribute() {},
      append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } },
      replaceChildren(...items) { for (const item of this.children) item.parent = null; this.children = []; this.append(...items); },
    };
  }
  const c = {conversationSearch: null, node: element, $: id => controls[id] ||= Object.assign(element("div"), {root: true}),
    document: {activeElement: null}, savedViews: {activeId: "all", render() {}},
    state: {sessions: [], selected: null, pinning: new Set(), updatingThreads: new Set(),
      phaseScroll: new Map(), organization: {projects: []}, listSignature: "",
      overviewMotion: {capture() {}, play() {}}},
    threadGrouping: require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js"),
    threadOrganization: require("../plugins/agent-coord/scripts/agent_coord/web/organization.js"),
    threadViews: require("../plugins/agent-coord/scripts/agent_coord/web/views.js").threadViews,
    groupHeading: label => element("h2", label),
    threadCard: thread => Object.assign(element("button"), {dataset: {thread: thread.thread_id}}),
    renderStatus() {}, renderClosedToggle() {},
  };
  c.$("welcome"); c.$("view").value = "active"; c.$("group-by").value = "phase";
  c.threadsInProject = threads => threads; // This fixture supplies one project’s inventory.
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function renderList("), source.indexOf("async function select(")), c);
  c.board = () => c.$("overview").children.find(el => el.className === "phase-board");
  c.renderList();
  return c;
}

const cardIds = section => section.children.find(el => el.className === "thread-cards")
  .children.map(card => card.dataset.cardThread);
const attentionCards = c => {
  const panel = c.$("overview").children.find(el => el.className === "attention-panel");
  return panel ? cardIds(panel.children[0]) : [];
};
const sidebarAttentionIds = c => c.$("sessions").children
  .find(el => el.className === "thread-group priority").children
  .map(el => el.dataset.thread).filter(Boolean);

test("pinned replies appear once while attention badges and sidebar navigation remain available", () => {
  const c = overview();
  c.state.sessions = [thread("pin", {pinned: true}),
    thread("pinned-reply", {pinned: true, response_state: "reply", needs_attention: true}),
    thread("reply", {response_state: "reply", needs_attention: true})];
  for (const view of ["active", "attention"]) {
    c.$("view").value = view;
    c.renderList();
    const sections = c.$("overview").children;
    assert.equal(sections[0].dataset.group, "pinned");
    assert.equal(sections[1].className, "attention-panel");
    assert.deepEqual(cardIds(sections[0]), view === "active" ? ["pin", "pinned-reply"] : ["pinned-reply"]);
    assert.deepEqual(attentionCards(c), ["reply"]);
    assert.deepEqual(sidebarAttentionIds(c), ["pinned-reply", "reply"]);
    assert.match(c.$("results-count").textContent, view === "active" ? /^3 threads/ : /^2 threads/);
    assert.equal(c.threadViews.badges(c.state.sessions, {}).attention, 2);
    assert.ok(!c.$("sessions").children.some(el => el.className.includes("pinned")));
  }
  c.state.sessions.forEach(item => { item.pinned = false; });
  c.renderList();
  assert.equal(c.$("overview").children[0].className, "attention-panel");
  assert.deepEqual(attentionCards(c), ["pinned-reply", "reply"]);
});

test("Attention hides when all its cards are pinned without losing required actions", () => {
  for (const response_state of ["input", "action", "failed", "reply"]) {
    const c = overview();
    c.state.sessions = [thread("pinned", {pinned: true, response_state, needs_attention: true})];
    c.renderList();
    assert.deepEqual(cardIds(c.$("overview").children[0]), ["pinned"]);
    assert.ok(!c.$("overview").children.some(el => el.className === "attention-panel"));
    assert.ok(c.board());
    assert.deepEqual(sidebarAttentionIds(c), ["pinned"]);
    assert.equal(c.threadViews.badges(c.state.sessions, {}).attention, 1);
    assert.equal(c.state.sessions[0].needs_attention, true);
  }
});

test("pinning removes stage and organization cards, preserves navigation, and unpinning restores them", () => {
  for (const groupBy of ["phase", "project", "repository"]) {
    for (const phase of ["new", "investigation", "planning", "orchestrating", "implementation", "validation", "deployment", "finished"]) {
      const c = overview();
      c.$("group-by").value = groupBy;
      c.state.sessions = [thread("pin", {pinned: true, work_phase: phase}), thread("other", {work_phase: phase})];
      const boardCards = () => c.$("overview").children.find(el => el.className.endsWith("-board"))
        .children.flatMap(cardIds).filter(Boolean);
      const sidebarCards = () => c.$("sessions").children.flatMap(el => el.children)
        .map(el => el.dataset.thread).filter(Boolean);
      c.renderList();
      assert.deepEqual(cardIds(c.$("overview").children[0]), ["pin"]);
      assert.deepEqual(boardCards(), ["other"]);
      assert.ok(sidebarCards().includes("pin"));
      assert.match(c.$("results-count").textContent, /^2 threads/);
      assert.equal(c.state.sessions[0].work_phase, phase);
      c.state.sessions[0].pinned = false;
      c.renderList();
      assert.deepEqual(boardCards().sort(), ["other", "pin"]);
    }
  }
});

test("a pinned Later thread stays in Attention when it is absent from Pinned", () => {
  const c = overview();
  c.state.sessions = [thread("now", {pinned: true, response_state: "reply", needs_attention: true}),
    thread("later", {pinned: true, attention: "later", response_state: "reply", needs_attention: true})];
  c.renderList();
  assert.deepEqual(cardIds(c.$("overview").children[0]), ["now"]);
  assert.deepEqual(attentionCards(c), ["later"]);
});
test("Pinned obeys search and placement filters; snoozed pins stay in Later", () => {
 const c=overview(); c.state.sessions=[thread("keep",{pinned:true}),thread("sleep",{pinned:true,attention:"later",snoozed:true})]; c.renderList();
 assert.equal(c.$("overview").children[0].children.find(e=>e.className==="thread-cards").children.length,1);
 c.$("search").value="missing"; c.renderList(); assert.ok(!c.$("overview").children.some(e=>e.dataset.group==="pinned"));
 c.$("search").value=""; c.$("view").value="later"; c.renderList();
 assert.equal(c.$("overview").children[0].dataset.group,"later");
 assert.match(c.$("results-count").textContent, /^1 thread/);
});
