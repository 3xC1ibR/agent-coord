"use strict";
const {test} = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(process.env.HISTORY_SOURCE || require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
const AgentMessages = require("../plugins/agent-coord/scripts/agent_coord/web/agent-messages.js");
const slice = (from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const clone = value => JSON.parse(JSON.stringify(value));
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }
function setup({total = 121, browser = true, paged = true} = {}) {
  const elements = new Map(), calls = [], errors = [];
  let c;
  const descendants = el => el.childNodes.flatMap(child => [child, ...descendants(child)]);
  function element(tag, textContent = "", className = "") {
    return {tag, textContent, className, dataset: {}, childNodes: [], listeners: {}, scrollTop: 0, clientHeight: 100,
      classList: {add() {}, remove() {}, toggle() {}},
      get scrollHeight() { return descendants(this).length * 10; },
      append(...children) { this.childNodes.push(...children.flatMap(child => child.tag === "fragment" ? child.childNodes : [child])); },
      replaceChildren(...children) { this.childNodes = []; this.append(...children); },
      setAttribute(key, value) { this[key] = value; },
      addEventListener(key, fn) { this.listeners[key] = fn; },
      focus() { c.document.activeElement = this; }, scrollIntoView() {},
      querySelectorAll(selector) { return descendants(this).filter(child => selector === "details[data-id]" ? child.tag === "details" && child.dataset.id : selector === "[data-search-item]" && child.dataset.searchItem); },
      querySelector(selector) { return descendants(this).find(child => selector === "summary" ? child.tag === "summary" : selector === ".timeline-earlier" && child.className.includes("timeline-earlier")); }};
  }
  const all = Array.from({length: total}, (_, i) => ({id: "t" + i, status: "completed", startedAt: i * 10 + 1, completedAt: i * 10 + 5,
    items: [{id: "u" + i, type: "userMessage", content: [{text: "Question " + i}]},
      ...["x", "y"].map(id => ({id: id + i, type: "commandExecution", command: "verify", aggregatedOutput: id.repeat(2000), status: "completed"})),
      {id: "a" + i, type: "agentMessage", text: "Answer " + i}]}));
  const compact = turn => ({...clone(turn), items: turn.items.map(item => item.aggregatedOutput ? {...item, aggregatedOutput: item.aggregatedOutput.slice(0, 512), abridged: true} : clone(item))});
  function page(before = all.length) {
    const start = Math.max(0, before - 40);
    return {id: "one", view: "recent", turns: all.slice(start, before).map(compact), earlierTurns: start, beforeSeq: start, totalTurns: all.length};
  }
  const work = {thread_id: "one", client: "claude", browser_session: browser, can_read_history: true, attention: "now"};
  const detail = () => ({session: {cwd: "/project", browser_session: browser}, work_thread: clone(work), running: false, requests: [], thread: page()});
  c = {state: {selected: "one", detail: detail(), config: {conversationHistoryPages: paged ? 1 : 0, conversationSnapshotControls: 1}, sessions: [work]},
    document: {activeElement: null, createDocumentFragment: () => element("fragment"), createElement: element,
      getElementById: id => descendants(c.$("timeline")).find(el => el.id === id)},
    $: id => elements.get(id) || elements.set(id, element("div")).get(id), node: element,
    AgentMessages, messageMarkdown: {render: text => text}, ChatImageAttachments: {appendPreviews() {}},
    AbortController, setTimeout, clearTimeout, sessionStorage: {getItem() {}, setItem() {}},
    sessionPath: id => "sessions/" + id, threadPath: id => "threads/" + id,
    renderThread() {}, renderRequests() {}, renderQueuedMessages() {}, renderStatus() {}, loadSessionModels() {}, scheduleDetail() {},
    isSessionDraft: () => false, showError: error => errors.push(error.message),
    async api(path, body, options = {}) {
      calls.push({path, options});
      if (c.read) { const result = await c.read(path, options); if (result !== undefined) return result; }
      if (path.includes("/full")) return {turn: clone(all.find(turn => path.includes("/" + turn.id + "/")))};
      const url = new URL(path, "http://localhost/"), target = url.searchParams.get("turn_id");
      if (target) {
        const index = all.findIndex(turn => turn.id === target);
        if (index < 0) throw Object.assign(new Error("Turn not found"), {status: 404});
        return {thread: page(index + 1)};
      }
      if (url.searchParams.has("before_seq")) return {thread: page(Number(url.searchParams.get("before_seq")))};
      return detail();
    }};
  vm.createContext(c);
  vm.runInContext(slice("function reconcileAttentionSnapshot(", "async function markThreadHandled("), c);
  vm.runInContext(slice("function conversationEntry(", "function renderThread("), c);
  vm.runInContext(slice("function itemText(", "function requestButton("), c);
  c.conversationEntry("one").detail = c.state.detail;
  const render = () => c.renderTimeline();
  const tools = () => descendants(c.$("timeline")).filter(el => el.className === "tool");
  const fullButton = (id = "x81") => tools().find(el => el.dataset.id === id)?.childNodes.find(el => el.className === "quiet tool-complete");
  render();
  return {c, calls, errors, all, page, compact, work, detail, render, fullButton, tools, entry: () => c.state.conversations.get("one"),
    earlier: () => c.$("timeline").childNodes.find(el => el.className === "quiet timeline-earlier")};
}

test("earlier turns read one compact page, deduplicate overlap, and keep the reading position", async () => {
  const h = setup(), el = h.c.$("timeline"); el.scrollTop = 150;
  const distance = el.scrollHeight - el.scrollTop;
  await h.earlier().onclick();
  assert.deepEqual(h.calls.map(x => x.path), ["sessions/one?before_seq=81&limit=40"]);
  assert.equal(h.entry().detail.thread.turns.length, 80);
  assert.equal(new Set(h.entry().detail.thread.turns.map(t => t.id)).size, 80);
  assert.equal(h.earlier().textContent, "Show 41 earlier turns");
  assert.equal(el.scrollHeight - el.scrollTop, distance);
  await h.earlier().onclick(); await h.earlier().onclick();
  assert.equal(h.entry().detail.thread.turns.length, 121); assert.equal(h.earlier(), undefined);
  assert.equal(h.entry().completeHistory, undefined);
});

test("a full-turn read retains only the requested output and preserves its disclosure", async () => {
  const h = setup(); const tool = h.tools().find(el => el.dataset.id === "x81"); tool.open = true;
  await h.fullButton().onclick();
  assert.deepEqual(h.calls.map(x => x.path), ["sessions/one/turns/t81/full"]);
  const items = h.entry().detail.thread.turns[0].items;
  assert.equal(items[1].aggregatedOutput.length, 2000);
  assert.equal(items[2].aggregatedOutput.length, 512); assert.equal(items[2].abridged, true);
  assert.equal(h.fullButton(), undefined); assert.equal(h.entry().expandedOutput.size, 1);
  assert.equal(h.tools().find(el => el.dataset.id === "x81").open, true);
});

test("latest snapshots retain opened older pages and requested output without full-history reads", async () => {
  const h = setup(); await h.earlier().onclick(); await h.fullButton("x41").onclick();
  h.all.push({...clone(h.all.at(-1)), id: "new", items: [{id: "new-answer", type: "agentMessage", text: "New result"}]});
  await h.c.refreshDetail({background: true});
  assert.equal(h.entry().detail.thread.turns.length, 81);
  assert.equal(h.entry().detail.thread.turns[0].id, "t41");
  assert.equal(h.entry().detail.thread.turns[0].items[1].aggregatedOutput.length, 2000);
  assert.equal(h.entry().detail.thread.turns.at(-1).id, "new");
  assert.equal(h.entry().detail.thread.earlierTurns, 41);
  assert.equal(h.calls.at(-1).options.headers["X-Agent-Coord-History-View"], "recent");
});

test("a refresh bridges more than forty new turns without losing retained context", async () => {
  const h = setup(); await h.earlier().onclick();
  for (let i = 121; i < 205; i++) h.all.push({...clone(h.all[0]), id: "t" + i});
  await h.c.refreshDetail({background: true});
  const turns = h.entry().detail.thread.turns;
  assert.deepEqual(Array.from(turns, t => t.id), h.all.slice(41).map(t => t.id));
  assert.equal(h.entry().detail.thread.earlierTurns, 41);
  assert.ok(h.calls.slice(2).every(call => call.path.includes("before_seq=")));
});

test("paging and output errors leave retryable accessible controls and the recent content", async () => {
  for (const mode of ["page", "output"]) {
    const h = setup(); const gate = deferred(); h.c.read = () => gate.promise;
    const button = mode === "page" ? h.earlier() : h.fullButton(); const reading = button.onclick();
    assert.equal(button.disabled, true); assert.equal(button["aria-busy"], "true");
    gate.reject(new Error("Read unavailable")); await reading;
    assert.equal(button.disabled, false); assert.equal(button["aria-busy"], "false");
    assert.deepEqual(h.errors, ["Read unavailable"]); assert.equal(h.entry().detail.thread.turns.length, 40);
    h.c.read = null; await button.onclick(); assert.equal(h.errors.length, 1);
  }
});

test("late page and output responses cannot repaint another conversation or an evicted cache entry", async () => {
  for (const mode of ["page", "output"]) for (const evicted of [true, false]) {
    const h = setup(); const gate = deferred(); h.c.read = () => gate.promise;
    const reading = (mode === "page" ? h.earlier() : h.fullButton()).onclick();
    h.c.state.selected = "other";
    if (evicted) h.c.state.conversations.delete("one");
    const other = {thread: {turns: []}, work_thread: {browser_session: true}};
    h.c.state.detail = other;
    h.c.$("timeline").replaceChildren(h.c.node("p", "Other conversation"));
    gate.resolve(mode === "page" ? {thread: h.page(81)} : {turn: clone(h.all[81])}); await reading;
    assert.equal(h.c.state.detail, other);
    assert.equal(h.c.$("timeline").childNodes[0].textContent, "Other conversation");
  }
});

test("a page accepted during a background refresh invalidates the older snapshot", async () => {
  const h = setup(); const snapshot = deferred();
  h.c.read = path => path === "sessions/one" ? snapshot.promise : undefined;
  const refresh = h.c.refreshDetail({background: true});
  await h.earlier().onclick(); snapshot.resolve(h.detail()); await refresh;
  assert.equal(h.entry().detail.thread.turns.length, 80);
  assert.equal(h.c.state.detail.thread.turns[0].id, "t41");
});

test("a changed compact preview discards stale full output, including a read still in flight", async () => {
  const h = setup(); const gate = deferred(); h.c.read = path => path.includes("/full") ? gate.promise : undefined;
  const reading = h.fullButton().onclick();
  h.entry().detail.thread.turns[0].items[1].aggregatedOutput = "new preview";
  gate.resolve({turn: clone(h.all[81])}); await reading;
  assert.equal(h.entry().expandedOutput, undefined);
  h.c.read = null; h.all[81].items[1].aggregatedOutput = "z".repeat(2000);
  await h.c.refreshDetail(); await h.fullButton().onclick();
  h.all[81].items[1].aggregatedOutput = "changed".repeat(200);
  await h.c.refreshDetail();
  assert.equal(h.entry().expandedOutput.size, 0);
  assert.equal(h.entry().detail.thread.turns[0].items[1].abridged, true);
});

test("read-only external histories support paging and tool reads", async () => {
  const h = setup({browser: false}); await h.earlier().onclick(); await h.fullButton("x41").onclick();
  assert.equal(h.c.state.detail.session.browser_session, false); assert.equal(h.c.state.detail.work_thread.browser_session, false);
  assert.deepEqual(h.calls.map(x => x.path), ["sessions/one?before_seq=81&limit=40", "sessions/one/turns/t41/full"]);
});

test("search targets use indexed lookup and compact context pages, surviving later refreshes", async () => {
  const h = setup(); h.c.state.searchTarget = {thread: "one", turn: "t20", item: "a20"};
  await h.c.refreshDetail();
  assert.ok(h.calls.some(x => x.path === "sessions/one?turn_id=t20&limit=40"));
  assert.equal(h.c.state.searchTarget, null); // Rendered and focused.
  assert.ok(h.c.state.detail.thread.turns.some(turn => turn.id === "t20"));
  await h.c.refreshDetail({background: true});
  assert.ok(h.c.state.detail.thread.turns.some(turn => turn.id === "t20"));
  assert.equal(h.entry().completeHistory, undefined);
});

test("coordination message links traverse compact pages without selecting a full-history view", async () => {
  const h = setup(); h.c.state.messageTarget = {thread: "one", id: "7"};
  await h.c.refreshDetail();
  assert.equal(h.c.state.detail.thread.turns.length, 121);
  assert.ok(h.calls.every(x => x.options.headers?.["X-Agent-Coord-History-View"] === "recent" || x.path.includes("before_seq=")));
});

test("legacy backends keep their complete-history path and the conversation cache stays bounded", async () => {
  const h = setup({paged: false}); await h.earlier().onclick();
  assert.equal(h.entry().completeHistory, true);
  assert.equal(h.calls.at(-1).path, "sessions/one");
  assert.equal(h.calls.at(-1).options.headers?.["X-Agent-Coord-History-View"], undefined);
  for (let i = 0; i < 6; i++) h.c.conversationEntry("other-" + i);
  assert.equal(h.c.state.conversations.size, 4); assert.ok(h.c.state.conversations.has("one"));
});

test("prepending anchors a retained passage instead of the replaced earlier-turn button", async () => {
  const h = setup(), el = h.c.$("timeline"), button = h.earlier(), passage = el.childNodes[1];
  Object.defineProperty(el, "children", {get: () => el.childNodes});
  let top = 200;
  passage.getBoundingClientRect = () => ({top, bottom: top + 100});
  const scroller = {scrollTop: 150, getBoundingClientRect: () => ({top: 100, bottom: 1000})};
  h.c.state.timelineScroll = {scroller: () => scroller, hold() {}, beforeRender: () => false, afterRender() {},
    restore(value) { scroller.scrollTop = value; }};
  Object.defineProperty(passage, "isConnected", {get: () => el.childNodes.includes(passage)});
  const render = h.c.renderTimeline;
  h.c.renderTimeline = () => { render(); top = 8200; };
  button.focus(); await button.onclick();
  assert.equal(scroller.scrollTop, 8150);
  assert.equal(h.c.document.activeElement, h.earlier());
  assert.deepEqual(h.errors, []);
});

test("output expansion restores keyboard focus after disabling and replacing its button", async () => {
  const h = setup(), button = h.fullButton(); button.focus();
  const read = h.c.api;
  h.c.api = async (...args) => { h.c.document.activeElement = null; return read(...args); };
  await button.onclick();
  assert.equal(h.c.document.activeElement.tag, "summary");
  assert.equal(h.fullButton(), undefined); assert.deepEqual(h.errors, []);
});

test("nonprogressing page cursors and mismatched turn payloads remain retryable", async () => {
  for (const output of [true, false]) {
    const h = setup();
    h.c.read = () => output ? {turn: clone(h.all[82])} : {thread: h.page()};
    const button = output ? h.fullButton() : h.earlier(); await button.onclick();
    assert.equal(h.errors.length, 1); assert.equal(button.disabled, false);
    assert.equal(h.entry().detail.thread.turns.length, 40);
  }
});

function streaming(h) {
  h.c.scheduleTimelineRender = () => h.render();
  vm.runInContext(slice("function applyEvent(", "let listTimer, detailTimer;"), h.c);
  return (method, params) => h.c.applyEvent({method, params: {threadId: "one", turnId: "live", ...params}});
}

test("canonical reasoning and tool deltas preserve identity, ordering, disclosures and the scroll hold", () => {
  const h = setup(), send = streaming(h), el = h.c.$("timeline");
  let holds = 0;
  h.c.state.timelineScroll = {following: false, beforeRender: () => false, afterRender(follow) { assert.equal(follow, false); holds++; }};
  send("item/started", {item: {id: "thinking", type: "reasoning", summary: []}});
  send("item/started", {item: {id: "tool", type: "mcpToolCall", tool: "Read", arguments: {}, visibleOutput: ""}});
  h.tools().find(el => el.dataset.id === "thinking").open = true;
  h.tools().find(el => el.dataset.id === "tool").open = true;
  el.scrollTop = 170;
  send("item/reasoning/summaryTextDelta", {itemId: "thinking", summaryIndex: 1, delta: "second"});
  send("item/reasoning/summaryTextDelta", {itemId: "thinking", summaryIndex: 0, delta: "first"});
  send("item/reasoning/summaryTextDelta", {itemId: "thinking", summaryIndex: 1, delta: " part"});
  send("item/mcpToolCall/outputDelta", {itemId: "tool", delta: "first\n"});
  send("item/mcpToolCall/outputDelta", {itemId: "tool", delta: "second"});
  const items = h.c.state.detail.thread.turns.at(-1).items;
  assert.deepEqual(Array.from(items, item => item.id), ["thinking", "tool"]);
  assert.deepEqual(Array.from(items[0].summary), ["first", "second part"]);
  assert.equal(items[1].visibleOutput, "first\nsecond");
  assert.equal(h.c.itemText(items[0]), "first\nsecond part");
  assert.ok(h.c.itemText(items[1]).includes("first"));
  assert.equal(h.tools().find(el => el.dataset.id === "thinking").open, true);
  assert.equal(h.tools().find(el => el.dataset.id === "tool").open, true);
  assert.equal(el.scrollTop, 170); assert.ok(holds >= 7);
});

test("canonical deltas received during snapshot refresh are retained exactly once", async () => {
  const h = setup(), send = streaming(h), gate = deferred();
  send("item/started", {item: {id: "thinking", type: "reasoning", summary: [""]}});
  send("item/started", {item: {id: "tool", type: "mcpToolCall", visibleOutput: ""}});
  const stale = clone(h.c.state.detail);
  h.c.read = path => path === "sessions/one" ? gate.promise : undefined;
  const refresh = h.c.refreshDetail({background: true});
  send("item/reasoning/summaryTextDelta", {itemId: "thinking", summaryIndex: 0, delta: "analysis"});
  send("item/mcpToolCall/outputDelta", {itemId: "tool", delta: "output"});
  gate.resolve(stale); await refresh;
  const items = h.c.state.detail.thread.turns.at(-1).items;
  assert.equal(items[0].summary[0], "analysis"); assert.equal(items[1].visibleOutput, "output");
  assert.deepEqual(Array.from(items, item => item.id), ["thinking", "tool"]);
});

test("the first canonical tool delta preserves an existing native result prefix", () => {
  for (const [initial, expected] of [[{result: "Native prefix\n"}, "Native prefix\nsuffix"],
    [{result: {text: "structured result"}}, "suffix"], [{result: "Earlier result", visibleOutput: ""}, "suffix"]]) {
    const h = setup(), send = streaming(h);
    send("item/started", {item: {id: "tool", type: "mcpToolCall", ...initial}});
    send("item/mcpToolCall/outputDelta", {itemId: "tool", delta: "suffix"});
    send("item/mcpToolCall/outputDelta", {itemId: "tool", delta: " continued"});
    assert.equal(h.c.state.detail.thread.turns.at(-1).items[0].visibleOutput, expected + " continued");
    assert.equal(h.c.state.detail.thread.turns.at(-1).items.length, 1);
  }
});
