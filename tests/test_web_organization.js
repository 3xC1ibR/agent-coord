"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {NONE, matches, groupThreads} = require("../plugins/agent-coord/scripts/agent_coord/web/organization.js");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function assignmentHarness() {
  const elements = new Map(), calls = [];
  const c = {
    $: id => {
      if (!elements.has(id)) elements.set(id, {value: "", children: [],
        replaceChildren(...items) { this.children = items; }, append(item) { this.children.push(item); },
        showModal() { this.open = true; }, close() { this.open = false; }, focus() {},
      });
      return elements.get(id);
    },
    Option: function(text, value) { return {text, value}; },
    state: {selected: "other", folders: {selected: {cwd: "/other"}},
      detail: {work_thread: {thread_id: "other", project_cwd: "/other"}}},
    action: fn => fn(), threadPath: id => "threads/" + id,
    refreshThread: async () => assert.fail("Must not refresh the other conversation"), refreshList: async () => {},
    api: async (path, body) => {
      calls.push({path, body});
      if (path === "organization") return {repositories: [], projects: [
        {id: "card-group", name: "Card group", cwd: "/card"}, {id: "other-group", name: "Other", cwd: "/other"},
        {id: "legacy", name: "Legacy", cwd: null}]};
      if (path === "projects") return {id: "new-group"};
      return {};
    },
  };
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function assignmentFields("), source.indexOf("function isSessionDraft()")), c);
  c.calls = calls;
  return c;
}

test("card group dialog uses the target group and workspace without changing selection", async () => {
  const c = assignmentHarness();
  await c.organizeThread({thread_id: "card", project_cwd: "/card", project_id: "card-group"});
  assert.equal(c.state.selected, "other");
  assert.equal(c.$("organization-dialog").open, true);
  assert.equal(c.$("organization-workspace").textContent, "Project: /card");
  assert.equal(c.$("edit-project").value, "card-group");
  assert.equal(c.$("edit-project").children.some(item => item.value === "other-group"), false);
  assert.equal(c.$("edit-project").children.some(item => item.value === "legacy"), false);
  c.$("edit-project").value = "__new__";
  c.$("edit-project-value").value = "New group";
  c.$("organization-form").onsubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(JSON.parse(JSON.stringify(c.calls.slice(1))), [
    {path: "projects", body: {name: "New group", cwd: "/card"}},
    {path: "threads/card", body: {project_id: "new-group"}},
  ]);
  assert.equal(c.$("organization-dialog").open, false);
  assert.equal(c.state.selected, "other");
});

test("group dialog uses a terminal thread's project when origin is unavailable", async () => {
  const c = assignmentHarness();
  await c.organizeThread({thread_id: "card", cwd: "/card", project_id: "card-group"});
  assert.deepEqual(Array.from(c.$("edit-project").children, item => item.value), ["", "card-group", "__new__"]);
});

test("card group assignment supports an existing group and removing the association", async () => {
  for (const project of ["card-group", ""]) {
    const c = assignmentHarness();
    await c.organizeThread({thread_id: "card", project_cwd: "/card"});
    c.$("edit-project").value = project;
    c.$("organization-form").onsubmit({preventDefault() {}});
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(JSON.parse(JSON.stringify(c.calls.slice(1))), [
      {path: "threads/card", body: {project_id: project || null}},
    ]);
  }
});

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

test("domain grouping uses explicit names before scope fallback and retains missing signals", () => {
  const input = [
    {thread_id: "explicit", domain: "web", scope_cluster: "backend"},
    {thread_id: "same", domain: " WEB "},
    {thread_id: "fallback", domain: null, scope_cluster: "web"},
    {thread_id: "path", scope_cluster: "src/api"},
    {thread_id: "workspace", domain: ".", domain_source: "derived"},
    {thread_id: "missing"},
    {thread_id: "literal-none", domain: "__none__"},
  ];
  const before = JSON.stringify(input), groups = groupThreads(input, "domain");
  assert.deepEqual(groups.find(group => group.label === "web").threads.map(thread => thread.thread_id), ["explicit", "same", "fallback"]);
  assert.equal(groups.find(group => group.label === "Workspace").threads[0].thread_id, "workspace");
  assert.equal(groups.at(-1).label, "No domain");
  assert.equal(new Set(groups.map(group => group.key)).size, groups.length);
  assert.equal(groups.flatMap(group => group.threads).length, input.length);
  assert.equal(JSON.stringify(input), before);
  assert.deepEqual(groupThreads([], "domain"), []);
});
