"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

test("sending during a Claude turn queues instead of issuing unsupported steering", async () => {
  const elements = new Map(), calls = [];
  const c = {$: id => { if (!elements.has(id)) elements.set(id, {value: "follow up"}); return elements.get(id); },
    state: {selected: "one", detail: {running: true, activeTurn: "turn", work_thread: {client: "claude", browser_session: true}},
      drafts: new Map(), commandFeedback: new Map(), closing: new Set()},
    sessionPath: id => "sessions/" + id, renderStatus() {}, refreshDetail: async () => {}, refreshList: async () => {},
    api: async (path, body) => { calls.push({path, body}); return {}; }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), c);
  vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), c);
  await c.sendMessage();
  assert.equal(calls[0].path, "sessions/one/queue");
  assert.equal(calls[0].body.expectedTurnId, undefined);
});
