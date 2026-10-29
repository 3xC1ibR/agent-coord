"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function harness() {
  const element = (tag, textContent = "", className = "") => ({tag, textContent, className, dataset: {}, childNodes: [],
    append(...children) { this.childNodes.push(...children); },
    replaceChildren(fragment) { this.childNodes = fragment.childNodes; },
    setAttribute(name, value) { this[name] = value; },
    querySelectorAll() { return []; }, scrollHeight: 100, scrollTop: 0, clientHeight: 100});
  const timeline = element("div");
  const old = {id: "old", status: "interrupted", items: []};
  const live = {id: "live", status: "inProgress", items: [{id: "compact", type: "contextCompaction"}]};
  const state = {selected: "thread", detail: {running: true, activeTurn: "live", thread: {turns: [old, live]},
    work_thread: {browser_session: true, client: "codex"}}};
  const context = {state, $: () => timeline, node: element,
    document: {createDocumentFragment: () => element("fragment"), createElement: element},
    messageMarkdown: {render: text => text}};
  const entry = {};
  context.conversationEntry = () => entry;
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function itemText("), source.indexOf("function requestButton(")) +
    "; state.outgoing = new OutgoingMessages({itemText});", context);
  return {context, state, timeline, old, live};
}
const all = node => [node, ...node.childNodes.flatMap(all)];
const user = (id, text) => ({id, type: "userMessage", content: [{type: "text", text}]});

test("compaction retains accepted messages exactly once and separates historical stop from running state", () => {
  const {context: c, state, timeline, live} = harness();
  for (const text of ["Did stop work?", "?", "?"]) {
    const entry = state.outgoing.add("thread", {text, detail: state.detail});
    state.outgoing.accepted(entry);
  }
  c.renderTimeline();
  assert.equal(all(timeline).filter(n => n.className === "message user outgoing").length, 3);
  assert.equal(timeline.childNodes[0].textContent, "Earlier turn stopped.");
  assert.equal(timeline.childNodes.at(-1).textContent, "Working…");
  assert.ok(all(timeline).some(n => n.textContent === "Conversation compaction"));

  live.items.push(user("one", "Did stop work?"), user("two", "?"));
  c.renderTimeline();
  assert.equal(all(timeline).filter(n => n.className === "message user outgoing").length, 1);
  assert.equal(all(timeline).filter(n => n.className === "message user").length, 2);
  c.renderTimeline();
  assert.equal(all(timeline).filter(n => n.className === "message user outgoing").length, 1);
  live.items.push(user("three", "?"), {id: "reply", type: "agentMessage", text: "Yes."});
  state.detail.running = false; live.status = "completed";
  c.renderTimeline();
  assert.equal(all(timeline).filter(n => n.className === "message user outgoing").length, 0);
  assert.equal(all(timeline).filter(n => n.className === "message user").length, 3);
  assert.ok(!all(timeline).some(n => n.textContent === "Working…"));
});

test("a genuinely stopped latest turn keeps the stopped footer", () => {
  const {context: c, state, timeline, old} = harness();
  state.detail.thread.turns = [old]; state.detail.running = false; state.detail.activeTurn = null;
  c.renderTimeline();
  assert.equal(timeline.childNodes.at(-1).textContent, "Turn stopped.");
});

test("a new running turn missing from history cannot inherit the stopped turn's prose", () => {
  const {context: c, state, timeline, old} = harness();
  old.items.push({id: "old-prose", type: "agentMessage", text: "Earlier response."});
  state.detail.thread.turns = [old];
  c.renderTimeline();
  assert.ok(all(timeline).some(n => n.textContent === "Earlier turn stopped."));
  assert.equal(timeline.childNodes.at(-1).textContent, "Working…");
});
