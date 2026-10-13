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
  assert.deepEqual(descendants($("overview")).filter(el => el.dataset.group).map(el => el.attributes["aria-label"]), ["backend", "web"]);
  $("group-by-domain").onclick();
  assert.equal($("group-by").value, "phase");
  assert.equal($("group-by-domain").attributes["aria-pressed"], "false");
  assert.equal(values.get("agent-coord-group-by"), "phase");
});

test("domain grouping keeps pins visible once, leads with urgent cards, and retains unassigned work", () => {
  const input = [thread("ordinary", {domain: "web"}), thread("urgent", {domain: "web", response_state: "input"}),
    thread("pin", {domain: "backend", pinned: true}), thread("fallback", {scope_cluster: "backend"}), thread("unassigned")];
  const before = JSON.stringify(input), {$, c} = setup(input, "domain");
  c.renderList();
  const cards = descendants($("overview")).filter(el => el.dataset.cardThread);
  assert.deepEqual(cards.map(el => el.dataset.cardThread), ["pin", "fallback", "urgent", "ordinary", "unassigned"]);
  assert.equal(cards.find(el => el.dataset.cardThread === "urgent").urgent, true);
  assert.equal(cards.filter(el => el.dataset.cardThread === "pin").length, 1);
  assert.equal(descendants($("overview")).find(el => el.dataset.group === "domain-missing").attributes["aria-label"], "No domain");
  assert.equal(JSON.stringify(input), before);
});

test("domain mode groups Later and Closed and survives preference restoration", () => {
  for (const [view, placement] of [["later", "later"], ["archived", "archived"]]) {
    const input = [thread("web", {domain: "web", attention: placement}), thread("docs", {domain: "docs", attention: placement})];
    const {$, c} = setup(input, "domain");
    $("view").value = view; c.renderList();
    assert.equal($("group-by").value, "domain");
    assert.deepEqual(descendants($("overview")).filter(el => el.dataset.group).map(el => el.attributes["aria-label"]), ["docs", "web"]);
  }
});
