"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const vm = require("node:vm");

const html = readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/index.html"), "utf8");
const source = html.match(/<script id="workspace-startup">([\s\S]*?)<\/script>/)[1];
const entries = [...html.matchAll(/<script id="workspace-program" src="([^"]+)" defer><\/script>/g)];

function startup() {
  const inserted = [], timers = [], status = {}, message = {}, events = {};
  const notice = {hidden: true, querySelector: () => message};
  const script = () => ({remove() { this.removed = true; }});
  const initial = Object.assign(script(), {id: "workspace-program", src: entries[0][1]});
  vm.runInNewContext(source, {
    setTimeout(callback, delay) { timers.push({callback, delay}); },
    document: {
      documentElement: {dataset: status},
      addEventListener(type, listener, capture) { assert.equal(capture, true); events[type] = listener; },
      getElementById: id => { assert.equal(id, "error"); return notice; },
      createElement: tag => { assert.equal(tag, "script"); return script(); },
      head: {append(script) { inserted.push(script); }},
    },
  });
  const current = () => inserted.at(-1) || initial;
  return {inserted, timers, status, notice, message, current,
    emit(type, target = current()) { events[type]({target}); }};
}

test("workspace initializes from one versioned build entry and a linked stylesheet", () => {
  assert.deepEqual(entries.map(match => match[1]), ["/dist/app.js"]);
  assert.match(html, /<link rel="stylesheet" href="\/dist\/app.css">/);
  assert.doesNotMatch(html, /data-startup/);
  const page = startup();
  assert.equal(page.status.startup, "loading");
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
