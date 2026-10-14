"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const {ThreadScratchpad} = require("../plugins/agent-coord/scripts/agent_coord/web/scratchpad.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/project-scratchpad.js"), "utf8");
const ProjectScratchpad = vm.runInNewContext(source + "\nProjectScratchpad;", {ThreadScratchpad, URLSearchParams});
const settle = () => new Promise(resolve => setImmediate(resolve));

function setup(api) {
  const document = new EventTarget(), view = new EventTarget(), elements = new Map(), timers = new Map(), storage = new Map();
  let timer = 0;
  class Element extends EventTarget {
    constructor(id) {
      super(); Object.assign(this, {id, value: "", hidden: false, disabled: false, isConnected: true,
        selectionStart: 0, selectionEnd: 0, selectionDirection: "forward", scrollTop: 0, scrollLeft: 0, scrollHeight: 500});
      const classes = new Set();
      this.classList = {add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c)};
    }
    append(el) { el.parentElement = this; }
    focus(options) { document.activeElement = this; this.focusOptions = options; }
    setSelectionRange(start, end, direction) { Object.assign(this, {selectionStart: start, selectionEnd: end, selectionDirection: direction}); }
    showModal() { this.open = true; }
    close() { this.open = false; setImmediate(() => this.dispatchEvent(new Event("close"))); }
  }
  for (const id of ["project-scratchpad-page", "project-scratchpad-dialog", "project-scratchpad-editor", "project-scratchpad-text",
    "scratchpad-project-name", "close-project-scratchpad", "project-scratchpad-status", "composer", "body"]) elements.set(id, new Element(id));
  const $ = id => elements.get(id);
  $("project-scratchpad-page").hidden = true;
  $("project-scratchpad-page").append($("project-scratchpad-editor"));
  Object.assign(document, {body: $("body"), activeElement: $("composer"), getElementById: $,
    querySelector: () => [...elements.values()].find(el => el.open) || null});
  Object.assign(view, {document, sessionStorage: {getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k)},
    setTimeout: fn => { timers.set(++timer, fn); return timer; }, clearTimeout: id => timers.delete(id)});
  const notes = new Map(), calls = [];
  const pad = new ProjectScratchpad({document, view, api: api || (async (path, body) => {
    calls.push({path, body});
    if (body) { notes.set(body.path, body.text); return {text: body.text}; }
    return {text: notes.get(new URLSearchParams(path.split("?")[1]).get("path")) || ""};
  })});
  ProjectScratchpad.bindShortcut(document, () => pad);
  return {pad, $, document, view, notes, calls, storage,
    type(text) { $("project-scratchpad-text").value = text; $("project-scratchpad-text").dispatchEvent(new Event("input")); },
    key(extra = {}) {
      const event = new Event("keydown", {cancelable: true});
      Object.assign(event, {key: "S", code: "KeyS", metaKey: true, shiftKey: true, ...extra});
      document.dispatchEvent(event); return event;
    },
    async tick() { const jobs = [...timers.values()]; timers.clear(); for (const fn of jobs) fn(); await settle(); },
  };
}

test("shortcut toggles a floating editor and restores the composer's draft, selection, focus and scroll", async () => {
  const h = setup(), composer = h.$("composer"), input = h.$("project-scratchpad-text");
  Object.assign(composer, {value: "Unsent message", selectionStart: 2, selectionEnd: 5, scrollTop: 42});
  h.pad.setProject({cwd: "/work", name: "Work"});
  h.notes.set("/work", "Existing notes");
  assert.equal(h.key().defaultPrevented, true); await settle();
  assert.equal(h.pad.dialog.open, true); assert.equal(h.document.activeElement, input);
  assert.equal(input.selectionStart, input.value.length);
  assert.equal(h.$("scratchpad-project-name").textContent, "Work");
  h.type("Existing notes\nAnother task");
  assert.equal(h.key({repeat: true}).defaultPrevented, true); assert.equal(h.pad.dialog.open, true);
  h.key(); await settle();
  assert.equal(h.pad.dialog.open, false); assert.equal(h.document.activeElement, composer);
  assert.equal(composer.value, "Unsent message"); assert.equal(composer.selectionStart, 2);
  assert.equal(composer.selectionEnd, 5); assert.equal(composer.scrollTop, 42);
  assert.equal(h.notes.get("/work"), "Existing notes\nAnother task");
});

test("tab and floating view share the same editor, text and pending save", async () => {
  const h = setup(); h.pad.setProject({cwd: "/work"});
  h.pad.showTab(); await settle();
  const editor = h.pad.editor;
  h.type("A tab note"); h.pad.toggle();
  assert.equal(h.pad.editor, editor); assert.equal(editor.parentElement, h.pad.dialog);
  assert.equal(h.pad.input.value, "A tab note");
  h.type("A tab note\nCaptured in the floating view");
  h.pad.dialog.dispatchEvent(new Event("cancel", {cancelable: true})); await settle();
  assert.equal(h.pad.tabActive, true); assert.equal(editor.parentElement, h.pad.page);
  assert.equal(h.pad.input.value, "A tab note\nCaptured in the floating view");
  assert.equal(h.notes.get("/work"), h.pad.input.value);
});

test("Escape dismisses floating notes, saves, and returns focus without relying on a native dialog cancel", async () => {
  const h = setup(); h.pad.setProject({cwd: "/work"}); h.pad.toggle(); await settle();
  h.type("A quick capture");
  const escape = {key: "Escape", code: "Escape", metaKey: false, shiftKey: false};
  assert.equal(h.key(escape).defaultPrevented, true); await settle();
  assert.equal(h.pad.dialog.open, false); assert.equal(h.document.activeElement, h.$("composer"));
  assert.equal(h.notes.get("/work"), "A quick capture");
  assert.equal(h.key(escape).defaultPrevented, false);
});

test("changing projects flushes the original note and hides its editor", async () => {
  const h = setup(); h.pad.setProject({cwd: "/one"}); h.pad.toggle(); await settle();
  h.type("Project one"); h.pad.setProject({cwd: "/two"});
  assert.equal(h.pad.dialog.open, false); assert.equal(h.pad.tabActive, false);
  h.pad.showTab(); await settle(); assert.equal(h.pad.input.value, "");
  h.type("Project two"); h.pad.hideTab(); await settle();
  assert.equal(h.notes.get("/one"), "Project one"); assert.equal(h.notes.get("/two"), "Project two");
  h.pad.setProject({cwd: "/one"}); h.pad.showTab(); await settle();
  assert.equal(h.pad.input.value, "Project one");
});

test("no project, unrelated dialogs, composition and unrelated shortcuts keep their normal behavior", () => {
  const h = setup();
  assert.equal(h.key().defaultPrevented, false);
  h.pad.setProject({cwd: "/work"});
  h.$("composer").open = true;
  assert.equal(h.key().defaultPrevented, false);
  h.$("composer").open = false;
  for (const extra of [{isComposing: true}, {shiftKey: false}, {altKey: true}, {metaKey: false}, {key: "x", code: "KeyX"}]) {
    assert.equal(h.key(extra).defaultPrevented, false);
  }
  assert.equal(h.pad.dialog.open, undefined);
});

test("a delayed load does not steal focus after dismissal or leak another project's note", async () => {
  const resolves = [];
  const h = setup(() => new Promise(resolve => resolves.push(resolve)));
  h.pad.setProject({cwd: "/one"}); h.pad.toggle(); h.pad.close();
  resolves.shift()({text: "One"}); await settle();
  assert.equal(h.document.activeElement, h.$("composer"));
  h.pad.toggle(); h.pad.setProject({cwd: "/two"}); h.pad.toggle();
  resolves.shift()({text: "Old one"}); await settle();
  assert.notEqual(h.pad.input.value, "Old one");
  resolves.shift()({text: "Two"}); await settle();
  assert.equal(h.pad.input.value, "Two"); assert.equal(h.document.activeElement, h.pad.input);
});

test("floating notes restore focus inside a tiled conversation", async () => {
  const h = setup(), composer = h.$("composer");
  h.document.activeElement = {contentDocument: {activeElement: composer}};
  h.pad.setProject({cwd: "/work"}); h.pad.toggle(); await settle(); h.pad.close();
  assert.equal(h.document.activeElement, composer);
});
