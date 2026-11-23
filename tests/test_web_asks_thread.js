"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {AsksUI, AsksCopy} = require("../plugins/agent-coord/scripts/agent_coord/web/asks.js");
const {Element, makeDocument} = require("./web_dom_stub.js");

const NOW = new Date(2026, 10, 22, 10, 0, 0).getTime();
const DAY = 86400000;
const IDS = ["open-asks-view", "asks-entry-label", "asks-count", "open-asks", "asks-dialog", "sidebar-backdrop", "menu-toggle",
  "asks-toggle-count", "thread-asks", "thread-asks-history", "asks-title", "asks-summary", "close-asks", "asks-sections", "asks-filters",
  "asks-filter-project", "asks-filter-kind", "asks-filter-since", "asks-notice", "asks-error", "asks-status", "asks-list"];
const base = {session_id: "thread-1", thread_title: "Checkout", project_label: "Shop", context: null, unblocks: null,
  effort_minutes: null, link: null, due_at: null, options: [], blocking: false, status: "open", age_seconds: 4000,
  idle_seconds: 60, stale: false, stale_reason: null, overdue: false, snoozed: false, snoozed_until: null, thread_closed: false};
const decision = {...base, id: 1, kind: "decision", question: "Which plan leads?", options: ["Monthly", "Annual"],
  blocking: true, resolutions: ["answered", "dismissed"], age_seconds: 3000};
const request = {...base, id: 2, kind: "request", question: "Provide a Stripe test key", unblocks: "A working test purchase.",
  resolutions: ["granted", "declined", "dismissed"], age_seconds: 9000};
const approval = {...base, id: 3, kind: "approval", question: "Approve a $40 budget", overdue: true,
  due_at: "2026-11-20T12:00:00+00:00", resolutions: ["approved", "declined", "dismissed"], age_seconds: 5000};
const snoozed = {...base, id: 4, kind: "action", question: "Upload an ID photo", snoozed: true,
  snoozed_until: "2026-11-23T15:00:00+00:00", unblocks: "Live payments.", resolutions: ["done", "not_done", "dismissed"]};
const elsewhere = {...base, id: 5, kind: "action", question: "Add payout details", session_id: "thread-2", thread_title: "Crawler",
  blocking: true, unblocks: "Pricing.", resolutions: ["done", "not_done", "dismissed"]};
const resolvedAt = days => new Date(NOW - days * DAY).toISOString();
const log = [
  {...base, id: 11, kind: "approval", question: "Approve the launch discount", status: "resolved", resolution: "approved",
    note: "For 30 days only.", answer: null, resolved_at: resolvedAt(1)},
  {...base, id: 12, kind: "decision", question: "Which currency first?", status: "resolved", resolution: "answered",
    note: null, answer: "USD", resolved_at: resolvedAt(3)},
  {...base, id: 13, kind: "review", question: "Read the old memo", status: "resolved", resolution: "accepted",
    note: null, answer: null, resolved_at: resolvedAt(45)},
  {...base, id: 14, kind: "action", question: "Confirm the speaker test", status: "resolved", resolution: "done",
    note: null, answer: null, resolved_at: resolvedAt(2), session_id: "thread-2", thread_title: "Crawler"},
];

function setup(open, {resolved = log, kinds, ids = IDS, respond, composer = true} = {}) {
  const calls = [], errors = [], navigated = [];
  const state = {open, resolved, now: NOW, sent: [], opened: [], requests: 0};
  // The message draft of the conversation the page shows.
  const draft = Object.assign(new Element("textarea"), {setSelectionRange(start, end) { this.selection = [start, end]; }});
  const document = makeDocument(ids);
  const $ = id => document.getElementById(id);
  $("asks-filter-project").append(Object.assign(new Element("option"), {textContent: "All projects"}));
  $("sidebar-backdrop").hidden = true;
  const api = async (path, body) => {
    calls.push({path, body});
    if (path === "config") return {askKinds: kinds !== false};
    if (body !== undefined) return respond ? respond(path, body) : {id: Number(path.split("/")[1]), status: "resolved", ...body};
    return {data: path.includes("status=resolved") ? state.resolved : state.open};
  };
  const ui = new AsksUI({document, api, view: {setInterval() {}, clearInterval() {}}, navigate: url => { navigated.push(url); },
    onError: error => errors.push(error), copyText: async () => {}, clock: () => state.now, ...(kinds === undefined ? {} : {kinds}),
    messageSent: thread => state.sent.push(thread), requestId: () => "request-" + (state.requests += 1),
    openComposer: async thread => { state.opened.push(thread); return composer ? draft : null; }});
  const strip = $("thread-asks"), history = $("thread-asks-history");
  const rows = part => strip ? strip.children.find(node => node.id === "thread-asks-" + part).byClass("ask-row") : [];
  const toggle = part => strip.children.find(node => node.className.includes("thread-asks-" + part) && node.tagName === "button");
  const list = part => strip.children.find(node => node.id === "thread-asks-" + part);
  const control = (node, text) => node.byTag("button").find(button => button.textContent === text);
  const reads = path => calls.filter(call => call.body === undefined && call.path === path).length;
  const posts = () => calls.filter(call => call.body !== undefined);
  return {ui, $, state, strip, history, draft, rows, toggle, list, control, calls, reads, posts, errors, navigated};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test("a conversation lists its own open asks: blocking, overdue, oldest, then snoozed", async () => {
  const {ui, strip, rows, toggle, list} = setup([request, snoozed, elsewhere, approval, decision]);
  await ui.refresh();
  assert.equal(strip.hidden, true, "no conversation is open");
  await ui.showThread("thread-1");
  assert.equal(strip.hidden, false);
  assert.equal(strip.getAttribute("aria-label"), AsksCopy.thread.label);
  assert.deepEqual(rows("open").map(row => row.dataset.id), ["1", "3", "2", "4"]);
  // Unfolded, the blocking row says so itself; the line repeats it only when folded.
  assert.equal(toggle("open").text, "▾ 3 asks waiting for you");
  assert.equal(toggle("open").getAttribute("aria-expanded"), "true");
  assert.equal(toggle("open").getAttribute("aria-controls"), "thread-asks-open");
  assert.equal(list("open").hidden, false);
  const [first, second, , last] = rows("open");
  // The conversation is the thread, so its rows do not name it; no heading says Blocking, so the row does.
  assert.equal(first.byClass("ask-thread").length, 0);
  assert.match(first.text, /Decision asked 50m ago Blocking/);
  assert.doesNotMatch(second.text, /Blocking/);
  assert.match(second.text, /Overdue · due/);
  assert.match(last.text, /Snoozed until/);
  assert.ok(last.byTag("button").some(button => button.textContent === "Show now"));

  await ui.showThread("thread-2");
  assert.deepEqual(rows("open").map(row => row.dataset.id), ["5"]);
  assert.equal(toggle("open").text, "▾ 1 ask waiting for you");
  await ui.showThread("thread-9");
  assert.equal(strip.hidden, true, "a conversation without asks has no strip");
  await ui.showThread(null);
  assert.equal(strip.hidden, true);
});

test("resolving in the conversation posts the kind's outcome, and the strip leaves with the last open ask", async () => {
  const {ui, $, strip, history, rows, control, posts} = setup([decision, request, elsewhere], {resolved: []});
  await ui.refresh();
  await ui.showThread("thread-1");
  assert.equal($("asks-count").textContent, "3");
  const row = rows("open").find(node => node.dataset.id === "2");
  const note = row.byClass("ask-note")[0];
  assert.equal(note.hidden, false, "the reply field is in view on every ask");
  note.value = "  In 1Password under Stripe.  ";
  control(row, "Grant").onclick();
  await settle();
  assert.deepEqual(posts().map(call => [call.path, call.body]),
    [["asks/2", {resolution: "granted", note: "In 1Password under Stripe."}]]);
  assert.deepEqual(rows("open").map(node => node.dataset.id), ["1"]);
  assert.equal($("asks-count").textContent, "2", "the sidebar count moves with the conversation");
  assert.equal($("asks-toggle-count").textContent, "2");
  // The resolution is recorded in the side panel, not above the composer.
  assert.deepEqual(strip.children.map(node => node.className), ["thread-asks-toggle thread-asks-open", "thread-asks-list"]);
  assert.match(history.text, /^Resolved asks Provide a Stripe test key Granted · Request Note In 1Password under Stripe\.$/);

  control(rows("open")[0], "Annual").onclick();
  await settle();
  assert.deepEqual(posts()[1].body, {resolution: "answered", option: "Annual"});
  assert.equal(strip.hidden, true, "with nothing open, nothing about asks sits above the composer");
  assert.equal(history.byClass("thread-asks-history-item").length, 2);
  assert.equal($("asks-count").textContent, "1");
});

test("a failed resolution keeps the row and what was typed", async () => {
  const {ui, rows, control} = setup([request], {resolved: [],
    respond: () => { throw new Error("Ask 2 is already resolved"); }});
  await ui.refresh();
  await ui.showThread("thread-1");
  const row = rows("open")[0];
  row.byClass("ask-note")[0].value = "Kept";
  control(row, "Decline").onclick();
  await settle();
  assert.equal(rows("open").length, 1);
  assert.equal(row.byClass("ask-note")[0].value, "Kept");
  const error = row.byClass("ask-row-error")[0];
  assert.equal(error.hidden, false);
  assert.equal(error.textContent, "Ask 2 is already resolved. Your input is kept.");
  assert.equal(control(row, "Decline").disabled, false);
});

test("a reply from the conversation's strip is sent as the user's message and marks the ask", async () => {
  const {ui, state, rows, posts} = setup([request, elsewhere], {resolved: []});
  await ui.refresh();
  await ui.showThread("thread-1");
  const row = rows("open")[0], field = row.byClass("ask-note")[0];
  field.value = "what do you need for stripe?";
  row.byTag("form")[0].onsubmit({preventDefault() {}});
  await settle();
  assert.deepEqual(posts(), [{path: "sessions/thread-1/messages", body: {
    message: 'Re ask #2 "Provide a Stripe test key" (still open): what do you need for stripe?', requestId: "request-1"}}]);
  assert.deepEqual(state.sent, ["thread-1"]);
  assert.equal(rows("open")[0], row, "the ask stays, in the same row");
  assert.equal(field.value, "");
  assert.equal(row.byClass("ask-replied")[0].text, "You replied just now what do you need for stripe?");
  // The Queue shows the same mark: both read one record.
  assert.deepEqual(ui.viewer.replyOf(request).text, "what do you need for stripe?");
});

test("Quote in message names the ask in the conversation's draft without losing what is there", async () => {
  const {ui, state, draft, rows, control, posts} = setup([request, elsewhere], {resolved: []});
  await ui.refresh();
  await ui.showThread("thread-1");
  const row = rows("open")[0], field = row.byClass("ask-note")[0];
  draft.value = "Half a thought";
  field.value = "which key?";
  control(row, "Quote in message").onclick();
  await settle();
  assert.deepEqual(state.opened, ["thread-1"]);
  assert.equal(draft.value, 'Half a thought\n\nRe ask #2 "Provide a Stripe test key" (still open): which key?');
  assert.equal(draft.focused, true);
  assert.deepEqual(draft.selection, [draft.value.length, draft.value.length]);
  assert.equal(field.value, "");
  assert.equal(posts().length, 0, "nothing is sent until the user sends the message");
  assert.equal(control(row, "Reply in conversation"), undefined, "the conversation is already open");
  // Asking again does not repeat a reference the draft already has.
  draft.value = 'Re ask #2 "Provide a Stripe test key" (still open): ';
  control(row, "Quote in message").onclick();
  await settle();
  assert.equal(draft.value, 'Re ask #2 "Provide a Stripe test key" (still open): ');
});

test("Reply in conversation from the Queue closes it and opens the asking conversation", async () => {
  const {ui, $, state, draft, control} = setup([request, elsewhere], {resolved: []});
  await $("open-asks-view").onclick({preventDefault() {}});
  assert.equal($("asks-dialog").open, true);
  const row = $("asks-list").byClass("ask-row").find(node => node.dataset.id === "5");
  control(row, "Reply in conversation").onclick();
  await settle();
  assert.equal($("asks-dialog").open, false);
  assert.deepEqual(state.opened, ["thread-2"]);
  assert.equal(draft.value, 'Re ask #5 "Add payout details" (still open): ');
});

test("a conversation whose draft cannot take text is opened and the typed text stays with the ask", async () => {
  const {ui, state, draft, rows, control} = setup([request], {resolved: [], composer: false});
  await ui.refresh();
  await ui.showThread("thread-1");
  const field = rows("open")[0].byClass("ask-note")[0];
  field.value = "which key?";
  control(rows("open")[0], "Quote in message").onclick();
  await settle();
  assert.deepEqual(state.opened, ["thread-1"]);
  assert.equal(field.value, "which key?");
  assert.equal(draft.value, "");
});

test("a conversation's resolutions are listed in its side panel, never above the composer", async () => {
  const {ui, strip, history, reads} = setup([decision]);
  await ui.refresh();
  assert.equal(reads("asks?status=resolved"), 0, "the log is not read until a conversation is open");
  assert.equal(history.hidden, true);
  await ui.showThread("thread-1");
  assert.equal(reads("asks?status=resolved"), 1);
  // Its own resolutions, newest first, whatever their age.
  assert.equal(history.hidden, false);
  assert.equal(history.children[0].tagName, "h3");
  assert.equal(history.children[0].textContent, "Resolved asks");
  const items = history.byClass("thread-asks-history-item");
  assert.deepEqual(items.map(item => item.byTag("strong")[0].textContent),
    ["Approve the launch discount", "Which currency first?", "Read the old memo"]);
  assert.match(items[0].text, /^Approve the launch discount Approved · Approval · Nov 21, .* Note For 30 days only\.$/);
  assert.match(items[1].text, /^Which currency first\? Answered · Decision · Nov 19, .* Answer USD$/);
  assert.equal(strip.byClass("ask-row-resolved").length, 0);

  // A conversation with nothing open shows nothing above its composer; its history is still in the panel.
  await ui.showThread("thread-2");
  assert.equal(reads("asks?status=resolved"), 1, "the log was read within the last minute");
  assert.equal(strip.hidden, true);
  assert.deepEqual(history.byTag("strong").map(node => node.textContent), ["Confirm the speaker test"]);
  await ui.showThread("thread-9");
  assert.equal(history.hidden, true);
  assert.equal(history.children.length, 0);
});

test("the side panel lists the ten most recent resolutions and says where the rest are", async () => {
  const many = Array.from({length: 13}, (_, index) => ({...base, id: 100 + index, kind: "action", question: "Step " + index,
    status: "resolved", resolution: "done", note: null, answer: null, resolved_at: resolvedAt(index)}));
  const {ui, history} = setup([], {resolved: many});
  await ui.refresh();
  await ui.showThread("thread-1");
  assert.equal(history.byClass("thread-asks-history-item").length, 10);
  assert.equal(history.children.at(-1).textContent, "3 earlier in the Queue's Resolved section.");
});

test("the resolved log is reread after a minute and when an ask is resolved elsewhere", async () => {
  const {ui, state, history, rows, reads} = setup([decision, request]);
  await ui.refresh();
  await ui.showThread("thread-1");
  await ui.showThread("thread-2");
  assert.equal(reads("asks?status=resolved"), 1);
  state.now += 61000;
  await ui.showThread("thread-1");
  assert.equal(reads("asks?status=resolved"), 2);

  // Ask 2 was granted from another device: it leaves the open list and enters the log.
  state.open = [decision];
  state.resolved = [{...request, status: "resolved", resolution: "granted", note: null, answer: null, resolved_at: resolvedAt(0)}, ...log];
  await ui.refresh();
  await settle();
  assert.equal(reads("asks?status=resolved"), 3);
  assert.deepEqual(rows("open").map(row => row.dataset.id), ["1"]);
  assert.deepEqual(history.byTag("strong").map(node => node.textContent),
    ["Provide a Stripe test key", "Approve the launch discount", "Which currency first?", "Read the old memo"]);
  // An unchanged list reads nothing more.
  await ui.refresh();
  await settle();
  assert.equal(reads("asks?status=resolved"), 3);
});

test("folding is remembered for each conversation and a typed note survives a refresh", async () => {
  const {ui, state, rows, toggle, list} = setup([decision, request, elsewhere], {resolved: []});
  await ui.refresh();
  await ui.showThread("thread-1");
  const input = rows("open")[0].byClass("ask-note")[0];
  input.value = "Annual, with a monthly toggle";
  state.open = [{...decision, age_seconds: 7300}, request, elsewhere];
  await ui.refresh();
  assert.equal(rows("open")[0].byClass("ask-note")[0], input, "the row is reused");
  assert.equal(input.value, "Annual, with a monthly toggle");
  assert.match(rows("open")[0].text, /asked 2h ago/);

  toggle("open").onclick();
  await settle();
  assert.equal(list("open").hidden, true);
  // Folded, the line says what blocks and names the first ask.
  assert.equal(toggle("open").text, "▸ 2 asks waiting for you 1 blocking Which plan leads?");
  assert.equal(toggle("open").getAttribute("aria-expanded"), "false");
  await ui.refresh();
  assert.equal(list("open").hidden, true, "a refresh does not unfold it");
  await ui.showThread("thread-2");
  assert.equal(list("open").hidden, false, "another conversation keeps its own state");
  await ui.showThread("thread-1");
  assert.equal(list("open").hidden, true);
  toggle("open").onclick();
  await settle();
  assert.equal(list("open").hidden, false);
});

test("one ask is shown in full and the others by their title, opened one at a time", async () => {
  const {ui, state, rows, control} = setup([request, approval, decision], {resolved: []});
  await ui.refresh();
  await ui.showThread("thread-1");
  const folded = () => rows("open").map(row => [row.dataset.id, row.className.includes("ask-row-folded")]);
  const title = id => rows("open").find(row => row.dataset.id === String(id)).byClass("ask-title-toggle")[0];
  assert.deepEqual(folded(), [["1", false], ["3", true], ["2", true]], "the first in order is shown in full");
  assert.deepEqual([1, 3, 2].map(id => title(id).getAttribute("aria-expanded")), ["true", "false", "false"]);
  assert.equal(title(2).textContent, "Provide a Stripe test key");
  assert.equal(title(2).parent.tagName, "h4");
  // An ask that sorts first arriving while the conversation is open does not displace the one in full.
  const earlier = {...decision, id: 30, question: "Pause the rollout?", age_seconds: 88888};
  state.open = [request, approval, decision, earlier];
  await ui.refresh();
  assert.deepEqual(folded(), [["30", true], ["1", false], ["3", true], ["2", true]]);
  // Reopening the conversation starts again from the first in order.
  await ui.showThread(null);
  await ui.showThread("thread-1");
  assert.deepEqual(folded(), [["30", false], ["1", true], ["3", true], ["2", true]]);
  state.open = [request, approval, decision];
  await ui.refresh();
  title(2).onclick();
  await settle();
  assert.deepEqual(folded(), [["1", true], ["3", true], ["2", false]], "opening one closes the other");

  // A more urgent ask arriving does not take the place of the one being answered.
  const urgent = {...decision, id: 20, question: "Stop the campaign?", age_seconds: 99999};
  state.open = [request, approval, decision, urgent];
  await ui.refresh();
  assert.deepEqual(folded(), [["20", true], ["1", true], ["3", true], ["2", false]]);
  // Once it is resolved, the first in order is shown again.
  control(rows("open")[3], "Grant").onclick();
  await settle();
  assert.deepEqual(folded(), [["20", false], ["1", true], ["3", true]]);
  // Its own title closes it; then every ask shows as a title.
  title(20).onclick();
  await settle();
  assert.deepEqual(folded().map(([, isFolded]) => isFolded), [true, true, true]);
  state.open = [approval, decision, urgent];
  await ui.refresh();
  assert.deepEqual(folded().map(([, isFolded]) => isFolded), [true, true, true], "a refresh does not reopen one");
});

test("snoozing in the conversation keeps the ask there, and a conversation with only snoozed asks starts folded", async () => {
  const {ui, state, $, rows, toggle, list, posts} = setup([request], {resolved: [],
    respond: (path, body) => ({id: 2, snoozed_until: body.until == null ? null : "2026-11-23T15:00:00+00:00"})});
  await ui.refresh();
  await ui.showThread("thread-1");
  const select = rows("open")[0].byClass("ask-snooze")[0];
  select.value = "tomorrow";
  select.onchange();
  await settle();
  assert.equal(posts()[0].path, "asks/2/snooze");
  assert.equal($("asks-count").hidden, true, "a snoozed ask leaves the count");
  // Nothing waits any more, so the strip folds to one line; the ask is still a tap away.
  assert.equal(toggle("open").text, "▸ 1 snoozed ask");
  assert.equal(list("open").hidden, true);
  toggle("open").onclick();
  await settle();
  assert.equal(list("open").hidden, false);
  assert.match(rows("open")[0].text, /Snoozed until/);
  rows("open")[0].byTag("button").find(button => button.textContent === "Show now").onclick();
  await settle();
  assert.deepEqual(posts()[1].body, {until: null});
  assert.equal(toggle("open").text, "▾ 1 ask waiting for you");

  state.open = [{...snoozed, session_id: "thread-3"}];
  await ui.refresh();
  await ui.showThread("thread-3");
  assert.equal(toggle("open").text, "▸ 1 snoozed ask");
  assert.equal(list("open").hidden, true);
});

test("a backend from before kinds gets decisions answered with the bodies it accepts and no resolved log", async () => {
  const legacy = {...decision, kind: undefined, resolutions: undefined};
  const {ui, strip, rows, control, posts, reads} = setup([legacy], {kinds: false});
  await ui.refresh();
  await ui.showThread("thread-1");
  assert.equal(reads("asks?status=resolved"), 0);
  assert.equal(strip.hidden, false);
  const row = rows("open")[0];
  assert.equal(row.byClass("ask-snooze").length, 0);
  assert.equal(control(row, "Dismiss"), undefined);
  control(row, "Monthly").onclick();
  await settle();
  assert.deepEqual(posts()[0].body, {option: "Monthly"});
  assert.equal(strip.hidden, true);
  assert.equal(reads("asks?status=resolved"), 0);
});

test("the navigation toggle carries the open count while the sidebar entry is out of sight", async () => {
  const {ui, $, state} = setup([decision, request, snoozed]);
  assert.equal($("asks-toggle-count").hidden, true);
  assert.equal($("menu-toggle").getAttribute("aria-description"), null);
  await ui.refresh();
  for (const id of ["asks-count", "asks-toggle-count"]) {
    assert.equal($(id).hidden, false);
    assert.equal($(id).textContent, "2", "snoozed asks are not counted");
    assert.equal($(id).classList.contains("asks-count-blocking"), true);
  }
  assert.equal($("menu-toggle").getAttribute("aria-description"), "2 asks waiting for you · 1 blocking");
  state.open = [request];
  await ui.refresh();
  assert.equal($("asks-toggle-count").textContent, "1");
  assert.equal($("asks-toggle-count").classList.contains("asks-count-blocking"), false);
  state.open = [];
  await ui.refresh();
  assert.equal($("asks-toggle-count").hidden, true);
  assert.equal($("asks-toggle-count").textContent, "");
  assert.equal($("menu-toggle").getAttribute("aria-description"), null);
});

test("a page without the strip or the toggle count still counts and opens the view", async () => {
  const ids = IDS.filter(id => !["thread-asks", "thread-asks-history", "asks-toggle-count", "menu-toggle"].includes(id));
  const {ui, $, reads, errors} = setup([decision], {ids});
  await ui.refresh();
  await ui.showThread("thread-1");
  assert.equal($("asks-count").textContent, "1");
  assert.equal(reads("asks?status=resolved"), 0, "nothing shows the log, so it is not read");
  assert.deepEqual(errors, []);
});
