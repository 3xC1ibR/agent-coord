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
    renderCard(t, needs) { const b = new Element("button"); b.dataset.thread = t.thread_id; b.textContent = t.title; b.onclick = () => opened.push(t.thread_id); rendered.push([t.thread_id, needs]); return b; },
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
test("unread child findings render in Replies to read independently of an idle creator", () => {
  const c = setup(), parent = thread("parent", {project_id: "evaluations", project_name: "Evaluations",
    needs_attention: false, work_phase: "finished", unhandled_response: false});
  const child = thread("child", {creation_mode: "orchestration", created_by_thread_id: "parent",
    response_state: "update", attention_reason: "findings", needs_attention: true, unread_result: true, unhandled_response: true});
  const records = [parent, child], before = JSON.stringify(records);
  c.render(records); c.selectLevel(1);
  for (const group of ["All groups", "Evaluations"]) {
    c.selectGroup(group);
    assert.deepEqual(c.ids(), ["child"]);
    const replies = c.page().querySelector('[data-section="replies"]');
    assert.equal(replies.querySelector(".ws-card").dataset.cardThread, "child");
    replies.querySelector('[data-thread="child"]').onclick();
    assert.equal(c.opened.at(-1), "child");
    const cell = c.host.querySelectorAll(".ws-cell").find(n => n.dataset.group === (group === "All groups" ? "overview:all" : "group:evaluations") && n.dataset.level === "1");
    assert.equal(cell.textContent, "1");
  }
  assert.equal(JSON.stringify(records), before);
  c.render([parent, {...child, unread_result: false, needs_attention: false}]);
  assert.deepEqual(c.ids(), []);
  c.selectLevel(2);
  assert.deepEqual(c.ids().sort(), ["child", "parent"]);
});
test("attention columns increase left to right and keep completed work until explicit dismissal", () => {
  const c = setup(), completed = thread("done", {work_phase: "finished", unhandled_response: true, unread_result: true});
  const list = [thread("run", {response_state: "working", unread_result: true}), completed,
    thread("answer", {unread_result: true}), thread("ask", {checkpoint: {next_actor: "user", next_action: "Pick one"}}),
    thread("failed", {response_state: "failed"}), thread("classified-blocker", {response_classification: classified("blocked"), unhandled_response: true}),
    thread("review", {response_classification: classified("review"), unhandled_response: true})];
  c.render(list);
  const board = c.page().querySelector(".ws-attention-board");
  assert.deepEqual(board.children.map(n => n.dataset.section), ["working", "attention", "blocked"]);
  assert.deepEqual(list.map(WorkingSet.columnOf), ["working", "working", "replies", "blocked", "blocked", "blocked", "attention"]);
  assert.equal(c.ids().length, 6);
  assert.equal(board.children[0].querySelectorAll(".ws-card").length, 2);
  assert.equal(board.children[1].querySelectorAll(".ws-card").length, 1);
  assert.equal(board.children[2].querySelectorAll(".ws-card").length, 3);
  c.page().querySelector('[data-action="dismiss"]').onclick(); assert.deepEqual(c.dismissed, ["done"]);
  c.render([{...completed, unread_result: false}]); assert.deepEqual(c.ids(), ["done"]);
  assert(c.page().querySelector('[data-action="dismiss"]'));
  c.render([{...completed, unhandled_response: false}]); assert.deepEqual(c.ids(), []);
  c.selectLevel(2); assert.deepEqual(c.ids(), ["done"]);
});
test("empty attention columns remain visible, and running work takes precedence over prior input", () => {
  const c = setup(); c.render([]);
  assert.equal(c.page().querySelector(".ws-attention-board").children.length, 3);
  assert.equal(WorkingSet.columnOf(thread("resumed", {response_state: "working", checkpoint: {next_actor: "user", next_action: "Old question"}})), "working");
  assert.equal(WorkingSet.columnOf(thread("handled", {work_phase: "finished", unhandled_response: false, unread_result: true})), null);
  assert.equal(WorkingSet.columnOf(thread("classified-done", {response_classification: classified("done"), unhandled_response: true, unread_result: false})), "working");
});
test("classification distinguishes answers from delivery and execution updates, with checkpoint fallbacks", () => {
  const unread = {unread_result: true, unhandled_response: true};
  for (const phase of ["investigation", "planning", "implementation", "deployment", "finished"]) {
    const answer = thread("answer", {...unread, work_phase: phase, response_classification: classified("findings")});
    assert.equal(WorkingSet.levelOf(answer), 1, phase);
    assert.equal(WorkingSet.levelOf({...answer, unread_result: false}), 2, phase);
    const delivery = {...answer, response_classification: classified("done")};
    assert.equal(WorkingSet.levelOf(delivery), 0, phase);
    assert.equal(WorkingSet.levelOf({...delivery, unread_result: false}), 0, phase);
    assert.equal(WorkingSet.levelOf({...delivery, unhandled_response: false}), 2, phase);
    const update = {...answer, response_classification: classified("update")};
    assert.equal(WorkingSet.columnOf(update), "working", phase);
    assert.equal(WorkingSet.levelOf({...update, unread_result: false}), 2, phase);
  }
  for (const status of ["pending", "uncertain", "unavailable"]) {
    const unknown = thread("unknown", {...unread, response_classification: {status, choice: "done"}});
    assert.equal(WorkingSet.levelOf(unknown), 1, status);
    assert.equal(WorkingSet.levelOf({...unknown, work_phase: "finished"}), 0, status);
    assert.equal(WorkingSet.levelOf({...unknown, work_phase: "finished", checkpoint_stale: true}), 1, status);
  }
  assert.equal(WorkingSet.levelOf(thread("missing", unread)), 1);
});
test("classified blockers and required reviews stay above ordinary replies until handled", () => {
  const c = setup();
  for (const choice of ["blocked", "review"]) {
    const t = thread(choice, {unread_result: true, unhandled_response: true, response_classification: classified(choice)});
    c.render([t]); c.selectLevel(0);
    assert.deepEqual(c.ids(), [choice]);
    assert.equal(c.page().querySelector('[data-section="' + (choice === "blocked" ? "blocked" : "attention") + '"]').querySelector(".ws-card").dataset.cardThread, choice);
    c.render([{...t, unread_result: false}]); assert.deepEqual(c.ids(), [choice]);
    c.render([{...t, unread_result: false, unhandled_response: false}]); assert.deepEqual(c.ids(), []);
    c.selectLevel(2); assert.deepEqual(c.ids(), [choice]);
  }
  const request = thread("permission", {status: "needs input", response_state: "input", unhandled_response: false,
    response_classification: classified("findings")});
  assert.equal(WorkingSet.columnOf(request), "blocked");
});
test("a question moves from running to unread reply to idle, while execution updates use Mark read", () => {
  const c = setup(), answer = thread("question", {unread_result: true, unhandled_response: true, response_classification: classified("findings")});
  c.render([{...answer, status: "running", response_state: "working"}]); assert.deepEqual(c.ids(), ["question"]);
  c.render([answer]); assert.deepEqual(c.ids(), []);
  c.selectLevel(1); assert.deepEqual(c.ids(), ["question"]);
  c.page().querySelector('[data-action="read"]').onclick(); assert.deepEqual(c.read, ["question"]);
  c.render([{...answer, unread_result: false}]); assert.deepEqual(c.ids(), []);
  c.selectLevel(2); assert.deepEqual(c.ids(), ["question"]);
  c.render([{...answer, response_classification: classified("update")}]); c.selectLevel(0);
  assert.deepEqual(c.ids(), ["question"]);
  assert(c.page().querySelector('[data-action="read"]'));
  assert.equal(c.page().querySelector('[data-action="dismiss"]'), null);
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
test("pins stay visible at the front of their state without duplicating a thread", () => {
  const c = setup(), list = Array.from({length: 14}, (_, n) => thread(String(n), {created_at: 200 + n}));
  list[0].pinned = true; c.render(list); c.selectLevel(2);
  assert.equal(c.ids()[0], "0"); assert.equal(c.page().querySelectorAll(".ws-pin").length, 1);
});
test("fresh visits start at All groups / Attention; each child appears independently in its own state", () => {
  const c = setup(); c.render([thread("parent"), thread("child", {creation_mode: "orchestration", created_by_thread_id: "parent", unread_result: true}), thread("run", {response_state: "working"})]);
  assert.deepEqual(c.ids(), ["run"]); assert.equal(c.page().querySelector(".ws-page-title").textContent, "All groups");
  c.selectLevel(1); assert.deepEqual(c.ids(), ["child"]);
  c.page().querySelector(".ws-parent").onclick(); assert.deepEqual(c.opened, ["parent"]);
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
  assert.equal(idle.textContent, "2"); assert.equal(idle.title, "A · Idle · 2 idle");
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
  const c = setup(); c.render([thread("a")]); assert(c.key("ArrowDown").prevented); assert.deepEqual(c.ids(), []);
  assert(c.key("ArrowDown").prevented); assert.deepEqual(c.ids(), ["a"]);
  for (const tag of ["input", "textarea", "button", "select", "summary", "a"]) assert(!c.key("ArrowUp", c.doc.createElement(tag)).prevented);
  assert(!c.key("ArrowUp", c.doc.body, {ctrlKey: true}).prevented);
  c.doc.modal = true; assert(!c.key("ArrowUp").prevented); c.doc.modal = false;
  c.enable(false); assert(!c.key("ArrowUp").prevented); c.enable(true);
  c.host.hidden = true; assert(!c.key("ArrowUp").prevented); c.host.hidden = false;
  c.key("g"); assert(c.data.values().some(v => JSON.parse(v).dimension === "domain"));
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
test("horizontal gestures scroll narrow attention columns without navigating groups", () => {
  const c = setup(); c.render([thread("a", {unread_result: true})]);
  const viewport = c.host.querySelector(".ws-viewport"), board = c.page().querySelector(".ws-attention-board");
  board.clientWidth = 300; board.scrollWidth = 760;
  const event = c.event({target: board, deltaX: 180}); viewport.emit("wheel", event);
  assert(!event.prevented); assert.equal(c.page().querySelector(".ws-page-title").textContent, "All groups");
  viewport.emit("touchstart", {target: board, touches: [{clientX: 300, clientY: 100}]});
  viewport.emit("touchend", {changedTouches: [{clientX: 100, clientY: 100}]});
  assert.equal(c.page().querySelector(".ws-page-title").textContent, "All groups");
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
    threadPath: id => "threads/" + id, api: async (...args) => calls.push(args), refreshList: async () => calls.push(["refresh"])};
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
  assert.equal(calls[0][0], "threads/a");
  assert.equal(JSON.stringify(calls[0][1]), JSON.stringify({handled: true, handled_checkpoint_id: 8, handled_completion_id: 12}));
  assert.deepEqual(calls[1], ["refresh"]);
});
