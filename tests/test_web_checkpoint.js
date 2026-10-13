"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const markup = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/index.html"), "utf8");

function setup(extra = {}) {
  const work = {thread_id: "thread", cwd: "/workspace", domain: "web", domain_source: "derived", ...extra};
  const elements = {}, calls = [];
  for (const id of ["edit-checkpoint", "checkpoint-domain", "checkpoint-domains", "checkpoint-domain-reset",
    "checkpoint-edit-phase", "checkpoint-summary", "checkpoint-next", "checkpoint-actor", "checkpoint-dialog", "checkpoint-form"]) {
    elements[id] = {value: "", dataset: {}, focus() {}, showModal() { this.open = true; },
      close() { this.open = false; }, replaceChildren(...children) { this.children = children; }, querySelector() { return {}; }};
  }
  let pending;
  const c = {state: {selected: "thread", detail: {work_thread: work}, viewThreads: [
    {...work, domain: "web", domain_source: "agent"}, {...work, domain: "docs", domain_source: "user"},
    {...work, domain: "web", domain_source: "user"}, {...work, domain: "src/web", domain_source: "derived"},
    {...work, domain: "elsewhere", cwd: "/other", domain_source: "user"},
    {...work, domain: "closed", attention: "archived", domain_source: "user"}]},
    $: id => elements[id], Option: function(text, value) { return {text, value}; },
    threadPath: id => "threads/" + id, action: fn => { pending = fn(); }, refreshThread: async () => {}, refreshList: async () => {},
    api: async (path, payload) => { calls.push({path, payload}); }};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('$("edit-checkpoint").onclick ='), source.indexOf('$("add-link").onclick =')), c);
  const submit = () => { elements["checkpoint-form"].onsubmit({preventDefault() {}}); return pending; };
  return {elements, c, calls, submit};
}

test("checkpoint domain input has a labeled datalist and an explicit automatic reset", () => {
  assert.match(markup, /<label>Domain<input id="checkpoint-domain" list="checkpoint-domains" maxlength="64"/);
  assert.match(markup, /<datalist id="checkpoint-domains"><\/datalist>/);
  assert.match(markup, /id="checkpoint-domain-reset" type="button"/);
});

test("dialog prepopulates domain and offers deduplicated explicit workspace names", () => {
  const {elements} = setup();
  elements["edit-checkpoint"].onclick();
  assert.equal(elements["checkpoint-domain"].value, "web");
  assert.deepEqual(Array.from(elements["checkpoint-domains"].children, option => option.value), ["web", "docs"]);
  assert.equal(elements["checkpoint-dialog"].open, true);
});

test("saving progress without touching domain preserves its ownership", async () => {
  for (const domain_source of ["derived", "agent", "user"]) {
    const {elements, calls, submit} = setup({domain_source});
    elements["edit-checkpoint"].onclick();
    elements["checkpoint-summary"].value = "Updated progress.";
    await submit();
    assert.equal(calls[0].path, "threads/thread/checkpoint");
    assert.equal(calls[0].payload.summary, "Updated progress.");
    assert.equal(Object.hasOwn(calls[0].payload, "domain"), false);
    assert.equal(elements["checkpoint-dialog"].open, false);
  }
});

test("editing domain submits an override and clearing it submits a reset", async () => {
  const {elements, calls, submit} = setup({domain_source: "user"});
  elements["edit-checkpoint"].onclick();
  elements["checkpoint-domain"].value = "backend";
  await submit();
  assert.equal(calls[0].payload.domain, "backend");
  elements["edit-checkpoint"].onclick();
  elements["checkpoint-domain"].value = "";
  await submit();
  assert.equal(calls[1].payload.domain, null);
});

test("explicitly choosing the same derived name can establish a user override", async () => {
  const {elements, calls, submit} = setup();
  elements["edit-checkpoint"].onclick();
  elements["checkpoint-domain"].oninput();
  await submit();
  assert.equal(calls[0].payload.domain, "web");
});

test("automatic reset works even when already blank and does not leak to later edits", async () => {
  const {elements, calls, submit} = setup({domain: null, domain_source: null});
  elements["edit-checkpoint"].onclick();
  elements["checkpoint-domain-reset"].onclick();
  await submit();
  assert.equal(calls[0].payload.domain, null);
  elements["edit-checkpoint"].onclick();
  await submit();
  assert.equal(Object.hasOwn(calls[1].payload, "domain"), false);
  elements["checkpoint-domain-reset"].onclick();
  elements["checkpoint-domain"].value = "docs";
  elements["checkpoint-domain"].oninput();
  await submit();
  assert.equal(calls[2].payload.domain, "docs");
});
