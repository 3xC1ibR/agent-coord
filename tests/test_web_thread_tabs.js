"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {ThreadTabs} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-tabs.js");
const thread = id => ({thread_id: id, title: "Thread " + id, attention: "now", response_state: "working"});

function setup(storage = new Map()) {
  const visits = [], errors = [];
  const tabs = new ThreadTabs({storage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    onSelect: id => { visits.push(id); if (id) tabs.open(thread(id)); else tabs.overview(); }, onError: error => errors.push(error)});
  tabs.setProject("/work");
  return {tabs, visits, errors, storage, ids: () => tabs.items.map(item => item.thread_id)};
}

test("overview and repeated opens preserve the working set and user order", async () => {
  const {tabs, visits, ids} = setup();
  tabs.open(thread("a")); tabs.open(thread("b")); tabs.open(thread("c"));
  tabs.move("c", 0);
  await tabs.activate(null);
  assert.equal(tabs.selected, null);
  assert.deepEqual(ids(), ["c", "a", "b"]);
  await tabs.activate("a"); await tabs.activate("a");
  assert.deepEqual(visits, [null, "a"]);
  assert.deepEqual(ids(), ["c", "a", "b"]);
});

test("closing tabs only navigates, preserves thread lifecycle, and returns to overview after the last", async () => {
  const {tabs, visits, ids} = setup(), a = thread("a"), b = thread("b"), c = thread("c");
  tabs.open(a); tabs.open(b); tabs.open(c); tabs.open(b);
  await tabs.close("a");
  assert.deepEqual(visits, []);
  assert.equal(tabs.selected, "b");
  await tabs.close("b");
  assert.deepEqual(ids(), ["c"]); assert.equal(tabs.selected, "c");
  await tabs.close("c");
  assert.deepEqual(visits, ["c", null]); assert.deepEqual(ids(), []);
  assert.deepEqual([a, b, c], [thread("a"), thread("b"), thread("c")]);
  await tabs.close(null); assert.deepEqual(visits, ["c", null]);
});

test("project changes and reload retain tab order without automatically opening conversations", () => {
  const {tabs, storage, ids, visits} = setup();
  tabs.open(thread("a")); tabs.open(thread("b")); tabs.move("b", 0);
  tabs.setProject("/other"); tabs.open(thread("other"));
  tabs.setProject("/work"); assert.deepEqual(ids(), ["b", "a"]); assert.equal(tabs.selected, null);
  const restored = setup(storage);
  assert.deepEqual(restored.ids(), ["b", "a"]); assert.deepEqual(visits, []);
  assert.equal(restored.tabs.selected, null);
});

test("inventory updates and filters do not remove, reorder, or open tabs", () => {
  const {tabs, ids} = setup();
  tabs.open(thread("a")); tabs.open(thread("b")); tabs.overview();
  tabs.update([{...thread("a"), title: "Renamed", attention: "later"}, thread("extra")]);
  assert.deepEqual(ids(), ["a", "b"]); assert.equal(tabs.selected, null);
  assert.equal(tabs.items[0].title, "Renamed");
  tabs.update([]); assert.deepEqual(ids(), ["a", "b"]);
});

test("a submitted draft keeps its tab position and cannot duplicate the created thread", () => {
  const {tabs, ids, storage} = setup();
  tabs.open(thread("a")); tabs.open(thread("new-session")); tabs.open(thread("b"));
  tabs.open(thread("new-session"));
  assert.deepEqual(setup(storage).ids(), ["a", "b"]);
  tabs.replace("new-session", thread("created"));
  assert.deepEqual(ids(), ["a", "created", "b"]); assert.equal(tabs.selected, "created");
});

test("navigation guards protect drafts during sending and failures remain visible", async () => {
  const {tabs, ids, visits, errors} = setup(); tabs.open(thread("a")); tabs.open(thread("b"));
  tabs.canNavigate = () => false;
  await tabs.close("b"); await tabs.activate("a");
  assert.deepEqual(ids(), ["a", "b"]); assert.deepEqual(visits, []);
  tabs.canNavigate = () => true;
  tabs.onSelect = async () => { throw new Error("Unavailable"); };
  await tabs.activate("a"); assert.equal(errors[0].message, "Unavailable");
});

test("keyboard reordering cannot move Overview and ignores composition", () => {
  const {tabs, ids} = setup(); tabs.open(thread("a")); tabs.open(thread("b"));
  const event = {key: "ArrowLeft", altKey: true, shiftKey: true, preventDefault() {}};
  tabs.keydown({...event, isComposing: true}, "b"); assert.deepEqual(ids(), ["a", "b"]);
  tabs.keydown(event, "b"); assert.deepEqual(ids(), ["b", "a"]);
  tabs.keydown(event, null); assert.deepEqual(ids(), ["b", "a"]);
});

test("unavailable or malformed storage does not prevent opening conversations", () => {
  for (const stored of ["null", "{}", "invalid", '[null,{}, {"thread_id":"a"},{"thread_id":"a"}]']) {
    const {tabs} = setup(new Map([["agent-coord.thread-tabs.:/work", stored]]));
    tabs.open(thread("b")); assert.equal(tabs.selected, "b");
    assert.equal(new Set(tabs.items.map(item => item.thread_id)).size, tabs.items.length);
  }
});
