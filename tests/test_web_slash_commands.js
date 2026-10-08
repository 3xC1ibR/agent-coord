"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const web = path.join(__dirname, "../plugins/agent-coord/scripts/agent_coord/web");

function setup({loadModels = async () => []} = {}) {
  const doc = {addEventListener() {}, createElement: () => new Element()};
  class Element {
    constructor() { this.ownerDocument = doc; this.children = []; this.attributes = {}; this.listeners = {}; this.hidden = true; }
    addEventListener(type, listener) { this.listeners[type] = listener; }
    emit(type, event = {}) { this.listeners[type]?.(event); }
    setAttribute(name, value) { this.attributes[name] = value; }
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
    getSession: () => f.session, loadModels,
    canComplete: () => f.enabled, onChange: () => f.changes++});
  f.type = (value, caret = value.length) => {
    input.value = value; input.selectionStart = input.selectionEnd = caret;
    input.emit("input");
  };
  f.key = (key, extra = {}) => {
    const event = {key, preventDefault() { this.defaultPrevented = true; }, ...extra};
    f.complete.keydown(event); return event;
  };
  f.names = () => menu.children.map(option => option.children[0].textContent);
  return f;
}

test("slash opens supported commands with descriptions and filters case-insensitively", () => {
  const f = setup(); f.type("/");
  assert.equal(f.menu.hidden, false);
  assert.deepEqual(f.names(), ["/cd", "/model", "/effort", "/permissions", "/fork", "/close", "/help"]);
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
  assert.equal(f.input.attributes["aria-activedescendant"], "slash-commands-6");
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
