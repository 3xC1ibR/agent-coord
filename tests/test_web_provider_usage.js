const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const sourcePath = path.join(__dirname, "../plugins/agent-coord/scripts/agent_coord/web/provider-usage.js");
const usage = require(sourcePath);

test("remaining allowance distinguishes unknown, zero and over-limit usage", () => {
  for (const value of [null, undefined, "25", NaN, Infinity, -1, true]) assert.equal(usage.remaining(value), null);
  assert.equal(usage.remaining(0), 100);
  assert.equal(usage.remaining(25), 75);
  assert.equal(usage.remaining(100), 0);
  assert.equal(usage.remaining(120), 0);
});
test("countdowns never imply a successful reset when the reported time passes", () => {
  assert.equal(usage.countdown(null, 1000), "Reset time unavailable");
  assert.match(usage.countdown(999, 1000), /awaiting updated usage/);
  assert.equal(usage.countdown(1060, 1000), "Resets in 1m");
  assert.equal(usage.countdown(8260, 1000), "Resets in 2h 1m");
  assert.equal(usage.countdown(91400, 1000), "Resets in 1d 1h");
});
test("freshness and retry state use successful observation and provider backoff", () => {
  assert.equal(usage.status({state: "ready", observed_at: 1000}, 1119), "Up to date");
  assert.equal(usage.status({state: "ready", observed_at: 1000}, 1120), "Last known usage");
  assert.equal(usage.status({state: "error", observed_at: 1000}, 1010), "Last known usage");
  assert.equal(usage.status({state: "sign_in_required", observed_at: null}, 1010), "Sign-in needed");
  assert.equal(usage.due({state: "unsupported"}, 1000), false);
  assert.equal(usage.due({state: "error", next_refresh_at: 1600}, 1599), false);
  assert.equal(usage.due({state: "error", next_refresh_at: 1600}, 1600), true);
});
test("Usage opens without navigating away and only its own iframe can close it", () => {
  const nodes = new Map(), env = new EventTarget();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, Object.assign(new EventTarget(), {
      hidden: true, attributes: {}, getClientRects: () => [1], focus() { env.focused = id; },
      removeAttribute(key) { delete this.attributes[key]; },
    }));
    return nodes.get(id);
  };
  const frame = $("usage-frame"), dialog = $("usage-dialog");
  frame.contentWindow = {};
  Object.defineProperty(frame, "src", {set(value) { frame.attributes.src = value; }});
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.dispatchEvent(new Event("close")); };
  env.document = {getElementById: $}; env.location = {origin: "http://localhost:9000"};
  vm.runInNewContext(fs.readFileSync(sourcePath, "utf8"), env);
  $("open-usage").onclick({preventDefault() {}});
  assert.equal(dialog.open, true);
  assert.equal(frame.attributes.src, "/usage?embedded=1");
  const send = (origin, source) => env.dispatchEvent(Object.assign(new Event("message"), {origin, source, data: {type: "ribbon-field-close-usage"}}));
  send("https://evil.test", frame.contentWindow);
  send(env.location.origin, {});
  assert.equal(dialog.open, true);
  send(env.location.origin, frame.contentWindow);
  assert.equal(dialog.open, false);
  assert.equal(frame.attributes.src, undefined);
  assert.equal(env.focused, "open-usage");
});
