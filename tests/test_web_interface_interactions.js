"use strict";

const assert = require("node:assert/strict");
const {test} = require("node:test");
const {makeDocument} = require("./web_dom_stub.js");
const AgentTree = require("../plugins/agent-coord/scripts/agent_coord/web/agent-tree.js");
const {inProject, newThreadContext} = require("../plugins/agent-coord/scripts/agent_coord/web/folders.js");
const TurnNotifications = require("../plugins/agent-coord/scripts/agent_coord/web/notifications.js");
const {SendWorkPalette} = require("../plugins/agent-coord/scripts/agent_coord/web/send-work.js");
const grouping = require("../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js");
const {ThreadTabs} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-tabs.js");
const WorkingSet = require("../plugins/agent-coord/scripts/agent_coord/web/working-set.js");

const thread = (id, details = {}) => ({thread_id: id, title: "Task " + id, browser_session: true,
  created_at: 100, attention: "now", status: "idle", response_state: "available", ...details});
const ids = items => items.map(item => item.thread_id);
const family = [
  thread("root"), thread("lead", {supervisor_thread_id: "root"}),
  thread("analyst", {supervisor_thread_id: "lead", status: "running", response_state: "working"}),
  thread("reviewer", {supervisor_thread_id: "analyst"}),
  thread("specialist", {supervisor_thread_id: "reviewer", cwd: "/outside/repository"}),
  thread("other", {supervisor_thread_id: "reviewer", status: "needs input"}),
  thread("fork", {forked_from_thread_id: "root"}),
];

test("a deep direct open and subsequent breadcrumb switches share a single root task tab", async () => {
  const graph = AgentTree.graph(family);
  const document = makeDocument(), host = document.createElement("nav");
  const tabs = new ThreadTabs({});
  tabs.setProject("/workspace");
  tabs.open(graph.nodes.get("specialist"), graph);
  const visits = [];
  const breadcrumb = new AgentTree.Breadcrumb({document, host, onSelect: id => {
    visits.push(id);
    tabs.selectConversation(tabs.selected, id);
    breadcrumb.update(graph, id);
  }});
  breadcrumb.update(graph, "specialist");
  assert.deepEqual(ids(tabs.items), ["root"]);
  assert.equal(tabs.items[0].root_thread_id, "root");
  assert.equal(tabs.items[0].title, "Task root");
  assert.equal(tabs.conversationFor("root"), "specialist");
  assert.deepEqual(host.byClass("agent-breadcrumb-step").map(step => step.dataset.agentThread),
    ["root", "lead", "analyst", "reviewer", "specialist"]);
  assert.equal(host.byClass("agent-breadcrumb-step").at(-1).getAttribute("aria-current"), "page");
  const switcher = host.byTag("details")[0];
  switcher.open = true;
  const option = host.byClass("agent-breadcrumb-option").find(item => item.dataset.agentThread === "other");
  assert.equal(option.byClass("agent-breadcrumb-status")[0].dataset.status, "waiting");
  await option.onclick();
  assert.deepEqual(visits, ["other"]);
  assert.equal(host.byTag("details")[0].open, false);
  assert.equal(tabs.conversationFor("root"), "other");
  assert.equal(tabs.items[0].title, "Task root");
  await host.byClass("agent-breadcrumb-step")[1].onclick();
  assert.deepEqual(visits, ["other", "lead"]);
  assert.equal(tabs.items[0].title, "Task root");
  assert.equal(tabs.conversationFor("root"), "lead");
  breadcrumb.update(graph, "fork");
  assert.equal(host.hidden, true, "fork provenance does not turn an independent task into an agent");
});

test("restored provisional agent tabs merge under their root without losing the selected agent", () => {
  const values = new Map();
  const storage = {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value)};
  const restored = new ThreadTabs({storage});
  const cwd = "/workspace";
  const key = restored.baseKey + ":" + cwd;
  values.set(key, JSON.stringify({items: [family[4], family[0], family[6]], selected: "specialist"}));
  restored.setProject(cwd);
  const graph = AgentTree.graph(family);
  assert.equal(restored.reconcileFamily(graph), true);
  assert.deepEqual(ids(restored.items), ["root", "fork"]);
  assert.equal(restored.destination(), "root");
  assert.equal(restored.conversationFor("root"), "specialist");
  restored.update([thread("specialist", {title: "Renamed specialist"}), family[0]]);
  assert.equal(restored.items[0].title, "Task root");
  const reloaded = new ThreadTabs({storage});
  reloaded.setProject(cwd);
  assert.equal(reloaded.destination(), "root");
  assert.equal(reloaded.conversationFor("root"), "specialist");
  assert.equal(reloaded.reconcileFamily(graph), false);
});

test("Attention compares severity before pins and time, and keeps actionable blockers after informational reads", () => {
  const now = 1_000_000;
  const pending = {needs_attention: true, can_handle_response: true, unhandled_response: true, unread_result: true};
  const item = (id, age, fields) => thread(id, {...pending, attention_since: now - age, ...fields});
  const items = [
    item("done", 1, {response_state: "update", attention_reason: "done"}),
    item("review", 40, {response_state: "action", attention_reason: "review"}),
    item("reply", 3, {response_state: "reply"}),
    item("approval", 60, {status: "needs input", response_state: "input", can_handle_response: false}),
    item("failure", 1000, {response_state: "failed"}),
    item("update", 10, {response_state: "update"}),
    item("blocking-ask", 300, {status: "needs input", open_ask: {id: 8, blocking: true}}),
    item("stalled", 2000, {status: "stalled", response_state: "action", can_handle_response: false}),
  ];
  const order = ["blocking-ask", "failure", "stalled", "review", "approval", "update", "reply", "done"];
  assert.deepEqual(ids(grouping.groupThreads(items).priority), order);
  assert.deepEqual(ids(WorkingSet.derive(items)[0].levels[0]), order);
  assert.deepEqual(ids(WorkingSet.listSections(items, {now}).sections.attention), order);
  const pinned = {...items[5], pinned: true, attention_since: now - 6000};
  assert.ok(grouping.attentionOrder(pinned, items[5]) < 0);
  assert.ok(grouping.attentionOrder(pinned, items[1]) > 0);
  const afterReading = items.map(item => ["reply", "update", "done"].includes(item.thread_id)
    ? {...item, unread_result: false, unhandled_response: false, can_handle_response: false, needs_attention: false, response_state: "available"}
    : {...item, unread_result: false});
  assert.deepEqual(ids(WorkingSet.listSections(afterReading, {now}).sections.attention), order.slice(0, 5));
  assert.equal(grouping.actionFor(afterReading[1]), "dismiss");
  assert.equal(grouping.actionFor(afterReading[2]), null);
  assert.equal(grouping.actionFor(afterReading[3]), null);
  assert.equal(grouping.actionFor(afterReading[6]), null);
});

test("workspace group membership can span repositories without retargeting session launch", () => {
  const workspace = {cwd: "/features/reporting", name: "Reporting"};
  const group = {id: "reporting", cwd: workspace.cwd};
  const member = thread("member", {project_id: group.id, project_launch_cwd: workspace.cwd,
    project_cwd: "/repo/backend", cwd: "/repo/backend/src"});
  const delegated = thread("delegate", {orchestration_workspaces: [workspace.cwd], cwd: "/repo/frontend"});
  const unrelated = thread("unrelated", {cwd: workspace.cwd + "-old"});
  assert.equal(inProject(member, workspace), true);
  assert.equal(inProject(delegated, workspace.cwd), true);
  assert.equal(inProject(unrelated, workspace), false);
  assert.deepEqual(newThreadContext({projects: [group]}, {project: group.id}, {cwd: "/repo/another"}),
    {cwd: "/repo/another", project_id: group.id});
  const dispatcher = {...member, thread_id: "dispatcher", dispatcher: true};
  assert.deepEqual(SendWorkPalette.matches([member, delegated, unrelated, dispatcher], workspace.cwd, "").map(item => item.id),
    ["member", "delegate"]);
  assert.deepEqual(SendWorkPalette.matches([member], {cwd: "/repo/another", project_id: group.id}, "").map(item => item.id), ["member"]);
});

test("Send work transmits group context for an explicit member and keeps the receipt key on retry", async () => {
  require("../plugins/agent-coord/scripts/agent_coord/web/dispatch.js");
  const requests = [], storage = new Map();
  const member = thread("destination", {project_id: "reporting", cwd: "/repo/backend"});
  const palette = Object.assign(Object.create(SendWorkPalette.prototype), {
    active: 0, cwd: "/features/reporting", group: "reporting", input: {value: "Review the measurements"},
    error: {}, rows: [{kind: "thread", id: member.thread_id}], threads: () => [member],
    cancelSuggestions() {}, render() {}, close() {}, showReceipt() {}, pollReceipt() {},
    drafts: new Map(),
    storage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key)},
    api: async (path, body) => { requests.push({path, body}); throw new Error("Receipt unavailable"); },
  });
  await palette.send();
  palette.group = null;
  await palette.send();
  assert.equal(requests[0].path, "send-work/send");
  assert.deepEqual(requests[0].body.destination, {kind: "thread", id: member.thread_id});
  assert.equal(requests[0].body.project_id, "reporting");
  assert.equal(requests[1].body.project_id, undefined);
  assert.equal(requests[1].body.requestId, requests[0].body.requestId);
  palette.locked = {kind: "dispatcher"};
  palette.group = "reporting";
  await palette.send();
  assert.deepEqual(requests[2].body.destination, {kind: "dispatcher"});
  assert.equal(requests[2].body.project_id, undefined);
});

test("quiet submission shortcut suppresses only its clean completion", async () => {
  const keyboard = {key: "Enter", altKey: true, metaKey: true};
  assert.equal(TurnNotifications.isQuietShortcut(keyboard), true);
  for (const extra of [{shiftKey: true}, {ctrlKey: true}, {isComposing: true}, {keyCode: 229}, {repeat: true}, {altKey: false}])
    assert.equal(TurnNotifications.isQuietShortcut({...keyboard, ...extra}), false);
  const emitted = [], claimed = [];
  const client = Object.assign(Object.create(TurnNotifications.prototype), {
    enabled: () => true, viewing: () => false,
    claim: async id => { claimed.push(id); return true; },
    claimApproval: async id => { claimed.push(id); return true; },
    env: {Notification: class { constructor(title) { emitted.push(title); } }},
  });
  await client.deliver({id: 1, status: "completed", thread_id: "a", quiet: true});
  await client.deliver({id: 2, status: "failed", thread_id: "a", quiet: true});
  await client.deliver({request_key: "approval", status: "approval", thread_id: "a", quiet: true});
  await client.deliver({id: 3, status: "completed", thread_id: "a"});
  assert.deepEqual(claimed, [1, 2, "approval", 3]);
  assert.deepEqual(emitted, ["Ribbon Field · Turn failed", "Ribbon Field · Codex is requesting approval", "Ribbon Field · Turn finished"]);
});
