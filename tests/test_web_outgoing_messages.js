"use strict";
// Pressing Enter must show the user's message before any network work, with
// honest sending, sent and not-sent states. These tests never resolve the
// request until they choose to, so a regression to "render after acceptance"
// fails deterministically rather than as a timing flake.
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
// The class lives in app.js so that any runtime serving app.js also serves it.
const OutgoingMessages = vm.runInThisContext(source.slice(source.indexOf("class OutgoingMessages {"),
  source.indexOf("function renderTimeline() {")) + "; OutgoingMessages");

class FakeElement {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {}; this.textContent = ""; this.className = ""; this.parent = null; }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); this.parent = null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  get text() { return [this.textContent, ...this.children.map(node => node.text)].filter(Boolean).join(" "); }
  querySelectorAll(selector) {
    assert.equal(selector, "article[data-outgoing]");
    return this.children.filter(node => node.tagName === "ARTICLE" && node.dataset.outgoing);
  }
  querySelector() { return null; }
}
const document = {createElement: tag => new FakeElement(tag)};
const itemText = item => (item.content || []).map(part => part.text || "").join("\n");

function outgoing() { return new OutgoingMessages({itemText}); }
function detailWith(...items) { return {thread: {turns: [{id: "turn", items}]}}; }
const user = (id, text) => ({id, type: "userMessage", content: [{type: "text", text}]});

test("an outgoing message is pending until the provider's copy appears", () => {
  const messages = outgoing();
  const existing = detailWith(user("old", "Run the tests"));
  const entry = messages.add("thread", {text: "Run the tests", detail: existing});
  messages.settle("thread", existing);
  assert.deepEqual(messages.list("thread"), [entry], "an older identical message must not settle a new one");
  messages.accepted(entry);
  messages.settle("thread", existing);
  assert.equal(messages.list("thread").length, 1, "acceptance alone does not remove it before the copy arrives");
  messages.settle("thread", detailWith(user("old", "Run the tests"), user("new", "Run the tests")));
  assert.deepEqual(messages.list("thread"), []);
});

test("a stream event that arrives before the acceptance reply settles the message", () => {
  const messages = outgoing();
  messages.add("thread", {text: "Hello", detail: detailWith()});
  messages.settle("thread", detailWith(user("new", "Context\n\nHello")));
  assert.deepEqual(messages.list("thread"), []);
});

test("identical steers each retain an echo until their own provider copy arrives", () => {
  const messages = outgoing();
  const first = messages.add("thread", {text: "?", detail: detailWith()});
  const second = messages.add("thread", {text: "?", detail: detailWith()});
  messages.accepted(first); messages.accepted(second);
  const firstCopy = detailWith(user("one", "?"));
  messages.settle("thread", firstCopy);
  assert.deepEqual(messages.list("thread"), [second]);
  messages.settle("thread", firstCopy);
  assert.deepEqual(messages.list("thread"), [second], "the same copy cannot settle again on refresh");
  messages.settle("thread", detailWith(user("one", "?"), user("two", "?")));
  assert.deepEqual(messages.list("thread"), []);
});

test("whole text and image counts distinguish messages while compaction delays delivery", () => {
  const messages = outgoing(), prefix = "Long shared prefix ".repeat(20);
  const first = messages.add("thread", {text: prefix + "first", detail: detailWith()});
  const second = messages.add("thread", {text: prefix + "second", detail: detailWith()});
  messages.settle("thread", detailWith(user("two", prefix + "second")));
  assert.deepEqual(messages.list("thread"), [first]);
  const image = messages.add("thread", {text: "", images: 1, detail: detailWith()});
  messages.settle("thread", detailWith(user("other", "unrelated")));
  assert.deepEqual(messages.list("thread"), [first, image]);
  messages.settle("thread", detailWith({id: "photo", type: "userMessage", content: [{type: "image", url: "data:image/png;base64,abc"}]}));
  assert.deepEqual(messages.list("thread"), [first]);
  assert.notEqual(first.key, second.key);
});

test("dispatcher requests settle by their durable request ID", () => {
  const messages = outgoing();
  const entry = messages.add("dispatcher", {text: "Route this", requestId: "request-1", detail: detailWith()});
  messages.accepted(entry);
  messages.settle("dispatcher", {thread: {turns: []}, dispatchRequests: [{request_id: "request-0"}]});
  assert.equal(messages.list("dispatcher").length, 1);
  messages.settle("dispatcher", {thread: {turns: []}, dispatchRequests: [{request_id: "request-1"}]});
  assert.deepEqual(messages.list("dispatcher"), []);
});

test("a rejected message stays visible as not sent until the next attempt", () => {
  const messages = outgoing();
  const entry = messages.add("thread", {text: "Deploy", detail: detailWith()});
  messages.rejected(entry, "Provider unavailable.");
  messages.settle("thread", detailWith(user("new", "Deploy")));
  assert.equal(messages.list("thread")[0].status, "failed");
  const timeline = new FakeElement("div");
  messages.update(document, timeline, "thread", detailWith());
  assert.match(timeline.text, /Not sent · Provider unavailable\./);
  const retry = messages.add("thread", {text: "Deploy", detail: detailWith()});
  assert.deepEqual(messages.list("thread"), [retry]);
});

test("updating in place replaces only outgoing nodes", () => {
  const messages = outgoing();
  const timeline = new FakeElement("div");
  const history = new FakeElement("article");
  timeline.append(history);
  const entry = messages.add("thread", {text: "First", images: 2, detail: detailWith()});
  messages.update(document, timeline, "thread", detailWith());
  assert.equal(timeline.children.length, 2);
  assert.match(timeline.text, /First 2 images Sending…/);
  messages.accepted(entry);
  messages.update(document, timeline, "thread", detailWith());
  assert.equal(timeline.children.length, 2);
  assert.equal(timeline.children[0], history);
  assert.match(timeline.text, /Sent/);
});

function composer({running = false} = {}) {
  const elements = new Map();
  const context = {
    document, OutgoingMessages,
    $: id => {
      if (!elements.has(id)) elements.set(id, id === "timeline" ? new FakeElement("div") : {});
      return elements.get(id);
    },
    state: {busy: false, selected: "one", sessions: [], closing: new Set(), commandFeedback: new Map(), drafts: new Map(),
      conversations: new Map(), timelineThread: "one", timelineScroll: {latest() { context.followed = true; }},
      detail: {running, activeTurn: running ? "turn-one" : null, thread: {turns: []},
        work_thread: {client: "claude", browser_session: true, attention: "now"}}},
    conversationEntry: id => ({detail: id === "one" ? context.state.detail : null}),
    renderSessionSettings() {}, threadGrouping: {status: () => ({label: "Idle"})},
    refreshDetail: async () => {}, refreshList: async () => {}, sessionPath: id => "sessions/" + id, showError() {},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), context);
  vm.runInContext(source.slice(source.indexOf("function renderStatus("), source.indexOf("function itemText(")), context);
  vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), context);
  context.state.outgoing = new OutgoingMessages({itemText});
  context.$("message").value = "Ship the fix";
  return context;
}

for (const running of [false, true]) {
  test(`Enter shows the message before the request settles (${running ? "steering" : "new turn"})`, async () => {
    const c = composer({running});
    let accept;
    c.api = (path, body) => {
      assert.equal(path, "sessions/one/messages");
      assert.equal(body.message, "Ship the fix");
      // Synchronously visible, composer already cleared, nothing awaited yet.
      assert.match(c.$("timeline").text, /Ship the fix Sending…/);
      assert.equal(c.$("message").value, "");
      assert.equal(c.followed, true);
      return new Promise(resolve => { accept = resolve; });
    };
    const sending = c.sendMessage();
    assert.equal(typeof accept, "function", "the request starts without waiting for any other work");
    c.$("message").value = "Next thought";
    accept(running ? {turnId: "turn-one"} : {turn: {id: "turn-two"}});
    await sending;
    assert.match(c.$("timeline").text, /Ship the fix Sent/);
    assert.equal(c.$("message").value, "Next thought", "text typed while sending is kept");
  });
}

test("a rejected send is marked not sent and its text returns to the composer", async () => {
  const c = composer();
  c.api = async () => { throw new Error("Claude Code disconnected."); };
  await assert.rejects(c.sendMessage(), /disconnected/);
  assert.match(c.$("timeline").text, /Ship the fix Not sent · Claude Code disconnected\./);
  assert.equal(c.$("message").value, "Ship the fix");
  assert.equal(c.state.sending.size, 0);
});

test("queued and command input keep their own displays", async () => {
  for (const [mode, text] of [["queue", "Later"], ["steer", "/compact"]]) {
    const c = composer({running: mode === "queue"});
    c.$("message").value = text;
    c.api = async () => ({queued: true, command: mode === "steer" ? {message: "Compacted"} : null});
    await c.sendMessage(mode);
    assert.equal(c.$("timeline").children.length, 0, mode);
  }
});
