"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {NONE, matches, groupThreads} = require("../plugins/agent-coord/scripts/agent_coord/web/organization.js");

const threads = [
  {thread_id: "both", repository_id: "api", repository_name: "Backend", repository_root: "/api", project_id: "migration", project_name: "Migration"},
  {thread_id: "repo", repository_id: "api", repository_name: "Backend", repository_root: "/api", project_id: null},
  {thread_id: "project", repository_id: null, project_id: "migration", project_name: "Migration"},
  {thread_id: "neither", repository_id: null, project_id: null},
  {thread_id: "other", repository_id: "web", repository_name: "Frontend", repository_root: "/web", project_id: "migration", project_name: "Migration"},
];
test("independent filters intersect and distinguish all from none", () => {
  const ids = (repo, project) => threads.filter(t => matches(t, repo, project)).map(t => t.thread_id);
  assert.deepEqual(ids("api", "migration"), ["both"]);
  assert.deepEqual(ids("", "migration"), ["both", "project", "other"]);
  assert.deepEqual(ids(NONE, ""), ["project", "neither"]);
  assert.deepEqual(ids("", NONE), ["repo", "neither"]);
  assert.deepEqual(ids(NONE, NONE), ["neither"]);
});
test("projects span repositories while missing associations remain visible", () => {
  const groups = groupThreads(threads, "project");
  assert.deepEqual(groups.map(g => g.label), ["Migration", "No group"]);
  assert.deepEqual(groups[0].threads.map(t => t.thread_id), ["both", "project", "other"]);
  assert.deepEqual(groupThreads(threads, "repository").map(g => g.label), ["Backend", "Frontend", "No repository"]);
});
test("equal repository names stay separate and order remains stable", () => {
  const input = [...threads, {thread_id: "same-name", repository_id: "other-api", repository_name: "Backend", repository_root: "/other-api"}];
  const before = JSON.stringify(input);
  const groups = groupThreads(input, "repository");
  assert.deepEqual(groups.filter(g => g.label === "Backend").map(g => g.detail), ["/api", "/other-api"]);
  assert.deepEqual(groups[0].threads.map(t => t.thread_id), ["both", "repo"]);
  assert.equal(JSON.stringify(input), before);
  assert.equal(groups.flatMap(g => g.threads).length, input.length);
});
test("changing grouping never drops threads with no associations", () => {
  for (const dimension of ["repository", "project", "none"]) {
    const result = groupThreads(threads, dimension).flatMap(g => g.threads);
    assert.deepEqual(new Set(result.map(t => t.thread_id)), new Set(threads.map(t => t.thread_id)));
  }
  assert.deepEqual(groupThreads([], "repository"), []);
});
