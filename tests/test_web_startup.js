"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const {loadWebModule} = require("./web_application_fixture.js");

const html = readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/index.html"), "utf8");
const source = html.match(/<script id="workspace-startup">([\s\S]*?)<\/script>/)[1];
const entries = [...html.matchAll(/<script id="workspace-program" src="([^"]+)" defer><\/script>/g)];

function startup() {
  const inserted = [], timers = [], status = {}, message = {}, events = {}, requests = [];
  const notice = {hidden: true, querySelector: () => message};
  const script = () => ({remove() { this.removed = true; }});
  const initial = Object.assign(script(), {id: "workspace-program", src: entries[0][1]});
  const environment = {
    fetch(path, options) { requests.push({path, ...options}); return Promise.resolve({ok: true}); },
    setTimeout(callback, delay) { timers.push({callback, delay}); },
    document: {
      documentElement: {dataset: status},
      addEventListener(type, listener, capture) { assert.equal(capture, true); events[type] = listener; },
      getElementById: id => { assert.equal(id, "error"); return notice; },
      createElement: tag => { assert.equal(tag, "script"); return script(); },
      head: {append(script) { inserted.push(script); }},
    },
  };
  vm.runInNewContext(source, environment);
  const current = () => inserted.at(-1) || initial;
  return {inserted, timers, status, notice, message, current, requests, configuration: environment.agentCoordInitialConfig,
    emit(type, target = current()) { events[type]({target}); }};
}

test("workspace defers one program while starting its independent configuration request", async () => {
  assert.deepEqual(entries.map(match => match[1]), ["/dist/app.js"]);
  assert.match(html, /<link rel="stylesheet" href="\/dist\/app.css">/);
  assert.doesNotMatch(html, /data-startup/);
  const page = startup();
  assert.equal(page.status.startup, "loading");
  assert.deepEqual(page.requests, [{path: "/api/browser/config", cache: "no-store", priority: "high"}]);
  assert.equal((await page.configuration).ok, true);
  page.emit("load", {id: "some-other-resource"});
  assert.equal(page.status.startup, "loading");
  page.emit("load");
  assert.equal(page.status.startup, "loaded");
  assert.deepEqual(page.timers, []);
  assert.equal(page.inserted.length, 0);
});

test("a failed bundle retries the same artifact and can initialize on retry", () => {
  const page = startup();
  const failed = page.current();
  page.emit("error");
  assert(failed.removed);
  assert.equal(page.notice.hidden, true);
  const next = page.timers.shift();
  assert(next.delay > 0);
  next.callback();
  assert.equal(page.current().src, failed.src);
  page.emit("load");
  assert.equal(page.status.startup, "loaded");
  assert.equal(page.inserted.length, 1);
  assert.deepEqual(page.timers, []);
  assert.equal(page.requests.length, 1);
});

test("unopened dialog documents stay inert and each first use retains its native frame", () => {
  const ids = ["settings-frame", "usage-frame", "activity-frame", "html-preview-window"];
  const frames = new Map(), templates = new Map();
  for (const id of ids) {
    assert.match(html, new RegExp(`<template id="${id}-template"><iframe id="${id}"[^>]*></iframe></template>`));
    templates.set(id + "-template", {
      content: {firstElementChild: {id}},
      replaceWith(frame) { frames.set(frame.id, frame); templates.delete(id + "-template"); },
    });
  }
  const context = vm.createContext({crypto: {randomUUID() {}},
    document: {getElementById: id => frames.get(id) || templates.get(id)}});
  loadWebModule(context, "browser-compatibility.js");
  assert.equal(frames.size, 0);
  for (const id of ids) {
    const frame = templates.get(id + "-template").content.firstElementChild;
    assert.equal(context.workspaceDialogFrame(context.document, id), frame);
    assert.equal(context.workspaceDialogFrame(context.document, id), frame);
    assert.equal(templates.has(id + "-template"), false);
  }
});

test("permanent failure stops retrying and exposes actionable startup feedback", () => {
  const page = startup();
  for (let attempt = 0; attempt < 3; attempt++) {
    const current = page.current();
    page.emit("error");
    assert(current.removed);
    if (attempt < 2) page.timers.shift().callback();
  }
  assert.equal(page.inserted.length, 2);
  assert.deepEqual(page.timers, []);
  assert.equal(page.status.startup, "failed");
  assert.equal(page.notice.hidden, false);
  assert.match(page.message.textContent, /Could not load Ribbon Field.*reload/);
});

test("a pending bundle can complete without a premature error, retry, or clearing an application error", () => {
  const page = startup();
  assert.deepEqual(page.timers, []);
  assert.equal(page.notice.hidden, true);
  assert.equal(page.status.startup, "loading");
  page.notice.hidden = false;
  page.message.textContent = "Conversation could not be opened.";
  page.emit("load");
  assert.equal(page.status.startup, "loaded");
  assert.equal(page.inserted.length, 0);
  assert.equal(page.notice.hidden, false);
  assert.equal(page.message.textContent, "Conversation could not be opened.");
});
