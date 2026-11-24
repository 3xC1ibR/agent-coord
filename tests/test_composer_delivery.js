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
