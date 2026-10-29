"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const WorkingSet = require("../plugins/agent-coord/scripts/agent_coord/web/working-set.js");
const thread = (id, extra = {}) => ({thread_id: id, title: "Thread " + id, attention: "now", cwd: "/work", project_cwd: "/work",
  work_phase: "investigation", status: "idle", response_state: "available", created_at: 100, updated_at: 200, ...extra});
const classified = choice => ({status: "classified", choice});

function setup({data = new Map(), broken = false, onGroupChange} = {}) {
  const doc = {events: {}, activeElement: null, modal: false,
    addEventListener(k, f) { this.events[k] = f; }, removeEventListener(k) { delete this.events[k]; }};
  class Element {
    constructor(tag) { Object.assign(this, {tagName: tag, children: [], attrs: {}, dataset: {}, style: {}, events: {}, className: "",
      isConnected: true, clientWidth: 1100, clientHeight: 700, scrollHeight: 700, scrollTop: 0, scrollLeft: 0}); }
    get classList() { const read = () => new Set(this.className.split(/\s+/).filter(Boolean)); return {
      contains: n => read().has(n), add: n => { const s = read(); s.add(n); this.className = [...s].join(" "); },
      toggle: (n, value) => { const s = read(); if (value ?? !s.has(n)) s.add(n); else s.delete(n); this.className = [...s].join(" "); }}; }
    append(...items) { for (const item of items) { this.children.push(item); if (typeof item === "object") item.parent = this; } }
    replaceChildren(...items) { this.children.forEach(n => { if (typeof n === "object") n.parent = null; }); this.children = []; this.append(...items); }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k] ?? null; }
    matches(selector) {
      if (selector.includes(",")) return selector.split(",").some(s => this.matches(s.trim()));
      const attr = selector.match(/\[([^=\]]+)(?:="([^"]*)")?\]/);
      if (attr) {
        const value = attr[1].startsWith("data-") ? this.dataset[attr[1].slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] : this.attrs[attr[1]];
        if (value == null || attr[2] != null && value !== attr[2]) return false;
        selector = selector.replace(attr[0], "");
      }
      return !selector || selector.startsWith(".") ? !selector || this.classList.contains(selector.slice(1)) : this.tagName === selector;
    }
    querySelectorAll(s) { return this.children.filter(n => typeof n === "object").flatMap(n => [...(n.matches(s) ? [n] : []), ...n.querySelectorAll(s)]); }
    querySelector(s) { return this.querySelectorAll(s)[0] || null; }
    closest(s) { return this.matches(s) ? this : this.parent?.closest(s) || null; }
    contains(n) { return this === n || this.children.some(c => typeof c === "object" && c.contains(n)); }
    addEventListener(k, f) { (this.events[k] ||= []).push(f); }
    emit(k, event) { for (const f of this.events[k] || []) f(event); }
    focus() { doc.activeElement = this; }
  }
  doc.createElement = tag => new Element(tag); doc.body = new Element("body");
  doc.querySelector = s => doc.modal ? {} : doc.body.querySelector(s);
  const observers = [], timers = new Map(); let timestamp = 1000, counter = 0, enabled = true;
  const win = {performance: {now: () => timestamp}, setTimeout: f => { timers.set(++counter, f); return counter; }, clearTimeout: n => timers.delete(n),
    ResizeObserver: class { constructor(f) { this.callback = f; observers.push(this); } observe() {} disconnect() { this.disconnected = true; } }};
  const host = new Element("section"); host.hidden = false; doc.body.append(host);
  const opened = [], read = [], dismissed = [], rendered = [];
  const ui = WorkingSet.create({document: doc, window: win,
    storage: {getItem(k) { if (broken) throw Error(); return data.get(k); }, setItem(k, v) { if (broken) throw Error(); data.set(k, v); }},
    isActive: () => enabled,
    renderCard(t, needs) {
      const card = new Element("div"); card.className = "thread-card";
      const b = new Element("button"); b.className = "session"; b.dataset.thread = t.thread_id; b.textContent = t.title; b.onclick = () => opened.push(t.thread_id);
      card.append(b); rendered.push([t.thread_id, needs]); return card;
    },
    onOpen: id => opened.push(id), onRead: t => read.push(t.thread_id), onDismiss: t => dismissed.push(t.thread_id), onGroupChange});
  const render = (threads, options = {}) => ui.render(threads, host, {workspace: "/work", ...options});
  const key = (value, target = doc.body, extra = {}) => { const e = {key: value, target, preventDefault() { this.prevented = true; }, ...extra}; doc.events.keydown(e); return e; };
  const page = () => host.querySelectorAll(".ws-page").find(p => p.attrs["aria-hidden"] === "false");
  const ids = () => page().querySelectorAll(".ws-card").map(n => n.dataset.cardThread);
  const selectLevel = n => host.querySelectorAll(".ws-level")[n].onclick();
  const selectGroup = key => host.querySelectorAll(".ws-cell").find(cell => cell.title.startsWith(key + " · ") && cell.dataset.level === page().dataset.level).onclick();
  const event = extra => ({target: page(), deltaX: 0, deltaY: 0, preventDefault() { this.prevented = true; }, ...extra});
  return {ui, doc, host, data, observers, rendered, opened, read, dismissed, render, key, page, ids, selectLevel, selectGroup, event,
    advance: n => { timestamp += n; }, enable: value => { enabled = value; }};
}

test("state membership separates unread findings from actual requests, ignores stale asks and preserves Later", () => {
  const input = thread("ask", {checkpoint: {next_actor: "user", next_action: "Choose a name"}, unread_result: true});
  const list = [thread("run", {status: "running", response_state: "working", unread_result: true}), input,
    thread("approval", {status: "needs input", response_state: "input"}), thread("findings", {response_state: "update", needs_attention: true, unread_result: true}),
    thread("checkpoint", {unread: true}), thread("stale", {...input, thread_id: "stale", checkpoint_stale: true}),
    thread("parked", {attention: "later", unread_result: true}), thread("snoozed", {snoozed: true, unread_result: true}),
    thread("later-run", {attention: "later", response_state: "working"}), thread("closed", {attention: "archived", status: "running"}),
    thread("failure", {response_state: "failed", unread_result: true})];
  assert.deepEqual(list.map(WorkingSet.levelOf), [0, 0, 0, 1, 2, 1, 2, 2, 0, 2, 0]);
  const before = JSON.stringify(list), groups = WorkingSet.derive(list);
  assert.deepEqual(groups[0].levels.flat().map(t => t.thread_id).sort(), list.map(t => t.thread_id).sort());
  assert.equal(JSON.stringify(list), before);
});
test("group IDs distinguish equal names, children inherit root groups, missing parents and cycles remain navigable", () => {
  const list = [thread("root", {project_id: "one", project_name: "Launch"}),
    thread("child", {creation_mode: "orchestration", created_by_thread_id: "root"}),
    thread("explicit", {creation_mode: "orchestration", created_by_thread_id: "root", project_id: "two", project_name: "Launch"}),
    thread("orphan", {creation_mode: "orchestration", created_by_thread_id: "missing"}),
    thread("a", {creation_mode: "orchestration", created_by_thread_id: "b"}), thread("b", {creation_mode: "orchestration", created_by_thread_id: "a"})];
  const groups = WorkingSet.derive(list);
  assert.deepEqual(groups.find(g => g.key === "group:one").levels.flat().map(t => t.thread_id).sort(), ["child", "root"]);
  assert.equal(groups.find(g => g.key === "group:two").levels.flat().length, 1);
  assert.equal(groups.slice(1).flatMap(g => g.levels.flat()).length, list.length);
});
test("child findings stay in Attention after reading until dismissed", () => {
  const c = setup(), parent = thread("parent", {project_id: "evaluations", project_name: "Evaluations"});
  const child = thread("child", {creation_mode: "orchestration", created_by_thread_id: "parent", response_state: "update",
    attention_reason: "findings", needs_attention: true, unread_result: true, unhandled_response: true});
  c.render([parent, child]);
  assert.deepEqual(c.ids(), ["child"]);
  c.selectLevel(1); assert.deepEqual(c.ids(), ["child"]); c.selectLevel(0);
  c.selectGroup("Evaluations"); assert.deepEqual(c.ids(), ["child"]);
  c.render([parent, {...child, unread_result: false}]); assert.deepEqual(c.ids(), ["child"]);
  c.selectLevel(1); assert.deepEqual(c.ids(), []); c.selectLevel(0);
  c.page().querySelector('[data-action="dismiss"]').onclick({stopPropagation() {}}); assert.deepEqual(c.dismissed, ["child"]);
  c.render([parent, {...child, unread_result: false, unhandled_response: false, needs_attention: false}]);
  assert.deepEqual(c.ids(), []); c.selectLevel(2); assert.deepEqual(c.ids().sort(), ["child", "parent"]);
});
test("attention sorts all classifications by recent activity and keeps read responses until dismissal", () => {
  const c = setup(), items = [
    thread("old-blocker", {status: "needs input", turn_started_at: 100}),
    thread("new-findings", {unhandled_response: true, unread_result: true, response_classification: classified("findings"), turn_completion: {completed_at: 300}}),
    thread("middle-review", {unhandled_response: true, response_classification: classified("review"), turn_completion: {completed_at: 200}}),
    thread("pinned-old", {pinned: true, unhandled_response: true, response_classification: classified("done"), turn_completion: {completed_at: 150}})];
  c.render(items); assert.deepEqual(c.ids(), ["new-findings", "middle-review", "pinned-old", "old-blocker"]);
  c.page().querySelector('[data-action="read"]').onclick(); assert.deepEqual(c.read, ["new-findings"]);
  c.render(items.map(t => t.thread_id === "new-findings" ? {...t, unread_result: false} : t));
  assert.deepEqual(c.ids(), ["new-findings", "middle-review", "pinned-old", "old-blocker"]);
  c.page().querySelector('[data-action="dismiss"]').onclick({stopPropagation() {}}); assert.deepEqual(c.dismissed, ["new-findings"]);
});
test("empty attention columns remain visible, and running work takes precedence over prior input", () => {
  const c = setup(); c.render([]);
  assert(c.page().querySelector(".ws-empty"));
  assert.equal(WorkingSet.columnOf(thread("resumed", {response_state: "working", checkpoint: {next_actor: "user", next_action: "Old question"}})), "working");
  assert.equal(WorkingSet.columnOf(thread("handled", {work_phase: "finished", unhandled_response: false, unread_result: true})), null);
  assert.equal(WorkingSet.columnOf(thread("classified-done", {response_classification: classified("done"), unhandled_response: true, unread_result: false})), "working");
});
test("unhandled responses stay in Attention across classifications and phases", () => {
  const unread = {unread_result: true, unhandled_response: true};
  for (const phase of ["investigation", "planning", "implementation", "deployment", "finished"]) {
    const answer = thread("answer", {...unread, work_phase: phase, response_classification: classified("findings")});
    assert.equal(WorkingSet.levelOf(answer), 0, phase);
    assert.equal(WorkingSet.levelOf({...answer, unread_result: false}), 0, phase);
    const delivery = {...answer, response_classification: classified("done")};
    assert.equal(WorkingSet.levelOf(delivery), 0, phase);
    assert.equal(WorkingSet.levelOf({...delivery, unread_result: false}), 0, phase);
    assert.equal(WorkingSet.levelOf({...delivery, unhandled_response: false}), 2, phase);
    const update = {...answer, response_classification: classified("update")};
    assert.equal(WorkingSet.columnOf(update), "working", phase);
    assert.equal(WorkingSet.levelOf({...update, unread_result: false}), 0, phase);
  }
  for (const status of ["pending", "uncertain", "unavailable"]) {
    const unknown = thread("unknown", {...unread, response_classification: {status, choice: "done"}});
    assert.equal(WorkingSet.levelOf(unknown), 0, status);
    assert.equal(WorkingSet.levelOf({...unknown, work_phase: "finished"}), 0, status);
    assert.equal(WorkingSet.levelOf({...unknown, work_phase: "finished", checkpoint_stale: true}), 0, status);
  }
  assert.equal(WorkingSet.levelOf(thread("missing", unread)), 0);
});
test("classified blockers and required reviews stay above ordinary replies until handled", () => {
  const c = setup();
  for (const choice of ["blocked", "review"]) {
    const t = thread(choice, {unread_result: true, unhandled_response: true, response_classification: classified(choice)});
    c.render([t]); c.selectLevel(0);
    assert.deepEqual(c.ids(), [choice]);
    assert.equal(c.page().querySelector('[data-section="attention"]').querySelector(".ws-card").dataset.cardThread, choice);
    c.render([{...t, unread_result: false}]); assert.deepEqual(c.ids(), [choice]);
    c.render([{...t, unread_result: false, unhandled_response: false}]); assert.deepEqual(c.ids(), []);
    c.selectLevel(2); assert.deepEqual(c.ids(), [choice]);
  }
  const request = thread("permission", {status: "needs input", response_state: "input", unhandled_response: false,
    response_classification: classified("findings")});
  assert.equal(WorkingSet.columnOf(request), "blocked");
});
test("dismissed saved requests leave Focus attention and its map until new input is needed", () => {
  for (const reason of ["review", "blocked"]) {
    const c = setup(), request = thread("request", {response_state: "input", attention_reason: reason,
      work_phase: "validation", checkpoint: {next_actor: "user", next_action: "Check the Phone board."},
      needs_attention: true, unhandled_response: true, can_handle_response: true, unread_result: true});
    c.render([request]);
    assert.deepEqual(c.ids(), ["request"]);
    const dismissed = {...request, needs_attention: false, unhandled_response: false, can_handle_response: false};
    c.render([dismissed]);
    assert.deepEqual(c.ids(), []);
    assert.equal(WorkingSet.needsReply(dismissed), false);
    const cell = c.host.querySelectorAll(".ws-cell").find(n => n.dataset.group === "overview:all" && n.dataset.level === "0");
    assert.equal(cell.querySelector(".ws-cell-count").textContent, "");
    assert.equal(cell.querySelector(".ws-cell-action"), null);
    c.selectLevel(2);
    assert.deepEqual(c.ids(), ["request"]);
    assert.ok(c.page().querySelector('[data-section="phase-validation"]'));
    c.render([request]); c.selectLevel(0);
    assert.deepEqual(c.ids(), ["request"], "A changed request surfaces again");
    assert.equal(WorkingSet.columnOf({...dismissed, status: "needs input", needs_attention: true}), "blocked",
      "Live approval is independent of the saved request receipt");
    assert.equal(WorkingSet.columnOf({...dismissed, status: "running", response_state: "working"}), "working");
  }
});
test("due reminders and new Later activity remain visible in Attention", () => {
  const c = setup();
  const reminder = thread("reminder", {snooze_due: true, needs_attention: true, unhandled_response: false});
  const later = thread("later", {attention: "later", needs_attention: true, response_state: "reply", unhandled_response: true, can_handle_response: true});
  const stalled = thread("stalled", {status: "stalled", response_state: "action", needs_attention: true, unhandled_response: false});
  c.render([reminder, later, stalled]);
  assert.deepEqual(c.ids().sort(), ["later", "reminder", "stalled"]);
  assert.equal(WorkingSet.levelOf({...later, needs_attention: false}), 2);
  assert.equal(WorkingSet.levelOf({...reminder, snooze_due: false, needs_attention: false}), 2);
});
test("a response remains in Attention when read and leaves when handled", () => {
  const c = setup(), answer = thread("question", {unread_result: true, unhandled_response: true, response_classification: classified("findings")});
  c.render([{...answer, status: "running", response_state: "working"}]); assert.deepEqual(c.ids(), ["question"]);
  c.render([answer]); assert.deepEqual(c.ids(), ["question"]);
  c.page().querySelector('[data-action="read"]').onclick(); assert.deepEqual(c.read, ["question"]);
  c.render([{...answer, unread_result: false}]); assert.deepEqual(c.ids(), ["question"]);
  c.render([{...answer, unread_result: false, unhandled_response: false}]); assert.deepEqual(c.ids(), []);
  c.selectLevel(2); assert.deepEqual(c.ids(), ["question"]);
});
test("saved positions preserve all original levels and migrate two-level Idle to level three", () => {
  for (const [version, oldLevel, newLevel] of [[1, 0, 0], [1, 1, 1], [1, 2, 2], [2, 0, 0], [2, 1, 2]]) {
    const key = 'agent-coord.working-set:v' + version + ':["/work","all","active"]';
    const c = setup({data: new Map([[key, JSON.stringify({level: oldLevel, dimension: "domain", pageKey: "overview:all"})]])});
    c.render([thread("a")]); assert.equal(c.page().dataset.level, String(newLevel));
    assert.equal(c.host.querySelector(".ws-map-foot").querySelector("select").children.find(o => o.selected).value, "domain");
  }
});
test("groups initially sort by user work recency and keep position through completion and metadata changes", () => {
  const a = thread("a", {project_id: "a", project_name: "A", turn_started_at: 1000, updated_at: 999999});
  const b = thread("b", {project_id: "b", project_name: "B", turn_started_at: 2000});
  const groups = WorkingSet.derive([a, b]); assert.deepEqual(groups.map(g => g.key), ["overview:all", "group:b", "group:a"]);
  const next = WorkingSet.derive([{...a, turn_completion: {completed_at: 3000}}, b], {order: groups.slice(1).map(g => g.key)});
  assert.deepEqual(next.map(g => g.key), groups.map(g => g.key));
  const restarted = WorkingSet.derive([{...a, turn_started_at: 4000, turn_completion: {completed_at: 500}}, b]);
  assert.equal(restarted[1].key, "group:a");
});
test("pins follow recency without duplicating a thread", () => {
  const c = setup(), list = Array.from({length: 14}, (_, n) => thread(String(n), {created_at: 200 + n}));
  list[0].pinned = true; c.render(list); c.selectLevel(2);
  assert.equal(c.ids()[0], "13"); c.page().querySelector(".ws-more").onclick(); assert.equal(c.page().querySelectorAll(".ws-pin").length, 1);
});
test("fresh visits start at All groups / Attention; each child appears independently in its own state", () => {
  const c = setup(); c.render([thread("parent"), thread("child", {creation_mode: "orchestration", created_by_thread_id: "parent", unread_result: true}), thread("run", {response_state: "working"})]);
  assert.deepEqual(c.ids(), ["run"]); assert.equal(c.page().querySelector(".ws-page-title").textContent, "All groups");
  c.selectLevel(1); assert.deepEqual(c.ids(), ["child"]);
  assert.equal(c.page().querySelector(".ws-parent"), null, "creator identity belongs to the shared card renderer");
  c.selectLevel(0); assert.deepEqual(c.ids(), ["run"]);
  c.selectLevel(2); assert.deepEqual(c.ids(), ["parent"]); assert(c.page().querySelector(".ws-phase-board"));
});
test("read receipts move responses to Idle without changing phase or dismissing required input", () => {
  const c = setup(), t = thread("findings", {unread_result: true}); c.render([t]); c.selectLevel(1);
  c.page().querySelector(".ws-read").onclick(); assert.deepEqual(c.read, ["findings"]);
  c.render([{...t, unread_result: false}]); assert.deepEqual(c.ids(), []);
  c.selectLevel(2); assert.deepEqual(c.ids(), ["findings"]);
  assert.equal(c.page().querySelector(".ws-section-title").querySelector("h4").textContent, "Investigating");
  c.render([{...t, unread_result: false, checkpoint: {next_actor: "user", next_action: "Review"}}]);
  c.selectLevel(0); assert.deepEqual(c.ids(), ["findings"]);
});
test("aggregate cards omit group labels, and groups keep the level when their current state is empty", () => {
  const c = setup(); c.render([thread("a", {project_id: "a", project_name: "A", unread_result: true}), thread("b", {project_id: "b", project_name: "B"})]);
  c.selectLevel(1); assert.deepEqual(c.ids(), ["a"]);
  assert.equal(c.page().querySelector(".ws-group-link"), null);
  c.selectGroup("A"); assert.deepEqual(c.ids(), ["a"]);
  assert.equal(c.page().querySelector(".ws-page-title").textContent, "A");
  c.selectGroup("B"); assert.deepEqual(c.ids(), []); assert(c.page().querySelector(".ws-empty"));
  c.selectLevel(2); assert.deepEqual(c.ids(), ["b"]);
});
test("map cells count every idle thread, read or unread", () => {
  const c = setup(); c.render([thread("a", {project_id: "a", project_name: "A"}), thread("b", {project_id: "a", project_name: "A"})]);
  const idle = c.host.querySelectorAll(".ws-cell").find(n => n.dataset.group === "group:a" && n.dataset.level === "2");
  assert.equal(idle.querySelector(".ws-cell-count").textContent, "2"); assert.equal(idle.title, "A · Idle · 2 conversations");
});
test("Attention map totals include working and completed cards independently of unread and action counts", () => {
  const c = setup(), working = thread("working", {response_state: "working"});
  const done = thread("done", {work_phase: "finished", unhandled_response: true});
  const cell = () => c.host.querySelectorAll(".ws-cell").find(n => n.dataset.group === "overview:all" && n.dataset.level === "0");
  c.render([working, done]);
  assert.equal(c.ids().length, 2);
  assert.equal(cell().querySelector(".ws-cell-count").textContent, "2");
  assert.equal(cell().title, "All groups · Attention · 2 conversations");
  assert.equal(cell().querySelector(".ws-cell-unread"), null);
  assert.equal(cell().querySelector(".ws-cell-action"), null);
  const blocked = thread("blocked", {status: "needs input", unread_result: true});
  const review = thread("review", {unhandled_response: true, response_classification: classified("review")});
  c.render([working, {...done, unread_result: true}, blocked, review]);
  assert.equal(cell().querySelector(".ws-cell-count").textContent, "4");
  assert.equal(cell().title, "All groups · Attention · 4 conversations · 2 unread · 1 blocked · 1 needing attention");
  assert.equal(cell().attrs["aria-label"], cell().title);
  assert(cell().classList.contains("ws-selected"));
  assert.equal(cell().querySelector(".ws-cell-unread").attrs["aria-hidden"], "true");
  assert.equal(cell().querySelector(".ws-cell-action").textContent, "!");
  c.render([working, done, {...blocked, unread_result: false}, review]);
  assert.equal(cell().querySelector(".ws-cell-count").textContent, "4");
  assert.equal(cell().querySelector(".ws-cell-unread"), null);
  assert(cell().querySelector(".ws-cell-action"), "reading a request does not resolve it");
  c.render([working, done, thread("blocked"), {...review, unhandled_response: false}]);
  assert.equal(cell().querySelector(".ws-cell-count").textContent, "2");
  assert.equal(cell().querySelector(".ws-cell-action"), null);
});
test("map signals stay local to each group and state, including unread checkpoints and empty cells", () => {
  const c = setup(), a = {project_id: "a", project_name: "A"}, b = {project_id: "b", project_name: "B"};
  const answer = thread("answer", {...a, unread_result: true});
  const checkpoint = thread("checkpoint", {...a, unread: true});
  const cell = (group, level) => c.host.querySelectorAll(".ws-cell").find(n => n.dataset.group === group && n.dataset.level === String(level));
  c.render([answer, checkpoint, thread("read", b)]);
  assert.equal(cell("group:a", 1).querySelector(".ws-cell-count").textContent, "1");
  assert.equal(cell("group:a", 1).title, "A · Replies to read · 1 conversation · 1 unread");
  assert(cell("group:a", 1).querySelector(".ws-cell-unread"));
  assert.equal(cell("group:a", 1).querySelector(".ws-cell-action"), null);
  assert.equal(cell("group:a", 2).title, "A · Idle · 1 conversation · 1 unread");
  assert(cell("group:a", 2).querySelector(".ws-cell-unread"));
  assert.equal(cell("group:b", 2).querySelector(".ws-cell-unread"), null);
  assert.equal(cell("group:b", 1).querySelector(".ws-cell-count").textContent, "");
  assert.equal(cell("group:b", 1).title, "B · Replies to read · Empty");
  assert.equal(cell("group:b", 1).querySelector(".ws-cell-unread"), null);
  assert.equal(cell("group:b", 1).querySelector(".ws-cell-action"), null);
  c.render([{...answer, unread_result: false}, {...checkpoint, unread: false}, thread("read", b)]);
  assert.equal(cell("group:a", 1).title, "A · Replies to read · Empty");
  assert.equal(cell("group:a", 2).title, "A · Idle · 2 conversations");
  assert.equal(cell("group:a", 2).querySelector(".ws-cell-unread"), null);
  assert.equal(cell("overview:all", 2).querySelector(".ws-cell-count").textContent, "3");
});
test("top tabs and map cells share group selection without resetting the level", () => {
  const list = [thread("a", {project_id: "a", project_name: "A"}), thread("b", {project_id: "b", project_name: "B"})];
  const changes = [], c = setup({onGroupChange: id => changes.push(id)});
  c.render(list, {selectedGroup: "a", view: "group-a"});
  assert.equal(c.page().dataset.key, "group:a");
  assert.equal(c.host.querySelector(".ws-group-nav"), null);
  assert.equal(c.host.querySelector('[aria-label="Choose group"]'), null);
  c.selectLevel(2); assert.deepEqual(changes, []);
  c.render(list, {selectedGroup: "b", view: "group-b"});
  assert.deepEqual(c.ids(), ["b"]); assert.equal(c.page().dataset.level, "2");
  assert.deepEqual(changes, []); // Rendering a tab selection must not activate it again.
  c.selectGroup("A"); assert.deepEqual(changes, ["a"]);
  c.render(list, {selectedGroup: "a", view: "group-a"});
  assert.deepEqual(c.ids(), ["a"]); assert.equal(c.page().dataset.level, "2");
  c.selectGroup("All groups"); assert.deepEqual(changes, ["a", null]);
  c.render(list, {selectedGroup: null, view: "all"});
  assert.equal(c.page().dataset.key, "overview:all");
  c.key("ArrowRight"); assert.equal(changes.at(-1), "a");
});
test("selected empty groups stay selected, and top tabs override a restored map position", () => {
  const c = setup(); c.render([thread("a", {project_id: "a", project_name: "A"})]); c.selectGroup("A");
  const reload = setup({data: c.data});
  reload.render([], {selectedGroup: "empty", groupName: "Empty group", view: "group-empty"});
  assert.equal(reload.page().dataset.key, "group:empty");
  assert.equal(reload.page().querySelector(".ws-page-title").textContent, "Empty group");
  assert(reload.page().querySelector(".ws-empty"));
  reload.render([], {selectedGroup: null, view: "all"});
  assert.equal(reload.page().dataset.key, "overview:all");
});
test("Ungrouped remains reachable under the All conversations tab", () => {
  const changes = [], c = setup({onGroupChange: id => changes.push(id)});
  const list = [thread("a", {project_id: "a", project_name: "A"}), thread("loose")];
  c.render(list, {selectedGroup: "a", view: "group-a"}); c.selectGroup("Ungrouped");
  assert.deepEqual(changes, [null]);
  c.render(list, {selectedGroup: null, view: "all"});
  assert.equal(c.page().dataset.key, "ungrouped:/work");
});
test("domain and workspace maps use All conversations; choosing a group tab returns to groups", () => {
  const changes = [], c = setup({onGroupChange: id => changes.push(id)});
  const list = [thread("a", {project_id: "a", project_name: "A", domain: "web"})];
  c.render(list, {selectedGroup: "a", view: "group-a"});
  c.key("g"); assert.deepEqual(changes, [null]);
  c.render(list, {selectedGroup: null, view: "all"});
  c.key("ArrowRight"); assert.equal(c.page().dataset.key, "domain:web");
  assert.deepEqual(changes, [null]);
  c.render(list, {selectedGroup: "a", view: "group-a"});
  assert.equal(c.page().dataset.key, "group:a");
});
test("saved navigation survives reload, remains workspace-specific, and removed groups fall back to All groups", () => {
  const c = setup(), list = [thread("a", {project_id: "a", project_name: "A"})]; c.render(list); c.selectLevel(2); c.selectGroup("A");
  const reloaded = setup({data: c.data}); reloaded.render(list); assert.deepEqual(reloaded.ids(), ["a"]);
  assert.equal(reloaded.page().querySelector(".ws-page-title").textContent, "A");
  reloaded.render([thread("other", {unread_result: true})], {workspace: "/other"});
  assert.equal(reloaded.page().dataset.level, "0"); reloaded.selectLevel(1); assert.deepEqual(reloaded.ids(), ["other"]);
  reloaded.render(list); assert.equal(reloaded.page().querySelector(".ws-page-title").textContent, "A");
  reloaded.render([thread("replacement")]); assert.equal(reloaded.page().querySelector(".ws-page-title").textContent, "All groups");
});
test("Later and Closed open Idle initially; corrupt or unavailable storage leaves navigation usable", () => {
  for (const placement of ["later", "archived"]) { const c = setup(); c.render([thread("parked", {attention: placement})], {placement}); assert.deepEqual(c.ids(), ["parked"]); }
  const c = setup({broken: true}); c.render([thread("a")]); c.selectLevel(2); c.render([thread("b")], {workspace: "/other"}); c.render([thread("a")]); assert.deepEqual(c.ids(), ["a"]);
  const bad = setup({data: new Map([['agent-coord.working-set:v1:["/work","all","active"]', 'null']])}); bad.render([]); assert.equal(bad.page().dataset.level, "0");
});
test("per-section caps expand and collapse without hiding groups or losing membership", () => {
  const c = setup(), list = Array.from({length: 16}, (_, n) => thread(String(n), {project_id: "a", project_name: "A", unread_result: true}));
  c.render(list); c.selectLevel(1); assert.equal(c.ids().length, 12); c.page().querySelector(".ws-more").onclick(); assert.equal(c.ids().length, 16);
  c.page().querySelector(".ws-more").onclick(); assert.equal(c.ids().length, 12);
  assert.equal(c.host.querySelectorAll(".ws-cell").length, 6);
});
test("polling retains page scroll and focus, and unchanged renders keep the DOM", () => {
  const c = setup(), t = thread("reply", {unread_result: true}); c.render([t]); c.selectLevel(1);
  const oldPage = c.page(); oldPage.scrollTop = 145; const control = oldPage.querySelector("button"); control.focus();
  c.render([t]); assert.equal(c.page(), oldPage);
  c.render([{...t, title: "Updated"}]); assert.equal(c.page().scrollTop, 145); assert.equal(c.doc.activeElement.dataset.thread, "reply");
  c.selectLevel(2); c.selectLevel(1); assert.equal(c.page().scrollTop, 145);
  assert.equal(c.observers.length, 1);
});
test("keyboard navigation is scoped to the visible overview and leaves forms, menus and modifiers alone", () => {
  const c = setup(); c.render([thread("a")]); assert(c.key("ArrowDown").prevented); assert.deepEqual(c.ids(), ["a"]);
  assert(c.key("ArrowDown").prevented); assert.deepEqual(c.ids(), ["a"]);
  for (const tag of ["input", "textarea", "button", "select", "summary", "a"]) assert(!c.key("ArrowUp", c.doc.createElement(tag)).prevented);
  assert(!c.key("ArrowUp", c.doc.body, {ctrlKey: true}).prevented);
  c.doc.modal = true; assert(!c.key("ArrowUp").prevented); c.doc.modal = false;
  c.enable(false); assert(!c.key("ArrowUp").prevented); c.enable(true);
  c.host.hidden = true; assert(!c.key("ArrowUp").prevented); c.host.hidden = false;
  c.key("g"); assert(c.data.values().some(v => JSON.parse(v).dimension === "domain"));
});
test("up/down skips empty Replies only in the current group and filtered view", () => {
  const c = setup(), reply = thread("reply", {project_id: "a", project_name: "A", unread_result: true});
  const idle = thread("idle", {project_id: "b", project_name: "B"});
  c.render([reply, idle]);
  c.key("ArrowDown"); assert.equal(c.page().dataset.level, "1");
  c.key("ArrowDown"); assert.equal(c.page().dataset.level, "2");
  c.key("ArrowUp"); assert.equal(c.page().dataset.level, "1");
  c.selectGroup("B"); c.selectLevel(0);
  c.key("ArrowDown"); assert.equal(c.page().dataset.level, "2");
  c.key("ArrowDown"); assert.equal(c.page().dataset.level, "2");
  c.key("ArrowUp"); assert.equal(c.page().dataset.level, "0");
  c.key("ArrowUp"); assert.equal(c.page().dataset.level, "0");
  c.selectGroup("A"); c.key("ArrowDown"); assert.deepEqual(c.ids(), ["reply"]);
  c.render([idle], {view: "filtered"});
  c.key("ArrowDown"); assert.equal(c.page().dataset.level, "2");
  c.key("ArrowUp"); assert.equal(c.page().dataset.level, "0");
});
test("reading the last reply stays on Replies and empty map cells remain selectable", () => {
  const c = setup(), reply = thread("reply", {unread_result: true});
  c.render([reply]); c.key("ArrowDown"); assert.equal(c.page().dataset.level, "1");
  c.render([{...reply, unread_result: false}]);
  assert.equal(c.page().dataset.level, "1"); assert.deepEqual(c.ids(), []);
  assert.equal(c.host.querySelectorAll(".ws-level").length, 3);
  c.key("ArrowUp"); assert.equal(c.page().dataset.level, "0");
  const emptyCell = c.host.querySelectorAll(".ws-cell").find(n => n.dataset.group === "overview:all" && n.dataset.level === "1");
  emptyCell.onclick(); assert.equal(c.page().dataset.level, "1");
  c.key("ArrowDown"); assert.equal(c.page().dataset.level, "2");
  c.selectLevel(1); assert.equal(c.page().dataset.level, "1");
  c.key("ArrowDown"); assert.equal(c.page().dataset.level, "2");
  c.key("ArrowUp"); assert.equal(c.page().dataset.level, "0");
});
test("wheel gestures scroll cards before switching states, honor cooldown and preserve browser zoom", () => {
  const c = setup(); c.render([thread("a", {unread_result: true})]);
  const page = c.page(), viewport = c.host.querySelector(".ws-viewport"); page.scrollHeight = 1500;
  const native = c.event({deltaY: 200}); viewport.emit("wheel", native); assert(!native.prevented); assert.equal(c.page().dataset.level, "0");
  page.scrollTop = 800; c.advance(400); viewport.emit("wheel", c.event({deltaY: 10, deltaMode: 1})); assert.equal(c.page().dataset.level, "1");
  c.advance(1000); viewport.emit("wheel", c.event({deltaY: -250, ctrlKey: true})); assert.equal(c.page().dataset.level, "1");
  viewport.emit("wheel", c.event({deltaY: -250})); assert.equal(c.page().dataset.level, "0");
});
test("touch scroll does not unexpectedly change levels; an edge swipe does", () => {
  const c = setup(); c.render([thread("a", {unread_result: true})]); const viewport = c.host.querySelector(".ws-viewport");
  c.selectLevel(1);
  const start = () => viewport.emit("touchstart", {target: c.page(), touches: [{clientX: 100, clientY: 300}]});
  const end = () => viewport.emit("touchend", {changedTouches: [{clientX: 100, clientY: 450}]});
  start(); c.page().scrollTop = 100; end(); assert.equal(c.page().dataset.level, "1");
  c.page().scrollTop = 0; start(); end(); assert.equal(c.page().dataset.level, "0");
});
test("horizontal gestures navigate groups from the attention list", () => {
  const c = setup(); c.render([thread("a", {project_id: "a", project_name: "A", unhandled_response: true})]);
  const viewport = c.host.querySelector(".ws-viewport");
  viewport.emit("touchstart", {target: c.page(), touches: [{clientX: 300, clientY: 100}]});
  viewport.emit("touchend", {changedTouches: [{clientX: 100, clientY: 100}]});
  assert.equal(c.page().querySelector(".ws-page-title").textContent, "A");
});
test("grouping changes reset to the aggregate and restore on reload; large inventories build only neighbors", () => {
  const c = setup(), list = Array.from({length: 40}, (_, n) => thread("t" + n, {project_id: String(n), project_name: "G" + n, domain: "d" + n}));
  c.render(list); assert.equal(c.host.querySelectorAll(".ws-page").length, 6); assert.equal(c.host.querySelectorAll(".ws-cell").length, 123);
  c.key("g"); const reload = setup({data: c.data}); reload.render(list);
  assert.equal(reload.host.querySelector(".ws-map-foot").querySelector("select").children.find(o => o.selected).value, "domain");
  c.ui.destroy(); assert.equal(c.observers[0].disconnected, true); assert.equal(c.host.children.length, 0); assert.equal(c.doc.events.keydown, undefined);
});
test("production integration shares cards and uses seen receipt IDs without marking a request handled", async () => {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const nodes = {"atlas-overview": {hidden: false}, "working-set-overview": {hidden: true}, overview: {classList: {toggle() {}}}, welcome: {hidden: false}, view: {value: "active"}};
  let config, options, rendered; const calls = [], state = {overviewLayout: {mode: "working-set"}, projectCwd: "/work", listSignature: "rev"};
  const c = {state, document: {}, window: {}, savedViews: {activeId: "all", openGroup: id => calls.push(["group", id])}, $: id => nodes[id],
    WorkingSet: {create(value) { config = value; return {render(t, host, opts) { options = opts; rendered = t; }}; }},
    threadCard: (...args) => calls.push(args), action: f => f(), select: id => calls.push(["open", id]),
    threadPath: id => "threads/" + id, api: async (...args) => calls.push(args), refreshList: async () => calls.push(["refresh"]),
    markThreadHandled: async item => calls.push(["dismiss", item])};
  vm.createContext(c); vm.runInContext(source.slice(source.indexOf("function renderOverviewLayout("), source.indexOf("function upcomingSchedules(")), c);
  c.renderOverviewLayout([]); assert.equal(nodes["atlas-overview"].hidden, true); assert.equal(nodes["working-set-overview"].hidden, false); assert.equal(nodes.overview.hidden, true);
  assert.equal(options.workspace, "/work");
  assert.equal(options.selectedGroup, null);
  const inventory = [thread("a"), thread("b")];
  c.savedViews.activeId = "group-a";
  c.renderOverviewLayout([inventory[0]], [], inventory);
  assert.equal(rendered, inventory); assert.equal(options.selectedGroup, "a");
  await config.onGroupChange("b"); assert.deepEqual(calls.pop(), ["group", "b"]);
  await config.onGroupChange(null); assert.deepEqual(calls.pop(), ["group", null]);
  const t = thread("a", {attention_target_thread_id: "parent", checkpoint: {id: 8}, turn_completion: {id: 12}});
  config.renderCard(t, true); assert.equal(calls[0][0].attention_target_thread_id, null); assert.equal(calls[0][2], true);
  assert.equal(calls[0][0].can_handle_response, false);
  await config.onRead(t); assert.equal(calls[1][0], "threads/a");
  assert.equal(JSON.stringify(calls[1][1]), JSON.stringify({seen: true, seen_checkpoint_id: 8, seen_completion_id: 12}));
  assert(!Object.hasOwn(calls[1][1], "handled"));
  calls.length = 0; await config.onDismiss(t);
  assert.deepEqual(calls[0], ["dismiss", t]);
});

// List overview: Attention, Working and Idle rows over the same grouping as Focus.
const NOW = 1_000_000;
const listThread = (id, extra = {}) => thread(id, {created_at: NOW - 5000, updated_at: NOW - 4000, ...extra});
const placed = (threads, id) => WorkingSet.placementOf(threads.find(t => t.thread_id === id), new Map(threads.map(t => [t.thread_id, t])), NOW);
test("list placement keeps requests, drains read replies, and leaves agent replies to their orchestrator", () => {
  const parent = listThread("parent");
  const agent = (id, extra) => listThread(id, {creation_mode: "orchestration", created_by_thread_id: "parent", ...extra});
  const threads = [parent,
    listThread("approval", {status: "needs input", attention: "later"}), listThread("question", {open_ask: {id: 1, question: "Which?"}}),
    listThread("failed", {response_state: "failed", unread_result: false}),
    listThread("reply", {unread_result: true, attention_since: NOW - 60}), listThread("read", {unread_result: false, attention_since: NOW - 60}),
    listThread("aged", {unread_result: true, attention_since: NOW - 90000}),
    listThread("asks", {unread: true, checkpoint: {id: 4, next_actor: "user", next_action: "Choose", created_at: NOW - 30}, attention_since: NOW - 30}),
    listThread("asked", {unread: false, checkpoint: {id: 4, next_actor: "user", next_action: "Choose"}, attention_since: NOW - 30}),
    listThread("handled", {unread: true, handled_checkpoint_id: 4, checkpoint: {id: 4, next_actor: "user", next_action: "Choose"}}),
    listThread("running", {status: "running", response_state: "working", unread_result: true}),
    listThread("later", {attention: "later", unread_result: true}), listThread("closed", {attention: "archived"}),
    agent("agent-reply", {unread_result: true, attention_since: NOW - 10}), agent("agent-failed", {response_state: "failed"}),
    agent("agent-approval", {status: "needs input"}), agent("agent-asks", {unread: true, attention_since: NOW - 10,
      checkpoint: {id: 2, next_actor: "user", next_action: "Approve the plan"}})];
  const expected = {approval: "attention", question: "attention", failed: "attention", reply: "attention", read: "idle", aged: "idle",
    asks: "attention", asked: "idle", handled: "idle", running: "working", later: "later", closed: null,
    "agent-reply": "nested", "agent-failed": "nested", "agent-approval": "attention", "agent-asks": "attention"};
  for (const [id, place] of Object.entries(expected)) assert.equal(placed(threads, id), place, id);
  assert.equal(WorkingSet.requestOf(threads.find(t => t.thread_id === "asks")), "needs-you");
  assert.equal(WorkingSet.requestOf(threads.find(t => t.thread_id === "stale") || listThread("stale", {checkpoint_stale: true,
    checkpoint: {id: 9, next_actor: "user", next_action: "Old"}})), null);
});
test("an orchestrator works while its agents run, but its own unread reply still reaches Attention", () => {
  const agent = listThread("agent", {creation_mode: "orchestration", created_by_thread_id: "lead", status: "running", response_state: "working"});
  const quiet = WorkingSet.listSections([listThread("lead"), agent], {now: NOW, workspace: "/work"});
  assert.deepEqual(quiet.sections.working.map(t => t.thread_id), ["lead"]);
  assert.deepEqual(quiet.agents.get("lead").map(t => t.thread_id), ["agent"]);
  const replied = WorkingSet.listSections([listThread("lead", {unread_result: true, attention_since: NOW - 5}), agent], {now: NOW, workspace: "/work"});
  assert.deepEqual(replied.sections.attention.map(t => t.thread_id), ["lead"]);
});
test("list order puts requests first, then follows the user's own last message, with pins first and empty threads apart", () => {
  const threads = [
    listThread("new-reply", {unread_result: true, attention_since: NOW - 10}), listThread("old-reply", {unread_result: true, attention_since: NOW - 900}),
    listThread("failed", {response_state: "failed", attention_since: NOW - 5000, checkpoint: {id: 1}}),
    listThread("mine-recent", {last_user_message_at: NOW - 20, turn_started_at: NOW - 3000, checkpoint: {id: 2}}),
    listThread("agent-woke", {last_user_message_at: NOW - 2000, turn_started_at: NOW - 5, checkpoint: {id: 3}}),
    listThread("turn-only", {turn_started_at: NOW - 100, checkpoint: {id: 4}}),
    listThread("pinned", {pinned: true, last_user_message_at: NOW - 4000, checkpoint: {id: 5}}),
    listThread("blank", {created_at: NOW - 1})];
  const {sections} = WorkingSet.listSections(threads, {now: NOW, workspace: "/work"});
  assert.deepEqual(sections.attention.map(t => t.thread_id), ["failed", "new-reply", "old-reply"]);
  assert.deepEqual(sections.idle.map(t => t.thread_id), ["pinned", "mine-recent", "turn-only", "agent-woke"]);
  assert.deepEqual(sections.empty.map(t => t.thread_id), ["blank"]);
});
test("list groups count Attention per group, filter by the chosen group, and show parked threads as one list", () => {
  const threads = [listThread("a", {project_id: "g1", project_name: "One", unread_result: true, attention_since: NOW - 1}),
    listThread("b", {project_id: "g2", project_name: "Two", checkpoint: {id: 1}}), listThread("c", {checkpoint: {id: 2}})];
  const all = WorkingSet.listSections(threads, {now: NOW, workspace: "/work"});
  assert.deepEqual(all.groups.map(g => [g.key, g.attention]), [["overview:all", 1], ["group:g1", 1], ["group:g2", 0], ["ungrouped:/work", 0]]);
  const one = WorkingSet.listSections(threads, {now: NOW, workspace: "/work", pageKey: "group:g2"});
  assert.equal(one.selected, "group:g2"); assert.deepEqual(one.sections.idle.map(t => t.thread_id), ["b"]); assert.equal(one.sections.attention.length, 0);
  assert.equal(WorkingSet.listSections(threads, {now: NOW, pageKey: "group:missing"}).selected, "overview:all");
  const later = WorkingSet.listSections([listThread("l", {attention: "later", checkpoint: {id: 3}})], {now: NOW, view: "later"});
  assert.deepEqual(later.sections.idle.map(t => t.thread_id), ["l"]);
  const domains = WorkingSet.listSections(threads.map(t => ({...t, domain: t.thread_id === "c" ? "ui" : null})), {now: NOW, dimension: "domain"});
  assert.deepEqual(domains.groups.map(g => g.name).sort(), ["All groups", "No domain", "ui"]);
});

function listSetup({data = new Map()} = {}) {
  const doc = {activeElement: null};
  class Node {
    constructor(tag) { Object.assign(this, {tagName: tag, children: [], attrs: {}, dataset: {}, className: "", textContent: ""}); }
    get classList() { const read = () => new Set(this.className.split(/\s+/).filter(Boolean)); return {
      add: n => { const s = read(); s.add(n); this.className = [...s].join(" "); },
      toggle: (n, on) => { const s = read(); if (on ?? !s.has(n)) s.add(n); else s.delete(n); this.className = [...s].join(" "); },
      contains: n => read().has(n)}; }
    append(...items) { for (const item of items) { this.children.push(item); if (typeof item === "object") item.parent = this; } }
    replaceChildren(...items) { this.children = []; this.append(...items); }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    matches(s) {
      if (s.startsWith("[data-")) { const [, k, v] = s.match(/\[data-([a-z]+)(?:="([^"]*)")?\]/); return this.dataset[k] != null && (v == null || this.dataset[k] === v); }
      if (s.startsWith("button:not(")) return this.tagName === "button" && !this.classList.contains(s.slice(12, -1));
      return s.startsWith(".") ? this.classList.contains(s.slice(1)) : this.tagName === s;
    }
    querySelectorAll(s) { return this.children.filter(n => typeof n === "object").flatMap(n => [...(n.matches(s) ? [n] : []), ...n.querySelectorAll(s)]); }
    closest(s) { return this.matches(s) ? this : this.parent?.closest?.(s) || null; }
    contains(n) { return this === n || this.children.some(c => typeof c === "object" && c.contains(n)); }
    focus() { doc.activeElement = this; }
    get text() { return this.children.map(c => typeof c === "object" ? c.text : String(c)).join("") || this.textContent; }
  }
  doc.createElement = tag => new Node(tag); doc.createTextNode = text => text;
  const calls = [], host = new Node("section");
  const ui = WorkingSet.createList({document: doc, now: () => NOW,
    storage: {getItem: k => data.get(k), setItem: (k, v) => data.set(k, v)},
    onOpen: id => calls.push(["open", id]), onRead: t => calls.push(["read", t.thread_id]), onClose: t => calls.push(["close", t.thread_id]),
    onMenu: (t, anchor) => calls.push(["menu", t.thread_id, anchor.dataset.action]), onGroupChange: id => calls.push(["group", id])});
  const rows = name => host.querySelectorAll(".tl-section").find(s => s.dataset.section === name)?.querySelectorAll(".tl-row").map(r => r.dataset.thread) || [];
  const control = (id, action) => host.querySelectorAll("[data-thread]").find(n => n.dataset.thread === id && n.dataset.action === action);
  return {ui, host, calls, rows, control, data, render: (threads, options = {}) => ui.render(threads, host, {workspace: "/work", revision: Math.random(), ...options})};
}
test("list rows open, read, close and open the thread menu without opening the row", () => {
  const c = listSetup();
  c.render([listThread("reply", {unread_result: true, attention_since: NOW - 60, checkpoint: {id: 1, summary: "Done"}}),
    listThread("quiet", {checkpoint: {id: 2, summary: "Earlier"}}), listThread("busy", {status: "running", response_state: "working"})]);
  assert.deepEqual([c.rows("attention"), c.rows("working"), c.rows("idle")], [["reply"], ["busy"], ["quiet"]]);
  assert.equal(c.control("quiet", "read"), undefined);
  for (const action of ["read", "more", "close"]) c.control("reply", action).onclick({stopPropagation() {}});
  c.control("reply", "open").onclick({stopPropagation() {}});
  const row = c.host.querySelectorAll(".tl-row").find(r => r.dataset.thread === "quiet");
  row.onclick({target: row.children[0]}); row.onclick({target: c.control("quiet", "more")});
  assert.deepEqual(c.calls, [["read", "reply"], ["menu", "reply", "more"], ["close", "reply"], ["open", "reply"], ["open", "quiet"]]);
});
test("list agents expand under their orchestrator, and a waiting agent names its orchestrator in Attention", () => {
  const c = listSetup();
  const agent = (id, extra) => listThread(id, {creation_mode: "orchestration", created_by_thread_id: "lead", ...extra});
  c.render([listThread("lead", {title: "Lead", checkpoint: {id: 1}}), agent("worker", {unread_result: true, checkpoint: {id: 2}}),
    agent("blocked", {status: "needs input", title: "Blocked agent"})]);
  assert.deepEqual(c.rows("attention"), ["blocked"]); assert.deepEqual(c.rows("idle"), ["lead"]);
  assert.equal(c.control("blocked", "open").text, "Lead › Blocked agent");
  const toggle = c.control("lead", "agents"); assert.equal(toggle.attrs["aria-expanded"], "false");
  toggle.onclick({stopPropagation() {}});
  assert.deepEqual(c.rows("idle"), ["lead", "blocked", "worker"]);
  assert.equal(c.control("lead", "agents").attrs["aria-expanded"], "true");
});
test("list group tabs filter rows, remember the choice, and report saved-view group changes", () => {
  const c = listSetup();
  const threads = [listThread("a", {project_id: "g1", project_name: "One", checkpoint: {id: 1}}), listThread("b", {checkpoint: {id: 2}})];
  c.render(threads);
  const tab = c.host.querySelectorAll(".tl-group").find(n => n.dataset.group === "group:g1");
  tab.onclick({stopPropagation() {}});
  assert.deepEqual(c.rows("idle"), ["a"]); assert.deepEqual(c.calls, [["group", "g1"]]);
  assert.match([...c.data.values()][0], /"pageKey":"group:g1"/);
  const again = listSetup({data: c.data}); again.render(threads); assert.deepEqual(again.rows("idle"), ["a"]);
  const controlled = listSetup(); controlled.render(threads, {selectedGroup: "g1", view: "group-g1"});
  assert.deepEqual(controlled.rows("idle"), ["a"]);
});
test("a background repaint keeps the reader's place in the overview scroller", () => {
  const c = listSetup();
  const threads = Array.from({length: 30}, (_, i) => listThread("t" + i, {checkpoint: {id: i + 1}}));
  c.render(threads);
  // WebKit resets the scrolled ancestor when the list's rows are replaced by new nodes.
  const scroller = {scrollTop: 900, scrollLeft: 0, parentElement: null};
  c.host.parentElement = scroller;
  const replace = c.host.replaceChildren.bind(c.host);
  c.host.replaceChildren = (...items) => { replace(...items); scroller.scrollTop = 0; };
  c.render(threads.map(t => ({...t, checkpoint: {...t.checkpoint, summary: "Updated"}})));
  assert.equal(scroller.scrollTop, 900);
  assert.equal(c.rows("idle").length, 30);
});
test("Idle shows fifty rows until asked, and empty threads collapse into one control", () => {
  const c = listSetup();
  const many = Array.from({length: 55}, (_, i) => listThread("t" + i, {last_user_message_at: NOW - i, checkpoint: {id: i + 1}}));
  c.render([...many, listThread("blank")]);
  assert.equal(c.rows("idle").length, 50); assert.equal(c.rows("empty").length, 0);
  const more = c.host.querySelectorAll(".tl-more"); assert.equal(more[0].textContent, "Show all 55"); assert.equal(more[1].textContent, "1 empty thread");
  more[0].onclick({stopPropagation() {}}); more[1].onclick({stopPropagation() {}});
  c.render([...many, listThread("blank")]);
  assert.equal(c.rows("idle").length, 55); assert.deepEqual(c.rows("empty"), ["blank"]);
});
test("production integration renders the list beside Focus with the shared thread menu", async () => {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const nodes = {"atlas-overview": {hidden: false}, "working-set-overview": {hidden: false}, "thread-list-overview": {hidden: true},
    overview: {classList: {toggle() {}}}, welcome: {hidden: false}, view: {value: "active"}};
  let config, options; const calls = [];
  const c = {state: {overviewLayout: {mode: "list"}, projectCwd: "/work", listSignature: "rev",
      threadActions: {showMenu: (...args) => calls.push(["menu", ...args])}},
    document: {}, window: {}, savedViews: {activeId: "all", openGroup: id => calls.push(["group", id])}, $: id => nodes[id],
    WorkingSet: {createList(value) { config = value; return {render(t, host, opts) { options = opts; }}; }},
    threadMenuItems: t => [{label: "Pin " + t.thread_id}], action: f => f(), select: id => calls.push(["open", id]),
    toggleThreadClosed: t => calls.push(["close", t.thread_id]), threadPath: id => "threads/" + id,
    api: async (...args) => calls.push(args), refreshList: async () => calls.push(["refresh"])};
  vm.createContext(c); vm.runInContext(source.slice(source.indexOf("function renderOverviewLayout("), source.indexOf("function upcomingSchedules(")), c);
  c.renderOverviewLayout([]);
  assert.equal(nodes["thread-list-overview"].hidden, false); assert.equal(nodes["working-set-overview"].hidden, true); assert.equal(nodes.overview.hidden, true);
  assert.equal(options.selectedGroup, null); assert.equal(options.placement, "active"); assert.equal(options.revision, "rev");
  const t = thread("a", {checkpoint: {id: 8}, turn_completion: {id: 12}});
  config.onOpen("a"); config.onClose(t); config.onMenu(t, "anchor", "event"); await config.onRead(t);
  assert.deepEqual(calls.slice(0, 3), [["open", "a"], ["close", "a"], ["menu", "anchor", "Thread a", [{label: "Pin a"}], "event"]]);
  assert.equal(JSON.stringify(calls[3]), JSON.stringify(["threads/a", {seen: true, seen_checkpoint_id: 8, seen_completion_id: 12}]));
});
