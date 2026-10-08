"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {RemoteAccessPanel} = require("../plugins/agent-coord/scripts/agent_coord/web/remote-access.js");

function setup(request) {
  class Element {
    constructor() { this.children = []; this.nodes = new Map(); this.listeners = {}; this.value = ""; }
    setAttribute(key, value) { this[key] = value; }
    querySelector(key) { if (!this.nodes.has(key)) this.nodes.set(key, new Element()); return this.nodes.get(key); }
    querySelectorAll() { return [...this.nodes.entries()].filter(([key]) => /enable|disable|refresh|create-link|copy/.test(key)).map(([, value]) => value); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    get childNodes() { return this.children; }
    addEventListener(type, fn) { this.listeners[type] = fn; }
    showModal() { this.open = true; }
    focus() { this.focused = true; }
    select() { this.selected = true; }
  }
  const document = {createElement: () => new Element(), body: new Element()};
  const copied = [];
  const panel = new RemoteAccessPanel({document, request, clipboard: {writeText: async value => copied.push(value)}});
  return {panel, copied};
}

test("enabling uses the selected port and presents pairing controls", async () => {
  const calls = [];
  const {panel} = setup(async (...args) => { calls.push(args); return {enabled: true, port: 8443, url: "https://mac.test.ts.net:8443/", devices: []}; });
  panel.find(".remote-port").value = "8443";
  await panel.find(".remote-enable").onclick();
  assert.deepEqual(calls, [["enable", {port: 8443}]]);
  assert.equal(panel.find(".remote-pair").hidden, false);
  assert.equal(panel.find(".remote-port").disabled, true);
});

test("pairing links copy only on request and disappear when dialog closes", async () => {
  const url = "https://mac.test.ts.net/pair#pair=private";
  const {panel, copied} = setup(async () => ({url}));
  await panel.find(".remote-create-link").onclick();
  assert.deepEqual(copied, []);
  await panel.find(".remote-copy").onclick();
  assert.deepEqual(copied, [url]);
  panel.dialog.listeners.close();
  assert.equal(panel.find(".remote-link").value, "");
  assert.equal(panel.find(".remote-link-box").hidden, true);
});

test("device names are rendered as text and revoke uses the device id", async () => {
  const calls = [];
  const {panel} = setup(async (...args) => { calls.push(args); return {enabled: true, port: 443, devices: []}; });
  panel.render({enabled: true, port: 443, devices: [{id: "phone", name: "<img src=x onerror=alert(1)>"}]});
  const row = panel.find(".remote-devices").children[0];
  assert.equal(row.children[0].textContent, "<img src=x onerror=alert(1)>");
  await row.children[1].onclick();
  assert.deepEqual(calls, [["revoke", {id: "phone"}]]);
});

test("errors are visible and duplicate operations are blocked", async () => {
  let finish, calls = 0;
  const {panel} = setup(() => { ++calls; return new Promise((_, reject) => { finish = reject; }); });
  const first = panel.load();
  await panel.load();
  assert.equal(calls, 1);
  finish(Error("Install Tailscale"));
  await first;
  assert.equal(panel.find(".remote-status").textContent, "Install Tailscale");
  assert.equal(panel.busy, false);
});
