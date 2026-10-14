"use strict";
const {test} = require("node:test"), assert = require("node:assert/strict");
globalThis.DispatchUI = require("../plugins/agent-coord/scripts/agent_coord/web/dispatch.js");
const {SendWorkPalette} = require("../plugins/agent-coord/scripts/agent_coord/web/send-work.js");
const settle = () => new Promise(resolve => setImmediate(resolve));

function setup(api) {
  const document = new EventTarget(), elements = new Map(), timers = new Map(), saved = new Map();
  let timer = 0;
  class Element extends EventTarget {
    constructor(id) { super(); Object.assign(this, {id, children: [], value: "", hidden: false, isConnected: true,
      selectionStart: 0, selectionEnd: 0, selectionDirection: "forward", scrollTop: 0, scrollLeft: 0, attrs: {}}); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(key, value) { this.attrs[key] = value; }
    focus() { document.activeElement = this; }
    setSelectionRange(start, end, direction) { Object.assign(this, {selectionStart: start, selectionEnd: end, selectionDirection: direction}); }
    showModal() { this.open = true; }
    close() { this.open = false; this.dispatchEvent(new Event("close")); }
  }
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
  Object.assign(document, {getElementById: $, createElement: tag => new Element(tag), activeElement: $("composer"),
    querySelector: () => [...elements.values()].find(el => el.open) || null});
  const view = {setTimeout: fn => { timers.set(++timer, fn); return timer; }, clearTimeout: id => timers.delete(id)};
  const threads = [{thread_id: "release", title: "Release deployment", cwd: "/work", browser_session: {}, checkpoint: {summary: "Deploy the app"}},
    {thread_id: "theme", title: "Gradient theme", cwd: "/work", browser_session: {}, checkpoint: {summary: "App appearance"}}];
  const calls = [], opened = [], storage = {getItem: k => saved.get(k), setItem: (k, v) => saved.set(k, v), removeItem: k => saved.delete(k)};
  const palette = new SendWorkPalette({document, view, storage, workspace: () => "/work", threads: () => threads,
    openThread: id => opened.push(id), api: async (path, body, options) => {
      calls.push({path, body, options});
      if (api) return api(path, body, options);
      if (path.endsWith("suggest")) return {candidates: []};
      return {receipt: {id: 1, cwd: "/work", state: "queued", target_id: "release", target_title: "Release deployment"}};
    }});
  SendWorkPalette.bindShortcut(document, () => palette);
  return {palette, document, $, calls, opened, saved, threads,
    type(value) { palette.input.value = value; palette.input.dispatchEvent(new Event("input")); },
    key(key, target = palette.input, extra = {}) {
      const event = new Event("keydown", {cancelable: true}); Object.assign(event, {key, ...extra}); target.dispatchEvent(event); return event;
    },
    async tick() { const pending = [...timers.values()]; timers.clear(); for (const fn of pending) fn(); await settle(); },
  };
}

test("shortcut preserves the source draft, selection and scroll; escape restores focus", () => {
  const h = setup(), composer = h.$("composer");
  Object.assign(composer, {value: "Unrelated draft", selectionStart: 2, selectionEnd: 6, scrollTop: 42});
  h.key("K", h.document, {metaKey: true, shiftKey: true});
  assert.equal(h.palette.dialog.open, true); h.type("redeploy");
  h.key("Escape", h.document);
  assert.equal(h.palette.dialog.open, false); assert.equal(h.document.activeElement, composer);
  assert.equal(composer.value, "Unrelated draft"); assert.equal(composer.selectionStart, 2); assert.equal(composer.scrollTop, 42);
  h.palette.toggle(); assert.equal(h.palette.input.value, "redeploy");
});

test("partial input matches immediately and New and Ask dispatcher are always choices", () => {
  const h = setup(); h.palette.toggle(); h.type("grad");
  assert.deepEqual(h.palette.rows.map(r => r.id), ["theme", "new", "dispatcher"]);
  assert.equal(h.calls.length, 0);
  h.type("redeploy"); assert.equal(h.palette.rows[0].id, "release");
  h.type("unknownzzzz"); assert.deepEqual(h.palette.rows.map(r => r.id), ["new", "dispatcher"]);
});

test("selected destination stays locked through edits and late suggestions", async () => {
  let resolve;
  const h = setup(() => new Promise(r => { resolve = r; }));
  h.palette.toggle(); h.type("deploy"); await h.tick();
  h.key("Tab"); assert.equal(h.palette.locked.id, "release");
  resolve({candidates: [{id: "theme", title: "Theme"}]}); await settle();
  assert.equal(h.palette.rows[h.palette.active].id, "release");
  h.type("Make it gradient instead"); assert.equal(h.palette.rows[h.palette.active].id, "release");
});

test("stale suggestions cannot replace a newer query", async () => {
  const resolvers = [];
  const h = setup(() => new Promise(r => resolvers.push(r)));
  h.palette.toggle(); h.type("deploy"); await h.tick(); h.type("grad"); await h.tick();
  resolvers[1]({candidates: [{id: "theme", title: "Theme"}]}); await settle();
  resolvers[0]({candidates: [{id: "release", title: "Release"}]}); await settle();
  assert.equal(h.palette.rows[0].id, "theme");
});

test("send contains only typed request and destination, stays in place, shows a queued receipt", async () => {
  const h = setup(); h.palette.toggle(); h.type("deploy"); h.key("Enter"); await settle();
  const sent = h.calls.find(c => c.path === "send-work/send").body;
  assert.deepEqual(Object.keys(sent).sort(), ["cwd", "destination", "message", "requestId"]);
  assert.deepEqual(sent.destination, {kind: "thread", id: "release"}); assert.equal(sent.message, "deploy");
  assert.equal(h.palette.dialog.open, false); assert.equal(h.opened.length, 0);
  assert.equal(h.palette.notice.children[0].textContent, "Queued");
  h.palette.notice.children[1].onclick({preventDefault() {}}); assert.deepEqual(h.opened, ["release"]);
});

test("failed submissions preserve draft and retry key; double Enter does not duplicate", async () => {
  let resolve, reject;
  const h = setup(() => new Promise((yes, no) => { resolve = yes; reject = no; }));
  h.palette.toggle(); h.type("deploy"); h.key("Enter"); h.key("Enter");
  assert.equal(h.calls.length, 1); reject(new Error("Offline")); await settle();
  assert.equal(h.palette.input.value, "deploy"); assert.equal(h.palette.dialog.open, true);
  h.key("Enter"); assert.equal(h.calls[0].body.requestId, h.calls[1].body.requestId);
  resolve({receipt: {id: 1, cwd: "/work", state: "accepted", target_id: "release"}}); await settle();
});

test("New and Ask dispatcher send explicit choices without invoking suggestions", async () => {
  for (const kind of ["new", "dispatcher"]) {
    const h = setup(); h.palette.toggle(); h.type("unknownzzzz");
    h.palette.list.children[kind === "new" ? 0 : 1].onclick(); await h.palette.send();
    assert.deepEqual(h.calls[0].body.destination, {kind});
    assert.equal(h.calls[0].path, "send-work/send");
  }
});

test("another modal and IME composition retain their keyboard behavior", () => {
  const h = setup(); h.$("other-dialog").open = true;
  assert.equal(h.key("K", h.document, {metaKey: true, shiftKey: true}).defaultPrevented, false);
  h.$("other-dialog").open = false; h.palette.toggle(); h.type("deploy");
  h.key("Enter", h.palette.input, {isComposing: true}); assert.equal(h.calls.length, 0);
});

test("workspace matching excludes other projects and dispatchers", () => {
  const h = setup(); h.threads.push({thread_id: "other", title: "deploy", cwd: "/other", browser_session: {}},
    {thread_id: "dispatcher", title: "deploy", cwd: "/work", browser_session: {}, dispatcher: true});
  assert.deepEqual(SendWorkPalette.matches(h.threads, "/work", "deploy").map(r => r.id), ["release"]);
});
