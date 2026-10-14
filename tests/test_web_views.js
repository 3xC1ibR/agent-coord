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

test("orchestrating filters select coordinating threads independently of specialist phases", () => {
  const dispatcher=thread("dispatcher",{work_phase:"orchestrating"});
  const specialist=thread("specialist",{work_phase:"implementation"});
  assert.equal(threadViews.matches(dispatcher,{phase:"orchestrating"}),true);
  assert.equal(threadViews.matches(specialist,{phase:"orchestrating"}),false);
  assert.equal(threadViews.matches(dispatcher,{phase:"orchestrating",repository:"other"}),false);
});

test("tab badges share handled attention, include pins, and exclude Later", () => {
  const input = [thread("reply", {response_state: "reply", pinned: true}), thread("approval", {response_state: "input"}),
    thread("failure", {response_state: "failed"}), thread("later", {response_state: "reply", attention: "later"}),
    thread("done", {response_state: "reply", unhandled_response: true, unread_result: true}), thread("closed", {response_state: "reply", attention: "archived"})];
  assert.deepEqual(threadViews.badges(input, {}), {attention: 4, active: false, completed: true});
  assert.deepEqual(threadViews.badges(input, {show: "completed"}), {attention: 0, active: false, completed: false});
  assert.deepEqual(threadViews.badges(input, {show: "archived"}), {attention: 0, active: false, completed: false});
  assert.deepEqual(input.filter(t => threadViews.matches(t, {show: "attention"})), input.filter(grouping.awaitsUser));
  input[4].unread_result = false;
  for (const filter of [{repository: "rig"}, {project: "migration"}]) {
    assert.deepEqual(threadViews.badges(input, filter), {attention: 4, active: false, completed: false});
  }
});

test("active-work badges follow the view's filters and ignore waiting or closed work", () => {
  const input = [thread("running", {attention: "later"}), thread("waiting", {repository_id: "stoic", response_state: "input"}),
    thread("closed", {repository_id: "stoic", attention: "archived"})];
  assert.equal(threadViews.badges(input, {repository: "rig", project: "migration", phase: "implementation", search: "running", show: "later"}).active, true);
  for (const filters of [{repository: "stoic"}, {project: "other"}, {phase: "finished"}, {search: "waiting"},
    {show: "attention"}, {show: "completed"}, {show: "archived"}]) {
    assert.equal(threadViews.badges(input, filters).active, false);
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
    "view-rename", "view-duplicate", "view-delete", "view-menu-toggle", "view-custom-actions", "filter-count",
    "mobile-view", "mobile-view-attention", "mobile-view-activity", "saved-view-tabs", "show-now", "show-later", "show-closed"];
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

test("mobile picker switches saved filters, keeps counts current, and falls back after deletion", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}},
    {id: "stoic", name: "Stoic", filters: {repository: "stoic"}}]});
  await c.views.start();
  c.views.mobile = {matches: true};
  c.views.render([thread("approval", {response_state: "input"})]);
  const picker = c.controls["mobile-view"];
  assert.match(picker.children.find(option => option.value === "rig").textContent, /1 need attention/);
  picker.value = "rig";
  await picker.onchange();
  assert.equal(c.views.activeId, "rig");
  assert.equal(c.controls.repository.value, "rig");
  assert.equal(c.controls["mobile-view-attention"].textContent, "1");
  assert.equal(c.controls["mobile-view-attention"].hidden, false);
  assert.equal(c.controls["filter-count"].textContent, "1");
  c.controls["phase-filter"].value = "validation";
  await c.views.persist();
  assert.equal(c.controls["filter-count"].textContent, "2");
  assert.equal(c.controls["mobile-view-attention"].hidden, true);
  await c.controls["view-delete"].onclick();
  assert.equal(picker.value, "all");
  assert.equal(c.controls["filter-count"].hidden, true);
  assert.equal(c.controls["view-custom-actions"].hidden, true);
  assert.deepEqual(c.errors, []);
});

test("an agent project link resets temporary filters without saving over the active named view", async () => {
  const c = setup({saved: [{id: "review", name: "Review", filters: {project: "other", search: "invoice"}}]});
  await c.views.start(); await c.views.activate("review");
  const before = structuredClone(c.records);
  await c.views.overview({project: "billing"});
  assert.equal(c.views.activeId, "all");
  assert.equal(c.controls.project.value, "billing");
  assert.equal(c.controls.search.value, "");
  assert.deepEqual(c.records, before);
  assert.equal(c.calls.filter(call => call.body).length, 0);
  await c.views.activate("review");
  assert.equal(c.controls.project.value, "other");
  assert.equal(c.controls.search.value, "invoice");
});

test("named views automatically save filters and grouping while keeping their own scroll", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}},
    {id: "stoic", name: "Stoic", filters: {repository: "stoic"}, group_by: "none"}]});
  await c.views.start();
  await c.views.activate("rig");
  c.controls.search.value = "Saved search"; c.controls["group-by"].value = "project"; c.controls.welcome.scrollTop = 340;
  await c.views.persist();
  await c.views.activate("stoic");
  assert.equal(c.controls.search.value, ""); assert.equal(c.controls["group-by"].value, "none");
  c.controls.welcome.scrollTop = 92;
  await c.views.activate("rig");
  assert.equal(c.controls.search.value, "Saved search"); assert.equal(c.controls["group-by"].value, "project");
  assert.equal(c.controls.welcome.scrollTop, 340);
  assert.equal(c.views.dirty(), false);
  assert.equal(c.calls.filter(call => call.body).length, 1);
  assert.equal(c.records[0].filters.search, "Saved search");
  await c.views.activate("rig", true);
  assert.equal(c.controls.search.value, "Saved search"); assert.equal(c.views.dirty(), false);
});

test("domain grouping is restored for saved views and survives window reopen", async () => {
  const c = setup({saved: [{id: "domains", name: "Domains", filters: {}, group_by: "domain"},
    {id: "phases", name: "Phases", filters: {}, group_by: "phase"}]});
  await c.views.start();
  await c.views.activate("domains");
  assert.equal(c.controls["group-by"].value, "domain");
  await c.views.activate("phases");
  c.controls["group-by"].value = "domain";
  await c.views.persist();
  assert.equal(c.records[1].group_by, "domain");
  const reopened = setup({saved: c.records, windowStorage: c.windowStorage, preferences: c.preferences});
  await reopened.views.start();
  assert.equal(reopened.controls["group-by"].value, "domain");
});

test("automatic filter saving survives a fresh window", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}}]});
  await c.views.start(); await c.views.activate("rig");
  c.controls["phase-filter"].value = "validation";
  await c.views.persist();
  assert.equal(c.records[0].filters.phase, "validation"); assert.equal(c.views.dirty(), false);
  assert.equal(c.records[0].version, 2);
  const other = setup({saved: c.records, preferences: c.preferences});
  await other.views.start();
  assert.equal(other.views.activeId, "rig"); assert.equal(other.controls["phase-filter"].value, "validation");
});

test("Now restores its filters after visiting a named view", async () => {
  const c = setup({saved: [{id: "review", name: "Review", filters: {phase: "validation"}}]});
  await c.views.start();
  c.controls.project.value = "migration";
  c.controls["phase-filter"].value = "implementation";
  c.controls.view.value = "attention";
  c.controls.search.value = "release";
  c.controls["group-by"].value = "domain";
  await c.views.persist();
  const expected = c.views.read();
  await c.views.activate("review");
  await c.controls["show-now"].onclick();
  assert.deepEqual(c.views.read(), expected);
  assert.equal(c.controls["show-now"].attributes["aria-pressed"], "true");
  assert.equal(c.calls.filter(call => call.body).length, 0);
  assert.equal(c.records[0].filters.phase, "validation");
});

test("built-in filters survive reopening, stay scoped to their workspace, and reset durably", async () => {
  const c = setup({saved: [{id: "review", name: "Review", cwd: "/opt/foo"}]});
  await c.views.setProject("/opt/foo");
  c.controls.project.value = "migration";
  c.controls["phase-filter"].value = "implementation";
  c.controls.search.value = "release";
  c.controls["group-by"].value = "domain";
  c.controls.welcome.scrollTop = 340;
  await c.views.persist();
  const expected = c.views.read();
  await c.views.activate("review");
  const reopened = setup({saved: c.records, preferences: c.preferences});
  await reopened.views.setProject("/opt/foo");
  await reopened.controls["show-now"].onclick();
  assert.deepEqual(reopened.views.read(), expected);
  assert.equal(reopened.controls.welcome.scrollTop, 0);
  await reopened.views.setProject("/opt/bar");
  assert.equal(reopened.controls.project.value, "");
  assert.equal(reopened.controls["phase-filter"].value, "");
  await reopened.views.setProject("/opt/foo");
  assert.deepEqual(reopened.views.read(), expected);
  await reopened.controls["reset-view"].onclick();
  const reset = setup({preferences: c.preferences, windowStorage: reopened.windowStorage});
  await reset.views.setProject("/opt/foo");
  assert.equal(reset.controls["phase-filter"].value, "");
  assert.equal(reset.controls.search.value, "");
  const fresh = setup({preferences: c.preferences});
  await fresh.views.setProject("/opt/foo");
  assert.equal(fresh.controls.project.value, "");
  assert.equal(fresh.controls["phase-filter"].value, "");
  assert.equal(fresh.controls.search.value, "");
  assert.equal(fresh.controls["group-by"].value, "phase");
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
  await c.controls["add-view"].onclick(); c.controls["view-name"].value = "Rig";
  await c.controls["view-form"].onsubmit({preventDefault() {}});
  const id = c.views.activeId;
  assert.equal(c.records[0].filters.repository, "rig");
  assert.equal(c.records[0].group_by, "repository");
  assert.equal(c.controls["view-dialog"].open, false);
  c.controls.search.value = "temporary";
  await c.controls["view-rename"].onclick(); c.controls["view-name"].value = "Rig work";
  await c.controls["view-form"].onsubmit({preventDefault() {}});
  assert.equal(c.records[0].name, "Rig work"); assert.equal(c.controls.search.value, "temporary");
  await c.controls["view-duplicate"].onclick(); c.controls["view-name"].value = "Rig copy";
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

test("view tabs keep focus as live counts update and text editing retains its shortcuts", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}}]});
  await c.views.start(); await c.views.activate("rig");
  c.views.render([thread("approval", {response_state: "input"})]);
  assert.equal(c.doc.activeElement, c.controls["saved-view-tabs"].children[0]);
  assert.equal(c.doc.activeElement.attributes["aria-selected"], "true");
  assert.match(c.doc.activeElement.attributes["aria-label"], /1 need attention/);
  assert.equal(c.controls["mobile-view-attention"].textContent, "1");
  assert.equal(c.controls["view-tabs"].children.length, 0);
  let prevented = false;
  c.doc.listeners.keydown({key: "ArrowLeft", ctrlKey: true, shiftKey: true,
    target: {closest: () => ({})}, preventDefault() { prevented = true; }});
  assert.equal(prevented, false); assert.equal(c.views.activeId, "rig");
  c.controls.welcome.scrollTop = 123; c.views.remember(); c.controls.welcome.hidden = true; c.controls.welcome.scrollTop = 0;
  c.views.remember();
  assert.equal(c.views.navigation.rig.scroll, 123);
});

test("view dots show running work and return to the unread-result state when it stops", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}},
    {id: "stoic", name: "Stoic", filters: {repository: "stoic"}}]});
  await c.views.start(); await c.views.activate("rig");
  const running = thread("running"), completed = thread("done", {response_state: "reply", unhandled_response: true, unread_result: true});
  c.views.render([running, completed]);
  const activity = c.controls["mobile-view-activity"];
  assert.equal(activity.className, "view-active");
  assert.equal(activity.hidden, false);
  running.response_state = "available";
  c.views.render([running, completed]);
  assert.equal(activity.className, "view-completed");
  assert.equal(c.doc.activeElement, c.controls["saved-view-tabs"].children[0]);
  completed.unread_result = false; c.views.render([running, completed]);
  assert.equal(activity.hidden, true);
});

test("reload migrates remembered named-view filters and falls back safely for a deleted view", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}}]});
  await c.views.start(); await c.views.activate("rig");
  c.controls.search.value = "draft filter"; c.controls.welcome.scrollTop = 88; c.views.remember();
  const reopened = setup({saved: c.records, windowStorage: c.windowStorage, preferences: c.preferences});
  await reopened.views.start(); reopened.views.restoreScroll();
  assert.equal(reopened.controls.search.value, "draft filter"); assert.equal(reopened.controls.welcome.scrollTop, 88);
  assert.equal(reopened.records[0].filters.search, "draft filter");
  assert.deepEqual(Object.keys(reopened.calls.find(call => call.body).body).sort(), ["filters", "group_by", "version"]);
  const deleted = setup({windowStorage: c.windowStorage, preferences: c.preferences});
  await deleted.views.start();
  assert.equal(deleted.views.activeId, "all"); assert.equal(deleted.controls.repository.value, "");
});

test("badges follow each view's filters immediately, including views with no requests", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig"}, {id: "stoic", name: "Stoic"}]});
  await c.views.start();
  const input = [thread("one", {repository_id: "stoic", response_state: "reply"}),
    thread("two", {repository_id: "stoic", response_state: "input"}),
    thread("elsewhere", {repository_id: "coord", response_state: "reply"})];
  c.views.render(input);
  await c.views.activate("rig"); c.controls.repository.value = "rig";
  const rigSave = c.views.persist();
  assert.equal(c.controls["mobile-view-attention"].hidden, true);
  await c.views.activate("stoic"); c.controls.repository.value = "stoic";
  const stoicSave = c.views.persist();
  assert.equal(c.controls["mobile-view-attention"].textContent, "2");
  await Promise.all([rigSave, stoicSave]);
  c.controls.search.value = "no matches"; await c.views.persist();
  assert.equal(c.controls["mobile-view-attention"].hidden, true);
});

test("rapid edits serialize by view and delayed responses cannot retarget a save", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig"}, {id: "stoic", name: "Stoic"}]});
  await c.views.start(); await c.views.activate("rig");
  const api = c.views.api, requests = [];
  c.views.api = async (path, body) => {
    if (body) await new Promise(resolve => requests.push(resolve));
    return api(path, body);
  };
  c.controls.repository.value = "rig";
  const first = c.views.persist(); await Promise.resolve();
  c.controls.search.value = "latest"; c.controls["group-by"].value = "project";
  const latest = c.views.persist();
  await c.views.activate("stoic"); c.controls.repository.value = "stoic";
  const other = c.views.persist(); await Promise.resolve();
  requests[1](); await other;
  assert.equal(c.records[1].filters.repository, "stoic");
  requests[0]();
  while (requests.length < 3) await new Promise(resolve => setImmediate(resolve));
  assert.equal(c.controls.repository.value, "stoic"); assert.equal(c.controls.search.value, "");
  await c.views.activate("rig");
  assert.equal(c.controls.search.value, "latest");
  requests[2](); await Promise.all([first, latest]);
  assert.equal(c.records[0].filters.repository, "rig");
  assert.equal(c.records[0].filters.search, "latest");
  assert.equal(c.records[0].group_by, "project"); assert.equal(c.records[0].version, 3);
  assert.equal(c.views.dirty(), false); assert.deepEqual(c.errors, []);
});

test("failed automatic saves preserve the filters for retry and reset loads a concurrent edit", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}}]});
  await c.views.start(); await c.views.activate("rig");
  const api = c.views.api;
  c.views.api = async (path, body) => { if (body) throw new Error("Offline"); return api(path, body); };
  c.controls.search.value = "keep this"; await c.views.persist();
  assert.equal(c.controls.search.value, "keep this");
  assert.equal(c.controls["update-view"].textContent, "Retry saving");
  assert.equal(c.controls["reset-view"].hidden, false);
  c.views.api = api; await c.controls["update-view"].onclick();
  assert.equal(c.records[0].filters.search, "keep this"); assert.equal(c.views.dirty(), false);
  c.records[0].version++; c.records[0].filters.phase = "validation";
  c.controls.search.value = "conflicting edit"; await c.views.persist();
  assert.match(c.errors.at(-1).message, /another window/);
  assert.equal(c.records[0].filters.search, "keep this");
  await c.controls["reset-view"].onclick();
  assert.equal(c.controls.search.value, "keep this"); assert.equal(c.controls["phase-filter"].value, "validation");
  assert.equal(c.views.dirty(), false);
});

test("All work filters remain local and stale remembered filters cannot replace newer saved views", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig", filters: {repository: "rig"}}]});
  await c.views.start(); c.controls.repository.value = "stoic"; await c.views.persist();
  assert.equal(c.calls.filter(call => call.body).length, 0);
  assert.equal(c.controls["reset-view"].hidden, false);
  await c.views.activate("rig"); await c.views.activate("all");
  assert.equal(c.controls.repository.value, "stoic");
  await c.controls["reset-view"].onclick(); assert.equal(c.controls.repository.value, "");
  await c.views.activate("rig"); c.controls.search.value = "old draft"; c.views.remember();
  c.records[0].version++; c.records[0].filters.phase = "validation";
  const reopened = setup({saved: c.records, windowStorage: c.windowStorage, preferences: c.preferences});
  await reopened.views.start();
  assert.equal(reopened.controls.search.value, ""); assert.equal(reopened.controls["phase-filter"].value, "validation");
  assert.equal(reopened.calls.filter(call => call.body).length, 0);
});

test("view actions wait for automatic saves without targeting a different tab", async () => {
  const c = setup({saved: [{id: "rig", name: "Rig"}, {id: "stoic", name: "Stoic"}]});
  await c.views.start(); await c.views.activate("rig");
  const api = c.views.api;
  let release;
  c.views.api = async (path, body) => {
    if (path === "views/rig" && body) await new Promise(resolve => { release = resolve; });
    return api(path, body);
  };
  c.controls.repository.value = "rig";
  const saving = c.views.persist(); await Promise.resolve();
  const renaming = c.controls["view-rename"].onclick();
  const deleting = c.controls["view-delete"].onclick();
  await c.views.activate("stoic"); release();
  await Promise.all([saving, renaming, deleting]);
  assert.equal(c.controls["view-dialog"].open, undefined);
  assert.deepEqual(c.records.map(view => view.id), ["stoic"]);
  assert.equal(c.views.activeId, "stoic"); assert.deepEqual(c.errors, []);
});


test("project switching isolates views and remembers each project's filters", async () => {
  const c = setup({saved: [
    {id: "parent", name: "Review", cwd: "/projects", filters: {phase: "validation"}},
    {id: "child", name: "Review", cwd: "/projects/website", filters: {phase: "planning"}},
  ]});
  await c.views.setProject("/projects");
  assert.deepEqual(c.views.items.map(v => v.id), ["all", "parent"]);
  assert.deepEqual(c.controls["saved-view-tabs"].children.map(v => v.dataset.viewId), ["parent"]);
  await c.views.activate("parent");
  await c.views.setProject("/projects/website");
  assert.deepEqual(c.views.items.map(v => v.id), ["all", "child"]);
  assert.deepEqual(c.controls["saved-view-tabs"].children.map(v => v.dataset.viewId), ["child"]);
  assert.equal(c.views.activeId, "all");
  c.controls.search.value = "child filter"; c.views.remember();
  await c.views.setProject("/projects");
  assert.equal(c.views.activeId, "parent");
  assert.equal(c.controls["phase-filter"].value, "validation");
  assert.equal(c.controls.search.value, "");
  await c.views.setProject("/projects/website");
  assert.equal(c.controls.search.value, "child filter");
  await c.views.openDialog("create");
  c.controls["view-name"].value = "Only here";
  await c.controls["view-form"].onsubmit({preventDefault() {}});
  assert.equal(c.records.at(-1).cwd, "/projects/website");
});

test("workspace tabs and mobile pickers exclude other workspaces and unassigned legacy views", async () => {
  const c = setup({saved: [
    {id: "foo", name: "Review", cwd: "/opt/foo"},
    {id: "bar", name: "Review", cwd: "/opt/bar"},
    {id: "legacy", name: "Old view", cwd: null},
    {id: "missing", name: "Unassigned view"},
  ]});
  for (const [cwd, id] of [["/opt/foo", "foo"], ["/opt/bar", "bar"], ["/opt/foo", "foo"]]) {
    await c.views.setProject(cwd);
    c.views.sync(structuredClone(c.records)); c.views.render();
    assert.deepEqual(c.views.items.map(view => view.id), ["all", id]);
    assert.deepEqual(c.controls["saved-view-tabs"].children.map(tab => tab.dataset.viewId), [id]);
    assert.deepEqual(c.controls["mobile-view"].children.map(option => option.value), ["all", id]);
    await c.views.activate(id === "foo" ? "bar" : "foo");
    assert.equal(c.views.activeId, "all");
  }
  assert.equal(c.records.length, 4);
  assert.deepEqual(c.errors, []);
});

test("created and duplicated views keep their workspace after switching and reopening", async () => {
  const c = setup();
  await c.views.setProject("/opt/foo");
  c.controls["phase-filter"].value = "validation";
  await c.views.openDialog("create"); c.controls["view-name"].value = "Review";
  await c.controls["view-form"].onsubmit({preventDefault() {}});
  const original = c.views.activeId;
  await c.views.openDialog("duplicate"); c.controls["view-name"].value = "Review copy";
  await c.controls["view-form"].onsubmit({preventDefault() {}});
  const duplicate = c.views.activeId;
  assert.notEqual(original, duplicate);
  assert.ok(c.records.every(view => view.cwd === "/opt/foo" && view.filters.phase === "validation"));
  await c.views.setProject("/opt/bar");
  assert.deepEqual(c.views.items.map(view => view.id), ["all"]);
  await c.views.openDialog("create"); c.controls["view-name"].value = "Review";
  await c.controls["view-form"].onsubmit({preventDefault() {}});
  const bar = c.views.activeId;
  assert.equal(c.records.at(-1).cwd, "/opt/bar");
  const reopened = setup({saved: c.records, preferences: c.preferences});
  await reopened.views.setProject("/opt/foo");
  assert.deepEqual(reopened.views.items.map(view => view.id), ["all", original, duplicate]);
  assert.equal(reopened.views.activeId, duplicate);
  await reopened.views.setProject("/opt/bar");
  assert.deepEqual(reopened.views.items.map(view => view.id), ["all", bar]);
  assert.equal(reopened.views.activeId, bar);
  assert.deepEqual(c.errors, []);
});

for (const mode of ["create", "duplicate"]) {
  test(`a delayed ${mode} stays in its original workspace when another workspace opens`, async () => {
    const c = setup({saved: [{id: "foo", name: "Review", cwd: "/opt/foo", filters: {phase: "validation"}}]});
    await c.views.setProject("/opt/foo"); await c.views.activate("foo");
    await c.views.openDialog(mode); c.controls["view-name"].value = "New review";
    const api = c.views.api;
    let release;
    c.views.api = async (path, body) => {
      if (path === "views" && body) await new Promise(resolve => { release = resolve; });
      return api(path, body);
    };
    const saving = c.controls["view-form"].onsubmit({preventDefault() {}});
    await Promise.resolve();
    await c.views.setProject("/opt/bar");
    c.controls.search.value = "Bar search";
    release(); await saving;
    const created = c.records.at(-1);
    assert.equal(created.cwd, "/opt/foo");
    assert.equal(created.filters.phase, "validation");
    assert.deepEqual(c.views.items.map(view => view.id), ["all"]);
    assert.equal(c.views.activeId, "all");
    assert.equal(c.controls.search.value, "Bar search");
    await c.views.setProject("/opt/foo");
    assert.deepEqual(c.views.items.map(view => view.id), ["all", "foo", created.id]);
    assert.deepEqual(c.errors, []);
  });
}

test("a create dialog cannot follow an in-flight workspace switch", async () => {
  const c = setup();
  await c.views.setProject("/opt/foo");
  // Both workspaces have All conversations selected; the view ID alone cannot guard this race.
  const switching = c.views.setProject("/opt/bar");
  const opening = c.views.openDialog("create");
  await Promise.all([switching, opening]);
  assert.equal(c.controls["view-dialog"].open, undefined);
  assert.deepEqual(c.records, []);
});

test("desktop tabs switch filters by click and keyboard and disappear without saved views", async () => {
  const c = setup({saved: [{id: "review", name: "Review", filters: {phase: "validation"}},
    {id: "build", name: "Build", filters: {phase: "implementation"}}]});
  await c.views.start();
  const tabs = c.controls["saved-view-tabs"];
  assert.equal(tabs.hidden, false);
  assert.deepEqual(tabs.children.map(tab => tab.tabIndex), [0, -1]);
  await tabs.children[0].onclick();
  assert.equal(c.controls["phase-filter"].value, "validation");
  assert.deepEqual(tabs.children.map(tab => tab.tabIndex), [0, -1]);
  for (const [key, expected] of [["ArrowRight", "build"], ["End", "build"], ["Home", "review"], ["ArrowLeft", "build"]]) {
    let prevented = false;
    await tabs.onkeydown({key, preventDefault() { prevented = true; }});
    assert.equal(prevented, true); assert.equal(c.views.activeId, expected);
    assert.equal(c.doc.activeElement.dataset.viewId, expected);
  }
  await c.controls["view-delete"].onclick();
  await tabs.children[0].onclick();
  await c.controls["view-delete"].onclick();
  assert.equal(tabs.hidden, true); assert.equal(c.views.activeId, "all");
  assert.equal(c.doc.activeElement, c.controls["show-now"]);
  assert.deepEqual(c.errors, []);
});


test("built-in view navigation leaves saved filters intact and keeps Closed selected", async () => {
  const c = setup({saved: [{id: "review", name: "Review", filters: {phase: "validation"}}]});
  await c.views.start(); await c.views.activate("review");
  assert.equal(c.controls["show-now"].attributes["aria-pressed"], "false");
  for (const [id, show] of [["show-later", "later"], ["show-now", "active"], ["show-closed", "archived"], ["show-closed", "archived"]]) {
    await c.controls[id].onclick();
    assert.equal(c.views.activeId, "all");
    assert.equal(c.controls.view.value, show);
    assert.equal(c.controls["phase-filter"].value, "");
    assert.equal(c.controls[id].attributes["aria-pressed"], "true");
    assert.equal(c.doc.activeElement, c.controls[id]);
  }
  assert.equal(c.records[0].filters.phase, "validation");
  assert.equal(c.records[0].filters.show, "active");
  assert.ok(c.calls.every(call => !call.path.endsWith("/update")));
  await c.views.activate("review");
  assert.equal(c.controls["phase-filter"].value, "validation");
  assert.equal(c.controls["show-closed"].attributes["aria-pressed"], "false");
});
