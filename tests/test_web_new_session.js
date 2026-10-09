"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const {newThreadContext} = require("../plugins/agent-coord/scripts/agent_coord/web/folders.js");
function setup() {
  const elements = new Map(), calls = [];
  const element = () => ({value: "", dataset: {}, children: [], classList: {add() {}, remove() {}},
    focus() {}, replaceChildren(...children) { this.children = children; }, append(...children) { this.children.push(...children); }});
  const c = {
    $: id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    node: (tag, text) => Object.assign(element(), {tag, textContent: text}),
    Option: function(text, value) { return {text, value}; }, navigation: null,
    state: {config: {cwd: "/server-process"}, folders: {selected: {cwd: "/workspace"}}, organization: {repositories: [], projects: []}, drafts: new Map(), commandFeedback: new Map()},
    threadOrganization: {NONE: "__none__"}, action: fn => fn(), sessionPath: id => "sessions/" + id,
    api: async (path, body) => {
      calls.push({path, body});
      if (path.startsWith("models?")) return {data: [{model: path.endsWith("claude") ? "sonnet" : "gpt-test", displayName: path.endsWith("claude") ? "Sonnet" : "GPT Test"}]};
      return {session: {thread_id: "created"}};
    },
    goHome: () => { if (c.state.selected) c.state.drafts.set(c.state.selected, c.$("message").value); c.state.selected = null; c.state.detail = null; },
    select: async id => { c.state.selected = id; c.$("message").value = c.state.drafts.get(id) || ""; },
    renderTitle() {}, renderQueuedMessages() {}, renderStatus() { c.renderModelPicker(); }, refreshList: async () => {}, calls,
  };
  c.sessionContext = () => newThreadContext(c.state.organization,
    {repository: c.$("repository").value, project: c.$("project").value}, c.state.folders.selected);
  c.renderFolders = () => {};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function filtersChanged()")), c);
  return c;
}

test("New session opens a draft with both model groups before creating any provider session", async () => {
  const c = setup();
  await c.$("new-session").onclick();
  assert.equal(c.state.selected, "new-session");
  assert.equal(c.$("conversation").hidden, false);
  assert.equal(c.calls.filter(call => call.path === "sessions").length, 0);
  assert.deepEqual(c.$("model-picker").children.map(group => group.label), ["Codex", "Claude Code"]);
  assert.equal(c.$("new-session").onclick, c.$("welcome-new").onclick);
  c.$("model-picker").value = c.modelValue("claude", "sonnet");
  await c.changeSessionModel();
  assert.equal(c.state.detail.session.client, "claude");
  assert.equal(c.calls.filter(call => call.body).length, 0);
  c.$("message").value = "Hello";
  await c.sendSessionDraft("Hello", [{name: "proof.png", url: "data:image/png;base64,YQ=="}]);
  const create = c.calls.find(call => call.path === "sessions");
  assert.equal(create.body.client, "claude");
  assert.equal(create.body.model, "sonnet");
  assert.equal(create.body.cwd, "/workspace");
  const send = c.calls.find(call => call.path === "sessions/created/messages");
  assert.equal(send.body.message, "Hello");
  assert.equal(send.body.images[0].name, "proof.png");
  assert.equal(c.state.selected, "created");
  assert.equal(c.state.newSessionDraft, null);
});

test("draft uses the window folder and group rather than the previously focused thread", async () => {
  const c = setup();
  c.state.detail = {work_thread: {cwd: "/workspace/current"}};
  c.state.organization.projects = [{id: "project"}]; c.$("project").value = "project";
  await c.newSession();
  assert.equal(c.state.detail.session.cwd, "/workspace");
  assert.equal(c.state.detail.session.project_id, "project");
  c.$("model-picker").value = c.modelValue("claude", "sonnet"); await c.changeSessionModel();
  c.$("model-picker").value = c.modelValue("codex", "gpt-test"); await c.changeSessionModel();
  await c.sendSessionDraft("Hello", []);
  assert.equal(c.calls.find(call => call.path === "sessions").body.client, "codex");
});

test("repository filter supplies workspace and preserves explicit no association", async () => {
  for (const value of ["repo", "__none__"]) {
    const c = setup(); c.state.organization.repositories = [{id: "repo", root: "/workspace/repo"}];
    c.$("repository").value = value; await c.newSession();
    assert.equal(c.state.detail.session.repository_id, value === "repo" ? "repo" : null);
    assert.equal(c.state.detail.session.cwd, value === "repo" ? "/workspace/repo" : "/workspace");
  }
});

test("one unavailable provider does not block the draft or the other model catalog", async () => {
  const c = setup(), api = c.api;
  c.api = async (path, body) => { if (path.endsWith("codex")) throw new Error("Codex missing"); return api(path, body); };
  await c.newSession();
  assert.equal(c.$("model-picker").children[1].children[1].text, "Sonnet");
  assert.match(c.$("model-picker-status").textContent, /Codex models unavailable/);
  c.$("model-picker").value = c.modelValue("claude", "sonnet"); await c.changeSessionModel();
  await c.sendSessionDraft("Hello", []);
  assert.equal(c.state.selected, "created");
});

test("repeat New session restores the same draft and chosen model", async () => {
  const c = setup(); await c.newSession();
  c.$("message").value = "Keep this";
  c.$("model-picker").value = c.modelValue("claude", "sonnet"); await c.changeSessionModel();
  c.goHome();
  c.state.folders.selected = {cwd: "/other"};
  c.state.organization.repositories = [{id: "different", root: "/different"}];
  c.$("repository").value = "different";
  await c.newSession();
  assert.equal(c.state.detail.session.cwd, "/workspace");
  assert.equal(c.state.detail.session.project_id, null);
  assert.equal(c.$("message").value, "Keep this");
  assert.equal(c.state.detail.session.model, "sonnet");
  assert.equal(c.calls.filter(call => call.path.startsWith("models?")).length, 2);
});

test("first session requires a folder, retains its group, and cancellation starts nothing", async () => {
  const c = setup(); c.state.folders.selected = null;
  c.state.organization.projects = [{id: "evaluation"}]; c.$("project").value = "evaluation";
  c.state.folderPicker = {choose: async () => false};
  await c.newSession();
  assert.equal(c.state.newSessionDraft, undefined);
  assert.equal(c.calls.length, 0);
  c.state.folderPicker.choose = async () => { c.state.folders.selected = {cwd: "/chosen"}; return true; };
  await c.newSession();
  assert.equal(c.state.detail.session.cwd, "/chosen");
  assert.equal(c.state.detail.session.project_id, "evaluation");
});

test("failed first send preserves the draft and retries the already-created session", async () => {
  const c = setup(); await c.newSession();
  c.$("message").value = "Keep this";
  const api = c.api; let fail = true;
  c.api = async (path, body) => { if (path.endsWith("/messages") && fail) throw new Error("Unavailable"); return api(path, body); };
  await assert.rejects(c.sendSessionDraft("Keep this", []), /Unavailable/);
  assert.equal(c.state.selected, "new-session");
  assert.equal(c.$("message").value, "Keep this");
  assert.equal(c.state.creatingSession, false);
  fail = false; await c.sendSessionDraft("Keep this", []);
  assert.equal(c.calls.filter(call => call.path === "sessions").length, 1);
  assert.equal(c.state.selected, "created");
});

test("creation failure allows changing provider and retrying without losing text", async () => {
  const c = setup(); await c.newSession(); c.$("message").value = "Keep";
  const api = c.api;
  c.api = async (path, body) => { if (path === "sessions") throw new Error("Missing CLI"); return api(path, body); };
  await assert.rejects(c.sendSessionDraft("Keep", []), /Missing CLI/);
  c.$("model-picker").value = c.modelValue("claude", "sonnet"); await c.changeSessionModel();
  assert.equal(c.state.detail.session.client, "claude");
  assert.equal(c.$("message").value, "Keep");
  c.api = api; await c.sendSessionDraft("Keep", []);
  assert.equal(c.state.selected, "created");
});

test("pending send does not navigate away from another thread or lose newly typed text", async () => {
  const c = setup(); await c.newSession();
  const api = c.api; let release;
  c.api = async (path, body) => path.endsWith("/messages") ? new Promise(resolve => { release = resolve; }) : api(path, body);
  const pending = c.sendSessionDraft("First", []);
  await new Promise(resolve => setImmediate(resolve));
  c.state.selected = "other"; c.state.drafts.set("new-session", "Next message");
  release({}); await pending;
  assert.equal(c.state.selected, "other");
  assert.equal(c.state.drafts.get("created"), "Next message");
});

test("established conversations keep their provider and save model changes", async () => {
  const c = setup(); await c.newSession();
  c.state.selected = "existing";
  c.state.detail = {session: {client: "claude", model: "sonnet"}, work_thread: {attention: "now"}};
  c.renderModelPicker();
  assert.deepEqual(c.$("model-picker").children.map(group => group.label), ["Claude Code"]);
  c.$("model-picker").value = c.modelValue("claude", "sonnet");
  await c.changeSessionModel();
  assert.equal(c.calls.at(-1).path, "sessions/existing");
  assert.equal(c.calls.at(-1).body.model, "sonnet");
});

async function effortSetup(client = "codex") {
  const c = setup(); await c.newSession();
  c.state.modelCatalogs.set(client, [{model: "reasoner", supportedReasoningEfforts:
    [{reasoningEffort: "low"}, {reasoningEffort: "high"}]}]);
  c.$("model-picker").value = c.modelValue(client, "reasoner"); await c.changeSessionModel();
  return c;
}

test("effort menu follows model capabilities and sends the selected draft effort for either provider", async () => {
  for (const client of ["codex", "claude"]) {
    const c = await effortSetup(client);
    assert.equal(c.$("effort-picker").disabled, false);
    assert.deepEqual(c.$("effort-picker").children.map(option => option.value), ["", "low", "high"]);
    c.$("effort-picker").value = "high"; await c.changeSessionEffort();
    assert.equal(c.state.detail.session.effort, "high");
    await c.sendSessionDraft("Hello", []);
    assert.equal(c.calls.find(call => call.path === "sessions").body.effort, "high");
  }
});

test("effort disables for unknown models, unsupported models, running and closed sessions", async () => {
  const c = await effortSetup();
  c.state.selected = "existing"; c.state.newSessionDraft = null;
  for (const mode of ["running", "archived", "unknown", "unsupported"]) {
    c.state.detail = {session: {client: "codex", model: "reasoner"}, work_thread: {attention: "now"}};
    if (mode === "running") c.state.detail.running = true;
    if (mode === "archived") c.state.detail.work_thread.attention = "archived";
    if (mode === "unknown") c.state.detail.session.model = null;
    if (mode === "unsupported") c.state.modelCatalogs.set("codex", [{model: "reasoner"}]);
    c.renderModelPicker(); assert.equal(c.$("effort-picker").disabled, true, mode);
    c.api = async () => assert.fail("Disabled effort must not save");
    await c.changeSessionEffort();
  }
});

test("effort saves or resets existing settings, preserves errors and ignores replies after navigation", async () => {
  const c = await effortSetup(); c.state.selected = "existing"; c.state.newSessionDraft = null;
  c.api = async (path, body) => {
    assert.equal(path, "sessions/existing");
    return {...c.state.detail.session, effort: body.effort};
  };
  c.$("effort-picker").value = "high"; await c.changeSessionEffort();
  assert.equal(c.state.detail.session.effort, "high");
  c.$("effort-picker").value = ""; await c.changeSessionEffort();
  assert.equal(c.state.detail.session.effort, null);
  c.api = async () => { throw new Error("Save failed"); };
  c.$("effort-picker").value = "low";
  await assert.rejects(c.changeSessionEffort(), /Save failed/);
  assert.equal(c.$("effort-picker").value, ""); assert.equal(c.state.busy, false);
  c.api = async () => {
    c.state.selected = "other"; c.state.detail.session = {model: "reasoner", effort: "low"};
    return {model: "reasoner", effort: "high"};
  };
  c.$("effort-picker").value = "high"; await c.changeSessionEffort();
  assert.equal(c.state.detail.session.effort, "low");
});
