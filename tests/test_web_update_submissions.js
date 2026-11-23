"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
test("a pending activity write leaves browser connections available for interactive requests", async () => {
  const calls = [];
  let release;
  let next = Promise.resolve();
  const locks = {request(name, callback) {
    assert.equal(name, "ribbon-field-activity-write");
    next = next.then(callback);
    return next;
  }};
  const api = vm.runInNewContext(source.slice(source.indexOf("async function api("), source.indexOf("async function openMentionFile(")) + ";api",
    {base: "", state: {config: {interactionContinuity: true, token: "test"}}, navigator: {locks},
      fetch: async path => {
        calls.push(path);
        if (calls.length === 1) await new Promise(resolve => { release = resolve; });
        return {status: 200, ok: true, json: async () => ({})};
      }});
  const first = api("activity/events", {events: []});
  await new Promise(setImmediate);
  const second = api("activity/events", {events: []});
  await api("sessions/test/messages", {message: "test"});
  assert.deepEqual(calls, ["activity/events", "sessions/test/messages"]);
  release();
  await Promise.all([first, second]);
  assert.deepEqual(calls, ["activity/events", "sessions/test/messages", "activity/events"]);
});
const {OutgoingMessages, UpdateSubmissionChanges} = vm.runInNewContext(
  source.slice(source.indexOf("class OutgoingMessages {"), source.indexOf("const TIMELINE_WINDOW_TURNS")) +
  ";({OutgoingMessages, UpdateSubmissionChanges})", {AbortController, setTimeout, clearTimeout});
const item = (id, text) => ({id, type: "userMessage", content: [{type: "text", text}]});
const history = (...items) => ({thread: {turns: [{id: "active", items}]}});
const receipt = (requestId, sequence, state = "queued", threadId = "thread") =>
  ({requestId, sequence, state, threadId, message: "same text", imageCount: 0, expectedTurnId: "active"});
const outgoing = () => new OutgoingMessages({itemText: item => item.content[0]?.text || ""});
const status = (rows, id = "thread") => Array.from(rows.list(id), e => [e.submissionId, e.status]);

test("sender merges the durable receipt with the local echo and ignores a late 202 after commit", () => {
  const messages = outgoing(), initial = history(item("older", "same text"));
  const echo = messages.add("thread", {text: "same text", detail: initial, submissionId: "one"});
  messages.transfers("thread", [receipt("one", 1)], initial);
  assert.equal(messages.list("thread")[0], echo);
  assert.deepEqual(status(messages), [["one", "queued"]]);
  const delivered = history(...initial.thread.turns[0].items, item("new", "same text"));
  messages.transfers("thread", [receipt("one", 1, "committed")], delivered);
  messages.settle("thread", delivered);
  messages.transfers("thread", [receipt("one", 1)], initial);
  assert.deepEqual(status(messages), []);
});

test("another window sees two identical requests in FIFO order and never consumes one native copy twice", () => {
  const messages = outgoing(), initial = history(item("older", "same text"));
  messages.transfers("thread", [receipt("two", 2), receipt("one", 1)], initial);
  assert.deepEqual(status(messages), [["one", "queued"], ["two", "queued"]]);
  messages.transfers("thread", [receipt("one", 1, "committed"), receipt("two", 2, "committed")], initial);
  const one = history(...initial.thread.turns[0].items, item("new-one", "same text"));
  messages.settle("thread", one);
  messages.settle("thread", one);
  assert.deepEqual(status(messages), [["two", "sent"]]);
  messages.settle("thread", history(...one.thread.turns[0].items, item("new-two", "same text")));
  assert.deepEqual(status(messages), []);
});

test("missing observations, stale queue frames and older identical native messages cannot confirm delivery", () => {
  const messages = outgoing();
  messages.transfers("thread", [receipt("one", 1)], null);
  const initial = history(item("old", "same text"));
  messages.settle("thread", initial);
  messages.transfers("thread", [], initial);
  messages.transfers("thread", [receipt("one", 1, "uncertain")], initial);
  messages.transfers("thread", [receipt("one", 1)], initial);
  messages.settle("thread", initial);
  assert.deepEqual(status(messages), [["one", "uncertain"]]);
  messages.transfers("thread", [receipt("one", 1, "committed")], initial);
  messages.settle("thread", initial);
  assert.deepEqual(status(messages), [["one", "sent"]]);
});

test("past commits use normal history on reconnect and terminal rejection survives later sends", () => {
  const messages = outgoing(), detail = history(item("old", "same text"));
  messages.transfers("thread", [receipt("past", 1, "committed"), receipt("bad", 2, "failed")], detail);
  messages.add("thread", {text: "next", detail});
  messages.transfers("thread", [receipt("bad", 2), receipt("past", 1)], detail);
  assert.deepEqual(status(messages), [["bad", "failed"], [null, "sending"]]);
  messages.transfers("thread", [receipt("foreign", 1, "queued", "other")], detail);
  assert.equal(messages.list("other").length, 0);
  assert.equal(messages.list("thread").length, 2);
});

test("receipt changes coalesce while one GET is in flight and drain all cursor pages in order", async () => {
  const calls = [], received = [];
  const updates = new UpdateSubmissionChanges({
    load: (id, cursor) => new Promise(resolve => calls.push({id, cursor, resolve})),
    receive: (id, rows) => received.push([id, rows[0]?.requestId])});
  updates.select("one");
  const pending = updates.refresh();
  updates.refresh(); updates.refresh();
  assert.equal(calls.length, 1);
  calls[0].resolve({threadId: "one", cursor: 1, more: true, submissions: [receipt("first", 1)]});
  await new Promise(setImmediate);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].cursor, 1);
  calls[1].resolve({threadId: "one", cursor: 4, more: false, submissions: [receipt("second", 2)]});
  await pending;
  updates.select("one");
  assert.equal(calls.length, 2, "an unchanged selection does not start a poll");
  assert.deepEqual(received, [["one", "first"], ["one", "second"]]);
});

test("unrelated transfer activity does not start foreground receipt reads", async () => {
  const reads = [];
  const updates = new UpdateSubmissionChanges({
    load: async id => {
      reads.push(id);
      return {threadId: id, watchKey: "my-watch-key", cursor: 0, more: false, submissions: []};
    },
    receive() {}
  });
  updates.select("selected");
  await new Promise(setImmediate);
  for (let i = 0; i < 100; i++) updates.invalidate(["other-watch-key"]);
  await new Promise(setImmediate);
  assert.deepEqual(reads, ["selected"]);
  await updates.invalidate(["my-watch-key"]);
  await updates.invalidate(null);
  assert.deepEqual(reads, ["selected", "selected", "selected"]);
});

test("navigation aborts the previous GET, ignores its late result and retains each thread's own cursor", async () => {
  const calls = [], received = [];
  const updates = new UpdateSubmissionChanges({
    load: (id, cursor, signal) => new Promise(resolve => calls.push({id, cursor, signal, resolve})),
    receive: (id, rows) => received.push([id, rows.length])});
  updates.select("one"); updates.select("two");
  assert.ok(calls[0].signal.aborted);
  calls[0].resolve({threadId: "one", cursor: 7, more: false, submissions: [receipt("old", 1)]});
  calls[1].resolve({threadId: "two", cursor: 8, more: false, submissions: [receipt("new", 1)]});
  await new Promise(setImmediate);
  updates.select("one"); updates.select("two"); updates.select(null);
  assert.deepEqual(calls.map(row => [row.id, row.cursor]), [["one",0],["two",0],["one",0],["two",8]]);
  for (const call of calls.slice(2)) {
    assert.ok(call.signal.aborted); call.resolve({});
  }
  await new Promise(setImmediate);
  assert.deepEqual(received, [["two",1]]);
});

test("three configured windows allocate exactly three provider streams and fetch private receipts after invalidation", async () => {
  const setup = source.slice(source.indexOf("  if (state.config.interactionContinuity === true)"),
                             source.indexOf("  state.activity = globalThis.ActivityCollector"));
  const connect = source.slice(source.indexOf("function connect()"), source.indexOf("// Per-item provider events"));
  const connections = [], reads = [];
  for (const id of ["one", "two", "three"]) {
    const state = {config: {interactionContinuity: true}, selected: id,
      outgoing: {transfers: () => false}};
    const context = {state, UpdateSubmissionChanges, AbortController, setTimeout, clearTimeout,
      base: "/api/browser/", Event, CustomEvent, window: new EventTarget(),
      api: async path => { reads.push(path); return {threadId: id, watchKey: "watch-" + id, submissions: [], more: false, cursor: 0}; },
      EventSource: class extends EventTarget {
        constructor(url) { super(); this.url = url; connections.push(this); }
      }};
    vm.runInNewContext(setup + connect + ";connect()", context);
  }
  await new Promise(setImmediate);
  assert.deepEqual(connections.map(s => s.url.split("?")[0]), Array(3).fill("/api/browser/events"));
  for (const connection of connections) connection.dispatchEvent(new Event("update-submission-change"));
  await new Promise(setImmediate);
  assert.deepEqual(reads, [...["one", "two", "three"], ...["one", "two", "three"]]
    .map(id => "update-submissions?thread=" + id + "&after=0"));
  for (const connection of connections) {
    connection.dispatchEvent(Object.assign(new Event("update-submission-change"), {
      data: JSON.stringify({revision: 10, watchKeys: ["watch-two"]})
    }));
  }
  await new Promise(setImmediate);
  assert.deepEqual(reads.slice(6), ["update-submissions?thread=two&after=0"]);
  connections[0].dispatchEvent(new Event("open"));
  await new Promise(setImmediate);
  assert.equal(reads.length, 8, "reconnection refreshes this window's selected thread");
});

test("a saved snapshot cannot settle an accepted transfer; a fresh native view settles it once", () => {
  const messages = outgoing(), old = history(item("old", "same text"));
  messages.transfers("thread", [receipt("request", 1)], old);
  const withNew = history(item("old", "same text"), item("native", "same text"));
  messages.transfers("thread", [receipt("request", 1, "committed")], old);
  messages.settle("thread", {...withNew, transferReadSource: "saved"});
  assert.deepEqual(status(messages), [["request", "sent"]]);
  messages.settle("thread", {...withNew, transferReadSource: "serving"});
  assert.deepEqual(status(messages), []);
});

test("the ordinary composer keeps the 202 queued and a concurrent stream confirmation survives a lost POST response", async () => {
  for (const lostResponse of [false, true]) {
    const outgoingMessages = outgoing();
    const detail = {...history(), running: false, work_thread: {client: "codex", browser_session: true, attention: "now"}};
    const input = {value: "same text"};
    const state = {config: {interactionContinuity: true}, selected: "thread", detail, closing: new Set(),
      commandFeedback: new Map(), drafts: new Map(), outgoing: outgoingMessages};
    const c = vm.createContext({state, document: {}, $: () => input, sessionPath: id => "sessions/" + id,
      DispatchUI: {requestKey: () => "accepted"}, conversationEntry: () => ({detail}),
      sessionStorage: {removeItem() {}}, renderStatus() {}, isSessionDraft: () => false,
      showError() {}, refreshList: async () => {}, refreshDetail: async () => {}});
    c.api = async (path, body) => {
      assert.equal(path, "sessions/thread/messages"); assert.equal(body.requestId, "accepted");
      assert.equal(outgoingMessages.list("thread")[0].submissionId, "accepted");
      if (lostResponse) {
        outgoingMessages.transfers("thread", [receipt("accepted", 1)], detail);
        throw new Error("response lost");
      }
      return {transferSubmission: receipt("accepted", 1)};
    };
    vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), c);
    await c.sendMessage();
    assert.deepEqual(status(outgoingMessages), [["accepted", "queued"]]);
    assert.equal(input.value, "");
  }
});

test("Enter can submit an ordinary message behind a starting turn on a verified receiver", async () => {
  for (const [ready, active, expected] of [[true,null,null],[true,"current","current"],[false,null,false]]) {
    const input = {value:"ordinary message"};
    const detail = {...history(), acceptsUntargetedInput:ready, running:true, activeTurn:active,
      work_thread:{client:"codex",browser_session:true,attention:"now"}};
    const state = {config:{interactionContinuity:true}, selected:"thread", detail, closing:new Set(),
      commandFeedback:new Map(), drafts:new Map(), outgoing:outgoing()};
    const requests = [];
    const c = vm.createContext({state, document:{}, $:()=>input, sessionPath:id=>"sessions/"+id,
      DispatchUI:{requestKey:()=>"request"}, conversationEntry:()=>({detail}), sessionStorage:{removeItem(){}},
      renderStatus(){}, isSessionDraft:()=>false, refreshList:async()=>{}, refreshDetail:async()=>{},showError(){},
      api:async(path,body)=>{requests.push(body);return {transferSubmission:receipt("request",1)};}});
    vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), c);
    await c.sendMessage();
    assert.equal(requests.length, expected === false ? 0 : 1);
    if (expected === null) assert.equal("expectedTurnId" in requests[0], false);
    if (active) assert.equal(requests[0].expectedTurnId, active);
  }
});

test("only visit posts use the isolated loopback origin and restricted credential", async () => {
  const calls = [], origin = "http://127.0.0.1:2431";
  const api = vm.runInNewContext(source.slice(source.indexOf("async function api("), source.indexOf("async function openMentionFile(")) + ";api",
    {base: "/api/browser/", state: {config: {interactionContinuity:true, token:"main", visitTransport:{origin,token:"visits"}}},
     fetch: async (url, options) => { calls.push({url,options}); return {ok:true, status:200, json:async()=>({})}; }});
  const batch = {events:[{event_id:"same",window_id:"one",visit_id:"visit"}]};
  await api("activity/events", batch, {keepalive:true});
  await api("sessions/one/messages", {message:"work"});
  await api("activity/settings");
  assert.deepEqual(calls.map(r=>r.url), [origin+"/api/browser/activity/events", "/api/browser/sessions/one/messages","/api/browser/activity/settings"]);
  assert.equal(calls[0].options.keepalive, true);
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.headers["X-Agent-Coord-Token"], "visits");
  assert.equal(calls[1].options.headers["X-Agent-Coord-Token"], "main");
  assert.deepEqual(JSON.parse(calls[0].options.body), batch);
});
