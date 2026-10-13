"use strict";
const {test} = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const grouping = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const walk = node => [node, ...node.children.flatMap(walk)];
const withClass = (root, name) => walk(root).filter(n => n.className.split(" ").includes(name));
const thread = (id, creator = null, extra = {}) => ({thread_id: id, title: id, attention: "now", status: "idle",
  work_phase: "investigation", response_state: "available", created_at: 1, links: [],
  creation_mode: creator ? "orchestration" : null,
  created_by_thread_id: creator, created_by: creator && {thread_id: creator, title: creator, available: true}, ...extra});
function harness(threads) {
  const controls = {}, selected = [];
  function element(tag, text = null, className = "") {
    return {tag, textContent: text, className, children: [], dataset: {}, attrs: {}, value: "", hidden: false,
      get isConnected() { return this.root || Boolean(this.parent?.isConnected); },
      get lastChild() { return this.children.at(-1); },
      setAttribute(key, value) { this.attrs[key] = value; },
      append(...nodes) { for (const n of nodes) { n.parent = this; this.children.push(n); } },
      replaceChildren(...nodes) { this.children.forEach(n => n.parent = null); this.children = []; this.append(...nodes); },
      insertBefore(n, before) { n.parent = this; this.children.splice(this.children.indexOf(before), 0, n); },
    };
  }
  const c = {node: element, $: id => controls[id] ||= Object.assign(element("div"), {root: true}),
    document: {activeElement: null, createElementNS: (_, tag) => element(tag), createTextNode: text => element("text", text)},
    state: {sessions: threads, selected: null, closing: new Set(), pinning: new Set(), updatingThreads: new Set(),
      phaseScroll: new Map(), organization: {projects: []}, listSignature: "", overviewMotion: {capture() {}, play() {}}},
    savedViews: {activeId: "all", render() {}}, conversationSearch: null,
    threadGrouping: grouping, threadOrganization: require("../plugins/agent-coord/scripts/agent_coord/web/organization.js"),
    threadViews: require("../plugins/agent-coord/scripts/agent_coord/web/views.js").threadViews,
    groupHeading: label => element("h2", label), renderStatus() {}, renderClosedToggle() {},
    associationLabel: () => "", relativeTime: () => "", snoozeLabel: () => "", action: fn => fn(), select: id => selected.push(id)};
  c.threadsInProject = threads => threads; // This fixture supplies one project’s inventory.
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function threadCard("), source.indexOf("function groupHeading(")), c);
  vm.runInContext(source.slice(source.indexOf("function renderList("), source.indexOf("function conversationEntry(")), c);
  c.$("view").value = "active"; c.$("group-by").value = "phase";
  c.renderList();
  c.cards = () => walk(c.$("overview")).filter(n => n.dataset.cardThread).map(n => n.dataset.cardThread);
  c.rows = () => withClass(c.$("overview"), "agent-row");
  c.selected = selected;
  return c;
}

test("only explicit orchestration creations nest across phases without changing counts or records", () => {
  const records = [thread("parent", null, {work_phase: "orchestrating"}), thread("dispatch", "parent", {creation_mode: null}),
    thread("orchestrated", "parent", {work_phase: "deployment", status: "running"}), thread("user")];
  const before = JSON.stringify(records), c = harness(records);
  assert.deepEqual(c.cards(), ["dispatch", "user", "parent"]);
  assert.deepEqual(c.rows().map(n => n.dataset.thread), ["orchestrated"]);
  assert.equal(c.$("thread-count").textContent, "4");
  assert.equal(JSON.stringify(records), before);
  assert.equal(withClass(c.rows()[0], "agent-row-status")[0].textContent, "Unknown provider · Deploying");
  assert.equal(withClass(c.rows()[0], "sr-only")[0].textContent, "Working");
  assert.ok(c.rows()[0].className.split(" ").includes("working"));
  c.rows()[0].onclick(); assert.deepEqual(c.selected, ["orchestrated"]);
  assert.equal(withClass(c.$("sessions"), "agent-row").length, 1);
  assert.equal(withClass(c.$("overview"), "agent-origin")[0].textContent, "Created by parent");
});

test("card menu exposes fork and group actions for the card rather than the selected conversation", async () => {
  const card = thread("card", null, {client: "codex", can_fork: true}), c = harness([card]);
  c.state.selected = "another";
  let items, forked, organized;
  c.state.threadActions = {showMenu: (_trigger, _title, choices) => { items = choices; }};
  c.forkThread = async (_onSuccess, id) => { forked = id; };
  c.organizeThread = async target => { organized = target; };
  const menu = () => {
    const element = c.threadCard(card);
    withClass(element, "card-more")[0].onclick({stopPropagation() {}});
    return items;
  };
  menu();
  assert.equal(Boolean(items.find(item => item.label === "Fork").disabled), false);
  await items.find(item => item.label === "Fork").run();
  await items.find(item => item.label === "Add to group").run();
  assert.equal(forked, "card");
  assert.equal(organized, card);
  assert.equal(c.state.selected, "another");
  assert.deepEqual(c.selected, []);
  card.project_id = "existing";
  assert.ok(menu().some(item => item.label === "Change group"));
  for (const client of ["codex", "claude"]) {
    card.client = client; card.can_fork = false;
    assert.equal(menu().find(item => item.label === "Fork").disabled, true);
  }
  card.can_fork = true; c.state.forking = true;
  assert.equal(menu().find(item => item.label === "Fork").disabled, true);
  const existingMenu = withClass(c.threadCard(card), "card-more")[0];
  c.state.forking = false;
  existingMenu.onclick({stopPropagation() {}});
  assert.equal(items.find(item => item.label === "Fork").disabled, false);
  card.client = "kimi";
  assert.equal(menu().some(item => item.label === "Fork"), false);
});

test("untagged and unknown creation modes stay separate regardless of reply or parent phase", () => {
  for (const creation_mode of [undefined, null, "dispatch", "unknown"]) {
    const c = harness([thread("parent", null, {work_phase: "orchestrating"}),
      thread("child", "parent", {creation_mode, reply_required: true, response_state: "reply"})]);
    assert.deepEqual(c.cards(), ["child", "parent"]);
    assert.equal(c.rows().length, 0);
  }
});

test("a standalone dispatched agent can create its own grouped orchestration", () => {
  const c = harness([thread("parent"), thread("dispatched", "parent", {creation_mode: null}), thread("nested", "dispatched")]);
  assert.deepEqual(c.cards(), ["dispatched", "parent"]);
  assert.deepEqual(c.rows().map(n => n.dataset.thread), ["nested"]);
});

test("collapse persists with count-free disclosure and styled unread child rows", () => {
  const c = harness([thread("parent"), thread("child", "parent", {status: "running", unread_result: true})]);
  let details = withClass(c.$("overview"), "agent-children")[0];
  assert.equal(details.open, true);
  assert.equal(withClass(details, "agent-counts").length, 0);
  assert.equal(withClass(details, "agent-children-toggle")[0].attrs["aria-label"], "Agents created by parent · 1 agent · 1 working · 1 unread");
  assert.match(c.rows()[0].className, /response-unread/);
  assert.ok(walk(details).every(n => !/\d+ agents?|unread|read response/i.test(n.textContent || "")));
  details.open = false; details.ontoggle();
  c.state.sessions[1].status = "idle"; c.renderList();
  details = withClass(c.$("overview"), "agent-children")[0];
  assert.equal(details.open, false);
  assert.equal(withClass(details, "agent-counts").length, 0);
  c.state.sessions[1].unread_result = false; c.renderList();
  assert.doesNotMatch(c.rows()[0].className, /response-unread/);
});

test("required input stays in Attention while routine agent replies remain nested", () => {
  const c = harness([thread("parent"), thread("answer", "parent", {response_state: "reply", unread_result: true}),
    thread("approval", "parent", {response_state: "input", attention_reason: "blocked"})]);
  assert.deepEqual(c.cards(), ["approval", "parent"]);
  assert.deepEqual(c.rows().map(n => n.dataset.thread), ["answer", "approval"]);
  const origin = withClass(c.$("overview"), "agent-origin")[0];
  assert.equal(origin.textContent, "Created by parent"); origin.onclick(); assert.deepEqual(c.selected, ["parent"]);
  assert.ok(origin.parent.className.split(" ").includes("thread-card"), "creator belongs inside its overview card");
  assert.ok(walk(c.$("overview")).filter(n => n.tag === "button").every(n => !walk(n).slice(1).some(x => x.tag === "button")), "card controls must not nest buttons");
});

test("missing, closed, differently placed and filtered creators never hide a child", () => {
  for (const parent of [null, thread("parent", null, {attention: "archived"}), thread("parent", null, {attention: "later"})]) {
    const c = harness([...(parent ? [parent] : []), thread("child", "parent")]);
    assert.deepEqual(c.cards(), ["child"]);
  }
  const c = harness([thread("parent", null, {work_phase: "planning", project_id: "a"}), thread("child", "parent", {project_id: "b"})]);
  for (const [control, value] of [["search", "child"], ["phase-filter", "investigation"], ["project", "b"]]) {
    c.$(control).value = value; c.renderList(); assert.deepEqual(c.cards(), ["child"]);
    c.$(control).value = "";
  }
});

test("nested spawns are navigable, malformed cycles stay visible, and unavailable origins cannot open", () => {
  const c = harness([thread("parent"), thread("child", "parent"), thread("grandchild", "child")]);
  assert.deepEqual(c.cards(), ["parent"]);
  assert.deepEqual(c.rows().map(n => n.dataset.thread), ["child", "grandchild"]);
  for (const items of [[thread("a", "b"), thread("b", "a")], [thread("a", "a")]]) {
    assert.equal(harness(items).cards().length, items.length);
  }
  const outside = harness([thread("child", "parent", {created_by: {title: "Outside workspace", available: false}})]);
  assert.equal(withClass(outside.$("overview"), "agent-origin")[0].disabled, true);
});

test("pinned agents remain directly accessible and Later and Closed support grouping", () => {
  const c = harness([thread("parent"), thread("child", "parent", {pinned: true})]);
  assert.deepEqual(c.cards(), ["child", "parent"]);
  for (const [placement, view] of [["later", "later"], ["archived", "archived"]]) {
    c.state.sessions.forEach(t => t.attention = placement); c.$("view").value = view; c.renderList();
    assert.deepEqual(c.cards(), ["parent"]); assert.equal(c.rows().length, 1);
    // Disclosure is a sibling of the card, never inside a button.
    assert.ok(walk(c.$("overview")).filter(n => n.tag === "button").every(n => !walk(n).slice(1).some(x => x.tag === "button" || x.tag === "details")));
  }
});
