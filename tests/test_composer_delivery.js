"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/outgoing-messages.js"), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function environment() {
  const rows = [], effects = [], acknowledged = [], recovered = [], reads = [];
  const discovery = deferred(), input = {value: ""};
  const state = {config: {ordinarySubmissionReceipts: 1}, selected: "other", drafts: new Map(),
    outgoing: {list: id => rows.filter(row => row.id === id), add(id, echo) { rows.push({...echo, id}); return rows.at(-1); },
      accepted(echo) {echo.status = "sent";}, rejected(echo) {echo.status = "failed";}},
    durableDrafts: {pending(id) {reads.push(id); return id === "one" ? discovery.promise : Promise.resolve([]);},
      stage: async () => {}, acknowledge: async (id, key) => {acknowledged.push(key);},
      recover: async (id, key) => {recovered.push(key); return true;}}};
  const context = vm.createContext({state, document: {}, $: () => input, renderStatus() {}, showError() {},
    api: async (path, body) => {effects.push(body.requestId); return {};}, setTimeout, clearTimeout});
  vm.runInContext(source + "\nstate.submissionQueue = new ConversationSubmissionQueue();", context);
  function send(id, requestId) {
    return context.submitComposerMessage({id, requestId, text: requestId, mentions: [], images: [],
      path: "sessions/" + id + "/messages", body: {message: requestId}, clear() {input.value = "";}});
  }
  return {context, state, rows, effects, acknowledged, recovered, discovery, input, reads, send};
}

test("a restored submission reserves its original position before new Enter, while another thread sends", async () => {
  const f = environment(), first = deferred();
  f.context.api = async (path, body) => {
    f.effects.push(body.requestId);
    if (body.requestId === "old" && f.effects.filter(id => id === "old").length === 1) return first.promise;
    return {};
  };
  f.input.value = "new";
  const newer = f.send("one", "new");
  assert.equal(f.input.value, "");
  assert.equal(f.rows[0].text, "new");
  await f.send("two", "independent");
  assert.deepEqual(f.effects, ["independent"]);
  const mounting = f.context.discoverComposerSubmissions("one");
  f.discovery.resolve([{requestId: "old", text: "old", images: [], mentions: [],
    path: "sessions/one/queue", body: {requestId: "old"}}]);
  await mounting;
  await tick();
  first.reject(Object.assign(new Error("Ambiguous"), {status: 409, code: "submission_outcome_unknown"}));
  await tick();
  assert.deepEqual(f.effects, ["independent", "old"]);
  assert.deepEqual(f.recovered, []);
  const old = f.rows.find(row => row.submissionId === "old");
  assert.equal(old.status, "uncertain");
  await old.retry();
  await newer;
  assert.deepEqual(f.effects, ["independent", "old", "old", "new"]);
  assert.deepEqual(f.acknowledged, ["independent", "old", "new"]);
  assert.deepEqual(f.reads, ["one", "two"]);
});

test("predecessor HTTP 400 uncertainty retains the original identity until authenticated reconciliation", async () => {
  const f = environment();
  f.discovery.resolve([]);
  f.context.api = async (path, body) => {
    f.effects.push(body.requestId);
    if (body.requestId === "first") throw Object.assign(new Error("This submission has an unconfirmed outcome. Check the conversation before sending again; it has not been replayed."), {status: 400});
    return {};
  };
  const first = f.send("one", "first"), second = f.send("one", "second");
  await assert.rejects(first, {submissionUnconfirmed: true});
  await tick();
  assert.deepEqual(f.effects, ["first"]);
  assert.equal(f.recovered.length, 0);
  f.state.submissionQueue.confirm("one", "first");
  await second;
  assert.deepEqual(f.effects, ["first", "second"]);
});

test("the local write barrier includes queued image staging and completes while provider acceptance is pending", async () => {
  const f = environment(), writing = deferred(), provider = deferred();
  f.discovery.resolve([]);
  f.context.api = () => provider.promise;
  f.context.writeComposerRecord("one", () => writing.promise);
  const delivery = f.send("one", "pending");
  let staged = false, drained = false;
  f.state.durableDrafts.stage = async () => {staged = true;};
  const barrier = f.context.drainComposerWrites().then(() => {drained = true;});
  await tick(); assert.equal(drained, false); assert.equal(staged, false);
  writing.resolve();
  await barrier;
  assert.equal(staged, true);
  provider.resolve({}); await delivery;
});

const app = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
function section(text, start, end) {
  const first = text.indexOf(start), last = text.indexOf(end, first + start.length);
  assert.ok(first >= 0 && last > first, start);
  return text.slice(first, last);
}

test("later discovery reserves a previously staged echo after a transient journal read failure", async () => {
  const f = environment(), journal = new Map();
  let attempts = 0;
  f.state.durableDrafts.pending = async () => {
    if (!attempts++) throw new Error("Temporary read error");
    return [...journal.values()];
  };
  f.state.durableDrafts.stage = async (id, key, row) => journal.set(key, row);
  f.state.durableDrafts.acknowledge = async (id, key) => journal.delete(key);
  await assert.rejects(f.send("one", "first"), /Temporary read/);
  await f.context.drainComposerWrites();
  assert.ok(journal.has("first"));
  await f.send("one", "second");
  assert.deepEqual(f.effects, ["first", "second"]);
  assert.equal(journal.size, 0);
});

test("identical transcript text cannot consume an unresolved request before its authenticated receipt", () => {
  const context = vm.createContext({});
  const Outgoing = vm.runInContext(section(app, "class OutgoingMessages {", "// The normal provider stream") + "; OutgoingMessages", context);
  const messages = new Outgoing({itemText: item => item.content[0].text});
  const keys = [], receipt = {threadId: "one", requestId: "head", turnId: "turn", message: "A", afterItemId: null, imageCount: 0};
  messages.onReceipt = (id, key) => keys.push(key);
  const echo = messages.add("one", {text: "A", images: 0, submissionId: "head", detail: {thread: {turns: []}}});
  echo.status = "uncertain";
  const detail = {thread: {turns: [{id: "turn", items: [{id: "item", type: "userMessage", content: [{type: "text", text: "A"}]}]}]}};
  messages.settle("one", detail);
  assert.equal(messages.list("one")[0], echo);
  messages.settle("one", {...detail, acceptedMessages: [receipt]});
  assert.deepEqual(keys, ["head"]); assert.equal(messages.list("one").length, 0);
});

test("the actual final cache record is bounded even when a transformed response lost its Content-Length", async () => {
  const cacheSource = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/conversation-cache.js"), "utf8");
  const Cache = vm.runInNewContext(cacheSource + "; ConversationSnapshotCache");
  const cache = new Cache("workspace");
  Object.defineProperty(cache, "ready", {value: Promise.resolve({transaction() {throw Error("Oversized record reached storage");}})});
  let removed;
  cache.remove = async id => {removed = id;};
  const detail = {thread: {turns: []}, coordinationMessages: [{body: "a".repeat(34 * 1024 * 1024)}]};
  await cache.write("thread", detail);
  assert.equal(removed, "thread");
  const compact = {thread: {turns: []}, session: {title: "文\n\u0001😀"}, running: true};
  assert.ok(Cache.retainedBytes(compact) >= Buffer.byteLength(JSON.stringify(compact)));
});

test("a restored project draft moves atomically to the new thread after another project becomes current", async () => {
  const creating = deferred(), input = {value: "first", ownerDocument: {}}, noop = () => {};
  const draft = {draftId: "new-session:/work", session: {cwd: "/work"}, work_thread: {browser_session: true}};
  const moves = [], state = {newSessionDraft: draft, selected: "new-session", projectCwd: "/work",
    drafts: new Map(), projectDrafts: new Map(), attachments: {snapshot: () => [], move: noop},
    outgoing: {node: noop}, durableDrafts: {move: async (...args) => moves.push(args)}};
  const context = vm.createContext({state, document: {}, $: () => input, crypto: {randomUUID: () => "request"},
    replaceTimelineContent: noop, api: () => creating.promise, savedViews: null, conversationEntry: () => ({}),
    sessionPath: id => "sessions/" + id, sessionStorage: {removeItem: noop}, isSessionDraft: () => false,
    refreshList: async () => {}, showError: noop, renderStatus: noop});
  vm.runInContext(source + "\n" + section(app, "async function sendSessionDraft(", '$("new-session").onclick'), context);
  context.submitComposerMessage = options => {const stage = Promise.resolve(); options.onStage(stage); return stage;};
  const sending = context.sendSessionDraft("first", []);
  state.projectDrafts.set("/work", {detail: draft, text: "follow-up"});
  state.newSessionDraft = null; state.projectCwd = "/next"; state.selected = null;
  creating.resolve({session: {thread_id: "created"}});
  await sending;
  assert.deepEqual(JSON.parse(JSON.stringify(moves)), [["new-session:/work", "created", {text: "follow-up", images: [], mentions: []}]]);
});

test("window-scoped composer identities survive reload while concurrently duplicated windows obtain their own lease", async () => {
  const f = environment(), locks = new Set();
  let next = 0;
  function open(initial) {
    const store = new Map(initial), events = new Map();
    const view = {crypto: {randomUUID: () => String(++next)}, addEventListener: (name, callback) => events.set(name, callback),
      sessionStorage: {getItem: key => store.get(key), setItem: (key, value) => store.set(key, value)},
      navigator: {locks: {async request(key, options, callback) {
        if (locks.has(key)) return callback(null);
        locks.add(key);
        try {return await callback({name: key});} finally {locks.delete(key);}
      }}}};
    return {view, store, close: () => events.get("pagehide")()};
  }
  const first = open(), id = await f.context.composerWindowScope(first.view);
  const duplicate = open(first.store);
  assert.notEqual(await f.context.composerWindowScope(duplicate.view), id);
  first.close(); await tick();
  const reloaded = open(first.store);
  assert.equal(await f.context.composerWindowScope(reloaded.view), id);
  assert.equal(await f.context.composerWindowScope({agentCoordDesktop: {windowId: "native"}}), "native");
  reloaded.close(); duplicate.close();
});
