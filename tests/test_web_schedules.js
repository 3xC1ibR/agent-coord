"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {ScheduledPromptsUI, localInput, scheduledTime, upcomingPrompts} = require("../plugins/agent-coord/scripts/agent_coord/web/schedules.js");

class Element {
  constructor(tag = "input") { this.tag = tag; this.children = []; this.hidden = false; this.disabled = false; this.open = false; this.listeners = {}; this.dataset = {}; this.attributes = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._value = undefined; }
  get options() { return this.children.flatMap(child => child.tag === "optgroup" ? child.children : [child]); }
  get value() { return this._value ?? (this.tag === "select" ? this.options[0]?.value || "" : ""); }
  set value(value) { this._value = value; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.close?.(); }
}
function setup(t) {
  const elements = new Map(), calls = [], saved = [], opened = [];
  const document = {getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element(["schedule-model", "schedule-effort"].includes(id) ? "select" : "input"));
    return elements.get(id);
  }, createElement: tag => new Element(tag)};
  const catalogs = {
    codex: [{model: "luna", displayName: "Luna", isDefault: true, defaultReasoningEffort: "medium", supportedReasoningEfforts: [{reasoningEffort: "medium"}, {reasoningEffort: "high"}]}],
    claude: [{model: "sonnet", defaultReasoningEffort: "low", supportedReasoningEfforts: [{reasoningEffort: "low"}]}],
    antigravity: [{model: "gemini-test", displayName: "Gemini Test", supportedReasoningEfforts: []}],
    kimi: [{model: "k3", displayName: "K3", defaultReasoningEffort: "high", supportedReasoningEfforts: [{reasoningEffort: "low"}, {reasoningEffort: "high"}, {reasoningEffort: "max"}]}],
    muse: [{model: "muse-wonder-1.0", displayName: "Muse Wonder", defaultReasoningEffort: "high", supportedReasoningEfforts: [{reasoningEffort: "high"}, {reasoningEffort: "max"}]}],
  };
  let draft = {message: "Run the backfill", settings: {cwd: "/repo", model: "luna", effort: "high", yolo: true}};
  const ui = new ScheduledPromptsUI({document, getDraft: () => draft, onSaved: item => saved.push(item),
    openThread: async id => opened.push(id), onError: error => { throw error; },
    api: async (path, body) => {
      calls.push({path, body});
      if (path.startsWith("models?client=")) return {data: catalogs[path.split("=")[1]]};
      return body ? {...body, version: 1, state: "scheduled"} : {data: []};
    }});
  t.after(() => { clearInterval(ui.timer); });
  return {ui, calls, saved, opened, catalogs, $: ui.$, setDraft: next => { draft = next; }};
}

test("date input handles midnight and rejects past dates and nonexistent DST times", () => {
  const previous = process.env.TZ; process.env.TZ = "America/Chicago";
  try {
    const now = new Date(2026, 9, 8, 23, 0).getTime();
    assert.equal(localInput(new Date(2026, 9, 9, 0, 0)), "2026-10-09T00:00");
    assert.equal(scheduledTime("2026-10-09T00:00", now), new Date(2026, 9, 9, 0, 0).getTime() / 1000);
    for (const value of ["", "invalid", "2026-10-08T22:00", "2027-03-14T02:30"]) assert.throws(() => scheduledTime(value, now), /future/);
  } finally { if (previous === undefined) delete process.env.TZ; else process.env.TZ = previous; }
});

test("saving snapshots selected model, effort, workspace, permission, and date without starting a session", async t => {
  const c = setup(t); await c.ui.open();
  assert.equal(c.$("schedule-model").value, JSON.stringify(["codex", "luna"]));
  assert.equal(c.$("schedule-effort").value, "high");
  assert.equal(c.$("schedule-full-access").checked, true);
  c.setDraft({message: "Another thread", settings: {cwd: "/elsewhere"}});
  await c.ui.save();
  const post = c.calls.find(call => call.path === "schedules" && call.body);
  assert.equal(post.body.message, "Run the backfill");
  assert.deepEqual(post.body.settings, {cwd: "/repo", client: "codex", model: "luna", effort: "high", yolo: true});
  assert.equal(post.body.timezone, Intl.DateTimeFormat().resolvedOptions().timeZone);
  assert.ok(post.body.run_at > Date.now() / 1000);
  assert.equal(post.body.target_thread_id, undefined);
  assert.equal(c.$("schedule-dialog").open, false);
  assert.equal(c.$("schedules-dialog").open, true);
  assert.equal(c.calls.some(call => call.path.startsWith("sessions")), false);
});

test("existing-thread schedules save the original destination and exact draft without settings", async t => {
  const c = setup(t);
  c.setDraft({source: "source-thread", message: "  continue\n ", settings: {cwd: "/repo", model: "removed"}});
  await c.ui.open();
  assert.match(c.$("schedule-destination").textContent, /existing thread/);
  assert.equal(c.$("schedule-settings").hidden, true);
  assert.equal(c.$("schedule-model").disabled, true);
  assert.equal(c.$("schedule-save").disabled, false);
  assert.equal(c.calls.length, 0);
  c.setDraft({source: "different-thread", message: "Different", settings: {cwd: "/elsewhere"}});
  await c.ui.save();
  const post = c.calls.find(call => call.path === "schedules" && call.body);
  assert.equal(post.body.target_thread_id, "source-thread");
  assert.equal(post.body.message, "  continue\n ");
  assert.equal(post.body.settings, undefined);
  assert.equal(c.saved[0].source, "source-thread");
});

test("editing an existing-thread schedule preserves destination independently of selected conversation", async t => {
  const c = setup(t);
  await c.ui.open({id: "saved-id", target_thread_id: "original-thread", thread_id: "original-thread", version: 2,
    message: "continue", run_at: Date.now() / 1000 + 86400, settings: {cwd: "/repo"}});
  await c.ui.save();
  const post = c.calls.find(call => call.path === "schedules/saved-id");
  assert.equal(post.body.target_thread_id, "original-thread");
  assert.equal(post.body.settings, undefined);
  c.setDraft({settings: {cwd: "/repo"}});
  await c.ui.open();
  assert.equal(c.$("schedule-settings").hidden, false);
  assert.equal(c.$("schedule-model").disabled, false);
});

test("model selection updates supported reasoning and permissions stay opt-in", async t => {
  const c = setup(t); c.setDraft({settings: {cwd: "/repo"}}); await c.ui.open();
  assert.equal(c.$("schedule-full-access").checked, false);
  c.$("schedule-model").value = JSON.stringify(["claude", "sonnet"]); c.ui.efforts("high");
  assert.deepEqual(c.$("schedule-effort").options.map(option => option.value), ["low"]);
  assert.equal(c.$("schedule-effort").value, "low");
  c.$("schedule-model").value = JSON.stringify(["kimi", "k3"]); c.ui.efforts("max");
  assert.deepEqual(c.$("schedule-effort").options.map(option => option.value), ["low", "high", "max"]);
  await c.ui.save();
  const post = c.calls.find(call => call.path === "schedules" && call.body);
  assert.equal(post.body.settings.client, "kimi");
  assert.equal(post.body.settings.effort, "max");
});

test("Muse schedules retain the chosen native model and reasoning effort", async t => {
  const c = setup(t);
  c.setDraft({message: "Review changes", settings: {cwd: "/repo", client: "muse", model: "muse-wonder-1.0", effort: "max"}});
  await c.ui.open();
  assert.equal(c.$("schedule-model").value, JSON.stringify(["muse", "muse-wonder-1.0"]));
  assert.deepEqual(c.$("schedule-effort").options.map(option => option.value), ["high", "max"]);
  await c.ui.save();
  assert.deepEqual(c.calls.find(call => call.path === "schedules" && call.body).body.settings,
    {cwd: "/repo", client: "muse", model: "muse-wonder-1.0", effort: "max", yolo: false});
});

test("unavailable saved model requires a new explicit selection", async t => {
  const c = setup(t); c.setDraft({settings: {cwd: "/repo", model: "removed"}}); await c.ui.open();
  assert.equal(c.$("schedule-model").value, "");
  assert.equal(c.$("schedule-save").disabled, true);
  assert.match(c.$("schedule-error").textContent, /unavailable/);
});

test("lost save response keeps form and stable request ID for safe retry", async t => {
  const c = setup(t); await c.ui.open(); const api = c.ui.api; let fail = true;
  c.ui.api = async (path, body) => { const result = await api(path, body); if (body && fail) throw new Error("Response lost"); return result; };
  await c.ui.save();
  assert.equal(c.$("schedule-dialog").open, true);
  assert.equal(c.$("schedule-message").value, "Run the backfill");
  assert.equal(c.$("schedule-error").textContent, "Response lost");
  fail = false; await c.ui.save();
  const posts = c.calls.filter(call => call.path === "schedules" && call.body);
  assert.equal(posts.length, 2);
  assert.deepEqual(posts[0].body, posts[1].body);
});

test("editing targets the original schedule and carries its version", async t => {
  const c = setup(t);
  await c.ui.open({id: "saved-id", version: 4, message: "Old prompt", run_at: Date.now() / 1000 + 86400,
    settings: {cwd: "/saved", client: "codex", model: "luna", effort: "high", yolo: false}});
  c.$("schedule-message").value = "Edited prompt"; await c.ui.save();
  const post = c.calls.find(call => call.path === "schedules/saved-id");
  assert.equal(post.body.version, 4); assert.equal(post.body.action, "edit");
  assert.equal(post.body.message, "Edited prompt");
});

test("attachments are not silently discarded", async t => {
  const c = setup(t); c.setDraft({settings: {cwd: "/repo"}, images: [{name: "image.png"}]});
  await assert.rejects(c.ui.open(), /Remove the attachments/);
  assert.equal(c.calls.length, 0);
});

test("history offers run now only for missed prompts and preserves review-only outcomes", async t => {
  const c = setup(t), api = c.ui.api;
  const rows = ["missed", "review", "completed"].map((state, i) => ({id: "job-" + i, version: 7, message: state,
    state, run_at: Date.now() / 1000, timezone: "UTC", cwd: "/repo", settings: {model: "luna"}, thread_id: i ? "thread-" + i : null}));
  c.ui.api = async (path, body) => {
    if (path === "schedules" && !body) return {data: rows};
    await api(path, body);
    return {...rows.find(item => path === "schedules/" + item.id), state: "scheduled"};
  };
  await c.ui.showList();
  const cards = c.$("schedule-list").children;
  const buttons = card => card.children.at(-1).children;
  assert.deepEqual(buttons(cards[0]).map(button => button.textContent), ["Edit", "Cancel", "Run now"]);
  assert.deepEqual(buttons(cards[1]).map(button => button.textContent), ["Open thread"]);
  await buttons(cards[0])[2].onclick();
  assert.deepEqual(c.calls.at(-1).body, {action: "run_now", version: 7});
  await buttons(cards[1])[0].onclick(); assert.deepEqual(c.opened, ["thread-1"]);
});

const scheduled = (id, extra = {}) => ({id, version: 1, message: "Continue " + id, state: "scheduled",
  run_at: 1800000000, timezone: "America/Chicago", cwd: "/repo", settings: {model: "luna"}, ...extra});
const textContent = element => [element.textContent || "", ...element.children.map(textContent)].join(" ");
const buttons = card => card.children.at(-1).children;

test("upcoming includes new and existing threads in run order, including waiting and starting prompts", () => {
  const rows = [scheduled("later", {run_at: 1900000000}), scheduled("target", {target_thread_id: "thread-a"}),
    scheduled("waiting", {state: "waiting", run_at: 1700000000, target_thread_id: "thread-a"}),
    scheduled("starting", {state: "starting", run_at: 1750000000}),
    ...["running", "completed", "cancelled", "failed", "missed", "review"].map(state => scheduled(state, {state}))];
  const original = rows.slice();
  assert.deepEqual(upcomingPrompts(rows).map(item => item.id), ["waiting", "starting", "target", "later"]);
  assert.deepEqual(upcomingPrompts(rows, {threadId: "thread-a"}).map(item => item.id), ["waiting", "target"]);
  assert.deepEqual(upcomingPrompts(rows, {matches: item => item.run_at > 1800000000}).map(item => item.id), ["later"]);
  assert.deepEqual(rows, original, "selection never reorders the shared history");
});

test("chat shows every upcoming prompt for its destination and clears on thread switch or delivery", t => {
  const c = setup(t), area = new Element("section");
  c.ui.update([scheduled("one", {target_thread_id: "a"}), scheduled("two", {state: "waiting", target_thread_id: "a"}),
    scheduled("elsewhere", {target_thread_id: "b"}), scheduled("new")]);
  c.ui.renderThread(area, "a");
  assert.equal(area.hidden, false);
  assert.equal(area.children[0].textContent, "Scheduled prompts");
  assert.match(textContent(area), /Continue one/);
  assert.match(textContent(area), /Waiting for thread/);
  assert.match(textContent(area), /America\/Chicago/);
  assert.doesNotMatch(textContent(area), /elsewhere|Continue new/);
  assert.deepEqual(buttons(area.children[1]).map(button => button.textContent), ["Edit", "Cancel"]);
  const retained = area.children[1]; c.ui.renderThread(area, "a");
  assert.equal(area.children[1], retained, "polling preserves focused controls when unchanged");
  c.ui.renderThread(area, "b");
  assert.match(textContent(area), /Continue elsewhere/);
  assert.doesNotMatch(textContent(area), /Continue one/);
  c.ui.update(c.ui.items.map(item => ({...item, state: "running"})));
  c.ui.renderThread(area, "b"); assert.equal(area.hidden, true);
  c.ui.renderThread(area, null); assert.equal(area.children.length, 0);
});

test("Later cards display destination, time, actions, and can open an existing thread", async t => {
  const c = setup(t), area = new Element("section");
  c.ui.update([scheduled("new"), scheduled("existing", {target_thread_id: "a", thread_id: "a", run_at: 1700000000})]);
  c.ui.renderUpcoming(area, c.ui.upcoming());
  const cards = area.children[0].children[1].children;
  assert.match(textContent(cards[0]), /Existing thread/);
  assert.match(textContent(cards[1]), /New thread/);
  assert.match(textContent(cards[1]), /America\/Chicago/);
  await buttons(cards[0]).find(button => button.textContent === "Open thread").onclick();
  assert.deepEqual(c.opened, ["a"]);
  const empty = new Element(); c.ui.renderUpcoming(empty, []); assert.equal(empty.children.length, 0);
});

test("cancellation updates chat and Later immediately and rejects an older in-flight snapshot", async t => {
  const c = setup(t), area = new Element(), item = scheduled("cancel", {target_thread_id: "a"});
  let staleResolve, freshResolve, changes = 0;
  c.ui.onChange = () => { changes++; c.ui.renderThread(area, "a"); };
  c.ui.update([item]);
  c.ui.api = (path, body) => body ? Promise.resolve({...item, state: "cancelled", version: 2}) :
    new Promise(resolve => { if (!staleResolve) staleResolve = resolve; else freshResolve = resolve; });
  const stale = c.ui.refresh({background: true});
  const cancelled = buttons(area.children[1]).find(button => button.textContent === "Cancel").onclick();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(area.hidden, true);
  assert.deepEqual(c.ui.upcoming(), []);
  staleResolve({data: [item]}); await stale;
  assert.deepEqual(c.ui.upcoming(), [], "old response cannot resurrect the cancelled prompt");
  freshResolve({data: [{...item, state: "cancelled", version: 2}]}); await cancelled;
  assert.equal(changes, 2);
});

test("background reads coalesce, and explicit refreshes supersede old reads", async t => {
  const c = setup(t), reads = [];
  c.ui.api = () => new Promise(resolve => reads.push(resolve));
  const old = c.ui.refresh({background: true}), polling = c.ui.refresh({background: true});
  assert.equal(reads.length, 1);
  const latest = c.ui.refresh();
  reads[1]({data: [scheduled("latest")]}); await latest;
  reads[0]({data: [scheduled("stale")]}); await Promise.all([old, polling]);
  assert.deepEqual(c.ui.upcoming().map(item => item.id), ["latest"]);
});

test("inline action errors are visible through the app error handler", async t => {
  const c = setup(t), errors = [];
  c.ui.onError = error => errors.push(error.message);
  c.ui.api = async () => { throw new Error("Schedule changed; refresh and try again."); };
  const card = c.ui.card(scheduled("conflict"));
  await buttons(card).find(button => button.textContent === "Cancel").onclick();
  assert.deepEqual(errors, ["Schedule changed; refresh and try again."]);
});

test("opening a thread does not suppress its newly visible cancel action while navigation finishes", async t => {
  const c = setup(t), item = scheduled("navigate", {target_thread_id: "a", thread_id: "a"});
  let finish;
  c.ui.openThread = () => new Promise(resolve => { finish = resolve; });
  c.ui.api = async (path, body) => body ? {...item, state: "cancelled"} : {data: [{...item, state: "cancelled"}]};
  c.ui.update([item]);
  const opening = buttons(c.ui.card(item)).find(button => button.textContent === "Open thread").onclick();
  await buttons(c.ui.card(item, {compact: true})).find(button => button.textContent === "Cancel").onclick();
  assert.deepEqual(c.ui.upcoming(), []);
  finish(); await opening;
});

test("Later schedule selection follows workspace and view filters without moving the destination thread", () => {
  const fs = require("node:fs"), vm = require("node:vm");
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const thread = {thread_id: "a", cwd: "/repo", title: "Checkout", attention: "now", project_id: "shop", work_phase: "implementation"};
  const rows = [scheduled("existing", {target_thread_id: "a"}), scheduled("new"), scheduled("elsewhere", {cwd: "/other"})];
  const controls = {view: {value: "later"}, "phase-filter": {value: ""}, project: {value: ""}, repository: {value: ""}};
  const context = {state: {sessions: [thread], folders: {selected: {cwd: "/repo"}},
    schedules: {upcoming: options => upcomingPrompts(rows, options)}}, $: id => controls[id],
    threadViews: require("../plugins/agent-coord/scripts/agent_coord/web/views.js").threadViews,
    inProject: require("../plugins/agent-coord/scripts/agent_coord/web/folders.js").inProject};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function upcomingSchedules("), source.indexOf("function agentThreadTree(")), context);
  const ids = query => Array.from(context.upcomingSchedules(query), item => item.id);
  assert.deepEqual(ids(""), ["existing", "new"]);
  assert.equal(thread.attention, "now", "displaying a prompt never parks its thread");
  assert.deepEqual(ids("checkout"), ["existing"]);
  controls.project.value = "shop"; assert.deepEqual(ids(""), ["existing"]);
  controls.project.value = ""; controls["phase-filter"].value = "implementation";
  assert.deepEqual(ids(""), ["existing"]);
  controls["phase-filter"].value = ""; controls.view.value = "active";
  assert.deepEqual(ids(""), []);
  controls.view.value = "later"; context.state.folders.selected.cwd = "/other";
  assert.deepEqual(ids(""), ["elsewhere"]);
});
