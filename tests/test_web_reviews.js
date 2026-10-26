"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {ReviewUI} = require("../plugins/agent-coord/scripts/agent_coord/web/reviews.js");

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.value = ""; this.attributes = {}; }
  append(...children) { this.children.push(...children); }
  before() {}
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  get options() { return this.children; }
}
const document = () => {
  const nodes = new Map();
  return {body: new Element("body"), createElement: tag => new Element(tag), getElementById: id => {
    if (!nodes.has(id)) nodes.set(id, new Element("div")); return nodes.get(id);
  }};
};
const run = values => ({id: "r1", state: "active", active: true, phase: "review", owner_id: "owner", critic_id: "critic", created_at: 20,
  standard: "Check constraints", packet: {original_request: "Original request"}, draft: "Provisional answer",
  steps: [{stage: "draft", actor_id: "owner", turn_id: "draft-1", result: {draft: "Provisional answer"}}], ...values});
const walk = node => [node, ...node.children.flatMap(walk)];

test("ordinary conversations retain their original turn order", () => {
  const turns = [{id: "timestamped", startedAt: 10}, {id: "legacy"}];
  assert.equal(ReviewUI.turns(turns, [], "owner"), turns);
});

test("review turns preserve chronology and tool visibility while withholding raw drafts", () => {
  const turns = [{id: "old", startedAt: 10, items: [{type: "agentMessage", text: "Previous answer"}]},
    {id: "draft-1", startedAt: 20, items: [{type: "agentMessage", text: "Provisional answer"}, {type: "commandExecution", command: "pwd"}]},
    {id: "new", startedAt: 30, items: [{type: "userMessage", content: [{text: "New request"}]}]}];
  const projected = ReviewUI.turns(turns, [run()], "owner");
  assert.deepEqual(projected.map(t => t.id), ["old", "draft-1", "review:r1", "new"]);
  assert.deepEqual(projected[1].items, [{type: "commandExecution", command: "pwd"}]);
  assert.equal(turns[1].items.length, 2, "Provider history stays intact");
});

test("starting events are gated before the refreshed review record binds the turn", () => {
  const projected = ReviewUI.turns([{id: "fresh", items: [{type: "reviewStage", runId: "r1"}, {type: "agentMessage", text: "private"}]}], [run({steps: []})], "owner");
  assert.equal(projected[0].items.length, 0);
});

test("a provisional answer is inspectable but never presented as the delivered result", () => {
  const card = ReviewUI.card(document(), run(), {render: text => text, open() {}});
  assert.equal(card.children.some(n => n.innerHTML === "Provisional answer"), false);
  const inspection = card.children.find(n => n.tagName === "details");
  assert.equal(inspection.open, false);
  assert.ok(walk(inspection).some(n => n.textContent?.includes("Provisional")));
  const complete = ReviewUI.card(document(), run({active: false, state: "complete"}), {render: text => text, open() {}});
  assert.ok(complete.children.some(n => n.innerHTML === "Provisional answer"));
});

test("findings escape markup and retain verification status and disagreement", () => {
  const review = run({active: false, state: "complete", steps: [{stage: "recheck", result: {outcome: "no_material_findings", coverage: "Limited",
    findings: [], rechecks: [{id: "F1", status: "disputed", reason: "<script>bad()</script>"}]}}]});
  const card = ReviewUI.card(document(), review, {render: text => text, open() {}});
  assert.ok(walk(card).some(n => n.textContent?.includes("disagreements remain")));
  const finding = walk(card).find(n => n.textContent?.includes("<script>"));
  assert.equal(finding.innerHTML, undefined);
});

test("request IDs remain stable on transport retry and differ for changed input", () => {
  const ui = new ReviewUI({document: document()});
  ui.settings.set("owner", {client: "kimi", standard: "Check constraints"});
  const first = {message: "My task"}; ui.decorate(first, "owner");
  const second = {message: "My task"}; ui.decorate(second, "owner");
  assert.equal(first.requestId, second.requestId);
  assert.equal(first.review.client, "kimi");
  const third = {message: "Changed task"}; ui.decorate(third, "owner");
  assert.notEqual(first.requestId, third.requestId);
  ui.sent("owner"); assert.equal(ui.settings.size, 0);
});

test("all supported critic providers appear and unavailable catalogs disable enable", async () => {
  const ui = new ReviewUI({document: document(), api: async () => ({data: []}), changed() {}});
  ui.render({session: {client: "muse"}, work_thread: {browser_session: true}}, "owner");
  await ui.configure();
  const nodes = walk(ui.dialog);
  const provider = nodes.find(n => n.tagName === "select");
  assert.deepEqual(provider.options.map(o => o.value), ["codex", "claude", "antigravity", "kimi", "muse"]);
  assert.equal(provider.value, "muse");
  assert.equal(nodes.find(n => n.textContent === "Enable review").disabled, true);
});
