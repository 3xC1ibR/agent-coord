"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {AsksView, formatAge, formatEffort, snoozeUntil, openGroups, linkTarget, splitQuestion, isLong} = require("../plugins/agent-coord/scripts/agent_coord/web/asks-page.js");
const {AsksCopy} = require("../plugins/agent-coord/scripts/agent_coord/web/asks-copy.js");
const {Element, makeDocument} = require("./web_dom_stub.js");

const NOW = new Date(2026, 10, 22, 10, 0, 0).getTime();
const base = {session_id: "thread-1", thread_title: "Checkout <b>", project_label: "Shop", context: null, unblocks: null,
  effort_minutes: null, link: null, due_at: null, options: [], blocking: false, status: "open", age_seconds: 4000,
  idle_seconds: 60, stale: false, stale_reason: null, overdue: false, snoozed: false, snoozed_until: null, thread_closed: false};
const action = {...base, id: 1, kind: "action", question: "Add payout billing details in Apify", blocking: true,
  unblocks: "Pricing for all 18 verified actors.", effort_minutes: 10, link: "https://console.apify.com/billing",
  resolutions: ["done", "not_done", "dismissed"], age_seconds: 9 * 3600};
const approval = {...base, id: 2, kind: "approval", question: "Approve a $40 test budget", overdue: true,
  due_at: "2026-11-20T12:00:00+00:00", resolutions: ["approved", "declined", "dismissed"], age_seconds: 3 * 86400};
const decision = {...base, id: 3, kind: "decision", question: "Which format?", context: "Default: CSV.", options: ["CSV", "JSON"],
  project_label: "Crawler", resolutions: ["answered", "dismissed"], session_id: "thread-3", thread_title: "Crawler"};
const request = {...base, id: 4, kind: "request", question: "Provide a SerpAPI key", unblocks: "Removes the rate limit.",
  project_label: "Crawler", resolutions: ["granted", "declined", "dismissed"], age_seconds: 5000};
const review = {...base, id: 5, kind: "review", question: "Read the pricing memo", project_label: null,
  resolutions: ["accepted", "changes_requested", "dismissed"], stale: true, stale_reason: "inactive", idle_seconds: 8 * 86400};
const closed = {...base, id: 6, kind: "decision", question: "Tone?", project_label: null, resolutions: ["answered", "dismissed"],
  stale: true, stale_reason: "thread_closed", thread_closed: true, age_seconds: 30};
const snoozed = {...base, id: 7, kind: "action", question: "Upload an ID photo", blocking: true, snoozed: true,
  snoozed_until: "2026-11-23T15:00:00+00:00", unblocks: "Live payments.", resolutions: ["done", "not_done", "dismissed"]};
const resolvedItems = [
  {...base, id: 11, kind: "request", question: "Provide dataset access", status: "resolved", resolution: "granted",
    note: "Read access for 30 days.", answer: null, resolved_at: "2026-11-21T18:00:00+00:00", project_label: "Crawler"},
  {...base, id: 12, kind: "decision", question: "How long?", status: "resolved", resolution: "answered", answer: "60 minutes",
    note: null, resolved_at: "2026-11-10T18:00:00+00:00", project_label: "Shop"},
  {...base, id: 13, kind: "action", question: "Confirm the speaker test", status: "resolved", resolution: "dismissed",
    note: null, answer: null, resolved_at: "2026-11-01T18:00:00+00:00", project_label: null},
];

function setup(open, {resolved = resolvedItems, respond, embedded = true} = {}) {
  const calls = [], opened = [], navigated = [], copied = [];
  const state = {open, resolved, closed: 0};
  const document = makeDocument(["asks-title", "asks-summary", "close-asks", "asks-sections", "asks-filters",
    "asks-filter-project", "asks-filter-kind", "asks-filter-since", "asks-error", "asks-status", "asks-list"]);
  const $ = id => document.getElementById(id);
  $("close-asks").hidden = true;
  $("asks-filter-project").append(Object.assign(new Element("option"), {textContent: "All projects"}));
  const api = async (path, body) => {
    calls.push({path, body});
    if (body !== undefined) return respond ? respond(path, body) : {id: Number(path.split("/")[1]), status: "resolved", ...body};
    return {data: path.includes("status=resolved") ? state.resolved : state.open};
  };
  const view = new AsksView({document, api, clock: () => NOW, openThread: id => opened.push(id),
    navigate: embedded ? url => navigated.push(url) : null, close: embedded ? () => { state.closed += 1; } : null,
    copyText: async text => { copied.push(text); }});
  const list = $("asks-list");
  const rows = () => list.byClass("ask-row");
  const row = id => rows().find(node => node.dataset.id === String(id));
  const control = (node, text) => node.byTag("button").find(button => button.textContent === text);
  const posts = () => calls.filter(call => call.body !== undefined);
  return {view, $, list, rows, row, control, calls, posts, opened, navigated, copied, state, document};
}

test("helpers format ages, effort and snooze times", () => {
  assert.deepEqual([formatAge(-5), formatAge(30), formatAge(120), formatAge(4000), formatAge(200000)], ["just now", "just now", "2m", "1h", "2d"]);
  assert.deepEqual([formatEffort(null), formatEffort(0), formatEffort(5), formatEffort(60), formatEffort(95)], ["", "", "5 min", "1 h", "1 h 35 min"]);
  const at = choice => new Date(snoozeUntil(choice, NOW) * 1000);
  assert.equal(at("hour").getTime(), NOW + 3600000);
  assert.equal(at("three_hours").getTime(), NOW + 3 * 3600000);
  assert.deepEqual([at("tomorrow").getDate(), at("tomorrow").getHours(), at("tomorrow").getMinutes()], [23, 9, 0]);
  assert.deepEqual([at("week").getDate(), at("week").getHours()], [29, 9]);
  assert.equal(snoozeUntil("", NOW), null);
});

test("open asks group as blocking, then overdue, then projects, oldest first", () => {
  const later = {...action, id: 20, age_seconds: 60};
  const groups = openGroups([decision, review, later, request, approval, action, closed]);
  assert.deepEqual(groups.map(group => [group.key, group.title, group.urgent, group.items.map(ask => ask.id)]), [
    ["blocking", "Blocking", true, [1, 20]], ["overdue", "Overdue", true, [2]],
    ["project:Crawler", "Crawler", false, [4, 3]], ["project:", "No project", false, [5, 6]]]);
  assert.deepEqual(openGroups([]), []);
});

test("a link leads to a web page, a place in the app, an HTML result, or a path to copy", () => {
  assert.deepEqual(linkTarget({link: null}), null);
  assert.deepEqual(linkTarget(action), {type: "url", href: "https://console.apify.com/billing", label: "Open console.apify.com"});
  assert.deepEqual(linkTarget({link: "agentcoord://thread/abc"}), {type: "app", href: "agentcoord://thread/abc", label: "Open in Ribbon Field"});
  assert.deepEqual(linkTarget({session_id: "t 1", link: "/work/My Reports/result.html"}),
    {type: "preview", label: "Open result.html", href: "/html-preview?thread=t+1&path=%2Fwork%2FMy+Reports%2Fresult.html"});
  assert.deepEqual(linkTarget({link: "/work/data.csv"}), {type: "path", path: "/work/data.csv", label: "Copy path"});
  assert.deepEqual(linkTarget({link: "javascript:alert(1)"}), null);
});

test("a long ask leads with its first line and collapses the rest", async () => {
  assert.deepEqual(splitQuestion("  One step left.\n\n1. Open the console.\n2. Add billing.  "),
    {title: "One step left.", detail: "1. Open the console.\n2. Add billing."});
  assert.deepEqual(splitQuestion("Which format?"), {title: "Which format?", detail: ""});
  assert.deepEqual([isLong("Short."), isLong("x".repeat(321)), isLong("a\nb\nc\nd\ne")], [false, true, true]);
  const steps = "1. Open the console.\n2. Add billing details.\n3. Verify identity.\n4. Close the dialog.\n5. Reply here.";
  const long = {...decision, id: 30, question: "One Apify step left that only you can do.\n" + steps, context: "c".repeat(400)};
  const {view, row, control} = setup([long, decision]);
  await view.refresh();
  assert.equal(row(30).byClass("ask-title")[0].textContent, "One Apify step left that only you can do.");
  const detail = row(30).byClass("ask-detail")[0], context = row(30).byClass("ask-context")[0];
  assert.equal(detail.textContent, steps);
  assert.deepEqual([detail.className, context.className], ["ask-detail ask-clamped", "ask-context ask-clamped"]);
  const [more] = row(30).byClass("ask-more");
  assert.deepEqual([more.textContent, more.getAttribute("aria-expanded")], ["Show more", "false"]);
  await more.onclick();
  assert.deepEqual([detail.className, more.textContent, more.getAttribute("aria-expanded")], ["ask-detail", "Show less", "true"]);
  assert.equal(context.className, "ask-context ask-clamped");
  await more.onclick();
  assert.equal(detail.className, "ask-detail ask-clamped");
  // An expanded passage survives a refresh, because the row is reused.
  await more.onclick();
  await view.refresh();
  assert.equal(row(30).byClass("ask-detail")[0].className, "ask-detail");
  assert.equal(row(3).byClass("ask-more").length, 0);
  assert.equal(row(3).byClass("ask-detail").length, 0);
});

test("the view is named from the copy alone", () => {
  const original = AsksCopy.viewName;
  AsksCopy.viewName = "Inbox";
  try {
    const {$, document} = setup([]);
    assert.equal($("asks-title").textContent, "Inbox");
    assert.equal(document.title, "Inbox — Ribbon Field");
    assert.equal($("asks-sections").getAttribute("aria-label"), "Inbox sections");
  } finally { AsksCopy.viewName = original; }
});

test("the open section shows each ask's kind, gain, effort, thread and state", async () => {
  const {view, $, list, row, control, opened, state} = setup([action, approval, decision, request, review, closed, snoozed]);
  assert.equal($("asks-status").textContent, "Loading…");
  await view.refresh();
  assert.equal($("asks-status").hidden, true);
  assert.equal($("asks-title").textContent, AsksCopy.viewName);
  assert.equal($("close-asks").hidden, false);
  $("close-asks").onclick();
  assert.equal(state.closed, 1);
  const tabs = $("asks-sections").children;
  assert.deepEqual(tabs.map(tab => [tab.text, tab.getAttribute("aria-selected")]), [["Open 6", "true"], ["Snoozed 1", "false"], ["Resolved", "false"]]);
  assert.equal(tabs[0].children[1].className, "asks-section-count asks-section-count-blocking");
  assert.deepEqual(list.byClass("ask-group-title").map(node => node.text), ["Blocking 1", "Overdue 1", "Crawler 2", "No project 2"]);
  assert.deepEqual(list.byClass("ask-row").map(node => node.dataset.id), ["1", "2", "4", "3", "5", "6"]);
  const first = row(1);
  assert.equal(first.byClass("ask-title")[0].textContent, "Add payout billing details in Apify");
  assert.equal(first.byClass("ask-meta")[0].text, "Action about 10 min asked 9h ago Checkout <b>");
  assert.equal(first.byClass("ask-unblocks")[0].text, "Unblocks Pricing for all 18 verified actors.");
  const link = first.byClass("ask-link")[0];
  assert.deepEqual([link.tagName, link.href, link.target, link.rel, link.textContent],
    ["a", "https://console.apify.com/billing", "_blank", "noopener noreferrer", "Open console.apify.com"]);
  assert.deepEqual(first.byTag("button").map(button => button.textContent), ["Checkout <b>", "Mark done", "Not done", "Dismiss"]);
  assert.equal(first.byClass("ask-note")[0].placeholder, "Note for the agent (optional)");
  assert.equal(row(2).byClass("ask-meta")[0].text, "Approval asked 3d ago Checkout <b> Overdue · due Nov 20");
  assert.equal(row(2).byClass("ask-flag-urgent").filter(node => !node.hidden).length, 1);
  assert.deepEqual(row(2).byTag("button").map(button => button.textContent).slice(1), ["Approve", "Decline", "Dismiss"]);
  assert.deepEqual(row(4).byTag("button").map(button => button.textContent).slice(1), ["Grant", "Decline", "Dismiss"]);
  assert.deepEqual(row(5).byTag("button").map(button => button.textContent).slice(1), ["Accept", "Request changes", "Dismiss"]);
  assert.deepEqual(row(3).byTag("button").map(button => button.textContent), ["Crawler", "CSV", "JSON", "Answer", "Dismiss"]);
  assert.equal(row(3).byClass("ask-context")[0].textContent, "Default: CSV.");
  assert.equal(row(3).byClass("ask-note")[0].placeholder, "Or answer in your own words…");
  assert.equal(row(6).byClass("ask-note")[0].placeholder, "Your answer…");
  // A stale ask says why, and a closed thread is named before the user resolves anything.
  assert.match(row(5).byClass("ask-meta")[0].text, /Stale · thread idle 8d$/);
  assert.match(row(6).byClass("ask-meta")[0].text, /asked just now Checkout <b> Stale · thread closed$/);
  await control(row(3), "Crawler").onclick();
  assert.deepEqual(opened, ["thread-3"]);
});

test("resolutions post the kind's outcome with the note and remove the row", async () => {
  const {view, row, rows, control, posts, $} = setup([action, approval, request, review]);
  await view.refresh();
  row(1).byClass("ask-note")[0].value = "  Card ending 4242.  ";
  await control(row(1), "Mark done").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/1", body: {resolution: "done", note: "Card ending 4242."}});
  assert.equal(row(1), undefined);
  await control(row(2), "Decline").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/2", body: {resolution: "declined"}});
  row(4).byClass("ask-note")[0].value = "The key is in 1Password.";
  // Enter in a note never chooses a resolution.
  let prevented = false;
  assert.equal(row(4).byTag("form")[0].onsubmit({preventDefault() { prevented = true; }}), undefined);
  assert.equal(prevented, true);
  assert.equal(posts().length, 2);
  await control(row(4), "Grant").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/4", body: {resolution: "granted", note: "The key is in 1Password."}});
  await control(row(5), "Dismiss").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/5", body: {resolution: "dismissed"}});
  assert.equal(rows().length, 0);
  assert.equal($("asks-list").byClass("asks-empty")[0].textContent, AsksCopy.empty.open);
  assert.equal($("asks-sections").children[0].text, "Open");
});

test("a decision is answered by option or in free text", async () => {
  const {view, row, control, posts} = setup([decision, closed]);
  await view.refresh();
  const form = row(3).byTag("form")[0], input = row(3).byClass("ask-note")[0];
  await form.onsubmit({preventDefault() {}});
  assert.equal(posts().length, 0);
  input.value = "JSON later";
  await control(row(3), "CSV").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/3", body: {resolution: "answered", option: "CSV", note: "JSON later"}});
  row(6).byClass("ask-note")[0].value = "  Friendly  ";
  await row(6).byTag("form")[0].onsubmit({preventDefault() {}});
  assert.deepEqual(posts().at(-1), {path: "asks/6", body: {resolution: "answered", answer: "Friendly"}});
});

test("a failed resolution keeps the row, its input and says what went wrong", async () => {
  let fail = true;
  const {view, row, control, posts, $} = setup([action], {respond: (path, body) => {
    if (fail) throw new Error("Ask 1 is already resolved.");
    return {id: 1, status: "resolved", ...body};
  }});
  await view.refresh();
  const input = row(1).byClass("ask-note")[0], error = row(1).byClass("ask-row-error")[0];
  input.value = "Keep this note";
  assert.equal(error.hidden, true);
  await control(row(1), "Mark done").onclick();
  assert.equal(row(1).byClass("ask-note")[0], input);
  assert.equal(input.value, "Keep this note");
  assert.equal(error.hidden, false);
  assert.equal(error.textContent, "Ask 1 is already resolved. Your input is kept.");
  assert.ok(row(1).byTag("button").every(button => button.disabled === false));
  assert.equal(input.disabled, false);
  assert.equal($("asks-error").hidden, true);
  fail = false;
  await control(row(1), "Mark done").onclick();
  assert.deepEqual(posts().at(-1).body, {resolution: "done", note: "Keep this note"});
  assert.equal(row(1), undefined);
});

test("refreshing preserves an unfinished note and updates the ask's age", async () => {
  const {view, row, state, list, $} = setup([action, decision]);
  await view.refresh();
  const element = row(1), input = element.byClass("ask-note")[0];
  input.value = "Unfinished note";
  state.open = [{...action, age_seconds: 2 * 86400, stale: true, stale_reason: "inactive", idle_seconds: 4 * 86400}, decision];
  await view.refresh();
  assert.equal(row(1), element);
  assert.equal(input.value, "Unfinished note");
  assert.match(element.byClass("ask-meta")[0].text, /asked 2d ago .* Stale · thread idle 4d$/);
  state.open = [state.open[0]];
  await view.refresh();
  assert.equal(row(1), element);
  assert.equal(input.value, "Unfinished note");
  assert.deepEqual(list.byClass("ask-row").length, 1);
  // A failed refresh keeps what is on screen and reports the failure.
  view.api = async () => { throw new Error("Connection lost."); };
  await view.refresh();
  assert.equal(row(1), element);
  assert.equal($("asks-error").textContent, "Connection lost. Showing the last loaded asks.");
  assert.equal($("asks-error").hidden, false);
});

test("snoozing moves an ask to the snoozed section, where it can be shown again", async () => {
  const {view, row, rows, control, posts, $, list} = setup([action, snoozed], {respond: (path, body) =>
    ({id: 1, status: "open", snoozed_until: body.until == null ? null : new Date(body.until * 1000).toISOString()})});
  await view.refresh();
  assert.deepEqual(rows().map(node => node.dataset.id), ["1"]);
  const select = row(1).byTag("select")[0];
  assert.deepEqual(select.children.map(option => option.textContent), ["Snooze…", "In 1 hour", "In 3 hours", "Tomorrow at 9 AM", "Next week"]);
  select.value = "three_hours";
  await select.onchange();
  assert.deepEqual(posts().at(-1), {path: "asks/1/snooze", body: {until: Math.floor(NOW / 1000) + 3 * 3600}});
  assert.equal(select.value, "");
  assert.equal(rows().length, 0);
  assert.deepEqual($("asks-sections").children.map(tab => tab.text), ["Open", "Snoozed 2", "Resolved"]);
  await $("asks-sections").children[1].onclick();
  assert.equal(list.byClass("ask-group-title").length, 0);
  assert.deepEqual(rows().map(node => node.dataset.id), ["1", "7"]);
  assert.match(row(7).byClass("ask-meta")[0].text, /Blocking Snoozed until Nov 23, /);
  assert.equal(row(7).byTag("select").length, 0);
  await control(row(7), "Show now").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/7/snooze", body: {until: null}});
  assert.deepEqual(rows().map(node => node.dataset.id), ["1"]);
  await control(row(1), "Show now").onclick();
  assert.equal(list.byClass("asks-empty")[0].textContent, AsksCopy.empty.snoozed);
  await $("asks-sections").children[0].onclick();
  assert.deepEqual(rows().map(node => node.dataset.id), ["1", "7"]);
});

test("the resolved section is a filterable log of outcomes and notes", async () => {
  const {view, $, rows, calls, list, row} = setup([action]);
  await view.refresh();
  assert.equal($("asks-filters").hidden, true);
  assert.equal(calls.some(call => call.path.includes("status=resolved")), false);
  await $("asks-sections").children[2].onclick();
  assert.equal(calls.at(-1).path, "asks?status=resolved");
  assert.equal($("asks-filters").hidden, false);
  assert.deepEqual(rows().map(node => node.dataset.id), ["11", "12", "13"]);
  assert.equal(row(11).byClass("ask-resolution")[0].textContent, "Granted");
  assert.equal(row(11).byClass("ask-unblocks")[0].text, "Note Read access for 30 days.");
  assert.equal(row(12).byClass("ask-unblocks")[0].text, "Answer 60 minutes");
  assert.equal(row(13).byClass("ask-resolution")[0].className, "ask-resolution ask-resolution-dismissed");
  assert.equal(row(13).byTag("form").length, 0);
  assert.deepEqual($("asks-filter-project").children.map(option => option.textContent), ["All projects", "Crawler", "Shop"]);
  assert.deepEqual($("asks-filter-kind").children.map(option => option.value), Object.keys(AsksCopy.kinds));
  $("asks-filter-project").value = "Shop"; $("asks-filter-project").onchange();
  assert.deepEqual(rows().map(node => node.dataset.id), ["12"]);
  $("asks-filter-kind").value = "request"; $("asks-filter-kind").onchange();
  assert.equal(list.byClass("asks-empty")[0].textContent, AsksCopy.empty.filtered);
  $("asks-filter-project").value = ""; $("asks-filter-kind").value = ""; $("asks-filter-since").value = "2026-11-05";
  $("asks-filter-since").onchange();
  assert.deepEqual(rows().map(node => node.dataset.id), ["11", "12"]);
});

test("a newly resolved ask joins the loaded log, and an empty log says so", async () => {
  const {view, $, rows, row, control, list} = setup([request], {resolved: [],
    respond: (path, body) => ({id: 4, status: "resolved", resolution: body.resolution, note: body.note, answer: null,
      resolved_at: "2026-11-22T16:00:00+00:00"})});
  await view.refresh();
  await $("asks-sections").children[2].onclick();
  assert.equal(list.byClass("asks-empty")[0].textContent, AsksCopy.empty.resolved);
  view.api = (original => async (path, body) => body === undefined && path.includes("status=resolved") ? {data: view.resolved} : original(path, body))(view.api);
  await $("asks-sections").children[0].onclick();
  row(4).byClass("ask-note")[0].value = "Use the team key.";
  await control(row(4), "Grant").onclick();
  await $("asks-sections").children[2].onclick();
  assert.deepEqual(rows().map(node => node.dataset.id), ["4"]);
  assert.equal(row(4).byClass("ask-resolution")[0].textContent, "Granted");
  assert.equal(row(4).byClass("ask-unblocks")[0].text, "Note Use the team key.");
});

test("app links navigate inside the workspace and file paths are copied", async () => {
  const appLink = {...request, id: 21, link: "agentcoord://thread/abc"};
  const path = {...review, id: 22, link: "/work/data.csv"};
  const {view, row, navigated, copied} = setup([appLink, path]);
  await view.refresh();
  let prevented = false;
  const link = row(21).byClass("ask-link")[0];
  link.onclick({preventDefault() { prevented = true; }});
  assert.deepEqual([prevented, navigated, link.target], [true, ["agentcoord://thread/abc"], undefined]);
  const copy = row(22).byClass("ask-link")[0];
  assert.deepEqual([copy.tagName, copy.textContent, copy.title], ["button", "Copy path", "/work/data.csv"]);
  await copy.onclick();
  assert.deepEqual([copied, copy.textContent], [["/work/data.csv"], "Copied"]);
  const standalone = setup([appLink], {embedded: false});
  await standalone.view.refresh();
  const direct = standalone.row(21).byClass("ask-link")[0];
  assert.deepEqual([direct.href, direct.target, direct.onclick], ["agentcoord://thread/abc", "_blank", undefined]);
  assert.equal(standalone.$("close-asks").hidden, true);
});
