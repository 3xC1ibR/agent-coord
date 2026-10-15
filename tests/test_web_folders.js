"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {WorkingFolders, ProjectTabs, FolderPicker, newThreadContext, inProject} = require("../plugins/agent-coord/scripts/agent_coord/web/folders.js");
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

test("views inherit the active project directory and optional group, ignoring repository filters", () => {
  const folder = {cwd: "/projects/stoic", repository: stoic};
  assert.deepEqual(newThreadContext(organization, {repository: "rig", project: "__none__"}, folder),
    {cwd: stoic.root, project_id: null});
  assert.deepEqual(newThreadContext(organization, {repository: "rig", project: "evaluation"}, folder),
    {cwd: stoic.root, project_id: "evaluation"});
  assert.deepEqual(newThreadContext(organization, {project: "evaluation"}, folder),
    {cwd: stoic.root, project_id: "evaluation"});
});

test("views preserve exact project subfolders and worktrees", () => {
  for (const cwd of [rig.root + "/src", "/worktrees/feature"]) {
    const folder = {cwd, repository: rig};
    assert.equal(newThreadContext(organization, {repository: "rig"}, folder).cwd, cwd);
    assert.equal(newThreadContext(organization, {repository: "stoic"}, folder).cwd, cwd);
  }
});

test("All work and ordinary folders use the window default; missing selections never broaden filters", () => {
  assert.deepEqual(newThreadContext(organization, {}, {cwd: "/notes", repository: null}), {cwd: "/notes", project_id: null});
  assert.equal(newThreadContext(organization, {}, null).cwd, "");
  assert.throws(() => newThreadContext(organization, {project: "deleted"}, null), /group is unavailable/);
  assert.equal(newThreadContext(organization, {repository: "deleted"}, null).cwd, "");
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
  folders.projects = [folders.selected = {cwd: "/missing"}];
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

function pickerFixture() {
  const elements = new Map();
  const document = {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {value: "", replaceChildren(...children) { this.children = children; }});
    return elements.get(id);
  }};
  const preferences = storage();
  const folders = new WorkingFolders({storage: preferences, preferences, api});
  let picked = "/chosen", calls = 0, opened = 0;
  const errors = [];
  const windows = [];
  const picker = new FolderPicker({document, folders, api,
    desktop: {newWindow: async url => windows.push(url), chooseFolder: async () => { calls++; return picked; }},
    onOpened: async () => { opened++; }, onError: error => errors.push(error)});
  return {picker, folders, document, windows, select: document.getElementById("working-folder"), errors,
    setPicked: path => { picked = path; }, calls: () => calls, opened: () => opened};
}

test("dropdown chooses a native folder from first use and retains it on cancellation", async t => {
  const previous = global.Option;
  global.Option = class { constructor(text, value) { Object.assign(this, {text, value}); } };
  t.after(() => { global.Option = previous; });
  const f = pickerFixture();
  f.picker.render();
  assert.equal(f.select.value, "");
  assert.equal(f.select.children[0].disabled, true);
  const action = f.select.children.find(option => option.text === "Choose a folder…");
  assert.ok(action.value);
  f.select.value = action.value;
  await f.select.onchange();
  assert.equal(f.calls(), 1);
  assert.equal(f.folders.selected.cwd, "/chosen");
  assert.equal(f.select.value, "/chosen");
  assert.equal(f.opened(), 1);
  f.setPicked(null);
  f.select.value = action.value;
  await f.select.onchange();
  assert.equal(f.calls(), 2);
  assert.equal(f.select.value, "/chosen");
  assert.equal(f.opened(), 1);
  f.select.value = rig.root;
  await f.select.onchange();
  assert.equal(f.select.value, rig.root);
  assert.equal(f.calls(), 2);
  assert.equal(f.opened(), 2);
  f.setPicked("/missing");
  f.select.value = action.value;
  await f.select.onchange();
  assert.equal(f.select.value, rig.root);
  assert.equal(f.errors.length, 1);
});


test("project membership uses the exact launch directory even after cwd changes", () => {
  const parent = {project_cwd: "/projects", cwd: "/projects/website"};
  const child = {project_cwd: "/projects/website", cwd: "/projects"};
  assert.equal(inProject(parent, "/projects"), true);
  assert.equal(inProject(child, "/projects"), false);
  assert.equal(inProject(parent, "/projects/website"), false);
  assert.equal(inProject(child, "/projects/website"), true);
  assert.equal(inProject(parent, ""), false);
});

test("project tabs retain exact directories, restore per window, and close without session mutations", async () => {
  const memory = storage();
  const folders = new WorkingFolders({storage: memory, preferences: memory, api});
  await folders.open(rig.root); await folders.open(rig.root + "/src"); await folders.open(rig.root);
  assert.deepEqual(folders.projects.map(p => p.cwd), [rig.root, rig.root + "/src"]);
  const restored = new WorkingFolders({storage: memory, preferences: memory, api});
  await restored.restore();
  assert.equal(restored.selected.cwd, rig.root);
  restored.close(rig.root);
  assert.equal(restored.selected.cwd, rig.root + "/src");
  restored.close(rig.root + "/src");
  assert.equal(restored.selected, null);
  const empty = new WorkingFolders({storage: memory, api});
  await empty.restore();
  assert.deepEqual(empty.projects, []);
  assert.equal(empty.selected, null);
});

test("desktop project changes publish snapshots and remote replacement keeps an available selection", async () => {
  const snapshots = [];
  const desktop = new WorkingFolders({storage: storage(), api, onChange: snapshot => snapshots.push(snapshot)});
  await desktop.open(rig.root); await desktop.open(rig.root + "/src"); desktop.activate(rig.root); desktop.close(rig.root);
  assert.deepEqual(snapshots.at(-1), {projects: [rig.root + "/src"], active: rig.root + "/src"});

  const remote = new WorkingFolders({storage: storage(), api});
  await remote.open("/notes");
  await remote.replace([rig.root, "/notes"], rig.root);
  assert.deepEqual(remote.projects.map(item => item.cwd), [rig.root, "/notes"]);
  assert.equal(remote.selected.cwd, "/notes");
  await remote.replace([rig.root], rig.root);
  assert.equal(remote.selected.cwd, rig.root);
});


test("opening in a new window preserves the current project and requests an independent tab set", async () => {
  const f = pickerFixture();
  const previous = global.Option;
  global.Option = class { constructor(text, value) { Object.assign(this, {text, value}); } };
  try {
    await f.folders.open(rig.root);
    const dialog = f.document.getElementById("project-target-dialog");
    let close;
    dialog.addEventListener = (name, callback) => { close = callback; };
    dialog.showModal = () => { dialog.returnValue = "window"; close(); };
    assert.equal(await f.picker.choose(), false);
    const destination = new URL(f.windows[0], "http://localhost");
    assert.equal(destination.searchParams.get("directory"), "/chosen");
    assert.equal(destination.searchParams.get("new_project"), "1");
    assert.equal(f.folders.selected.cwd, rig.root);
    assert.equal(f.opened(), 0);
  } finally { global.Option = previous; }
});

test("a new project window can discard copied tabs without changing the originating window", async () => {
  const sourceStorage = storage(), copiedStorage = storage();
  const source = new WorkingFolders({storage: sourceStorage, api});
  await source.open(rig.root); await source.open(rig.root + "/src");
  copiedStorage.setItem(source.key, sourceStorage.getItem(source.key));
  const destination = new WorkingFolders({storage: copiedStorage, api});
  destination.reset(); await destination.open("/notes");
  assert.deepEqual(destination.projects.map(p => p.cwd), ["/notes"]);
  assert.deepEqual(source.projects.map(p => p.cwd), [rig.root, rig.root + "/src"]);
});

test("project clicks activate immediately while an earlier project's refresh is pending", async () => {
  const folders = new WorkingFolders({storage: storage(), api});
  await folders.open("/a"); await folders.open("/b"); folders.activate("/a");
  let release, renders = 0, focused = 0;
  const delayed = new Promise(resolve => { release = resolve; }), visits = [];
  const tab = {focus() { focused++; }, scrollIntoView() {}};
  const tabs = Object.assign(Object.create(ProjectTabs.prototype), {folders, canSwitch: () => true,
    doc: {getElementById: () => ({querySelector: () => tab})}, render() { renders++; },
    onSwitch() { visits.push(folders.selected.cwd); return folders.selected.cwd === "/b" ? delayed : Promise.resolve(); }});
  const first = tabs.activate("/b");
  assert.equal(folders.selected.cwd, "/b"); assert.equal(renders, 1); assert.equal(focused, 1);
  await tabs.activate("/a");
  assert.equal(folders.selected.cwd, "/a"); assert.deepEqual(visits, ["/b", "/a"]);
  release(); await first;
  assert.equal(folders.selected.cwd, "/a"); assert.equal(focused, 2, "old refreshes must not steal focus");
});

test("phones switch, open, and close projects from one header picker", async () => {
  global.threadViews = {badges: threads => ({attention: threads.filter(t => t.needs_attention).length, active: 0, completed: 0})};
  const folders = new WorkingFolders({storage: storage(), api});
  await folders.open("/p/a"); await folders.open("/p/b"); folders.activate("/p/a");
  const node = tag => ({tag, children: [], hidden: false, attrs: {}, value: "", textContent: "",
    append(...items) { this.children.push(...items); }, replaceChildren() { this.children = []; },
    setAttribute(name, value) { this.attrs[name] = value; }, removeAttribute(name) { delete this.attrs[name]; }});
  const ids = {"mobile-project": node("select"), "mobile-project-attention": node("span"), "mobile-project-activity": node("span"),
    "mobile-close-project": node("button"), "view-menu": node("details"), "open-project-tab": node("button")};
  let opened = 0, switches = 0; ids["open-project-tab"].click = () => { opened++; };
  const tabs = Object.assign(Object.create(ProjectTabs.prototype), {folders, canSwitch: () => true, onError: error => { throw error; },
    doc: {getElementById: id => ids[id] || null, createElement: node}, onSwitch: async () => { switches++; },
    activate: async cwd => { folders.activate(cwd); await tabs.onSwitch(); tabs.renderMobilePicker([]); },
    render() { this.renderMobilePicker([{project_cwd: "/p/b", needs_attention: true}, {project_cwd: "/p/a", needs_attention: true}]); }});
  tabs.bindMobilePicker(); tabs.render();
  const picker = ids["mobile-project"];
  assert.deepEqual(picker.children.map(o => [o.value, o.textContent]), [["/p/a", "a"], ["/p/b", "b · 1 need attention"], ["__open__", "Open project…"]]);
  assert.equal(picker.value, "/p/a");
  assert.equal(ids["mobile-project-attention"].textContent, "1"); assert.equal(ids["mobile-project-attention"].hidden, false);
  picker.value = "__open__"; picker.onchange();
  assert.equal(opened, 1); assert.equal(picker.value, "/p/a", "Open project… must not stay selected");
  picker.value = "/p/b"; picker.onchange(); await new Promise(setImmediate);
  assert.equal(folders.selected.cwd, "/p/b");
  ids["view-menu"].attrs.open = ""; ids["mobile-close-project"].onclick(); await new Promise(setImmediate);
  assert.equal(ids["view-menu"].attrs.open, undefined);
  assert.deepEqual(folders.projects.map(p => p.cwd), ["/p/a"]); assert.equal(folders.selected.cwd, "/p/a"); assert.equal(switches, 2);
  delete global.threadViews;
});
