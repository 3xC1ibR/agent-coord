"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {awaitsUser, groupThreads, status, phase, reason, cardAge} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const thread = (id, work_phase = "investigation", extra = {}) => ({thread_id: id, attention: "now", status: "idle",
  work_phase, created_at: 1, response_state: "available", ...extra});
const ids = items => items.map(t => t.thread_id);

test("grouped child needs fold only when their parent is visible", () => {
  const parent = thread("parent", "orchestrating", {needs_attention: true});
  const child = thread("child", "implementation", {needs_attention: true, attention_grouped_under: "parent"});
  assert.deepEqual(ids(groupThreads([parent, child]).priority), ["parent"]);
  assert.deepEqual(ids(groupThreads([child]).priority), ["child"]);
});
const lane = (groups, key) => groups.phases.find(g => g.key === key);

test("all eight lifecycle columns have a fixed order, including empty stages", () => {
  const expected = ["New", "Investigating", "Planning", "Orchestrating", "Implementing", "Validating", "Deploying", "Done"];
  assert.deepEqual(groupThreads([]).phases.map(g => g.label), expected);
  const groups = groupThreads([thread("deploy", "deployment"), thread("findings"), thread("done", "finished")]);
  assert.deepEqual(groups.phases.map(g => g.label), expected);
  assert.deepEqual(ids(lane(groups, "investigation").threads), ["findings"]);
  assert.deepEqual(ids(lane(groups, "finished").threads), ["done"]);
});

test("orchestrating occupies its own column and does not itself require user attention", () => {
  const dispatcher = thread("dispatcher", "orchestrating", {checkpoint:{phase:"orchestrating",next_actor:"external"}});
  const specialist = thread("specialist", "deployment");
  const groups = groupThreads([dispatcher,specialist]);
  assert.deepEqual(ids(lane(groups,"orchestrating").threads), ["dispatcher"]);
  assert.deepEqual(ids(lane(groups,"deployment").threads), ["specialist"]);
  assert.equal(awaitsUser(dispatcher), false);
  assert.equal(status(dispatcher).label, "Orchestrating");
  assert.equal(phase({checkpoint:{phase:"orchestrating"}}), "orchestrating");
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

test("attention uses stakes-first severity order before timestamps, with pins breaking ties within a rank", () => {
  const items = [thread("old-blocker", "deployment", {needs_attention: true, attention_reason: "blocked", attention_since: 10}),
    thread("new-findings", "investigation", {needs_attention: true, attention_reason: "findings", attention_since: 30}),
    thread("new-review", "validation", {needs_attention: true, attention_reason: "review", attention_since: 30})];
  assert.deepEqual(ids(groupThreads(items).priority), ["old-blocker", "new-review", "new-findings"]);
});

test("dismissing findings and success restores the same stage while required actions remain", () => {
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
  assert.deepEqual(ids(groupThreads([newer, older]).priority), ["newer", "older"]);
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
      replaceChildren(...items) {
        for (const item of this.children) item.parent = null;
        this.children = []; this.append(...items);
        // Model the browser clamping an emptied scrolling region during layout.
        this.scrollTop = 0;
        if (this === controls.overview && controls.welcome) controls.welcome.scrollTop = 0;
      },
    };
  }
  const c = {conversationSearch: null, node: element, $: id => controls[id] ||= Object.assign(element("div"), {root: true}),
    document: {activeElement: null}, savedViews: {activeId: "all", render() {}},
    state: {sessions: [thread("one")], selected: null, pinning: new Set(), updatingThreads: new Set(),
      phaseScroll: new Map(), organization: {projects: []}, listSignature: ""},
    threadGrouping: require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js"),
    threadOrganization: require("../plugins/agent-coord/scripts/agent_coord/web/organization.js"),
    threadViews: require("../plugins/agent-coord/scripts/agent_coord/web/views.js").threadViews,
    groupHeading: label => element("h2", label), threadCard: () => element("button"), renderStatus() {}, renderClosedToggle() {},
  };
  c.$("welcome"); c.$("view").value = "active"; c.$("group-by").value = "phase";
  c.threadsInProject = threads => threads; // This fixture supplies one project’s inventory.
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function renderList("), source.indexOf("function conversationEntry(")), c);
  c.renderList();
  return c;
}

test("refreshing an open conversation renders compact sidebar rows and retires the legacy Cards overview board", () => {
  const c = overview(), original = c.threadCard, cards = [];
  c.threadCard = (thread, compact, attention) => {
    cards.push(compact);
    return original(thread, compact, attention);
  };
  c.$("welcome").hidden = true;
  c.state.selected = c.state.sessions[0].thread_id;
  c.renderList();
  assert.ok(cards.includes(true));
  assert.ok(!cards.includes(false), "Cards overview board is retired in favor of List, Focus, and Atlas");
  assert.equal(c.$("overview").children.length, 0);
  assert.equal(typeof c.createOverviewMotion, "undefined");
});

test("changing one inventory row retains the other sidebar button", () => {
  const c = overview(), original = c.threadCard, drawn = [];
  c.threadCard = (row, compact, attention) => {
    if (compact) drawn.push(row.thread_id);
    return original(row, compact, attention);
  };
  c.$("welcome").hidden = true;
  c.state.sessions = [thread("one"), thread("two")];
  c.renderList();
  drawn.length = 0;
  c.state.sessions[1] = {...c.state.sessions[1], title: "Changed"};
  c.renderList();
  assert.deepEqual(drawn, ["two"]);
  c.state.selected = "one";
  c.renderList();
  assert.deepEqual(drawn, ["two", "one"]);
});

test("closing a thread and later polling preserve sidebar scroll positions and results count", () => {
  const c = overview();
  c.state.sessions.push(thread("two")); c.renderList();
  c.$("welcome").scrollTop = 780; c.$("sessions").scrollTop = 320;
  c.state.pendingCloses = new Set(["one"]);
  c.renderList();
  assert.equal(c.$("welcome").scrollTop, 780);
  assert.equal(c.$("sessions").scrollTop, 320);
  assert.match(c.$("results-count").textContent, /^1 thread/);
  c.$("welcome").scrollTop = 910;
  c.state.sessions[1].updated_at = 2; c.renderList();
  assert.equal(c.$("welcome").scrollTop, 910, "polling preserves the user's latest position");
  c.state.pendingCloses.clear(); c.renderList();
  assert.match(c.$("results-count").textContent, /^2 threads/, "rollback restores the closing thread");
  assert.equal(c.$("welcome").scrollTop, 910);
});

test("sidebar Attention group appears only while it contains a thread", () => {
  const c = overview(), attention = () => c.$("sessions").children.filter(el => el.dataset.group === "priority");
  assert.equal(attention().length, 0);
  c.state.sessions[0].response_state = "reply"; c.renderList();
  assert.equal(attention().length, 1);
  c.state.sessions[0].response_state = "available"; c.renderList();
  assert.equal(attention().length, 0);
});
