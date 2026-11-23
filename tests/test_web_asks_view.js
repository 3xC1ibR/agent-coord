"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {AsksView, AsksCopy, formatAge, formatEffort, snoozeUntil, openGroups, linkTarget, splitQuestion, isLong} = require("../plugins/agent-coord/scripts/agent_coord/web/asks.js");
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

function setup(open, {resolved = resolvedItems, respond, legacy = false, storage, replyRecords = false, threadInfo} = {}) {
  const calls = [], opened = [], navigated = [], copied = [];
  const state = {open, resolved, closed: 0, sent: [], composed: [], composable: true, requests: 0};
  const document = makeDocument(["asks-title", "asks-summary", "close-asks", "asks-sections", "asks-filters",
    "asks-filter-project", "asks-filter-kind", "asks-filter-since", "asks-notice", "asks-error", "asks-status", "asks-list"]);
  const $ = id => document.getElementById(id);
  $("asks-filter-project").append(Object.assign(new Element("option"), {textContent: "All projects"}));
  const api = async (path, body) => {
    calls.push({path, body});
    if (body !== undefined) return respond ? respond(path, body) : {id: Number(path.split("/")[1]), status: "resolved", ...body};
    return {data: path.includes("status=resolved") ? state.resolved : state.open};
  };
  const view = new AsksView({document, api, clock: () => NOW, openThread: id => opened.push(id),
    navigate: url => navigated.push(url), close: () => { state.closed += 1; },
    copyText: async text => { copied.push(text); }, storage, threadInfo,
    messageSent: thread => state.sent.push(thread), requestId: () => "request-" + (state.requests += 1),
    compose: async (thread, text) => { state.composed.push([thread, text]); return state.composable; }});
  view.replyRecords = replyRecords;
  view.setLegacy(legacy);
  view.setVisible(true);
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
    ["blocking", "Blocking", true, [1, 20]], ["overdue", "Overdue", false, [2]],
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
  assert.deepEqual([isLong("Short."), isLong("x".repeat(241)), isLong("a\nb\nc\nd")], [false, true, true]);
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
    assert.equal(document.title, "");
    assert.equal($("asks-sections").getAttribute("aria-label"), "Inbox sections");
  } finally { AsksCopy.viewName = original; }
});

test("the open section shows each ask's kind, gain, effort, thread and state", async () => {
  const {view, $, list, row, control, opened, state} = setup([action, approval, decision, request, review, closed, snoozed]);
  assert.equal($("asks-status").textContent, "Loading…");
  await view.refresh();
  assert.equal($("asks-status").hidden, true);
  assert.equal($("asks-title").textContent, AsksCopy.viewName);
  assert.deepEqual([$("asks-notice").hidden, $("asks-sections").hidden], [true, false]);
  $("close-asks").onclick();
  assert.equal(state.closed, 1);
  const tabs = $("asks-sections").children;
  assert.deepEqual(tabs.map(tab => [tab.text, tab.getAttribute("aria-selected")]), [["Open 6", "true"], ["Snoozed 1", "false"], ["Resolved", "false"]]);
  // Red is spent once: on the blocking group, not on the count or the overdue group.
  assert.deepEqual(list.byClass("ask-group").map(node => node.className), ["ask-group ask-group-urgent", "ask-group", "ask-group", "ask-group"]);
  assert.deepEqual(list.byClass("ask-group-title").map(node => node.text), ["Blocking 1", "Overdue 1", "Crawler 2", "No project 2"]);
  assert.deepEqual(list.byClass("ask-row").map(node => node.dataset.id), ["1", "2", "4", "3", "5", "6"]);
  const first = row(1);
  assert.equal(first.byClass("ask-title")[0].textContent, "Add payout billing details in Apify");
  assert.equal(first.byClass("ask-meta")[0].text, "Action takes about 10 min asked 9h ago Checkout <b> Open console.apify.com");
  assert.equal(first.byClass("ask-unblocks")[0].text, "Unblocks Pricing for all 18 verified actors.");
  const link = first.byClass("ask-link")[0];
  assert.deepEqual([link.tagName, link.href, link.target, link.rel, link.textContent],
    ["a", "https://console.apify.com/billing", "_blank", "noopener noreferrer", "Open console.apify.com"]);
  // Every kind offers a reply in the user's own words, in view, beside its resolutions.
  assert.deepEqual(first.byTag("button").map(button => button.textContent),
    ["Checkout <b>", "Send reply", "Mark done", "Not done", "Reply in conversation", "Dismiss"]);
  for (const id of [1, 2, 3, 4, 5, 6]) {
    const field = row(id).byClass("ask-note")[0];
    assert.deepEqual([field.hidden, field.placeholder, field.parent.className], [false, "Reply in your own words…", "ask-reply"], "ask " + id);
    assert.equal(row(id).byClass("ask-replied")[0].hidden, true);
  }
  assert.equal(row(2).byClass("ask-meta")[0].text, "Approval asked 3d ago Checkout <b> Overdue · due Nov 20");
  assert.equal(row(2).byClass("ask-flag-attention").filter(node => !node.hidden).length, 1);
  assert.deepEqual(row(2).byTag("button").map(button => button.textContent).slice(1), ["Send reply", "Approve", "Decline", "Reply in conversation", "Dismiss"]);
  assert.deepEqual(row(4).byTag("button").map(button => button.textContent).slice(1), ["Send reply", "Grant", "Decline", "Reply in conversation", "Dismiss"]);
  assert.deepEqual(row(5).byTag("button").map(button => button.textContent).slice(1), ["Send reply", "Accept", "Request changes", "Reply in conversation", "Dismiss"]);
  // Every kind has the same arrangement: the field and Send reply, then its resolutions. A decision's are its options and its own text.
  assert.deepEqual(row(3).byTag("button").map(button => button.textContent),
    ["Crawler", "Send reply", "CSV", "JSON", "Answer with my text", "Reply in conversation", "Dismiss"]);
  assert.deepEqual([control(row(3), "Send reply").parent.className, control(row(3), "Answer with my text").parent.className,
    control(row(1), "Mark done").parent.className], ["ask-reply", "ask-entry", "ask-entry"]);
  assert.equal(row(3).byClass("ask-context")[0].textContent, "Default: CSV.");
  // A stale ask says why, and a closed thread is named before the user resolves anything.
  assert.match(row(5).byClass("ask-meta")[0].text, /Stale · thread idle 8d$/);
  assert.match(row(6).byClass("ask-meta")[0].text, /asked just now Checkout <b> Stale · thread closed$/);
  await control(row(3), "Crawler").onclick();
  assert.deepEqual(opened, ["thread-3"]);
});

test("resolutions post the kind's outcome with the note and remove the row", async () => {
  const {view, row, rows, control, posts, $} = setup([action, approval, request, review]);
  await view.refresh();
  // Text in the reply field goes with a chosen resolution as its note.
  const noteField = row(1).byClass("ask-note")[0];
  assert.equal(control(row(1), "Add note"), undefined);
  noteField.value = "  Card ending 4242.  ";
  await control(row(1), "Mark done").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/1", body: {resolution: "done", note: "Card ending 4242."}});
  assert.equal(row(1), undefined);
  await control(row(2), "Decline").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/2", body: {resolution: "declined"}});
  // Enter in an empty field does nothing; it never chooses a resolution.
  let prevented = false;
  assert.equal(row(4).byTag("form")[0].onsubmit({preventDefault() { prevented = true; }}), undefined);
  assert.equal(prevented, true);
  assert.equal(posts().length, 2);
  row(4).byClass("ask-note")[0].value = "The key is in 1Password.";
  await control(row(4), "Grant").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/4", body: {resolution: "granted", note: "The key is in 1Password."}});
  await control(row(5), "Dismiss").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/5", body: {resolution: "dismissed"}});
  assert.equal(rows().length, 0);
  assert.equal($("asks-list").byClass("asks-empty")[0].textContent, AsksCopy.empty.open);
  assert.equal($("asks-sections").children[0].text, "Open");
});

test("a decision is answered by an option or by Answer with my text", async () => {
  const {view, row, control, posts} = setup([decision, closed]);
  await view.refresh();
  const input = row(3).byClass("ask-note")[0];
  // Answer needs text: without it the field takes focus and nothing is sent.
  await control(row(3), "Answer with my text").onclick();
  assert.equal(posts().length, 0);
  assert.equal(input.focused, true);
  input.value = "JSON later";
  await control(row(3), "CSV").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/3", body: {resolution: "answered", option: "CSV", note: "JSON later"}});
  row(6).byClass("ask-note")[0].value = "  Friendly  ";
  await control(row(6), "Answer with my text").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/6", body: {resolution: "answered", answer: "Friendly"}});
  assert.equal(row(6), undefined);
});

test("a reply in the user's own words goes to the asking conversation and leaves the ask open", async () => {
  const {view, row, rows, control, posts, state} = setup([action, decision]);
  await view.refresh();
  const field = row(1).byClass("ask-note")[0], form = row(1).byTag("form")[0];
  // Nothing typed: Enter and Reply send nothing.
  await form.onsubmit({preventDefault() {}});
  assert.equal(posts().length, 0);
  // Once something is typed, a line says what each control does with it.
  const hint = row(1).byClass("ask-hint")[0];
  assert.equal(hint.hidden, true);
  field.value = "  what do you need for dataforseo?  ";
  field.oninput();
  assert.deepEqual([hint.hidden, hint.textContent], [false, AsksCopy.reply.hint]);
  let prevented = false;
  await form.onsubmit({preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  // An ordinary message to the asking conversation that names the ask and says it is still open.
  assert.deepEqual(posts(), [{path: "sessions/thread-1/messages", body: {
    message: 'Re ask #1 "Add payout billing details in Apify" (still open): what do you need for dataforseo?', requestId: "request-1"}}]);
  assert.equal(rows().length, 2, "the ask stays open");
  assert.equal(row(1).byClass("ask-note")[0], field, "its row is kept");
  assert.equal(field.value, "");
  assert.equal(hint.hidden, true);
  assert.ok(row(1).byTag("button").every(button => button.disabled === false));
  const mark = row(1).byClass("ask-replied")[0];
  assert.equal(mark.hidden, false);
  assert.equal(mark.text, "You replied just now what do you need for dataforseo?");
  assert.deepEqual(state.sent, ["thread-1"], "the open conversation is told a message went in");
  assert.equal(row(3).byClass("ask-replied")[0].hidden, true);
  // A later reply replaces the mark; a resolution afterwards still carries typed text as the note.
  field.value = "and the login?";
  assert.equal(control(row(1), "Send reply").type, "submit", "Send reply submits the form, as Enter does");
  await form.onsubmit({preventDefault() {}});
  assert.equal(posts().at(-1).body.message, 'Re ask #1 "Add payout billing details in Apify" (still open): and the login?');
  assert.equal(posts().at(-1).body.requestId, "request-2");
  assert.match(mark.text, /and the login\?$/);
  field.value = "Card saved.";
  await control(row(1), "Mark done").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/1", body: {resolution: "done", note: "Card saved."}});
});

test("a reply keeps its text on failure, is not sent twice on a retry, and names a long ask briefly", async () => {
  let fail = true;
  const long = {...request, id: 8, question: "Verify the DataForSEO account ".repeat(8).trim() + "\nThen tell me the plan."};
  const {view, row, posts} = setup([long], {respond: path => {
    if (path.startsWith("sessions/") && fail) throw new Error("Could not confirm whether the message was accepted");
    return {turnId: "turn-1"};
  }});
  await view.refresh();
  const field = row(8).byClass("ask-note")[0], form = row(8).byTag("form")[0], error = row(8).byClass("ask-row-error")[0];
  field.value = "which account?";
  await form.onsubmit({preventDefault() {}});
  assert.equal(error.hidden, false);
  assert.equal(error.textContent, "Could not confirm whether the message was accepted. Your input is kept.");
  assert.equal(field.value, "which account?");
  assert.equal(row(8).byClass("ask-replied")[0].hidden, true);
  fail = false;
  await form.onsubmit({preventDefault() {}});
  // The same text retried reuses its request ID, so an accepted first attempt is not repeated.
  assert.deepEqual(posts().map(call => call.body.requestId), ["request-1", "request-1"]);
  const reference = posts()[0].body.message.split(" (still open): ")[0];
  assert.equal(reference.length, 'Re ask #8 ""'.length + 120);
  assert.match(reference, /…"$/);
  assert.equal(error.hidden, true);
  assert.equal(row(8).byClass("ask-replied")[0].hidden, false);
});

test("a thread outside Ribbon Field cannot take a reply, and says what to do instead", async () => {
  const {view, row, posts} = setup([action], {threadInfo: id => ({thread_id: id, browser_session: false})});
  await view.refresh();
  const field = row(1).byClass("ask-note")[0];
  field.value = "what do you need?";
  await row(1).byTag("form")[0].onsubmit({preventDefault() {}});
  assert.equal(posts().length, 0);
  assert.equal(row(1).byClass("ask-row-error")[0].textContent, AsksCopy.reply.external + " Your input is kept.");
  assert.equal(field.value, "what do you need?");
});

test("the replied mark comes from the backend where it records replies, else from this browser", async () => {
  const stored = {};
  const storage = {getItem: key => stored[key] ?? null, setItem: (key, value) => { stored[key] = value; }};
  const first = setup([action, decision], {storage});
  await first.view.refresh();
  first.row(1).byClass("ask-note")[0].value = "which card?";
  await first.row(1).byTag("form")[0].onsubmit({preventDefault() {}});
  assert.deepEqual(first.posts().map(call => call.path), ["sessions/thread-1/messages"], "an older backend has no reply record");
  assert.deepEqual(Object.keys(JSON.parse(stored["agent-coord.ask-replies"])), ["1"]);
  // The page reloads: the mark is read back from this browser.
  const second = setup([action, decision], {storage});
  await second.view.refresh();
  assert.equal(second.row(1).byClass("ask-replied")[0].text, "You replied just now which card?");
  // Once the ask is no longer open its mark is forgotten.
  second.state.open = [decision];
  await second.view.refresh();
  assert.equal(stored["agent-coord.ask-replies"], "{}");

  // A backend that records replies is asked to keep the mark, and its record is what every device shows.
  const recorded = setup([action], {storage: {getItem: () => null, setItem() {}}, replyRecords: true,
    respond: (path, body) => path === "asks/1/reply"
      ? {id: 1, reply_text: body.text, replied_at: "2026-11-22T09:15:00+00:00"} : {turnId: "turn-1"}});
  await recorded.view.refresh();
  recorded.row(1).byClass("ask-note")[0].value = "which card?";
  await recorded.row(1).byTag("form")[0].onsubmit({preventDefault() {}});
  assert.deepEqual(recorded.posts().map(call => [call.path, call.body.text]),
    [["sessions/thread-1/messages", undefined], ["asks/1/reply", "which card?"]]);
  assert.equal(recorded.view.open[0].replied_at, "2026-11-22T09:15:00+00:00");
  const other = setup([{...action, reply_text: "which card?", replied_at: new Date(NOW - 6 * 3600000).toISOString()}]);
  await other.view.refresh();
  assert.equal(other.row(1).byClass("ask-replied")[0].text, "You replied 6h ago which card?");
  // The record failing does not undo a reply that reached the agent.
  const unrecorded = setup([action], {replyRecords: true,
    respond: path => { if (path === "asks/1/reply") throw new Error("Unknown route"); return {turnId: "turn-1"}; }});
  await unrecorded.view.refresh();
  unrecorded.row(1).byClass("ask-note")[0].value = "which card?";
  await unrecorded.row(1).byTag("form")[0].onsubmit({preventDefault() {}});
  assert.equal(unrecorded.row(1).byClass("ask-row-error")[0].hidden, true);
  assert.equal(unrecorded.row(1).byClass("ask-replied")[0].hidden, false);
});

test("Reply in conversation hands the ask and any typed text to the conversation's draft", async () => {
  const {view, row, control, state, posts, opened} = setup([action]);
  await view.refresh();
  const field = row(1).byClass("ask-note")[0];
  await control(row(1), "Reply in conversation").onclick();
  assert.deepEqual(state.composed, [["thread-1", 'Re ask #1 "Add payout billing details in Apify" (still open): ']]);
  field.value = "  which card?  ";
  await control(row(1), "Reply in conversation").onclick();
  assert.deepEqual(state.composed.at(-1), ["thread-1", 'Re ask #1 "Add payout billing details in Apify" (still open): which card?']);
  assert.equal(field.value, "", "the text moved to the draft");
  // A conversation whose draft cannot take text is still opened; the typed text stays with the ask.
  state.composable = false;
  field.value = "keep me";
  await control(row(1), "Reply in conversation").onclick();
  assert.equal(field.value, "keep me");
  assert.equal(posts().length, 0);
  assert.deepEqual(opened, []);
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
  assert.match(element.byClass("ask-meta")[0].text, /asked 2d ago .* Stale · thread idle 4d Open console\.apify\.com$/);
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
  assert.equal(row(13).byClass("ask-resolution")[0].textContent, "Dismissed");
  assert.match(row(11).byClass("ask-meta")[0].text, /^Request resolved Nov 21, /);
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
});

test("a hidden view keeps its data current without building rows", async () => {
  const {view, rows, calls} = setup([action, decision]);
  view.setVisible(false);
  await view.refresh();
  assert.equal(rows().length, 0);
  assert.deepEqual(view.counts(), {open: 2, snoozed: 0, blocking: 1});
  view.section = "resolved";
  await view.refresh();
  assert.equal(calls.some(call => call.path.includes("status=resolved")), false);
  view.section = "open";
  view.setVisible(true);
  assert.equal(rows().length, 2);
});

test("an older backend gets decisions only, answered with the bodies it accepts", async () => {
  // Before its update is applied, the backend lists asks without kinds and rejects resolutions.
  const legacyBlocking = {id: 40, session_id: "thread-40", thread_title: "Apify", project_name: "projects", question: "Which account?",
    context: null, options: ["Staging", "Production"], blocking: true, status: "open", age_seconds: 4000};
  const legacyQuiet = {id: 41, session_id: "thread-41", thread_title: "Docs", project_name: null, question: "Tone?",
    context: "Default: formal.", options: [], blocking: false, status: "open", age_seconds: 30};
  const {view, $, row, rows, control, posts, list} = setup([legacyBlocking, legacyQuiet], {legacy: true,
    respond: (path, body) => ({id: Number(path.split("/")[1]), status: "answered", ...body})});
  await view.refresh();
  assert.deepEqual([$("asks-notice").hidden, $("asks-notice").textContent, $("asks-sections").hidden],
    [false, AsksCopy.legacyNotice, true]);
  assert.deepEqual(list.byClass("ask-group-title").map(node => node.text), ["Blocking 1", "No project 1"]);
  assert.equal(row(40).byClass("ask-meta")[0].text, "Decision asked 1h ago Apify");
  assert.deepEqual(row(40).byTag("button").map(button => button.textContent),
    ["Apify", "Send reply", "Staging", "Production", "Answer with my text", "Reply in conversation"]);
  assert.equal(row(40).byTag("select").length, 0);
  row(40).byClass("ask-note")[0].value = "typed first";
  await control(row(40), "Production").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/40", body: {option: "Production"}});
  row(41).byClass("ask-note")[0].value = " Friendly ";
  await control(row(41), "Answer with my text").onclick();
  assert.deepEqual(posts().at(-1), {path: "asks/41", body: {answer: "Friendly"}});
  assert.equal(rows().length, 0);
  // Once the backend is updated the full view returns.
  view.setLegacy(false);
  assert.deepEqual([$("asks-notice").hidden, $("asks-sections").hidden], [true, false]);
});
