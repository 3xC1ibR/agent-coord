"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/ui.py"), "utf8")
  .match(/<script>\n([\s\S]*?)<\/script>/)[1];

function page() {
  const elements = Object.fromEntries(["tree", "detail", "health", "sort"].map(id =>
    [id, {innerHTML: "", textContent: "", querySelectorAll: () => []}]));
  const requests = [], timers = new Map();
  let timerID = 0;
  const context = vm.createContext({
    document: {getElementById: id => elements[id]}, location: {search: ""}, URLSearchParams, AbortController,
    console: {error() {}},
    setTimeout(callback, delay) { const id = ++timerID; timers.set(id, {callback, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback, delay) { const id = ++timerID; timers.set(id, {callback, delay}); return id; },
    fetch(url, options) { return new Promise((resolve, reject) => {
      requests.push({url, resolve, reject});
      options.signal?.addEventListener("abort", () => reject(new Error("Request timed out")));
    }); },
  });
  vm.runInContext(source, context);
  const tick = async () => { await new Promise(resolve => setImmediate(resolve)); };
  const timer = delay => {
    const entry = [...timers].find(([, item]) => item.delay === delay);
    assert.ok(entry, `Expected a ${delay}ms timer`);
    timers.delete(entry[0]); entry[1].callback();
  };
  const success = async (index = 0) => {
    requests[index].resolve({ok: true, json: async () => ({parents: [], process_count: 0})}); await tick();
  };
  return {context, elements, requests, timers, timer, success, tick};
}

test("slow monitor requests never overlap and polling resumes after completion", async () => {
  const p = page();
  vm.runInContext("refresh(); refresh();", p.context);
  assert.equal(p.requests.length, 1);
  assert.equal([...p.timers.values()].filter(t => t.delay === 1500).length, 0);
  await p.success();
  assert.match(p.elements.health.textContent, /runtime healthy/);
  p.timer(1500);
  assert.equal(p.requests.length, 2);
});

test("sort changes during a request use the latest sort on the next refresh", async () => {
  const p = page();
  p.elements.sort.value = "name";
  p.elements.sort.onchange();
  assert.equal(p.requests.length, 1);
  await p.success(); p.timer(1500);
  assert.match(p.requests[1].url, /sort=name/);
});

test("failed and stalled monitor requests show an error and retry", async () => {
  const p = page();
  p.timer(30000); await p.tick();
  assert.match(p.elements.health.textContent, /runtime unavailable/);
  assert.match(p.elements.tree.innerHTML, /retry/i);
  p.timer(1500); await p.success(1);
  assert.match(p.elements.health.textContent, /runtime healthy/);
  assert.match(p.elements.tree.innerHTML, /No delegations/);
});

test("workers without issues display their delegation identity", async () => {
  const p = page();
  const child = {
    delegation_id: "direct-worker", parent_session_id: "parent", child_session_id: null,
    client: "codex", name: null, bead_id: null, display_status: "launched",
    instructions: "Validate the change.", write_scope: ["src/**"], runtime_kind: "managed-pty",
    messages: [],
  };
  p.requests[0].resolve({ok: true, json: async () => ({process_count: 2, parents: [{
    session_id: "parent", client: "codex", name: "Parent", display_status: "idle",
    activity: "idle", cwd: "/tmp/repo", messages: [], children: [child],
  }]})});
  await p.tick();
  assert.match(p.elements.tree.innerHTML, /codex · direct-worker/);
  assert.match(p.elements.detail.innerHTML, /codex · direct-worker/);
  vm.runInContext("selected = allNodes().find(n => n.kind === 'child').id; renderDetail();", p.context);
  assert.match(p.elements.detail.innerHTML, /<small>Delegation<\/small><span>direct-worker<\/span>/);
  assert.doesNotMatch(p.elements.detail.innerHTML, /codex · null/);
});
