"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const web = "../plugins/agent-coord/scripts/agent_coord/web/";
const source = fs.readFileSync(require.resolve(web + "thread-usage.js"), "utf8");

function setup() {
  const context = vm.createContext({Intl});
  vm.runInContext(source, context);
  return context.agentCoordThreadUsage;
}

function element() {
  const document = {createElement: tag => ({tag, textContent: "", children: [], append(...children) { this.children.push(...children); }})};
  return {ownerDocument: document, dataset: {}, children: [], open: false,
    replaceChildren(...children) { this.children = children; }};
}

test("consumption uses reported totals without adding cache and reasoning subsets", () => {
  const value = setup().view({provider: "claude", status: "available", scope: "including_native_children",
    totals: {input_tokens: 900, output_tokens: 100, total_tokens: 1000, cached_input_tokens: 800, reasoning_output_tokens: 60},
    models: [{model: "one", totals: {total_tokens: 400}}, {model: "two", totals: {total_tokens: 600}}]});
  assert.equal(value.summary, "1K tokens");
  assert.equal(value.scope, "Includes provider subagents");
  assert.deepEqual(Array.from(value.rows, row => Array.from(row)), [["Input", "900"], ["Output", "100"], ["Total", "1,000"],
    ["Cache read · part of input", "800"], ["Reasoning · part of output", "60"]]);
  assert.deepEqual(Array.from(value.models, row => Array.from(row)), [["one", "400"], ["two", "600"]]);
});

test("context occupancy never becomes consumption and missing counters remain unknown", () => {
  const view = setup().view;
  const value = view({provider: "antigravity", status: "unavailable", context: {used_tokens: 53000, limit_tokens: 200000}});
  assert.equal(value.summary, "Tokens unavailable · context 27%");
  assert.equal(value.contextText, "53,000 / 200,000 tokens");
  assert.equal(value.rows.length, 0);
  assert.equal(view({totals: {total_tokens: null}}).summary, "Tokens unavailable");
  assert.equal(view({totals: {total_tokens: -1, input_tokens: "12", output_tokens: true}}).rows.length, 0);
  assert.equal(view({context: {used_tokens: 5, limit_tokens: 0}}).contextText, null);
});

test("partial totals differ from actual reported zero consumption", () => {
  const view = setup().view;
  assert.equal(view({status: "partial", totals: {total_tokens: 1200000}}).summary, "1.2M tokens · partial");
  assert.equal(view({status: "available", totals: {total_tokens: 0}}).summary, "0 tokens");
  assert.equal(view({status: "available", totals: {input_tokens: 1, output_tokens: 2}}).summary, "Tokens unavailable");
});

test("live updates keep the disclosure open, thread switches clear it, and text stays inert", () => {
  const {render} = setup();
  const el = element();
  const usage = {provider: "kimi", status: "partial", scope: "direct", totals: {total_tokens: 50},
    models: [{model: "<img src=x onerror=alert(1)>", totals: {total_tokens: 50}}], note: "<script>bad()</script>"};
  render(el, usage, "one");
  assert.equal(el.hidden, false);
  assert.equal(el.children[0].tag, "summary");
  assert.equal(el.children[0].textContent, "50 tokens · partial");
  el.open = true;
  render(el, {...usage, totals: {total_tokens: 60}}, "one");
  assert.equal(el.open, true);
  const panel = el.children[1];
  assert.ok(panel.children.some(node => node.textContent === "<script>bad()</script>"));
  assert.ok(panel.children.some(node => node.children.some(child => child.textContent === usage.models[0].model)));
  render(el, usage, "two");
  assert.equal(el.open, false);
  render(el, null, null);
  assert.equal(el.hidden, true);
  assert.equal(el.children.length, 0);
});

test("usage notifications update only their thread, including cached background detail", () => {
  const app = fs.readFileSync(require.resolve(web + "app.js"), "utf8");
  const background = {detail: {work_thread: {thread_id: "two"}}, revision: 0};
  let renders = 0;
  const context = vm.createContext({state: {selected: "one", detail: {work_thread: {thread_id: "one"}},
    conversations: new Map([["two", background]])}, renderStatus() { renders++; }});
  vm.runInContext(app.slice(app.indexOf("function applyEvent("), app.indexOf("let listTimer,")), context);
  const usage = {provider: "codex", status: "available", totals: {total_tokens: 40}};
  context.applyEvent({method: "thread/usage/updated", params: {threadId: "two", usage}});
  assert.equal(background.detail.work_thread.usage, usage);
  assert.equal(context.state.detail.work_thread.usage, undefined);
  assert.equal(renders, 0);
  context.applyEvent({method: "thread/usage/updated", params: {threadId: "one", usage}});
  assert.equal(context.state.detail.work_thread.usage, usage);
  assert.equal(renders, 1);
});

test("usage-only event batches avoid overview refetches while turn events still refresh", () => {
  const app = fs.readFileSync(require.resolve(web + "app.js"), "utf8");
  let events, refreshes = 0;
  const context = vm.createContext({state: {config: {}}, base: "/api/browser/", notifications: {receive() {}},
    EventSource: function() { events = this; }, window: {addEventListener() {}}, applyEvent() {},
    scheduleList() { refreshes++; }});
  vm.runInContext(app.slice(app.indexOf("function connect()"), app.indexOf("function newProject()")), context);
  context.connect();
  events.onmessage({data: JSON.stringify({events: [{method: "thread/usage/updated", params: {}},
    {method: "thread/tokenUsage/updated", params: {}}]})});
  assert.equal(refreshes, 0);
  events.onmessage({data: JSON.stringify({events: [{method: "turn/completed", params: {}}]})});
  assert.equal(refreshes, 1);
});
