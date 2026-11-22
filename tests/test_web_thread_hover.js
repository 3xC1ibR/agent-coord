"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {createThreadHover} = require("../plugins/agent-coord/scripts/agent_coord/web/thread-hover.js");

class Element {
  constructor() { this.children = []; this.handlers = {}; this.attributes = {}; this.dataset = {}; this.style = {setProperty(key, value) { this[key] = value; }, removeProperty(key) { delete this[key]; }}; this.isConnected = true; this.offsetWidth = 420; this.offsetHeight = 400; }
  append(...items) { this.children.push(...items); for (const item of items) item.parent = this; }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  contains(el) { return el === this || this.children.some(child => child.contains(el)); }
  closest(selector) {
    if (selector === '[hidden], [aria-hidden="true"]') return null;
    if (selector === '[data-action="peek"]') return this.dataset.action === "peek" ? this : this.parent?.closest(selector);
    return this.dataset.thread || this.dataset.threadTab !== undefined ? this : this.parent?.closest(selector);
  }
  matches(selector) { return selector === ":hover" && !!this.hovered; }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return {top: 600, right: 950, bottom: 720, left: 600}; }
  get text() { return (this.textContent || "") + this.children.map(c => c.text).join("\n"); }
  get html() { return (this.innerHTML || "") + this.children.map(c => c.html).join("\n"); }
}
function setup() {
  const overview = new Element(), tabs = new Element(), document = new Element(), window = new Element();
  document.body = new Element(); document.createElement = () => new Element();
  window.innerWidth = 1000; window.innerHeight = 800;
  window.MutationObserver = class { constructor(callback) { window.mutation = callback; } observe() {} };
  const first = new Element(), second = new Element(); first.dataset.thread = "first"; second.dataset.thread = "second";
  overview.append(first, second);
  overview.querySelectorAll = () => overview.children;
  tabs.querySelectorAll = () => tabs.children;
  let tasks = new Map(), sequence = 0, requests = [], opened = [], delays = [];
  const threads = {};
  const result = createThreadHover({overview, tabs, document, window,
    schedule: (fn, delay) => { delays.push(delay); tasks.set(++sequence, fn); return sequence; }, cancel: id => tasks.delete(id),
    getThread: id => !id || ["new-session", "project-scratchpad"].includes(id) ? null : threads[id] || ({thread_id: id, title: id, cwd: "/workspace", checkpoint: {summary: "Saved progress"}}),
    relativeTime: () => "Just now", select: id => opened.push(id),
    loadPreview: id => new Promise((resolve, reject) => requests.push({id, resolve, reject}))});
  return {...result, overview, tabs, document, window, first, second, requests, threads, opened, delays,
    async flush() { for (const [id, fn] of [...tasks]) if (tasks.delete(id)) fn(); await Promise.resolve(); },
    enter(target, type = "pointerover") { overview.handlers[type]({target, type, pointerType: "mouse"}); },
    leave(target) { overview.handlers.pointerout({target, relatedTarget: null}); }};
}
const response = id => ({thread: {title: id, client: "codex", checkpoint: {}}, latest_message: {role: "assistant", text: "<script>literal message</script>"}});

test("preview opens at the latest pointer position and does not follow subsequent movement", async () => {
  const s = setup();
  s.overview.handlers.pointerover({target: s.first, type: "pointerover", pointerType: "mouse", clientX: 100, clientY: 120});
  s.document.handlers.pointermove({pointerType: "mouse", clientX: 260, clientY: 180});
  await s.flush();
  assert.equal(s.panel.style.left, "270px"); assert.equal(s.panel.style.top, "190px");
  s.document.handlers.pointermove({pointerType: "mouse", clientX: 400, clientY: 220});
  s.requests[0].resolve(response("first")); await Promise.resolve();
  assert.equal(s.panel.style.left, "270px"); assert.equal(s.panel.style.top, "190px");
});

test("loading growth and list refreshes preserve pointer placement and scrolling", async () => {
  const s = setup(); s.panel.offsetHeight = 160;
  s.overview.handlers.pointerover({target: s.first, type: "pointerover", pointerType: "mouse", clientX: 700, clientY: 600});
  await s.flush();
  const {left, top} = s.panel.style;
  assert.equal(left, "270px"); assert.equal(top, "610px");
  assert.equal(s.panel.style["--thread-hover-available-height"], "178px");
  s.panel.offsetHeight = 500; s.panel.scrollTop = 42;
  s.requests[0].resolve(response("first")); await Promise.resolve(); s.refresh();
  assert.equal(s.panel.style.left, left); assert.equal(s.panel.style.top, top);
  assert.equal(s.panel.scrollTop, 42);
  s.panel.handlers.pointerdown(); s.window.innerHeight = 500; s.window.handlers.resize();
  assert.ok(parseFloat(s.panel.style.top) >= 12);
  assert.ok(parseFloat(s.panel.style.top) + parseFloat(s.panel.style["--thread-hover-available-height"]) <= 488);
});

test("pointer previews fit the bottom-right viewport edge and keyboard previews ignore old pointer coordinates", async () => {
  const s = setup();
  s.overview.handlers.pointerover({target: s.first, type: "pointerover", pointerType: "mouse", clientX: 990, clientY: 790});
  await s.flush();
  assert.equal(s.panel.style.left, "560px"); assert.equal(s.panel.style.top, "388px");
  s.hide(); s.enter(s.second, "focusin"); await s.flush();
  assert.equal(s.panel.style.left, "170px");
  assert.equal(s.panel.style["--thread-hover-available-height"], undefined);
});

test("moving across row descendants does not restart the pending hover delay", async () => {
  const s = setup(), title = new Element(); s.first.append(title);
  s.enter(s.first); s.enter(title); s.enter(s.first);
  assert.deepEqual(s.delays, [300]);
  await s.flush(); assert.equal(s.requests.length, 1);
});

test("pinning preserves the pressed control so its first click can open the thread", async () => {
  const s = setup(); s.enter(s.first); await s.flush();
  const open = find(s.panel, "hover-foot").children.at(-1);
  s.panel.handlers.pointerdown();
  assert.equal(find(s.panel, "hover-foot").children.at(-1), open);
  open.onclick({stopPropagation() {}});
  assert.deepEqual(s.opened, ["first"]); assert.equal(s.panel.hidden, true);
});

test("keyboard focus inside the panel survives row exit and closes after focus leaves", async () => {
  const s = setup(); s.enter(s.first, "focusin"); await s.flush();
  const open = find(s.panel, "hover-foot").children.at(-1);
  s.document.activeElement = open; s.leave(s.first); await s.flush();
  assert.equal(s.panel.hidden, false);
  s.document.activeElement = null; s.panel.handlers.focusout({target: open, relatedTarget: null}); await s.flush();
  assert.equal(s.panel.hidden, true);
});

test("inbox rows share the existing preview and its pinning and open action", async () => {
  const s = setup(), row = new Element(), title = new Element();
  row.className = "tl-row"; row.dataset.thread = "inbox"; row.append(title); s.overview.append(row);
  s.enter(title); await s.flush();
  assert.equal(s.requests[0].id, "inbox");
  assert.equal(row.attributes["aria-describedby"], "thread-hover");
  s.requests[0].resolve(response("Inbox conversation")); await Promise.resolve();
  assert.match(s.panel.text, /Inbox conversation/);
  s.panel.handlers.pointerdown(); s.leave(row); await s.flush();
  assert.equal(s.panel.hidden, false);
  find(s.panel, "hover-foot").children.at(-1).onclick({stopPropagation() {}});
  assert.deepEqual(s.opened, ["inbox"]); assert.equal(s.panel.hidden, true);
});

test("conversation tabs preview without activation, open below the strip and dismiss on navigation", async () => {
  const s = setup(), tab = new Element(); tab.dataset.threadTab = "first"; s.tabs.append(tab);
  tab.getBoundingClientRect = () => ({left: 300, right: 500, top: 40, bottom: 80});
  s.tabs.handlers.pointerover({target: tab, type: "pointerover", pointerType: "mouse"}); await s.flush();
  assert.equal(s.requests[0].id, "first"); assert.deepEqual(s.opened, []);
  assert.equal(s.panel.style.left, "300px"); assert.equal(s.panel.style.top, "90px");
  assert.equal(tab.attributes["aria-describedby"], "thread-hover");
  s.tabs.handlers.click({target: tab}); assert.equal(s.panel.hidden, true);
  s.tabs.handlers.focusin({target: tab, type: "focusin"}); await s.flush();
  assert.equal(s.panel.hidden, false);
  s.tabs.handlers.dragstart(); assert.equal(s.panel.hidden, true);
});

test("inbox keyboard focus keeps the preview open when the pointer leaves its row", async () => {
  const s = setup(), row = new Element(), title = new Element();
  row.dataset.thread = "inbox"; row.append(title); s.overview.append(row);
  s.document.activeElement = title; s.enter(title, "focusin"); await s.flush();
  s.leave(row); await s.flush(); assert.equal(s.panel.hidden, false);
  s.document.activeElement = null; s.leave(title); await s.flush();
  assert.equal(s.panel.hidden, true);
});

test("a tall tab preview fits below the strip in a short window", async () => {
  const s = setup(), tab = new Element(); tab.dataset.threadTab = "first"; s.tabs.append(tab);
  s.window.innerHeight = 500;
  tab.getBoundingClientRect = () => ({left: 300, right: 500, top: 40, bottom: 80});
  Object.defineProperty(s.panel, "offsetHeight", {get: () => Math.min(450, parseFloat(s.panel.style.maxHeight) || 450)});
  s.tabs.handlers.pointerover({target: tab, type: "pointerover", pointerType: "mouse"}); await s.flush();
  assert.equal(s.panel.style.top, "90px"); assert.equal(s.panel.style.maxHeight, "398px");
  s.hide(); s.enter(s.first); await s.flush();
  assert.equal(s.panel.style.maxHeight, "");
});

test("tab refresh stays on the same surface when the same thread also has a card", async () => {
  const s = setup(), tab = new Element(); tab.dataset.threadTab = "first"; s.tabs.append(tab);
  s.tabs.handlers.pointerover({target: tab, type: "pointerover", pointerType: "mouse"}); await s.flush();
  s.panel.handlers.pointerdown();
  const replacement = new Element(); replacement.dataset.threadTab = "first";
  tab.isConnected = false; s.tabs.children[0] = replacement; s.window.mutation();
  assert.equal(s.panel.hidden, false);
  assert.equal(replacement.attributes["aria-describedby"], "thread-hover");
  assert.equal(s.first.attributes["aria-describedby"], undefined);
  s.requests[0].resolve(response("Refreshed conversation")); await Promise.resolve();
  assert.match(s.panel.text, /Refreshed conversation/);
  replacement.isConnected = false; s.tabs.children = []; s.window.mutation();
  assert.equal(s.panel.hidden, true);
});

test("overview, scratchpad and draft tabs have no thread preview, and touch does not hover", async () => {
  const s = setup();
  for (const id of ["", "project-scratchpad", "new-session"]) {
    const tab = new Element(); tab.dataset.threadTab = id; s.tabs.append(tab);
    s.tabs.handlers.pointerover({target: tab, type: "pointerover", pointerType: "mouse"});
    s.tabs.handlers.focusin({target: tab, type: "focusin"});
  }
  const tab = new Element(); tab.dataset.threadTab = "first"; s.tabs.append(tab);
  s.tabs.handlers.pointerover({target: tab, type: "pointerover", pointerType: "touch"});
  await s.flush(); assert.equal(s.panel.hidden, true); assert.equal(s.requests.length, 0);
});

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
  assert.match(find(s.panel, "hover-message-text").text, /<script>literal message<\/script>/); // textContent, never HTML
  assert.doesNotMatch(s.panel.html, /<script>/);
  assert.equal(s.panel.style.left, "170px"); assert.equal(s.panel.style.top, "388px");
});

test("checkpoint bodies render safe Markdown; the message excerpt is plain text with truncation preserved", async () => {
  const s = setup(); s.enter(s.first); await s.flush();
  s.requests[0].resolve({
    thread: {title: "Preview", client: "claude", checkpoint: {
      summary: "## Progress\n\n**Ready** and *verified*.\n\n- First\n- Second",
      next_action: "Open [review](https://example.com) and run `check`.", next_actor: "user"
    }},
    latest_message: {role: "assistant", text: "#### Details\n\n**Bold** and `code`.\n\n```js\nconst ready = true;\n```\n\n[unsafe](javascript:alert(1))", truncated: true}
  });
  await Promise.resolve();
  const summary = s.panel.children.find(el => el.className === "hover-summary markdown");
  assert.match(summary.innerHTML, /<h2>Progress<\/h2>/);
  assert.match(summary.innerHTML, /<strong>Ready<\/strong> and <em>verified<\/em>/);
  assert.match(summary.innerHTML, /<ul><li><p>First<\/p><\/li><li><p>Second<\/p><\/li><\/ul>/);
  const next = find(s.panel, "hover-next");
  assert.equal(next.dataset.actor, "user");
  assert.equal(find(next, "hover-next-actor").textContent, "Your next step");
  assert.match(find(next, "hover-next-text").innerHTML, /href="https:\/\/example.com"/);
  assert.match(find(next, "hover-next-text").innerHTML, /<code>check<\/code>/);
  const body = find(s.panel, "hover-message-text");
  assert.equal(body.text, "Claude: \nDetails Bold and code. const ready = true; unsafe…");
  assert.doesNotMatch(s.panel.text, /Open the thread to read more|Most recent message|Where things stand/);
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
  assert.equal(find(s.panel, "hover-activity").textContent, "Running python3 -m unittest");
  assert.equal(find(s.panel, "hover-state-time").textContent, "· Just now");
  assert.equal(find(s.panel, "hover-earlier").textContent, "Earlier, planning Just now: Planned the change.");
  assert.equal(find(s.panel, "hover-agents").attributes["aria-label"], "1 agent"); assert.match(s.panel.text, /Explorer/);
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
  assert.equal(foot.children[0].textContent, "/workspace");
  foot.children.at(-1).onclick({stopPropagation() {}});
  assert.deepEqual(s.opened, ["first"]); assert.equal(s.panel.hidden, true);
});

test("the lens states status once and omits an earlier checkpoint from the same phase", async () => {
  const s = setup(); s.enter(s.first); await s.flush();
  s.requests[0].resolve({
    thread: {title: "Idle thread", client: "codex", status: "idle", response_state: "available", updated_at: 1,
      project_cwd: "/Users/someone/projects", cwd: "/Users/someone/projects", checkpoint: {phase: "discussion", summary: "Current direction."}},
    previous_checkpoint: {phase: "discussion", summary: "Same direction, earlier draft.", created_at: 0},
    latest_message: null});
  await Promise.resolve();
  assert.equal(findAll(s.panel, "hover-state-label").length, 1);
  assert.equal(find(s.panel, "hover-activity"), undefined);
  assert.equal(find(s.panel, "hover-earlier"), undefined);
  assert.doesNotMatch(s.panel.text, /Project:|Workspace:|Updated/);
  assert.equal(find(s.panel, "hover-path").textContent, "~/projects");
  assert.equal(find(s.panel, "hover-path").title, "/Users/someone/projects");
});
