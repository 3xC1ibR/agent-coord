"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const DispatchUI = require("../plugins/agent-coord/scripts/agent_coord/web/dispatch.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const values = new Map(), elements = new Map(), calls = [], warnings = [];
  const c = {state: {busy: false, selected: "dispatcher", config: {token: "test"}, drafts: new Map(),
    closing: new Set(), commandFeedback: new Map(), detail: {running: false,
      work_thread: {browser_session: true, dispatcher: true, attention: "now"}}},
    base: "/api/browser/", listRequest: 0, location: {protocol: "http:"},
    console: {warn: (...args) => warnings.push(args)},
    DispatchUI, sessionStorage: {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)},
    $: id => { if (!elements.has(id)) elements.set(id, {value: "", checked: false}); return elements.get(id); },
    sessionPath: id => "sessions/" + id, isSessionDraft: () => false,
    renderStatus() {}, refreshDetail: async () => {}, refreshList: async () => {},
    fetch: async (path, options) => {
      calls.push({path, body: options.body && JSON.parse(options.body)});
      return c.response;
    },
    response: {status: 200, ok: true, headers: {get: () => "application/json"}, json: async () => ({dispatched: true})}};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("async function api("), source.indexOf("async function openMentionFile(")), c);
  vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), c);
  c.$("message").value = "Review the navigation";
  return {c, values, calls, warnings};
}

test("unreadable send response preserves draft and reuses the same dispatcher request ID", async () => {
  const {c, calls, values, warnings} = setup();
  c.response.json = async () => { throw new SyntaxError("The string did not match the expected pattern."); };
  await assert.rejects(c.sendMessage(), /check the conversation before sending again/);
  assert.equal(c.$("message").value, "Review the navigation");
  assert.equal(c.state.busy, false);
  assert.equal(calls.length, 1, "No automatic send retry");
  assert.ok(values.has("dispatch-request:dispatcher"));
  assert.equal(warnings[0][1].path, "sessions/dispatcher/messages");
  c.response.json = async () => ({dispatched: true});
  await c.sendMessage();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].body.requestId, calls[0].body.requestId, "Manual retry recovers the existing receipt");
  assert.equal(c.$("message").value, "");
  assert.equal(values.has("dispatch-request:dispatcher"), false);
});

test("accepted dispatch followed by refresh failure reports receipt without replaying", async () => {
  const {c, calls, values} = setup();
  c.refreshDetail = async () => { throw new SyntaxError("The string did not match the expected pattern."); };
  await assert.rejects(c.sendMessage(), /request was received.*Do not send it again/);
  assert.equal(calls.length, 1);
  assert.equal(c.$("message").value, "");
  assert.equal(values.has("dispatch-request:dispatcher"), false);
  assert.equal(c.state.busy, false);
});

test("typing a new draft during an accepted send survives a failed refresh", async () => {
  const {c} = setup();
  c.response.json = async () => {
    c.$("message").value = "A different request";
    return {dispatched: true};
  };
  c.refreshList = async () => { throw new Error("Disconnected"); };
  await assert.rejects(c.sendMessage(), /request was received/);
  assert.equal(c.$("message").value, "A different request");
  assert.equal(c.state.drafts.get("dispatcher"), "A different request");
});

test("invalid refresh JSON gives useful context and does not retry the read", async () => {
  const {c, calls} = setup();
  c.response.json = async () => { throw new SyntaxError("The string did not match the expected pattern."); };
  await assert.rejects(c.api("threads?archived=false"), /Could not refresh the workspace/);
  assert.equal(calls.length, 1);
});

test("structured server errors retain their message and the unsent draft", async () => {
  const {c, calls} = setup();
  c.response.ok = false;
  c.response.status = 503;
  c.response.json = async () => ({error: "Ribbon Field is busy. Check the conversation before sending again."});
  await assert.rejects(c.sendMessage(), /Ribbon Field is busy/);
  assert.equal(c.$("message").value, "Review the navigation");
  assert.equal(calls.length, 1);
});
