"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const createTransientNotice = require("../plugins/agent-coord/scripts/agent_coord/web/transient-notice.js");

function fixture() {
  const timers = new Map(); let now = 0, id = 0;
  const view = {
    setTimeout(run, delay) { timers.set(++id, {run, at: now + delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const element = new EventTarget(), link = {}, button = {}, copy = {textContent: ""};
  Object.assign(element, {hidden: true, ownerDocument: {}, contains: target => target === link || target === button,
    querySelector: selector => selector === "span" ? copy : button});
  const emit = (type, properties = {}) => {
    const event = new Event(type); Object.assign(event, properties); element.dispatchEvent(event);
  };
  const advance = delay => {
    const end = now + delay;
    for (;;) {
      const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      now = next[1].at; timers.delete(next[0]); next[1].run();
    }
    now = end;
  };
  return {element, view, timers, emit, advance, link, button, copy};
}

test("popups expire after six seconds and replacement content gets its full interval", () => {
  const h = fixture(), notice = createTransientNotice(h.element, h.view);
  notice.show(); h.advance(5999); assert.equal(h.element.hidden, false);
  h.advance(1); assert.equal(h.element.hidden, true); assert.equal(h.timers.size, 0);
  notice.show(10000); h.advance(9000); notice.show(10000); h.advance(1000);
  assert.equal(h.element.hidden, false);
  h.advance(9000); assert.equal(h.element.hidden, true);
});

test("hover and keyboard interaction pause expiry and leaving gives time to read", () => {
  const h = fixture(), notice = createTransientNotice(h.element, h.view);
  notice.show(); h.advance(5000); h.emit("pointerenter", {pointerType: "mouse"}); h.advance(20000);
  assert.equal(h.element.hidden, false);
  h.emit("focusin"); h.emit("pointerleave"); h.advance(20000);
  h.emit("focusout", {relatedTarget: h.link}); h.advance(20000);
  assert.equal(h.element.hidden, false);
  h.emit("focusout", {relatedTarget: null}); h.advance(5999);
  assert.equal(h.element.hidden, false); h.advance(1); assert.equal(h.element.hidden, true);
});

test("touch pointers do not leave popups indefinitely paused", () => {
  const h = fixture(), notice = createTransientNotice(h.element, h.view);
  notice.show(); h.emit("pointerenter", {pointerType: "touch"}); h.advance(6000);
  assert.equal(h.element.hidden, true);
});

test("manual dismissal clears timers so an old deadline cannot hide a new popup", () => {
  const h = fixture(), notice = createTransientNotice(h.element, h.view);
  notice.show(); h.advance(5000); notice.dismiss();
  assert.equal(h.element.hidden, true); assert.equal(h.timers.size, 0);
  notice.show(); h.advance(1000); assert.equal(h.element.hidden, false);
  h.advance(5000); assert.equal(h.element.hidden, true);
});

test("global errors expire without repeated background errors extending their lifetime", () => {
  const h = fixture(); let dialogError = null;
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const context = vm.createContext({createTransientNotice, window: h.view, $: () => h.element,
    document: {querySelector: () => dialogError}});
  vm.runInContext(source.slice(source.indexOf("const errorNotice ="), source.indexOf("async function api(")), context);
  context.showError(new Error("Offline")); h.advance(9000); context.showError(new Error("Offline"));
  h.advance(1000); assert.equal(h.element.hidden, true);
  context.showError(new Error("Different error")); h.advance(9000);
  assert.equal(h.element.hidden, false); h.button.onclick(); assert.equal(h.timers.size, 0);
  dialogError = {hidden: true}; context.showError(new Error("Choose a folder")); h.advance(20000);
  assert.equal(dialogError.hidden, false); assert.equal(dialogError.textContent, "Choose a folder");
  assert.equal(h.element.hidden, true);
});
