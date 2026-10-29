"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function harness() {
  const timers = new Map(), entries = new Map();
  let timerId = 0, createdDetails = 0, paints = 0, storageWrites = 0, requests = 0;
  function element(tag, textContent = "", className = "") {
    if (tag === "details") createdDetails++;
    return {tag, textContent, className, dataset: {}, children: [], listeners: [], open: false,
      append(...nodes) {
        for (const child of nodes) {
          if (child.tag === "fragment") { this.append(...[...child.children]); continue; }
          if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
          child.parent = this; this.children.push(child);
        }
      },
      replaceChildren(...nodes) { for (const child of this.children) child.parent = null; this.children = []; this.append(...nodes); if (this === timeline) paints++; },
      get childNodes() { return this.children; },
      setAttribute(key, value) { this[key] = value; },
      addEventListener(name, fn) { this.listeners.push({name, fn}); },
      querySelector(selector) { return selector === ".openui-block" && this.innerHTML?.includes("openui-block") ? {} : null; },
      querySelectorAll() { return descendants(this).filter(child => child.tag === "details" && child.dataset.id); }};
  }
  const timeline = element("main");
  const tools = Array.from({length: 465}, (_, i) => ({id: "tool-" + i, type: "commandExecution", command: "test " + i, status: "completed", aggregatedOutput: "Saved output"}));
  const prose = Array.from({length: 73}, (_, i) => ({id: "answer-" + i, type: "agentMessage", text: "Saved answer " + i}));
  const detail = {requests: [], work_thread: {client: "kimi", browser_session: true}, thread: {turns: [
    {id: "history", status: "completed", items: [...tools, ...prose]},
    {id: "live", status: "inProgress", items: [{id: "stream", type: "reasoning", summary: ["initial"]}]}]}};
  const state = {selected: "thread", detail, drafts: new Map(), conversations: entries};
  const c = vm.createContext({state, node: element, $: () => timeline,
    document: {createElement: element, createDocumentFragment: () => element("fragment")},
    sessionStorage: {getItem() { return null; }, setItem() { storageWrites++; }},
    conversationEntry(id) { if (!entries.has(id)) entries.set(id, {revision: 0}); return entries.get(id); },
    messageMarkdown: {render: text => text}, isSessionDraft: () => state.selected === "new-session",
    renderStatus() {}, renderRequests() { requests++; }, scheduleDetail() {},
    setTimeout(fn, delay) { assert.equal(delay, 50); const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); }});
  vm.runInContext(source.slice(source.indexOf("function itemText("), source.indexOf("function focusAgentMessage(")), c);
  vm.runInContext(source.slice(source.indexOf("function scheduleTimelineRender("), source.indexOf("let listTimer, detailTimer;")), c);
  vm.runInContext(source.slice(source.indexOf("function retainConversation("), source.indexOf("function sameConversation(")), c);
  c.renderTimeline();
  return {c, state, timeline, timers, entries,
    stats: () => ({createdDetails, paints, storageWrites, requests}),
    flush() { const pending = [...timers.values()]; timers.clear(); for (const fn of pending) fn(); },
    snapshot(text, type = "reasoning") { c.applyEvent({method: "item/started", params: {threadId: "thread", turnId: "live",
      item: {id: "stream", type, ...(type === "reasoning" ? {summary: [text]} : {text})}}}); }};
}
function descendants(node) { return node.children.flatMap(child => [child, ...descendants(child)]); }

test("a 1500-event replay updates a 539-item conversation with one paint and reuses historical tools", () => {
  const h = harness(), before = h.stats();
  const original = descendants(h.timeline).find(node => node.dataset.id === "tool-0");
  original.open = true;
  for (let i = 0; i < 1500; i++) h.snapshot("chunk " + i);
  assert.equal(h.timers.size, 1);
  assert.equal(h.stats().paints, before.paints, "the SSE callback must not rebuild the DOM per event");
  assert.equal(h.state.detail.thread.turns[1].items[0].summary[0], "chunk 1499");
  h.flush();
  assert.equal(h.stats().paints - before.paints, 1);
  assert.equal(h.stats().createdDetails - before.createdDetails, 2, "only the changed row and turn group are created");
  assert.equal(h.stats().storageWrites - before.storageWrites, 1, "disclosures are persisted once per paint");
  assert.equal(descendants(h.timeline).find(node => node.dataset.id === "tool-0"), original);
  assert.equal(original.open, true);
  assert.equal(original.listeners.length, 1, "reused rows do not accumulate toggle listeners");
});

test("individual streamed deltas accumulate without postponing the scheduled paint", () => {
  const h = harness(); h.snapshot("", "agentMessage"); h.flush();
  const before = h.stats().paints;
  for (let i = 0; i < 100; i++) h.c.applyEvent({method: "item/agentMessage/delta",
    params: {threadId: "thread", turnId: "live", itemId: "stream", delta: "x"}});
  assert.equal(h.timers.size, 1); h.flush();
  assert.equal(h.state.detail.thread.turns[1].items[0].text, "x".repeat(100));
  assert.equal(h.stats().paints - before, 1);
});

test("approval controls remain immediate while a transcript paint is pending", () => {
  const h = harness(); h.snapshot("new content");
  h.c.applyEvent({method: "item/commandExecution/requestApproval", requestKey: "approval", params: {threadId: "thread"}});
  assert.equal(h.stats().requests, 1);
  assert.equal(h.state.detail.requests[0].key, "approval");
  assert.equal(h.timers.size, 1);
});

test("switching tabs before a paint cannot retain stale nodes or paint over the new conversation", () => {
  const h = harness(); h.snapshot("new content"); h.c.retainConversation();
  assert.equal(h.entries.get("thread").nodes, null);
  assert.equal(h.entries.get("thread").detail.thread.turns[1].items[0].summary[0], "new content");
  const before = h.stats().paints;
  h.state.selected = "other"; h.state.detail = {thread: {turns: []}}; h.flush();
  assert.equal(h.stats().paints, before);
});

test("a synchronous completion or refreshed snapshot cancels the pending streamed paint", () => {
  const h = harness(); h.snapshot("last content");
  h.c.applyEvent({method: "turn/completed", params: {threadId: "thread", turn: {id: "live", status: "completed"}}});
  assert.equal(h.timers.size, 0);
  assert.equal(h.state.detail.thread.turns[1].items[0].summary[0], "last content");
  assert.equal(h.state.detail.running, false);
  assert.equal(h.stats().paints, 2);
});

test("tool cache drops removed items and replaces changed rows while retaining others", () => {
  const h = harness(), first = descendants(h.timeline).find(node => node.dataset.id === "tool-0");
  const items = h.state.detail.thread.turns[0].items;
  items[0].aggregatedOutput = "Changed output"; items.splice(1, 1);
  h.c.renderTimeline();
  assert.notEqual(descendants(h.timeline).find(node => node.dataset.id === "tool-0"), first);
  assert.equal(descendants(h.timeline).some(node => node.dataset.id === "tool-1"), false);
  assert.equal(h.entries.get("thread").toolNodes.size, 465);
});


test("reused interactive responses become actionable when streaming ends without replacing the message", () => {
  const h = harness(), turn = h.state.detail.thread.turns[1];
  turn.items = [{id: "interactive", type: "agentMessage", text: "Interactive response"}];
  h.c.messageMarkdown.render = () => '<div class="openui-block"></div>';
  const committed = [];
  h.c.OpenUIThread = {commit(container, fragment) {
    committed.push([...fragment.childNodes]);
    container.replaceChildren(fragment);
  }};
  h.c.renderTimeline();
  const original = committed.at(-1).find(node => node.dataset.openuiKey);
  assert.ok(original);
  assert.equal(original.dataset.openuiStreaming, "true");
  for (const status of ["completed", "inProgress", "failed", "inProgress", "interrupted"]) {
    turn.status = status;
    h.c.renderTimeline();
    const current = committed.at(-1).find(node => node.dataset.openuiKey);
    assert.equal(current, original, "keep the message mounted while refreshing the interactive state");
    assert.equal(current.dataset.openuiStreaming, String(status === "inProgress"));
  }
});
