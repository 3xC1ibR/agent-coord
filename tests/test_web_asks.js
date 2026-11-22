"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {AsksUI} = require("../plugins/agent-coord/scripts/agent_coord/web/asks.js");
const {AsksCopy} = require("../plugins/agent-coord/scripts/agent_coord/web/asks-copy.js");
const {Element, makeDocument} = require("./web_dom_stub.js");

function setup(api) {
  const navigated = [], errors = [];
  const document = makeDocument(["open-asks-view", "asks-entry-label", "asks-count", "open-asks", "asks-dialog", "asks-frame",
    "sidebar-backdrop", "menu-toggle"]);
  const $ = id => document.getElementById(id);
  $("asks-frame").contentWindow = {};
  $("sidebar-backdrop").hidden = true;
  $("sidebar-backdrop").click = () => { $("sidebar-backdrop").clicked = true; };
  const view = new Element("window"); view.location = {origin: "http://127.0.0.1:8765"};
  const ui = new AsksUI({document, api, view, navigate: url => { navigated.push(url); }, onError: error => errors.push(error)});
  const send = (data, overrides = {}) => view.dispatch("message", {origin: view.location.origin, source: $("asks-frame").contentWindow, data, ...overrides});
  return {ui, $, view, navigated, errors, send};
}
const blocking = {id: 7, blocking: true, snoozed: false};
const quiet = {id: 8, blocking: false, snoozed: false};
const snoozedBlocking = {id: 9, blocking: true, snoozed: true};

test("the entry is named from the copy and counts open asks that are not snoozed", async () => {
  const {ui, $} = setup(async () => ({data: [blocking, quiet, snoozedBlocking]}));
  assert.equal($("asks-entry-label").textContent, AsksCopy.viewName);
  assert.equal($("asks-dialog").getAttribute("aria-label"), AsksCopy.viewName);
  assert.equal($("open-asks").getAttribute("aria-label"), AsksCopy.viewName);
  assert.equal($("asks-count").hidden, true);
  await ui.refresh();
  assert.equal($("asks-count").hidden, false);
  assert.equal($("asks-count").textContent, "2");
  assert.ok($("asks-count").classList.contains("asks-count-blocking"));
  assert.equal($("open-asks-view").title, "2 asks waiting for you · 1 blocking");
  assert.equal($("open-asks").title, $("open-asks-view").title);
  assert.equal($("open-asks-view").getAttribute("aria-label"), AsksCopy.viewName + ": 2 asks waiting for you · 1 blocking");
  ui.api = async () => ({data: [quiet, snoozedBlocking]});
  await ui.refresh();
  assert.equal($("asks-count").textContent, "1");
  assert.equal($("asks-count").classList.contains("asks-count-blocking"), false);
  assert.equal($("open-asks-view").title, "1 ask waiting for you");
  ui.api = async () => ({data: [snoozedBlocking]});
  await ui.refresh();
  assert.equal($("asks-count").hidden, true);
  assert.equal($("open-asks-view").title, AsksCopy.viewName);
  assert.equal($("open-asks-view").getAttribute("aria-label"), AsksCopy.viewName);
});

test("the sidebar entry and the question-mark button both open the embedded view", () => {
  const {$} = setup(async () => ({data: []}));
  for (const id of ["open-asks-view", "open-asks"]) {
    $("asks-dialog").open = false; delete $("asks-frame").src;
    let prevented = false;
    $(id).onclick({preventDefault() { prevented = true; }});
    assert.equal(prevented, true, id);
    assert.equal($("asks-frame").src, "/asks?embedded=1");
    assert.equal($("asks-dialog").open, true);
  }
  $("sidebar-backdrop").hidden = false;
  $("open-asks-view").onclick({preventDefault() {}});
  assert.equal($("sidebar-backdrop").clicked, true);
});

test("messages from the embedded view close it, open a thread, or follow an app link", async () => {
  const {$, send, navigated, view} = setup(async () => ({data: []}));
  const open = () => { $("open-asks-view").onclick({preventDefault() {}}); };
  open();
  send({type: "ribbon-field-asks-thread", thread_id: "thread 7"}, {origin: "https://elsewhere.example"});
  send({type: "ribbon-field-asks-thread", thread_id: "thread 7"}, {source: view});
  send({type: "ribbon-field-asks-thread", thread_id: 7});
  send({type: "ribbon-field-asks-navigate", url: "https://example.com"});
  send({type: "unknown"});
  assert.deepEqual(navigated, []);
  assert.equal($("asks-dialog").open, true);
  const intents = [];
  view.agentCoordActivity = {markIntent: surface => intents.push(surface)};
  send({type: "ribbon-field-asks-thread", thread_id: "thread 7"});
  assert.deepEqual(navigated, ["agentcoord://thread/thread%207"]);
  assert.deepEqual(intents, ["decisions"]);
  assert.equal($("asks-dialog").open, false);
  open();
  send({type: "ribbon-field-asks-navigate", url: "agentcoord://view/Needs%20me"});
  assert.deepEqual(navigated.at(-1), "agentcoord://view/Needs%20me");
  assert.equal($("asks-dialog").open, false);
  open();
  send({type: "ribbon-field-close-asks"});
  assert.equal($("asks-dialog").open, false);
});

test("closing the view unloads it, returns focus and refreshes the count", async () => {
  let reads = 0;
  const {$, send} = setup(async () => { reads += 1; return {data: [quiet]}; });
  $("open-asks-view").onclick({preventDefault() {}});
  send({type: "ribbon-field-close-asks"});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal($("asks-frame").src, undefined);
  assert.equal($("open-asks-view").focused, true);
  assert.equal(reads, 1);
  assert.equal($("asks-count").textContent, "1");
});
