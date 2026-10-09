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
  closest() { return this.dataset.thread ? this : this.parent?.closest(); }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return {top: 600, right: 950, bottom: 720, left: 600}; }
  get text() { return (this.textContent || "") + this.children.map(c => c.text).join("\n"); }
  get html() { return (this.innerHTML || "") + this.children.map(c => c.html).join("\n"); }
}
function setup() {
  const overview = new Element(), document = new Element(), window = new Element();
  document.body = new Element(); document.createElement = () => new Element();
  window.innerWidth = 1000; window.innerHeight = 800;
  window.MutationObserver = class { observe() {} };
  const first = new Element(), second = new Element(); first.dataset.thread = "first"; second.dataset.thread = "second";
  overview.append(first, second);
  let tasks = new Map(), sequence = 0, requests = [];
  const result = createThreadHover({overview, document, window,
    schedule: fn => { tasks.set(++sequence, fn); return sequence; }, cancel: id => tasks.delete(id),
    getThread: id => ({thread_id: id, title: id, cwd: "/workspace", checkpoint: {summary: "Saved progress"}}),
    statusLabel: () => "Working", relativeTime: () => "Just now",
    loadPreview: id => new Promise((resolve, reject) => requests.push({id, resolve, reject}))});
  return {...result, overview, document, window, first, second, requests,
    async flush() { const pending = [...tasks.values()]; tasks.clear(); for (const fn of pending) fn(); await Promise.resolve(); },
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

test("late responses cannot overwrite another card and raw HTML is escaped", async () => {
  const s = setup(); s.enter(s.first); await s.flush(); s.enter(s.second); await s.flush();
  s.requests[1].resolve(response("second")); await Promise.resolve();
  s.requests[0].resolve(response("first")); await Promise.resolve();
  assert.match(s.panel.text, /second/); assert.doesNotMatch(s.panel.text, /first/);
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

test("preview stays open while moving onto it and closes after leaving", async () => {
  const s = setup(); s.enter(s.first); await s.flush();
  s.leave(s.first); s.panel.handlers.pointerenter(); await s.flush();
  assert.equal(s.panel.hidden, false);
  s.panel.handlers.pointerleave(); await s.flush(); assert.equal(s.panel.hidden, true);
});

test("failed requests retain useful context without global errors; click dismisses", async () => {
  const s = setup(); s.enter(s.first); await s.flush(); s.requests[0].reject(new Error("offline")); await Promise.resolve();
  assert.match(s.panel.html, /<p>Saved progress<\/p>/); assert.match(s.panel.text, /Latest message unavailable/);
  s.overview.handlers.click(); assert.equal(s.panel.hidden, true);
});
