"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {ThreadScratchpad} = require("../plugins/agent-coord/scripts/agent_coord/web/scratchpad.js");

const settle = () => new Promise(resolve => setImmediate(resolve));
function harness(api, storage = new Map(), compactStatus = false) {
  const listeners = {}, timers = new Map();
  let timer = 0;
  const input = {value: "", addEventListener: (name, fn) => { listeners[name] = fn; }};
  const view = {addEventListener: (name, fn) => { listeners[name] = fn; },
    document: {addEventListener: (name, fn) => { listeners[name] = fn; }},
    sessionStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    setTimeout: (fn, delay) => { timers.set(++timer, {fn, delay}); return timer; }, clearTimeout: id => timers.delete(id)};
  const panel = {}, status = {setAttribute(key, value) { this[key] = value; }};
  const pad = new ThreadScratchpad({panel, input, status, api, view, compactStatus});
  return {pad, input, status, panel, storage, listeners,
    type(text) { input.value = text; listeners.input(); },
    async tick() { const jobs = [...timers.values()]; timers.clear(); for (const job of jobs) job.fn(); await settle(); }};
}

test("typing debounces saves, preserves exact text, and saves an empty note", async () => {
  let saved = "", writes = 0;
  const h = harness(async (_, body) => { if (body) { saved = body.text; writes++; } return {text: saved}; });
  h.pad.select("one"); await settle();
  assert.equal(h.input.disabled, false);
  h.type("a"); h.type("  notes\n<text> 📝\t");
  assert.equal(writes, 0); assert.equal(h.status.textContent, "Saving…");
  await h.tick();
  assert.equal(writes, 1); assert.equal(saved, "  notes\n<text> 📝\t");
  assert.equal(h.status.textContent, "Saved"); assert.equal(h.storage.size, 0);
  h.type(""); h.listeners.blur(); await settle();
  assert.equal(saved, ""); assert.equal(writes, 2);
});

test("compact save feedback is quiet until an edit succeeds and never claims a failed save", async () => {
  let fail = false;
  const h = harness(async (_, body) => {
    if (body && fail) throw new Error("Offline");
    return {text: body?.text ?? "Existing note"};
  }, new Map(), true);
  h.pad.select("one"); await settle();
  assert.equal(h.status.textContent, "");
  h.type("Updated note");
  assert.equal(h.status.textContent, "Saving…");
  await h.tick();
  assert.equal(h.status.textContent, "✓");
  assert.equal(h.status["aria-label"], "Saved");
  fail = true; h.type("Another edit"); await h.tick();
  assert.equal(h.status.textContent, "Couldn’t save. Retrying…");
  fail = false; await h.tick();
  assert.equal(h.status.textContent, "✓");
  h.pad.select("two"); await settle();
  assert.equal(h.status.textContent, "");
});

test("in-flight saves serialize newer edits including reverting to the previous value", async () => {
  const writes = [], finish = [];
  const h = harness((_, body) => body ? new Promise(resolve => { writes.push(body.text); finish.push(resolve); }) : Promise.resolve({text: "original"}));
  h.pad.select("one"); await settle();
  h.type("first"); await h.tick();
  h.type("second"); await h.tick();
  assert.deepEqual(writes, ["first"]);
  h.type("original");
  assert.equal(h.storage.get("agent-coord.scratchpad-draft:one"), "original");
  finish.shift()({text: "first"}); await settle(); await h.tick();
  assert.deepEqual(writes, ["first", "original"]);
  finish.shift()({text: "original"}); await settle();
  assert.equal(h.status.textContent, "Saved"); assert.equal(h.input.value, "original");
});

test("switching threads flushes the right note and late loads never replace the selected note", async () => {
  let loadOne;
  const writes = [];
  const h = harness((path, body) => {
    if (body) { writes.push([path, body.text]); return Promise.resolve(body); }
    if (path.includes("one")) return new Promise(resolve => { loadOne = resolve; });
    return Promise.resolve({text: "two"});
  });
  h.pad.select("one"); h.pad.select("two"); await settle();
  loadOne({text: "one"}); await settle();
  assert.equal(h.input.value, "two");
  h.type("two edited"); h.pad.select(null); await settle();
  assert.deepEqual(writes, [["threads/two/scratchpad", "two edited"]]);
  assert.equal(h.panel.hidden, true);
});

test("an uncertain save retries the latest text even when it matches the old saved text", async () => {
  let rejectSave;
  const writes = [];
  const h = harness((_, body) => {
    if (!body) return Promise.resolve({text: "original"});
    writes.push(body.text);
    return writes.length === 1 ? new Promise((resolve, reject) => { rejectSave = reject; }) : Promise.resolve(body);
  });
  h.pad.select("one"); await settle();
  h.type("first"); await h.tick(); h.type("original");
  rejectSave(new Error("Response lost")); await settle(); await h.tick();
  assert.deepEqual(writes, ["first", "original"]);
  assert.equal(h.status.textContent, "Saved");
});

test("failed saves retain text across reload and retry, including clearing the note", async () => {
  const h = harness(async (_, body) => { if (body) throw new Error("Offline"); return {text: "old"}; });
  h.pad.select("one"); await settle(); h.type(""); await h.tick();
  assert.equal(h.status.textContent, "Couldn’t save. Retrying…");
  assert.equal(h.storage.get("agent-coord.scratchpad-draft:one"), "");
  let saved;
  const reopened = harness(async (_, body) => { if (body) saved = body.text; return {text: body?.text ?? "old"}; }, h.storage);
  reopened.pad.select("one"); await settle();
  assert.equal(reopened.input.value, "");
  await reopened.tick();
  assert.equal(saved, ""); assert.equal(reopened.status.textContent, "Saved");
  assert.equal(h.storage.size, 0);
});

test("load failures cannot overwrite stored notes and retry automatically", async () => {
  let attempts = 0;
  const h = harness(async (_, body) => {
    assert.equal(body, undefined);
    if (++attempts === 1) throw new Error("Offline");
    return {text: "saved note"};
  });
  h.pad.select("one"); await settle();
  assert.equal(h.input.disabled, true);
  assert.equal(h.status.textContent, "Couldn’t load. Retrying…");
  await h.tick();
  assert.equal(h.input.value, "saved note"); assert.equal(h.input.disabled, false);
});

test("page exit flushes with keepalive and reopening a clean thread fetches current notes", async () => {
  let saved = "first", options;
  const h = harness(async (_, body, opts) => { if (body) { saved = body.text; options = opts; } return {text: saved}; });
  h.pad.select("one"); await settle();
  h.type("last edit"); h.listeners.pagehide(); await settle();
  assert.equal(saved, "last edit"); assert.equal(options.keepalive, true);
  h.pad.select(null); saved = "changed elsewhere"; h.pad.select("one"); await settle();
  assert.equal(h.input.value, "changed elsewhere");
});
