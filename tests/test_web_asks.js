"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {AsksUI, formatAge} = require("../plugins/agent-coord/scripts/agent_coord/web/asks.js");

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.value = ""; this.hidden = false; this.dataset = {}; this.classList = {set: new Set(), toggle: (name, on) => { on ? this.classList.set.add(name) : this.classList.set.delete(name); }, contains: name => this.classList.set.has(name)}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  querySelectorAll(selector) {
    const tags = selector.split(",").map(value => value.trim());
    return this.children.flatMap(child => [...(tags.includes(child.tagName) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  find(className) { return this.querySelectorAll("button, span, p, form, input, article, div, h2").find(node => node.className === className); }
}
function setup(api) {
  const opened = [], errors = [];
  const button = new Element("button"), badge = new Element("span");
  const document = {body: new Element("body"), hidden: false, createElement: tag => new Element(tag), getElementById: () => null};
  const ui = new AsksUI({document, api, openThread: id => opened.push(id), onError: error => errors.push(error), button, badge});
  return {ui, opened, errors, button, badge};
}
const blocking = {id: 7, session_id: "thread-7", thread_title: "Checkout <b>", project_name: "Shop", question: "Which account?", context: null, options: ["Staging", "Production"], blocking: true, age_seconds: 4000};
const quiet = {id: 8, session_id: "thread-8", thread_title: "Docs", project_name: null, question: "Tone?", context: "Default: formal.", options: [], blocking: false, age_seconds: 30};

test("formatAge is coarse and never negative", () => {
  assert.deepEqual([formatAge(-5), formatAge(30), formatAge(120), formatAge(4000), formatAge(200000)], ["just now", "just now", "2m", "1h", "2d"]);
});

test("badge counts open asks and marks blocking ones", async () => {
  const {ui, badge, button} = setup(async () => ({data: [blocking, quiet]}));
  await ui.refresh();
  assert.equal(badge.hidden, false);
  assert.equal(badge.textContent, "2");
  assert.ok(badge.classList.contains("asks-count-blocking"));
  assert.match(button.title, /2 decisions .* 1 blocking/);
  ui.api = async () => ({data: []});
  await ui.refresh();
  assert.equal(badge.hidden, true);
  assert.equal(button.title, "Decisions");
});

test("dialog renders text safely, opens the thread, and answers through the API", async t => {
  const calls = [];
  const {ui, opened} = setup(async (path, body) => { calls.push({path, body}); return body ? {status: "answered"} : {data: [blocking, quiet]}; });
  t.after(() => ui.close());
  await ui.open();
  assert.equal(ui.dialog.open, true);
  const rows = ui.dialog.children[2].children;
  assert.equal(rows.length, 2);
  const [first, second] = rows;
  assert.equal(first.className, "ask-row ask-blocking");
  assert.equal(first.find("ask-kind").textContent, "Blocking");
  assert.equal(first.find("ask-thread").textContent, blocking.thread_title);
  assert.equal(first.find("ask-thread").innerHTML, undefined);
  assert.equal(first.find("ask-question").textContent, "Which account?");
  assert.equal(second.find("ask-context").textContent, "Default: formal.");
  assert.equal(second.find("ask-age").textContent, "just now");
  const options = first.querySelectorAll("button").filter(node => node.className === "ask-option");
  assert.deepEqual(options.map(node => node.textContent), ["Staging", "Production"]);
  await options[1].onclick();
  assert.deepEqual(calls.at(-1), {path: "asks/7", body: {option: "Production"}});
  assert.equal(ui.items.length, 1);
  const remaining = ui.dialog.children[2].children[0];
  const form = remaining.find("ask-free");
  form.children[0].value = "  Friendly  ";
  await form.onsubmit({preventDefault() {}});
  assert.deepEqual(calls.at(-1), {path: "asks/8", body: {answer: "Friendly"}});
  assert.equal(ui.items.length, 0);
  assert.equal(ui.dialog.find("asks-empty").textContent, "No open decisions. Agents will ask here when they need you.");
  ui.items = [blocking]; ui.render();
  await ui.dialog.children[2].children[0].find("ask-thread").onclick();
  assert.deepEqual(opened, ["thread-7"]);
  assert.equal(ui.dialog.open, false);
});

test("a failed answer re-enables the row and surfaces the error", async t => {
  const {ui, errors} = setup(async (path, body) => { if (body) throw new Error("Ask already answered"); return {data: [blocking]}; });
  t.after(() => ui.close());
  await ui.open();
  const row = ui.dialog.children[2].children[0];
  const option = row.querySelectorAll("button").find(node => node.className === "ask-option");
  await option.onclick();
  assert.equal(errors.length, 1);
  assert.equal(errors[0].message, "Ask already answered");
  assert.ok(row.querySelectorAll("button, input").every(node => node.disabled === false));
  assert.equal(ui.items.length, 1);
});

test("polling preserves an answer draft and updates its age", async () => {
  let items = [blocking, quiet];
  const {ui} = setup(async () => ({data: items}));
  await ui.open();
  const row = ui.dialog.children[2].children[0];
  const input = row.find("ask-free").children[0];
  input.value = "Keep my unfinished answer";
  items = [{...blocking, age_seconds: 86400}, quiet];
  await ui.refresh();
  assert.equal(ui.dialog.children[2].children[0], row);
  assert.equal(row.find("ask-free").children[0], input);
  assert.equal(input.value, "Keep my unfinished answer");
  assert.equal(row.find("ask-age").textContent, "1d");
  items = [items[0]];
  await ui.refresh();
  assert.equal(ui.dialog.children[2].children[0], row);
  assert.equal(input.value, "Keep my unfinished answer");
  ui.close();
});
