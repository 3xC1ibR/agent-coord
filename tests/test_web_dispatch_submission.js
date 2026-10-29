"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const DispatchUI = require("../plugins/agent-coord/scripts/agent_coord/web/dispatch.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const values = new Map(), elements = new Map(), calls = [], warnings = [], delays = [], errors = [];
  const c = {state: {busy: false, selected: "dispatcher", config: {token: "test"}, drafts: new Map(),
    closing: new Set(), commandFeedback: new Map(), detail: {running: false,
      work_thread: {browser_session: true, dispatcher: true, attention: "now"}}},
    base: "/api/browser/", listRequest: 0, location: {protocol: "http:"},
    console: {warn: (...args) => warnings.push(args)},
    showError: error => errors.push(error.message),
    setTimeout: (callback, delay) => { delays.push(delay); callback(); },
    DispatchUI, sessionStorage: {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)},
    $: id => { if (!elements.has(id)) elements.set(id, {value: "", checked: false}); return elements.get(id); },
    sessionPath: id => "sessions/" + id, isSessionDraft: () => false,
    renderStatus() {}, refreshDetail: async () => {}, refreshList: async () => {},
    fetch: async (path, options) => {
      calls.push({path, body: options.body && JSON.parse(options.body)});
      return typeof c.response === "function" ? c.response(calls.length) : c.response;
    },
    response: {status: 200, ok: true, headers: {get: () => "application/json"}, json: async () => ({dispatched: true})}};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("async function api("), source.indexOf("async function openMentionFile(")), c);
  vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), c);
  c.$("message").value = "Review the navigation";
  return {c, values, calls, warnings, delays, errors};
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
  const {c, calls, values, errors} = setup();
  c.refreshDetail = async () => { throw new SyntaxError("The string did not match the expected pattern."); };
  await c.sendMessage(); await new Promise(resolve => setImmediate(resolve));
  assert.match(errors[0], /message was received.*view could not refresh/);
  assert.equal(calls.length, 1);
  assert.equal(c.$("message").value, "");
  assert.equal(values.has("dispatch-request:dispatcher"), false);
  assert.equal(c.state.busy, false);
});

test("typing a new draft during an accepted send survives a failed refresh", async () => {
  const {c, errors} = setup();
  c.response.json = async () => {
    c.$("message").value = "A different request";
    return {dispatched: true};
  };
  c.refreshList = async () => { throw new Error("Disconnected"); };
  await c.sendMessage(); await new Promise(resolve => setImmediate(resolve));
  assert.match(errors[0], /message was received/);
  assert.equal(c.$("message").value, "A different request");
  assert.equal(c.state.drafts.get("dispatcher"), "A different request");
});

test("persistent invalid refresh JSON gives useful context after one delayed retry", async () => {
  const {c, calls, delays} = setup();
  c.response.json = async () => { throw new SyntaxError("The string did not match the expected pattern."); };
  await assert.rejects(c.api("threads?archived=false"), /Could not refresh the workspace/);
  assert.equal(calls.length, 2);
  assert.deepEqual(delays, [250]);
});

test("a refresh recovers from an interrupted body or proxy HTML without surfacing an error", async () => {
  for (const failure of [new SyntaxError("Unexpected token '<'"), new TypeError("Load failed")]) {
    const {c, calls, warnings} = setup(), valid = c.response;
    c.response = attempt => attempt === 1 ? {...valid, json: async () => { throw failure; }} : valid;
    assert.equal((await c.api("threads?archived=false")).dispatched, true);
    assert.equal(calls.length, 2);
    assert.equal(warnings.length, 1, "retain diagnostics for the failed read");
  }
});

test("a refresh retries transport errors and temporary server failures once", async () => {
  for (const status of [null, 502, 503, 504]) {
    const {c, calls} = setup(), valid = c.response;
    c.response = attempt => {
      if (attempt > 1) return valid;
      if (status === null) throw new TypeError("Failed to fetch");
      return {...valid, status, ok: false, json: async () => ({error: "Temporarily unavailable"})};
    };
    assert.equal((await c.api("organization")).dispatched, true);
    assert.equal(calls.length, 2);
  }
});

test("persistent temporary server failures retain their status and message", async () => {
  const {c, calls} = setup();
  Object.assign(c.response, {status: 503, ok: false, json: async () => ({error: "Ribbon Field is busy."})});
  await assert.rejects(c.api("views"), error => error.status === 503 && /busy/.test(error.message));
  assert.equal(calls.length, 2);
});

test("ordinary server errors and cancelled requests are not retried", async () => {
  for (const status of [400, 401, 403, 404, 500]) {
    const {c, calls} = setup();
    Object.assign(c.response, {status, ok: false, json: async () => ({error: "Rejected"})});
    await assert.rejects(c.api("views"), error => error.status === status);
    assert.equal(calls.length, 1);
  }
  const {c, calls} = setup();
  c.response = () => { throw Object.assign(new Error("Cancelled"), {name: "AbortError"}); };
  await assert.rejects(c.api("views"), /Cancelled/);
  assert.equal(calls.length, 1);
});

test("a cancelled body read and unreadable authentication response are not retried", async () => {
  for (const cancelled of [true, false]) {
    const {c, calls} = setup();
    c.response.status = cancelled ? 200 : 401;
    c.response.json = async () => { throw new TypeError("Load failed"); };
    await assert.rejects(c.api("views", undefined, {signal: {aborted: cancelled}}), /Could not refresh/);
    assert.equal(calls.length, 1);
  }
});

test("unreadable changes and explicit non-GET requests are never replayed", async () => {
  for (const body of [undefined, {pinned: true}]) {
    const {c, calls} = setup();
    c.response.json = async () => { throw new SyntaxError("Unexpected end of JSON input"); };
    await assert.rejects(c.api("threads/dispatcher", body, {method: "POST"}), /Could not/);
    assert.equal(calls.length, 1);
  }
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
