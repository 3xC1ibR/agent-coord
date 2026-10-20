"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function composer(client) {
  const elements = new Map(), calls = [];
  const c = {$: id => { if (!elements.has(id)) elements.set(id, {value: "follow up"}); return elements.get(id); },
    state: {selected: "one", detail: {running: true, activeTurn: "turn", work_thread: {client, browser_session: true}},
      drafts: new Map(), commandFeedback: new Map(), closing: new Set()},
    sessionPath: id => "sessions/" + id, renderStatus() {}, refreshDetail: async () => {}, refreshList: async () => {},
    api: async (path, body) => { calls.push({path, body}); return {}; }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), c);
  vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), c);
  return {c, calls};
}

for (const client of ["antigravity", "kimi", "muse"]) test(`sending during a ${client} turn queues instead of issuing unsupported steering`, async () => {
  const {c, calls} = composer(client);
  await c.sendMessage();
  assert.equal(calls[0].path, "sessions/one/queue");
  assert.equal(calls[0].body.expectedTurnId, undefined);
});

for (const client of ["codex", "claude"]) test(`sending during a ${client} turn steers the expected turn`, async () => {
  const {c, calls} = composer(client);
  await c.sendMessage();
  assert.equal(calls[0].path, "sessions/one/messages");
  assert.equal(calls[0].body.expectedTurnId, "turn");
});

test("Claude explicit queue keeps the message for after the turn", async () => {
  const {c, calls} = composer("claude");
  await c.sendMessage("queue");
  assert.equal(calls[0].path, "sessions/one/queue");
  assert.equal(calls[0].body.expectedTurnId, undefined);
});

test("Antigravity fixed questions render a select and submit the native option ID", () => {
  const nodes = [];
  const node = (tag, text) => {
    const item = {tag, textContent: text, children: [], dataset: {},
      append(...children) { this.children.push(...children); }, replaceChildren() { this.children = []; }};
    nodes.push(item); return item;
  };
  const area = node("div");
  const c = {node, $: () => area,
    state: {detail: {work_thread: {client: "antigravity"}, requests: [{key: "q", method: "item/tool/requestUserInput", params: {
      questions: [{id: "interaction_1", question: "Which?", allowCustomAnswer: false,
        options: [{value: "native-a", label: "First"}, {value: "native-b", label: "Second"}]}]}}]}},
    requestButton: (label, decision, request, form) => { c.form = form; return node("button", label); }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function renderRequests()"), source.indexOf("function applyEvent(")), c);
  c.renderRequests();
  const select = nodes.find(n => n.tag === "select");
  assert.deepEqual(select.children.map(n => [n.textContent, n.value]), [["First", "native-a"], ["Second", "native-b"]]);
  select.value = "native-b";
  assert.equal(c.form().answers.interaction_1.answers[0], "native-b");
  assert.equal(nodes.find(n => n.tag === "h3").textContent, "Antigravity has a question");
});


test("Kimi questions preserve multiple selections and offer cancel", () => {
  const nodes = [], buttons = [];
  const node = (tag, text) => {
    const item = {tag, textContent: text, children: [], dataset: {},
      append(...children) { this.children.push(...children); }, replaceChildren() { this.children = []; }};
    nodes.push(item); return item;
  };
  const area = node("div");
  const c = {node, $: () => area,
    state: {detail: {work_thread: {client: "kimi"}, requests: [{key: "q", method: "item/tool/requestUserInput", params: {
      allowCancel: true, questions: [{id: "q0", question: "Platforms?", allowCustomAnswer: false, multiSelect: true,
        options: [{value: "macOS", label: "Mac"}, {value: "Linux", label: "Linux"}]}]}}]}},
    requestButton: (label, decision, request, form) => { buttons.push({label, decision, form}); return node("button", label); }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function renderRequests()"), source.indexOf("function applyEvent(")), c);
  c.renderRequests();
  const select = nodes.find(n => n.tag === "select");
  assert.equal(select.multiple, true);
  select.selectedOptions = [{value: "macOS"}, {value: "Linux"}];
  assert.deepEqual(Array.from(buttons[0].form().answers.q0.answers), ["macOS", "Linux"]);
  assert.equal(buttons[1].decision, "cancel");
  assert.equal(nodes.find(n => n.tag === "h3").textContent, "Kimi has a question");
});
