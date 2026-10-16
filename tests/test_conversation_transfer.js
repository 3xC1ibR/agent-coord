"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const {ThreadTabs} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-tabs.js");
const {paneLayout} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-panes.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
function fixture() {
  const input = {value: "unsent follow-up", selectionStart: 2, selectionEnd: 7,
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }, focus() {}};
  const state = {selected: "a", projectCwd: "/work", drafts: new Map(), conversationPositions: new Map(),
    attachments: {drafts: new Map([["a", [{name: "image", url: "data:image/png;base64,YQ=="}]]]),
      items(id) { return this.drafts.get(id) || []; }, pending() { return false; }},
    mentions: {drafts: new Map(), snapshot: () => [{threadId: "other"}], restore(id, text, mentions) { this.drafts.set(id, mentions); }},
    timelineScroll: {following: false, position: () => 318, restore(top) { this.restored = top; }, latest() { this.following = true; }},
    detail: {work_thread: {thread_id: "a"}, running: true, requests: [{id: "approval"}]}};
  const visits = [];
  const select = async id => {
    if (state.selected) state.drafts.set(state.selected, input.value);
    state.selected = id; input.value = state.drafts.get(id) || "";
    state.detail.work_thread.thread_id = id; state.threadTabs.open({thread_id: id}); visits.push(id);
  };
  state.threadTabs = new ThreadTabs({onSelect: async id => { if (id) await select(id); else { state.selected = null; state.threadTabs.overview(); visits.push(null); } }});
  state.threadTabs.setProject("/work"); state.threadTabs.open({thread_id: "a", title: "A"});
  const document = {getElementById: () => input, querySelector: () => null};
  const window = {agentCoordPanes: {active: false, records: new Map(), layouts: new Map()}};
  const storage = new Map([["timeline-disclosures:a", '["expanded-tool"]']]);
  const c = vm.createContext({window, requestAnimationFrame: fn => fn(), paneLayout,
    sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)}});
  vm.runInContext(source.slice(source.indexOf("function setupConversationTransfer("), source.indexOf("function setupComposerSizing(")), c);
  const transfer = c.setupConversationTransfer({state, document, select, goHome() {}, renderStatus() {}});
  return {state, input, window, transfer, visits, storage};
}
test("capture keeps live session and approval ownership while preserving draft, attachment, mention, selection and reading position", () => {
  const f = fixture(), original = f.state.detail, payload = f.transfer.capture("a");
  assert.equal(payload.draft.text, "unsent follow-up");
  assert.equal(payload.draft.scroll, 318); assert.equal(payload.draft.following, false);
  assert.equal(payload.draft.selectionStart, 2); assert.equal(payload.draft.selectionEnd, 7);
  assert.equal(payload.draft.images[0].name, "image"); assert.equal(payload.draft.mentions[0].threadId, "other");
  assert.equal(payload.draft.disclosures[0], "expanded-tool");
  assert.equal(f.state.detail, original); assert.equal(original.running, true); assert.equal(original.requests[0].id, "approval");
  assert.equal(f.state.threadTabs.items.length, 1, "source remains until destination confirms restoration");
});
test("restore opens the same thread and restores unsent input and scroll without creating a provider session", async () => {
  const source = fixture(), payload = source.transfer.capture("a"), target = fixture();
  target.input.value = ""; target.state.attachments.drafts.clear();
  target.state.threadTabs.items.splice(0); target.state.selected = null;
  await target.transfer.restore(JSON.parse(JSON.stringify(payload)));
  assert.deepEqual(target.visits, ["a"]); assert.equal(target.input.value, payload.draft.text);
  assert.equal(target.input.selectionStart, 2); assert.equal(target.state.timelineScroll.restored, 318);
  assert.equal(target.state.attachments.items("a")[0].name, "image");
  assert.equal(target.storage.get("timeline-disclosures:a"), '["expanded-tool"]');
});
test("an unrelated in-flight send cannot leave a duplicate after the destination was restored", async () => {
  const source = fixture(), payload = source.transfer.capture("a");
  source.state.busy = true; source.state.sending = new Set(["another-tab"]);
  await source.transfer.release("a", payload.sourceCwd, payload);
  assert.equal(source.state.threadTabs.items.length, 0);
});
test("closed-tab drafts are protected, and incoming staged tabs cannot be detached before commit", async () => {
  const source = fixture(), payload = source.transfer.capture("a"), target = fixture();
  target.state.drafts.set("a", "Closed target draft"); target.input.value = "Closed target draft";
  await target.state.threadTabs.close("a");
  await assert.rejects(target.transfer.stage(payload, "conflict"), /different draft/);
  assert.equal(target.state.drafts.get("a"), "Closed target draft");
  assert.equal(target.state.threadTabs.items.length, 0);
  target.state.drafts.clear(); target.state.attachments.drafts.clear(); target.state.mentions.drafts.clear();
  await target.transfer.stage(payload, "move");
  assert.equal(target.transfer.capture("a"), null);
  assert.equal(target.transfer.capture("a", {validate: false}).draft.text, payload.draft.text);
  await target.transfer.finish("move", true);
  assert.equal(target.transfer.capture("a").draft.text, payload.draft.text);
});
test("source removal retains its recoverable draft until commit acknowledgement", async () => {
  const f = fixture(), payload = f.transfer.capture("a");
  await f.transfer.release("a", "/work", payload, {retainDraft: true});
  assert.equal(f.state.threadTabs.items.length, 0);
  assert.equal(f.state.drafts.get("a"), payload.draft.text);
  await f.transfer.restore(f.transfer.capture("a", {validate: false}), {rollback: true});
  assert.equal(f.state.threadTabs.items.length, 1); assert.equal(f.input.value, payload.draft.text);
});
test("failed commit reopens the final auxiliary tab even when close was handled without navigation", async () => {
  const f = fixture(), payload = f.transfer.capture("a");
  f.state.threadTabs.onClose = async () => true;
  await f.transfer.release("a", "/work", payload, {retainDraft: true});
  assert.equal(f.state.selected, "a"); assert.equal(f.transfer.empty(), true);
  await f.transfer.restore(f.transfer.capture("a", {validate: false}), {rollback: true});
  assert.equal(f.transfer.empty(), false); assert.equal(f.state.threadTabs.items[0].thread_id, "a");
  assert.equal(await f.transfer.finish("lost-on-reload", true), false);
});
test("failed source commit rolls back a staged destination and restores its original tab and draft", async () => {
  const source = fixture(), payload = source.transfer.capture("a"), target = fixture();
  target.state.threadTabs.items.splice(0); target.state.attachments.drafts.clear();
  target.state.threadTabs.open({thread_id: "b"}); target.state.selected = "b"; target.input.value = "Other window draft";
  await target.transfer.stage(payload, "move");
  assert.deepEqual(target.state.threadTabs.items.map(item => item.thread_id), ["b", "a"]);
  assert.equal(target.state.transferLocks.has("a"), true);
  source.input.value = "Changed source";
  await assert.rejects(source.transfer.release("a", "/work", payload), /draft changed/);
  await target.transfer.finish("move", false);
  assert.deepEqual(target.state.threadTabs.items.map(item => item.thread_id), ["b"]);
  assert.equal(target.state.selected, "b"); assert.equal(target.input.value, "Other window draft");
  assert.equal(source.state.threadTabs.items[0].thread_id, "a");
});
test("repeated transfer commits leave one owner and a conflicting target draft is preserved", async () => {
  const first = fixture(), second = fixture();
  second.state.threadTabs.items.splice(0); second.state.attachments.drafts.clear(); second.state.selected = null;
  const one = first.transfer.capture("a");
  await second.transfer.stage(one, "first"); await first.transfer.release("a", "/work", one); await second.transfer.finish("first", true);
  assert.equal(first.state.threadTabs.items.length, 0);
  const third = fixture(); third.state.threadTabs.items.splice(0); third.state.attachments.drafts.clear(); third.state.selected = null;
  const two = second.transfer.capture("a");
  await third.transfer.stage(two, "second"); await second.transfer.release("a", "/work", two); await third.transfer.finish("second", true);
  assert.equal(second.state.threadTabs.items.length, 0); assert.equal(third.state.threadTabs.items.length, 1);
  const conflict = fixture(); conflict.input.value = "Different target draft";
  await assert.rejects(conflict.transfer.stage(two, "conflict"), /different draft/);
  assert.equal(conflict.input.value, "Different target draft");
  assert.equal(conflict.state.threadTabs.items.length, 1);
});
test("background tabs retain saved draft and reading position independently of the current tab", () => {
  const f = fixture(); f.state.threadTabs.open({thread_id: "b"});
  f.state.drafts.set("b", "background draft"); f.state.conversationPositions.set("b", {scroll: 42, following: false});
  const payload = f.transfer.capture("b");
  assert.equal(payload.draft.text, "background draft"); assert.equal(payload.draft.scroll, 42);
});
test("a restored background tab without a reading position follows latest, and cross-project tiles open in their own project", () => {
  const f = fixture(); f.state.threadTabs.open({thread_id: "b", cwd: "/other"});
  const payload = f.transfer.capture("b");
  assert.equal(payload.draft.following, true);
  assert.equal(payload.cwd, "/other"); assert.equal(payload.sourceCwd, "/work");
});
test("pending sends, incomplete images, modal dialogs and temporary drafts cannot transfer", () => {
  const f = fixture();
  f.state.busy = true; assert.equal(f.transfer.capture("a"), null); f.state.busy = false;
  f.state.attachments.drafts.get("a")[0].url = null; assert.equal(f.transfer.capture("a"), null);
  assert.equal(f.transfer.capture("new-session"), null);
});
test("release navigates adjacent tabs, and the final tab returns to Overview without changing backend activity", async () => {
  const f = fixture(); f.state.threadTabs.open({thread_id: "b"}); f.state.threadTabs.open({thread_id: "a"});
  await f.transfer.release("a");
  assert.equal(f.state.selected, "b"); assert.equal(f.state.detail.running, true);
  assert.equal(f.state.drafts.has("a"), false);
  await f.transfer.release("b"); assert.equal(f.state.selected, null); assert.deepEqual(f.visits, ["b", null]);
});
test("a project switch while a window loads removes only the transferred project's tab", async () => {
  const f = fixture(); f.state.threadTabs.setProject("/other"); f.state.threadTabs.open({thread_id: "other"});
  await f.transfer.release("a", "/work");
  assert.deepEqual(f.state.threadTabs.items.map(item => item.thread_id), ["other"]);
  assert.equal(f.state.threadTabs.projects.get("/work").length, 0);
});
test("source transfer lock follows only its conversation and release rejects newer draft text or images", async () => {
  const f = fixture(), payload = f.transfer.capture("a");
  f.transfer.lock("a", true); assert.equal(f.state.transferLocks.has("a"), true);
  f.state.threadTabs.open({thread_id: "b"}); assert.equal(f.state.transferLocks.has("b"), false);
  f.input.value = "Edited while opening";
  f.state.attachments.drafts.get("a").push({name: "later", url: "data:image/png;base64,Yg=="});
  await assert.rejects(f.transfer.release("a", "/work", payload), /draft changed/);
  assert.equal(f.state.threadTabs.items.some(item => item.thread_id === "a"), true);
  assert.equal(f.state.attachments.items("a").length, 2);
  f.transfer.lock("a", false); assert.equal(f.state.transferLocks.has("a"), false);
});
test("tiled tabs export their own live pane draft and release only that pane", async () => {
  const f = fixture(), panes = f.window.agentCoordPanes;
  panes.active = true; panes.viewId = "view";
  panes.records.set("a", {thread: {thread_id: "a"}, frame: {contentDocument: {getElementById: () => f.input, querySelector: () => null}, remove() {}},
    api: {canTransfer: () => true, exportDraft: () => ({text: "pane draft", scroll: 99})}});
  panes.layouts.set("view", {groups: [["a", "b"]], tabs: ["a"], selected: "a", order: ["a", "b"], columns: [1]});
  panes.render = () => {}; panes.focus = id => assert.equal(id, "b");
  assert.equal(f.transfer.capture("a").draft.text, "pane draft");
  await f.transfer.release("a");
  assert.deepEqual(panes.layouts.get("view").groups, [["b"]]); assert.equal(panes.records.has("a"), false);
});
