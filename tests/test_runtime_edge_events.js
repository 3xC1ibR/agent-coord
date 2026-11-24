"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname,
  "../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const connect = source.slice(source.indexOf("\nfunction connect()"), source.indexOf("\n// Per-item provider events"));

test("a cached conversation reconnects with its generation and durable completion cursor", () => {
  const streams = [];
  const window = new EventTarget();
  const previousGeneration = "a".repeat(32);
  const currentGeneration = "b".repeat(32);
  const state = {config: {eventCursor: previousGeneration + "/1", completionCursor: 17}};
  vm.runInNewContext(connect + "\nconnect()", {
    base: "/api/browser/", state, window, notifications: {receive() {}},
    EventSource: class {
      constructor(url) { this.url = url; streams.push(this); }
      close() { this.closed = true; }
    },
  });
  assert.equal(streams.length, 1);
  assert.equal(streams[0].url, "/api/browser/events?after=" + previousGeneration + "/1&completion_after=17");
  streams[0].onmessage({
    data: JSON.stringify({seq: 2, completionSeq: 24, events: []}),
    lastEventId: currentGeneration + "/2:24",
  });
  window.dispatchEvent(Object.assign(new Event("pagehide"), {persisted: true}));
  assert.equal(streams[0].closed, true);
  window.dispatchEvent(Object.assign(new Event("pageshow"), {persisted: true}));
  assert.equal(streams.length, 2);
  assert.equal(streams[1].url, "/api/browser/events?after=" + currentGeneration + "/2&completion_after=24");
});
