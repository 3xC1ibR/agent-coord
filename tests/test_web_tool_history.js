"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function render(turnStatus) {
  const element = (tag, text, className) => ({tag, textContent: text, className, dataset: {}, childNodes: [],
    append(...children) { this.childNodes.push(...children); },
    replaceChildren(fragment) { this.childNodes = fragment.childNodes; },
    setAttribute(name, value) { this[name] = value; },
    querySelectorAll() { return []; }, scrollHeight: 100, scrollTop: 0, clientHeight: 100});
  const timeline = element("div");
  const turn = {id: "turn", status: turnStatus, items: [
    {id: "success", type: "mcpToolCall", tool: "Read file", status: "completed", result: "contents"},
    {id: "uncertain", type: "mcpToolCall", tool: "Read file", status: "unknown",
      statusReason: "Antigravity reported completion without a tool result."},
  ]};
  const context = {state: {selected: "thread", detail: {thread: {turns: [turn]}, work_thread: {browser_session: true}}},
    $: () => timeline, node: element, document: {createDocumentFragment: () => element("fragment")},
    messageMarkdown: {render: text => text}, timeline};
  const entry = {};
  context.conversationEntry = () => entry;
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function itemText("), source.indexOf("function requestButton(")), context);
  context.renderTimeline();
  return {context, timeline, turn};
}

test("unknown tool outcomes remain visible and distinct from the completed turn", () => {
  const {context, timeline, turn} = render("completed");
  const group = timeline.childNodes[0];
  const completion = group.childNodes[0].childNodes.at(-1);
  assert.equal(completion.textContent, "?");
  assert.equal(completion["aria-label"], "Turn completed · 1 tool outcome unknown");
  const unknown = group.childNodes[2];
  assert.equal(unknown.dataset.status, "unknown");
  assert.equal(unknown.childNodes[0].childNodes.at(-1).textContent, "Outcome unknown");
  assert.match(unknown.childNodes[1].textContent, /without a tool result/);
  assert.equal(group.childNodes[1].dataset.status, "completed");
  // A later confirmed result replaces the uncertain label, including cached rows.
  turn.items[1] = {...turn.items[1], status: "completed", result: "late contents"};
  delete turn.items[1].statusReason;
  context.renderTimeline();
  assert.equal(timeline.childNodes[0].childNodes[0].childNodes.at(-1).textContent, "✓");
  assert.equal(timeline.childNodes[0].childNodes[2].dataset.status, "completed");
});

test("turn failure and stop remain separate from unknown tool outcomes", () => {
  for (const [status, label] of [["failed", "Turn failed"], ["interrupted", "Turn stopped"]]) {
    const {timeline} = render(status);
    const group = timeline.childNodes[0];
    assert.equal(group.childNodes[0].childNodes.at(-1)["aria-label"], label + " · 1 tool outcome unknown");
    assert.equal(group.childNodes[2].dataset.status, "unknown");
  }
});
