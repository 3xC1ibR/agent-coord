const test = require("node:test");
const assert = require("node:assert/strict");
const metrics = require("../plugins/agent-coord/scripts/agent_coord/web/coordination-metrics.js");

test("missing telemetry stays unavailable while real zero stays zero", () => {
  for (const invalid of [null, undefined, NaN, Infinity, -1, "12", false]) {
    assert.equal(metrics.count(invalid), "Unavailable");
    assert.equal(metrics.bytes(invalid), "Unavailable");
    assert.equal(metrics.value({argument_tokens: invalid}, "argument", true), null);
  }
  assert.equal(metrics.count(0), "0");
  assert.equal(metrics.bytes(0), "0 B");
  assert.equal(metrics.value({argument_tokens: 0}, "argument", true), 0);
});

test("token mode requires both the declared unit and tokenizer availability", () => {
  assert.equal(metrics.tokensAvailable({measurement: {unit: "tokens", tokenizer_available: true}}), true);
  for (const measurement of [{unit: "bytes", tokenizer_available: true}, {unit: "tokens", tokenizer_available: false}, {unit: "tokens"}, null]) {
    assert.equal(metrics.tokensAvailable({measurement}), false);
  }
  assert.equal(metrics.value({argument_tokens: 900, argument_bytes: 3800}, "argument", false), 3800);
});

test("visible coverage names sources, exclusions and incomplete collection", () => {
  assert.match(metrics.coverageSummary({coverage: {providers: ["codex"]}, status: {state: "ready"}}), /^Codex · MCP \+ standalone CLI/);
  assert.match(metrics.coverageSummary({coverage: {providers: ["codex"]}, status: {state: "partial"}}), /Collection incomplete/);
  assert.match(metrics.coverageSummary({coverage: {providers: ["codex"]}, status: {state: "running"}}), /Collection incomplete/);
  assert.match(metrics.coverageSummary({}), /coverage unavailable/);
  assert.match(metrics.coverageSummary({}), /Compound commands and unsupported providers are excluded/);
});

test("partially measured token totals disclose their denominator and cannot imply a saving", () => {
  const full = {calls: 100, argument_token_calls: 100, return_token_calls: 100};
  const partial = {calls: 100, argument_token_calls: 100, return_token_calls: 90};
  assert.deepEqual(metrics.measurementCoverage(partial, "return"), {complete: false, caption: "Measured 90 of 100 calls"});
  assert.equal(metrics.tokenComparisonAvailable(partial, full, "argument"), true);
  assert.equal(metrics.tokenComparisonAvailable(partial, full, "return"), false);
  assert.equal(metrics.tokenComparisonAvailable(full, partial, "return"), false);
  assert.equal(metrics.tokenComparisonAvailable(full, {}, "return"), false);
  assert.equal(metrics.measurementCoverage({calls: 100, return_token_calls: 101}, "return").complete, false);
  assert.equal(metrics.measurementCoverage({calls: 0, return_token_calls: 0}, "return").complete, true);
});

test("source recovery suppresses comparisons even when indexed call fields are complete", () => {
  assert.equal(metrics.sourceRecoveryPending({status: {running: true}}), true);
  assert.equal(metrics.sourceRecoveryPending({status: {running: false, progress: {has_more: true}}}), true);
  assert.equal(metrics.sourceRecoveryPending({coverage: {files_done: 9, files_total: 10}}), true);
  assert.equal(metrics.sourceRecoveryPending({status: {progress: {files_done: 9, files_total: 10}}}), true);
  assert.equal(metrics.sourceRecoveryPending({status: {running: false, progress: {has_more: false}}, coverage: {files_done: 10, files_total: 10}}), false);
});

test("period comparisons never infer an infinite saving or a missing baseline", () => {
  assert.equal(metrics.comparison(80, 100), "20% less than previous period");
  assert.equal(metrics.comparison(125, 100), "25% more than previous period");
  assert.equal(metrics.comparison(0, 100), "100% less than previous period");
  assert.equal(metrics.comparison(0, 0), "No change");
  assert.equal(metrics.comparison(10, 0), "No previous value");
  assert.equal(metrics.comparison(10, null), "Previous measurement unavailable");
  assert.equal(metrics.comparison(null, 100), "Previous measurement unavailable");
});

test("polling runs only while indexing is active and the page is visible", () => {
  let visible = true, reads = 0, next = 1;
  const timers = new Map(), cancelled = [];
  const poller = metrics.createPoller({visible: () => visible, read: () => reads++,
    schedule: (fn, delay) => { assert.equal(delay, 5000); timers.set(next, fn); return next++; },
    cancel: id => { cancelled.push(id); timers.delete(id); }});
  poller.update(false); assert.equal(timers.size, 0);
  poller.update(true); assert.equal(timers.size, 1);
  const callback = [...timers.values()][0];
  visible = false; poller.visibility();
  assert.equal(timers.size, 0); callback(); assert.equal(reads, 0);
  visible = true; poller.visibility(); assert.equal(reads, 1);
  poller.update(true); assert.equal(timers.size, 1);
  poller.update(false); assert.equal(timers.size, 0);
  poller.visibility(); assert.equal(reads, 1);
  poller.update(true); poller.stop();
  assert.equal(timers.size, 0);
  poller.update(true); poller.visibility();
  assert.equal(timers.size, 0); assert.equal(reads, 1);
  assert.ok(cancelled.length >= 3);
});

test("unknown daily values remain chart gaps and chart work is bounded", () => {
  assert.deepEqual(metrics.series([{date: "2026-11-01", argument_tokens: 0}, {date: "2026-11-02", argument_tokens: null}, {date: "2026-11-03", argument_tokens: "100"}], "argument_tokens"), [
    {date: "2026-11-01", value: 0}, {date: "2026-11-02", value: null}, {date: "2026-11-03", value: null},
  ]);
  assert.equal(metrics.series(Array.from({length: 10000}, () => ({date: "2026-11-01", argument_tokens: 1})), "argument_tokens").length, 366);
});
