"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {Model, Workspace, describe} = require("../plugins/agent-coord/scripts/agent_coord/web/dispatcher-workspace.js");
require("../plugins/agent-coord/scripts/agent_coord/web/dispatch.js");

const thread = (id, values = {}) => ({thread_id: id, title: "Work " + id, attention: "now",
  project_cwd: "/work", response_state: "working", status: "running", ...values});
const handoff = (id, target, values = {}) => ({key: "request:" + id, target,
  state: "accepted", createdAt: id * 100, ...values});
const completed = (id, values = {}) => thread(id, {response_state: "update", status: "idle",
  turn_completion: {id: 20, status: "completed", completed_at: 2000}, unread_result: true,
  response_classification: {status: "classified", choice: "findings"},
  checkpoint: {summary: "The investigation found a slow dependency.", next_actor: "nobody", next_action: ""},
  ...values});

function workspaceLifecycle({width = 1080, height = 600} = {}) {
  const callbacks = {}, elements = new Map();
  const document = {
    activeElement: null, querySelector: () => null,
    addEventListener: (name, callback) => { callbacks[name] = callback; },
    getElementById: id => {
      if (!elements.has(id)) elements.set(id, {hidden: true,
        classList: {toggle() {}}, setAttribute() {}, focus() { document.activeElement = this; },
        contains(element) { return this === element; },
      });
      return elements.get(id);
    },
    body: {classList: {add() {}, remove() {}, toggle() {}}},
  };
  const storage = new Map(), measurements = [], focused = [], media = {matches: width <= 800};
  const panes = {records: new Map(), layouts: new Map(), active: false,
    container: {hidden: true, getBoundingClientRect() {
      measurements.push(this.hidden);
      return {width: this.hidden ? 0 : width, height: this.hidden ? 0 : height};
    }},
    get layout() { return this.layouts.get(this.viewId); },
    tile(id, threads) {
      this.active = true; this.viewId = id;
      this.layouts.set(id, {order: threads.map(item => item.thread_id)});
      for (const item of threads) if (!this.records.has(item.thread_id)) this.records.set(item.thread_id, {thread: item});
    },
    getDraft: () => ({text: "", images: []}), render() {}, changed() {}, onFocus() {},
    focus: id => focused.push(id),
  };
  const workspace = new Workspace({document, panes,
    view: {matchMedia: () => media, addEventListener: (name, callback) => { callbacks[name] = callback; }, setTimeout: callback => callback()},
    storage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    fetchThread: async id => thread(id), onError: error => { throw error; }, openDispatcher() {},
  });
  workspace.render = items => { workspace.rendered = items; };
  return {workspace, callbacks, panes, storage, measurements, focused, media};
}

test("mobile keeps routed work in the list until opened and shows only one conversation", async () => {
  const {workspace, panes} = workspaceLifecycle({width: 768, height: 1024});
  workspace.show("dispatcher", "/work");
  const inventory = [thread("a"), thread("b")];
  const receipts = inventory.map((item, i) => ({id: i + 1, dispatcher_id: "dispatcher",
    target_id: item.thread_id, state: "accepted", created_at: 100}));
  workspace.update(inventory, {work_thread: {thread_id: "dispatcher"}, dispatchRequests: receipts});
  assert.equal(workspace.capacity, 1);
  assert.deepEqual(workspace.model.visible, [], "New work must not take over the mobile screen");
  assert.equal(workspace.model.items.length, 2);
  await workspace.open("a");
  assert.deepEqual(panes.layout.order, ["a"]);
  await workspace.open("b");
  assert.deepEqual(panes.layout.order, ["b"]);
  assert.equal(workspace.model.items.length, 2, "Other work remains available on return");
  workspace.update([completed("a"), completed("b")]);
  assert.deepEqual(workspace.model.visible, ["b"], "Completion must not dismiss the open conversation");
});

function renderedWorkspace() {
  const fixture = workspaceLifecycle({width: 390, height: 844}), {workspace} = fixture;
  function element(tag = "div") {
    return {tag, children: [], dataset: {}, attributes: {}, className: "", disabled: false,
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      setAttribute(key, value) { this.attributes[key] = value; }};
  }
  workspace.document.createElement = element;
  const lists = {};
  for (const id of ["dispatcher-attention", "dispatcher-running", "dispatcher-results"]) {
    const list = lists[id] = element(), count = element("small");
    workspace.$(id).querySelector = selector => selector === ".dispatch-items" ? list : count;
  }
  workspace.render = Workspace.prototype.render;
  workspace.show("dispatcher", "/work");
  return {...fixture, lists};
}

test("dispatcher result Dismiss handles the displayed response without opening or removing its conversation", async () => {
  const {workspace, lists, panes} = renderedWorkspace();
  const item = completed("a", {can_handle_response: true, checkpoint: {id: 12}, turn_completion: {id: 8}});
  workspace.model.ids.add("a");
  let resolve, calls = 0;
  workspace.dismissThread = async displayed => {
    calls++; assert.equal(displayed, item);
    await new Promise(done => { resolve = done; });
    workspace.update([{...item, can_handle_response: false, unhandled_response: false}]);
  };
  workspace.update([item]);
  const card = () => lists["dispatcher-results"].children[0];
  const dismiss = () => card().children.find(child => child.dataset.dispatchAction === "dismiss");
  assert.equal(dismiss().textContent, "Dismiss");
  assert.equal(dismiss().attributes["aria-label"], "Dismiss · Work a");
  assert.equal(card().tag, "div", "The two buttons are siblings, never nested");
  const saving = dismiss().onclick();
  assert.equal(dismiss().disabled, true);
  await dismiss().onclick();
  assert.equal(calls, 1);
  resolve(); await saving;
  assert.equal(dismiss(), undefined);
  assert.equal(card().children[0].tag, "button", "Dismissed results remain available");
  assert.deepEqual(workspace.model.visible, []);
  assert.deepEqual(panes.layout.order, []);
  assert.equal(workspace.threads[0].attention, "now");
});

test("dispatcher dismissal survives failure and cannot acknowledge required input or a newer response", async () => {
  const {workspace, lists} = renderedWorkspace();
  const item = completed("a", {can_handle_response: true, turn_completion: {id: 8}});
  workspace.model.ids.add("a"); workspace.model.ids.add("blocked");
  const blocked = thread("blocked", {response_state: "input", can_handle_response: false});
  const errors = []; workspace.onError = error => errors.push(error.message);
  workspace.dismissThread = async () => { throw new Error("Offline"); };
  workspace.update([item, blocked]);
  const dismiss = () => lists["dispatcher-results"].children[0].children.find(child => child.dataset.dispatchAction === "dismiss");
  assert.equal(lists["dispatcher-attention"].children[0].children.length, 1);
  await workspace.dismiss(blocked);
  assert.deepEqual(errors, []);
  await dismiss().onclick();
  assert.deepEqual(errors, ["Offline"]);
  assert.equal(dismiss().disabled, false, "A failed request can be retried");
  workspace.dismissThread = async displayed => {
    assert.equal(displayed.turn_completion.id, 8);
    workspace.update([{...item, turn_completion: {id: 9}}, blocked]);
  };
  await dismiss().onclick();
  assert.equal(dismiss().disabled, false, "The new notification still offers Dismiss");
  assert.equal(workspace.threads[0].turn_completion.id, 9);
});

test("dismissed blockers leave dispatcher attention while retaining their explanation", async () => {
  for (const response_state of ["action", "input"]) {
    const {workspace, lists} = renderedWorkspace();
    const item = completed("blocked", {response_state, attention_reason: "blocked",
      needs_attention: true, unhandled_response: true, can_handle_response: true,
      response_classification: {status: "classified", choice: "blocked"},
      checkpoint: {id: 12, next_actor: "user", next_action: "Sign in to deploy.", summary: "Deployment is paused."}});
    workspace.model.ids.add(item.thread_id);
    workspace.dismissThread = async displayed => {
      assert.equal(displayed, item);
      workspace.update([{...item, needs_attention: false, unhandled_response: false, can_handle_response: false}]);
    };
    workspace.update([item]);
    const card = lists["dispatcher-attention"].children[0];
    await card.children.find(child => child.dataset.dispatchAction === "dismiss").onclick();
    assert.equal(lists["dispatcher-attention"].children.length, 0);
    assert.equal(lists["dispatcher-results"].children.length, 1);
    const saved = describe(workspace.threads[0]);
    assert.equal(saved.kind, "results");
    assert.equal(saved.label, "Blocked");
    assert.equal(saved.summary, "Sign in to deploy.");
    assert.equal(saved.thread.attention, "now");
    workspace.update([{...item, checkpoint: {...item.checkpoint, id: 13}}]);
    assert.equal(lists["dispatcher-attention"].children.length, 1);
  }
});

test("resizing to mobile retains the selected conversation even when geometry capacity is unchanged", async () => {
  const {workspace, panes, media, callbacks} = workspaceLifecycle({width: 800, height: 700});
  media.matches = false;
  workspace.show("dispatcher", "/work");
  workspace.update([thread("a"), thread("b")]);
  await workspace.open("a"); await workspace.open("b");
  panes.selected = "b";
  media.matches = true; callbacks.resize();
  assert.deepEqual(workspace.model.visible, ["b"]);
  assert.deepEqual(panes.layout.order, ["b"]);
  media.matches = false; callbacks.resize();
  assert.equal(workspace.capacity, 4);
  await workspace.open("a");
  assert.deepEqual(workspace.model.visible, ["b", "a"]);
});

test("workspace inventory updates are safe before opt-in with absent or unrelated detail", () => {
  const {workspace} = workspaceLifecycle();
  const inventory = [thread("a")];
  for (const detail of [undefined, null, {}, {work_thread: {}}, {work_thread: {thread_id: "unrelated"}}]) {
    assert.doesNotThrow(() => workspace.update(inventory, detail));
    assert.equal(workspace.threads, inventory);
    assert.deepEqual(workspace.entries, []);
    assert.equal(workspace.active, false);
    assert.equal(workspace.model, undefined, "Overview loading must not initialize dispatcher view state");
  }
  workspace.update(null, null);
  assert.equal(workspace.threads, inventory, "A detail-only refresh preserves the inventory");
});

test("resize, focus, hide, and sync are harmless before the workspace is opened", () => {
  const {workspace, callbacks, panes} = workspaceLifecycle();
  assert.doesNotThrow(() => {
    callbacks.resize(); callbacks.focusin(); panes.onResize(); workspace.sync(); workspace.hide();
  });
  assert.equal(workspace.active, false);
  assert.equal(workspace.model, undefined);
});

test("workspace consumes only its selected dispatcher's detail and preserves receipts during inventory refresh", () => {
  const {workspace} = workspaceLifecycle();
  let syncs = 0;
  workspace.sync = () => { syncs++; };
  workspace.show("dispatcher", "/work");
  const receipt = {id: 7, dispatcher_id: "dispatcher", target_id: "a", target_title: "Work a",
    state: "queued", created_at: 100, body: {message: "Do the work"}};
  const inventory = [thread("a")];
  workspace.update(inventory, {work_thread: {thread_id: "dispatcher"}, dispatchRequests: [receipt]});
  assert.equal(workspace.entries.length, 1);
  assert.equal(workspace.entries[0].target, "a");
  assert.equal(workspace.entries[0].state, "queued");
  const accepted = workspace.entries;
  workspace.update(null, null);
  workspace.update(undefined, {work_thread: {thread_id: "worker"}, dispatchRequests: []});
  assert.equal(workspace.entries, accepted);
  assert.equal(workspace.threads, inventory);
  assert.equal(syncs, 4, "Opening and each refresh request synchronize the view");
  workspace.update(undefined, {work_thread: {thread_id: "dispatcher"}, dispatchRequests: []});
  assert.deepEqual(workspace.entries, [], "An authoritative empty dispatcher detail can clear old receipts");
});

test("switching dispatcher identity ignores a late detail response from the previous workspace", () => {
  const {workspace} = workspaceLifecycle();
  workspace.sync = () => {};
  workspace.show("first", "/work");
  workspace.model.open("a");
  const original = workspace.model;
  workspace.hide(); workspace.show("second", "/other");
  workspace.update([], {work_thread: {thread_id: "first"}, dispatchRequests: [
    {id: 1, dispatcher_id: "first", target_id: "a", state: "accepted", created_at: 100}]});
  assert.deepEqual(workspace.entries, []);
  assert.deepEqual(workspace.model.visible, []);
  workspace.hide(); workspace.show("first", "/work");
  assert.equal(workspace.model, original);
  assert.deepEqual(workspace.model.visible, ["a"]);
});

test("restoring several tiles measures the visible container before applying capacity", () => {
  const {workspace, panes, storage, measurements} = workspaceLifecycle({width: 1080, height: 350});
  const ids = ["a", "b", "c"];
  storage.set("ribbon-field.dispatcher-workspace:dispatcher", JSON.stringify({ids, visible: ids}));
  workspace.update(ids.map(id => thread(id)));
  assert.equal(panes.container.hidden, true);
  workspace.show("dispatcher", "/work");
  assert.equal(panes.container.hidden, false);
  assert.ok(measurements.length > 0);
  assert.equal(measurements.includes(true), false, "Hidden zero-size geometry must not shrink restored work");
  assert.equal(workspace.capacity, 3);
  assert.deepEqual(workspace.model.visible, ids);
  assert.deepEqual(panes.layout.order, ids);
});

test("restored queued work survives an older result until dispatcher receipts arrive", () => {
  const {workspace, storage} = workspaceLifecycle();
  storage.set("ribbon-field.dispatcher-workspace:dispatcher", JSON.stringify({
    ids: ["a"], visible: ["a"], seen: ["request:1:a", "request:2:a"], pending: [],
  }));
  const oldResult = completed("a");
  workspace.update([oldResult]);
  workspace.show("dispatcher", "/work");
  assert.equal(workspace.receiptsReady, false);
  assert.deepEqual(workspace.model.visible, ["a"], "The old result cannot retire a retained tile before receipt load");
  workspace.update([oldResult], {work_thread: {thread_id: "unrelated"}, dispatchRequests: []});
  assert.equal(workspace.receiptsReady, false);
  assert.deepEqual(workspace.model.visible, ["a"]);
  const receipt = {id: 2, dispatcher_id: "dispatcher", target_id: "a", state: "queued", created_at: 3000};
  workspace.update([oldResult], {work_thread: {thread_id: "dispatcher"}, dispatchRequests: [receipt]});
  assert.equal(workspace.receiptsReady, true);
  assert.deepEqual(workspace.model.visible, ["a"]);
  assert.equal(workspace.model.items[0].label, "Queued");
  workspace.update([oldResult], {work_thread: {thread_id: "dispatcher"}, dispatchRequests: [{...receipt, state: "accepted"}]});
  assert.deepEqual(workspace.model.visible, ["a"]);
  assert.equal(workspace.model.items[0].label, "Starting");
  workspace.update([completed("a", {turn_completion: {id: 21, status: "completed", completed_at: 4000}})]);
  assert.deepEqual(workspace.model.visible, [], "Only the new completed assignment settles into Results");
  assert.equal(workspace.model.items[0].kind, "results");
});

test("an authoritative empty receipt list releases restoration protection", () => {
  const {workspace, storage} = workspaceLifecycle();
  storage.set("ribbon-field.dispatcher-workspace:dispatcher", JSON.stringify({ids: ["a"], visible: ["a"]}));
  workspace.update([completed("a")]);
  workspace.show("dispatcher", "/work");
  assert.deepEqual(workspace.model.visible, ["a"]);
  workspace.update(null, {work_thread: {thread_id: "dispatcher"}, dispatchRequests: []});
  assert.equal(workspace.receiptsReady, true);
  assert.deepEqual(workspace.model.visible, []);
  assert.equal(workspace.model.items[0].kind, "results");
});

test("a delayed open cannot resurrect a click after leaving and reentering the same dispatcher", async () => {
  const {workspace, focused} = workspaceLifecycle();
  let resolveThread;
  workspace.fetchThread = () => new Promise(resolve => { resolveThread = resolve; });
  workspace.show("dispatcher", "/work");
  const pending = workspace.open("late");
  workspace.hide(); workspace.show("dispatcher", "/work");
  resolveThread(thread("late"));
  await pending;
  assert.deepEqual(workspace.model.visible, []);
  assert.equal(workspace.model.ids.has("late"), false);
  assert.equal(workspace.threads.some(item => item.thread_id === "late"), false);
  assert.deepEqual(focused, []);
});

test("a dispatcher starts empty without importing arbitrary project conversations", () => {
  const model = new Model();
  model.update([thread("existing"), completed("old-result")], [], {capacity: 4});
  assert.deepEqual(model.items, []);
  assert.deepEqual(model.visible, []);
  model.update([thread("existing"), thread("routed")], [handoff(1, "routed")], {capacity: 4});
  assert.deepEqual(model.items.map(item => item.thread.thread_id), ["routed"]);
  assert.deepEqual(model.visible, ["routed"]);
});

test("a routing receipt can arrive before its conversation inventory", () => {
  const model = new Model(), receipt = handoff(1, "new-agent");
  model.update([], [receipt], {capacity: 2});
  assert.deepEqual(model.visible, []);
  model.update([thread("new-agent")], [receipt], {capacity: 2});
  assert.deepEqual(model.visible, ["new-agent"]);
  model.update([thread("new-agent")], [receipt], {capacity: 2});
  assert.deepEqual(model.visible, ["new-agent"], "Polling does not duplicate the pane");
});

test("reusing a destination preserves one tile and its position", () => {
  const model = new Model(), threads = [thread("a"), thread("b")];
  model.update(threads, [handoff(1, "a"), handoff(2, "b")], {capacity: 2});
  model.update(threads, [handoff(1, "a"), handoff(2, "b"), handoff(3, "a")], {capacity: 2});
  assert.deepEqual(model.visible, ["a", "b"]);
  assert.equal(model.items.length, 2);
});

test("removing a tile survives polling while a new assignment can reopen it", () => {
  const model = new Model(), threads = [thread("a")], first = handoff(1, "a");
  model.update(threads, [first]);
  model.remove("a");
  model.update(threads, [first]);
  assert.deepEqual(model.visible, []);
  assert.equal(model.items[0].thread.thread_id, "a", "The conversation remains accessible in the rail");
  model.update(threads, [first, handoff(2, "a")]);
  assert.deepEqual(model.visible, ["a"]);
  assert.equal(threads[0].attention, "now");
});

test("background completion moves to Results without closing its session", () => {
  const model = new Model(), receipt = handoff(1, "a");
  model.update([thread("a")], [receipt]);
  const result = completed("a");
  model.update([result], [receipt]);
  assert.deepEqual(model.visible, []);
  assert.equal(model.items[0].kind, "results");
  assert.equal(model.items[0].label, "Findings");
  assert.equal(model.items[0].summary, result.checkpoint.summary);
  assert.equal(model.items[0].unread, true);
  assert.equal(result.attention, "now");
});

test("reading or editing freezes the whole layout as other work finishes or arrives", () => {
  const model = new Model(), initial = [handoff(1, "reading"), handoff(2, "background")];
  model.update([thread("reading"), thread("background")], initial, {capacity: 2});
  const next = [...initial, handoff(3, "new")];
  const inventory = [completed("reading"), completed("background"), thread("new")];
  model.update(inventory, next, {capacity: 1, protectedIds: ["reading"]});
  assert.deepEqual(model.visible, ["reading", "background"], "No shrinking, removal, or replacement while protected");
  assert.equal(model.items.find(item => item.thread.thread_id === "new").kind, "running");
  model.update(inventory, next, {capacity: 2});
  assert.deepEqual(model.visible, ["new"], "Deferred layout changes apply once reading or editing ends");
});

test("opening a result keeps it readable across future inventory updates", () => {
  const model = new Model(), receipt = handoff(1, "a"), result = completed("a");
  model.update([result], [receipt]);
  model.open("a");
  model.update([result], [receipt]);
  assert.deepEqual(model.visible, ["a"]);
  assert.equal(model.items[0].kind, "results");
  model.remove("a");
  model.update([result], [receipt]);
  assert.deepEqual(model.visible, []);
});

test("tile capacity stays bounded while overflow remains available", () => {
  const model = new Model(), threads = Array.from({length: 10}, (_, i) => thread(String(i)));
  const receipts = threads.map((item, i) => handoff(i + 1, item.thread_id));
  model.update(threads, receipts, {capacity: 3});
  assert.deepEqual(model.visible, ["0", "1", "2"]);
  assert.equal(model.items.length, 10);
  model.open("9", 3);
  model.update(threads, receipts, {capacity: 3});
  assert.deepEqual(model.visible, ["1", "2", "9"]);
  assert.equal(model.items.length, 10, "Explicit focus does not discard displaced work");
});

test("input, required review, failure, interruption, and uncertain responses stay visible", () => {
  for (const state of ["input", "action", "failed", "interrupted", "reply"]) {
    const result = completed(state, {response_state: state,
      response_classification: {status: "uncertain", choice: "done"}});
    assert.equal(describe(result).kind, "needs", state + " must not settle as a successful result");
    assert.notEqual(describe(result).label, "Completed");
  }
  const request = completed("approval", {response_state: "input", status: "running",
    checkpoint: {next_actor: "user", next_action: "Approve the deployment.", summary: "The patch is ready."}});
  assert.equal(describe(request).kind, "needs", "A real approval takes priority over runtime activity");
  assert.equal(describe(request).summary, "Approve the deployment.");
});

test("uncertain and failed delivery receipts remain actionable without creating a success tile", () => {
  const model = new Model(), receipts = ["uncertain", "failed", "paused"].map((state, i) => handoff(i, "a", {state}));
  model.update([completed("a")], receipts);
  assert.deepEqual(model.visible, []);
  assert.deepEqual(model.items, []);
  assert.deepEqual(model.receipts.map(receipt => receipt.state), ["uncertain", "failed", "paused"]);
});

test("pending routing is visible before a destination exists", () => {
  const model = new Model(), receipts = ["pending", "routing", "creating"].map((state, i) => handoff(i, null, {state}));
  model.update([], receipts);
  assert.deepEqual(model.visible, []);
  assert.equal(model.receipts.length, 3);
});

test("read state updates without mutating thread placement or implying a handled commitment", () => {
  const model = new Model(), receipt = handoff(1, "a");
  const original = completed("a", {attention: "later", handled_completion_id: 0, seen_completion_id: 0});
  const before = JSON.stringify(original);
  model.update([original], [receipt]);
  model.open("a");
  assert.equal(JSON.stringify(original), before);
  const read = {...original, unread_result: false, seen_completion_id: 20};
  model.update([read], [receipt]);
  assert.equal(model.items[0].unread, false);
  assert.equal(read.attention, "later");
  assert.equal(read.handled_completion_id, 0);
  model.remove("a");
  assert.equal(read.attention, "later");
});

test("saved workspace visibility restores without reviving dismissed tiles", () => {
  const original = new Model(), receipts = [handoff(1, "a"), handoff(2, "b")];
  const inventory = [completed("a"), thread("b")];
  original.update(inventory, receipts, {capacity: 2});
  original.open("a", 2); original.remove("b");
  const restored = new Model(JSON.parse(JSON.stringify(original.snapshot())));
  restored.update(inventory, receipts, {capacity: 2});
  assert.deepEqual(restored.visible, ["a"]);
  assert.equal(restored.manual.has("a"), true);
  assert.equal(restored.dismissed.has("b"), true);
  assert.equal(restored.items.length, 2);
});

test("restoring after a receipt precedes inventory still opens the eventual destination", () => {
  const original = new Model(), receipt = handoff(1, "arriving");
  original.update([], [receipt]);
  const restored = new Model(JSON.parse(JSON.stringify(original.snapshot())));
  restored.update([thread("arriving")], [receipt]);
  assert.deepEqual(restored.visible, ["arriving"]);
});

test("a new queued or accepted request cannot be mistaken for the previous completed result", () => {
  const old = completed("a", {turn_completion: {id: 1, status: "completed", completed_at: 50}});
  assert.equal(describe(old, handoff(1, "a", {state: "queued"})).label, "Queued");
  assert.equal(describe(old, handoff(1, "a", {state: "sending"})).label, "Starting");
  assert.equal(describe(old, handoff(1, "a")).kind, "running");
  const finished = completed("a", {turn_completion: {id: 2, status: "completed", completed_at: 150}});
  assert.equal(describe(finished, handoff(1, "a")).kind, "results");
});

test("stale checkpoints do not repeat an obsolete requested action", () => {
  const item = describe(completed("a", {response_state: "reply", checkpoint_stale: true,
    checkpoint: {next_actor: "user", next_action: "Answer the old question.", summary: "Obsolete result."}}));
  assert.equal(item.kind, "needs");
  assert.equal(item.summary.includes("old question"), false);
  assert.equal(item.summary.includes("Obsolete result"), false);
});

test("an archived session stops occupying a tile without changing its saved placement", () => {
  const model = new Model(), receipt = handoff(1, "a"), archived = thread("a", {attention: "archived"});
  model.update([thread("a")], [receipt]);
  model.update([archived], [receipt]);
  assert.deepEqual(model.visible, []);
  assert.deepEqual(model.items, []);
  assert.equal(archived.attention, "archived");
});


test("Escape dismisses mobile options with focus return but does not intercept desktop input", () => {
  const {workspace, callbacks} = workspaceLifecycle();
  workspace.show("dispatcher", "/work");
  workspace.options(true);
  const composer = workspace.$("composer"), trigger = workspace.$("dispatcher-options-toggle");
  composer.focus();
  let prevented = 0;
  const escape = {key: "Escape", preventDefault() { prevented++; }};
  trigger.offsetHeight = 0; // Disclosure hidden after resizing to desktop.
  callbacks.keydown(escape);
  assert.equal(workspace.optionsOpen, true);
  assert.equal(workspace.document.activeElement, composer);
  assert.equal(prevented, 0);
  trigger.offsetHeight = 44;
  callbacks.keydown(escape);
  assert.equal(workspace.optionsOpen, false);
  assert.equal(workspace.document.activeElement, trigger);
  assert.equal(prevented, 1);
});
