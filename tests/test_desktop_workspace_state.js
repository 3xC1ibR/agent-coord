"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {create} = require("../desktop/macos/workspace-state.js");
const {ThreadTabs} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-tabs.js");
const {WorkingFolders} = require("../plugins/agent-coord/scripts/agent_coord/web/folders.js");
const {ThreadPanes} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-panes.js");
const ChatImageAttachments = require("../plugins/agent-coord/scripts/agent_coord/web/image-attachments.js");
const {ThreadMentionDraft} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-mentions.js");

class Storage {
  constructor() { this.data = new Map(); }
  get length() { return this.data.size; }
  key(index) { return [...this.data.keys()][index] ?? null; }
  getItem(key) { return this.data.get(key) ?? null; }
  setItem(key, value) { this.data.set(key, String(value)); }
  removeItem(key) { this.data.delete(key); }
}
class Element extends EventTarget {
  constructor() {
    super(); this.value = ""; this.selectionStart = this.selectionEnd = 0; this.scrollTop = 0;
    const classes = new Set();
    this.classList = {contains: key => classes.has(key), add: (...keys) => keys.forEach(key => classes.add(key)),
      remove: (...keys) => keys.forEach(key => classes.delete(key)), toggle: (key, on) => on ? classes.add(key) : classes.delete(key)};
  }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  append() {}
  setAttribute() {}
}
function fixture({snapshot = null, conversation = true, panes = false} = {}) {
  const document = new EventTarget(), window = new EventTarget(), nodes = new Map(), timers = new Map(), saves = [], failures = [];
  let sequence = 0, state, views, calls = [];
  const listeners = new Map(), listen = document.addEventListener.bind(document);
  document.addEventListener = (type, callback, options) => {
    listeners.set(type, [...(listeners.get(type) || []), callback]); listen(type, callback, options);
  };
  for (const name of ["message", "timeline", "welcome", "leave-tiles", "tile-threads", "page-location"]) nodes.set(name, new Element());
  Object.assign(document, {body: new Element(), getElementById: id => nodes.get(id), createElement: () => new Element(), hasFocus: () => false});
  Object.assign(window, {document, sessionStorage: new Storage(), localStorage: new Storage(),
    setTimeout: (fn, delay) => { const id = ++sequence; timers.set(id, {fn, delay}); return id; },
    clearTimeout: id => timers.delete(id)});
  document.defaultView = window;
  const helper = create({window, document, getState: () => state, getViews: () => views,
    getNavigation: () => ({capture: () => ({directory: state?.projectCwd, thread: state?.selected}), remember() {}}),
    onSave: value => saves.push(value), onError: error => failures.push(error),
    select: async (id, options) => { calls.push(["select", id, options]); state.selected = id; if (state.selectFailure) throw new Error("Thread was deleted"); },
    newSession: async () => { calls.push(["newSession"]); state.selected = "new-session"; state.detail = state.newSessionDraft; },
    goHome: () => { calls.push(["goHome"]); state.selected = null; }, render() {}});
  helper.preload(snapshot);
  const workspaceStorage = conversation ? window.sessionStorage : window.localStorage;
  const boot = () => {
    state = {config: {token: "must-not-persist", workspaceRoot: "/work"}, projectCwd: "/work", selected: null,
      drafts: new Map(), groupExpansion: new Map(), phaseScroll: new Map(), transferLocks: new Set(["busy"]),
      timelineScroll: {following: false, position: () => nodes.get("timeline").scrollTop,
        restore: value => { nodes.get("timeline").scrollTop = value; }, latest() { this.following = true; }},
      attachments: new ChatImageAttachments({document, getThread: () => state.selected, canAttach: () => true, onChange() {}}),
      mentions: {drafts: new Map(), restore(id, text, refs) {
        this.drafts.set(id, new ThreadMentionDraft(text, refs));
        const current = state.selected === "new-session" ? state.newSessionDraft?.draftId : state.selected;
        // The real controller renders immediately, validating the visible draft.
        if (id === current) this.snapshot(id, nodes.get("message").value);
      },
        snapshot(id, text) { const entry = this.drafts.get(id); if (!entry) return []; entry.update(text); return entry.snapshot(); }},
      folders: new WorkingFolders({storage: workspaceStorage, key: conversation ? "stable-uuid" : "native-3", scope: "/work"}),
      threadTabs: new ThreadTabs({storage: workspaceStorage, scope: "/work:" + (conversation ? "stable-uuid" : "native-3")})};
    state.threadTabs.setProject("/work");
    views = {cwd: "/work", baseKey: "agent-coord.views./work:native-3", activeId: "all", navigation: {},
      items: [{id: "all"}, {id: "view-two"}], current() { return this.items.find(item => item.id === this.activeId); },
      read: () => ({filters: {search: "typed search"}, group_by: "phase"}), apply(value) { this.applied = value; }, render() {}, restoreScroll() {}};
    if (panes) {
      const shell = window.agentCoordPanes = new ThreadPanes({document, container: new Element(),
        getDraft: id => ({text: state.selected === id ? nodes.get("message").value : state.drafts.get(id) || "",
          images: state.attachments.items(id), mentions: state.mentions.snapshot(id, state.drafts.get(id) || "")}),
        onFocus() {}, onDraft: (id, value) => state.drafts.set(id, value.text)});
      // Rendering is covered by pane tests; exercise its actual state and attach
      // lifecycle here without starting provider-backed iframe documents.
      shell.render = function () { this.slots = this.layout.tabs.map(id => ({id})); };
      shell.position = () => {};
    }
    return state;
  };
  return {window, document, nodes, helper, boot, saves, timers, calls, failures,
    userEvent: (type, target = {}) => { for (const fn of listeners.get(type) || []) fn({type, target, isTrusted: true}); },
    get state() { return state; }, get views() { return views; },
    runTimers() { for (const [id, timer] of [...timers]) if (timer.delay < 15000) { timers.delete(id); timer.fn(); } }};
}
function newDraft(id, cwd, client = "claude") {
  return {draftId: id, session: {cwd, client, model: "model-choice", effort: "high", yolo: true, project_id: "group"},
    work_thread: {thread_id: "new-session", title: "New session", project_cwd: cwd, client}, thread: {turns: []}};
}
function populated() {
  const f = fixture(), s = f.boot();
  s.folders.adopt({cwd: "/work", workspace_id: "work"}); s.folders.adopt({cwd: "/other", workspace_id: "other"}); s.folders.activate("/work");
  s.threadTabs.open({thread_id: "family", title: "Parent", tree_root: "family", tree_open: true});
  s.threadTabs.selectConversation("family", "child");
  s.threadTabs.setProject("/other"); s.threadTabs.open({thread_id: "other", title: "Other"}); s.threadTabs.setProject("/work");
  s.threadTabs.selectConversation("family", "child");
  s.selected = "child"; f.nodes.get("message").value = "Ask @Agent";
  f.nodes.get("message").setSelectionRange(4, 10); f.nodes.get("timeline").scrollTop = 123;
  s.drafts.set("background", "other draft");
  s.mentions.restore("child", "Ask @Agent", [{start: 4, end: 10, text: "@Agent", session_id: "agent"}]);
  s.attachments.drafts.set("child", [{name: "diagram.png", url: "data:image/png;base64,YQ=="}]);
  s.newSessionDraft = newDraft("new-session:7", "/work"); s.draftSequence = 7;
  s.drafts.set("new-session", "Unsent launch");
  s.projectDrafts = new Map([["/other", {detail: newDraft("new-session:6", "/other", "codex"), text: "Other launch"}]]);
  s.projectDestinations = new Map([["/other", {tab: "other", tiled: false}]]);
  s.conversationPositions = new Map([["background", {scroll: 444, following: false}]]);
  s.groupExpansion.set("open-group", true); s.phaseScroll.set("working", 17);
  f.window.sessionStorage.setItem("timeline-disclosures:child", '["thought-1"]');
  f.window.sessionStorage.setItem("agent-coord.project-scratchpad-draft:/work", "unsaved notes");
  f.window.sessionStorage.setItem("agent-coord.notifications.focus", "ephemeral");
  f.window.sessionStorage.setItem("authentication", "secret");
  f.window.sessionStorage.setItem("dispatch-request:child", '{"signature":"123:456","id":"durable-request"}');
  f.views.activeId = "view-two"; f.views.navigation = {"view-two": {filters: {search: "typed search"}, scroll: 26}};
  f.helper.ready();
  return f;
}

test("native window round trip preloads auxiliary folders/tabs and recovers aliases, drafts, views and reading positions", async () => {
  const first = populated(), snapshot = first.helper.capture();
  const f = fixture({snapshot}), s = f.boot();
  assert.deepEqual(s.folders.projects.map(item => item.cwd), ["/work", "/other"]);
  assert.equal(s.folders.selected.cwd, "/work");
  assert.equal(s.threadTabs.conversationFor("family"), "child");
  assert.equal(s.threadTabs.destination(), "family");
  s.selected = "child"; f.helper.ready();
  assert.equal(await f.helper.restore(snapshot), true);
  assert.deepEqual(f.calls, [], "startup already selected the route; restoring does not open providers again");
  assert.equal(f.nodes.get("message").value, "Ask @Agent");
  assert.equal(f.nodes.get("message").selectionStart, 4);
  assert.equal(s.drafts.get("background"), "other draft");
  assert.equal(s.attachments.items("child")[0].url, "data:image/png;base64,YQ==");
  assert.equal(s.mentions.snapshot("child", "Ask @Agent")[0].session_id, "agent");
  assert.equal(s.threadTabs.selected, "family");
  assert.equal(s.threadTabs.projects.get("/other")[0].thread_id, "other");
  assert.equal(s.projectDrafts.get("/other").detail.session.client, "codex");
  assert.equal(s.newSessionDraft.session.model, "model-choice");
  assert.equal(s.newSessionDraft.session.yolo, true); assert.equal(s.draftSequence, 7);
  assert.equal(f.views.activeId, "view-two"); assert.equal(f.views.applied.filters.search, "typed search");
  assert.equal(f.nodes.get("timeline").scrollTop, 123);
  assert.equal(s.conversationPositions.get("background").scroll, 444);
  assert.equal(s.groupExpansion.get("open-group"), true); assert.equal(s.phaseScroll.get("working"), 17);
  assert.equal(f.window.sessionStorage.getItem("timeline-disclosures:child"), '["thought-1"]');
  const encoded = JSON.stringify(snapshot);
  assert.equal(f.window.sessionStorage.getItem("dispatch-request:child"), '{"signature":"123:456","id":"durable-request"}');
  for (const excluded of ["must-not-persist", "transferLocks", "ephemeral", "secret"]) assert.ok(!encoded.includes(excluded), excluded);
});

test("active new-session metadata and full images survive without creating or sending a session", async () => {
  const first = populated(); first.state.selected = "new-session";
  first.nodes.get("message").value = "Plan @Agent";
  first.state.threadTabs.open(first.state.newSessionDraft.work_thread);
  const url = "data:image/png;base64," + "YQYQ".repeat(65536);
  first.state.attachments.drafts.set("new-session:7", Array.from({length: 4}, (_, i) => ({name: i + ".png", url})));
  first.state.mentions.restore("new-session:7", "Plan @Agent", [{start: 5, end: 11, text: "@Agent", session_id: "agent"}]);
  const snapshot = first.helper.capture(), f = fixture({snapshot}), s = f.boot();
  // Startup's route may have already opened a provisional default composer.
  s.selected = "new-session"; s.newSessionDraft = newDraft("new-session:1", "/work", "codex");
  f.helper.ready(); await f.helper.restore(snapshot);
  assert.equal(s.detail.session.client, "claude"); assert.equal(s.detail.session.effort, "high");
  assert.equal(f.nodes.get("message").value, "Plan @Agent");
  assert.equal(s.attachments.items("new-session:7").length, 4);
  assert.equal(s.attachments.items("new-session:7")[3].url, url);
  assert.equal(s.mentions.snapshot("new-session:7", "Plan @Agent")[0].session_id, "agent");
  assert.equal(s.threadTabs.items.at(-1).thread_id, "new-session");
  assert.deepEqual(f.calls, []);
});

test("quit during async boot keeps the injected snapshot and restoration never erases newer text or selection", async () => {
  const snapshot = populated().helper.capture(), f = fixture({snapshot});
  assert.deepEqual(f.helper.capture(), snapshot); f.runTimers(); assert.equal(f.saves.length, 0);
  const s = f.boot(); s.selected = "child";
  f.nodes.get("message").value = "edited while booting";
  const edit = new Event("input"); Object.defineProperty(edit, "target", {value: {id: "message"}});
  f.document.dispatchEvent(edit);
  assert.equal(new Map(f.helper.capture().drafts).get("child"), "edited while booting");
  f.helper.ready(); f.helper.storageChanged("agent-coord.thread-tabs.test"); f.runTimers();
  assert.equal(f.saves.length, 0);
  await f.helper.restore(snapshot);
  assert.equal(f.nodes.get("message").value, "edited while booting");
  assert.equal(s.drafts.get("child"), "edited while booting");
  f.nodes.get("message").value = "after restoration";
  await f.helper.restore(snapshot);
  assert.equal(f.nodes.get("message").value, "after restoration", "duplicate native delivery is idempotent");
  f.runTimers(); assert.equal(f.saves.length, 1);
});

test("main windows capture only their scoped local storage and ignore unrelated window preferences", () => {
  const f = fixture({conversation: false}), s = f.boot();
  s.folders.adopt({cwd: "/work"}); s.threadTabs.open({thread_id: "one", title: "One"});
  f.window.localStorage.setItem("agent-coord.thread-tabs./work:native-9:/work", "other window");
  f.window.localStorage.setItem("agent-coord.notifications.focus", "ephemeral");
  f.helper.ready();
  const snapshot = f.helper.capture();
  assert.ok(snapshot.localStorage[s.folders.key]);
  assert.ok(snapshot.localStorage[s.threadTabs.baseKey + ":/work"]);
  assert.equal(Object.keys(snapshot.localStorage).length, 2);
});

test("Send work composers retain per-project text and creation settings without transient send state", async () => {
  const first = populated();
  first.window.agentCoordSendWork = {cwd: "/work", input: {value: "route this later"}, draft: {client: "claude", model: "chosen", effort: "high", yolo: false},
    locked: {id: "new", kind: "new", title: "New conversation"}, sending: true,
    drafts: new Map([["/other", {message: "another project", draft: {client: "codex"}, locked: null}]])};
  const snapshot = first.helper.capture(), f = fixture({snapshot}); f.boot();
  f.window.agentCoordSendWork = {cwd: "/work", input: {value: ""}, drafts: new Map(), render() {}};
  f.helper.ready(); await f.helper.restore();
  const palette = f.window.agentCoordSendWork;
  assert.equal(palette.input.value, "route this later"); assert.equal(palette.draft.model, "chosen");
  assert.equal(palette.locked.kind, "new"); assert.equal(palette.drafts.get("/other").message, "another project");
  assert.ok(!JSON.stringify(snapshot.sendWorkDrafts).includes("sending"));
});

test("split panes retain nested geometry, hidden tabs, focus and deferred drafts through real pane attachment", async () => {
  const first = fixture({panes: true}), s = first.boot(), shell = first.window.agentCoordPanes;
  s.drafts.set("a", "draft a"); s.drafts.set("b", "draft b");
  shell.record({thread_id: "a", title: "A"}); shell.record({thread_id: "b", title: "B"});
  shell.layouts.set("/work:all", {presentation: "tabs", columns: [1, 1], groups: [["a"], ["b"]], tabs: ["a", "b"],
    selected: "b", order: ["a", "b"], maximized: null, tree: {axis: "horizontal", sizes: [3, 2], children: [{slot: 0}, {slot: 1}]}});
  shell.active = true; shell.viewId = "/work:all";
  shell.records.get("b").api = {exportDraft: () => ({text: "pane edit", images: [], mentions: [], scroll: 75, following: false})};
  first.helper.ready(); const snapshot = first.helper.capture();
  const f = fixture({snapshot, panes: true}); f.boot(); f.helper.ready();
  const target = f.window.agentCoordPanes, frame = {contentWindow: {}, contentDocument: f.document};
  target.record({thread_id: "b", title: "B"}).frame = frame;
  let settled = false;
  const restoring = f.helper.restore(snapshot).then(() => { settled = true; });
  await Promise.resolve(); assert.equal(settled, false, "visible pane drafts are awaited");
  let recovered;
  target.attach(frame.contentWindow, {restoreDraft: value => { recovered = value; }, setActive() {}});
  await restoring;
  assert.equal(target.selected, "b"); assert.deepEqual(target.layout.tree.sizes, [3, 2]);
  assert.equal(target.records.get("a").initialDraft.text, "draft a");
  assert.equal(recovered.text, "pane edit"); assert.equal(recovered.scroll, 75);
  assert.ok(f.document.body.classList.contains("conversation-split"));
  assert.deepEqual(f.calls, []);
});

test("an inactive pane cannot overwrite a newer draft in the normal conversation", () => {
  const f = fixture({panes: true}), s = f.boot(), shell = f.window.agentCoordPanes;
  s.selected = "a"; f.nodes.get("message").value = "new main draft";
  shell.record({thread_id: "a", title: "A"}).api = {exportDraft: () => ({text: "stale pane draft", images: [], mentions: []})};
  f.helper.ready(); const snapshot = f.helper.capture();
  assert.equal(new Map(snapshot.drafts).get("a"), "new main draft");
  assert.equal(new Map(snapshot.panes.records).get("a").draft.text, "new main draft");
});

test("saves debounce edits, ignore streaming pane notifications and flush post-send draft deletion", () => {
  const f = fixture(), s = f.boot(); f.helper.ready(); f.runTimers();
  const initial = f.saves.length;
  for (let i = 0; i < 100; i++) f.window.dispatchEvent(new Event("agent-coord-pane-state"));
  assert.equal(f.timers.size, 0); assert.equal(f.saves.length, initial);
  for (let i = 0; i < 10; i++) { s.drafts.set("a", String(i)); f.document.dispatchEvent(new Event("input")); }
  assert.equal(f.timers.size, 1); f.runTimers(); assert.equal(f.saves.length, initial + 1);
  s.drafts.delete("a"); f.runTimers(); assert.equal(f.saves.length, initial + 2);
  assert.ok(!new Map(f.saves.at(-1).drafts).has("a"));
});

test("non-workspace pages and incomplete legacy snapshots remain safe", async () => {
  const f = fixture(); assert.deepEqual(f.helper.capture(), {version: 1, sessionStorage: {}});
  assert.equal(await f.helper.restore({}), false);
  const legacy = fixture({snapshot: {version: 0, panes: {layouts: [["bad", {groups: [null]}]]}}});
  legacy.boot(); legacy.helper.ready(); assert.equal(await legacy.helper.restore(), true);
  assert.deepEqual(legacy.failures, []);
});

test("focus clicks keep restoration while actual navigation or composer edits take precedence", async () => {
  const snapshot = populated().helper.capture();
  const focus = fixture({snapshot}); focus.boot(); focus.helper.ready();
  focus.userEvent("click", {id: "message", closest: () => null});
  await focus.helper.restore(); assert.equal(focus.state.selected, "child");
  assert.equal(focus.nodes.get("message").value, "Ask @Agent");
  const navigated = fixture({snapshot}); navigated.boot(); navigated.state.selected = "different"; navigated.helper.ready();
  navigated.userEvent("click", {closest: () => ({dataset: {threadTab: "different"}})});
  await navigated.helper.restore(); assert.equal(navigated.state.selected, "different");
  assert.deepEqual(navigated.calls, []);
  assert.equal(navigated.state.drafts.get("child"), "Ask @Agent", "navigation does not discard unrelated saved drafts");
});

test("a deleted selected thread falls back safely and later edits continue to save", async () => {
  const snapshot = populated().helper.capture(), f = fixture({snapshot}), s = f.boot();
  s.selectFailure = true; f.helper.ready();
  assert.equal(await f.helper.restore(), true);
  assert.equal(s.selected, null); assert.equal(s.drafts.get("child"), "Ask @Agent");
  assert.match(f.failures[0].message, /deleted/);
  s.drafts.set("child", "recovered draft edited later"); f.runTimers();
  assert.equal(new Map(f.saves.at(-1).drafts).get("child"), "recovered draft edited later");
});

test("a failed initial route retains its draft when leaving the broken conversation", async () => {
  const snapshot = populated().helper.capture(), f = fixture({snapshot}), s = f.boot();
  s.selected = "child"; s.conversations = new Map([["child", {refreshError: "Thread was deleted"}]]);
  f.helper.ready(); await f.helper.restore();
  assert.equal(s.selected, null); assert.equal(s.drafts.get("child"), "Ask @Agent");
  f.runTimers(); assert.equal(f.saves.length, 1);
});

test("a pane attach timeout retains its deferred draft and does not freeze subsequent saves", async () => {
  const snapshot = populated().helper.capture();
  snapshot.panes = {active: true, viewId: "/work:all", layouts: [["/work:all", {columns: [1], groups: [["a"]], tabs: ["a"], order: ["a"], selected: "a"}]],
    records: [["a", {thread: {thread_id: "a"}, draft: {text: "pane draft", images: []}}]]};
  const f = fixture({snapshot, panes: true}); f.boot(); f.helper.ready();
  f.window.agentCoordPanes.record({thread_id: "a"}).frame = {contentWindow: {}};
  const restoring = f.helper.restore();
  for (const [id, timer] of [...f.timers]) if (timer.delay === 15000) { f.timers.delete(id); timer.fn(); }
  assert.equal(await restoring, true);
  assert.equal(f.window.agentCoordPanes.records.get("a").initialDraft.text, "pane draft");
  f.state.drafts.set("other", "new work"); f.runTimers();
  assert.equal(new Map(f.saves.at(-1).drafts).get("other"), "new work");
  assert.match(f.failures[0].message, /still loading/);
});

test("native quit can wait for local sends and unfinished image reads", () => {
  const f = fixture(), s = f.boot(); f.helper.ready();
  assert.equal(f.helper.busy(), false);
  s.detail = {running: true, requests: [{method: "approval"}]};
  assert.equal(f.helper.busy(), false, "model work and approvals do not block interface quit");
  s.sending = new Set(["child"]); assert.equal(f.helper.busy(), true); s.sending.clear();
  s.attachments.drafts.set("child", [{name: "pending.png", url: null}]); assert.equal(f.helper.busy(), true);
  s.attachments.drafts.get("child")[0].url = "data:image/png;base64,YQ=="; assert.equal(f.helper.busy(), false);
});
