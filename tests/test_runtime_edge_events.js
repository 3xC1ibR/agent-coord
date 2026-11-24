"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname,
  "../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const connect = source.slice(source.indexOf("\nfunction connect()"), source.indexOf("\n// Per-item provider events"));

test("a cached conversation reconnects with its generation and durable completion cursor", () => {
  const streams = [];
  const window = new EventTarget();
  const previousGeneration = "a".repeat(32);
  const currentGeneration = "b".repeat(32);
  const state = {config: {eventCursor: previousGeneration + "/1", completionCursor: 17}};
  vm.runInNewContext(connect + "\nconnect()", {
    base: "/api/browser/", state, window, notifications: {receive() {}},
    EventSource: class {
      constructor(url) { this.url = url; streams.push(this); }
      close() { this.closed = true; }
    },
  });
  assert.equal(streams.length, 1);
  assert.equal(streams[0].url, "/api/browser/events?after=" + previousGeneration + "/1&completion_after=17");
  streams[0].onmessage({
    data: JSON.stringify({seq: 2, completionSeq: 24, events: []}),
    lastEventId: currentGeneration + "/2:24",
  });
  window.dispatchEvent(Object.assign(new Event("pagehide"), {persisted: true}));
  assert.equal(streams[0].closed, true);
  window.dispatchEvent(Object.assign(new Event("pageshow"), {persisted: true}));
  assert.equal(streams.length, 2);
  assert.equal(streams[1].url, "/api/browser/events?after=" + currentGeneration + "/2&completion_after=24");
});

test("metadata invalidations refresh only affected conversation panes and reject replay", () => {
  const implementation = source.slice(source.indexOf("function invalidateConversationMetadata("),source.indexOf("function connect()"));
  const calls = [], state = {config:{conversationMetadataCursor:7,conversationOpen:1}, selected:"a",
    conversations:new Map([["a",{revision:0}],["b",{revision:0}]]),
    asks:{refresh(){calls.push("asks");return Promise.resolve();}}};
  const context = {state, scheduleDetail:()=>calls.push("detail"),scheduleList:()=>calls.push("list"),showError:assert.fail,
    agentCoordPanes:{records:new Map(["a","b"].map(id=>[id,{api:{scheduleRefresh:()=>calls.push("pane "+id)}}]))}};
  const invalidate = vm.runInNewContext(implementation + "\ninvalidateConversationMetadata", context);
  invalidate({revision:8,threadIds:["a"],topics:["asks"]});
  invalidate({revision:8,threadIds:["a"],topics:["asks"]});
  assert.deepEqual(calls,["detail","pane a","asks"]);
  assert.equal(state.conversations.get("a").revision,1);
  assert.equal(state.conversations.get("b").revision,0);
  invalidate({revision:1,reset:true,threadIds:[],topics:[]});
  assert.equal(state.config.conversationMetadataCursor,1);
  assert.equal(state.conversations.get("b").revision,1);
});

test("a legacy transfer gateway uses the receipt-bearing session read until composite coverage is advertised", () => {
  const configure = vm.runInNewContext(source.slice(source.indexOf("function workspaceConfig("),source.indexOf("async function boot()"))+"\nworkspaceConfig");
  const config = {conversationOpen:1,interactionContinuity:true};
  assert.equal(configure(config).conversationOpen,0);
  assert.equal(configure({...config,conversationOpenTransferReceipts:1}).conversationOpen,1);
  assert.equal(configure({conversationOpen:1}).conversationOpen,1);
});

test("the same optimistic submit uses only the supported predecessor request field", async () => {
  const outbox = fs.readFileSync(path.join(__dirname,
    "../plugins/agent-coord/scripts/agent_coord/web/outgoing-messages.js"), "utf8");
  for (const advertised of [false, true]) {
    const state = {config: advertised ? {ordinarySubmissionReceipts: 1} : {},
      outgoing: {add() {return {};}, accepted() {}}};
    let received, cleared = 0;
    const context = vm.createContext({state, crypto:{randomUUID:()=>"stable-client-id"},
      api: async (path, body) => { received = {path, body};return {}; }});
    vm.runInContext(outbox + "\nstate.submissionQueue = new ConversationSubmissionQueue();",context);
    context.clear=()=>cleared++;
    await vm.runInContext('submitComposerMessage({id:"one",text:"input",images:[],mentions:[],body:{message:"input"},path:"sessions/one/messages",clear})',context);
    assert.equal(cleared,1);
    assert.equal(received.body.requestId,"stable-client-id");
    assert.equal(Object.hasOwn(received.body,"client_message_id"),advertised);
  }
});
