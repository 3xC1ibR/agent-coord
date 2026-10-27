"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {createThreadHover} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-hover.js");

class Element {
  constructor() { this.children = []; this.handlers = {}; this.attributes = {}; this.dataset = {}; this.style = {}; this.isConnected = true; this.offsetWidth = 420; this.offsetHeight = 400; }
  append(...items) { this.children.push(...items); for (const item of items) item.parent = this; }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  contains(el) { return el === this || this.children.some(child => child.contains(el)); }
  closest(selector) { if (selector === '[hidden], [aria-hidden="true"]') return null; return this.dataset.thread ? this : this.parent?.closest(selector); }
  matches(selector) { return selector === ":hover" && !!this.hovered; }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return {top: 600, right: 950, bottom: 720, left: 600}; }
  get text() { return (this.textContent || "") + this.children.map(c => c.text).join("\n"); }
  get html() { return (this.innerHTML || "") + this.children.map(c => c.html).join("\n"); }
}
function setup() {
  const overview = new Element(), document = new Element(), window = new Element();
  document.body = new Element(); document.createElement = () => new Element();
  window.innerWidth = 1000; window.innerHeight = 800;
  window.MutationObserver = class { constructor(callback) { window.mutation = callback; } observe() {} };
  const first = new Element(), second = new Element(); first.dataset.thread = "first"; second.dataset.thread = "second";
  overview.append(first, second);
  overview.querySelectorAll = () => overview.children;
  let tasks = new Map(), sequence = 0, requests = [], opened = [];
  const threads = {};
  const result = createThreadHover({overview, document, window,
    schedule: fn => { tasks.set(++sequence, fn); return sequence; }, cancel: id => tasks.delete(id),
    getThread: id => threads[id] || ({thread_id: id, title: id, cwd: "/workspace", checkpoint: {summary: "Saved progress"}}),
    relativeTime: () => "Just now", select: id => opened.push(id),
    loadPreview: id => new Promise((resolve, reject) => requests.push({id, resolve, reject}))});
  return {...result, overview, document, window, first, second, requests, threads, opened,
    async flush() { for (const [id, fn] of [...tasks]) if (tasks.delete(id)) fn(); await Promise.resolve(); },
    enter(target, type = "pointerover") { overview.handlers[type]({target, type, pointerType: "mouse"}); },
    leave(target) { overview.handlers.pointerout({target, relatedTarget: null}); }};
}
const response = id => ({thread: {title: id, client: "codex", checkpoint: {}}, latest_message: {role: "assistant", text: "<script>literal message</script>"}});

test("brief hover makes no request; focus opens and Escape dismisses", async () => {
  const s = setup(); s.enter(s.first); s.leave(s.first); await s.flush();
  assert.equal(s.requests.length, 0);
  s.document.activeElement = s.first; s.enter(s.first, "focusin"); await s.flush();
  assert.equal(s.requests.length, 1); assert.equal(s.panel.hidden, false);
  assert.equal(s.first.attributes["aria-describedby"], "thread-hover");
  s.document.handlers.keydown({key: "Escape"});
  assert.equal(s.panel.hidden, true); assert.equal(s.first.attributes["aria-describedby"], undefined);
  s.requests[0].resolve(response("first")); await Promise.resolve();
  assert.equal(s.panel.hidden, true);
});

test("repeated card exits and unrelated Focus controls cannot cancel a new preview", async () => {
  const s = setup();
  s.enter(s.first);
  s.leave(s.first); s.leave(s.first);
  s.enter(s.second);
  s.leave(s.first); // A focusout from a previous card/control can follow pointerover.
  await s.flush();
  assert.equal(s.panel.hidden, false);
  assert.equal(s.requests.length, 1);
  assert.equal(s.requests[0].id, "second");
  assert.equal(s.second.attributes["aria-describedby"], "thread-hover");
});

test("returning from a card control before the preview opens restarts its hover delay", async () => {
  const s = setup();
  s.enter(s.first); s.leave(s.first); s.enter(s.first);
  await s.flush();
  assert.equal(s.panel.hidden, false);
  assert.equal(s.requests.length, 1);
  assert.equal(s.requests[0].id, "first");
});

test("late responses cannot overwrite another card and raw HTML is escaped", async () => {
  const s = setup(); s.enter(s.first); await s.flush(); s.enter(s.second); await s.flush();
  s.requests[1].resolve(response("second")); await Promise.resolve();
  s.requests[0].resolve(response("first")); await Promise.resolve();
  assert.match(s.panel.text, /second/); assert.doesNotMatch(s.panel.text, /first/);
  assert.equal(s.panel.children[0].className, "hover-title");
  assert.equal(s.panel.children.some(el => el.className === "hover-status"), false);
  assert.match(s.panel.html, /&lt;script&gt;literal message&lt;\/script&gt;/);
  assert.doesNotMatch(s.panel.html, /<script>/);
  assert.equal(s.panel.style.left, "170px"); assert.equal(s.panel.style.top, "388px");
});

test("checkpoint and message bodies render safe Markdown with truncation preserved", async () => {
  const s = setup(); s.enter(s.first); await s.flush();
  s.requests[0].resolve({
    thread: {title: "Preview", client: "claude", checkpoint: {
      summary: "## Progress\n\n**Ready** and *verified*.\n\n- First\n- Second",
      next_action: "Open [review](https://example.com) and run `check`.", next_actor: "user"
    }},
    latest_message: {role: "assistant", text: "#### Details\n\n```js\nconst ready = true;\n```\n\n[unsafe](javascript:alert(1))", truncated: true}
  });
  await Promise.resolve();
  const summary = s.panel.children.find(el => el.className === "hover-summary markdown");
  assert.match(summary.innerHTML, /<h2>Progress<\/h2>/);
  assert.match(summary.innerHTML, /<strong>Ready<\/strong> and <em>verified<\/em>/);
  assert.match(summary.innerHTML, /<ul><li><p>First<\/p><\/li><li><p>Second<\/p><\/li><\/ul>/);
  const next = s.panel.children.filter(el => el.className === "hover-summary markdown")[1];
  assert.match(next.innerHTML, /href="https:\/\/example.com"/);
  assert.match(next.innerHTML, /<code>check<\/code>/);
  const section = s.panel.children.find(el => el.className === "hover-message");
  const body = section.children.find(el => el.className === "hover-message-text markdown");
  assert.match(body.innerHTML, /<h4>Details<\/h4>/);
  assert.match(body.innerHTML, /<pre><code>const ready = true;<\/code><\/pre>/);
  assert.doesNotMatch(body.innerHTML, /javascript:/);
  assert.match(body.innerHTML, /…/);
  assert.match(section.text, /Claude/);
  assert.match(section.text, /Open the thread to read more/);
});

test("stale checkpoint requests are not presented as the current user next step", async () => {
  const s = setup(); s.enter(s.first); await s.flush();
  s.requests[0].resolve({thread: {title: "Calendar editing", checkpoint_stale: true,
    checkpoint: {summary: "Waiting for credentials.", next_actor: "user", next_action: "Run aws login."}},
    latest_message: null});
  await Promise.resolve();
  assert.match(s.panel.html, /Waiting for credentials/);
  assert.match(s.panel.text, /New activity since this checkpoint/);
  assert.doesNotMatch(s.panel.text, /Your next step/);
  assert.doesNotMatch(s.panel.html, /Run aws login/);
});

test("preview stays open while moving onto it and closes after leaving", async () => {
  const s = setup(); s.enter(s.first); await s.flush();
  s.leave(s.first); s.panel.handlers.pointerenter(); await s.flush();
  assert.equal(s.panel.hidden, false);
  s.panel.handlers.pointerleave(); await s.flush(); assert.equal(s.panel.hidden, true);
});

test("overview refresh keeps the preview anchored to the hovered replacement card", async () => {
  const s = setup(); s.enter(s.first); await s.flush();
  const replacement = new Element(); replacement.dataset.thread = "first";
  // A newly inserted card may not match :hover until the next browser paint.
  s.document.handlers.pointermove({clientX: 50, clientY: 60, pointerType: "mouse"});
  s.document.elementFromPoint = () => replacement;
  s.first.isConnected = false; s.overview.children[0] = replacement;
  s.refresh(); s.window.mutation();
  assert.equal(s.panel.hidden, false);
  assert.equal(replacement.attributes["aria-describedby"], "thread-hover");
  s.requests[0].resolve(response("updated")); await Promise.resolve();
  assert.match(s.panel.text, /updated/);
  s.leave(replacement); await s.flush(); assert.equal(s.panel.hidden, true);
});

test("replacement before the hover delay opens only if the same card remains hovered", async () => {
  const s = setup(); s.enter(s.first);
  const replacement = new Element(); replacement.dataset.thread = "first"; replacement.hovered = true;
  s.first.isConnected = false; s.overview.children[0] = replacement; s.window.mutation();
  await s.flush(); assert.equal(s.panel.hidden, false); assert.equal(s.requests[0].id, "first");
  replacement.isConnected = false;
  const unhovered = new Element(); unhovered.dataset.thread = "first"; s.overview.children[0] = unhovered;
  s.window.mutation(); assert.equal(s.panel.hidden, true);
});

test("failed requests retain useful context without global errors; click dismisses", async () => {
  const s = setup(); s.enter(s.first); await s.flush(); s.requests[0].reject(new Error("offline")); await Promise.resolve();
  assert.match(s.panel.html, /<p>Saved progress<\/p>/); assert.match(s.panel.text, /Latest message unavailable/);
  s.overview.handlers.click({target: s.overview}); assert.equal(s.panel.hidden, true);
});

const find = (el, cls) => (el.className || "").split(" ").includes(cls) ? el : el.children.map(child => find(child, cls)).find(Boolean);
const findAll = (el, cls) => ((el.className || "").split(" ").includes(cls) ? [el] : []).concat(...el.children.map(child => findAll(child, cls)));

test("the lens says what the agent is doing now, what came earlier, and lists its agents; a child can be peeked", async () => {
  const s = setup(); s.enter(s.first); await s.flush();
  s.requests[0].resolve({
    thread: {thread_id: "first", title: "Parent", client: "claude", status: "running", response_state: "working", updated_at: 10,
      checkpoint: {summary: "Halfway there.", next_action: "Run the suite", next_actor: "agent"}, last_tool: {name: "Bash", kind: "run", label: "python3 -m unittest", at: 11}},
    latest_message: null, previous_checkpoint: {phase: "planning", summary: "Planned the change.", created_at: 5},
    children: [{thread_id: "kid", title: "Explorer", client: "codex", status: "idle", response_state: "available", checkpoint: {summary: "Mapped the files.", phase: "finished"}, last_tool: null}]});
  await Promise.resolve();
  assert.equal(s.panel.children[0].className, "hover-title");
  assert.match(s.panel.text, /Running python3 -m unittest · Just now/);
  assert.match(s.panel.text, /Earlier/); assert.match(s.panel.text, /Planned the change/);
  assert.match(s.panel.text, /Agents · 1/); assert.match(s.panel.text, /Explorer/);
  assert.equal(s.isPinned(), false); assert.equal(s.panel.attributes.role, "tooltip");
  find(s.panel, "hover-agent").onclick({stopPropagation() {}});
  assert.equal(s.isPinned(), true); assert.equal(s.panel.attributes.role, "dialog");
  assert.equal(s.requests[1].id, "kid");
  assert.match(s.panel.text, /Explorer/); assert.match(s.panel.html, /Mapped the files/); // Markdown bodies render into innerHTML
  const back = find(s.panel, "hover-back"); assert.match(back.textContent, /Parent/);
  back.onclick({stopPropagation() {}});
  assert.equal(s.panel.children[0].textContent, "Parent");
});

test("clicking inside pins the lens, Peek opens it pinned, and a refresh re-renders fresh list data", async () => {
  const s = setup(); s.enter(s.first); await s.flush(); s.requests[0].resolve(response("first")); await Promise.resolve();
  s.leave(s.first); await s.flush(); assert.equal(s.panel.hidden, true);
  s.enter(s.first); await s.flush(); await Promise.resolve(); // served from the short-lived cache
  assert.equal(s.requests.length, 1);
  s.panel.handlers.pointerdown({}); s.leave(s.first); await s.flush();
  assert.equal(s.panel.hidden, false); assert.equal(s.panel.attributes.role, "dialog");
  assert.ok(find(s.panel, "hover-close"));
  s.threads.first = {thread_id: "first", title: "first", status: "running", response_state: "working", checkpoint: {summary: "Saved progress"}, last_tool: {kind: "edit", label: "app.js", at: 1}};
  s.refresh(); assert.match(s.panel.text, /Editing app.js/);
  find(s.panel, "hover-close").onclick({stopPropagation() {}});
  assert.equal(s.panel.hidden, true); assert.equal(s.isPinned(), false);
  const button = s.document.createElement("button"); button.dataset.thread = "second"; s.overview.append(button);
  s.open(button, "second"); assert.equal(s.isPinned(), true); assert.equal(s.panel.hidden, false);
  assert.equal(s.requests.at(-1).id, "second");
  s.overview.handlers.click({target: {closest: () => button}}); assert.equal(s.panel.hidden, false);
  s.overview.handlers.click({target: {closest: () => null}}); assert.equal(s.panel.hidden, true);
});

test("Open thread hands off to the app and closes the lens", async () => {
  const s = setup(); s.enter(s.first); await s.flush(); s.requests[0].resolve(response("first")); await Promise.resolve();
  const foot = find(s.panel, "hover-foot"); assert.ok(foot);
  foot.children[0].onclick({stopPropagation() {}});
  assert.deepEqual(s.opened, ["first"]); assert.equal(s.panel.hidden, true);
});
