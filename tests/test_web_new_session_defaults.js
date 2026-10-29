"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/settings.js"), "utf8");
const key = "agent-coord.new-session-defaults";
const plain = value => JSON.parse(JSON.stringify(value));
const defaults = {client: "codex", model: null, effort: null, yolo: false};
function storage(initial = null) {
  let value = initial;
  return {getItem: () => value, setItem: (name, saved) => { assert.equal(name, key); value = saved; }};
}
function fixture({saved, api, store = storage(saved)} = {}) {
  const nodes = new Map(), errors = [];
  class Element {
    constructor() { this.children = []; this.value = ""; this.disabled = false; this.textContent = ""; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
  }
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const env = {localStorage: store};
  vm.runInNewContext(source, env);
  const catalog = client => [{model: client + "-model", displayName: client + " model", supportedReasoningEfforts:
    client === "antigravity" ? [] : [{reasoningEffort: "low"}, {reasoningEffort: "high"}]}];
  const request = async (path, body) => path === "session-defaults" ? {model: body.client + "-model"}
    : {data: catalog(path.split("=")[1])};
  const mount = () => env.NewSessionDefaults.mount({document: {getElementById: $, createElement: () => new Element()},
    api: api || request, config: {cwd: "/workspace"}, report: error => errors.push(error.message)});
  return {$, env, mount, errors, store, request};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test("unset, malformed, or unsupported provider preferences retain safe defaults", () => {
  for (const saved of [null, "invalid", "null", "[]", "{}", '{"client":"unknown","yolo":true}']) {
    assert.deepEqual(plain(fixture({saved}).env.NewSessionDefaults.read()), defaults);
  }
  const f = fixture();
  Object.defineProperty(f.env, "localStorage", {get() { throw Error("blocked"); }});
  assert.deepEqual(plain(f.env.NewSessionDefaults.read()), defaults);
});

test("preferences persist only the four creation defaults and require a boolean for full access", () => {
  const f = fixture();
  f.env.NewSessionDefaults.write({client: "claude", model: " sonnet ", effort: " high ", yolo: "true", cwd: "/unexpected"});
  assert.deepEqual(plain(f.env.NewSessionDefaults.read()), {client: "claude", model: "sonnet", effort: "high", yolo: false});
  assert.deepEqual(plain(fixture({store: f.store}).env.NewSessionDefaults.read()), plain(f.env.NewSessionDefaults.read()));
});

test("Settings saves model, compatible reasoning, and permissions and restores them on reopen", async () => {
  const f = fixture(); await f.mount();
  f.$("new-session-model").value = JSON.stringify(["claude", "claude-model"]);
  f.$("new-session-model").onchange(); await settle();
  assert.deepEqual(f.$("new-session-effort").children.map(item => item.value), ["", "low", "high"]);
  f.$("new-session-effort").value = "high"; f.$("new-session-effort").onchange();
  f.$("new-session-permissions").value = "full"; f.$("new-session-permissions").onchange();
  assert.deepEqual(plain(f.env.NewSessionDefaults.read()), {client: "claude", model: "claude-model", effort: "high", yolo: true});
  const reopened = fixture({store: f.store}); await reopened.mount();
  assert.equal(reopened.$("new-session-model").value, JSON.stringify(["claude", "claude-model"]));
  assert.equal(reopened.$("new-session-effort").value, "high");
  assert.equal(reopened.$("new-session-permissions").value, "full");
});

test("changing models resets reasoning and disables it for models without effort support", async () => {
  const f = fixture({saved: JSON.stringify({client: "codex", model: "codex-model", effort: "high", yolo: true})});
  await f.mount();
  f.$("new-session-model").value = JSON.stringify(["antigravity", "antigravity-model"]);
  f.$("new-session-model").onchange(); await settle();
  assert.equal(f.$("new-session-effort").disabled, true);
  assert.equal(f.env.NewSessionDefaults.read().effort, null);
  assert.equal(f.env.NewSessionDefaults.read().yolo, true);
});

test("configured models expose the reasoning levels of the effective model", async () => {
  const f = fixture(); await f.mount();
  assert.equal(f.$("new-session-effort").disabled, false);
  assert.deepEqual(f.$("new-session-effort").children.map(item => item.value), ["", "low", "high"]);
  f.$("new-session-effort").value = "low"; f.$("new-session-effort").onchange();
  assert.deepEqual(plain(f.env.NewSessionDefaults.read()), {...defaults, effort: "low"});
});

test("unavailable catalogs preserve saved choices and can be refreshed", async () => {
  let failing = true;
  const initial = {client: "codex", model: "codex-model", effort: "high", yolo: false};
  const f = fixture({saved: JSON.stringify(initial), api: (...args) => {
    if (failing) return Promise.reject(Error("offline"));
    return f.request(...args);
  }});
  await f.mount();
  assert.deepEqual(plain(f.env.NewSessionDefaults.read()), initial);
  assert.match(f.$("new-session-defaults-status").textContent, /could not load/);
  assert.equal(f.$("new-session-effort").value, "high");
  failing = false; await f.$("refresh-new-session-models").onclick();
  assert.equal(f.$("new-session-effort").disabled, false);
  assert.deepEqual(f.$("new-session-effort").children.map(item => item.value), ["", "low", "high"]);
});

test("late configured-model responses cannot replace a newly selected model's reasoning", async () => {
  let release;
  const f = fixture({api: (path, body) => path === "session-defaults"
    ? new Promise(resolve => { release = resolve; }) : f.request(path, body)});
  const mounting = f.mount(); await settle();
  f.$("new-session-model").value = JSON.stringify(["antigravity", "antigravity-model"]);
  f.$("new-session-model").onchange();
  release({model: "codex-model"}); await mounting;
  assert.equal(f.$("new-session-effort").disabled, true);
  assert.equal(f.$("new-session-effort").children.length, 1);
});

test("failed persistence restores the permission control and reports failure", async () => {
  const f = fixture({store: {getItem: () => null, setItem() { throw Error("storage full"); }}});
  await f.mount();
  f.$("new-session-permissions").value = "full"; f.$("new-session-permissions").onchange();
  assert.equal(f.$("new-session-permissions").value, "default");
  assert.match(f.errors[0], /Could not save/);
  assert.deepEqual(plain(f.env.NewSessionDefaults.read()), defaults);
});

test("configured model resolution failures remain visible after catalogs finish loading", async () => {
  const f = fixture({api: (path, body) => path === "session-defaults"
    ? Promise.reject(Error("configuration unavailable")) : f.request(path, body)});
  await f.mount();
  assert.match(f.$("new-session-defaults-status").textContent, /Could not load reasoning levels/);
  assert.equal(f.$("new-session-effort").disabled, true);
});
