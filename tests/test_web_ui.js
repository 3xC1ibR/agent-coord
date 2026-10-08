"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {awaitsUser, groupThreads, status, phase, reason, cardAge} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const thread = (id, work_phase = "investigation", extra = {}) => ({thread_id: id, attention: "now", status: "idle",
  work_phase, created_at: 1, response_state: "available", ...extra});
const ids = items => items.map(t => t.thread_id);
const lane = (groups, key) => groups.phases.find(g => g.key === key);

test("all seven lifecycle columns have a fixed order, including empty stages", () => {
  const expected = ["Getting started", "Investigating", "Planning", "Implementing", "Validating", "Deploying", "Done"];
  assert.deepEqual(groupThreads([]).phases.map(g => g.label), expected);
  const groups = groupThreads([thread("deploy", "deployment"), thread("findings"), thread("done", "finished")]);
  assert.deepEqual(groups.phases.map(g => g.label), expected);
  assert.deepEqual(ids(lane(groups, "investigation").threads), ["findings"]);
  assert.deepEqual(ids(lane(groups, "finished").threads), ["done"]);
});

test("blocked deployment outranks requested review, healthy rollout, findings, and routine success", () => {
  const items = [thread("success", "finished", {attention_reason: "done", response_state: "update", pinned: true}),
    thread("findings", "investigation", {attention_reason: "findings", response_state: "update"}),
    thread("healthy", "deployment", {attention_reason: "update", response_state: "update"}),
    thread("review", "validation", {attention_reason: "review", response_state: "input"}),
    thread("credentials", "deployment", {attention_reason: "blocked", response_state: "input"})];
  const groups = groupThreads(items);
  assert.deepEqual(ids(groups.priority), ["credentials", "review", "healthy", "findings", "success"]);
  assert.equal(lane(groups, "deployment").attention, 2);
  assert.equal(lane(groups, "finished").attention, 1);
  assert.equal(groups.phases.flatMap(g => g.threads).length, 0);
  assert.equal(reason(items[0]).label, "✓ Done");
});

test("reading findings and success restores the same stage while required actions remain", () => {
  const items = [thread("findings", "investigation", {response_state: "update", needs_attention: true}),
    thread("success", "finished", {response_state: "update", needs_attention: true}),
    thread("blocker", "deployment", {response_state: "input", needs_attention: true})];
  for (const t of items.slice(0, 2)) { t.response_state = "available"; t.needs_attention = false; t.unread = false; }
  items[2].unread = false;
  const groups = groupThreads(items);
  assert.deepEqual(ids(groups.priority), ["blocker"]);
  assert.deepEqual(ids(lane(groups, "investigation").threads), ["findings"]);
  assert.deepEqual(ids(lane(groups, "finished").threads), ["success"]);
  assert.equal(phase({work_phase: "investigation", checkpoint: {phase: "finished"}}), "investigation");
  assert.equal(phase({checkpoint: {phase: "discussion"}}), "investigation");
});

test("stage peers sort by recent activity while attention retains its priority order", () => {
  const older = thread("older", "implementation", {created_at: 1, updated_at: 900});
  const newer = thread("newer", "implementation", {created_at: 2, updated_at: 800});
  for (const input of [[newer, older], [older, newer]]) {
    assert.deepEqual(ids(lane(groupThreads(input), "implementation").threads), ["older", "newer"]);
    older.updated_at++;
  }
  for (const t of [older, newer]) { t.response_state = "reply"; t.attention_since = 5; }
  assert.deepEqual(ids(groupThreads([newer, older]).priority), ["older", "newer"]);
  newer.pinned = true;
  assert.deepEqual(ids(groupThreads([newer, older]).priority), ["older", "newer"]);
});

test("every stage sorts running first then latest activity, regardless of pin or creation time", () => {
  for (const stage of groupThreads([]).phases.map(g => g.key)) {
    const items = [thread("pinned", stage, {pinned: true, updated_at: 300}),
      thread("newest", stage, {updated_at: 400}),
      thread("running-old", stage, {response_state: "working", updated_at: 100}),
      thread("running-new", stage, {status: "running", updated_at: 200}),
      thread("created", stage, {created_at: 350})];
    assert.deepEqual(ids(lane(groupThreads(items), stage).threads),
      ["running-new", "running-old", "newest", "created", "pinned"]);
    items[0].updated_at = 500;
    assert.deepEqual(ids(lane(groupThreads(items.reverse()), stage).threads),
      ["running-new", "running-old", "pinned", "newest", "created"]);
  }
});

test("card aging follows last activity with exact six-hour and one-day boundaries", () => {
  const now = 200000, card = thread("aging", "implementation", {updated_at: now});
  for (const [seconds, expected] of [[0, "fresh"], [21599, "fresh"], [21600, "settled"],
      [86399, "settled"], [86400, "old"], [172800, "old"]]) {
    card.updated_at = now - seconds;
    assert.equal(cardAge(card, now), expected);
  }
  card.updated_at = now;
  assert.equal(cardAge(card, now), "fresh");
  assert.equal(cardAge(thread("missing", "new", {created_at: null}), now), "fresh");
  assert.equal(cardAge(thread("future", "new", {created_at: now + 10}), now), "fresh");
  assert.equal(cardAge(thread("creation", "new", {created_at: now - 86400}), now), "old");
});

test("running, pinned, and required-action cards stay expanded regardless of age", () => {
  for (const extra of [{response_state: "working"}, {status: "running"}, {pinned: true},
    {response_state: "input"}, {response_state: "action"}, {response_state: "failed"},
    {checkpoint: {next_actor: "user", next_action: "Approve rollout"}}]) {
    assert.equal(cardAge(thread("protected", "deployment", extra), 200000), "fresh");
  }
  assert.equal(cardAge(thread("stale-action", "deployment", {checkpoint_stale: true,
    checkpoint: {next_actor: "user", next_action: "Old request"}}), 200000), "old");
});

test("Later and Closed stay out of the current board and queue, preserving all context", () => {
  const now = thread("now"), later = thread("later", "deployment", {attention: "later", response_state: "input", pinned: true, unread: true});
  const closed = thread("closed", "finished", {attention: "archived", pinned: true});
  const input = [now, later, closed], before = JSON.stringify(input), groups = groupThreads(input);
  assert.deepEqual(groups.priority, []);
  assert.deepEqual(ids(groups.later), ["later"]);
  assert.deepEqual(ids(groups.closed), ["closed"]);
  assert.deepEqual(ids(groups.phases.flatMap(g => g.threads)), ["now"]);
  assert.equal(JSON.stringify(input), before);
  later.attention = "now";
  assert.equal(awaitsUser(later), true);
  assert.equal(later.unread, true);
});

test("unclassified replies remain visible after reading; running and interrupted turns are separate", () => {
  const reply = thread("unknown", "new", {response_state: "reply", unread: false});
  const working = thread("work", "implementation", {response_state: "working", unread: true});
  const stopped = thread("stopped", "implementation", {response_state: "interrupted"});
  assert.deepEqual(ids(groupThreads([reply, working, stopped]).priority), ["unknown"]);
  assert.equal(status(working).key, "running");
  assert.equal(status(stopped).label, "Stopped");
});

test("placement, pin, phase, and response transitions never drop or duplicate a thread", () => {
  for (const attention of ["now", "later", "archived"]) for (const response_state of ["working", "input", "update", "available", "reply"]) {
    const moving = thread("moving", "implementation", {attention, response_state, pinned: true});
    const groups = groupThreads([moving, thread("unknown", "future-phase")]);
    const shown = [...groups.priority, ...groups.phases.flatMap(g => g.threads), ...groups.later, ...groups.closed];
    assert.equal(shown.length, 2);
    assert.equal(new Set(ids(shown)).size, 2);
  }
});

function overview() {
  const vm = require("node:vm"), fs = require("node:fs");
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const controls = {};
  function element(tag, text, className = "") {
    return {tag, textContent: text, className, children: [], dataset: {}, value: "", hidden: false, scrollLeft: 0,
      get isConnected() { return this.root || Boolean(this.parent?.isConnected); },
      get clientWidth() { return this.isConnected && !controls.welcome.hidden ? 346 : 0; },
      setAttribute() {},
      append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } },
      replaceChildren(...items) { for (const item of this.children) item.parent = null; this.children = []; this.append(...items); },
    };
  }
  const c = {node: element, $: id => controls[id] ||= Object.assign(element("div"), {root: true}),
    document: {activeElement: null}, savedViews: {activeId: "all", render() {}},
    state: {sessions: [thread("one")], selected: null, pinning: new Set(), updatingThreads: new Set(),
      phaseScroll: new Map(), organization: {projects: []}, listSignature: "",
      overviewMotion: {capture() {}, play() {}}},
    threadGrouping: require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js"),
    threadOrganization: require("../plugins/agent-coord/scripts/agent_coord/web/organization.js"),
    threadViews: require("../plugins/agent-coord/scripts/agent_coord/web/views.js").threadViews,
    groupHeading: label => element("h2", label), threadCard: () => element("button"), renderStatus() {}, renderClosedToggle() {},
  };
  c.$("welcome"); c.$("view").value = "active"; c.$("group-by").value = "phase";
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function renderList("), source.indexOf("async function select(")), c);
  c.board = () => c.$("overview").children.find(el => el.className === "phase-board");
  c.renderList();
  return c;
}

test("mobile board keeps its horizontal position when a checkpoint refresh rebuilds cards", () => {
  const c = overview(), oldBoard = c.board();
  oldBoard.scrollLeft = 930; oldBoard.onscroll();
  c.state.sessions[0].checkpoint = {summary: "Investigation updated"}; c.renderList();
  assert.notEqual(c.board(), oldBoard);
  assert.equal(c.board().scrollLeft, 930);
  oldBoard.scrollLeft = 0; oldBoard.onscroll(); // A queued event from the detached board must not reset it.
  c.state.sessions[0].updated_at = 2; c.renderList();
  assert.equal(c.board().scrollLeft, 930);
});

test("opening a thread and refreshing its hidden overview does not forget the mobile column", () => {
  const c = overview();
  c.board().scrollLeft = 620; c.board().onscroll();
  c.$("welcome").hidden = true; c.state.selected = "one"; c.renderList();
  c.board().scrollLeft = 0; c.board().onscroll();
  c.state.sessions[0].updated_at = 2; c.renderList();
  c.$("welcome").hidden = false; c.state.selected = null; c.renderList();
  assert.equal(c.board().scrollLeft, 620);
});

test("saved views retain independent horizontal positions even with identical filters", () => {
  const c = overview();
  c.board().scrollLeft = 310; c.board().onscroll();
  c.savedViews.activeId = "feature"; c.renderList();
  assert.equal(c.board().scrollLeft, 0);
  c.board().scrollLeft = 1240; c.board().onscroll();
  c.savedViews.activeId = "all"; c.renderList();
  assert.equal(c.board().scrollLeft, 310);
  c.savedViews.activeId = "feature"; c.renderList();
  assert.equal(c.board().scrollLeft, 1240);
});

test("Attention appears only while it contains a thread and never hides the lifecycle board", () => {
  const c = overview(), attention = () => c.$("overview").children.filter(el => el.className === "attention-panel");
  assert.equal(attention().length, 0);
  assert.equal(c.board().children.length, 7);
  c.state.sessions[0].response_state = "reply"; c.renderList();
  assert.equal(attention().length, 1);
  assert.equal(c.board().children.length, 7);
  c.state.sessions[0].response_state = "available"; c.renderList();
  assert.equal(attention().length, 0);
  assert.equal(c.board().children.length, 7);
});

function motionHarness() {
  const vm = require("node:vm"), fs = require("node:fs");
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const animations = [], copies = [], listeners = {}, reduced = {matches: false, addEventListener(_, fn) { this.change = fn; }};
  let items = [];
  function card(id, group, left, top = 100) {
    const rect = {left, top, width: 200, height: 120, right: left + 200, bottom: top + 120};
    const el = {dataset: {cardThread: id}, group, rect, style: {setProperty() {}},
      closest(selector) { return selector === "[data-group]" ? {dataset: {group: this.group}} : null; },
      getBoundingClientRect() { return this.rect; }, querySelectorAll() { return []; },
      cloneNode() { return card(id, group, left, top); }, removeAttribute() {}, setAttribute() {},
      remove() { this.removed = true; },
      animate(frames, timing) {
        const animation = {el: this, frames, timing, cancel() { this.cancelled = true; }};
        animations.push(animation); return animation;
      },
    };
    return el;
  }
  const welcome = {hidden: false};
  const c = {window: {matchMedia: () => reduced, innerHeight: 900, innerWidth: 1400,
    addEventListener: (name, fn) => { listeners[name] = fn; }}, Element: {prototype: {animate() {}}},
    document: {body: {append(el) { copies.push(el); }}}, getComputedStyle: () => [],
    $: id => id === "welcome" ? welcome : {querySelectorAll: () => items},
  };
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function createOverviewMotion("), source.indexOf("function renderList(")), c);
  const motion = c.createOverviewMotion();
  function render(next, view = "all") { const before = motion.capture(view); items = next; motion.play(before); }
  return {render, card, animations, copies, listeners, reduced, welcome};
}

test("section moves glide above the layout, settle neighbors, and clean up on finish", () => {
  const h = motionHarness();
  h.render([h.card("moving", "planning", 10), h.card("peer", "implementation", 300, 100)]);
  assert.equal(h.animations.length, 0);
  h.render([h.card("moving", "implementation", 300), h.card("peer", "implementation", 300, 240)]);
  assert.equal(h.copies.length, 1);
  assert.equal(h.copies[0].inert, true);
  assert.equal(h.copies[0].style.position, "fixed");
  assert.equal(h.animations[0].frames[0].transform, "translate(-290px, 0px) scale(1, 1)");
  assert.equal(h.animations[3].frames[0].transform, "translate(0px, -140px)");
  for (const animation of h.animations) animation.onfinish?.();
  assert.equal(h.copies[0].removed, true);
  assert.ok(h.animations.every(animation => animation.cancelled));
});

test("motion skips metadata changes, view switches, hidden boards, and reduced motion", () => {
  for (const scenario of ["metadata", "view", "hidden", "reduced"]) {
    const h = motionHarness();
    h.render([h.card("one", "planning", 10)]);
    h.welcome.hidden = scenario === "hidden";
    h.reduced.matches = scenario === "reduced";
    h.render([h.card("one", scenario === "metadata" ? "planning" : "priority", 300)], scenario === "view" ? "other" : "all");
    assert.equal(h.animations.length, 0, scenario);
  }
});

test("interrupted movement continues from its visible position and removes previous copies", () => {
  const h = motionHarness();
  h.render([h.card("one", "planning", 10)]);
  h.render([h.card("one", "implementation", 300)]);
  h.copies[0].rect = {...h.copies[0].rect, left: 150, right: 350};
  h.render([h.card("one", "implementation", 300)]);
  assert.equal(h.copies[0].removed, true);
  assert.equal(h.animations[3].frames[0].transform, "translate(-150px, 0px) scale(1, 1)");
  h.listeners.scroll();
  assert.ok(h.copies.every(copy => copy.removed));
  assert.ok(h.animations.every(animation => animation.cancelled));
});

test("offscreen cards do not fly across the viewport and motion preference changes cancel copies", () => {
  const h = motionHarness();
  h.render([h.card("one", "planning", -210)]);
  h.render([h.card("one", "implementation", 300)]);
  assert.equal(h.animations.length, 0);
  h.render([h.card("one", "priority", 10)]);
  h.reduced.matches = true; h.reduced.change();
  assert.ok(h.copies.every(copy => copy.removed));
  assert.ok(h.animations.every(animation => animation.cancelled));
});
