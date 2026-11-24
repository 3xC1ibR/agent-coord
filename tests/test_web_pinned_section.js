"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {threadListFixture} = require("./web_application_fixture.js");
const grouping = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const {threadViews} = require("../plugins/agent-coord/scripts/agent_coord/web/views.js");

function thread(id, extra = {}) {
  return {thread_id: id, title: id, attention: "now", status: "idle", work_phase: "implementation",
    original_request: "Inspect the result.", checkpoint: {id: 1, created_at: 100}, ...extra};
}
function render(threads, placement = "active", search = "") {
  const view = threadListFixture({now: () => 120});
  const visible = threads.filter(item => threadViews.matches(item, {show: placement, search}));
  view.render(visible, {placement});
  return view;
}
const inSection = (view, name) => view.section(name) ? view.rows(view.section(name)) : [];

test("pinned replies appear once while attention badges and sidebar navigation remain available", () => {
  const threads = [thread("pin", {pinned: true}),
    thread("pinned-reply", {pinned: true, response_state: "reply", needs_attention: true, unhandled_response: true, unread_result: true}),
    thread("reply", {response_state: "reply", needs_attention: true, unhandled_response: true, unread_result: true})];
  for (const placement of ["active", "attention"]) {
    const view = render(threads, placement);
    assert.equal(view.target.children[0].dataset.section, "pinned");
    assert.deepEqual(inSection(view, "pinned").sort(),
      placement === "active" ? ["pin", "pinned-reply"] : ["pinned-reply"]);
    assert.deepEqual(inSection(view, "attention"), ["reply"]);
    assert.equal(new Set(view.rows()).size, view.rows().length);
    assert.deepEqual(grouping.groupThreads(threads).priority.map(item => item.thread_id).sort(), ["pinned-reply", "reply"]);
    assert.equal(threadViews.badges(threads, {}).attention, 2);
  }
  threads.forEach(item => { item.pinned = false; });
  const unpinned = render(threads);
  assert.equal(unpinned.section("pinned"), undefined);
  assert.deepEqual(inSection(unpinned, "attention").sort(), ["pinned-reply", "reply"]);
});

test("pinning every Attention thread keeps each thread accessible in Pinned", () => {
  for (const response_state of ["input", "action", "failed", "reply"]) {
    const threads = [thread("pinned", {pinned: true, response_state, needs_attention: true, unread_result: true})];
    const view = render(threads);
    assert.deepEqual(inSection(view, "pinned"), ["pinned"]);
    assert.deepEqual(inSection(view, "attention"), []);
    assert.equal(view.control("pinned", "open").textContent, "pinned");
    assert.equal(threadViews.badges(threads, {}).attention, 1);
    assert.equal(threads[0].needs_attention, true);
  }
});

test("pinning and unpinning never duplicate or lose a conversation across work phases", () => {
  for (const work_phase of ["new", "investigation", "planning", "orchestrating", "implementation", "validation", "deployment", "finished"]) {
    const threads = [thread("pin", {pinned: true, work_phase}), thread("other", {work_phase})];
    const pinned = render(threads);
    assert.deepEqual(inSection(pinned, "pinned"), ["pin"]);
    assert.deepEqual(pinned.rows().sort(), ["other", "pin"]);
    assert.equal(threads[0].work_phase, work_phase);
    threads[0].pinned = false;
    assert.deepEqual(render(threads).rows().sort(), ["other", "pin"]);
  }
});

test("a pinned Later reply stays reachable while a snoozed pin stays in Later", () => {
  const threads = [
    thread("now", {pinned: true, response_state: "reply", needs_attention: true, unhandled_response: true}),
    thread("later", {pinned: true, attention: "later", response_state: "reply", needs_attention: true, unhandled_response: true}),
    thread("sleep", {pinned: true, attention: "later", snoozed: true}),
  ];
  const active = render(threads);
  assert.deepEqual(inSection(active, "pinned").sort(), ["later", "now"]);
  assert.ok(!active.rows().includes("sleep"));
  assert.deepEqual(inSection(render(threads, "later"), "parked").sort(), ["later", "sleep"]);
});

test("Pinned obeys search filters without keeping hidden pins in the overview", () => {
  const threads = [thread("keep", {pinned: true}), thread("other")];
  assert.deepEqual(inSection(render(threads, "active", "keep"), "pinned"), ["keep"]);
  assert.equal(render(threads, "active", "missing").section("pinned"), undefined);
});
