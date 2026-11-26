"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup(thread) {
  const element = (tag, text, className) => ({tag, textContent: text, className, dataset: {}, childNodes: [],
    append(...children) { this.childNodes.push(...children); },
    replaceChildren(fragment) { this.childNodes = fragment.childNodes; },
    setAttribute(name, value) { this[name] = value; },
    querySelectorAll() { return []; }, scrollHeight: 100, scrollTop: 0, clientHeight: 100,
    style: {}, listeners: [], addEventListener(name) { this.listeners.push(name); }});
  const timeline = element("div");
  const context = {state: {selected: "test-thread", detail: {thread, work_thread: {browser_session: true}}},
    $: () => timeline, node: element, document: {createDocumentFragment: () => element("fragment")},
    messageMarkdown: {render: text => text}, timeline};
  const disclosureEntry = {};
  context.conversationEntry = () => disclosureEntry;
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
  assert.equal(c.timeline.childNodes[1].childNodes[0].textContent, "Existing request");
  assert.equal(c.timeline.childNodes[2].childNodes[0].innerHTML, "Cached answer");
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

// iOS cancels a touch scroll whenever the page assigns scrollTop. A transcript
// re-sliced on every scroll frame, with anchor corrections after each render,
// pinned long conversations to the bottom on iPhone (Nov 26, 2026).
test("a long transcript renders every shown turn independently of the scroll position", () => {
  const turns = Array.from({length: 80}, (_, index) => ({id: "turn-" + index, items: [
    {type: "userMessage", content: [{text: "Question " + index}]},
    {type: "agentMessage", text: "Answer " + index}]}));
  const c = setup({turns});
  c.conversationEntry().allTurns = true; // the reader chose Show earlier turns
  c.timeline.clientHeight = 600; c.timeline.scrollHeight = 40000; c.timeline.scrollTop = 39400;
  c.renderTimeline();
  const atBottom = c.timeline.childNodes.map(child => child.className + ":" + (child.childNodes?.[0]?.textContent ?? child.textContent));
  c.timeline.scrollTop = 12000;
  c.renderTimeline();
  const reading = c.timeline.childNodes.map(child => child.className + ":" + (child.childNodes?.[0]?.textContent ?? child.textContent));
  assert.deepEqual(reading, atBottom, "rendered rows must not depend on where the reader has scrolled");
  assert.equal(c.timeline.childNodes.filter(child => child.className === "message user").length, 80,
    "every shown turn is rendered");
  assert.equal(c.timeline.childNodes.some(child => child.className === "timeline-spacer"), false);
  assert.deepEqual(c.timeline.listeners.filter(name => name === "scroll"), [],
    "rendering must not attach a scroll-driven re-render to the transcript");
});
