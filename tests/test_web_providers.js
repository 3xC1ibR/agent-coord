"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

for (const client of ["claude", "antigravity"]) test(`sending during a ${client} turn queues instead of issuing unsupported steering`, async () => {
  const elements = new Map(), calls = [];
  const c = {$: id => { if (!elements.has(id)) elements.set(id, {value: "follow up"}); return elements.get(id); },
    state: {selected: "one", detail: {running: true, activeTurn: "turn", work_thread: {client, browser_session: true}},
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
