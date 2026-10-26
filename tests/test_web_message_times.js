"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const context = {node: (tag, textContent, className) => ({tag, textContent, className})};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf("function messageTimestamp("), source.indexOf("function forkRelations(")), context);

test("message times accept recorded seconds, milliseconds, and ISO dates", () => {
  for (const field of ["timelineAt", "createdAt", "created_at", "timestamp"]) {
    for (const value of [1793030400, 1793030400000, "2026-10-26T16:00:00Z"]) {
      const item = {type: "agentMessage", [field]: value};
      const timing = context.messageTimestamp(item, {});
      assert.equal(timing.date.toISOString(), "2026-10-26T16:00:00.000Z");
      const label = context.messageTimeLabel(item, {});
      assert.equal(label.tag, "time");
      assert.equal(label.dateTime, "2026-10-26T16:00:00.000Z");
      assert.ok(label.textContent);
      assert.match(label.title, /^Message recorded · /);
    }
  }
});

test("invalid times never become the current time and valid epoch zero is retained", () => {
  for (const value of [undefined, null, "", false, "invalid", Infinity, NaN, 1e20]) {
    assert.equal(context.messageTimestamp({timelineAt: value}, {}), null);
    const label = context.messageTimeLabel({timelineAt: value}, {});
    assert.equal(label.tag, "span");
    assert.equal(label.textContent, "Time unavailable");
    assert.equal(label.dateTime, undefined);
  }
  assert.equal(context.messageTimestamp({timelineAt: 0}, {}).date.toISOString(), "1970-01-01T00:00:00.000Z");
  assert.equal(context.messageTimestamp({timelineAt: "bad", createdAt: 1793030400}, {}).label, "Message recorded");
});

test("legacy boundary fallbacks apply only to the first user and final assistant", () => {
  const first = {type: "userMessage"}, commentary = {type: "agentMessage"}, steer = {type: "userMessage"}, final = {type: "agentMessage"};
  const turn = {status: "completed", startedAt: 1793030400, completedAt: 1793030460, items: [first, commentary, steer, final]};
  assert.equal(context.messageTimestamp(first, turn).label, "Turn started");
  assert.equal(context.messageTimestamp(final, turn).date.toISOString(), "2026-10-26T16:01:00.000Z");
  assert.equal(context.messageTimestamp(commentary, turn), null);
  assert.equal(context.messageTimestamp(steer, turn), null);
  turn.status = "inProgress";
  assert.equal(context.messageTimestamp(final, turn), null);
  first.timelineAt = 1793030410;
  assert.equal(context.messageTimestamp(first, turn).date.toISOString(), "2026-10-26T16:00:10.000Z");
});
