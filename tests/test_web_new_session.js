"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const {newThreadContext} = require("../plugins/agent-coord/scripts/agent_coord/web/folders.js");
const {ThreadTabs} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-tabs.js");
const ChatImageAttachments = require("../plugins/agent-coord/scripts/agent_coord/web/image-attachments.js");
const {ThreadMentions} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-mentions.js");
function setup() {
  const elements = new Map(), calls = [];
  const element = () => ({value: "", dataset: {}, children: [], classList: {add() {}, remove() {}},
    focus() {}, replaceChildren(...children) { this.children = children; }, append(...children) { this.children.push(...children); }});
  const c = {
    $: id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    node: (tag, text) => Object.assign(element(), {tag, textContent: text}),
    Option: function(text, value) { return {text, value}; }, navigation: null, savedViews: null,
    state: {busy: false, config: {cwd: "/server-process"}, folders: {selected: {cwd: "/workspace"}}, organization: {repositories: [], projects: []}, drafts: new Map(), commandFeedback: new Map(), closing: new Set()},
    threadOrganization: {NONE: "__none__"}, action: fn => fn(), sessionPath: id => "sessions/" + id,
    api: async (path, body) => {
      calls.push({path, body});
      if (path === "session-defaults") return {model: body.model || "gpt-test", modelLabel: body.model || "GPT Test", effort: "high", supportsEffort: true};
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
  vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), c);
  return c;
}

test("configuration commands can be the first submission and display their feedback", async () => {
  for (const command of ["/help", "/cd .", "/model gpt-test", "/effort high"]) {
    const c = setup(); await c.newSession();
    const api = c.api;
    c.api = async (path, body) => {
      const result = await api(path, body);
      return path.endsWith("/messages") ? {command: {message: "Command applied"}} : result;
    };
    c.$("message").value = command;
    await c.sendMessage();
    assert.equal(c.calls.filter(call => call.path === "sessions").length, 1, command);
    assert.equal(c.calls.find(call => call.path.endsWith("/messages")).body.message, command);
    assert.equal(c.state.commandFeedback.get("created"), "Command applied");
    assert.equal(c.state.selected, "created");
    assert.equal(c.state.busy, false);
  }
});

async function commandFocusSetup() {
  const c = setup(); await c.newSession();
  const input = c.$("message"), doc = {body: {}, documentElement: {}, hasFocus: () => true, querySelector: () => null};
  input.ownerDocument = doc;
  input.focus = options => { doc.activeElement = input; input.focusOptions = options; };
  input.focus();
  c.state.navigationRevision = 0;
  const select = c.select, api = c.api;
  c.select = async (id, {focus = true} = {}) => {
    c.state.navigationRevision++;
    // Selecting an uncached conversation briefly hides the composer. Pane
    // selection returns without explicitly focusing it again.
    doc.activeElement = doc.body;
    await select(id);
    if (focus && !c.state.paneMode) input.focus({preventScroll: true});
  };
  c.api = async (path, body) => path.endsWith("/messages")
    ? {command: {message: "Command applied"}} : api(path, body);
  return c;
}

test("first permission commands retain composer focus in standalone and pane conversations", async () => {
  for (const paneMode of [false, true]) for (const command of ["/yolo", "/permissions yolo", "/permissions default"]) {
    const c = await commandFocusSetup(), input = c.$("message"), doc = input.ownerDocument;
    c.state.paneMode = paneMode;
    input.value = command;
    await c.sendMessage();
    assert.equal(c.state.selected, "new-session");
    assert.equal(c.calls.some(call => call.body && call.path !== "session-defaults"), false);
    assert.equal(input.value, "");
    assert.equal(doc.activeElement, input, `${command}, pane=${paneMode}`);
  }
});

test("a first command does not restore composer focus over user interaction", async () => {
  for (const interaction of ["click", "dialog", "blur", "navigate", "navigate-back"]) {
    const c = await commandFocusSetup(), input = c.$("message"), doc = input.ownerDocument;
    const other = {}, select = c.select;
    c.state.paneMode = true;
    c.select = async (...args) => {
      await select(...args);
      if (interaction === "click") doc.activeElement = other;
      if (interaction === "dialog") doc.querySelector = () => ({});
      if (interaction === "blur") doc.hasFocus = () => false;
      if (interaction === "navigate") c.state.selected = "other";
      if (interaction === "navigate-back") c.state.navigationRevision += 2;
    };
    input.value = "/model gpt-test";
    await c.sendMessage();
    assert.equal(doc.activeElement, interaction === "click" ? other : doc.body, interaction);
  }
});

test("a first command does not reclaim focus after navigation while sending", async () => {
  const c = await commandFocusSetup(), input = c.$("message"), doc = input.ownerDocument, api = c.api;
  c.state.paneMode = true;
  c.api = async (path, body) => {
    if (path.endsWith("/messages")) c.state.navigationRevision += 2;
    return api(path, body);
  };
  input.value = "/model gpt-test";
  await c.sendMessage();
  assert.equal(doc.activeElement, doc.body);
});

test("creating a conversation in a hidden group restores that group only after creation succeeds", async () => {
  const c = setup(), restored = [];
  c.savedViews = {showGroup: (id, cwd) => restored.push({id, cwd})};
  c.sessionContext = () => ({cwd: "/workspace", project_id: "launch"});
  await c.newSession();
  assert.deepEqual(restored, [], "Opening a draft is not creation");
  const api = c.api;
  c.api = async (path, body) => { if (path === "sessions") throw new Error("Unavailable"); return api(path, body); };
  await assert.rejects(c.sendSessionDraft("Hello", []), /Unavailable/);
  assert.deepEqual(restored, []);
  c.api = api;
  await c.sendSessionDraft("Hello", []);
  assert.deepEqual(restored, [{id: "launch", cwd: "/workspace"}]);
});

test("draft thread commands never create a provider session or become prompts", async () => {
  for (const command of ["/fork", "/close extra", "/fork extra"]) {
    const c = setup(); await c.newSession(); c.$("message").value = command;
    await assert.rejects(c.sendMessage(), /before forking|without arguments/);
    assert.equal(c.calls.filter(call => call.body && call.path !== "session-defaults").length, 0, command);
    assert.equal(c.$("message").value, command);
    assert.equal(c.state.busy, false);
  }
  const c = setup(); await c.newSession(); c.$("message").value = "/CLOSE ";
  await c.sendMessage();
  assert.equal(c.state.newSessionDraft, null);
  assert.equal(c.state.selected, null);
  assert.equal(c.calls.filter(call => call.body && call.path !== "session-defaults").length, 0);
});

test("New session opens a draft with all provider groups before creating any provider session", async () => {
  const c = setup();
  await c.$("new-session").onclick();
  assert.equal(c.state.selected, "new-session");
  assert.equal(c.$("conversation").hidden, false);
  assert.equal(c.calls.filter(call => call.path === "sessions").length, 0);
  assert.deepEqual(c.$("model-picker").children.map(group => group.label), ["Codex", "Claude Code", "Google Antigravity", "Kimi Code", "Muse Code"]);
  assert.equal(c.$("new-session").onclick, c.$("welcome-new").onclick);
  c.$("model-picker").value = c.modelValue("claude", "sonnet");
  await c.changeSessionModel();
  assert.equal(c.state.detail.session.client, "claude");
  assert.equal(c.calls.filter(call => call.body && call.path !== "session-defaults").length, 0);
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

test("slash selection updates draft settings and creates the selected provider on first send", async () => {
  const c = setup(); await c.newSession();
  c.state.detail.session.effort = "high";
  c.selectDraftModel({client: "claude", model: "sonnet"});
  assert.equal(c.state.detail.session.client, "claude");
  assert.equal(c.state.detail.work_thread.client, "claude");
  assert.equal(c.state.detail.session.model, "sonnet");
  assert.equal(c.state.detail.session.effort, null);
  assert.equal(c.$("model-picker").value, c.modelValue("claude", "sonnet"));
  assert.equal(c.calls.filter(call => call.body && call.path !== "session-defaults").length, 0);
  await c.sendSessionDraft("/model sonnet", []);
  const create = c.calls.find(call => call.path === "sessions");
  assert.equal(create.body.client, "claude");
  assert.equal(create.body.model, "sonnet");
});

test("provider selection is locked after creation, including a failed first send", async () => {
  const c = setup(); await c.newSession();
  c.selectDraftModel({client: "claude", model: "sonnet"});
  c.state.newSessionDraft.createdId = "created";
  assert.deepEqual(Array.from(c.sessionModelClients()), ["claude"]);
  c.selectDraftModel({client: "codex", model: "gpt-test"});
  assert.equal(c.state.detail.session.client, "claude");
  c.state.selected = "created"; c.state.newSessionDraft = null;
  c.selectDraftModel({client: "codex", model: "gpt-test"});
  assert.equal(c.state.detail.session.client, "claude");
});

test("Antigravity selection is sent to the backend and its setup error is visible", async () => {
  const c = setup();
  const api = c.api;
  c.api = async (path, body) => {
    if (path === "models?client=antigravity") throw new Error("Run agent-coord antigravity login");
    return api(path, body);
  };
  await c.newSession();
  c.$("model-picker").value = c.modelValue("antigravity", "");
  await c.changeSessionModel();
  assert.match(c.$("model-picker-status").textContent, /antigravity login/);
  await c.sendSessionDraft("Hello", []);
  assert.equal(c.calls.find(call => call.path === "sessions").body.client, "antigravity");
});

test("Kimi selection is sent to the backend and its setup error is visible", async () => {
  const c = setup();
  const api = c.api;
  c.api = async (path, body) => {
    if (path === "models?client=kimi") throw new Error("Run kimi login");
    return api(path, body);
  };
  await c.newSession();
  c.$("model-picker").value = c.modelValue("kimi", "");
  await c.changeSessionModel();
  assert.match(c.$("model-picker-status").textContent, /kimi login/);
  await c.sendSessionDraft("Hello", []);
  assert.equal(c.calls.find(call => call.path === "sessions").body.client, "kimi");
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

test("repository metadata cannot override the active project directory", async () => {
  for (const value of ["repo", "__none__"]) {
    const c = setup(); c.state.organization.repositories = [{id: "repo", root: "/workspace/repo"}];
    c.$("repository").value = value; await c.newSession();
    assert.equal(c.state.detail.session.repository_id, undefined);
    assert.equal(c.state.detail.session.cwd, "/workspace");
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
  assert.equal(c.calls.filter(call => call.path.startsWith("models?")).length, 5);
});

test("model menus share an in-flight catalog request and reuse its result", async () => {
  const c = setup(), api = c.api;
  let release, loads = 0;
  c.api = (path, body) => {
    if (path !== "models?client=codex") return api(path, body);
    loads++;
    return new Promise(resolve => { release = resolve; });
  };
  const opening = c.newSession();
  await new Promise(resolve => setImmediate(resolve));
  const completion = c.loadModelCatalog("codex");
  assert.equal(loads, 1);
  const catalog = [{model: "shared"}];
  release({data: catalog});
  await opening;
  assert.equal(await completion, catalog);
  assert.equal(await c.loadModelCatalog("codex"), catalog);
  assert.equal(c.state.modelLoading.size, 0);
});

test("failed shared catalog requests can retry without poisoning other providers", async () => {
  const c = setup(), api = c.api; let attempts = 0;
  c.api = (path, body) => {
    if (path === "models?client=codex" && ++attempts === 1) return Promise.reject(new Error("Temporarily offline"));
    return api(path, body);
  };
  await c.newSession();
  assert.equal(c.state.modelErrors.get("codex"), "Temporarily offline");
  assert(c.state.modelCatalogs.has("claude"));
  await c.loadModelCatalog("codex");
  assert.equal(attempts, 2);
  assert.equal(c.state.modelErrors.has("codex"), false);
  assert.equal(c.state.modelLoading.size, 0);
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
  assert.equal(c.sessionBusy(), false);
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
  const update = c.calls.find(call => call.path === "sessions/existing");
  assert.equal(update.body.model, "sonnet");
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

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
const settle = () => new Promise(resolve => setImmediate(resolve));
function projectSetup(readFile = async () => "data:image/png;base64,YQ==") {
  const c = setup();
  c.state.projectCwd = "/workspace";
  c.state.threadTabs = new ThreadTabs({});
  c.state.threadTabs.setProject(c.state.projectCwd);
  c.state.attachments = new ChatImageAttachments({getThread: () => c.composerDraftId(),
    canAttach: () => true, onChange() {}, onError: error => c.errors.push(error), readFile});
  c.state.mentions = Object.assign(Object.create(ThreadMentions.prototype), {
    drafts: new Map(), getThread: () => c.composerDraftId(), input: c.$("message"), render() {},
  });
  c.errors = [];
  c.renderList = () => {};
  c.refreshDetail = async () => {};
  c.showError = error => c.errors.push(error);
  c.savedViews = {setProject: async () => {}, restoreScroll() {}, showGroup() {}};
  vm.runInContext(source.slice(source.indexOf("async function switchProject("), source.indexOf("function renderFolders()")), c);
  c.switchTo = async cwd => {
    c.state.folders.selected = {cwd};
    await c.switchProject();
  };
  c.select = async id => {
    if (id === "new-session") return c.newSession();
    if (c.state.selected) c.state.drafts.set(c.state.selected, c.$("message").value);
    c.state.selected = id;
    c.state.detail = {session: {client: "codex"}, work_thread: {thread_id: id, browser_session: true, attention: "now"}};
    c.$("message").value = c.state.drafts.get(id) || "";
    c.state.threadTabs.open({thread_id: id, title: id});
  };
  return c;
}
const image = name => ({name, type: "image/png", size: 1});

test("first sends stay with their projects while another project's draft is created and sent", async () => {
  for (const phase of ["creation", "message"]) {
    const c = projectSetup(); await c.newSession();
    const a = c.state.newSessionDraft, gates = {a: deferred(), b: deferred()};
    c.$("message").value = "Send A";
    await c.state.attachments.add([image("A.png")]);
    let creations = 0;
    c.api = async (path, body) => {
      c.calls.push({path, body});
      if (path === "sessions") {
        const id = ++creations === 1 ? "a" : "b";
        if (phase === "creation") await gates[id].promise;
        return {session: {thread_id: id}};
      }
      if (phase === "message") await gates[path.split("/")[1]].promise;
      return {};
    };
    const first = c.sendMessage(); await settle();
    assert.equal(c.sessionBusy(), true);
    await c.sendMessage(); assert.equal(creations, 1, "duplicate submission stays blocked");
    c.$("message").value = "Next A";
    c.state.mentions.draft().text = "Next A";
    await c.state.attachments.add([image("next-A.png")]);
    await c.switchTo("/other"); await c.newSession();
    const b = c.state.newSessionDraft;
    assert.notEqual(a.draftId, b.draftId);
    assert.equal(c.sessionBusy(), false);
    c.$("message").value = "Send B";
    await c.state.attachments.add([image("B.png")]);
    c.state.mentions.draft().text = "Send B";
    const second = c.sendMessage(); await settle();
    assert.equal(creations, 2);
    gates.a.resolve(); await first;
    assert.equal(c.state.selected, "new-session");
    assert.equal(c.state.newSessionDraft, b);
    assert.equal(c.$("message").value, "Send B");
    assert.equal(c.state.mentions.draft().text, "Send B");
    assert.equal(c.state.attachments.snapshot()[0].name, "B.png");
    assert.equal(c.sessionBusy(), true, "A's completion must not release B's pending send");
    assert.equal(c.state.drafts.get("a"), "Next A");
    assert.equal(c.state.attachments.snapshot("a")[0].name, "next-A.png");
    assert.equal(c.state.mentions.draft("a").text, "Next A");
    assert.equal(c.state.threadTabs.projects.get("/workspace")[0].thread_id, "a");
    gates.b.resolve(); await second;
    assert.equal(c.state.selected, "b");
    await c.switchTo("/workspace");
    assert.equal(c.state.selected, "a");
    assert.equal(c.$("message").value, "Next A");
    assert.equal(c.state.newSessionDraft, null);
    assert.equal(c.state.sending.size, 0);
  }
});

test("failed first send preserves its originating draft for retry after switching projects", async () => {
  const c = projectSetup(); await c.newSession();
  const a = c.state.newSessionDraft, gate = deferred();
  c.$("message").value = "Keep A";
  await c.state.attachments.add([image("A.png")]);
  const api = c.api; let failed = false;
  c.api = async (path, body) => {
    if (path.endsWith("/messages") && !failed) { failed = true; return gate.promise; }
    return api(path, body);
  };
  const sending = c.sendMessage(); await settle();
  await c.switchTo("/other"); await c.newSession();
  c.$("message").value = "Keep B";
  gate.reject(new Error("Unavailable"));
  await assert.rejects(sending, /Unavailable/);
  assert.equal(c.$("message").value, "Keep B"); assert.equal(c.sessionBusy(), false);
  await c.switchTo("/workspace");
  assert.equal(c.state.newSessionDraft, a);
  assert.equal(c.$("message").value, "Keep A");
  assert.equal(c.state.attachments.snapshot()[0].name, "A.png");
  await c.sendMessage();
  assert.equal(c.calls.filter(call => call.path === "sessions").length, 1);
  assert.equal(c.state.selected, "created");
});

test("returning to a draft during creation retains its pending send and completes in that project", async () => {
  const c = projectSetup(); await c.newSession();
  const gate = deferred(), api = c.api;
  c.$("message").value = "Send A";
  c.api = async (path, body) => { if (path === "sessions") await gate.promise; return api(path, body); };
  const sending = c.sendMessage(); await settle();
  await c.switchTo("/other"); await c.newSession();
  await c.switchTo("/workspace");
  assert.equal(c.sessionBusy(), true);
  gate.resolve(); await sending;
  assert.equal(c.state.projectCwd, "/workspace"); assert.equal(c.state.selected, "created");
  assert.equal(c.state.sending.size, 0);
});

test("image reads and failures stay with their draft across project switches", async () => {
  for (const failure of [false, true]) {
    const gate = deferred(), c = projectSetup(file => file.name === "A.png" ? gate.promise : "data:image/png;base64,YQ==");
    await c.newSession(); const a = c.state.newSessionDraft;
    const reading = c.state.attachments.add([image("A.png")]); await settle();
    c.$("message").value = "Do not send incomplete A";
    await c.sendMessage(); assert.equal(c.calls.some(call => call.path === "sessions"), false);
    await c.switchTo("/other"); await c.newSession();
    const b = c.state.newSessionDraft;
    await c.state.attachments.add([image("B.png")]);
    if (failure) gate.reject(new Error("Read failed")); else gate.resolve("data:image/png;base64,YQ==");
    await reading;
    assert.equal(c.state.newSessionDraft, b);
    assert.equal(c.state.attachments.snapshot()[0].name, "B.png");
    await c.switchTo("/workspace");
    assert.equal(c.state.newSessionDraft, a);
    assert.equal(c.state.attachments.pending(), false);
    assert.equal(c.state.attachments.snapshot().length, failure ? 0 : 1);
    assert.equal(c.errors.length, failure ? 1 : 0);
  }
});

test("an existing send and its refresh cannot block another project's send or clear its draft", async () => {
  const c = projectSetup(); await c.select("a");
  c.$("message").value = "Send A";
  const gate = deferred(), refresh = deferred(), api = c.api;
  c.api = async (path, body) => path === "sessions/a/messages" ? gate.promise : api(path, body);
  const first = c.sendMessage(); await settle();
  // Leave while the original HTTP request is pending; the next project is usable immediately.
  c.refreshList = async () => {};
  await c.switchTo("/other"); await c.select("b"); c.$("message").value = "Send B";
  const second = c.sendMessage(); await second;
  assert.equal(c.sessionBusy(), false);
  c.$("message").value = "Next B";
  c.refreshList = () => refresh.promise;
  gate.resolve({}); await settle();
  assert.equal(c.sessionBusy(), false, "A's refresh must not disable B's composer");
  assert.equal(c.$("message").value, "Next B");
  refresh.resolve(); await first;
  assert.equal(c.state.drafts.has("a"), false);
  assert.equal(c.state.sending.size, 0);
});

test("draft permission commands stay local and configure the first real message", async () => {
  for (const client of ["codex", "claude", "antigravity", "kimi", "muse"]) {
    const c = setup(); await c.newSession(); c.state.newSessionDraft.session.client = client;
    c.calls.length = 0;
    for (const [command, expected] of [["/YOLO", true], ["/yolo", true], ["/permissions", true], ["/permissions default", false], ["/permissions YOLO", true]]) {
      c.$("message").value = command; await c.sendMessage();
      assert.equal(c.state.newSessionDraft.session.yolo, expected);
      assert.match(c.state.newSessionDraft.commandFeedback, /Permissions:/);
      assert.equal(c.state.selected, "new-session"); assert.equal(c.calls.filter(call => call.path !== "session-defaults").length, 0);
    }
    c.$("message").value = "Hello"; await c.sendMessage();
    assert.equal(c.calls.find(call => call.path === "sessions").body.yolo, true);
    assert.equal(c.calls.find(call => call.path === "sessions").body.client, client);
    assert.equal(c.calls.find(call => call.path.endsWith("/messages")).body.message, "Hello");
  }
});

test("invalid draft permission commands and attached images never create sessions", async () => {
  const c = setup(); await c.newSession(); c.calls.length = 0;
  for (const command of ["/yolo extra", "/permissions unknown", "/permissions yolo extra"]) {
    c.$("message").value = command;
    await assert.rejects(c.sendMessage(), /Use/);
    assert.equal(c.$("message").value, command);
    assert.equal(c.state.newSessionDraft.session.yolo, false);
  }
  c.state.attachments = {snapshot: () => [{name: "image.png", url: "data:image/png;base64,iVBORw0KGgo="}], pending: () => false};
  for (const command of ["/yolo", "/permissions yolo"]) {
    c.$("message").value = command;
    await assert.rejects(c.sendMessage(), /Remove attached images/);
    assert.equal(c.$("message").value, command);
    assert.equal(c.state.newSessionDraft.session.yolo, false);
  }
  assert.equal(c.calls.length, 0);
});

test("draft permission button and commands share the same setting", async () => {
  const c = setup(); await c.newSession();
  vm.runInContext(source.slice(source.indexOf("async function saveSessionPermissions()"), source.indexOf("function renderStatus()")), c);
  const dialog = c.$("permissions-dialog"); dialog.dataset.threadId = "new-session"; dialog.close = () => {};
  c.calls.length = 0;
  c.$("message").value = "/yolo"; await c.sendMessage();
  assert.equal(c.state.detail.session.yolo, true);
  c.$("edit-yolo").checked = false; await c.saveSessionPermissions();
  assert.equal(c.state.detail.session.yolo, false);
  assert.equal(c.state.newSessionDraft.commandFeedback, null);
  c.$("message").value = "/permissions"; await c.sendMessage();
  assert.match(c.state.newSessionDraft.commandFeedback, /default/);
  assert.equal(c.calls.length, 0);
});

test("a draft already created after a failed send applies permissions through its saved session", async () => {
  const c = setup(); await c.newSession(); c.state.newSessionDraft.createdId = "saved";
  c.calls.length = 0; c.$("message").value = "/yolo";
  await c.sendMessage();
  assert.equal(c.calls.some(call => call.path === "sessions"), false);
  assert.equal(c.calls.find(call => call.path === "sessions/saved/messages").body.message, "/yolo");
});

test("new draft displays effective configured settings without creating a provider conversation", async () => {
  const c = setup(), api = c.api;
  c.api = async (path, body) => {
    if (path === "session-defaults") return {model: "configured", modelLabel: "Configured model", effort: "xhigh", supportsEffort: true};
    if (path === "models?client=codex") return {data: [{model: "configured", displayName: "Configured model",
      supportedReasoningEfforts: [{reasoningEffort: "low"}, {reasoningEffort: "xhigh"}]}]};
    return api(path, body);
  };
  await c.newSession();
  assert.equal(c.$("model-picker").children[0].children[0].text, "Configured model");
  assert.equal(c.$("effort-picker").children[0].text, "xhigh (configured)");
  assert.equal(c.$("effort-picker").disabled, false);
  assert.equal(c.state.detail.session.model, null, "Keep inheriting the provider configuration");
  assert.equal(c.calls.some(call => call.path === "sessions"), false);
});

test("defaults are scoped by folder, provider and model and late replies do not change the active choice", async () => {
  const c = setup(); await c.newSession(); const pending = [];
  c.state.sessionDefaults.clear();
  c.api = (path, body) => new Promise(resolve => pending.push({path, body, resolve}));
  const first = c.loadSessionDefaults({client: "codex", cwd: "/one"});
  c.loadSessionDefaults({client: "codex", cwd: "/one"});
  const session = {client: "claude", cwd: "/two", model: "alias"};
  c.state.detail.session = session;
  const second = c.loadSessionDefaults(session);
  assert.equal(pending.length, 2, "Deduplicate in-flight reads");
  pending[1].resolve({model: "concrete", modelLabel: "Claude configured", effort: "high"}); await second;
  pending[0].resolve({model: "other", modelLabel: "Old folder", effort: "low"}); await first;
  assert.equal(c.effectiveSessionSettings(session).model, "concrete");
  assert.equal(c.effectiveSessionSettings(session).effort, "high");
  session.effort = "low";
  assert.equal(c.effectiveSessionSettings(session).effort, "low", "An explicit effort overrides inheritance");
});

test("failed defaults show unavailable and a fresh draft retries changed native configuration", async () => {
  const c = setup(), api = c.api; let reads = 0;
  c.api = async (path, body) => {
    if (path !== "session-defaults") return api(path, body);
    if (++reads === 1) throw new Error("CLI unavailable");
    return {model: "new-config", effort: "medium"};
  };
  await c.newSession();
  assert.equal(c.$("model-picker").children[0].children[0].text, "Model unavailable");
  c.renderModelPicker(); assert.equal(reads, 1, "Do not retry on every render");
  c.state.newSessionDraft = null; await c.newSession();
  assert.equal(c.$("model-picker").children[0].children[0].text, "new-config");
  assert.equal(reads, 2);
});

test("New session uses saved settings for creation and preserves edits when returning to a draft", async () => {
  const c = setup();
  let defaults = {client: "claude", model: "sonnet", effort: "high", yolo: true};
  c.NewSessionDefaults = {read: () => ({...defaults})};
  await c.newSession();
  assert.equal(c.state.newSessionDraft.work_thread.client, "claude");
  assert.equal(c.state.newSessionDraft.session.cwd, "/workspace");
  for (const [key, value] of Object.entries(defaults)) assert.equal(c.state.newSessionDraft.session[key], value);
  c.state.newSessionDraft.session.effort = "low";
  defaults = {client: "codex", model: "gpt-test", effort: null, yolo: false};
  await c.newSession();
  assert.equal(c.state.newSessionDraft.session.effort, "low");
  assert.equal(c.state.newSessionDraft.session.client, "claude");
  c.$("message").value = "Start work";
  await c.sendMessage();
  const created = c.calls.find(call => call.path === "sessions").body;
  assert.equal(created.client, "claude"); assert.equal(created.model, "sonnet");
  assert.equal(created.effort, "low"); assert.equal(created.yolo, true);
  await c.newSession();
  for (const [key, value] of Object.entries(defaults)) assert.equal(c.state.newSessionDraft.session[key], value);
});
