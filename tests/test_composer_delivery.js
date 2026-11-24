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
  const consumed = new Set();
  const claim = async (database, scope, generation) => {
    const key = scope + ":" + generation;
    if (consumed.has(key)) return false;
    consumed.add(key); return true;
  };
  function open(seed) {
    const events = new Map(), store = new Map(seed), view = {navigator: {}, crypto: {randomUUID: () => String(++sequence)},
      addEventListener: (name, handler) => events.set(name, handler),
      sessionStorage: {getItem: key => store.get(key), setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key)}};
    return {store, events, view};
  }
  const first = open(), id = await f.context.composerWindowScope(first.view, claim);
  const duplicate = open(first.store);
  assert.notEqual(await f.context.composerWindowScope(duplicate.view, claim), id);
  first.events.get("pagehide")();
  const reload = open(first.store), copiedHandoff = open(first.store);
  assert.equal(await f.context.composerWindowScope(reload.view, claim), id);
  assert.notEqual(await f.context.composerWindowScope(copiedHandoff.view, claim), id);
});

test("checkpoint persists the first decoded image while the second is pending and drains both file reads", async () => {
  const f = environment(), first = deferred(), second = deferred(), saved = [];
  f.context.composerDraftId = () => "one";
  f.state.durableDrafts.write = async (id, patch) => {saved.push(patch.images.map(image => image.name));};
  const images = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/image-attachments.js"), "utf8");
  vm.runInContext(images, f.context);
  f.state.attachments = new f.context.ChatImageAttachments({document: {}, getThread: () => "one", canAttach: () => true,
    onChange: () => f.context.persistAttachmentChanges(), onError: assert.fail,
    readFile: file => file.name === "a.png" ? first.promise : second.promise});
  const adding = f.state.attachments.add(["a.png", "b.png"].map(name => ({name, type: "image/png", size: 2})));
  first.resolve("data:image/png;base64,YQ=="); await tick();
  assert.deepEqual(Array.from(saved[0]), ["a.png"]);
  let drained = false;
  const barrier = f.context.drainComposerWrites().then(() => {drained = true;});
  await tick(); assert.equal(drained, false);
  second.resolve("data:image/png;base64,Yg==");
  await Promise.all([barrier, adding]);
  assert.deepEqual(Array.from(saved.at(-1)), ["a.png", "b.png"]);
});

test("successful recovery of a failed local stage clears its obsolete exit error", async () => {
  const f = environment(), gate = deferred(), writes = [];
  f.discovery.resolve([]);
  f.context.composerDraftId = () => "one";
  f.state.durableDrafts.stage = () => gate.promise;
  f.state.durableDrafts.write = async (id, draft) => {writes.push(draft);};
  const sending = f.send("one", "first");
  await tick(); f.input.value = "follow-up";
  const draining = f.context.drainComposerWrites();
  gate.reject(new Error("Temporary journal failure"));
  await assert.rejects(sending, /Temporary journal failure/);
  await draining;
  assert.equal(writes.at(-1).text, "follow-up");
  assert.deepEqual(f.effects, []);
  assert.equal(f.state.composerWriteFailures.size, 0);
});

test("a definitive creation rejection retires A while preserving the newer project draft B", async () => {
  const store = creationStore(), response = deferred();
  const first = creationWindow(store, () => response.promise);
  store.recover = async () => false;
  first.input.value = "A";
  const sending = first.context.sendSessionDraft("A", []);
  await first.context.drainComposerWrites();
  first.input.value = "B";
  await store.write(first.projectId, {text: "B"});
  response.reject(Object.assign(new Error("Invalid session settings"), {status: 400}));
  await assert.rejects(sending, {status: 400});
  assert.equal(first.input.value, "B"); assert.equal((await store.read(first.projectId)).text, "B");
  assert.equal((await store.pending(first.projectId)).length, 0);
  assert.equal(first.state.creationIntents.has(first.projectId), false);
});

test("Enter before a verified open paints and stages immediately, then binds only the fresh route", async () => {
  const f = environment(), opening = deferred(), routes = [], stored = new Map();
  f.discovery.resolve([]);
  f.state.selected = "one"; f.state.detail = null; f.state.detailRefreshing = true;
  f.state.attachments = {snapshot: () => [], pending: () => false};
  f.state.outgoing.update = () => {};
  f.state.sourceWindowId = "window";
  const entry = {pending: opening.promise, detail: null};
  f.state.conversations = new Map([["one", entry]]);
  f.context.conversationEntry = () => entry;
  f.context.sessionPath = id => "sessions/" + id;
  f.context.crypto = {randomUUID: () => "cold-intent"};
  f.state.durableDrafts.stage = async (id, key, row) => stored.set(key, row);
  f.state.durableDrafts.resolveSubmission = async (id, key, path, body) => {
    const row = {...stored.get(key), path, body}; stored.set(key, row); return row;
  };
  f.context.api = async (path, body) => {routes.push([path, body]); return {};};
  vm.runInContext(section(app, "function submitAfterFirstRead(", "function requestComposerSubmit()"), f.context);
  f.input.value = "Wait for routing";
  const sending = f.context.submitAfterFirstRead(false);
  assert.equal(f.input.value, "");
  assert.equal(f.rows[0].text, "Wait for routing");
  await f.context.drainComposerWrites();
  assert.equal(stored.get("cold-intent").path, "");
  assert.equal(routes.length, 0);
  entry.detail = {work_thread: {thread_id: "one", client: "codex", browser_session: true}, running: true, activeTurn: "current"};
  opening.resolve(true);
  await sending;
  assert.equal(routes[0][0], "sessions/one/messages");
  assert.equal(routes[0][1].expectedTurnId, "current");
  assert.equal(stored.get("cold-intent").path, routes[0][0]);
});

test("a legacy unknown row releases later messages only after the user explicitly stops tracking it", async () => {
  const f = environment();
  const newer = f.send("one", "next");
  f.discovery.resolve([{requestId: "legacy", text: "earlier", images: [], mentions: [],
    body: {requestId: "legacy"}, path: "sessions/one/messages"}]);
  await tick();
  assert.deepEqual(f.effects, []);
  const echo = f.rows.find(row => row.submissionId === "legacy");
  assert.equal(echo.retry, undefined);
  await echo.release(); await newer;
  assert.equal(echo.status, "unverified");
  assert.deepEqual(f.effects, ["next"]);
  assert.deepEqual(f.acknowledged, ["legacy", "next"]);
});

test("a later checkpoint retries a failed repair using the current complete composer", async () => {
  const f = environment(), writes = [];
  f.discovery.resolve([]); f.context.composerDraftId = () => "one";
  f.state.durableDrafts.stage = async () => {throw Error("Storage unavailable");};
  f.state.durableDrafts.write = async () => {throw Error("Still unavailable");};
  await assert.rejects(f.send("one", "A"));
  await assert.rejects(f.context.drainComposerWrites(), /unavailable/);
  f.input.value = "B";
  f.state.durableDrafts.write = async (id, patch) => {writes.push(patch);};
  await f.context.writeComposerRecord("one", () => f.state.durableDrafts.write("one", {text: "B"}), "text");
  await f.context.drainComposerWrites();
  assert.equal(writes.at(-1).text, "B");
  assert.equal(f.state.composerWriteFailures.size, 0);
  assert.equal(f.effects.length, 0);
});

test("configured Review is recorded with a cold input and bound to the verified idle thread", async () => {
  const f = environment();
  const entry = {pending: Promise.resolve(true), detail: {work_thread: {thread_id: "one", browser_session: true, client: "codex"}}};
  f.context.conversationEntry = () => entry; f.context.sessionPath = id => "sessions/" + id;
  f.state.durableDrafts.resolveSubmission = async (id, key, path, body) => ({path, body});
  const row = {requestId: "review", body: {message: "Review this", review: {mode: "standard"}}};
  const bound = await f.context.bindComposerSubmission("one", row);
  assert.equal(bound.body.review.mode, "standard");
  entry.detail.running = true; entry.detail.activeTurn = "active";
  assert.equal((await f.context.bindComposerSubmission("one", row)).body.review, undefined);
});
