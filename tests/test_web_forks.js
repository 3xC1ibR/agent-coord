"use strict";
const {test} = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const AgentMessages = require("../plugins/agent-coord/scripts/agent_coord/web/agent-messages.js");
function element(tag, textContent = "", className = "") {
  return {tag, textContent, className, dataset: {}, children: [],
    append(...nodes) { for (const n of nodes) this.children.push(...(n?.tag === "fragment" ? n.children : [n])); },
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); },
    get childNodes() { return this.children; },
    setAttribute(key, value) { this[key] = value; },
    querySelectorAll() { return []; }};
}
const turn = id => ({id, status: "completed", startedAt: 10, completedAt: 20,
  items: [{type: "agentMessage", text: id}]});
function harness(work = {}, turns = [turn("original"), turn("later")], messages = []) {
  const timeline = element("main"), entry = {}, navigated = [];
  const c = {state: {selected: "test", detail: {work_thread: work, thread: {turns}, coordinationMessages: messages}},
    AgentMessages, node: element, $: () => timeline, document: {createDocumentFragment: () => element("fragment")},
    conversationEntry: () => entry, messageMarkdown: {render: text => text},
    action: fn => fn(), select: id => navigated.push(id), timeline, navigated};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function itemText("), source.indexOf("function requestButton(")), c);
  return c;
}
const markers = c => c.timeline.children.filter(n => n.className === "fork-marker");
const text = n => typeof n === "string" ? n : (n.textContent || "") + n.children.map(text).join("");
const fork = (id, extra = {}) => ({thread_id: id, title: "<Fork " + id + ">", forked_from_turn_id: "original", ...extra});

test("source markers remain at the fork boundary after later turns and link safely", () => {
  const c = harness({forked_sessions: [fork("child/a"), fork("second")]});
  c.renderTimeline();
  assert.equal(markers(c).length, 2);
  assert.equal(c.timeline.children[0].tag, "article");
  assert.equal(c.timeline.children.at(-1).tag, "article");
  assert.match(text(markers(c)[0]), /Forked session <Fork child\/a>/);
  const link = markers(c)[0].children.find(n => n.tag === "a");
  assert.equal(link.href, "#child%2Fa");
  let prevented = false;
  link.onclick({preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.deepEqual(c.navigated, ["child/a"]);
  prevented = false;
  link.onclick({ctrlKey: true, preventDefault() { prevented = true; }});
  assert.equal(prevented, false);
  c.renderTimeline();
  assert.equal(markers(c).length, 2);
});

test("fork origin separates inherited history from new turns and supports nested forks", () => {
  const c = harness({forked_from: {thread_id: "parent", title: "Source"}, forked_from_turn_id: "original",
    forked_sessions: [fork("grandchild", {forked_from_turn_id: "later"})]});
  c.renderTimeline();
  assert.equal(c.timeline.children[1].className, "fork-marker");
  assert.match(text(c.timeline.children[1]), /Forked from Source/);
  assert.match(text(c.timeline.children.at(-1)), /Forked session <Fork grandchild>/);
});

test("empty and unavailable fork boundaries retain navigable markers", () => {
  for (const turns of [[], [turn("unavailable-boundary")]]) {
    const c = harness({forked_from: {thread_id: "parent", title: "Source"}, forked_from_turn_id: null,
      forked_sessions: [fork("child", {forked_from_turn_id: "missing"})]}, turns);
    c.renderTimeline();
    assert.equal(markers(c).length, 2);
    assert.match(text(c.timeline.children[0]), /Forked from/);
  }
});

test("coordination message entries do not duplicate or move the fork boundary", () => {
  const c = harness({forked_sessions: [fork("child")]}, [turn("original"), turn("later")],
    [{id: 1, created_at: 100, body: "Message", direction: "received", counterpart: {title: "Peer"}}]);
  // Keep this test focused on the interleaving contract.
  c.AgentMessages = {...AgentMessages, card: () => element("aside")};
  c.renderTimeline();
  assert.equal(markers(c).length, 1);
  assert.equal(c.timeline.children[1].className, "fork-marker");
  assert.equal(c.timeline.children[2].tag, "article");
});

test("sidebar relationships contain origin and every direct fork with current titles", () => {
  const c = harness(), work = {forked_from: {thread_id: "parent", title: "Renamed source"},
    forked_sessions: [fork("child"), fork("closed-child")]};
  const links = c.forkLinks(work);
  assert.equal(links.children.length, 3);
  assert.match(text(links), /Forked from Renamed source/);
  assert.match(text(links), /Forked session <Fork closed-child>/);
  assert.equal(c.forkLinks({}).children.length, 0);
});

test("fork metadata and title changes invalidate cached timeline nodes", () => {
  const c = harness();
  vm.runInContext(source.slice(source.indexOf("function sameConversation("), source.indexOf("async function select(")), c);
  const detail = {thread: {turns: []}, work_thread: {client: "claude", browser_session: true}};
  assert.equal(c.sameConversation(detail, structuredClone(detail)), true);
  const changed = structuredClone(detail);
  changed.work_thread.forked_sessions = [fork("child")];
  assert.equal(c.sameConversation(detail, changed), false);
  const renamed = structuredClone(changed);
  renamed.work_thread.forked_sessions[0].title = "New fork title";
  assert.equal(c.sameConversation(changed, renamed), false);
});

test("metadata refresh updates markers without a new provider message", async () => {
  const c = harness({thread_id: "test", forked_sessions: []});
  vm.runInContext(source.slice(source.indexOf("async function refreshThread("), source.indexOf("function renderThread(")), c);
  c.isSessionDraft = () => false;
  c.threadPath = id => id;
  c.reconcileAttentionSnapshot = thread => thread;
  c.renderThread = c.renderStatus = () => {};
  c.api = async () => ({thread_id: "test", forked_sessions: [fork("new-child")]});
  c.renderTimeline();
  assert.equal(markers(c).length, 0);
  await c.refreshThread();
  assert.equal(markers(c).length, 1);
  assert.match(text(markers(c)[0]), /new-child/);
});
