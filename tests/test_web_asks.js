"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {AsksUI, AsksCopy} = require("../plugins/agent-coord/scripts/agent_coord/web/asks.js");
const {Element, makeDocument} = require("./web_dom_stub.js");

const IDS = ["open-asks-view", "asks-entry-label", "asks-count", "open-asks", "asks-dialog", "sidebar-backdrop", "menu-toggle",
  "asks-title", "asks-summary", "close-asks", "asks-sections", "asks-filters", "asks-filter-project", "asks-filter-kind",
  "asks-filter-since", "asks-notice", "asks-error", "asks-status", "asks-list"];
function setup(open, {config = {askKinds: true}, ids = IDS} = {}) {
  const navigated = [], errors = [], calls = [], timers = [];
  const state = {open, config};
  const document = makeDocument(ids);
  const $ = id => document.getElementById(id);
  $("asks-filter-project").append(Object.assign(new Element("option"), {textContent: "All projects"}));
  $("sidebar-backdrop").hidden = true;
  $("sidebar-backdrop").click = () => { $("sidebar-backdrop").clicked = true; };
  const view = {setInterval: callback => { timers.push(callback); return timers.length; }, clearInterval: id => { if (Number.isInteger(id)) timers[id - 1] = null; }};
  const api = async (path, body) => {
    calls.push({path, body});
    if (path === "config") { if (state.config instanceof Error) throw state.config; return state.config; }
    if (body !== undefined) return {id: Number(path.split("/")[1]), status: "resolved", ...body};
    return {data: state.open};
  };
  const ui = new AsksUI({document, api, view, navigate: url => { navigated.push(url); }, onError: error => errors.push(error),
    copyText: async () => {}, clock: () => Date.now()});
  const open_ = () => $("open-asks-view").onclick({preventDefault() {}});
  return {ui, $, state, navigated, errors, calls, timers, open: open_};
}
const item = {session_id: "thread-7", thread_title: "Checkout", project_label: "Shop", context: null, options: [], status: "open",
  age_seconds: 60, kind: "action", unblocks: "Pricing.", resolutions: ["done", "not_done", "dismissed"], snoozed: false};
const blocking = {...item, id: 7, question: "Add payout details", blocking: true};
const quiet = {...item, id: 8, question: "Upload ID", blocking: false};
const snoozedBlocking = {...item, id: 9, question: "Confirm account", blocking: true, snoozed: true, snoozed_until: "2999-01-01T00:00:00+00:00"};

test("the entry is named from the copy and counts open asks that are not snoozed", async () => {
  const {ui, $, state} = setup([blocking, quiet, snoozedBlocking]);
  assert.equal($("asks-entry-label").textContent, AsksCopy.viewName);
  assert.equal($("open-asks").getAttribute("aria-label"), AsksCopy.viewName);
  assert.equal($("asks-title").textContent, AsksCopy.viewName);
  assert.equal($("asks-count").hidden, true);
  await ui.refresh();
  assert.equal($("asks-count").hidden, false);
  assert.equal($("asks-count").textContent, "2");
  assert.ok($("asks-count").classList.contains("asks-count-blocking"));
  assert.equal($("open-asks-view").title, "2 asks waiting for you · 1 blocking");
  assert.equal($("open-asks").title, $("open-asks-view").title);
  assert.equal($("open-asks-view").getAttribute("aria-label"), AsksCopy.viewName + ": 2 asks waiting for you · 1 blocking");
  // The dialog is closed, so no rows are built for a background refresh.
  assert.equal($("asks-list").byClass("ask-row").length, 0);
  state.open = [quiet, snoozedBlocking];
  await ui.refresh();
  assert.equal($("asks-count").textContent, "1");
  assert.equal($("asks-count").classList.contains("asks-count-blocking"), false);
  assert.equal($("open-asks-view").title, "1 ask waiting for you");
  state.open = [snoozedBlocking];
  await ui.refresh();
  assert.equal($("asks-count").hidden, true);
  assert.equal($("open-asks-view").title, AsksCopy.viewName);
  assert.equal($("open-asks-view").getAttribute("aria-label"), AsksCopy.viewName);
});

test("the sidebar entry and the question-mark button both open the view and start its refresh cycle", async () => {
  const {$, timers, calls, ui} = setup([blocking, quiet]);
  for (const id of ["open-asks-view", "open-asks"]) {
    let prevented = false;
    await $(id).onclick({preventDefault() { prevented = true; }});
    assert.equal(prevented, true, id);
    assert.equal($("asks-dialog").open, true);
    assert.deepEqual($("asks-list").byClass("ask-row").map(row => row.dataset.id), ["7", "8"]);
    $("asks-dialog").close();
    assert.equal(ui.viewer.visible, false);
  }
  assert.deepEqual(calls.filter(call => call.path === "config").length, 2);
  // One live timer while open; closing clears it.
  assert.deepEqual(timers, [null, null]);
  $("sidebar-backdrop").hidden = false;
  await $("open-asks-view").onclick({preventDefault() {}});
  assert.equal($("sidebar-backdrop").clicked, true);
  assert.equal(typeof timers.at(-1), "function");
  const before = calls.length;
  timers.at(-1)();
  assert.equal(calls.length, before + 1);
});

test("resolving in the view moves the sidebar count at once", async () => {
  const {$, open} = setup([blocking, quiet]);
  await open();
  assert.equal($("asks-count").textContent, "2");
  const row = $("asks-list").byClass("ask-row")[0];
  await row.byTag("button").find(button => button.textContent === "Mark done").onclick();
  assert.equal($("asks-count").textContent, "1");
  assert.equal($("asks-count").classList.contains("asks-count-blocking"), false);
});

test("opening a thread or an app link closes the view and navigates in the workspace", async () => {
  const {$, open, navigated} = setup([blocking, {...quiet, link: "agentcoord://view/Needs%20me"}]);
  await open();
  const rows = $("asks-list").byClass("ask-row");
  await rows[0].byClass("ask-thread")[0].onclick();
  assert.deepEqual(navigated, ["agentcoord://thread/thread-7"]);
  assert.equal($("asks-dialog").open, false);
  await open();
  $("asks-list").byClass("ask-row")[1].byClass("ask-link")[0].onclick({preventDefault() {}});
  assert.deepEqual(navigated.at(-1), "agentcoord://view/Needs%20me");
  assert.equal($("asks-dialog").open, false);
  await open();
  $("close-asks").onclick();
  assert.equal($("asks-dialog").open, false);
  assert.equal($("open-asks-view").focused, true);
});

test("a backend from before kinds is detected on opening and served the decisions-only view", async () => {
  const legacyAsk = {id: 3, session_id: "thread-3", thread_title: "Docs", project_name: null, question: "Tone?", context: null,
    options: [], blocking: true, status: "open", age_seconds: 30};
  const {$, open, state, ui, errors} = setup([legacyAsk], {config: {token: "t"}});
  await open();
  assert.equal(ui.viewer.legacy, true);
  assert.deepEqual([$("asks-notice").hidden, $("asks-sections").hidden], [false, true]);
  assert.equal($("asks-count").textContent, "1");
  $("asks-dialog").close();
  // After the update is applied the same window gets the full view on its next opening.
  state.config = {token: "t", askKinds: true};
  await open();
  assert.equal(ui.viewer.legacy, false);
  assert.deepEqual([$("asks-notice").hidden, $("asks-sections").hidden], [true, false]);
  $("asks-dialog").close();
  // A failed capability check reports the error and keeps the last known mode.
  state.config = new Error("Connection lost.");
  await open();
  assert.equal(errors.at(-1).message, "Connection lost.");
  assert.equal(ui.viewer.legacy, false);
});

test("deduplicated footer without #open-asks, composite hydrate, and SSE polling suppression work cleanly", async () => {
  const idsWithoutFooter = IDS.filter(id => id !== "open-asks");
  const {ui, $, calls, timers, errors, open} = setup([blocking, quiet, snoozedBlocking], {ids: idsWithoutFooter});
  assert.equal($("open-asks"), null);

  // Composite /open hydration updates counts and tooltips with zero HTTP fetches.
  ui.hydrate([blocking, quiet, snoozedBlocking]);
  assert.equal(calls.length, 0);
  assert.equal($("asks-count").hidden, false);
  assert.equal($("asks-count").textContent, "2");
  assert.ok($("asks-count").classList.contains("asks-count-blocking"));
  assert.equal($("open-asks-view").title, "2 asks waiting for you · 1 blocking");

  await open();
  assert.equal($("asks-dialog").open, true);
  const tick = timers.at(-1);
  assert.equal(typeof tick, "function");

  try {
    globalThis.agentCoordEventsConnected = true;
    const before = calls.length;
    tick();
    assert.equal(calls.length, before, "SSE-connected windows skip interval refresh polling");

    globalThis.agentCoordEventsConnected = false;
    tick();
    assert.equal(calls.length, before + 1);
  } finally {
    delete globalThis.agentCoordEventsConnected;
  }
  assert.deepEqual(errors, []);
});
