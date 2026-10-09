"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const Dispatch = require("../plugins/agent-coord/scripts/agent_coord/web/dispatch.js");

const receipt = (id, values = {}) => ({id, dispatcher_id: "entry", request_id: "request-" + id,
  target_id: "vendor", target_title: "Vendor form", state: "accepted", created_at: id * 10,
  body: {message: "Request " + id, images: []}, ...values});
const user = text => ({type: "userMessage", content: [{type: "text", text}]});

test("fast routes remain visible chronologically alongside real dispatcher replies", () => {
  const input = [{id: "fallback", startedAt: 35, items: [user("Request 3"), {type: "agentMessage", text: "Which vendor?"}]}];
  const result = Dispatch.turns(input, [receipt(1), receipt(2), receipt(3, {target_id: "entry", turn_id: "fallback"})]);
  assert.deepEqual(result.map(t => t.id), ["dispatch-1", "dispatch-2", "dispatch-3", "fallback"]);
  assert.equal(result.flatMap(t => t.items).filter(i => i.type === "userMessage").length, 3);
  assert.equal(result.at(-1).items[0].text, "Which vendor?");
  assert.equal(input[0].items.length, 2, "Provider history remains unchanged");
});

test("internal catch-up stays out of display while later user steering remains visible", () => {
  const source = [{id: "fallback", startedAt: 30, items: [
    {type: "userMessage", content: [{type: "text", text: "[Ribbon Field dispatcher context]\nprivate history"}, {type: "text", text: "Request 1"}]},
    user("Actually use the other vendor")]}];
  const result = Dispatch.turns(source, [receipt(1, {target_id: "entry", turn_id: "fallback"})]);
  assert.equal(JSON.stringify(result).includes("private history"), false);
  assert.equal(result.at(-1).items[0].content[0].text, "Actually use the other vendor");
});

test("request keys survive network retries and reload without retaining message contents", () => {
  const values = new Map(), storage = {getItem: k => values.get(k), setItem: (k,v) => values.set(k,v)};
  let count = 0; const uuid = () => "uuid-" + ++count;
  const body = {message: "private input", askDispatcher: false};
  assert.equal(Dispatch.requestKey(storage, "entry", body, uuid), "uuid-1");
  assert.equal(Dispatch.requestKey(storage, "entry", body, uuid), "uuid-1");
  assert.equal(Dispatch.requestKey(storage, "entry", {...body, askDispatcher: true}, uuid), "uuid-2");
  assert.equal([...values.values()].join().includes("private input"), false);
});

test("receipts link to the destination and report acceptance without claiming completion", () => {
  const document = {createElement: tag => ({tag, children: [], append(...children) {this.children.push(...children);}})};
  let opened, dismissed;
  const node = Dispatch.card(document, receipt(1), {open: id => {opened = id;}});
  assert.equal(node.children[0].textContent, "Sent");
  assert.equal(node.children[1].href, "/#vendor");
  node.children[1].onclick({preventDefault() {}});
  assert.equal(opened, "vendor");
  const uncertain = Dispatch.card(document, receipt(2, {state: "uncertain"}), {dismiss: id => {dismissed = id;}});
  assert.equal(uncertain.children[0].textContent, "Delivery needs review");
  uncertain.children.at(-1).onclick(); assert.equal(dismissed, 2);
});
