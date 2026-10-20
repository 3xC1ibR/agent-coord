"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const Tree = require("../plugins/agent-coord/scripts/agent_coord/web/agent-tree.js");
const thread = (id, parent, extra = {}) => ({thread_id: id, title: id, browser_session: true, created_at: 100,
  attention: "now", status: "idle", response_state: "available", work_phase: "planning",
  ...(parent ? {creation_mode: "orchestration", created_by_thread_id: parent} : {}), ...extra});

test("the tree spans state, placement and group boundaries without inventing dispatch or fork edges", () => {
  const records = [thread("root"), thread("child", "root", {attention: "later", project_id: "different"}),
    thread("closed", "child", {attention: "archived"}), thread("dispatch", "root", {creation_mode: null}),
    thread("fork", null, {forked_from: {thread_id: "root"}}), thread("unrelated", null, {project_id: "different"})];
  const tree = Tree.graph(records);
  assert.deepEqual(tree.family("root"), ["root", "child", "closed"]);
  assert.equal(tree.root("closed"), "root");
  assert.equal(tree.root("dispatch"), "dispatch");
  assert.equal(tree.root("fork"), "fork");
});
test("missing parents, self links, and historical cycles cannot trap tree navigation", () => {
  const tree = Tree.graph([thread("a", "b"), thread("b", "a"), thread("c", "b"),
    thread("missing", "unavailable"), thread("self", "self")]);
  assert.deepEqual(tree.family(tree.root("c")).sort(), ["a", "b", "c"]);
  assert.equal(tree.root("missing"), "missing");
  assert.equal(tree.root("self"), "self");
});
test("full cards have non-overlapping positions, first-level columns and connected deeper descendants", () => {
  const records = [thread("root"), thread("research", "root"), thread("writer", "root"), thread("coder", "root"),
    thread("test1", "coder"), thread("test2", "coder"), thread("nested", "test1")];
  const tree = Tree.graph(records), layout = Tree.layout(tree, "root");
  assert.equal(layout.nodes.length, records.length); assert.equal(layout.edges.length, records.length - 1);
  for (const a of layout.nodes) for (const b of layout.nodes) if (a !== b) {
    assert.ok(a.x + Tree.CARD_W <= b.x || b.x + Tree.CARD_W <= a.x || a.y + Tree.CARD_H <= b.y || b.y + Tree.CARD_H <= a.y,
      a.id + " overlaps " + b.id);
  }
  for (const edge of layout.edges) assert.ok(!/NaN|undefined/.test(edge.path));
  const after = Tree.layout(Tree.graph(records.map(t => ({...t, response_state: "working"}))), "root");
  assert.deepEqual(after, layout, "status changes preserve geometry");
  const collapsed = Tree.layout(tree, "root", new Set(["coder"]));
  assert.deepEqual(collapsed.nodes.map(n => n.id).sort(), ["coder", "research", "root", "writer"]);
});
test("current running state supersedes a historical failure; stale asks do not require intervention", () => {
  assert.equal(Tree.status(thread("running", null, {response_state: "working", turn_completion: {status: "failed"}})).key, "running");
  assert.equal(Tree.status(thread("ask", null, {checkpoint: {next_actor: "user", next_action: "Approve"}})).key, "waiting");
  assert.equal(Tree.status(thread("stale", null, {checkpoint_stale: true, checkpoint: {next_actor: "user", next_action: "Approve"}})).key, "idle");
  assert.equal(Tree.duration(thread("idle")), "");
});

function setup() {
  const doc = {activeElement: null};
  class Element {
    constructor(tag) { Object.assign(this, {tagName: tag, children: [], dataset: {}, attrs: {}, events: {}, style: {}, className: "",
      clientWidth: 900, clientHeight: 650}); }
    get classList() { const read = () => new Set(this.className.split(/\s+/).filter(Boolean)); return {
      contains: n => read().has(n),
      add: n => { const s = read(); s.add(n); this.className = [...s].join(" "); },
      remove: n => { const s = read(); s.delete(n); this.className = [...s].join(" "); },
      toggle: (n, value) => { const s = read(); if (value ?? !s.has(n)) s.add(n); else s.delete(n); this.className = [...s].join(" "); }}; }
    append(...nodes) { for (const n of nodes) { n.remove?.(); this.children.push(n); n.parent = this; } }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(n => n !== this); this.parent = null; }
    replaceChildren(...nodes) { for (const n of this.children) n.parent = null; this.children = []; this.append(...nodes); }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    removeAttribute(k) { delete this.attrs[k]; }
    matches(s) {
      if (s.includes(",")) return s.split(",").some(x => this.matches(x.trim()));
      if (s.startsWith(".")) return this.classList.contains(s.slice(1));
      if (s.startsWith("[role=")) return this.attrs.role === "treeitem";
      return this.tagName === s;
    }
    closest(s) { return this.matches(s) ? this : this.parent?.closest(s); }
    querySelector(s) { for (const n of this.children) { if (n.matches(s)) return n; const found = n.querySelector(s); if (found) return found; } return null; }
    contains(n) { return this === n || this.children.some(c => c.contains(n)); }
    addEventListener(k, f) { this.events[k] = f; }
    focus() { doc.activeElement = this; }
  }
  doc.createElement = tag => new Element(tag); doc.createElementNS = (_, tag) => new Element(tag);
  const selected = [], panel = new Tree.Panel({document: doc, window: {}, host: doc.createElement("div"), onSelect: id => selected.push(id)});
  return {panel, selected, doc};
}
test("selection updates reuse card elements and camera; folding and keyboard navigation preserve the full root", () => {
  const {panel, selected, doc} = setup();
  const records = [thread("root"), thread("child", "root"), thread("grandchild", "child")];
  panel.update(records, "root", "root");
  const child = panel.cardEls.get("child");
  panel.zoom(1.25); const camera = {...panel.view};
  child.onclick({target: child}); assert.deepEqual(selected, ["child"]);
  panel.update(records, "root", "child");
  assert.equal(panel.root, "root"); assert.equal(panel.cardEls.get("child"), child);
  assert.equal(child.attrs["aria-selected"], "true"); assert.equal(panel.view.x, camera.x);
  panel.fold("child"); assert.equal(panel.cardEls.has("grandchild"), false);
  panel.update(records, "root", "child"); assert.equal(panel.cardEls.has("grandchild"), false);
  let prevented = false;
  panel.keydown({key: "ArrowRight", target: child, preventDefault() { prevented = true; }});
  assert.equal(prevented, true); assert.equal(panel.cardEls.has("grandchild"), true);
  panel.keydown({key: "ArrowRight", target: child, preventDefault() {}});
  assert.equal(doc.activeElement.dataset.agentId, "grandchild");
  panel.keydown({key: "Enter", target: doc.activeElement, preventDefault() {}});
  assert.equal(selected.at(-1), "grandchild");
});
test("attention control reveals a folded failed descendant before selecting it", () => {
  const {panel, selected} = setup();
  const records = [thread("root"), thread("child", "root"), thread("failed", "child", {response_state: "failed"})];
  panel.update(records, "root", "root"); panel.fold("child"); panel.findAttention();
  assert.equal(panel.cardEls.has("failed"), true); assert.equal(selected.at(-1), "failed");
  assert.equal(panel.root, "root");
});
