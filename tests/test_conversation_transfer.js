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
  const select = async id => { state.selected = id; state.detail.work_thread.thread_id = id; state.threadTabs.open({thread_id: id}); visits.push(id); };
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
  target.input.value = "";
  await target.transfer.restore(JSON.parse(JSON.stringify(payload)));
  assert.deepEqual(target.visits, ["a"]); assert.equal(target.input.value, payload.draft.text);
  assert.equal(target.input.selectionStart, 2); assert.equal(target.state.timelineScroll.restored, 318);
  assert.equal(target.state.attachments.items("a")[0].name, "image");
  assert.equal(target.storage.get("timeline-disclosures:a"), '["expanded-tool"]');
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
