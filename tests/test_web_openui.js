"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {actionMessage, reconcileChildren, commit} = require("../plugins/agent-coord/scripts/agent_coord/web/openui.js");
const {normalizeSource, renderError} = require("../plugins/agent-coord/scripts/agent_coord/web/openui-frame.js");

test("saved portfolio layout and tab shorthand adapt to the installed chat catalog", () => {
  const source = fs.readFileSync(require.resolve("./fixtures/openui/portfolio.openui"), "utf8");
  const normalized = normalizeSource(source);
  assert.ok(normalized.startsWith("root = Card([header, kpis, tabs, action_card])"));
  assert.ok(normalized.includes("kpis = Card([kpi1, kpi2, kpi3, kpi4])"));
  assert.ok(normalized.includes('TabItem("1. Top 13 ADVANCE Streams (Ready Now)","1. Top 13 ADVANCE Streams (Ready Now)"\n, ['));
  assert.equal((normalized.match(/@ToAssistant\(/g) || []).length, 4);
  assert.equal(normalizeSource(normalized), normalized);
});

test("compatibility preserves quoted source, comments, references, and current signatures", () => {
  const source = 'root = Card([TextContent("Stack([x]), Grid([a], 4), TabItem(\\\"x\\\", [])"), tabs])\n' +
    '// Stack([a])\n# Grid([a], 4)\n/* TabItem("a", []) */\n' +
    'tabs = Tabs([TabItem("id", "Label", [TextContent("a, b)")])])\n' +
    'Stack = "reference"\nGrid = 4\nTabItem = "reference"';
  assert.equal(normalizeSource(source), source);
});

test("compatibility handles nested calls and leaves unsupported or incomplete signatures diagnosable", () => {
  assert.equal(normalizeSource('root = Stack([Grid([Card([TextContent("a, b)")])], 4)])'),
    'root = Card([Card([Card([TextContent("a, b)")])])])');
  assert.equal(normalizeSource('root = TabItem(label, [TextContent("x")])'),
    'root = TabItem(label,label\n, [TextContent("x")])');
  assert.equal(normalizeSource('root = TabItem("Label" // explanation\n, [TextContent("hello")])'),
    'root = TabItem("Label" // explanation\n,"Label" // explanation\n, [TextContent("hello")])');
  for (const source of ['root = Grid([x], columns)', 'root = Stack([x], "horizontal")',
    'root = Grid([x], 2, "gap")', 'root = Stack([x]', 'root = Stack([x})',
    'root = @Stack([x])', 'root = object.Stack([x])', 'root = "Stack([x])',
    'root = /* Stack([x])']) {
    assert.equal(normalizeSource(source), source);
  }
});

test("render diagnostics identify the failing statement and remain bounded", () => {
  assert.equal(renderError([]), null);
  assert.equal(renderError([{severity: "warning", message: "partial"}]), null);
  assert.equal(renderError([{statementId: "root", message: 'Unknown component "Missing"'}]),
    'OpenUI: root: Unknown component "Missing"');
  assert.ok(renderError([{message: "x".repeat(2000)}]).length < 300);
});

test("the sandbox normalizes rendering, reports recovery, and forwards literal actions once", () => {
  const rendered = [], messages = [], handlers = {};
  const parent = {}, port = {start() {}, postMessage: message => messages.push(message)};
  const body = {setAttribute() {}, getBoundingClientRect: () => ({height: 100})};
  const context = {parent, document: {body, documentElement: {dataset: {}, classList: {toggle() {}}},
    getElementById() {}, addEventListener() {}},
    ResizeObserver: class { observe() {} }};
  context.window = {addEventListener: (name, handler) => { handlers[name] = handler; },
    __OpenUI: {React: {createElement: (_, props) => props}, createRoot: () => ({render: props => rendered.push(props)}),
      Renderer: {}, openuiChatLibrary: {}}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/openui-frame.js"), "utf8"), context);
  handlers.message({source: parent, data: {type: "rf-openui-init"}, ports: [port]});
  port.onmessage({data: {type: "render", response: 'root = Stack([TextContent("hello")])', theme: "dark", disabled: false}});
  assert.equal(rendered.at(-1).response, 'root = Card([TextContent("hello")])');
  rendered.at(-1).onError([{statementId: "root", message: "example"}]);
  assert.equal(messages.at(-1).type, "error");
  rendered.at(-1).onError([]);
  assert.equal(messages.at(-1).type, "rendered");
  const event = {type: "continue_conversation", humanFriendlyMessage: 'Stack([literal]) /close', params: {}};
  rendered.at(-1).onAction(event);
  const action = messages.find(message => message.type === "action");
  assert.equal(action.event, event);
  assert.equal(body.inert, true);
  rendered.at(-1).onAction(event);
  assert.equal(messages.filter(message => message.type === "action").length, 1);
});

test("UI responses preserve action and submitted values as a literal message", () => {
  const event = {type: "to_assistant", message: "/close", formState: {trip: {destination: {value: "Paris"}}}};
  const text = actionMessage(event);
  assert.ok(text.startsWith("OpenUI response:\n"));
  assert.deepEqual(JSON.parse(text.split("\n").slice(1).join("\n")), event);
  assert.throws(() => actionMessage(null, "x"), /Invalid/);
  assert.throws(() => actionMessage([], "x"), /Invalid/);
  assert.throws(() => actionMessage({text: "x".repeat(32768)}, "x"), /too large/);
});

class Element {
  constructor(name) { this.name = name; this.dataset = {}; this.childNodes = []; this.parentNode = null; this.moves = 0; }
  contains(other) { return this === other || this.childNodes.some(child => child.contains(other)); }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() { return this.parentNode?.childNodes[this.parentNode.childNodes.indexOf(this) + 1] || null; }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1); this.parentNode = null;
  }
  insertBefore(child, cursor) {
    child.moves++;
    child.remove();
    this.childNodes.splice(cursor ? this.childNodes.indexOf(cursor) : this.childNodes.length, 0, child);
    child.parentNode = this;
  }
  querySelectorAll() { return []; }
}

test("refresh replaces surrounding rows without detaching an interactive message", () => {
  const timeline = new Element("timeline"), before = new Element("old-tool"), frame = new Element("interactive"), after = new Element("after");
  for (const item of [before, frame, after]) timeline.insertBefore(item, null);
  frame.moves = 0;
  const freshTool = new Element("fresh-tool"), newEnd = new Element("new-end");
  reconcileChildren(timeline, [freshTool, frame, newEnd]);
  assert.deepEqual(timeline.childNodes.map(item => item.name), ["fresh-tool", "interactive", "new-end"]);
  assert.equal(frame.moves, 0);
  assert.equal(before.parentNode, null);
  assert.equal(after.parentNode, null);
});

test("identical messages survive polling while streaming state changes", () => {
  const timeline = new Element("timeline");
  const card = () => {
    const value = new Element("card"); value.dataset = {openuiKey: "thread/turn/item", openuiText: "same", openuiStreaming: "true"}; return value;
  };
  const first = card(); commit(timeline, {childNodes: [first]}, {});
  first.moves = 0;
  const second = card(); second.dataset.openuiStreaming = "false";
  commit(timeline, {childNodes: [second]}, {});
  assert.equal(timeline.firstChild, first);
  assert.equal(first.dataset.openuiStreaming, "false");
  assert.equal(first.moves, 0);
  const third = card(); third.dataset.openuiText = "changed";
  commit(timeline, {childNodes: [third]}, {});
  assert.equal(timeline.firstChild, third);
});

test("late timestamp metadata updates without detaching the cached interactive message", () => {
  const timeline = new Element("timeline"), first = new Element("card"), next = new Element("card");
  for (const card of [first, next]) card.dataset = {openuiKey: "timestamp-thread/item", openuiText: "same"};
  const newTime = {outerHTML: '<time datetime="2026-10-26T16:00:00Z">Oct 26, 11:00 AM</time>'};
  first.time = {outerHTML: '<span>Time unavailable</span>', replaceWith(value) { first.time = value; }};
  first.querySelector = () => first.time;
  next.querySelector = () => newTime;
  commit(timeline, {childNodes: [first]}, {});
  first.moves = 0;
  commit(timeline, {childNodes: [next]}, {});
  assert.equal(timeline.firstChild, first);
  assert.equal(first.time, newTime);
  assert.equal(first.moves, 0);
});

function sender({running = false, archived = false, dispatcher = false, refreshFails = false} = {}) {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const calls = [];
  const state = {selected: "thread-a", detail: {running, work_thread: {browser_session: true, dispatcher, attention: archived ? "archived" : "now"}}};
  const context = {state, console: {warn() {}}, api: async (...args) => calls.push(args), sessionPath: id => "/sessions/" + id,
    refreshDetail: async () => { if (refreshFails) throw Error("offline"); }, refreshList: async () => {},
    DispatchUI: {requestKey: () => "request-id"}, sessionStorage: {}};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("async function sendOpenUIResponse("), source.indexOf("function focusSearchMessage(")), context);
  return {context, calls, state, send: (thread, text) => context.sendOpenUIResponse(thread, text)};
}

test("actions route to their originating thread and queue during a running turn", async () => {
  for (const running of [false, true]) {
    const {send, calls} = sender({running});
    await send("thread-a", "OpenUI response:\n{}");
    assert.equal(calls[0][0], "/sessions/thread-a/" + (running ? "queue" : "messages"));
    assert.equal(calls[0][1].message, "OpenUI response:\n{}");
    assert.equal(calls.length, 1);
  }
  const {send, calls} = sender({dispatcher: true, running: true});
  await send("thread-a", "OpenUI response:\n{}");
  assert.equal(calls[0][0], "/sessions/thread-a/messages");
  assert.equal(calls[0][1].askDispatcher, true);
});

test("closed and switched threads reject an action without sending it", async () => {
  for (const config of [{archived: true}, {}]) {
    const {send, calls} = sender(config);
    await assert.rejects(send(config.archived ? "thread-a" : "thread-b", "response"), /Reopen/);
    assert.equal(calls.length, 0);
  }
});

test("accepted responses remain successful if history refresh fails", async () => {
  const {send, calls} = sender({refreshFails: true});
  await send("thread-a", "response");
  assert.equal(calls.length, 1);
});

test("cached-tab restoration parks and retains interactive previews", () => {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const node = {dataset: {openuiKey: "thread/turn/item"}};
  const timeline = {childNodes: [node], scrollTop: 30};
  let parked;
  const context = {state: {selected: "thread", detail: {}, conversations: new Map()}, isSessionDraft: () => false,
    OpenUIThread: {park: element => { parked = element; }}, $: () => timeline};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function conversationEntry("), source.indexOf("function sameConversation(")), context);
  vm.runInContext("retainConversation()", context);
  assert.equal(context.state.conversations.get("thread").nodes[0], node);
  assert.equal(parked, timeline);
  assert.equal(context.state.conversations.get("thread").scroll, 30);
});
