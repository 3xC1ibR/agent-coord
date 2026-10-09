"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {WorkingFolders, newThreadContext} = require("../plugins/agent-coord/scripts/agent_coord/web/folders.js");
const rig = {id: "rig", root: "/projects/rig"};
const stoic = {id: "stoic", root: "/projects/stoic"};
const organization = {repositories: [rig, stoic], projects: [{id: "evaluation"}]};
const storage = () => {
  const values = new Map();
  return {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
};
const api = async (route, body) => {
  assert.equal(route, "workspaces/open");
  if (body.path === "/missing") throw new Error("Folder unavailable");
  return {cwd: body.path, repository: body.path.startsWith(rig.root) ? rig : null};
};

test("Rig views retain explicit No group and evaluation membership independently of folder", () => {
  const folder = {cwd: "/projects/stoic", repository: stoic};
  assert.deepEqual(newThreadContext(organization, {repository: "rig", project: "__none__"}, folder),
    {cwd: rig.root, repository_id: "rig", project_id: null});
  assert.deepEqual(newThreadContext(organization, {repository: "rig", project: "evaluation"}, folder),
    {cwd: rig.root, repository_id: "rig", project_id: "evaluation"});
  assert.deepEqual(newThreadContext(organization, {project: "evaluation"}, folder),
    {cwd: stoic.root, project_id: "evaluation"});
});

test("repository views preserve explicitly chosen subfolders and worktrees", () => {
  for (const cwd of [rig.root + "/src", "/worktrees/feature"]) {
    const folder = {cwd, repository: rig};
    assert.equal(newThreadContext(organization, {repository: "rig"}, folder).cwd, cwd);
    assert.equal(newThreadContext(organization, {repository: "stoic"}, folder).cwd, stoic.root);
  }
});

test("All work and ordinary folders use the window default; missing selections never broaden filters", () => {
  assert.deepEqual(newThreadContext(organization, {}, {cwd: "/notes", repository: null}), {cwd: "/notes", project_id: null});
  assert.equal(newThreadContext(organization, {}, null).cwd, "");
  assert.throws(() => newThreadContext(organization, {project: "deleted"}, null), /group is unavailable/);
  assert.throws(() => newThreadContext(organization, {repository: "deleted"}, null), /repository is unavailable/);
});

test("window selections restore independently while recent folders are shared and deduplicated", async () => {
  const preferences = storage();
  const one = new WorkingFolders({storage: preferences, preferences, key: "native-0", api});
  const two = new WorkingFolders({storage: preferences, preferences, key: "native-1", api});
  await one.open(rig.root); await two.open("/notes"); await one.open(rig.root);
  assert.equal(two.selected.cwd, "/notes");
  assert.deepEqual(one.recent(), [rig.root, "/notes"]);
  const reopened = new WorkingFolders({storage: preferences, preferences, key: "native-1", api});
  await reopened.restore();
  assert.equal(reopened.selected.cwd, "/notes");
  assert.equal(one.selected.cwd, rig.root);
});

test("first run has no process-directory default and rejected or missing folders cannot become defaults", async () => {
  const preferences = storage();
  const folders = new WorkingFolders({storage: preferences, preferences, api});
  await folders.restore(); assert.equal(folders.selected, null);
  await folders.open(rig.root);
  await assert.rejects(folders.open("/missing"), /unavailable/);
  assert.equal(folders.selected.cwd, rig.root);
  folders.selected = {cwd: "/missing"};
  await assert.rejects(folders.restore(), /unavailable/);
  assert.equal(folders.selected, null);
  assert.equal(new WorkingFolders({storage: preferences, preferences, api}).selected, null);
});

test("folder selection works with unavailable storage and explicit UI workspace seeds first use", async () => {
  const unavailable = {getItem() { throw Error(); }, setItem() { throw Error(); }};
  const folders = new WorkingFolders({storage: unavailable, preferences: unavailable, api});
  await folders.restore(rig.root);
  assert.equal(folders.selected.cwd, rig.root);
  assert.deepEqual(folders.recent(), []);
});
