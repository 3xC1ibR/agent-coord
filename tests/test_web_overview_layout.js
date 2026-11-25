"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const {OverviewLayout} = require("../plugins/agent-coord/scripts/agent_coord/web/overview-layout.js");
function setup({saved, narrow = false, blocked = false, brokenStorage = false} = {}) {
  const events = {}, calls = [], values = new Map(saved ? [["agent-coord.overview-layout", saved]] : []);
  const doc = {activeElement: null, addEventListener: (type, fn) => events["doc:" + type] = fn};
  class Element {
    constructor() { this.children = []; this.dataset = {}; this.attrs = {}; this.style = {}; this.isConnected = true; }
    append(...items) { this.children.push(...items); for (const item of items) if (typeof item === "object") item.parent = this; }
    setAttribute(k, v) { this.attrs[k] = v; }
    contains(item) { return this === item || this.children.some(c => typeof c === "object" && c.contains(item)); }
    remove() { this.isConnected = false; if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); }
    focus() { doc.activeElement = this; }
    getBoundingClientRect() { return {left: 100, bottom: 80, width: 160, height: 100}; }
  }
  doc.createElement = () => new Element(); doc.body = new Element();
  const media = {matches: narrow, addEventListener: (_, fn) => media.changed = fn};
  const view = {matchMedia: () => media, innerWidth: 1200, innerHeight: 800, addEventListener: (type, fn) => events[type] = fn};
  const storage = {getItem(k) { if (brokenStorage) throw Error(); return values.get(k); }, setItem(k, v) { if (brokenStorage) throw Error(); values.set(k, v); }};
  const ui = new OverviewLayout({document: doc, window: view, storage, canNavigate: () => !blocked, onChange: (...args) => calls.push(args)});
  const trigger = new Element(); doc.body.append(trigger);
  return {ui, doc, events, calls, values, media, trigger};
}

test("List defaults, legacy cards migration, invalid preferences, explicit Atlas choice, and reload", () => {
  assert.equal(setup().ui.mode, "list");
  assert.equal(setup({saved: "unknown"}).ui.mode, "list");
  const migrated = setup({saved: "cards"});
  assert.equal(migrated.ui.mode, "list");
  assert.equal(migrated.values.get(migrated.ui.key), "list");
  assert.equal(migrated.ui.choose("cards"), false);
  const c = setup();
  assert.equal(c.ui.choose("atlas"), true);
  assert.equal(c.ui.mode, "atlas");
  assert.equal(c.values.get(c.ui.key), "atlas");
  assert.deepEqual(c.calls, [["atlas", {navigate: true}]]);
  assert.equal(setup({saved: "atlas"}).ui.mode, "atlas");
});

test("the menu exposes List, Focus, Atlas in exact order with checked radio state and keyboard navigation", () => {
  const c = setup(); let prevented = 0; c.ui.open(c.trigger, {preventDefault() { prevented++; }});
  assert.equal(prevented, 1); const panel = c.ui.menu;
  assert.deepEqual(panel.children.map(btn => [btn.dataset.layout, btn.children[1]]), [
    ["list", "List"],
    ["working-set", "Focus"],
    ["atlas", "Atlas"],
  ]);
  assert.equal(panel.children[0].attrs["aria-checked"], "true");
  assert.equal(panel.children[1].attrs["aria-checked"], "false");
  assert.equal(panel.children[2].attrs["aria-checked"], "false");
  assert.equal(c.doc.activeElement, panel.children[0]);
  panel.onkeydown({key: "ArrowDown", preventDefault() {}});
  assert.equal(c.doc.activeElement, panel.children[1]);
  c.events["doc:keydown"]({key: "Escape", preventDefault() {}, stopPropagation() {}});
  assert.equal(c.ui.menu, null);
  assert.equal(c.doc.activeElement, c.trigger);
  assert.equal(c.calls.length, 0);
});

test("selection switches immediately, closes the menu and marks the remembered choice", () => {
  const c = setup(); c.ui.open(c.trigger); c.ui.menu.children[2].onclick();
  assert.equal(c.ui.menu, null); assert.equal(c.ui.mode, "atlas");
  c.ui.open(c.trigger); assert.equal(c.ui.menu.children[2].attrs["aria-checked"], "true");
  c.ui.menu.children[0].onclick(); assert.equal(c.ui.mode, "list");
});

test("outside clicks and scrolling dismiss without changing the preference", () => {
  const c = setup({saved: "atlas"}); c.ui.open(c.trigger);
  c.events["doc:pointerdown"]({target: c.doc.body}); assert.equal(c.ui.menu, null);
  c.ui.open(c.trigger); c.events.scroll(); assert.equal(c.ui.menu, null);
  assert.equal(c.ui.mode, "atlas"); assert.equal(c.calls.length, 0);
});

test("desktop Atlas falls back to List on narrow screens and restores when widened", () => {
  const c = setup({saved: "atlas", narrow: true}); assert.equal(c.ui.mode, "list");
  c.ui.open(c.trigger);
  assert.equal(c.ui.menu.children[2].dataset.layout, "atlas");
  assert.equal(c.ui.menu.children[2].disabled, true);
  assert.equal(c.ui.choose("atlas"), false);
  c.media.matches = false; c.media.changed();
  assert.equal(c.ui.mode, "atlas");
  assert.deepEqual(c.calls, [["atlas", {navigate: false}]]);
  const blocked = setup({blocked: true});
  blocked.ui.open(blocked.trigger);
  assert.equal(blocked.ui.menu, undefined);
  assert.equal(blocked.ui.choose("atlas"), false);
});

test("unavailable storage does not prevent switching", () => {
  const c = setup({brokenStorage: true});
  assert.equal(c.ui.mode, "list");
  c.ui.choose("atlas");
  assert.equal(c.ui.mode, "atlas");
});

test("Focus and List are available and remembered across desktop and phones", () => {
  const c = setup(); c.ui.open(c.trigger);
  assert.equal(c.ui.menu.children[1].dataset.layout, "working-set");
  c.ui.menu.children[1].onclick();
  assert.equal(c.ui.mode, "working-set");
  assert.equal(c.values.get(c.ui.key), "working-set");
  assert.equal(setup({saved: "working-set"}).ui.mode, "working-set");
  const phone = setup({saved: "working-set", narrow: true});
  assert.equal(phone.ui.mode, "working-set");
  phone.ui.open(phone.trigger);
  assert.equal(phone.ui.menu.children[0].disabled, false);
  assert.equal(phone.ui.menu.children[1].disabled, false);
  phone.ui.choose("list"); assert.equal(phone.ui.mode, "list");
  phone.ui.choose("working-set"); assert.equal(phone.ui.mode, "working-set");
});
