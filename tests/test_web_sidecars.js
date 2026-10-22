"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {SidecarsUI} = require("../plugins/agent-coord/scripts/agent_coord/web/sidecars.js");

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.value = ""; this.hidden = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
  querySelectorAll(selector) {
    const tags = selector.split(",").map(value => value.trim());
    return this.children.flatMap(child => [...(tags.includes(child.tagName) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
}
function setup(api, threads = []) {
  const opened = [], errors = [];
  const document = {body: new Element("body"), createElement: tag => new Element(tag)};
  const ui = new SidecarsUI({document, api, getThreads: () => threads, openThread: id => opened.push(id), onError: error => errors.push(error)});
  clearInterval(ui.timer);
  return {ui, opened, errors, container: new Element("section")};
}
const watch = {id: "watch-1", target_id: "target", sidecar_id: "sidecar", sidecar_title: "Completion watcher", target_title: "Checkout", state: "active", status: "watching", finding: "<script>unsafe()</script>"};
const target = {thread_id: "target", title: "Checkout", browser_session: true, client: "codex", cwd: "/project"};

test("sidecar strip renders text safely and controls the captured target", async () => {
  const calls = [];
  const {ui, opened, container} = setup(async (path, body) => { calls.push({path, body}); return {watches: [watch], requests: []}; });
  ui.container = container; ui.thread = "target";
  ui.render({watches: [watch], requests: []});
  assert.equal(container.hidden, false);
  const row = container.children[0], controls = row.children[1];
  assert.equal(row.children[0].children[2].textContent, watch.finding);
  assert.equal(row.children[0].children[2].innerHTML, undefined);
  await controls.children[0].onclick(); assert.deepEqual(opened, ["sidecar"]);
  await controls.children[1].onclick();
  assert.deepEqual(calls[0], {path: "threads/target/sidecars", body: {action: "pause", id: "watch-1"}});
  ui.thread = "sidecar"; ui.render({watches: [watch], requests: []});
  assert.equal(container.children[0].children[1].children.length, 1);
});

test("late refresh cannot replace another conversation's sidecars", async () => {
  const pending = [];
  const {ui, container} = setup(path => new Promise(resolve => pending.push({path, resolve})));
  ui.showThread(target, container);
  ui.showThread({...target, thread_id: "other"}, container);
  pending[1].resolve({watches: [], requests: []});
  await Promise.resolve();
  pending[0].resolve({watches: [watch], requests: []});
  await Promise.resolve();
  assert.equal(container.hidden, true);
  assert.equal(container.children.length, 0);
});

test("attachment transport retry preserves its request key and exact payload", async () => {
  const calls = [];
  let fail = true;
  const {ui} = setup(async (path, body) => {
    if (path.startsWith("models")) return {data: [{model: "test-model", supportedReasoningEfforts: []}]};
    calls.push({path, body: structuredClone(body)});
    if (fail) throw new Error("Connection lost");
    return {status: "queued"};
  });
  await ui.open(target);
  const controls = ui.form.querySelectorAll("input, select, textarea, button");
  const model = controls.filter(item => item.tagName === "select")[3]; model.value = "test-model";
  await ui.form.onsubmit({preventDefault() {}});
  const first = calls[0];
  assert.ok(first.body.request_id);
  assert.equal(first.path, "threads/target/sidecars");
  assert.equal(first.body.max_interventions, 2);
  assert.equal(ui.dialog.open, true);
  fail = false;
  await ui.form.onsubmit({preventDefault() {}});
  assert.deepEqual(calls[1], first);
  assert.equal(ui.dialog.open, false);
});

test("completed and detached watches retain open links without action buttons", () => {
  const {ui, container} = setup(async () => {});
  ui.container = container; ui.thread = "target";
  ui.render({watches: [{...watch, state: "complete", status: "complete"}, {...watch, id: "watch-2", state: "detached", status: "detached"}], requests: []});
  assert.equal(container.children.length, 2);
  for (const row of container.children) assert.equal(row.children[1].children.length, 1);
});
