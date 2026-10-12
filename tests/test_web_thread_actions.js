"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const createThreadActions = require("../plugins/agent-coord/scripts/agent_coord/web/thread-actions.js");

function setup(reduced = false) {
  const handlers = {}, timers = new Map(), animations = [];
  let now = 0, nextTimer = 0;
  function advance(milliseconds) {
    const until = now + milliseconds;
    while (true) {
      const next = [...timers].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      const [id, timer] = next; now = timer.at; timers.delete(id); timer.run();
    }
    now = until;
  }
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.style = {}; this.dataset = {}; this.isConnected = true; this.classes = new Set(); this.classList = {add: key => this.classes.add(key), remove: key => this.classes.delete(key)}; }
    append(...items) { for (const item of items) { this.children.push(item); item.parent = this; } }
    addEventListener(name, callback) { (this.listeners ||= {})[name] = callback; }
    emit(name, event = {}) { this.listeners?.[name]?.(event); }
    setAttribute(key, value) { this.attributes[key] = value; }
    removeAttribute(key) { delete this.attributes[key]; }
    remove() { this.isConnected = false; if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
    contains(item) { return this === item || this.children.some(child => child.contains(item)); }
    focus() { doc.activeElement = this; }
    get firstChild() { return this.children[0]; }
    getBoundingClientRect() { return {left: 200, top: 200, right: 228, bottom: 228, width: 180, height: 90}; }
    querySelectorAll() { return []; }
    cloneNode() { return new Element(this.tagName); }
    animate(frames, timing) { const result = {frames, timing, finished: new Promise(() => {}), cancel() { this.cancelled = true; }}; animations.push(result); return result; }
  }
  const body = new Element("body"), tab = new Element("button"); tab.id = "show-closed"; body.append(tab);
  const doc = {body, createElement: tag => new Element(tag),
    getElementById: id => body.children.find(item => item.id === id),
    querySelectorAll: () => [], addEventListener: (name, fn) => { handlers["doc:" + name] = fn; }};
  const win = {innerWidth: 390, innerHeight: 500, matchMedia: () => ({matches: reduced}),
    setTimeout: (run, delay) => { const id = ++nextTimer; timers.set(id, {run, at: now + delay}); return id; },
    clearTimeout: id => timers.delete(id),
    addEventListener: (name, fn) => { handlers[name] = fn; }};
  return {ui: createThreadActions(doc, win), doc, handlers, timers, advance, animations, tab, Element};
}
const event = key => ({key, preventDefault() {}, stopPropagation() {}});

test("overflow menu opens without opening the card, supports keyboard navigation and restores focus", () => {
  const {ui, doc, handlers} = setup(); let called = 0;
  const trigger = ui.menu("HPA", [{label: "Move to Later…", run: () => called++}, {label: "Move to Now", run() {}}]);
  doc.body.append(trigger); trigger.onclick(event());
  const panel = doc.body.children.find(item => item.className === "thread-action-menu");
  assert.equal(trigger.attributes["aria-expanded"], "true");
  assert.equal(doc.activeElement, panel.firstChild);
  panel.onkeydown(event("ArrowDown")); assert.equal(doc.activeElement, panel.children[1]);
  panel.onkeydown(event("Home")); assert.equal(doc.activeElement, panel.firstChild);
  handlers["doc:keydown"](event("Escape")); assert.equal(doc.activeElement, trigger);
  assert.equal(trigger.attributes["aria-expanded"], "false");
  trigger.onclick(event()); doc.activeElement.onclick(event()); assert.equal(called, 1);
  assert.equal(trigger.attributes["aria-expanded"], "false");
});

test("close notices retain independent Undo actions and a failed undo remains retryable", async () => {
  const {ui, doc, timers, advance} = setup(); let failures = true, restored = 0;
  ui.notify("one", "First closed", async () => { if (failures) throw new Error("Offline"); restored++; });
  ui.notify("two", "Second closed", async () => { restored++; });
  advance(9000);
  const region = doc.getElementById("thread-feedback"), first = region.children[0], second = region.children[1];
  await first.children[1].onclick();
  assert.equal(first.children[1].disabled, false);
  assert.match(first.firstChild.textContent, /Offline.*Undo again/);
  await second.children[1].onclick(); assert.equal(region.children.length, 1);
  failures = false; await first.children[1].onclick();
  assert.equal(region.children.length, 0); assert.equal(restored, 2);
  assert.equal(timers.size, 0);
});

test("close notifications expire independently after a usable Undo interval", () => {
  const {ui, doc, advance} = setup();
  const undo = () => assert.fail("expiration must not undo the close");
  ui.notify("one", "First closed", undo);
  const region = doc.getElementById("thread-feedback");
  advance(4000); ui.notify("two", "Second closed", undo);
  advance(5999); assert.equal(region.children.length, 2);
  advance(1); assert.equal(region.children.length, 1);
  assert.equal(region.firstChild.firstChild.textContent, "Second closed");
  advance(4000); assert.equal(region.children.length, 0);
});

test("hover and keyboard focus pause Undo expiration until interaction ends", () => {
  const {ui, doc, advance, tab} = setup();
  ui.notify("one", "Closed", () => {});
  const region = doc.getElementById("thread-feedback"), notice = region.firstChild;
  advance(9000); notice.emit("pointerenter"); advance(20000);
  assert.equal(region.children.length, 1);
  notice.emit("focusin"); notice.emit("pointerleave"); advance(20000);
  assert.equal(region.children.length, 1);
  notice.emit("focusout", {relatedTarget: notice.children[1]}); advance(20000);
  assert.equal(region.children.length, 1);
  notice.emit("focusout", {relatedTarget: tab}); advance(9999);
  assert.equal(region.children.length, 1);
  advance(1); assert.equal(region.children.length, 0);
});

test("an in-flight Undo survives its deadline and a failure gets a fresh retry interval", async () => {
  const {ui, doc, advance} = setup(); let rejectUndo;
  ui.notify("one", "Closed", () => new Promise((_, reject) => { rejectUndo = reject; }));
  const region = doc.getElementById("thread-feedback"), notice = region.firstChild, undo = notice.children[1];
  advance(9000); const pending = undo.onclick(); advance(20000);
  assert.equal(region.children.length, 1); assert.equal(undo.disabled, true);
  rejectUndo(new Error("Offline")); await pending;
  assert.equal(undo.disabled, false); assert.match(notice.firstChild.textContent, /Try Undo again/);
  advance(9999); assert.equal(region.children.length, 1);
  advance(1); assert.equal(region.children.length, 0);
});

test("replacing a notice cancels its timer and a late Undo result cannot affect the replacement", async () => {
  const {ui, doc, advance, timers} = setup(); let rejectUndo;
  ui.notify("one", "Original", () => new Promise((_, reject) => { rejectUndo = reject; }));
  const region = doc.getElementById("thread-feedback"), pending = region.firstChild.children[1].onclick();
  ui.notify("one", "Replacement", () => {});
  rejectUndo(new Error("Offline")); await pending;
  assert.equal(timers.size, 1);
  advance(9000); ui.notify("one", "Latest", () => {});
  assert.equal(region.children.length, 1); assert.equal(region.firstChild.firstChild.textContent, "Latest");
  advance(1000); assert.equal(region.children.length, 1);
  advance(9000); assert.equal(region.children.length, 0); assert.equal(timers.size, 0);
});

test("close feedback indicates Closed without motion under reduced-motion preferences", () => {
  const {ui, tab, animations, Element} = setup(true);
  ui.closed({copy: new Element("div"), rect: {left: 0, top: 0, width: 100, height: 100}});
  assert.equal(tab.classes.has("thread-destination"), true);
  assert.equal(animations.length, 0);
});

test("close animation heads toward Closed and is removed on viewport changes", () => {
  const {ui, tab, animations, Element, handlers} = setup();
  const copy = new Element("div");
  ui.closed({copy, rect: {left: 0, top: 0, width: 100, height: 100}});
  assert.equal(tab.classes.has("thread-destination"), true);
  assert.equal(copy.inert, true); assert.equal(copy.attributes["aria-hidden"], "true");
  assert.match(animations[0].frames[1].transform, /scale\(\.08\)/);
  handlers.resize(); assert.equal(copy.isConnected, false); assert.equal(animations[0].cancelled, true);
});
