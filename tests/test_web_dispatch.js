"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const Dispatch = require("../plugins/agent-coord/scripts/agent_coord/web/dispatch.js");

const receipt = (id, values = {}) => ({id, dispatcher_id: "entry", request_id: "request-" + id,
  target_id: "vendor", target_title: "Vendor form", state: "accepted", created_at: id * 10,
  body: {message: "Request " + id, images: []}, ...values});
const user = text => ({type: "userMessage", content: [{type: "text", text}]});

test("fast routes remain visible chronologically alongside real dispatcher replies", () => {
  const input = [{id: "fallback", startedAt: 35, items: [user("Request 3"), {type: "agentMessage", text: "Which vendor?"}]}];
  const result = Dispatch.turns(input, [receipt(1), receipt(2), receipt(3, {target_id: "entry", turn_id: "fallback"})]);
  assert.deepEqual(result.map(t => t.id), ["dispatch-1", "dispatch-2", "dispatch-3", "fallback"]);
  assert.equal(result.flatMap(t => t.items).filter(i => i.type === "userMessage").length, 3);
  assert.equal(result.at(-1).items[0].text, "Which vendor?");
  assert.equal(input[0].items.length, 2, "Provider history remains unchanged");
});

test("internal catch-up stays out of display while later user steering remains visible", () => {
  const source = [{id: "fallback", startedAt: 30, items: [
    {type: "userMessage", content: [{type: "text", text: "[Ribbon Field dispatcher context]\nprivate history"}, {type: "text", text: "Request 1"}]},
    user("Actually use the other vendor")]}];
  const result = Dispatch.turns(source, [receipt(1, {target_id: "entry", turn_id: "fallback"})]);
  assert.equal(JSON.stringify(result).includes("private history"), false);
  assert.equal(result.at(-1).items[0].content[0].text, "Actually use the other vendor");
});

test("request keys survive network retries and reload without retaining message contents", () => {
  const values = new Map(), storage = {getItem: k => values.get(k), setItem: (k,v) => values.set(k,v)};
  let count = 0; const uuid = () => "uuid-" + ++count;
  const body = {message: "private input", askDispatcher: false};
  assert.equal(Dispatch.requestKey(storage, "entry", body, uuid), "uuid-1");
  assert.equal(Dispatch.requestKey(storage, "entry", body, uuid), "uuid-1");
  assert.equal(Dispatch.requestKey(storage, "entry", {...body, askDispatcher: true}, uuid), "uuid-2");
  assert.equal([...values.values()].join().includes("private input"), false);
});

test("receipts link to the destination and report acceptance without claiming completion", () => {
  const document = {createElement: tag => ({tag, children: [], append(...children) {this.children.push(...children);}})};
  let opened, dismissed;
  const node = Dispatch.card(document, receipt(1), {open: id => {opened = id;}});
  assert.equal(node.children[0].textContent, "Sent");
  assert.equal(node.children[1].href, "/#vendor");
  node.children[1].onclick({preventDefault() {}});
  assert.equal(opened, "vendor");
  const uncertain = Dispatch.card(document, receipt(2, {state: "uncertain"}), {dismiss: id => {dismissed = id;}});
  assert.equal(uncertain.children[0].textContent, "Delivery needs review");
  uncertain.children.at(-1).onclick(); assert.equal(dismissed, 2);
});

test("the project dispatcher reuses a preferred open identity without crossing projects", () => {
  const thread = (id, values = {}) => ({thread_id: id, dispatcher: true, browser_session: true,
    attention: "now", project_cwd: "/work/one", updated_at: 1, ...values});
  const threads = [thread("existing"), thread("recent", {updated_at: 2}),
    thread("closed", {attention: "archived", updated_at: 10}), thread("other", {project_cwd: "/work/two"}),
    thread("ordinary", {dispatcher: false}), thread("terminal", {browser_session: false})];
  assert.equal(Dispatch.chooseDispatcher(threads, "/work/one", "existing").thread_id, "existing");
  assert.equal(Dispatch.chooseDispatcher(threads, "/work/one", "closed").thread_id, "recent");
  assert.equal(Dispatch.chooseDispatcher(threads, "/work/one", "other").thread_id, "recent");
  assert.equal(Dispatch.chooseDispatcher(threads, "/work/missing"), null);
  assert.equal(Dispatch.chooseDispatcher(threads, ""), null);
});

test("handoffs animate once after acceptance, never on historical, queued or uncertain receipts", () => {
  const tracker = new Dispatch.HandoffTracker();
  const events = (...receipts) => Dispatch.handoffs({dispatchRequests: receipts});
  assert.deepEqual(tracker.update("entry", events(receipt(1), receipt(2, {state: "queued"}))), []);
  assert.deepEqual(tracker.update("entry", events(receipt(1), receipt(2, {state: "sending"}), receipt(3, {state: "uncertain"}))), []);
  assert.deepEqual(tracker.update("entry", events(receipt(1), receipt(2), receipt(3, {state: "uncertain"}))).map(e => e.key), ["request:2"]);
  assert.deepEqual(tracker.update("entry", events(receipt(1), receipt(2))), []);
  assert.deepEqual(tracker.update("entry", events(receipt(4, {target_id: "entry"}))), []);
  assert.deepEqual(tracker.update("another", events(receipt(5))), [], "Opening a different dispatcher primes its own history");
});

test("LLM-created agents and later assignments share the handoff stream, excluding unrelated messages", () => {
  const message = (id, values = {}) => ({id, direction: "sent", classification: "action_required",
    created_at: id * 10, counterpart: {session_id: "worker", title: "Worker"}, ...values});
  const tracker = new Dispatch.HandoffTracker(); tracker.update("entry", []);
  const entries = Dispatch.handoffs({dispatchRequests: [receipt(1)], coordinationMessages: [
    message(2, {creation_request_id: "created"}), message(3), message(4, {direction: "received"}),
    message(5, {classification: "informational"}), message(6, {suppressed_at: 1}), message(7, {closed_at: 1})]});
  assert.deepEqual(entries.map(e => e.key), ["request:1", "message:2", "message:3"]);
  assert.deepEqual(tracker.update("entry", entries).map(e => e.target), ["vendor", "worker", "worker"]);
  assert.deepEqual(tracker.update("entry", entries), []);
});

function startHarness(threads = []) {
  const fs = require("node:fs"), vm = require("node:vm");
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const calls = [], preferences = new Map(), elements = new Map();
  const context = {DispatchUI: Dispatch, Promise, state: {folders: {selected: {cwd: "/work"}}, sessions: threads,
    navigationRevision: 0, dispatchReady: true, dispatchDock: {preference: cwd => preferences.get(cwd) || {},
      remember: (cwd, value) => preferences.set(cwd, value)}},
    $: id => { if (!elements.has(id)) elements.set(id, {hidden: true}); return elements.get(id); },
    sessionContext: () => ({cwd: "/work"}), sessionPath: id => "sessions/" + id,
    api: async (path, body) => { calls.push([path, body]); return {session: {thread_id: "created"}}; },
    select: async (id, options) => { calls.push(["select", id, options]); },
    refreshList: async () => {}, showError: error => { throw error; }};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function currentProjectDispatcher("), source.indexOf("function sessionContext(")), context);
  return {context, calls, preferences};
}

test("early boot cannot create a duplicate before the existing dispatcher is discovered", async () => {
  const fs = require("node:fs");
  const html = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/index.html"), "utf8");
  assert.match(html.match(/<button\b[^>]*\bid="start-dispatcher"[^>]*>/)[0], /\sdisabled(?:\s|>)/,
    "The entry is disabled before JavaScript starts");
  const {context, calls, preferences} = startHarness();
  context.state.dispatchReady = false;
  preferences.set("/work", {id: "existing", hidden: true});
  context.syncDispatcherOverview();
  assert.equal(context.$("start-dispatcher").disabled, true);
  await context.startDispatcher();
  assert.deepEqual(calls, [], "An early invocation must not create or configure a conversation");

  context.state.sessions = [{thread_id: "existing", cwd: "/work", dispatcher: true,
    browser_session: true, attention: "now"}];
  context.syncDispatcherOverview();
  assert.equal(context.$("start-dispatcher").disabled, true, "Stay disabled until boot completes");
  context.state.dispatchReady = true;
  context.syncDispatcherOverview();
  assert.equal(context.$("start-dispatcher").disabled, false);
  assert.equal(context.$("start-dispatcher").textContent, "Open dispatcher ↗");
  await context.startDispatcher();
  assert.deepEqual(calls.map(call => call[0]), ["select"]);
  assert.equal(calls[0][1], "existing");
});

test("Start dispatcher reuses the project conversation and its settings", async () => {
  const {context, calls} = startHarness([{thread_id: "existing", cwd: "/work", dispatcher: true,
    browser_session: true, attention: "now"}]);
  await context.startDispatcher();
  assert.deepEqual(calls.map(call => call[0]), ["select"]);
  assert.equal(calls[0][1], "existing");
  assert.equal(calls[0][2].dock, true);
});

test("Start creates and designates a conversation without submitting a provider prompt", async () => {
  const {context, calls} = startHarness();
  await context.startDispatcher();
  assert.deepEqual(calls.map(call => call[0]), ["sessions", "sessions/created/dispatch", "select"]);
  assert.equal(calls[0][1].yolo, false);
  assert.equal(calls[1][1].enabled, true);
});

test("configuration retries retain the created identity and never duplicate a dispatcher", async () => {
  const {context, calls} = startHarness();
  const api = context.api; let fail = true;
  context.api = async (path, body) => {
    const result = await api(path, body);
    if (path.endsWith("/dispatch") && fail) throw new Error("Temporarily unavailable");
    return result;
  };
  await assert.rejects(context.startDispatcher(), /unavailable/);
  fail = false; await context.startDispatcher();
  assert.equal(calls.filter(call => call[0] === "sessions").length, 1);
  assert.equal(calls.filter(call => call[0] === "sessions/created/dispatch").length, 2);
});

test("creation never steals focus after navigation or permits a duplicate click", async () => {
  const {context, calls} = startHarness();
  const api = context.api; let release;
  context.api = async (path, body) => {
    if (path === "sessions") await new Promise(resolve => { release = resolve; });
    return api(path, body);
  };
  const first = context.startDispatcher();
  await context.startDispatcher();
  context.state.navigationRevision++; release(); await first;
  assert.equal(calls.filter(call => call[0] === "sessions").length, 1);
  assert.equal(calls.filter(call => call[0] === "select").length, 0);
});
