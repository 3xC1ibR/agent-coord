"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup(thread) {
  const element = (tag, text, className) => ({tag, textContent: text, className, childNodes: [],
    append(...children) { this.childNodes.push(...children); },
    replaceChildren(fragment) { this.childNodes = fragment.childNodes; },
    setAttribute(name, value) { this[name] = value; },
    querySelectorAll() { return []; }, scrollHeight: 100, scrollTop: 0, clientHeight: 100});
  const timeline = element("div");
  const context = {state: {detail: {thread, work_thread: {browser_session: true}}},
    $: () => timeline, node: element, document: {createDocumentFragment: () => element("fragment")},
    messageMarkdown: {render: text => text}, timeline};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function itemText("), source.indexOf("function requestButton(")), context);
  return context;
}

test("unavailable history is not presented as a new empty conversation", () => {
  const c = setup({turns: [], historyUnavailable: true});
  c.renderTimeline();
  assert.equal(c.timeline.childNodes.length, 1);
  assert.match(c.timeline.childNodes[0].textContent, /history couldn’t be fully loaded/);
  assert.doesNotMatch(c.timeline.childNodes[0].textContent, /beginning of your session/);
  assert.equal(c.timeline.childNodes[0].role, "status");
});

test("a successfully read empty thread retains the new conversation prompt", () => {
  const c = setup({turns: [], historyUnavailable: false});
  c.renderTimeline();
  assert.match(c.timeline.childNodes[0].textContent, /beginning of your session/);
});

test("history failure preserves cached messages and the notice clears on recovery", () => {
  const thread = {historyUnavailable: true, turns: [{items: [
    {type: "userMessage", content: [{text: "Existing request"}]},
    {type: "agentMessage", text: "Cached answer"}]}]};
  const c = setup(thread);
  c.renderTimeline();
  assert.equal(c.timeline.childNodes.length, 3);
  assert.equal(c.timeline.childNodes[1].childNodes[1].textContent, "Existing request");
  assert.equal(c.timeline.childNodes[2].childNodes[1].innerHTML, "Cached answer");
  thread.historyUnavailable = false;
  c.renderTimeline();
  assert.equal(c.timeline.childNodes.length, 2);
  assert.equal(c.timeline.childNodes[0].className, "message user");
});

test("terminal threads retain their continuation guidance", () => {
  const c = setup({turns: []});
  c.state.detail.work_thread = {browser_session: false, client: "codex"};
  c.renderTimeline();
  assert.match(c.timeline.childNodes[0].textContent, /terminal session/);
});
