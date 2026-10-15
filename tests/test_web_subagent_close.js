"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const createThreadActions = require("../plugins/agent-coord/scripts/agent_coord/web/thread-actions.js");

function setup(storage = new Map()) {
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.listeners = {}; }
    append(...children) { children.forEach(child => { this.children.push(child); child.parent = this; }); }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener(key, callback) { this.listeners[key] = callback; }
    remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
    focus() { doc.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; this.listeners.close?.(); }
  }
  const doc = {body: new Element("body"), createElement: tag => new Element(tag), addEventListener() {}};
  const win = {addEventListener() {}, localStorage: {
    getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
  }};
  const ui = createThreadActions(doc, win);
  return {ui, doc, storage, win, dialog: () => doc.body.children.find(item => item.tag === "dialog")};
}
const settle = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
const children = [{thread_id: "child", title: "Child", running: true}, {thread_id: "nested", running: false}];

test("close prompt offers No and Yes, explains running agents, and remembers either choice across reloads", async () => {
  for (const choice of ["no", "yes"]) {
    const h = setup();
    const pending = h.ui.prepareClose("parent", async path => {
      assert.equal(path, "threads/parent/close-plan"); return {subagents: children};
    });
    await settle();
    const dialog = h.dialog(), [title, description, label, actions] = dialog.children;
    assert.equal(title.textContent, "Close subagent threads?");
    assert.match(description.textContent, /1 still running/);
    assert.match(description.textContent, /No closes only this thread/);
    assert.equal(label.children[1].textContent, "Don’t ask me again");
    assert.equal(h.doc.activeElement, actions.children[0]);
    label.children[0].checked = true;
    assert.deepEqual(actions.children.map(button => button.textContent), ["No", "Yes"]);
    actions.children[choice === "yes" ? 1 : 0].onclick();
    const result = await pending;
    assert.deepEqual(result.body, choice === "yes" ? {subagent_thread_ids: ["child", "nested"]} : {});
    assert.equal(h.dialog(), undefined);
    const reload = setup(h.storage);
    assert.deepEqual(await reload.ui.prepareClose("parent", async () => ({subagents: children})), result);
    assert.equal(reload.dialog(), undefined);
  }
});

test("Escape cancels the entire close and never saves a choice", async () => {
  const h = setup(), pending = h.ui.prepareClose("parent", async () => ({subagents: children}));
  await settle();
  h.dialog().children[2].children[0].checked = true;
  let prevented = false;
  h.dialog().listeners.cancel({preventDefault() { prevented = true; }});
  assert.equal(await pending, null);
  assert.equal(prevented, true);
  assert.equal(h.storage.size, 0);
});

test("threads without open children bypass the prompt even with a saved Yes", async () => {
  const h = setup(new Map([["agent-coord:close-subagents", "yes"]]));
  assert.deepEqual(await h.ui.prepareClose("parent", async () => ({subagents: []})), {body: {}, subagents: []});
  assert.equal(h.dialog(), undefined);
});

test("unchecked choices are asked again and storage failures do not prevent closure", async () => {
  const h = setup();
  Object.defineProperty(h.win, "localStorage", {get() { throw Error("Storage unavailable"); }});
  for (let i = 0; i < 2; i++) {
    const pending = h.ui.prepareClose("parent", async () => ({subagents: children}));
    await settle();
    h.dialog().children[2].children[0].checked = !!i;
    h.dialog().children[3].children[0].onclick();
    assert.deepEqual((await pending).body, {});
  }
});

test("concurrent close requests serialize dialogs and use the remembered choice", async () => {
  const h = setup();
  const first = h.ui.prepareClose("one", async () => ({subagents: children}));
  const second = h.ui.prepareClose("two", async () => ({subagents: children.slice(0, 1)}));
  await settle();
  assert.equal(h.doc.body.children.length, 1);
  h.dialog().children[2].children[0].checked = true;
  h.dialog().children[3].children[1].onclick();
  await first;
  assert.deepEqual((await second).body, {subagent_thread_ids: ["child"]});
  assert.equal(h.dialog(), undefined);
});
