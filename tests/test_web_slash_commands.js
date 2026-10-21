"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const web = path.join(__dirname, "../plugins/agent-coord/scripts/agent_coord/web");

function setup({loadModels = async () => [], loadSkills = async () => [], onReplace = () => {},
  getModelClients, onSelectModel = () => {}} = {}) {
  const doc = {addEventListener() {}, createElement: () => new Element()};
  class Element {
    constructor() { this.ownerDocument = doc; this.children = []; this.attributes = {}; this.listeners = {}; this.hidden = true; }
    addEventListener(type, listener) { this.listeners[type] = listener; }
    emit(type, event = {}) { this.listeners[type]?.(event); }
    setAttribute(name, value) { this.attributes[name] = value; }
    getAttribute(name) { return this.attributes[name]; }
    removeAttribute(name) { delete this.attributes[name]; }
    append(...items) { this.children.push(...items); }
    replaceChildren() { this.children = []; }
    scrollIntoView() { this.scrolled = true; }
    setRangeText(value, start, end) {
      this.value = this.value.slice(0, start) + value + this.value.slice(end);
      this.selectionStart = this.selectionEnd = start + value.length;
    }
  }
  const input = new Element(), menu = new Element(); menu.id = "slash-commands";
  doc.activeElement = input;
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(path.join(web, "slash-commands.js"), "utf8"), context);
  const f = {input, menu, doc, context, enabled: true, thread: "one", changes: 0,
    session: {client: "codex", model: "first-model"}};
  f.complete = new context.ChatSlashCommands({input, menu, getThread: () => f.thread,
    getSession: () => f.session, loadModels, loadSkills, onReplace, getModelClients, onSelectModel,
    canComplete: () => f.enabled, onChange: () => f.changes++});
  f.type = (value, caret = value.length) => {
    input.value = value; input.selectionStart = input.selectionEnd = caret;
    input.emit("input");
  };
  f.key = (key, extra = {}) => {
    const event = {key, preventDefault() { this.defaultPrevented = true; }, ...extra};
    f.complete.keydown(event); return event;
  };
  f.names = () => Array.from(f.complete.options || [], option => option.children[0].textContent);
  return f;
}

test("slash opens supported commands with descriptions and filters case-insensitively", () => {
  const f = setup(); f.type("/");
  assert.equal(f.menu.hidden, false);
  assert.deepEqual(f.names(), ["/cd", "/model", "/effort", "/permissions", "/yolo", "/fork", "/close", "/help"]);
  assert.equal(f.input.attributes["aria-expanded"], "true");
  assert.equal(f.menu.children[0].attributes["aria-selected"], "true");
  assert.match(f.menu.children[0].children[2].textContent, /directory/);
  f.type("/MO"); assert.deepEqual(f.names(), ["/model"]);
  f.type("/missing"); assert.equal(f.menu.hidden, true);
  assert.equal(f.input.attributes["aria-activedescendant"], undefined);
});

const models = [
  {model: "first-model", displayName: "First model", description: "For everyday work",
    supportedReasoningEfforts: [{reasoningEffort: "low", description: "Faster responses"}, {reasoningEffort: "high"}]},
  {model: "second-model", supportedReasoningEfforts: [{reasoningEffort: "medium"}]},
];
const settle = () => new Promise(resolve => setImmediate(resolve));

test("new draft model completion groups providers and selects provider with model", async () => {
  const clients = ["codex", "claude", "antigravity", "kimi", "muse"], selected = [], loads = [];
  const f = setup({getModelClients: () => clients,
    loadModels: async client => {
      loads.push(client);
      return [{model: "shared-model", displayName: client + " model"}];
    }, onSelectModel: choice => { selected.push({...choice}); f.session.client = choice.client; }});
  f.type("/model "); await settle();
  assert.deepEqual(loads, clients);
  assert.deepEqual(f.menu.children.map(group => group.attributes["aria-label"]),
    ["Codex", "Claude Code", "Google Antigravity", "Kimi Code", "Muse Code"]);
  assert.deepEqual(f.names(), clients.map(() => "shared-model"));
  assert.match(f.menu.children[1].children[0].children[1].textContent, /Claude Code/);
  f.key("ArrowDown");
  assert.equal(f.complete.options[1].scrolled, true);
  assert.equal(f.input.attributes["aria-activedescendant"], "slash-commands-1");
  f.key("Tab");
  assert.deepEqual(selected, [{client: "claude", model: "shared-model"}]);
  assert.equal(f.input.value, "/model shared-model ");
  assert.equal(f.changes, 1);
  f.type("/model shared");
  f.menu.children[3].children[0].emit("click");
  assert.deepEqual(selected[1], {client: "kimi", model: "shared-model"});
});

test("draft model completion filters across catalogs and tolerates unavailable providers", async () => {
  const clients = ["codex", "claude", "antigravity"], pending = {};
  const f = setup({getModelClients: () => clients,
    loadModels: client => new Promise((resolve, reject) => { pending[client] = {resolve, reject}; })});
  f.type("/model "); await settle();
  pending.claude.resolve([{model: "sonnet", supportedReasoningEfforts: [{reasoningEffort: "high"}]}]);
  pending.antigravity.reject(new Error("Not installed")); await settle();
  assert.deepEqual(f.names(), ["sonnet"]);
  pending.codex.resolve(models); await settle();
  assert.deepEqual(f.names(), ["first-model", "second-model", "sonnet"]);
  f.type("/model so");
  assert.deepEqual(f.names(), ["sonnet"]);
  assert.equal(f.menu.children[0].attributes["aria-label"], "Claude Code");
  // Effort remains specific to the selected provider even when model names collide.
  f.session = {client: "claude", model: "sonnet"};
  f.type("/effort "); assert.deepEqual(f.names(), ["high"]);
  f.key("Escape");
  pending.antigravity.resolve([{model: "late-model"}]); await settle();
  assert.equal(f.menu.hidden, true);
});

const skills = [
  {name: "agent-coord:manage-threads", description: "Review work threads", scope: "user"},
  {name: "skill-creator", description: "Create a reusable skill", scope: "system"},
];

test("dollar opens skills inline, filters namespaced names, and inserts without submitting", async () => {
  const f = setup({loadSkills: async () => skills});
  f.type("Please use $"); await settle();
  assert.deepEqual(f.names(), ["$agent-coord:manage-threads", "$skill-creator"]);
  assert.equal(f.menu.attributes["aria-label"], "Skills");
  assert.equal(f.menu.children[0].children[2].textContent, "Review work threads");
  f.key("ArrowDown"); assert.equal(f.input.attributes["aria-activedescendant"], "slash-commands-1");
  f.key("Enter"); assert.equal(f.input.value, "Please use $skill-creator ");
  assert.equal(f.changes, 1); assert.equal(f.menu.hidden, true);
  assert.equal(f.key("Enter").defaultPrevented, undefined);
  f.type("Use $MANAGE"); assert.deepEqual(f.names(), ["$agent-coord:manage-threads"]);
  f.key("Tab"); assert.equal(f.input.value, "Use $agent-coord:manage-threads ");
  f.type("Use $missing"); assert.equal(f.menu.hidden, true);
});

test("skill insertion replaces the whole active token and preserves neighboring text and references", async () => {
  const {ThreadMentionDraft} = require(path.join(web, "thread-mentions.js"));
  const text = "Use $sk-old with @Other thread";
  const ref = {start: text.indexOf("@"), end: text.length, text: "@Other thread", session_id: "other"};
  const draft = new ThreadMentionDraft(text, [ref]);
  const f = setup({loadSkills: async () => skills, onReplace: (start, end, value) => draft.edit(start, end, value)});
  f.type(text, 7); await settle(); f.key("Tab");
  assert.equal(f.input.value, "Use $skill-creator with @Other thread");
  assert.equal(draft.text, f.input.value);
  assert.equal(draft.snapshot()[0].start, f.input.value.indexOf("@"));
  f.type("First line\n($manage)", 19); await settle(); f.key("Enter");
  assert.equal(f.input.value, "First line\n($agent-coord:manage-threads)");
});

test("skill catalogs follow cwd and provider, ignore late responses, and retry after failure", async () => {
  const requests = [];
  const f = setup({loadSkills: session => new Promise((resolve, reject) => requests.push({session, resolve, reject}))});
  f.session = {client: "codex", cwd: "/one"};
  f.type("$"); await settle();
  f.session = {client: "codex", cwd: "/two"};
  f.complete.render(); await settle();
  requests[0].resolve(skills); await settle(); assert.equal(f.menu.hidden, true);
  requests[1].resolve([{name: "two"}]); await settle(); assert.deepEqual(f.names(), ["$two"]);
  f.session = {client: "claude", cwd: "/two"};
  f.complete.render(); await settle(); requests[2].reject(new Error("unavailable")); await settle();
  assert.equal(f.menu.hidden, true);
  f.type("$"); await settle(); assert.equal(requests.length, 4);
  requests[3].resolve([]); await settle();
  assert.equal(f.key("Enter").defaultPrevented, undefined);
});

test("skill completion preserves native editing, dismisses with Escape, and refreshes on focus", async () => {
  let loads = 0;
  const f = setup({loadSkills: async () => { loads++; return skills; }});
  f.type("$"); await settle();
  for (const extra of [{shiftKey: true}, {ctrlKey: true}, {isComposing: true}]) {
    assert.equal(f.key("Enter", extra).defaultPrevented, undefined);
  }
  f.key("Escape"); f.complete.render(); assert.equal(f.menu.hidden, true);
  f.type("$skill"); assert.equal(f.menu.hidden, false);
  f.input.emit("blur"); assert.equal(f.menu.hidden, true);
  f.input.emit("focus"); await settle(); assert.equal(loads, 2);
  f.input.emit("compositionstart"); assert.equal(f.menu.hidden, true);
  f.input.emit("compositionend"); assert.equal(f.menu.hidden, false);
  for (const text of ["cost$skill", "\\$skill", "${skill}", "$skill with ordinary text"]) {
    f.type(text); assert.equal(f.menu.hidden, true, text);
  }
  f.type("$"); f.enabled = false; f.complete.render(); assert.equal(f.menu.hidden, true);
});

function bindAppSlash(f, app, state) {
  state.newSessionDraft = state.detail;
  f.context.renderStatus = () => {};
  vm.runInContext(app.slice(app.indexOf("function isSessionDraft()"),
    app.indexOf("function renderModelPicker()")), f.context);
  vm.runInContext(app.slice(app.indexOf("  state.slashCommands = new ChatSlashCommands("),
    app.indexOf("  state.attachments = new ChatImageAttachments(")), f.context);
  f.complete = state.slashCommands;
}

test("new-session skill lookup uses its selected working folder and saves the completed draft", async () => {
  const f = setup(), calls = [];
  const elements = {message: f.input, "slash-commands": f.menu, composer: {hidden: false}};
  const state = {selected: "new-session", drafts: new Map(), closing: new Set(),
    detail: {session: {client: "codex", cwd: "/chosen"}, work_thread: {browser_session: true, attention: "now"}}};
  Object.assign(f.context, {state, $: id => elements[id], loadModelCatalog: async () => [],
    api: async (path, body) => { calls.push([path, {...body}]); return {data: skills}; }});
  const app = fs.readFileSync(path.join(web, "app.js"), "utf8");
  bindAppSlash(f, app, state);
  f.type("Use $skill"); await settle(); f.key("Tab");
  assert.deepEqual(calls, [["skills", {client: "codex", cwd: "/chosen"}]]);
  assert.equal(state.drafts.get("new-session"), "Use $skill-creator ");
});

test("new-session composer completes commands, provider models and effort before the first message", async () => {
  const f = setup(), calls = [];
  const elements = {message: f.input, "slash-commands": f.menu, composer: {hidden: false}};
  const state = {selected: "new-session", drafts: new Map(), closing: new Set(),
    detail: {session: {client: "codex", model: null},
      work_thread: {browser_session: true, attention: "now"}}};
  Object.assign(f.context, {state, $: id => elements[id], isSessionDraft: () => true,
    loadModelCatalog: async client => { calls.push("models?client=" + client); return client === "claude" ? models : []; },
    api: async path => { calls.push(path); return {data: models}; }});
  const app = fs.readFileSync(path.join(web, "app.js"), "utf8");
  bindAppSlash(f, app, state);
  f.type("/");
  assert.equal(f.menu.hidden, false);
  assert(f.names().includes("/help"));
  f.type("/mo"); f.key("Tab"); await settle();
  assert.deepEqual(calls, ["codex", "claude", "antigravity", "kimi", "muse"].map(client => "models?client=" + client));
  assert.deepEqual(f.names(), ["first-model", "second-model"]);
  f.key("Tab");
  assert.equal(f.input.value, "/model first-model ");
  assert.equal(state.detail.session.client, "claude");
  assert.equal(state.detail.work_thread.client, "claude");
  assert.deepEqual(f.names(), ["low", "high"]);
  f.key("ArrowDown"); f.key("Tab");
  assert.equal(state.drafts.get("new-session"), "/model first-model high ");
  assert.equal(f.key("Enter").defaultPrevented, undefined);
  f.type("/permissions yo"); f.key("Tab");
  assert.equal(state.drafts.get("new-session"), "/permissions yolo ");
  for (const guard of ["busy", "hidden", "archived", "closing"]) {
    state.busy = guard === "busy";
    elements.composer.hidden = guard === "hidden";
    state.detail.work_thread.attention = guard === "archived" ? "archived" : "now";
    state.closing = new Set(guard === "closing" ? [state.selected] : []);
    f.type("/"); assert.equal(f.menu.hidden, true, guard);
  }
});

test("slash model suggestions use an already-loaded picker catalog synchronously", () => {
  const f = setup();
  const elements = {message: f.input, "slash-commands": f.menu, composer: {hidden: false}};
  const state = {selected: "new-session", modelCatalogs: new Map([["codex", models],
    ...["claude", "antigravity", "kimi", "muse"].map(client => [client, []])]),
    drafts: new Map(), closing: new Set(), detail: {session: f.session,
      work_thread: {browser_session: true, attention: "now"}}};
  Object.assign(f.context, {state, $: id => elements[id], isSessionDraft: () => true,
    loadModelCatalog: () => assert.fail("The picker already loaded these models"),
    api: () => assert.fail("The picker already loaded these models")});
  const app = fs.readFileSync(path.join(web, "app.js"), "utf8");
  bindAppSlash(f, app, state);
  f.type("/model ");
  assert.equal(f.menu.hidden, false);
  assert.deepEqual(f.names(), ["first-model", "second-model"]);
  f.type("/effort ");
  assert.deepEqual(f.names(), ["low", "high"]);
});

test("fork and close autocomplete without executing and leave Enter available to send", () => {
  const f = setup();
  for (const [prefix, name] of [["/fo", "/fork"], ["/cl", "/close"]]) {
    f.type(prefix); assert.deepEqual(f.names(), [name]);
    assert.equal(f.key("Enter").defaultPrevented, true);
    assert.equal(f.input.value, name + " ");
    assert.equal(f.menu.hidden, true);
    assert.equal(f.key("Enter").defaultPrevented, undefined);
  }
});

test("permissions command opens modes and completes without submitting or loading models", async () => {
  let loads = 0;
  const f = setup({loadModels: async () => { loads++; return models; }});
  f.type("/per");
  assert.deepEqual(f.names(), ["/permissions"]);
  assert.equal(f.key("Tab").defaultPrevented, true);
  assert.equal(f.input.value, "/permissions ");
  assert.equal(f.menu.hidden, false);
  assert.deepEqual(f.names(), ["default", "yolo"]);
  assert.equal(f.menu.attributes["aria-label"], "Permissions");
  assert.match(f.menu.children[1].children[2].textContent, /without approval prompts/);
  f.key("ArrowDown");
  assert.equal(f.key("Enter").defaultPrevented, true);
  assert.equal(f.input.value, "/permissions yolo ");
  assert.equal(f.menu.hidden, true);
  assert.equal(f.key("Enter").defaultPrevented, undefined);
  assert.equal(f.changes, 2);
  await settle();
  assert.equal(loads, 0);
});

test("permissions mode prefixes filter, preserve surrounding text and respect dismissal", () => {
  const f = setup();
  f.type("  /PERMISSIONS DE");
  assert.deepEqual(f.names(), ["default"]);
  f.menu.children[0].emit("click");
  assert.equal(f.input.value, "  /PERMISSIONS default ");
  f.type("/permissions yo extra", 15);
  assert.deepEqual(f.names(), ["yolo"]);
  f.key("Tab");
  assert.equal(f.input.value, "/permissions yolo extra");
  for (const value of ["/permissions unknown", "/permissions default extra", "/permissions yolo ", "/permissions\n", "/permissions\ndef"]) {
    f.type(value); assert.equal(f.menu.hidden, true, value);
    assert.equal(f.key("Enter").defaultPrevented, undefined, value);
  }
  f.type("/permissions ");
  assert.equal(f.key("Escape").defaultPrevented, true);
  f.complete.render(); assert.equal(f.menu.hidden, true);
  assert.equal(f.key("Enter").defaultPrevented, undefined);
});

test("command completion opens provider models then supported effort without submitting", async () => {
  const clients = [], f = setup({loadModels: async client => { clients.push(client); return models; }});
  f.type("/mo"); f.key("Tab"); await settle();
  assert.deepEqual(clients, ["codex"]);
  assert.equal(f.menu.hidden, false);
  assert.deepEqual(f.names(), ["first-model", "second-model"]);
  assert.equal(f.menu.attributes["aria-label"], "Models");
  assert.equal(f.menu.children[0].children[2].textContent, "For everyday work");
  f.key("ArrowDown"); assert.equal(f.key("Enter").defaultPrevented, true);
  assert.equal(f.input.value, "/model second-model ");
  assert.deepEqual(f.names(), ["medium"]);
  assert.equal(f.menu.attributes["aria-label"], "Reasoning effort");
  f.key("Tab"); assert.equal(f.input.value, "/model second-model medium ");
  assert.equal(f.menu.hidden, true);
  assert.equal(f.key("Enter").defaultPrevented, undefined);
  assert.equal(f.changes, 3);
});

test("model and effort prefixes filter and pointer completion replaces only that argument", async () => {
  const f = setup({loadModels: async () => models});
  f.type("/model SE"); await settle();
  assert.deepEqual(f.names(), ["second-model"]);
  f.type("  /model fi high", 11); f.menu.children[0].emit("click");
  assert.equal(f.input.value, "  /model first-model high");
  f.type('/model "second-model" m');
  assert.deepEqual(f.names(), ["medium"]);
  f.key("Tab"); assert.equal(f.input.value, '/model "second-model" medium ');
  f.type('/model "sec'); f.key("Enter");
  assert.equal(f.input.value, "/model second-model ");
  f.type("/effort H"); assert.deepEqual(f.names(), ["high"]);
  f.key("Enter"); assert.equal(f.input.value, "/effort high ");
});

test("effort suggestions track the active model and never invent unsupported choices", async () => {
  const f = setup({loadModels: async () => models});
  f.type("/effort "); await settle();
  assert.deepEqual(f.names(), ["low", "high"]);
  f.key("ArrowDown");
  f.session.model = "second-model"; f.complete.render();
  assert.deepEqual(f.names(), ["medium"]);
  assert.equal(f.menu.children[0].attributes["aria-selected"], "true");
  for (const value of ["/model missing ", "/model first-model ultra", "/effort high extra", "/model first-model high extra", "/model\n", "/effort\n"]) {
    f.type(value); assert.equal(f.menu.hidden, true, value);
  }
  f.session.model = "missing"; f.type("/effort "); assert.equal(f.menu.hidden, true);
});

test("late model results respect provider switches and Escape dismissal", async () => {
  const pending = {}, clients = [];
  const f = setup({loadModels: client => { clients.push(client); return new Promise(resolve => { pending[client] = resolve; }); }});
  f.type("/model "); f.complete.render(); await settle();
  assert.deepEqual(clients, ["codex"]);
  f.thread = "two"; f.session = {client: "claude", model: "sonnet"}; f.complete.render(); await settle();
  pending.codex(models); await settle();
  assert.equal(f.menu.hidden, true);
  pending.claude([{model: "sonnet", supportedReasoningEfforts: [{reasoningEffort: "high"}]}]); await settle();
  assert.deepEqual(f.names(), ["sonnet"]);
  f.key("Escape"); f.complete.render(); assert.equal(f.menu.hidden, true);
  f.type("/effort "); assert.deepEqual(f.names(), ["high"]);
  f.thread = "one"; f.session = {client: "codex", model: "first-model"}; f.complete.render();
  assert.deepEqual(f.names(), ["low", "high"]);
  assert.deepEqual(clients, ["codex", "claude"]);
});

test("loading failures allow manual commands and retry on later input", async () => {
  let calls = 0;
  const f = setup({loadModels: async () => { if (++calls === 1) throw new Error("Offline"); return models; }});
  f.type("/model "); await settle();
  assert.equal(f.menu.hidden, true);
  f.type("/model fi"); await settle();
  assert.deepEqual(f.names(), ["first-model"]);
  f.type("/model unknown"); assert.equal(f.key("Enter").defaultPrevented, undefined);
});

test("model results arriving after blur or draft changes do not reopen suggestions", async () => {
  let resolve;
  const f = setup({loadModels: () => new Promise(done => { resolve = done; })});
  f.type("/model "); await settle();
  f.doc.activeElement = null; f.input.emit("blur");
  resolve(models); await settle(); assert.equal(f.menu.hidden, true);
  f.doc.activeElement = f.input; f.type("ordinary text"); f.complete.render();
  assert.equal(f.menu.hidden, true);
});

test("Escape dismisses suggestions even while the catalog is loading", async () => {
  let resolve;
  const f = setup({loadModels: () => new Promise(done => { resolve = done; })});
  f.type("/model "); await settle();
  assert.equal(f.key("Escape").defaultPrevented, true);
  resolve(models); await settle();
  assert.equal(f.menu.hidden, true);
  assert.equal(f.key("Enter").defaultPrevented, undefined);
});

test("arrows wrap, filtering resets selection, Enter and Tab insert without submitting", () => {
  const f = setup(); f.type("/");
  assert.equal(f.key("ArrowUp").defaultPrevented, true);
  assert.equal(f.input.attributes["aria-activedescendant"], "slash-commands-7");
  f.key("ArrowDown"); f.key("ArrowDown");
  assert.equal(f.menu.children[1].attributes["aria-selected"], "true");
  assert.equal(f.menu.children[1].scrolled, true);
  assert.equal(f.key("Enter").defaultPrevented, true);
  assert.equal(f.input.value, "/model "); assert.equal(f.changes, 1);
  assert.equal(f.menu.hidden, true);
  assert.equal(f.key("Enter").defaultPrevented, undefined);
  f.type("/"); f.key("ArrowUp"); f.type("/e");
  assert.equal(f.input.attributes["aria-activedescendant"], "slash-commands-0");
  f.key("Tab"); assert.equal(f.input.value, "/effort ");
});

test("pointer selection preserves focus and existing arguments", () => {
  const f = setup(); f.type('  /mo "some-model" high', 5);
  let prevented = false;
  f.menu.emit("mousedown", {preventDefault() { prevented = true; }});
  f.menu.children[0].emit("click");
  assert.equal(prevented, true);
  assert.equal(f.input.value, '  /model "some-model" high');
  assert.equal(f.input.selectionStart, 8);
  assert.equal(f.changes, 1);
  f.complete.render(); assert.equal(f.menu.hidden, true);
});

test("Escape stays dismissed through renders until the draft changes", () => {
  const f = setup(); f.type("/");
  assert.equal(f.key("Escape").defaultPrevented, true);
  f.complete.render(); f.input.emit("keyup");
  assert.equal(f.menu.hidden, true);
  assert.equal(f.key("Enter").defaultPrevented, undefined);
  f.type("/m"); assert.equal(f.menu.hidden, false);
});

test("ordinary text, paths, arguments, selections and unfocused composers do not complete", () => {
  const f = setup();
  for (const value of ["", "Discuss /model", "https://example.com", "/tmp/file", "/cd /tmp", "/model ", "hello\n/"]) {
    f.type(value); assert.equal(f.menu.hidden, true, value);
    assert.equal(f.key("Tab").defaultPrevented, undefined, value);
  }
  f.type("/model", 0); assert.equal(f.menu.hidden, true);
  f.type("/model"); f.input.selectionEnd = 2; f.complete.render(); assert.equal(f.menu.hidden, true);
  f.type("/"); f.doc.activeElement = null; f.input.emit("blur");
  f.complete.render(); assert.equal(f.menu.hidden, true);
});

test("modified keys and IME preserve native editing; disabled composers close the menu", () => {
  const f = setup(); f.type("/");
  for (const modifier of ["shiftKey", "ctrlKey", "altKey", "metaKey", "isComposing"]) {
    assert.equal(f.key("Enter", {[modifier]: true}).defaultPrevented, undefined);
    assert.equal(f.input.value, "/");
  }
  f.input.emit("compositionstart"); assert.equal(f.menu.hidden, true);
  assert.equal(f.key("Enter").defaultPrevented, undefined);
  f.input.emit("compositionend"); assert.equal(f.menu.hidden, false);
  f.enabled = false; f.complete.render(); assert.equal(f.menu.hidden, true);
  f.enabled = true; f.input.disabled = true; f.complete.render(); assert.equal(f.menu.hidden, true);
});

test("thread changes reset selection and do not retain a dismissed menu", () => {
  const f = setup(); f.type("/"); f.key("ArrowUp"); f.key("Escape");
  f.thread = "two"; f.complete.render();
  assert.equal(f.menu.hidden, false);
  assert.equal(f.input.attributes["aria-activedescendant"], "slash-commands-0");
});

test("composer completion takes precedence over send and queue, then restores both", () => {
  const f = setup();
  const elements = {message: f.input, send: {}, queue: {}, composer: {requestSubmit() { f.submits++; }}};
  Object.assign(f.context, {$: id => elements[id], state: {slashCommands: f.complete, detail: {running: true}},
    action: fn => fn(), sendMessage: mode => { assert.equal(mode, "queue"); f.queues++; }});
  const app = fs.readFileSync(path.join(web, "app.js"), "utf8");
  vm.runInContext(app.slice(app.indexOf("function composerKeydown("), app.indexOf('$("message").onkeydown')), f.context);
  f.submits = f.queues = 0;
  for (const key of ["Enter", "Tab"]) {
    f.type("/he");
    f.context.composerKeydown({key, preventDefault() {}});
    assert.equal(f.input.value, "/help ");
    assert.equal(f.submits + f.queues, 0);
  }
  f.type("ordinary message");
  f.context.composerKeydown({key: "Enter", preventDefault() {}});
  f.context.composerKeydown({key: "Tab", preventDefault() {}});
  assert.equal(f.submits, 1); assert.equal(f.queues, 1);
});

test("/yolo completes without arguments and explains its permission effect", () => {
  const f = setup(); f.type("/YO");
  assert.deepEqual(f.names(), ["/yolo"]);
  assert.match(f.menu.children[0].children[2].textContent, /without approval prompts/);
  f.key("Tab"); assert.equal(f.input.value, "/yolo ");
});
