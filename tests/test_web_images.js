"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const ChatImageAttachments = require("../plugins/agent-coord/scripts/agent_coord/web/image-attachments.js");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const url = "data:image/png;base64,iVBORw0KGgo=";
const file = (name = "screenshot.png", extra = {}) => ({name, type: "image/png", size: 100, ...extra});

function element(tag, text, className) {
  return {tag, textContent: text, className, childNodes: [], hidden: false, dataset: {},
    append(...children) { this.childNodes.push(...children); },
    replaceChildren(...children) { this.childNodes = children.flatMap(child => child.tag === "fragment" ? child.childNodes : [child]); },
    setAttribute(name, value) { this[name] = value; }, querySelectorAll() { return []; },
    contains(target) { return target === this || this.childNodes.some(child => child.contains?.(target)); },
    classList: {toggle() {}}, scrollHeight: 100, scrollTop: 0, clientHeight: 100};
}
function setup(readFile = async () => url) {
  const elements = new Map(), listeners = new Map(), errors = [];
  const c = {selected: "one", enabled: true, changes: 0, errors, listeners,
    $: id => { if (!elements.has(id)) elements.set(id, element("div")); return elements.get(id); }};
  c.document = {createElement: element, createDocumentFragment: () => element("fragment"), getElementById: c.$,
    addEventListener(type, callback) { listeners.set(type, callback); }};
  c.attachments = new ChatImageAttachments({document: c.document, getThread: () => c.selected,
    canAttach: () => c.enabled, onChange: () => { c.changes++; }, onError: error => errors.push(error.message), readFile});
  return c;
}

test("dropped images stay in their original draft when reading finishes after switching threads", async () => {
  let finish;
  const c = setup(() => new Promise(resolve => { finish = resolve; }));
  const reading = c.attachments.add([file()]);
  assert.equal(c.attachments.pending(), true);
  await Promise.resolve();
  c.selected = "two";
  finish(url); await reading;
  assert.equal(c.attachments.items().length, 0);
  c.selected = "one";
  assert.equal(c.attachments.pending(), false);
  assert.equal(c.attachments.snapshot()[0].url, url);
  c.attachments.render();
  const card = c.$("image-attachments").childNodes[0];
  assert.equal(card.childNodes[0].src, url);
  assert.equal(card.childNodes[2]["aria-label"], "Remove screenshot.png");
  card.childNodes[2].onclick();
  assert.equal(c.attachments.items().length, 0);
});

test("validation rejects unsupported, empty and oversized files and bounds concurrent reads", async () => {
  const c = setup();
  await c.attachments.add([file("bad.svg", {type: "image/svg+xml"}), file("empty.png", {size: 0}), file("large.png", {size: 5 * 1024 * 1024 + 1})]);
  assert.equal(c.attachments.items().length, 0);
  assert.match(c.errors[0], /PNG, JPEG, WebP, or GIF/);
  assert.match(c.errors[0], /5 MiB/);
  await Promise.all([c.attachments.add([file(), file(), file()]), c.attachments.add([file(), file()])]);
  assert.equal(c.attachments.items().length, 4);
  assert.match(c.errors.at(-1), /at most 4/);
});

test("missing browser MIME types use extensions and read errors do not leave a pending attachment", async () => {
  const c = setup(async file => { if (file.name === "broken.png") throw new Error("Read failed"); return url; });
  await c.attachments.add([file("Screenshot.PNG", {type: ""}), file("broken.png")]);
  assert.equal(c.attachments.items().length, 1);
  assert.equal(c.attachments.snapshot()[0].url, url);
  assert.equal(c.attachments.pending(), false);
  assert.deepEqual(c.errors, ["Read failed"]);
});

test("browser-generated filenames from data URLs get a readable attachment name", async () => {
  const c = setup();
  await c.attachments.add([file("data".repeat(100))]);
  assert.equal(c.attachments.items()[0].name, "Image.png");
});

test("a successful send removes only its snapshot and preserves later attachments and other drafts", async () => {
  const c = setup();
  await c.attachments.add([file("old.png")]);
  const sent = c.attachments.snapshot();
  await c.attachments.add([file("new.png")]);
  c.selected = "two";
  await c.attachments.add([file("other.png")]);
  c.attachments.sent("one", sent);
  assert.deepEqual(c.attachments.items("one").map(x => x.name), ["new.png"]);
  assert.deepEqual(c.attachments.items("two").map(x => x.name), ["other.png"]);
});

test("file drops prevent browser navigation and attach only inside an enabled chat", async () => {
  const c = setup(); c.attachments.bind();
  let prevented = 0;
  const event = {target: c.$("conversation"), dataTransfer: {types: ["Files"], files: [file()]}, preventDefault() { prevented++; }};
  c.listeners.get("dragover")(event);
  assert.equal(event.dataTransfer.dropEffect, "copy");
  assert.equal(c.$("image-drop-hint").hidden, false);
  c.listeners.get("dragleave")({relatedTarget: null});
  assert.equal(c.$("image-drop-hint").hidden, true);
  c.listeners.get("drop")(event);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(c.attachments.items().length, 1);
  assert.equal(c.$("image-drop-hint").hidden, true);
  c.enabled = false;
  c.listeners.get("drop")(event);
  c.enabled = true; event.target = element("outside");
  c.listeners.get("dragover")(event);
  assert.equal(event.dataTransfer.dropEffect, "none");
  c.listeners.get("drop")(event);
  assert.equal(c.attachments.items().length, 1);
  assert.equal(prevented, 5);
  c.listeners.get("drop")({dataTransfer: {types: ["text/plain"]}, preventDefault() { assert.fail("Text drop intercepted"); }});
});

function appSetup() {
  const c = setup();
  c.state = {selected: "one", sessions: [], closing: new Set(), commandFeedback: new Map(), drafts: new Map(), attachments: c.attachments,
    detail: {running: false, activeTurn: null, work_thread: {browser_session: true, attention: "now"}, thread: {turns: []}}};
  c.attachments.getThread = () => c.state.selected;
  Object.assign(c, {ChatImageAttachments, node: element, renderSessionSettings() {},
    threadGrouping: {status: () => ({label: "Idle"})}, messageMarkdown: {render: text => text},
    refreshDetail: async () => {}, refreshList: async () => {}, sessionPath: id => "sessions/" + id});
  const disclosureEntry = {};
  c.conversationEntry = () => disclosureEntry;
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function isSessionDraft()"), source.indexOf("function modelValue(")), c);
  vm.runInContext(source.slice(source.indexOf("function renderStatus("), source.indexOf("function requestButton(")), c);
  vm.runInContext(source.slice(source.indexOf("async function sendMessage("), source.indexOf('$("composer").onsubmit')), c);
  vm.runInContext(source.slice(source.indexOf("function composerKeydown("), source.indexOf('$("message").onkeydown')), c);
  c.$("message").value = "";
  return c;
}

test("image-only send, steering, and queue use attachment inputs and clear successful drafts", async () => {
  for (const mode of ["send", "steer", "queue"]) {
    const c = appSetup();
    c.state.detail.running = mode !== "send"; c.state.detail.activeTurn = mode === "send" ? null : "active";
    await c.attachments.add([file()]);
    let called = false;
    c.api = async (path, body) => {
      called = true;
      assert.equal(path, "sessions/one/" + (mode === "queue" ? "queue" : "messages"));
      assert.equal(body.images[0].url, url);
      assert.equal(body.message, "");
      assert.equal(body.expectedTurnId, mode === "steer" ? "active" : undefined);
      return {};
    };
    await c.sendMessage(mode);
    assert.equal(called, true, mode);
    assert.equal(c.attachments.items().length, 0);
  }
});

test("failed sends keep images and text available for retry; reads block incomplete submissions", async () => {
  const c = appSetup(); await c.attachments.add([file()]);
  c.$("message").value = "Review this screenshot";
  c.api = async () => { throw new Error("Send failed"); };
  await assert.rejects(c.sendMessage(), /Send failed/);
  assert.equal(c.attachments.items().length, 1);
  assert.equal(c.$("message").value, "Review this screenshot");
  c.attachments.items()[0].url = null;
  c.api = async () => assert.fail("Partial draft sent");
  c.renderStatus();
  assert.equal(c.$("send").disabled, true);
  await c.sendMessage();
});

test("image-only Tab queues while running and preserves images added during the request", async () => {
  const c = appSetup(); await c.attachments.add([file("sent.png")]);
  c.state.detail.running = true; c.state.detail.activeTurn = "active";
  c.renderStatus();
  let operation, prevented = false;
  c.action = callback => { operation = callback(); };
  c.api = async () => { await c.attachments.add([file("next.png")]); return {}; };
  c.composerKeydown({key: "Tab", preventDefault() { prevented = true; }});
  await operation;
  assert.equal(prevented, true);
  assert.deepEqual(c.attachments.items().map(x => x.name), ["next.png"]);
});

test("history and queued messages render image thumbnails without loading remote or SVG URLs", () => {
  const c = appSetup();
  c.state.detail.thread.turns = [{items: [{type: "userMessage", content: [
    {type: "text", text: "Compare"}, {type: "image", url},
    {type: "image", url: "https://example.com/private.png"}, {type: "image", url: "data:image/svg+xml;base64,PHN2Zz4="},
  ]}]}];
  c.renderTimeline();
  const body = c.$("timeline").childNodes[0].childNodes[0];
  assert.match(body.textContent, /Compare/);
  const gallery = body.childNodes[0];
  assert.equal(gallery.childNodes.length, 1);
  assert.equal(gallery.childNodes[0].src, url);
  c.state.detail.queuedMessages = [{id: "q", message: "", images: [{name: "shot.png", url}], state: "queued"}];
  c.renderQueuedMessages();
  const queued = c.$("queued-messages").childNodes[1];
  assert.equal(queued.childNodes[1].childNodes[0].src, url);
});

test("mobile chooser reuses attachment validation, supports reselection, and ignores a stale thread", async () => {
  const c = setup(); c.attachments.bind();
  const picker = c.$("image-picker"), button = c.$("attach-image");
  let clicks = 0; picker.click = () => { clicks++; };
  button.onclick(); assert.equal(clicks, 1);
  picker.files = [file("photo.png")]; picker.value = "photo.png"; picker.onchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(c.attachments.snapshot()[0].name, "photo.png");
  assert.equal(picker.value, "");
  c.attachments.sent("one", c.attachments.snapshot());
  button.onclick(); picker.onchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(c.attachments.snapshot().length, 1);
  button.onclick(); c.selected = "two"; picker.onchange();
  assert.equal(c.attachments.items().length, 0);
  c.enabled = false; c.attachments.render(); button.onclick();
  assert.equal(button.disabled, true); assert.equal(clicks, 3);
});
