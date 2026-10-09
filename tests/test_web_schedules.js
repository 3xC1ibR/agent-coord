"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {ScheduledPromptsUI, localInput, scheduledTime} = require("../plugins/agent-coord/scripts/agent_coord/web/schedules.js");

class Element {
  constructor(tag = "input") { this.tag = tag; this.children = []; this.hidden = false; this.disabled = false; this.open = false; this.listeners = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._value = undefined; }
  get options() { return this.children.flatMap(child => child.tag === "optgroup" ? child.children : [child]); }
  get value() { return this._value ?? (this.tag === "select" ? this.options[0]?.value || "" : ""); }
  set value(value) { this._value = value; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
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
  };
  let draft = {source: "source-thread", message: "Run the backfill", settings: {cwd: "/repo", model: "luna", effort: "high", yolo: true}};
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
  assert.equal(c.saved[0].source, "source-thread");
  assert.equal(c.$("schedule-dialog").open, false);
  assert.equal(c.$("schedules-dialog").open, true);
  assert.equal(c.calls.some(call => call.path.startsWith("sessions")), false);
});

test("model selection updates supported reasoning and permissions stay opt-in", async t => {
  const c = setup(t); c.setDraft({settings: {cwd: "/repo"}}); await c.ui.open();
  assert.equal(c.$("schedule-full-access").checked, false);
  c.$("schedule-model").value = JSON.stringify(["claude", "sonnet"]); c.ui.efforts("high");
  assert.deepEqual(c.$("schedule-effort").options.map(option => option.value), ["low"]);
  assert.equal(c.$("schedule-effort").value, "low");
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
  c.ui.api = async (path, body) => path === "schedules" && !body ? {data: rows} : api(path, body);
  await c.ui.showList();
  const cards = c.$("schedule-list").children;
  const buttons = card => card.children.at(-1).children;
  assert.deepEqual(buttons(cards[0]).map(button => button.textContent), ["Edit", "Cancel", "Run now"]);
  assert.deepEqual(buttons(cards[1]).map(button => button.textContent), ["Open thread"]);
  await buttons(cards[0])[2].onclick();
  assert.deepEqual(c.calls.at(-1).body, {action: "run_now", version: 7});
  await buttons(cards[1])[0].onclick(); assert.deepEqual(c.opened, ["thread-1"]);
});
