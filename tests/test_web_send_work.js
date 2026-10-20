"use strict";
const {test} = require("node:test"), assert = require("node:assert/strict");
globalThis.DispatchUI = require("../plugins/agent-coord/scripts/agent_coord/web/dispatch.js");
globalThis.createTransientNotice = require("../plugins/agent-coord/scripts/agent_coord/web/transient-notice.js");
require("../plugins/agent-coord/scripts/agent_coord/web/slash-commands.js");
const {SendWorkPalette} = require("../plugins/agent-coord/scripts/agent_coord/web/send-work.js");
const settle = () => new Promise(resolve => setImmediate(resolve));

function setup(api) {
  const document = new EventTarget(), elements = new Map(), timers = new Map(), saved = new Map();
  let timer = 0, now = 0, cwd = "/work";
  class Element extends EventTarget {
    constructor(id) { super(); Object.assign(this, {id, children: [], value: "", hidden: false, isConnected: true,
      ownerDocument: document, selectionStart: 0, selectionEnd: 0, selectionDirection: "forward", scrollTop: 0, scrollLeft: 0, scrollHeight: 48, style: {}, attrs: {}}); }
    append(...children) {
      for (const child of children) {
        if (child.parentElement) child.parentElement.children = child.parentElement.children.filter(c => c !== child);
        child.parentElement = this; this.children.push(child);
      }
    }
    replaceChildren(...children) { this.children = children; }
    contains(target) { return this === target || this.children.some(child => child.contains?.(target)); }
    setAttribute(key, value) { this.attrs[key] = value; }
    getAttribute(key) { return this.attrs[key]; }
    removeAttribute(key) { delete this.attrs[key]; }
    insertBefore(child, before) { const index = this.children.indexOf(before); this.children.splice(index < 0 ? this.children.length : index, 0, child); }
    scrollIntoView() {}
    setRangeText(value, start, end) {
      this.value = this.value.slice(0, start) + value + this.value.slice(end);
      this.selectionStart = this.selectionEnd = start + value.length;
    }
    focus() { document.activeElement = this; }
    setSelectionRange(start, end, direction) { Object.assign(this, {selectionStart: start, selectionEnd: end, selectionDirection: direction}); }
    showModal() { this.open = true; }
    close() { this.open = false; this.dispatchEvent(new Event("close")); }
  }
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
  Object.assign(document, {getElementById: $, createElement: tag => new Element(tag), activeElement: $("composer"),
    querySelector: () => [...elements.values()].find(el => el.open) || null});
  const view = {setTimeout: (run, delay) => { timers.set(++timer, {run, at: now + delay}); return timer; }, clearTimeout: id => timers.delete(id)};
  async function advance(delay) {
    const end = now + delay;
    for (;;) {
      const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      now = next[1].at; timers.delete(next[0]); next[1].run(); await settle();
    }
    now = end;
  }
  const threads = [{thread_id: "release", title: "Release deployment", cwd: "/work", browser_session: {}, checkpoint: {summary: "Deploy the app"}},
    {thread_id: "theme", title: "Gradient theme", cwd: "/work", browser_session: {}, checkpoint: {summary: "App appearance"}}];
  const calls = [], opened = [], storage = {getItem: k => saved.get(k), setItem: (k, v) => saved.set(k, v), removeItem: k => saved.delete(k)};
  const palette = new SendWorkPalette({document, view, storage, workspace: () => cwd, threads: () => threads,
    openThread: id => opened.push(id), api: async (path, body, options) => {
      calls.push({path, body, options});
      if (api) return api(path, body, options);
      if (path.startsWith("models?")) return {data: [{model: path.endsWith("claude") ? "claude-model" : "available-model",
        supportedReasoningEfforts: [{reasoningEffort: "medium"}, {reasoningEffort: "high"}]}]};
      if (path.endsWith("suggest")) return {candidates: []};
      if (path.endsWith("dispatcher-settings")) return {settings: body.settings || {client: "codex", model: "available-model", effort: null, yolo: false}};
      return {receipt: {id: 1, cwd: "/work", state: "queued", target_id: "release", target_title: "Release deployment"}};
    }});
  SendWorkPalette.bindShortcut(document, () => palette);
  return {palette, document, $, calls, opened, saved, threads, timers, advance,
    workspace(value) { cwd = value; palette.syncOverview(); },
    type(value) { palette.input.value = value; palette.input.setSelectionRange(value.length, value.length); palette.input.dispatchEvent(new Event("input")); },
    key(key, target = palette.input, extra = {}) {
      const event = new Event("keydown", {cancelable: true}); Object.assign(event, {key, ...extra}); target.dispatchEvent(event); return event;
    },
    async tick() { await advance(Math.max(now, ...[...timers.values()].map(timer => timer.at)) - now); },
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

test("unchanged queued receipts expire after six seconds and stop polling", async () => {
  const receipt = {id: 1, cwd: "/work", state: "queued", target_id: "release"};
  const h = setup(path => path === "send-work/send" ? {receipt} : receipt);
  h.palette.toggle(); h.type("deploy"); await h.palette.send();
  const link = h.palette.notice.children[1];
  await h.advance(5999);
  assert.equal(h.palette.notice.hidden, false);
  assert.equal(h.palette.notice.children[1], link, "polls retain the existing destination link");
  await h.advance(1); assert.equal(h.palette.notice.hidden, true);
  const polls = h.calls.length; await h.advance(20000);
  assert.equal(h.calls.length, polls); assert.equal(h.timers.size, 0);
});

test("a delivery state change gives the updated receipt a fresh reading interval", async () => {
  const h = setup();
  h.palette.receipt = {id: 1, state: "sending"}; h.palette.showReceipt();
  await h.advance(5000);
  h.palette.receipt = {id: 1, state: "accepted"}; h.palette.showReceipt();
  await h.advance(1000); assert.equal(h.palette.notice.hidden, false);
  await h.advance(5000); assert.equal(h.palette.notice.hidden, true);
});

test("failed and uncertain receipts allow ten seconds for reading", async () => {
  for (const state of ["failed", "uncertain", "paused"]) {
    const h = setup();
    h.palette.receipt = {id: 1, state}; h.palette.showReceipt();
    await h.advance(9999); assert.equal(h.palette.notice.hidden, false);
    await h.advance(1); assert.equal(h.palette.notice.hidden, true);
  }
});

test("expired or manually dismissed receipts cannot reappear after an in-flight poll", async () => {
  for (const manual of [false, true]) {
    let resolve;
    const receipt = {id: 1, cwd: "/work", state: "sending"};
    const h = setup(path => path === "send-work/send" ? {receipt} : new Promise(done => { resolve = done; }));
    h.palette.toggle(); h.type("deploy"); await h.palette.send(); await h.advance(1000);
    if (manual) h.palette.notice.children.at(-1).onclick();
    else await h.advance(5000);
    assert.equal(h.palette.notice.hidden, true);
    resolve({...receipt, state: "accepted"}); await settle();
    assert.equal(h.palette.notice.hidden, true); assert.equal(h.timers.size, 0);
  }
});

test("receipt links stay available while keyboard focused and expire after leaving", async () => {
  const h = setup(); h.palette.receipt = {id: 1, state: "accepted", target_id: "release"};
  h.palette.showReceipt();
  h.palette.notice.dispatchEvent(new Event("focusin")); await h.advance(20000);
  assert.equal(h.palette.notice.hidden, false);
  const event = new Event("focusout"); event.relatedTarget = null; h.palette.notice.dispatchEvent(event);
  await h.advance(6000); assert.equal(h.palette.notice.hidden, true);
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

test("/new prepares a local draft, exposes only configuration commands, and survives Escape", async () => {
  const h = setup(); h.palette.toggle(); h.type("/");
  assert.deepEqual(h.palette.slash.matches.map(c => c.name), ["/new"]);
  h.key("Enter");
  assert.equal(h.palette.locked.kind, "new"); assert.equal(h.palette.input.value, "");
  assert.equal(h.palette.settings.hidden, false); assert.equal(h.palette.list.hidden, true);
  assert.equal(h.calls.length, 0);
  h.type("/");
  assert.deepEqual(h.palette.slash.matches.map(c => c.name), ["/new", "/model", "/effort", "/permissions"]);
  h.key("Escape", h.document); assert.equal(h.palette.dialog.open, true); assert.equal(h.palette.menu.hidden, true);
  h.type("Fix the build"); h.key("Escape", h.document); h.palette.toggle();
  assert.equal(h.palette.locked.kind, "new"); assert.equal(h.palette.input.value, "Fix the build");
  await h.tick(); assert.equal(h.calls.length, 0);
});

test("new-agent commands configure provider, model, effort and permissions without sending", async () => {
  const h = setup(); h.palette.toggle(); h.type("/new"); h.key("Enter");
  h.type("/model claude"); await settle();
  assert.equal(h.palette.slash.matches.length, 1); h.key("Enter"); await settle();
  assert.equal(h.palette.input.value, "");
  assert.equal(h.palette.draft.client, "claude"); assert.equal(h.palette.draft.model, "claude-model");
  h.type("/effort hi"); h.key("Enter"); await settle();
  assert.equal(h.palette.draft.effort, "high"); assert.equal(h.palette.input.value, "");
  h.type("/permissions yo"); h.key("Tab");
  assert.equal(h.palette.draft.yolo, true); assert.equal(h.palette.input.value, "");
  assert.match(h.palette.settings.children[2].textContent, /Full access/);
  assert.equal(h.calls.some(c => c.path === "send-work/send" || c.path === "send-work/suggest"), false);
  h.type("Fix the build"); h.key("Enter"); await settle();
  const sent = h.calls.find(c => c.path === "send-work/send").body;
  assert.equal(sent.message, "Fix the build"); assert.deepEqual(sent.destination, {kind: "new"});
  assert.deepEqual(sent.settings, {client: "claude", model: "claude-model", effort: "high", yolo: true});
  assert.deepEqual(h.opened, []); assert.equal(h.palette.dialog.open, false);
  assert.deepEqual(h.palette.draft, {client: "codex", model: null, effort: null, yolo: false});
});

test("clickable settings preserve request text and unsupported commands cannot create conversations", async () => {
  const h = setup(); h.palette.toggle(); h.type("/new"); h.key("Enter");
  h.type("Fix the build"); h.palette.settings.children[2].onclick();
  h.palette.slash.choose(1);
  assert.equal(h.palette.draft.yolo, true); assert.equal(h.palette.input.value, "Fix the build");
  for (const command of ["/fork", "/close", "/effort high"]) {
    h.type(command); await h.palette.send();
    assert.equal(h.palette.error.hidden, false); assert.equal(h.palette.input.value, command);
  }
  assert.equal(h.calls.length, 0);
});

test("manual configuration validates values, preserves message lines and resets effort for a new model", async () => {
  const h = setup(); h.palette.toggle(); h.type("/new\nFix the build"); await h.palette.send();
  assert.equal(h.palette.input.value, "Fix the build");
  h.type("/model available-model high\nFix the build"); await h.palette.send();
  assert.equal(h.palette.draft.effort, "high"); assert.equal(h.palette.input.value, "Fix the build");
  h.type("/effort impossible"); await h.palette.send(); assert.equal(h.palette.error.hidden, false);
  assert.equal(h.palette.draft.effort, "high");
  h.type("/model claude-model"); await h.palette.send();
  assert.equal(h.palette.draft.effort, null); assert.equal(h.palette.draft.client, "claude");
  h.type("/permissions default"); await h.palette.send(); assert.equal(h.palette.draft.yolo, false);
  assert.equal(h.calls.some(c => c.path === "send-work/send"), false);
});

test("configuration before /new stays local and does not load model catalogs", async () => {
  const h = setup(); h.palette.toggle(); h.type("/model available-model"); await h.palette.send();
  assert.equal(h.palette.error.hidden, false); assert.match(h.palette.error.textContent, /\/new/);
  await h.tick(); assert.equal(h.calls.length, 0);
});

test("failed new submissions retain settings and retry identity; changing destination omits settings", async () => {
  const h = setup(async () => { throw new Error("Offline"); });
  h.palette.toggle(); h.type("/new"); h.key("Enter");
  h.type("/permissions yolo"); await h.palette.send();
  h.type("Fix the build"); await h.palette.send(); await h.palette.send();
  assert.equal(h.calls[0].body.requestId, h.calls[1].body.requestId);
  assert.equal(h.palette.draft.yolo, true); assert.equal(h.palette.input.value, "Fix the build");
  h.$("send-work-unlock").onclick(); h.palette.locked = {kind: "thread", id: "theme"};
  await h.palette.send(); assert.equal("settings" in h.calls[2].body, false);
});

test("pinned dispatcher exists only on overview and shares its draft and selection with the popup", () => {
  const h = setup(); h.palette.pin(true);
  assert.equal(h.palette.content.parentElement, h.palette.dock);
  assert.equal(h.palette.dialog.open, undefined);
  assert.equal(h.palette.list.hidden, true);
  assert.equal(h.saved.get("ribbon-field.floating-dispatch"), "true");
  h.type("deploy"); h.key("Tab");
  h.$("welcome").hidden = true; h.palette.syncOverview();
  assert.equal(h.palette.overviewBar.hidden, true); assert.equal(h.palette.dock.hidden, true);
  h.key("K", h.document, {metaKey: true, shiftKey: true});
  assert.equal(h.palette.dialog.open, true);
  assert.equal(h.palette.input.value, "deploy"); assert.equal(h.palette.locked.id, "release");
  h.key("Escape", h.document);
  h.$("welcome").hidden = false; h.palette.syncOverview();
  h.key("K", h.document, {metaKey: true, shiftKey: true});
  assert.equal(h.palette.dialog.open, false); assert.equal(h.document.activeElement, h.palette.input);
  assert.equal(h.palette.input.value, "deploy"); assert.equal(h.palette.locked.id, "release");
  h.palette.pin(false);
  assert.equal(h.palette.dock.hidden, true); h.palette.toggle();
  assert.equal(h.palette.input.value, "deploy");
});

test("workspace switches retain separate drafts and never carry a target across projects", () => {
  const h = setup(); h.palette.pin(true); h.type("deploy"); h.key("Tab");
  h.workspace("/other"); assert.equal(h.palette.input.value, ""); assert.equal(h.palette.locked, null);
  h.type("Other project work");
  h.workspace("/work"); assert.equal(h.palette.input.value, "deploy"); assert.equal(h.palette.locked.id, "release");
  h.workspace("/other"); assert.equal(h.palette.input.value, "Other project work"); assert.equal(h.palette.locked, null);
});

test("pinned send clears only the submitted workspace and stays ready for another request", async () => {
  const h = setup(); h.palette.pin(true); h.type("deploy"); await h.palette.send();
  assert.equal(h.palette.docked, true); assert.equal(h.palette.input.value, "");
  assert.equal(h.document.activeElement, h.palette.input); assert.equal(h.palette.list.hidden, true);
  let resolve;
  const delayed = setup(() => new Promise(done => { resolve = done; }));
  delayed.palette.pin(true); delayed.type("deploy"); const sending = delayed.palette.send();
  delayed.workspace("/other"); delayed.type("Keep this draft");
  resolve({receipt: {id: 1, state: "accepted", target_id: "release"}}); await sending;
  assert.equal(delayed.palette.input.value, "Keep this draft");
  delayed.workspace("/work"); assert.equal(delayed.palette.input.value, "");
});

test("dispatcher settings are saved independently of the new-conversation draft and message", async () => {
  const h = setup(); h.palette.pin(true); h.$("send-work-new").onclick(); h.type("Fix the build");
  h.palette.draft.yolo = true;
  await h.palette.openProfile();
  assert.equal(h.$("send-work-profile").hidden, false);
  h.$("send-work-profile-permissions").value = "default";
  h.$("send-work-profile-effort").value = "high";
  await h.palette.saveProfile();
  const save = h.calls.find(c => c.path === "send-work/dispatcher-settings" && c.body.settings);
  assert.deepEqual(save.body.settings, {client: "codex", model: "available-model", effort: "high", yolo: false});
  assert.equal(h.palette.draft.yolo, true); assert.equal(h.palette.draft.model, null);
  assert.equal(h.palette.input.value, "Fix the build"); assert.equal(h.palette.locked.kind, "new");
  assert.equal(h.$("send-work-profile").hidden, true);
});

test("late dispatcher settings cannot populate another project or reopen a closed panel", async () => {
  let resolve;
  const h = setup(() => new Promise(done => { resolve = done; }));
  h.palette.pin(true); const loading = h.palette.openProfile(); h.workspace("/other");
  resolve({settings: {client: "claude", model: "other", effort: null, yolo: true}}); await loading;
  assert.equal(h.$("send-work-profile").hidden, true); assert.equal(h.palette.profile, undefined);
  assert.equal(h.calls.length, 1);
});

test("overview refresh does not reset keyboard destination selection", () => {
  const h = setup(); h.palette.pin(true); h.type("deploy"); h.key("ArrowDown");
  const chosen = h.palette.locked;
  h.palette.syncOverview();
  assert.equal(h.palette.locked, chosen); assert.equal(h.palette.rows[h.palette.active], chosen);
});
