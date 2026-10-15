"use strict";
const {test} = require("node:test"), assert = require("node:assert/strict");
const {plan, create} = require("../plugins/agent-coord/scripts/agent_coord/web/view-drops.js");
const projects = [{id: "old", name: "Original", cwd: "/work"}, {id: "group", name: "Release", cwd: "/work"}];
const thread = (extra = {}) => ({thread_id: "card", title: "Ship it", project_cwd: "/work", project_id: "old",
  attention: "now", work_phase: "implementation", needs_attention: false, ...extra});
const view = (filters = {}, extra = {}) => ({id: "release", name: "Release", cwd: "/work", version: 1,
  group_by: "phase", filters: {project: "group", ...filters}, ...extra});

test("drop plans assign or clear a group without rewriting the thread or view", () => {
  const card = thread(), target = view(), before = JSON.stringify([card, target]);
  assert.deepEqual(plan(card, target, projects), {allowed: true, projectId: "group", message: "Assign to Release"});
  assert.deepEqual(plan(card, view({project: "__none__"}), projects), {allowed: true, projectId: null, message: "Remove from group"});
  assert.equal(JSON.stringify([card, target]), before);
  for (const group_by of ["phase", "domain", "project", "none"]) {
    assert.equal(plan(card, view({}, {group_by}), projects).allowed, true);
  }
});

test("combined filters must match after group assignment", () => {
  for (const [values, filters, allowed] of [
    [{}, {phase: "implementation"}, true], [{}, {phase: "validation"}, false],
    [{}, {show: "attention"}, false], [{needs_attention: true}, {show: "attention"}, true],
    [{}, {show: "completed"}, false], [{work_phase: "finished"}, {show: "completed"}, true],
    [{}, {show: "later"}, false], [{attention: "later"}, {show: "later"}, true],
    [{attention: "later"}, {}, false],
    [{repository_id: "repo"}, {repository: "repo"}, true],
    [{repository_id: "repo"}, {repository: "other"}, false],
    [{repository_id: null}, {repository: "__none__"}, true],
  ]) assert.equal(plan(thread(values), view(filters), projects).allowed, allowed, JSON.stringify([values, filters]));
});

test("ambiguous, stale, closed, and unchanged destinations explain why they reject a card", () => {
  for (const [card, target, groups, reason] of [
    [thread(), view({project: ""}), projects, /no group filter/],
    [thread(), view({search: "Ship"}), projects, /search/],
    [thread(), view({show: "archived"}), projects, /closed/],
    [thread({attention: "archived"}), view(), projects, /closed/],
    [thread(), view({}, {cwd: "/elsewhere"}), projects, /workspace/],
    [thread(), view(), [{id: "group", name: "Other", cwd: "/elsewhere"}], /workspace/],
    [thread(), view(), [], /no longer available/],
    [thread(), view({}, {pending: true}), projects, /saving/],
    [thread({project_id: "group"}), view(), projects, /already/],
    [thread({project_id: null}), view({project: "__none__"}), projects, /already/],
    [null, view(), projects, /no longer available/],
    [thread(), null, projects, /no longer available/],
  ]) {
    const result = plan(card, target, groups);
    assert.equal(result.allowed, false); assert.match(result.message, reason);
  }
});

function setup() {
  const c = {local: thread(), remote: thread(), localViews: [view(), view({phase: "validation"}, {id: "review"})],
    remoteViews: [view(), view({phase: "validation"}, {id: "review"})], projects: structuredClone(projects),
    calls: [], errors: [], notices: [], changes: []};
  const listeners = {};
  class Element {
    constructor(dataset = {}) {
      this.dataset = dataset; this.classes = new Set(); this.children = [];
      this.classList = {remove: key => this.classes.delete(key),
        toggle: (key, value) => value ? this.classes.add(key) : this.classes.delete(key)};
    }
    setAttribute() {}
    append(item) { this.children.push(item); }
    closest(selector) {
      return selector === "[data-drag-thread]" ? this.dataset.dragThread ? this : null
        : this.dataset.viewId ? this : null;
    }
  }
  c.tabs = c.localViews.map(v => new Element({viewId: v.id}));
  c.source = new Element({dragThread: "card"});
  const doc = {body: new Element(), createElement: () => new Element(), querySelectorAll: () => c.tabs,
    addEventListener: (type, fn) => { listeners[type] = fn; }};
  c.request = async (path, body) => {
    c.calls.push({path, body});
    if (path === "views") return {data: structuredClone(c.remoteViews)};
    if (path === "organization") return {projects: structuredClone(c.projects)};
    assert.equal(path, "threads/card");
    if (body) {
      if (c.failSave) throw new Error("Offline");
      assert.deepEqual(Object.keys(body), ["project_id"]);
      c.remote = {...c.remote, ...body};
    }
    return {...c.remote};
  };
  c.ui = create({document: doc, getThread: id => id === "card" ? c.local : null,
    getViews: () => c.localViews, getProjects: () => c.projects, api: (...args) => c.request(...args),
    onChanged: async record => { c.local = record; c.changes.push(record); },
    notify: (key, message, undo) => c.notices.push({key, message, undo}), onError: error => c.errors.push(error)});
  c.hint = doc.body.children[0];
  c.event = target => ({target, dataTransfer: {setData(type, id) { this.type = type; this.id = id; }},
    preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }});
  c.emit = (type, target) => { const event = c.event(target); return {event, result: listeners[type](event)}; };
  c.writes = () => c.calls.filter(call => call.body);
  return c;
}

test("native drag highlights valid tabs, explains rejected tabs, and assigns on drop", async () => {
  const c = setup(), start = c.emit("dragstart", c.source).event;
  assert.equal(start.dataTransfer.id, "card"); assert.equal(start.dataTransfer.effectAllowed, "move");
  assert.equal(c.tabs[0].classes.has("view-drop-allowed"), true);
  assert.equal(c.tabs[1].classes.has("view-drop-blocked"), true);
  const rejected = c.emit("dragover", c.tabs[1]).event;
  assert.equal(rejected.prevented, undefined); assert.equal(rejected.dataTransfer.dropEffect, "none");
  assert.match(c.hint.textContent, /stage filter/);
  const accepted = c.emit("dragover", c.tabs[0]).event;
  assert.equal(accepted.prevented, true); assert.equal(accepted.dataTransfer.dropEffect, "move");
  assert.equal(c.hint.textContent, "Assign to Release");
  assert.equal(c.tabs[0].classes.has("view-drop-over"), true);
  await c.emit("drop", c.tabs[0]).result;
  assert.equal(c.hint.hidden, true); assert.ok(c.tabs.every(tab => !tab.classes.size));
  assert.deepEqual(c.writes(), [{path: "threads/card", body: {project_id: "group"}}]);
  assert.equal(c.changes.length, 1); assert.equal(c.remote.work_phase, "implementation");
  assert.equal(c.remote.attention, "now"); assert.equal(c.errors.length, 0);
  await c.notices[0].undo();
  assert.equal(c.remote.project_id, "old");
});

test("canceling a drag, dropping outside tabs, and external drags never assign groups", async () => {
  const c = setup();
  await c.emit("drop", c.tabs[0]).result;
  c.emit("dragstart", c.source); c.emit("dragend", c.source);
  await c.emit("drop", c.tabs[0]).result;
  c.emit("dragstart", c.source); await c.emit("drop", c.source).result;
  assert.equal(c.calls.length, 0); assert.equal(c.hint.hidden, true);
  assert.ok(c.tabs.every(tab => !tab.classes.size));
});

test("replaced tabs and filters are evaluated during a drag", () => {
  const c = setup(); c.emit("dragstart", c.source);
  c.localViews[0] = view({phase: "validation"});
  c.emit("dragover", c.tabs[0]);
  assert.equal(c.tabs[0].classes.has("view-drop-allowed"), false);
  assert.match(c.hint.textContent, /stage filter/);
});

test("drop rechecks remote thread state, deleted groups, and changed view definitions before writing", async () => {
  for (const mutate of [
    c => { c.remote.attention = "later"; },
    c => { c.remoteViews[0].version++; },
    c => { c.remoteViews[0].filters.phase = "validation"; },
    c => { c.remoteViews = []; },
    c => { c.projects = []; },
  ]) {
    const c = setup(), request = c.request;
    c.request = async (...args) => { mutate(c); return request(...args); };
    await assert.rejects(c.ui.assign("card", "release"));
    assert.equal(c.writes().length, 0); assert.equal(c.notices.length, 0);
  }
});

test("duplicate drops are coalesced and failed assignments can be retried", async () => {
  const c = setup(), request = c.request;
  let finish;
  c.request = (...args) => args[0] === "views" ? new Promise(resolve => { finish = () => resolve({data: c.remoteViews}); }) : request(...args);
  const pending = c.ui.assign("card", "release");
  await c.ui.assign("card", "release");
  finish(); await pending;
  assert.equal(c.writes().length, 1);
  const failed = setup(); failed.failSave = true;
  await assert.rejects(failed.ui.assign("card", "release"), /Offline/);
  assert.equal(failed.remote.project_id, "old"); assert.equal(failed.notices.length, 0);
  failed.failSave = false; await failed.ui.assign("card", "release");
  assert.equal(failed.remote.project_id, "group");
});

test("Undo restores the actual previous group, including unassigned, but preserves later decisions", async () => {
  for (const previous of [null, "different"]) {
    const c = setup(); c.remote.project_id = previous;
    await c.ui.assign("card", "release"); await c.notices[0].undo();
    assert.equal(c.remote.project_id, previous);
  }
  const c = setup(); await c.ui.assign("card", "release");
  c.remote.project_id = "new-decision";
  await c.notices[0].undo();
  assert.equal(c.remote.project_id, "new-decision"); assert.equal(c.writes().length, 1);
});

test("No group drops clear the association and Undo can recover from a network failure", async () => {
  const c = setup(); c.localViews[0] = c.remoteViews[0] = view({project: "__none__"});
  await c.ui.assign("card", "release"); assert.equal(c.remote.project_id, null);
  c.failSave = true; await assert.rejects(c.notices[0].undo(), /Offline/);
  c.failSave = false; await c.notices[0].undo(); assert.equal(c.remote.project_id, "old");
});

test("direct group drops ignore display filters and preserve placement, with stale membership checks and Undo", async () => {
  const target = view({phase: "validation", search: "unrelated", show: "archived"},
    {id: "group-group", kind: "group", projectId: "group"});
  for (const attention of ["now", "later"]) assert.equal(plan(thread({attention}), target, projects).allowed, true);
  assert.equal(plan(thread({attention: "archived"}), target, projects).allowed, false);
  const c = setup(); c.localViews = [target]; c.remote.attention = "later";
  await c.ui.assign("card", target.id);
  assert.equal(c.remote.project_id, "group"); assert.equal(c.remote.attention, "later");
  assert.equal(c.calls.some(call => call.path === "views"), false);
  await c.notices[0].undo(); assert.equal(c.remote.project_id, "old");
  const stale = setup(); stale.localViews = [target];
  const request = stale.request;
  stale.request = (path, body) => path === "organization" ? {projects: []} : request(path, body);
  await assert.rejects(stale.ui.assign("card", target.id), /no longer available/);
  assert.equal(stale.writes().length, 0);
});
