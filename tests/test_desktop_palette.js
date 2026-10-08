"use strict";
const assert = require("node:assert/strict");
const {test} = require("node:test");
const Palette = require("../desktop/macos/command-palette.js");
const settle = () => new Promise(resolve => setImmediate(resolve));

function fixture(loadThreads = async () => [
  {thread_id: "one", title: "Build native app", cwd: "/projects/desktop"},
  {thread_id: "two", title: "Café notes", cwd: "/projects/writing", project_name: "Ideas", repository_name: "journal"},
]) {
  const all = [], runs = [], errors = [];
  const document = {activeElement: null};
  class Element extends EventTarget {
    constructor(tag) {
      super(); this.tagName = tag; this.children = []; this.attributes = new Map(); this.dataset = {};
      this.value = ""; this.open = false; this._text = ""; all.push(this);
    }
    get isConnected() { return this === document.body || this === document.head || !!this.parent?.isConnected; }
    set textContent(value) { this._text = String(value); this.replaceChildren(); }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
    append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
    replaceChildren(...nodes) { for (const child of this.children) child.parent = null; this.children = []; this.append(...nodes); }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    scrollIntoView() {}
    getBoundingClientRect() { return {left: 20, right: 620, top: 60, bottom: 500}; }
  }
  document.createElement = tag => new Element(tag);
  document.querySelector = selector => selector === "dialog[open]" ? all.find(node => node.tagName === "dialog" && node.open && node.isConnected) : null;
  document.head = new Element("head"); document.body = new Element("body");
  const draft = new Element("textarea"); draft.value = "Keep this draft"; draft.selectionStart = 4; draft.selectionEnd = 8;
  document.body.append(draft); draft.focus();
  const palette = new Palette({document, loadThreads, onError: error => errors.push(error.message),
    actions: () => [
      {id: "new", label: "New session", shortcut: "⌘N", keywords: "create chat", run: () => runs.push("new")},
      {id: "sidebar", label: "Toggle sidebar", run: () => runs.push("sidebar")},
    ], selectThread: async id => runs.push(id)});
  const search = value => { palette.input.value = value; palette.input.dispatchEvent(new Event("input")); };
  const key = (key, extra = {}) => {
    const event = Object.assign(new Event("keydown", {cancelable: true}), {key, ...extra});
    palette.input.dispatchEvent(event); return event;
  };
  return {palette, document, draft, search, key, runs, errors};
}

test("search finds action keywords, thread titles, workspace paths, and accents", async () => {
  const f = fixture(); f.palette.toggle(); await settle();
  f.search("create chat"); assert.match(f.palette.list.textContent, /New session/);
  f.search("desktop"); assert.match(f.palette.list.textContent, /Build native app/);
  assert(!f.palette.list.textContent.includes("New session"));
  f.search("cafe ideas"); assert.match(f.palette.list.textContent, /Café notes/);
  f.search("journal"); assert.match(f.palette.list.textContent, /Café notes/);
  assert.equal(f.runs.length, 0);
});

test("arrow navigation and Enter select one result while Escape preserves draft and focus", async () => {
  const f = fixture(); f.palette.toggle(); await settle();
  assert.equal(f.document.activeElement, f.palette.input);
  assert.equal(f.palette.input.getAttribute("aria-activedescendant"), "desktop-command-option-0");
  f.key("ArrowDown"); assert.equal(f.palette.input.getAttribute("aria-activedescendant"), "desktop-command-option-1");
  f.key("Enter"); await settle(); assert.deepEqual(f.runs, ["sidebar"]);
  assert.equal(f.palette.dialog.open, false);
  f.draft.focus(); f.palette.toggle(); f.search("native");
  f.key("Escape");
  assert.equal(f.document.activeElement, f.draft);
  assert.equal(f.draft.value, "Keep this draft");
  assert.deepEqual([f.draft.selectionStart, f.draft.selectionEnd], [4, 8]);
  assert.equal(f.palette.input.getAttribute("aria-expanded"), "false");
});

test("Enter navigates to a matching thread and empty results cannot execute a stale selection", async () => {
  const f = fixture(); f.palette.toggle(); await settle();
  f.search("native"); f.key("Enter"); await settle(); assert.deepEqual(f.runs, ["one"]);
  f.palette.toggle(); await settle(); f.search("nothing matches this");
  assert.match(f.palette.status.textContent, /No matching/);
  assert.equal(f.palette.input.getAttribute("aria-activedescendant"), null);
  f.key("Enter"); f.key("ArrowDown"); await settle(); assert.deepEqual(f.runs, ["one"]);
});

test("composition Enter and normal editing keys keep the palette open", async () => {
  const f = fixture(); f.palette.toggle(); await settle();
  assert.equal(f.key("Enter", {isComposing: true}).defaultPrevented, false);
  assert.equal(f.key("ArrowLeft").defaultPrevented, false);
  assert.equal(f.key("Home").defaultPrevented, false);
  await settle(); assert.deepEqual(f.runs, []); assert(f.palette.dialog.open);
});

test("closing aborts a request and stale results cannot replace a reopened palette", async () => {
  const requests = [];
  const f = fixture(signal => new Promise(resolve => requests.push({signal, resolve})));
  f.palette.toggle(); await settle(); f.palette.close();
  assert(requests[0].signal.aborted);
  f.palette.toggle(); await settle();
  requests[1].resolve([{thread_id: "new", title: "New result"}]); await settle();
  requests[0].resolve([{thread_id: "old", title: "Old result"}]); await settle();
  assert(f.palette.list.textContent.includes("New result")); assert(!f.palette.list.textContent.includes("Old result"));
});

test("another dialog blocks the palette, and thread-load failure leaves commands usable", async () => {
  const f = fixture(async () => { throw new Error("offline"); });
  const dialog = f.document.createElement("dialog"); f.document.body.append(dialog); dialog.showModal();
  assert.equal(f.palette.toggle(), false); assert(!f.palette.dialog.open);
  dialog.close(); f.palette.toggle(); await settle();
  assert.match(f.palette.status.textContent, /Threads could not load/);
  f.search("new session"); f.key("Enter"); await settle(); assert.deepEqual(f.runs, ["new"]);
});

test("duplicate thread IDs are removed and untrusted titles remain literal text", async () => {
  const f = fixture(async () => [{thread_id: "a", title: "<img src=x onerror=alert(1)>"},
    {thread_id: "a", title: "duplicate"}, {title: "missing ID"}]);
  f.palette.toggle(); await settle(); f.search("img");
  assert.equal(f.palette.list.children.length, 1);
  assert.match(f.palette.list.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(f.palette.list.children[0].children[0].children[0].tagName, "span");
});
