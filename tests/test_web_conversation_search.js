"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const {Search, parameters, highlight} = require("../plugins/agent-coord/scripts/agent_coord/web/conversation-search.js");
class Element {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.hidden = false; this.value = ""; this.textContent = ""; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}
function setup(api = async () => ({items: [], coverage: {saved_histories: 0, threads: 0}})) {
  const elements = new Map();
  const document = {getElementById: id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    createElement: tag => new Element(tag), createTextNode: text => Object.assign(new Element("#text"), {textContent: text})};
  const $ = document.getElementById;
  $("search-scope").value = "all";
  const search = new Search({document, api, filters: () => ({show: "active", repository: "repo"})});
  return {search, $, document};
}
test("all scope ignores current view, current scope includes its filters", () => {
  const f = {show: "archived", repository: "repo", project: "group", phase: "planning", project_cwd: "/work/current"};
  const all = new URLSearchParams(parameters(" Jev ", "all", true, f));
  assert.equal(all.get("q"), "Jev"); assert.equal(all.get("my_messages"), "true");
  assert.equal(all.has("repository"), false);
  assert.equal(all.get("project_cwd"), "/work/current");
  const global = new URLSearchParams(parameters("Jev", "all_projects", true, f));
  assert.equal(global.get("my_messages"), "true");
  for (const key of ["project_cwd", "project", "repository", "phase", "show"]) assert.equal(global.has(key), false);
  const current = new URLSearchParams(parameters("Jev", "current", false, f));
  assert.equal(current.get("show"), "archived"); assert.equal(current.get("phase"), "planning");
});
test("highlight treats HTML and regex punctuation as text", () => {
  const {document} = setup(), el = new Element();
  highlight(document, el, "<img onerror=oops> use Jev and a+b", "jev a+b");
  assert.equal(el.children.map(x => x.textContent).join(""), "<img onerror=oops> use Jev and a+b");
  assert.deepEqual(el.children.filter(x => x.tag === "mark").map(x => x.textContent), ["Jev", "a+b"]);
  assert.equal(el.children.some(x => x.tag === "img"), false);
});
test("late responses cannot overwrite newer search or a cleared query", async () => {
  const pending = [];
  const {search, $} = setup(() => new Promise(resolve => pending.push(resolve)));
  $("search").value = "old"; search.update(); clearTimeout(search.timer);
  const old = search.fetch(search.signature, search.serial);
  $("search").value = "new"; search.update(); clearTimeout(search.timer);
  pending[0]({items: [{thread_id: "wrong"}], coverage: {}}); await old;
  assert.deepEqual(search.items, []);
  const next = search.fetch(search.signature, search.serial);
  $("search").value = ""; assert.equal(search.update(), false);
  pending[1]({items: [{thread_id: "wrong-again"}], coverage: {}}); await next;
  assert.equal($("conversation-results").hidden, true); assert.equal($("overview").hidden, false);
  assert.deepEqual(search.items, []);
});
test("results expose coverage, message links, escaped excerpts and pagination", async () => {
  const match = {role: "user", timestamp: 1234, excerpt: "can we use Jev?", url: "agentcoord://thread/t?turn=a&item=b"};
  const item = {thread_id: "t", title: "Thread", attention: "archived", client: "codex", matches: [match], message_count: 5};
  const {search, $} = setup(async () => ({items: [item], next_cursor: "next", coverage: {threads: 3, saved_histories: 1, missing_histories: 1, partial_histories: 1}}));
  $("search").value = "jev"; search.update(); clearTimeout(search.timer);
  await search.fetch(search.signature, search.serial);
  assert.match($("search-status").textContent, /including closed/);
  assert.match($("search-status").textContent, /1 has partial history/);
  assert.equal($("search-more").hidden, false);
  const card = $("search-hits").children[0];
  assert.equal(card.children.find(c => c.className === "search-excerpt").href, match.url);
  await search.fetch(search.signature, search.serial, "next");
  assert.equal(search.items.length, 1);
  assert.match(card.children.at(-1).textContent, /showing the best 1/);
});
test("empty project results offer an all-project search with the same query", async () => {
  const {search, $} = setup();
  $("search").value = "remember this"; search.update(); clearTimeout(search.timer);
  await search.fetch(search.signature, search.serial);
  const expand = $("search-hits").children.find(child => child.tag === "button");
  assert.equal(expand.textContent, "Search all projects");
  expand.onclick(); clearTimeout(search.timer);
  assert.equal($("search").value, "remember this");
  assert.equal($("search-scope").value, "all_projects");
  await search.fetch(search.signature, search.serial);
  assert.match($("search-status").textContent, /All projects, including closed/);
  assert.equal($("search-hits").children.some(child => child.tag === "button"), false);
});
