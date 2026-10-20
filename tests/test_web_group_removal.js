"use strict";
const {test} = require("node:test"), assert = require("node:assert/strict");
const {SavedViews} = require("../plugins/agent-coord/scripts/agent_coord/web/views.js");

function setup(groups, preferences = {values: new Map(), getItem(key) { return this.values.get(key); },
  setItem(key, value) { this.values.set(key, value); }}) {
  const controls = new Map(), calls = [], errors = [];
  const doc = {activeElement: null, listeners: {}, body: {classList: {contains: () => false}},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    querySelector() { return [...controls.values()].find(el => el.open && el.id.endsWith("dialog")) || null; }};
  class Element {
    constructor(id = "") { this.id = id; this.value = ""; this.children = []; this.dataset = {}; this.attributes = {};
      this.scrollTop = 0; this.hidden = false; this.style = {}; this.classList = {contains: () => false}; }
    get options() { return this.children; }
    append(child) { this.children.push(child); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(key, value) { this.attributes[key] = value; }
    contains(el) { return el === this || this.children.some(child => child.contains(el)); }
    querySelector() { return this.children.find(el => el.attributes["aria-selected"] === "true"); }
    focus() { doc.activeElement = this; }
    scrollIntoView() {}
    getBoundingClientRect() { return {left: 10, right: 110, top: 10, bottom: 40, width: 100, height: 30}; }
    showModal() { this.open = true; }
    close() { this.open = false; this.onclose?.(); }
  }
  const $ = id => { if (!controls.has(id)) controls.set(id, new Element(id)); return controls.get(id); };
  doc.getElementById = $; doc.createElement = () => new Element();
  $("view").value = "active"; $("group-by").value = "phase"; $("group-context-menu").hidden = true;
  $("group-context-menu").append($("context-rename-group")); $("group-context-menu").append($("context-remove-group"));
  const api = async (path, body) => {
    calls.push({path, body});
    const group = groups.find(group => group.id === path.split("/")[1]);
    if (path.startsWith("projects/")) {
      if (body) {
        if (path.endsWith("/delete")) groups.splice(groups.indexOf(group), 1);
        else Object.assign(group, body);
        return {...group};
      }
      return {...group, thread_count: group.thread_count || 0};
    }
    if (path === "organization") return {projects: groups};
    return {data: []};
  };
  const views = new SavedViews({document: doc, api, preferences, onSwitch: async () => {}, onRefresh: async () => {}, onError: error => errors.push(error)});
  views.groups = groups;
  // The projection also works independently of the larger group navigation UI.
  views.rebuild ||= () => { views.items = [views.all, ...views.groupItems()]; };
  views.rebuild(); views.render();
  return {views, $, doc, calls, errors, preferences};
}

const groupTab = (c, id) => c.$("saved-view-tabs").children.find(tab => tab.dataset.viewId === "group-" + id);
function dragEvent(clientX = 10) {
  return {clientX, dataTransfer: {values: {}, setData(key, value) { this.values[key] = value; }},
    preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }};
}

test("right-click rename keeps the selected group and filters while renaming the clicked group", async () => {
  const c = setup([{id: "launch", name: "Launch"}, {id: "keep", name: "Keep"}]);
  await c.views.activate("group-keep");
  c.$("search").value = "installer";
  groupTab(c, "launch").oncontextmenu({type: "contextmenu", ...dragEvent()});
  assert.equal(c.doc.activeElement, c.$("context-rename-group"));
  await c.$("context-rename-group").onclick();
  assert.equal(c.$("view-dialog").open, true);
  assert.equal(c.$("view-name").value, "Launch");
  assert.equal(c.$("group-context-menu").hidden, true);
  c.$("view-name").value = "Ship it";
  await c.$("view-form").onsubmit(dragEvent());
  assert.deepEqual(c.calls.filter(call => call.body), [{path: "projects/launch", body: {name: "Ship it"}}]);
  assert.equal(c.views.activeId, "group-keep");
  assert.equal(c.$("search").value, "installer");
  assert.equal(groupTab(c, "launch").children[0].textContent, "Ship it");
  assert.deepEqual(c.errors, []);
});

test("cancelling right-click rename writes nothing", async () => {
  const c = setup([{id: "launch", name: "Launch"}]);
  groupTab(c, "launch").oncontextmenu({type: "contextmenu", ...dragEvent()});
  await c.$("context-rename-group").onclick();
  c.$("view-name").value = "Changed"; c.$("view-dialog").close();
  assert.equal(c.calls.some(call => call.body), false);
  assert.equal(c.views.groups[0].name, "Launch");
});

test("keyboard context-menu navigation reaches rename and removal", () => {
  const c = setup([{id: "launch", name: "Launch"}]);
  groupTab(c, "launch").onkeydown({key: "F10", shiftKey: true, ...dragEvent()});
  assert.equal(c.doc.activeElement, c.$("context-rename-group"));
  for (const [key, id] of [["ArrowDown", "context-remove-group"], ["ArrowDown", "context-rename-group"],
    ["End", "context-remove-group"], ["Home", "context-rename-group"], ["ArrowUp", "context-remove-group"]]) {
    c.$("group-context-menu").onkeydown({key, ...dragEvent()});
    assert.equal(c.doc.activeElement, c.$(id));
  }
});

test("dragging groups reorders before and after without changing selection, filters, or membership", async () => {
  const c = setup(["a", "b", "c"].map(id => ({id, name: id})));
  await c.views.activate("group-b"); c.$("search").value = "installer";
  const source = groupTab(c, "c"), target = groupTab(c, "a"), start = dragEvent();
  assert.equal(source.draggable, true);
  assert.equal(c.$("saved-view-tabs").children[0].draggable, undefined);
  source.ondragstart(start);
  assert.equal(start.dataTransfer.values["application/x-ribbon-field-group"], "group-c");
  const over = dragEvent(20); target.ondragover(over);
  assert.equal(over.prevented, true); assert.equal(target.dataset.groupDrop, "before");
  c.views.sync([], c.views.groups); c.views.render();
  assert.equal(groupTab(c, "c"), source, "polling preserves the native drag source");
  target.ondrop(dragEvent(20)); source.ondragend();
  assert.deepEqual(c.views.items.map(view => view.id), ["all", "group-c", "group-a", "group-b"]);
  assert.equal(c.views.activeId, "group-b"); assert.equal(c.$("search").value, "installer");
  assert.equal(c.calls.some(call => call.body), false);
  groupTab(c, "c").ondragstart(dragEvent());
  groupTab(c, "b").ondragover(dragEvent(100));
  assert.equal(groupTab(c, "b").dataset.groupDrop, "after");
  groupTab(c, "b").ondrop(dragEvent(100));
  assert.deepEqual(c.views.items.map(view => view.id), ["all", "group-a", "group-b", "group-c"]);
  assert.equal(c.views.dragGroup, null);
});

test("group order survives fresh windows, inventory changes, and workspace switches", async () => {
  const groups = [{id: "a", name: "A", cwd: "/foo"}, {id: "b", name: "B", cwd: "/foo"},
    {id: "c", name: "C", cwd: "/bar"}, {id: "d", name: "D", cwd: "/bar"}];
  const c = setup(groups);
  await c.views.setProject("/foo");
  groupTab(c, "b").ondragstart(dragEvent()); groupTab(c, "a").ondrop(dragEvent(20));
  const fresh = setup(groups, c.preferences);
  await fresh.views.setProject("/foo");
  assert.deepEqual(fresh.views.groupItems().map(view => view.projectId), ["b", "a"]);
  await fresh.views.setProject("/bar");
  assert.deepEqual(fresh.views.groupItems().map(view => view.projectId), ["c", "d"]);
  fresh.views.moveGroup("group-d", "group-c");
  await fresh.views.setProject("/foo");
  fresh.views.sync([], [...groups.filter(group => group.id !== "a"), {id: "new", name: "New", cwd: "/foo"}]); fresh.views.render();
  assert.deepEqual(fresh.views.groupItems().map(view => view.projectId), ["b", "new"]);
  await fresh.views.setProject("/bar");
  assert.deepEqual(fresh.views.groupItems().map(view => view.projectId), ["d", "c"]);
});

test("external drags, cancelled drags, and drags across workspaces cannot change group order", async () => {
  const c = setup([{id: "a", name: "A", cwd: "/foo"}, {id: "b", name: "B", cwd: "/foo"}, {id: "c", name: "C", cwd: "/bar"}]);
  await c.views.setProject("/foo");
  const target = groupTab(c, "a"), external = dragEvent();
  target.ondragover(external); target.ondrop(external);
  assert.equal(external.prevented, undefined);
  const source = groupTab(c, "b"); source.ondragstart(dragEvent()); target.ondragover(dragEvent()); source.ondragend();
  assert.equal(target.dataset.groupDrop, undefined);
  assert.deepEqual(c.views.groupItems().map(view => view.projectId), ["a", "b"]);
  source.ondragstart(dragEvent()); await c.views.setProject("/bar");
  groupTab(c, "c").ondrop(dragEvent()); source.ondragend();
  assert.deepEqual(c.views.groupItems().map(view => view.projectId), ["c"]);
  assert.equal(c.preferences.getItem(c.views.key + ".group-order"), undefined);
});

test("invalid or unavailable preferences do not block group ordering", () => {
  for (const invalid of ["{", "{}", "null"]) {
    const c = setup([{id: "a", name: "A"}, {id: "b", name: "B"}], {getItem: () => invalid,
      setItem() { throw new Error("Storage disabled"); }});
    c.views.moveGroup("group-b", "group-a"); c.views.sync([], c.views.groups);
    assert.deepEqual(c.views.groupItems().map(view => view.projectId), ["b", "a"]);
    assert.deepEqual(c.errors, []);
  }
});

test("removing an empty active group immediately returns to All conversations", async () => {
  const c = setup([{id: "launch", name: "Launch"}, {id: "keep", name: "Keep"}]);
  await c.views.activate("group-launch");
  assert.equal(c.$("group-remove").hidden, false);
  await c.$("group-remove").onclick();
  assert.equal(c.$("remove-group-dialog").open, false);
  assert.equal(c.views.activeId, "all");
  assert.deepEqual(c.views.groups.map(group => group.id), ["keep"]);
  assert.equal(c.$("group-remove").hidden, true);
  assert.deepEqual(c.calls.filter(call => call.path.endsWith("/delete")), [{path: "projects/launch/delete", body: {thread_count: 0}}]);
  assert.deepEqual(c.errors, []);
});

test("right-click removal targets the clicked group and confirms its complete count", async () => {
  const c = setup([{id: "launch", name: "Launch", thread_count: 5}, {id: "keep", name: "Keep"}]);
  await c.views.activate("group-keep");
  const tab = c.$("saved-view-tabs").children.find(tab => tab.dataset.viewId === "group-launch");
  let prevented = false;
  tab.oncontextmenu({type: "contextmenu", clientX: 30, clientY: 40, preventDefault() { prevented = true; }, stopPropagation() {}});
  assert.equal(prevented, true); assert.equal(c.views.activeId, "group-keep");
  assert.equal(c.$("group-context-menu").hidden, false);
  await c.$("context-remove-group").onclick();
  assert.equal(c.$("remove-group-dialog").open, true);
  assert.equal(c.$("remove-group-title").textContent, "Remove “Launch”?");
  assert.match(c.$("remove-group-description").textContent, /5 conversations will become ungrouped/);
  assert.equal(c.doc.activeElement, c.$("cancel-remove-group"));
  assert.equal(c.calls.some(call => call.path.endsWith("/delete")), false);
  await c.$("remove-group-form").onsubmit({preventDefault() {}});
  assert.equal(c.views.activeId, "group-keep");
  assert.deepEqual(c.calls.filter(call => call.path.endsWith("/delete")), [{path: "projects/launch/delete", body: {thread_count: 5}}]);
  assert.deepEqual(c.errors, []);
});

test("cancelling group removal writes nothing and Shift+F10 opens the same menu", async () => {
  const c = setup([{id: "launch", name: "Launch", thread_count: 1}]);
  await c.views.activate("group-launch");
  const tab = c.$("saved-view-tabs").children.find(tab => tab.dataset.viewId === "group-launch");
  tab.onkeydown({key: "F10", shiftKey: true, preventDefault() {}, stopPropagation() {}});
  assert.equal(c.$("group-context-menu").hidden, false);
  c.doc.listeners.keydown({key: "Escape", preventDefault() {}});
  assert.equal(c.$("group-context-menu").hidden, true);
  assert.equal(c.doc.activeElement, tab);
  await c.$("group-remove").onclick();
  c.$("remove-group-dialog").close();
  assert.equal(c.views.removeTarget, null);
  assert.equal(c.calls.some(call => call.path.endsWith("/delete")), false);
  assert.equal(c.views.activeId, "group-launch");
});

test("membership changes keep the confirmation open and leave the group in place", async () => {
  const c = setup([{id: "launch", name: "Launch", thread_count: 1}]);
  await c.views.activate("group-launch");
  await c.$("group-remove").onclick();
  const api = c.views.api;
  c.views.api = (path, body) => path.endsWith("/delete") ? Promise.reject(new Error("Group membership changed. Try again.")) : api(path, body);
  await c.$("remove-group-form").onsubmit({preventDefault() {}});
  assert.equal(c.$("remove-group-dialog").open, true);
  assert.equal(c.$("remove-group-error").hidden, false);
  assert.match(c.$("remove-group-error").textContent, /membership changed/);
  assert.equal(c.$("confirm-remove-group").disabled, false);
  assert.equal(c.views.activeId, "group-launch");
});
