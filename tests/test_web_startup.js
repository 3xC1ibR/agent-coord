"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const vm = require("node:vm");

const html = readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/index.html"), "utf8");
const source = html.match(/<script id="workspace-startup">([\s\S]*?)<\/script>/)[1];
const preloads = [...html.matchAll(/<link rel="preload" as="script" href="([^"]+)"/g)].map(match => match[1]);

function startup() {
  const inserted = [], timers = [], status = {}, message = {};
  const notice = {hidden: true, querySelector: () => message};
  vm.runInNewContext(source, {
    setTimeout(callback, delay) { timers.push({callback, delay}); },
    document: {
      documentElement: {dataset: status},
      querySelector: selector => { assert.equal(selector, 'link[as="script"]'); return {href: preloads[0]}; },
      getElementById: id => { assert.equal(id, "error"); return notice; },
      createElement: tag => { assert.equal(tag, "script"); return {remove() { this.removed = true; }}; },
      head: {append(script) { inserted.push(script); }},
    },
  });
  return {inserted, timers, status, notice, message, current: () => inserted.at(-1)};
}

test("workspace initializes from one versioned build entry and a linked stylesheet", () => {
  assert.deepEqual(preloads, ["/dist/app.js"]);
  assert.match(html, /<link rel="stylesheet" href="\/dist\/app.css">/);
  assert.doesNotMatch(html, /data-startup/);
  const page = startup();
  assert.equal(page.status.startup, "loading");
  assert.equal(page.current().src, preloads[0]);
  page.current().onload();
  assert.equal(page.status.startup, "loaded");
  assert.deepEqual(page.timers, []);
  assert.equal(page.inserted.length, 1);
});

test("a failed bundle retries the same artifact and can initialize on retry", () => {
  const page = startup();
  const failed = page.current();
  failed.onerror();
  assert(failed.removed);
  assert.equal(page.notice.hidden, true);
  const next = page.timers.shift();
  assert(next.delay > 0);
  next.callback();
  assert.equal(page.current().src, failed.src);
  page.current().onload();
  assert.equal(page.status.startup, "loaded");
  assert.equal(page.inserted.length, 2);
  assert.deepEqual(page.timers, []);
});

test("permanent failure stops retrying and exposes actionable startup feedback", () => {
  const page = startup();
  for (let attempt = 0; attempt < 3; attempt++) {
    const current = page.current();
    current.onerror();
    assert(current.removed);
    if (attempt < 2) page.timers.shift().callback();
  }
  assert.equal(page.inserted.length, 3);
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
  page.current().onload();
  assert.equal(page.status.startup, "loaded");
  assert.equal(page.inserted.length, 1);
  assert.equal(page.notice.hidden, false);
  assert.equal(page.message.textContent, "Conversation could not be opened.");
});
