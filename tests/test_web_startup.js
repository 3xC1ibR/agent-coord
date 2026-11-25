"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const html = readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/index.html"), "utf8");
const source = html.match(/<script id="workspace-startup">([\s\S]*?)<\/script>/)[1];
const paths = [...html.matchAll(/<link rel="preload" as="script" href="([^"]+)" data-startup>/g)].map(match => match[1]);

function fixture({themeFailed = false} = {}) {
  const inserted = [], timers = new Map(), status = {}, message = {}, notice = {hidden: true, querySelector: () => message};
  let now = 0, sequence = 0;
  const context = vm.createContext({URL, Array,
    setTimeout(callback, delay) { const id = ++sequence; timers.set(id, {at: now + delay, callback}); return id; },
    clearTimeout(id) { timers.delete(id); },
    document: {
      documentElement: {dataset: status},
      querySelectorAll: () => [
        ...(themeFailed ? [{src: "http://localhost:8765/theme.js?v=content-digest"}] : []),
        ...paths.map(path => ({href: "http://localhost:8765" + path + "?v=content-digest"})),
      ],
      getElementById: () => notice,
      createElement: () => ({remove() { this.removed = true; }}),
      head: {append(script) { inserted.push(script); }},
    },
  });
  const run = () => vm.runInContext(source, context);
  run();
  return {inserted, timers, status, notice, message, run,
    current: () => inserted.at(-1),
    advance(ms) {
      const end = now + ms;
      while (true) {
        const next = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        timers.delete(next[0]); now = next[1].at; next[1].callback();
      }
      now = end;
    },
  };
}

test("startup executes the bundled workspace script once with zero sequential waterfall", () => {
  assert.deepEqual(paths, ["/dist/app.js"]);
  const cssLinks = [...html.matchAll(/<link rel="stylesheet" href="([^"]+)">/g)].map(m => m[1]);
  assert.deepEqual(cssLinks, ["/dist/app.css"]);
  const page = fixture();
  assert.equal(page.inserted.length, 1);
  assert.equal(new URL(page.current().src).pathname, "/dist/app.js");
  page.current().onload();
  assert.equal(page.status.startup, "loaded");
  assert.equal(page.timers.size, 0);
  page.run();
  assert.equal(page.inserted.length, 1, "a second loader invocation cannot initialize again");
});

test("a failed before-paint theme script recovers before the workspace bundle", () => {
  const page = fixture({themeFailed: true});
  assert.equal(new URL(page.current().src).pathname, "/theme.js");
  page.current().onload();
  assert.equal(new URL(page.current().src).pathname, "/dist/app.js");
  page.current().onload();
  assert.equal(page.status.startup, "loaded");
  assert.equal(page.inserted.length, 2);
});

test("transient bundle load failure retries with cache-preserving retry query", () => {
  const page = fixture({themeFailed: true});
  for (const path of ["/theme.js", "/dist/app.js"]) {
    const previous = page.current(), count = page.inserted.length;
    previous.onerror();
    previous.onerror();
    previous.onload();
    page.advance(99);
    assert.equal(page.inserted.length, count);
    page.advance(1);
    assert.equal(page.inserted.length, count + 1);
    const url = new URL(page.current().src);
    assert.equal(url.pathname, path);
    assert.equal(url.searchParams.get("v"), "content-digest");
    assert.equal(url.searchParams.get("startup_retry"), "1");
    assert.equal(previous.removed, true);
    page.current().onload();
  }
  assert.equal(page.status.startup, "loaded");
  assert.equal(page.inserted.length, 4);
  assert.equal(page.timers.size, 0);
});

test("persistent failure stops after six attempts and exposes the existing error notice", () => {
  const page = fixture();
  page.current().onerror();
  for (const delay of [100, 200, 400, 800, 1600]) {
    page.advance(delay);
    page.current().onerror();
  }
  assert.equal(page.inserted.length, 6);
  assert.equal(page.status.startup, "failed");
  assert.equal(page.notice.hidden, false);
  assert.match(page.message.textContent, /Could not load Ribbon Field/);
  assert.equal(page.timers.size, 0);
  page.advance(10000);
  page.run();
  assert.equal(page.inserted.length, 6);
});

test("a stalled request reaches the total deadline without unsafe reinsertion", () => {
  const page = fixture(), script = page.current();
  page.advance(3999);
  assert.equal(page.status.startup, "loading");
  page.advance(1);
  assert.equal(page.status.startup, "waiting");
  assert.equal(page.inserted.length, 1);
  assert.equal(page.notice.hidden, false);
  assert.equal(page.timers.size, 0);
  script.onload(); script.onerror();
  assert.equal(page.inserted.length, 1);
  assert.equal(page.status.startup, "loaded");
  assert.equal(page.notice.hidden, true);
  assert.equal(page.timers.size, 0);
});

test("late success cannot start another retry window for a failed dependent", () => {
  const page = fixture({themeFailed: true});
  page.advance(4000);
  page.current().onload();
  page.current().onerror();
  assert.equal(page.status.startup, "failed");
  assert.equal(page.notice.hidden, false);
  page.advance(10000);
  assert.equal(page.inserted.length, 2);
  assert.equal(page.timers.size, 0);
});

test("late load failure never reinserts the uncertain script", () => {
  const page = fixture();
  page.advance(4000);
  page.current().onerror();
  assert.equal(page.status.startup, "failed");
  page.advance(10000);
  assert.equal(page.inserted.length, 1);
  assert.equal(page.timers.size, 0);
});

test("late completion preserves an unrelated application error", () => {
  const page = fixture();
  page.advance(4000);
  page.message.textContent = "Conversation could not be opened.";
  page.current().onload();
  assert.equal(page.status.startup, "loaded");
  assert.equal(page.notice.hidden, false);
  assert.equal(page.message.textContent, "Conversation could not be opened.");
});

test("the recovery deadline covers the whole sequence, not each asset separately", () => {
  const page = fixture({themeFailed: true});
  page.advance(3500);
  page.current().onload();
  page.current().onerror();
  page.advance(100);
  page.current().onerror();
  page.advance(200);
  page.current().onerror();
  page.advance(200);
  assert.equal(page.status.startup, "failed");
  assert.equal(page.timers.size, 0);
  const attempts = page.inserted.length;
  page.advance(10000);
  assert.equal(page.inserted.length, attempts);
});
