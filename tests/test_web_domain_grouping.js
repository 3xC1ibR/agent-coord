"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const threadGrouping = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const threadOrganization = require("../plugins/agent-coord/scripts/agent_coord/web/organization.js");
const {threadViews} = require("../plugins/agent-coord/scripts/agent_coord/web/views.js");

const thread = (id, extra = {}) => ({thread_id: id, title: id, attention: "now", response_state: "working",
  created_at: 1, checkpoint: {phase: "implementation"}, ...extra});
class Element {
  constructor(text = "", className = "") { this.textContent = text; this.className = className; this.dataset = {};
    this.children = []; this.attributes = {}; this.value = ""; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(key, value) { this.attributes[key] = value; }
  get lastElementChild() { return this.children.at(-1); }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
}
function setup(threads, preference = "phase") {
  const controls = {}, values = new Map([["agent-coord-group-by", preference]]);
  const $ = id => controls[id] ||= new Element();
  $("view").value = "active"; $("group-by").value = "phase";
  const c = {state: {sessions: threads, selected: null, pinning: new Set(), updatingThreads: new Set(),
      phaseScroll: new Map(), organization: {projects: []}, overviewMotion: {capture() {}, play() {}}},
    $, node: (tag, text, cls) => new Element(text, cls), groupHeading: label => new Element(label),
    document: {activeElement: null}, conversationSearch: null, threadGrouping, threadOrganization, threadViews,
    threadsInProject: items => items, agentThreadTree: items => ({roots: items}),
    agentThreadCard: (item, tree, compact, urgent) => { const card = new Element(item.title); card.urgent = urgent; return card; },
    renderClosedToggle() {}, renderStatus() {}, savedViews: {activeId: "all", render() {}, persist() {}},
    localStorage: {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value)},
    filtersChanged() { c.renderList(); }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function renderList()"), source.indexOf("function agentThreadTree(")), c);
  vm.runInContext(source.slice(source.indexOf('try { const saved = localStorage.getItem("agent-coord-group-by")'),
    source.indexOf('$("search").oninput = filtersChanged;')), c);
  return {c, $, values};
}
const descendants = root => [root, ...root.children.flatMap(descendants)];

test("overview button toggles domain grouping, persists it, and reflects selected state", () => {
  const {$, c, values} = setup([thread("web", {domain: "web"}), thread("api", {domain: "backend"})]);
  $("group-by-domain").onclick();
  assert.equal($("group-by").value, "domain");
  assert.equal($("group-by-domain").attributes["aria-pressed"], "true");
  assert.equal(values.get("agent-coord-group-by"), "domain");
  assert.deepEqual(descendants($("sessions")).filter(el => el.dataset.group).map(el => el.attributes["aria-label"]), ["backend", "web"]);
  $("group-by-domain").onclick();
  assert.equal($("group-by").value, "phase");
  assert.equal($("group-by-domain").attributes["aria-pressed"], "false");
  assert.equal(values.get("agent-coord-group-by"), "phase");
});

test("domain grouping sorts sidebar sections by recency and retains unassigned work without Cards board", () => {
  const input = [thread("ordinary", {domain: "web", updated_at: 200}), thread("urgent", {domain: "web", response_state: "input", updated_at: 100}),
    thread("pin", {domain: "backend", pinned: true, updated_at: 300}), thread("fallback", {scope_cluster: "backend", updated_at: 50}), thread("unassigned")];
  const before = JSON.stringify(input), {$, c} = setup(input, "domain");
  c.renderList();
  assert.equal($("overview").children.length, 0, "Cards overview board is retired");
  const groups = descendants($("sessions")).filter(el => el.dataset.group);
  assert.deepEqual(groups.map(el => el.attributes["aria-label"]), ["backend", "web", "No domain"]);
  assert.deepEqual(groups[0].children.slice(1).map(el => el.textContent), ["pin", "fallback"]);
  assert.deepEqual(groups[1].children.slice(1).map(el => el.textContent), ["ordinary", "urgent"]);
  assert.equal(groups.find(el => el.dataset.group === "domain-missing").attributes["aria-label"], "No domain");
  assert.equal(JSON.stringify(input), before);
});

test("domain mode groups Later and Closed in sidebar navigation and survives preference restoration", () => {
  for (const [view, placement] of [["later", "later"], ["archived", "archived"]]) {
    const input = [thread("web", {domain: "web", attention: placement}), thread("docs", {domain: "docs", attention: placement})];
    const {$, c} = setup(input, "domain");
    $("view").value = view; c.renderList();
    assert.equal($("group-by").value, "domain");
    assert.deepEqual(descendants($("sessions")).filter(el => el.dataset.group).map(el => el.attributes["aria-label"]), ["docs", "web"]);
  }
});
