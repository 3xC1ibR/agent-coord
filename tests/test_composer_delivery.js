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
    path: "sessions/one/queue", body: {requestId: "old", client_message_id: "old"}}]);
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

const creationSource = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/conversation-creation.js"), "utf8");
function creationStore() {
  const drafts = new Map(), journal = new Map();
  const key = (id, requestId) => JSON.stringify([id, requestId]);
  return {drafts, journal,
    async stage(id, requestId, row) {
      journal.set(key(id, requestId), {id, row});
      if (drafts.get(id)?.text === row.text) drafts.set(id, {text: "", images: [], mentions: []});
    },
    async pending(id) {return [...journal.values()].filter(entry => entry.id === id).map(entry => entry.row);},
    async pendingThreadIds() {return [...new Set([...journal.values()].map(entry => entry.id))];},
    async read(id) {return drafts.get(id);},
    async write(id, patch) {drafts.set(id, {...drafts.get(id), ...patch});},
    async acknowledge(id, requestId) {journal.delete(key(id, requestId));},
    async promoteCreation(projectId, creationId, threadId, message) {
      const creation = journal.get(key(projectId, creationId));
      if (!creation) return null;
      const row = {...creation.row, ...message};
      journal.delete(key(projectId, creationId));
      journal.set(key(threadId, row.requestId), {id: threadId, row});
      drafts.set(threadId, drafts.get(projectId)); drafts.delete(projectId); return row;
    }};
}
function creationWindow(store, api, capable = true) {
  const f = environment(), noop = () => {};
  const projectId = "new-session:/work", draft = {draftId: projectId, session: {cwd: "/work", client: "codex"}, work_thread: {browser_session: true}};
  Object.assign(f.state, {selected: "new-session", newSessionDraft: draft, projectCwd: "/work", projectDrafts: new Map(),
    durableDrafts: store, attachments: {snapshot: () => [], move: noop}, mentions: {sent: noop}, sourceWindowId: "origin"});
  f.state.config.sessionCreationReceipts = capable ? 1 : 0;
  f.state.outgoing.node = (document, row) => row;
  Object.assign(f.context, {crypto: {randomUUID: (() => {let sequence = 0; return () => "key-" + (++sequence);})()},
    document: {}, api, replaceTimelineContent: row => {f.lastEcho = row;}, composerDraftId: () => projectId,
    savedViews: null, conversationEntry: () => ({}), sessionPath: id => "sessions/" + id,
    refreshList: async () => {}, refreshDetail: async () => {}, isSessionDraft: () => true, select: async id => {f.state.selected = id;}});
  vm.runInContext(creationSource + "\n" + section(app, "async function sendSessionDraft(", '$("new-session").onclick'), f.context);
  f.input.ownerDocument = {};
  return {...f, projectId, draft};
}

test("creation and first-message response losses recover separate immutable IDs and preserve the later project edit", async () => {
  const store = creationStore(), requests = [], effects = new Set(), failCreate = deferred();
  let loseCreate = true, loseMessage = true;
  const api = async (path, body) => {
    requests.push([path, body.requestId]); effects.add(path + body.requestId);
    if (path === "sessions") {
      if (loseCreate) {loseCreate = false; return failCreate.promise;}
      return {session: {thread_id: "created"}, thread: {turns: []}};
    }
    if (loseMessage) {loseMessage = false; throw Object.assign(new Error("Lost message response"), {status: 502});}
    return {};
  };
  const first = creationWindow(store, api);
  first.input.value = "first";
  await store.write(first.projectId, {text: "first", images: [], mentions: []});
  const start = first.context.sendSessionDraft("first", []);
  assert.equal(first.input.value, "");
  await first.context.drainComposerWrites();
  assert.equal((await store.pending(first.projectId))[0].text, "first");
  await first.context.writeComposerRecord(first.projectId, () => store.write(first.projectId, {text: "second"}));
  first.state.projectDrafts.set("/work", {detail: first.draft, text: "second"});
  first.state.selected = null; first.state.newSessionDraft = null; first.state.projectCwd = "/other";
  failCreate.reject(Object.assign(new Error("Response lost"), {status: 502}));
  await assert.rejects(start, {status: 502});
  const second = creationWindow(store, api);
  second.state.selected = null; second.state.newSessionDraft = null; second.state.projectCwd = "/other";
  await second.context.recoverWorkspaceOutbox();
  for (let i = 0; i < 6; i++) await tick();
  assert.equal((await store.pending(first.projectId)).length, 0);
  assert.equal((await store.pending("created"))[0].text, "first");
  assert.equal((await store.read("created")).text, "second");
  const third = creationWindow(store, api);
  await third.context.recoverWorkspaceOutbox();
  await tick();
  assert.equal((await store.pending("created")).length, 0);
  assert.deepEqual(requests.map(pair => pair[0]), ["sessions", "sessions", "sessions/created/messages", "sessions/created/messages"]);
  assert.equal(requests[0][1], requests[1][1]);
  assert.equal(requests[2][1], requests[3][1]);
  assert.notEqual(requests[0][1], requests[2][1]);
  assert.equal(effects.size, 2);
});

test("a predecessor's unresolved session creation stays journaled after upgrade without a second provider call", async () => {
  const store = creationStore(), calls = [];
  const api = async (path, body) => {calls.push(body); throw Object.assign(new Error("Lost"), {status: 502});};
  const first = creationWindow(store, api, false);
  first.input.value = "first";
  await assert.rejects(first.context.sendSessionDraft("first", []), {status: 502});
  assert.equal(Object.hasOwn(calls[0], "requestId"), false);
  const reopened = creationWindow(store, api, true);
  await reopened.context.recoverWorkspaceOutbox();
  await tick();
  assert.equal(calls.length, 1);
  assert.equal((await store.pending(first.projectId)).length, 1);
  assert.equal(reopened.state.creationIntents.get(first.projectId).status, "uncertain");
});

test("second Enter cannot overtake a prior echo whose discovery failed before its stage committed", async () => {
  const f = environment(), gate = deferred(), recorded = [];
  let reads = 0;
  f.state.durableDrafts.pending = async () => {
    if (!reads++) throw Error("Unavailable");
    return recorded.slice();
  };
  f.state.durableDrafts.stage = async (id, key, row) => {await gate.promise; recorded.push(row);};
  await assert.rejects(f.send("one", "first"), /Unavailable/);
  const second = f.send("one", "second");
  await tick();
  assert.equal(f.effects.length, 0);
  gate.resolve(); await second;
  assert.deepEqual(f.effects, ["first", "second"]);
});

test("a browser without locks grants its scope only to the document following its own pagehide", async () => {
  const f = environment();
  let sequence = 0;
  function open(seed) {
    const events = new Map(), store = new Map(seed), view = {navigator: {}, crypto: {randomUUID: () => String(++sequence)},
      addEventListener: (name, handler) => events.set(name, handler),
      sessionStorage: {getItem: key => store.get(key), setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key)}};
    return {store, events, view};
  }
  const first = open(), id = await f.context.composerWindowScope(first.view);
  const duplicate = open(first.store);
  assert.notEqual(await f.context.composerWindowScope(duplicate.view), id);
  first.events.get("pagehide")();
  const reload = open(first.store);
  assert.equal(await f.context.composerWindowScope(reload.view), id);
});
