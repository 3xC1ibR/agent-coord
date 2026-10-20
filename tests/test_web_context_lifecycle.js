"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/context-lifecycle.js"), "utf8");

function setup() {
  const context = vm.createContext({});
  vm.runInContext(source, context);
  const document = {createElement(tag) {
    const element = {tag, textContent: "", dataset: {}, children: [], attributes: {}, open: false,
      setAttribute(key, value) { this.attributes[key] = value; },
      append(...children) { this.children.push(...children); }, ownerDocument: document};
    Object.defineProperty(element, "innerHTML", {set() { throw new Error("Report text must stay inert"); }});
    return element;
  }};
  return {render: context.agentCoordContextLifecycle.render, host: document.createElement("div")};
}

function report(recommendation = "reuse") {
  return {recommendation, reason: "Useful debugging constraints remain here.",
    reuse_for: ["Related fixes"], fresh_for: ["Independent review"],
    handover: {confirmed_facts: ["Empty input is rejected."], constraints: ["Keep existing paths."],
      tried_and_failed: ["Parsing before trimming failed."], open_questions: ["Check Unicode whitespace."]}};
}

function nodes(element) { return [element, ...element.children.flatMap(nodes)]; }
function texts(element) { return nodes(element).map(node => node.textContent).filter(Boolean); }

test("missing and unrecognized reports stay unknown without a default recommendation", () => {
  for (const value of [undefined, null, {recommendation: "unknown"}, {recommendation: "toString"}]) {
    const {render, host} = setup();
    render(host, {checkpoint: {context_lifecycle: value}});
    assert.deepEqual(texts(host), ["Context reuse", "No context reuse advice saved yet."]);
    assert.equal(host.children[0].attributes["aria-label"], "Context reuse");
  }
});

test("each recommendation has a human-readable label, reason, and task-specific guidance", () => {
  for (const [recommendation, label] of [["reuse", "Reuse this conversation"], ["fresh", "Fresh context recommended"],
    ["either", "Either context works"]]) {
    const {render, host} = setup();
    render(host, {checkpoint: {context_lifecycle: report(recommendation)}});
    const content = texts(host);
    for (const text of [label, "Useful debugging constraints remain here.", "Related fixes", "Independent review"])
      assert.ok(content.includes(text));
    assert.equal(nodes(host).filter(node => node.tag === "h3").length, 1);
  }
});

test("the handover contains all four kinds of useful state without exposing runtime identifiers", () => {
  const {render, host} = setup();
  render(host, {checkpoint: {id: 123, context_lifecycle: {...report(), assignment_message_id: 456}}});
  const content = texts(host);
  for (const text of ["Empty input is rejected.", "Keep existing paths.", "Parsing before trimming failed.",
    "Check Unicode whitespace."]) assert.ok(content.includes(text));
  assert.ok(!content.some(text => text.includes("123") || text.includes("456")));
  assert.equal(nodes(host).find(node => node.tag === "details").open, false);
});

test("earlier-turn advice is explicitly distinguished from advice for the current turn", () => {
  for (const stale of [false, true]) {
    const {render, host} = setup();
    render(host, {checkpoint: {context_lifecycle: report()}, checkpoint_stale: stale});
    assert.equal(texts(host).includes("Saved before the current turn. Review before reusing."), stale);
  }
});

test("the handover stays open during refresh and resets when switching conversations", () => {
  const {render, host} = setup();
  const options = {sameThread: true, opened: new Set(["context-handover"])};
  render(host, {checkpoint: {context_lifecycle: report()}}, options);
  assert.equal(nodes(host).find(node => node.tag === "details").open, true);
  host.children = [];
  render(host, {checkpoint: {context_lifecycle: report("fresh")}}, {...options, sameThread: false});
  assert.equal(nodes(host).find(node => node.tag === "details").open, false);
});

test("report strings are displayed as inert text, including malicious markup", () => {
  const {render, host} = setup();
  const text = '<img src=x onerror="alert(1)">';
  render(host, {checkpoint: {context_lifecycle: {...report(), reason: text,
    reuse_for: [text], handover: {confirmed_facts: [text]}}}});
  assert.equal(texts(host).filter(value => value === text).length, 3);
  assert.ok(!nodes(host).some(node => node.tag === "img" || node.tag === "script"));
});

test("empty or absent handover lists do not produce empty disclosures", () => {
  const {render, host} = setup();
  render(host, {checkpoint: {context_lifecycle: {...report(), reuse_for: [], fresh_for: [],
    handover: {confirmed_facts: [], constraints: [], tried_and_failed: [], open_questions: []}}}});
  assert.equal(nodes(host).filter(node => node.tag === "details" || node.tag === "ul").length, 0);
  assert.deepEqual(texts(host), ["Context reuse", "Reuse this conversation", "Useful debugging constraints remain here."]);
});
