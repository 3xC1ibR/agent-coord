"use strict";
const {test} = require("node:test"), assert = require("node:assert/strict");
const {SavedViews} = require("../plugins/agent-coord/scripts/agent_coord/web/views.js");

function setup(groups) {
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
  const api = async (path, body) => {
    calls.push({path, body});
    const group = groups.find(group => group.id === path.split("/")[1]);
    if (path.startsWith("projects/")) {
      if (body) { groups.splice(groups.indexOf(group), 1); return {...group}; }
      return {...group, thread_count: group.thread_count || 0};
    }
    if (path === "organization") return {projects: groups};
    return {data: []};
  };
  const views = new SavedViews({document: doc, api, onSwitch: async () => {}, onRefresh: async () => {}, onError: error => errors.push(error)});
  views.groups = groups;
  // The projection also works independently of the larger group navigation UI.
  views.rebuild ||= () => { views.items = [views.all, ...views.groupItems()]; };
  views.rebuild(); views.render();
  return {views, $, doc, calls, errors};
}

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
