"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {threadViews, SavedViews} = require("../plugins/agent-coord/scripts/agent_coord/web/views.js");
const grouping = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");

const thread = (id, values = {}) => ({thread_id: id, title: id, attention: "now", repository_id: "rig", project_id: "migration",
  response_state: "working", checkpoint: {phase: "implementation"}, ...values});

test("filters intersect, include unassigned threads, and separate closed threads", () => {
  const input = [thread("rig"), thread("stoic", {repository_id: "stoic"}),
    thread("unassigned", {repository_id: null, project_id: null}), thread("closed", {attention: "archived"})];
  const ids = filters => input.filter(t => threadViews.matches(t, filters)).map(t => t.thread_id);
  assert.deepEqual(ids({project: "migration"}), ["rig", "stoic"]);
  assert.deepEqual(ids({project: "migration", repository: "rig"}), ["rig"]);
  assert.deepEqual(ids({repository: "__none__", project: "__none__"}), ["unassigned"]);
  assert.deepEqual(ids({repository: "missing"}), []);
  assert.deepEqual(ids({show: "archived"}), ["closed"]);
  assert.deepEqual(ids({search: "STOIC", phase: "implementation"}), ["stoic"]);
  assert.deepEqual(ids({phase: "validation"}), []);
});

test("tab badges count pinned requests, exclude Later, and share completion read state", () => {
  const input = [thread("reply", {response_state: "reply", pinned: true}), thread("approval", {response_state: "input"}),
    thread("failure", {response_state: "failed"}), thread("later", {response_state: "reply", attention: "later"}),
    thread("done", {response_state: "completed", unread_result: true}), thread("closed", {response_state: "reply", attention: "archived"})];
  assert.deepEqual(threadViews.badges(input, {}), {attention: 3, completed: true});
  assert.deepEqual(threadViews.badges(input, {show: "completed"}), {attention: 0, completed: true});
  assert.deepEqual(threadViews.badges(input, {show: "archived"}), {attention: 0, completed: false});
  assert.deepEqual(input.filter(t => threadViews.matches(t, {show: "attention"})), input.filter(grouping.awaitsUser));
  input[4].unread_result = false;
  for (const filter of [{repository: "rig"}, {project: "migration"}]) {
    assert.deepEqual(threadViews.badges(input, filter), {attention: 3, completed: false});
  }
});

test("keyboard shortcuts cycle views without intercepting ordinary Tab or browser shortcuts", () => {
  assert.equal(threadViews.shortcut({key: "ArrowRight", ctrlKey: true, shiftKey: true}, 2, 3), 0);
  assert.equal(threadViews.shortcut({key: "ArrowLeft", ctrlKey: true, shiftKey: true}, 0, 3), 2);
  assert.equal(threadViews.shortcut({key: "Tab"}, 0, 3), null);
  assert.equal(threadViews.shortcut({key: "Tab", ctrlKey: true}, 0, 3), null);
  assert.equal(threadViews.shortcut({key: "ArrowRight", metaKey: true}, 0, 3), null);
  assert.equal(threadViews.shortcut({key: "ArrowRight", isComposing: true}, 0, 3, true), null);
  assert.equal(threadViews.shortcut({key: "End"}, 0, 3, true), 2);
  assert.equal(threadViews.shortcut({key: "Home"}, 2, 3, true), 0);
});

function storage() {
  const values = new Map();
  return {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value)};
}

function setup({saved = [], windowStorage = storage(), preferences = storage()} = {}) {
  const doc = {activeElement: null, listeners: {}, body: {classList: {contains: () => false}},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    querySelector() { return Object.values(controls).find(el => el.open && el.id.endsWith("dialog")) || null; }};
  class Element {
    constructor(id = "") { this.id = id; this.value = ""; this.children = []; this.dataset = {}; this.attributes = {};
      this.scrollTop = 0; this.hidden = false; this.classList = {contains: () => false}; }
    get options() { return this.children; }
    append(child) { child.parent = this; this.children.push(child); }
    replaceChildren(...children) { this.children = []; children.forEach(child => this.append(child)); }
    setAttribute(key, value) { this.attributes[key] = value; }
    contains(element) { return element === this || this.children.some(child => child.contains(element)); }
    querySelector() { return this.children.find(el => el.attributes["aria-selected"] === "true"); }
    closest() { return null; }
    focus() { doc.activeElement = this; }
    scrollIntoView() {}
    showModal() { this.open = true; }
    close() { this.open = false; }
  }
  const ids = ["repository", "project", "phase-filter", "view", "search", "group-by", "welcome", "view-tabs", "view-menu",
    "filter-menu", "reset-view", "update-view", "view-move-left", "view-move-right", "back-home", "overview-title", "add-view",
    "save-view", "view-dialog-title", "view-dialog-description", "view-name", "view-error", "view-dialog", "view-form",
    "view-rename", "view-duplicate", "view-delete", "view-menu-toggle"];
  const controls = Object.fromEntries(ids.map(id => [id, new Element(id)]));
  controls.view.value = "active"; controls["group-by"].value = "phase";
  doc.getElementById = id => controls[id]; doc.createElement = () => new Element();
  const records = saved.map(v => ({...v, filters: threadViews.filters(v.filters), version: v.version || 1, group_by: v.group_by || "phase"}));
  const calls = [], errors = [];
  const api = async (path, body) => {
    calls.push({path, body});
    if (!body) return {data: structuredClone(records)};
    const [, id, operation] = path.split("/");
    if (!id) {
      const item = {id: "created-" + records.length, ...structuredClone(body), version: 1}; records.push(item); return structuredClone(item);
    }
    const index = records.findIndex(view => view.id === id), item = records[index];
    if (operation === "move") {
      const next = index + (body.direction === "left" ? -1 : 1);
      [records[index], records[next]] = [records[next], records[index]];
      return {data: structuredClone(records)};
    }
    if (!item || item.version !== body.version) throw new Error("View changed in another window");
    if (operation === "delete") { records.splice(index, 1); return {data: structuredClone(records)}; }
    Object.assign(item, structuredClone(body), {version: item.version + 1}); return structuredClone(item);
  };
  let switches = 0;
  const views = new SavedViews({document: doc, api, storage: windowStorage, preferences, onError: error => errors.push(error),
    onSwitch: async () => { switches++; controls.welcome.hidden = false; }});
  return {views, controls, doc, calls, records, errors, windowStorage, preferences, switches: () => switches};
}

test("switching remembers each view's filters, grouping, and scroll without saving temporary edits", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}},
    {id: "stoic", name: "Stoic", filters: {repository: "stoic"}, group_by: "none"}]});
  await c.views.start();
  await c.views.activate("rig");
  c.controls.search.value = "Temporary search"; c.controls["group-by"].value = "project"; c.controls.welcome.scrollTop = 340;
  await c.views.activate("stoic");
  assert.equal(c.controls.search.value, ""); assert.equal(c.controls["group-by"].value, "none");
  c.controls.welcome.scrollTop = 92;
  await c.views.activate("rig");
  assert.equal(c.controls.search.value, "Temporary search"); assert.equal(c.controls["group-by"].value, "project");
  assert.equal(c.controls.welcome.scrollTop, 340);
  assert.equal(c.views.dirty(), true);
  assert.equal(c.calls.filter(call => call.body).length, 0);
  assert.equal(c.records[0].filters.search, "");
  await c.views.activate("rig", true);
  assert.equal(c.controls.search.value, ""); assert.equal(c.views.dirty(), false);
});

test("update explicitly saves filters and survives a fresh window", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}}]});
  await c.views.start(); await c.views.activate("rig");
  c.controls["phase-filter"].value = "validation";
  await c.controls["update-view"].onclick();
  assert.equal(c.records[0].filters.phase, "validation"); assert.equal(c.views.dirty(), false);
  assert.equal(c.records[0].version, 2);
  const other = setup({saved: c.records, preferences: c.preferences});
  await other.views.start();
  assert.equal(other.views.activeId, "rig"); assert.equal(other.controls["phase-filter"].value, "validation");
});

test("polling preserves unsaved changes and refuses to overwrite a newer saved definition", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}}]});
  await c.views.start(); await c.views.activate("rig");
  c.controls.search.value = "unsaved";
  c.records[0].version = 2; c.records[0].filters.phase = "validation";
  c.views.sync(structuredClone(c.records));
  assert.equal(c.controls.search.value, "unsaved");
  await c.controls["update-view"].onclick();
  assert.match(c.errors[0].message, /another window/);
  assert.equal(c.records[0].filters.search, "");
  await c.views.activate("rig", true);
  assert.equal(c.controls.search.value, ""); assert.equal(c.controls["phase-filter"].value, "validation");
  c.records.length = 0; c.views.sync([]);
  assert.equal(c.views.activeId, "all"); assert.equal(c.controls.repository.value, "");
});

test("rapid switches keep the latest view's scroll even when earlier requests finish last", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig"}, {id: "stoic", name: "Stoic"}]});
  await c.views.start();
  c.views.navigation.rig = {version: 1, filters: {}, group_by: "phase", scroll: 100};
  c.views.navigation.stoic = {version: 1, filters: {}, group_by: "phase", scroll: 200};
  const resolve = [];
  c.views.onSwitch = () => new Promise(done => resolve.push(done));
  const first = c.views.activate("rig"), second = c.views.activate("stoic");
  resolve[1](); await second; resolve[0](); await first;
  assert.equal(c.views.activeId, "stoic"); assert.equal(c.controls.welcome.scrollTop, 200);
});

test("optional browser storage cannot prevent view creation or navigation", async () => {
  const blocked = {getItem() { throw new Error("Unavailable"); }, setItem() { throw new Error("Unavailable"); }};
  const c = setup({windowStorage: blocked, preferences: blocked});
  await c.views.start(); await c.views.activate("all"); c.views.remember();
  assert.equal(c.views.activeId, "all"); assert.deepEqual(c.errors, []);
});

test("save, rename, duplicate, reorder and delete change only view definitions", async () => {
  const c = setup(); await c.views.start();
  c.controls.repository.value = "rig"; c.controls["group-by"].value = "repository";
  c.controls["add-view"].onclick(); c.controls["view-name"].value = "Rig";
  await c.controls["view-form"].onsubmit({preventDefault() {}});
  const id = c.views.activeId;
  assert.equal(c.records[0].filters.repository, "rig");
  assert.equal(c.records[0].group_by, "repository");
  assert.equal(c.controls["view-dialog"].open, false);
  c.controls.search.value = "temporary";
  c.controls["view-rename"].onclick(); c.controls["view-name"].value = "Rig work";
  await c.controls["view-form"].onsubmit({preventDefault() {}});
  assert.equal(c.records[0].name, "Rig work"); assert.equal(c.controls.search.value, "temporary");
  c.controls["view-duplicate"].onclick(); c.controls["view-name"].value = "Rig copy";
  await c.controls["view-form"].onsubmit({preventDefault() {}});
  const copy = c.views.activeId;
  assert.notEqual(copy, id); assert.equal(c.records[1].filters.search, "");
  assert.deepEqual(c.records[1].filters, c.records[0].filters);
  await c.controls["view-move-left"].onclick();
  assert.deepEqual(c.records.map(v => v.id), [copy, id]);
  await c.controls["view-delete"].onclick();
  assert.equal(c.views.activeId, "all"); assert.equal(c.records.length, 1);
  assert.equal(c.records[0].id, id); assert.equal(c.views.navigation[copy], undefined);
  assert.ok(c.calls.every(call => call.path.startsWith("views")));
  assert.deepEqual(c.errors, []);
});

test("tabs remain accessible as live counts update and text editing retains its shortcuts", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}}]});
  await c.views.start(); await c.views.activate("rig");
  c.views.render([thread("approval", {response_state: "input"})]);
  assert.equal(c.doc.activeElement.dataset.viewId, "rig");
  assert.match(c.doc.activeElement.attributes["aria-label"], /1 need you/);
  assert.equal(c.controls["view-tabs"].children.filter(el => el.tabIndex === 0).length, 1);
  let prevented = false;
  c.doc.listeners.keydown({key: "ArrowLeft", ctrlKey: true, shiftKey: true,
    target: {closest: () => ({})}, preventDefault() { prevented = true; }});
  assert.equal(prevented, false); assert.equal(c.views.activeId, "rig");
  c.controls.welcome.scrollTop = 123; c.views.remember(); c.controls.welcome.hidden = true; c.controls.welcome.scrollTop = 0;
  c.views.remember();
  assert.equal(c.views.navigation.rig.scroll, 123);
});

test("reload restores a window's temporary state and falls back safely for a deleted active view", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}}]});
  await c.views.start(); await c.views.activate("rig");
  c.controls.search.value = "draft filter"; c.controls.welcome.scrollTop = 88; c.views.remember();
  const reopened = setup({saved: c.records, windowStorage: c.windowStorage, preferences: c.preferences});
  await reopened.views.start(); reopened.views.restoreScroll();
  assert.equal(reopened.controls.search.value, "draft filter"); assert.equal(reopened.controls.welcome.scrollTop, 88);
  const deleted = setup({windowStorage: c.windowStorage, preferences: c.preferences});
  await deleted.views.start();
  assert.equal(deleted.views.activeId, "all"); assert.equal(deleted.controls.repository.value, "");
});
