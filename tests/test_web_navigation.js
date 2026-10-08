"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {parse, safe, initialLink, Router} = require("../plugins/agent-coord/scripts/agent_coord/web/navigation.js");
const markdown = require("../plugins/agent-coord/scripts/agent_coord/web/markdown.js");
const request = "11111111-1111-4111-8111-111111111111";

test("only recognized app routes are clickable in assistant messages", () => {
  for (const url of ["agentcoord://overview?project=billing", "agentcoord://view/review", "agentcoord://thread/thread-1"]) {
    assert.equal(safe(url), true);
    assert.match(markdown.render(`[Open](${url})`), /<a href="agentcoord:/);
  }
  for (const url of ["agentcoord://send?message=oops", "agentcoord://overview?project=x&project=y",
    "agentcoord://overview?database=bad", "agentcoord://overview?request=x", "agentcoord://overview?project=%0a",
    "agentcoord://user@overview", "agentcoord://view/a%2Fb", "agentcoord://thread/t?project=x", "javascript:alert%281%29"]) {
    assert.equal(safe(url), false, url);
    assert.doesNotMatch(markdown.render(`[Open](${url})`), /<a /);
  }
  assert.equal(initialLink({search: "?project=billing"}), "agentcoord://overview?project=billing");
  assert.equal(initialLink({search: "?view=release"}), "agentcoord://view/release");
});

function setup() {
  let snapshot = {viewId: "review", filters: {search: "invoices"}, group_by: "project", thread: "manager"};
  const entries = [], calls = [], errors = [], applied = [], writes = [];
  let index = -1;
  const history = {
    get state() { return entries[index]?.state || null; },
    replaceState: (state, _, url) => { writes.push({state, url}); if (index < 0) index = 0; entries[index] = {state, url}; },
    pushState: (state, _, url) => { writes.push({state, url}); entries.splice(++index); entries.push({state, url}); },
  };
  const router = new Router({history, location: {}, windowId: request, onError: e => errors.push(e),
    api: async (path, body) => { calls.push({path, body}); return path.endsWith("resolve") ? parse(body.url) : {}; },
    capture: () => structuredClone(snapshot),
    apply: async route => { applied.push(route); snapshot = {viewId: "all", filters: route.filters, group_by: "phase", thread: null}; router.remember(); },
    restore: async value => { snapshot = structuredClone(value); },
  });
  return {router, entries, calls, errors, applied, writes, snapshot: () => snapshot,
    back: async () => { index--; await router.back(history.state.agentCoord); }};
}

test("streaming pane status bursts do not rewrite unchanged navigation history", () => {
  const c = setup();
  for (let i = 0; i < 1000; i++) c.router.remember();
  assert.equal(c.writes.length, 1);
  c.snapshot().thread = "another-thread";
  c.router.remember();
  assert.equal(c.writes.length, 2);
  assert.equal(c.entries[0].url, "/?view=review#another-thread");
});

test("changes to filters and grouping are saved even when the URL stays the same", () => {
  const c = setup();
  c.router.remember();
  const url = c.entries[0].url;
  c.snapshot().filters.search = "receipts";
  c.router.remember();
  c.snapshot().group_by = "phase";
  c.router.remember();
  c.snapshot().tiled = true;
  c.router.remember();
  assert.equal(c.writes.length, 4);
  assert.equal(c.entries[0].url, url);
  assert.deepEqual(c.entries[0].state.agentCoord, c.snapshot());
});

test("history deduplication follows the current entry after navigation and Back", async () => {
  const c = setup();
  await c.router.open("agentcoord://overview?project=billing");
  c.router.remember();
  assert.equal(c.writes.length, 2);
  await c.back();
  c.router.remember();
  assert.equal(c.writes.length, 2);
  c.snapshot().filters.search = "changed after Back";
  c.router.remember();
  assert.equal(c.writes.length, 3);
  assert.equal(c.entries[0].state.agentCoord.filters.search, "changed after Back");
  assert.equal(c.entries[1].state.agentCoord.filters.project, "billing");
});

test("navigation acknowledges after applying and retains return history", async () => {
  const c = setup(), before = structuredClone(c.snapshot());
  const result = await c.router.open("agentcoord://overview?project=billing&request=" + request);
  assert.equal(result.status, "displayed");
  assert.deepEqual(c.entries[0].state.agentCoord, before);
  assert.equal(c.entries[1].url, "/?project=billing");
  assert.equal(c.calls.at(-1).path, "navigation/ack");
  assert.equal(c.calls.at(-1).body.status, "displayed");
  await c.router.back(c.entries[0].state.agentCoord);
  assert.deepEqual(c.snapshot(), before);
});

test("missing targets and open dialogs do not navigate or report success", async () => {
  for (const blocked of [true, false]) {
    const c = setup();
    c.router.blocked = () => blocked;
    if (!blocked) c.router.api = async path => { if (path.endsWith("resolve")) throw new Error("Project unavailable"); };
    const result = await c.router.open("agentcoord://overview?project=missing&request=" + request);
    assert.equal(result.status, "failed");
    assert.equal(c.applied.length, 0);
    assert.equal(c.snapshot().thread, "manager");
    assert.equal(c.errors.length, 1);
  }
});

test("rapid app links apply in order and leave the final requested destination", async () => {
  const c = setup();
  await Promise.all([c.router.open("agentcoord://overview?project=billing"), c.router.open("agentcoord://overview?project=tax")]);
  assert.deepEqual(c.applied.map(route => route.filters.project), ["billing", "tax"]);
  assert.equal(c.snapshot().filters.project, "tax");
});

test("a destination removed or superseded during navigation is not acknowledged as displayed", async () => {
  const c = setup();
  c.router.apply = async () => {};
  const result = await c.router.open("agentcoord://overview?project=billing&request=" + request);
  assert.equal(result.status, "failed");
  assert.equal(c.calls.at(-1).body.status, "failed");
  assert.equal(c.entries.length, 1);
});
