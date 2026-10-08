"use strict";
const assert = require("node:assert/strict");
const {test} = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/theme.js"), "utf8");

function fixture({saved = null, dark = false, blocked = false} = {}) {
  const events = {}, documentEvents = {}, controlEvents = {}, values = new Map();
  if (saved !== null) values.set("agent-coord.theme", saved);
  const select = {value: "system", addEventListener: (name, fn) => { controlEvents[name] = fn; }};
  let ready = false, mediaChange;
  const media = {matches: dark, addEventListener: (name, fn) => { mediaChange = fn; }};
  const storage = {
    getItem(key) { if (blocked) throw Error("denied"); return values.get(key) ?? null; },
    setItem(key, value) { if (blocked) throw Error("denied"); values.set(key, value); },
  };
  const document = {
    documentElement: {dataset: {}, style: {}},
    querySelectorAll: () => ready ? [select] : [],
    addEventListener: (name, fn) => { documentEvents[name] = fn; },
  };
  vm.runInNewContext(source, {document, window: {
    localStorage: storage, matchMedia: () => media,
    addEventListener: (name, fn) => { events[name] = fn; },
  }});
  return {
    document, select, values, storage,
    ready() { ready = true; documentEvents.DOMContentLoaded(); },
    choose(value) { select.value = value; controlEvents.change(); },
    system(dark) { media.matches = dark; mediaChange(); },
    event(event) { events.storage(event); },
    theme() { return document.documentElement.dataset.theme; },
  };
}

test("system theme applies before DOM ready and tracks OS changes", () => {
  const page = fixture({dark: true});
  assert.equal(page.theme(), "dark");
  assert.equal(page.document.documentElement.style.colorScheme, "dark");
  page.ready();
  assert.equal(page.select.value, "system");
  page.system(false);
  assert.equal(page.theme(), "light");
});

test("explicit choice persists on reload and overrides the OS until System is chosen", () => {
  const page = fixture({saved: "light", dark: true});
  assert.equal(page.theme(), "light");
  page.ready();
  page.choose("dark");
  page.system(false);
  assert.equal(page.theme(), "dark");
  assert.equal(fixture({saved: page.values.get("agent-coord.theme")}).theme(), "dark");
  page.choose("system");
  assert.equal(page.theme(), "light");
  page.system(true);
  assert.equal(page.theme(), "dark");
});

test("storage events synchronize open pages including reset and invalid preferences", () => {
  const page = fixture({dark: true});
  page.ready();
  page.event({key: "agent-coord.theme", newValue: "light", storageArea: page.storage});
  assert.equal(page.theme(), "light");
  assert.equal(page.select.value, "light");
  page.event({key: "unrelated", newValue: "dark"});
  page.event({key: "agent-coord.theme", newValue: "dark", storageArea: {}});
  assert.equal(page.theme(), "light");
  page.event({key: null, newValue: null, storageArea: page.storage});
  assert.equal(page.theme(), "dark");
  assert.equal(page.select.value, "system");
  page.event({key: "agent-coord.theme", newValue: "invalid"});
  assert.equal(page.select.value, "system");
});

test("unavailable storage and invalid saved values do not prevent theme selection", () => {
  assert.equal(fixture({saved: "invalid", dark: true}).theme(), "dark");
  const page = fixture({blocked: true});
  page.ready();
  page.choose("dark");
  assert.equal(page.theme(), "dark");
  page.choose("light");
  assert.equal(page.theme(), "light");
});
