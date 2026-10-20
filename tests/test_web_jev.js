"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/jev.js"), "utf8");

function fixture({remote = false, embedded = false, search = "", overview = true} = {}) {
  class Element extends EventTarget {
    constructor() { super(); this.nodes = new Map(); this.value = ""; this.disabled = false; this.hidden = false; this.dataset = {}; this.attributes = {}; this.children = []; }
    set innerHTML(text) { this.html = text; }
    get innerHTML() { return this.html || ""; }
    querySelector(selector) { if (!this.nodes.has(selector)) this.nodes.set(selector, new Element()); return this.nodes.get(selector); }
    querySelectorAll() { return [...this.nodes.values()]; }
    setAttribute(name, value) { this.attributes[name] = value; }
    append(node) { this.children.push(node); }
    focus() { this.focused = true; }
    click() { return this.onclick?.(); }
    remove() { this.removed = true; }
  }
  const cards = [], requests = [], calls = [], timers = [], channels = [], bar = new Element(), dialog = new Element();
  const settings = new Element(); settings.onclick = () => calls.push("settings");
  const document = Object.assign(new EventTarget(), {body: {classList: {contains: () => true}, append: node => cards.push(node)},
    querySelector: selector => selector === ".overview-statusbar" && overview ? bar : null,
    createElement: () => new Element(), getElementById: id => id === "settings-dialog" ? dialog : settings});
  const env = Object.assign(new EventTarget(), {document, URLSearchParams, location: {search}, fetch: async (...args) => {
    requests.push(args); return {ok: true, json: async () => ({enabled: true, configured: true, verified: true})};
  }, setInterval: callback => { timers.push(callback); return callback; }, clearInterval: callback => { callback.cleared = true; },
  BroadcastChannel: class {
    constructor(name) { this.name = name; channels.push(this); }
    postMessage(data) { for (const channel of channels) if (channel !== this && !channel.closed && channel.name === this.name) channel.onmessage?.({data}); }
    close() { this.closed = true; }
  }});
  env.window = env; env.parent = embedded ? {} : env;
  vm.runInNewContext(source, env);
  const container = new Element(), config = {remote, token: "csrf-token"};
  return {api: env.agentCoordJev, env, document, bar, dialog, timers, channels, container, config, cards, calls, requests, $: id => container.querySelector("#" + id)};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test("Settings tests and enables through CSRF API without retaining the entered key", async () => {
  const f = fixture();
  await f.api.mountSettings(f.container, f.config);
  assert.match(f.container.innerHTML, /type="password"/);
  assert.match(f.container.innerHTML, /selected excerpts/);
  assert.match(f.container.innerHTML, /billed through your TypeSafe account/);
  f.$("jev-key").value = "test-secret";
  await f.$("jev-form").onsubmit({preventDefault() {}});
  const [url, options] = f.requests.at(-1);
  assert.equal(url, "/api/browser/jev/settings");
  assert.equal(options.headers["X-Agent-Coord-Token"], "csrf-token");
  assert.deepEqual(JSON.parse(options.body), {action: "test_enable", api_key: "test-secret"});
  assert.equal(f.$("jev-key").value, "");
  assert.match(f.$("jev-status").textContent, /Connected/);
  assert.equal(f.$("jev-disable").hidden, false);
  assert.equal(f.$("jev-remove").hidden, false);
});

test("failures clear entered credentials and preserve the prior enabled status", async () => {
  const f = fixture();
  let fail = false;
  const request = async body => {
    if (fail) throw Error("TypeSafe rejected this key.");
    return {enabled: true, configured: true, verified: true, storage: "keychain"};
  };
  await f.api.mountSettings(f.container, f.config, request);
  fail = true; f.$("jev-key").value = "bad-key";
  await f.$("jev-form").onsubmit({preventDefault() {}});
  assert.equal(f.$("jev-key").value, "");
  assert.match(f.$("jev-status").textContent, /Connected/);
  assert.equal(f.$("jev-error").hidden, false);
  assert.match(f.$("jev-error").textContent, /rejected/);
  assert.equal(f.$("jev-key").disabled, false);
});

test("disabled saved credentials can be retested without entering a key, and removed", async () => {
  const f = fixture(), calls = [];
  const request = async body => {
    if (body) calls.push(body);
    return {enabled: body?.action === "test_enable", configured: body?.action !== "remove"};
  };
  await f.api.mountSettings(f.container, f.config, request);
  await f.$("jev-form").onsubmit({preventDefault() {}});
  assert.equal(calls[0].action, "test_enable");
  assert.equal("api_key" in calls[0], false);
  await f.$("jev-disable").click();
  assert.equal(calls[1].action, "disable");
  assert.match(f.$("jev-status").textContent, /off/);
  await f.$("jev-remove").click();
  assert.equal(calls[2].action, "remove");
  assert.equal(f.$("jev-remove").hidden, true);
});

test("blank key validation and repeated submission do not send extra requests", async () => {
  const f = fixture(); let count = 0, resolve;
  const request = async body => {
    if (!body) return {configured: false};
    count++; return new Promise(done => { resolve = done; });
  };
  await f.api.mountSettings(f.container, f.config, request);
  await f.$("jev-form").onsubmit({preventDefault() {}});
  assert.equal(count, 0);
  assert.equal(f.$("jev-key").focused, true);
  f.$("jev-key").value = "test-key";
  const pending = f.$("jev-form").onsubmit({preventDefault() {}});
  await f.$("jev-form").onsubmit({preventDefault() {}});
  assert.equal(count, 1);
  assert.equal(f.$("jev-key").disabled, true);
  resolve({enabled: true, configured: true, verified: true});
  await pending;
});

test("onboarding saves Skip once without enabling or opening Settings", async () => {
  const f = fixture(), bodies = [];
  const request = async body => { if (body) bodies.push(body); return {onboarding_dismissed: !!bodies.length}; };
  await f.api.onboard(f.config, request);
  const card = f.cards[0];
  assert.match(card.innerHTML, /Optional/);
  await card.querySelector("[data-jev-skip]").click();
  assert.equal(bodies[0].action, "dismiss");
  assert.equal(card.removed, true);
  assert.equal(f.calls.length, 0);
  await f.api.onboard(f.config, request);
  assert.equal(f.cards.length, 1);
});

test("Set up opens the existing Settings surface and remote or embedded pages stay quiet", async () => {
  const f = fixture();
  await f.api.onboard(f.config, async () => ({}));
  await f.cards[0].querySelector("[data-jev-setup]").click();
  assert.deepEqual(f.calls, ["settings"]);
  for (const options of [{remote: true}, {embedded: true}, {search: "?detached=1"}]) {
    const f = fixture(options);
    await f.api.onboard(f.config, async () => assert.fail("No onboarding request expected"));
    assert.equal(f.cards.length, 0);
  }
  const remote = fixture({remote: true});
  remote.api.mountSettings(remote.container, remote.config, async () => assert.fail("No key request expected"));
  assert.equal(remote.container.hidden, true);
});

test("dismissal errors keep the prompt available for retry", async () => {
  const f = fixture();
  await f.api.onboard(f.config, async body => { if (body) throw Error("Disk unavailable"); return {}; });
  await f.cards[0].querySelector("[data-jev-skip]").click();
  assert.equal(f.cards[0].removed, undefined);
  assert.equal(f.cards[0].querySelector("[role=alert]").hidden, false);
});

test("overview icon reports enabled and disabled states and opens existing Settings", async () => {
  const f = fixture(); let enabled = true;
  const status = f.api.mountStatus(f.config, async () => ({enabled}));
  const button = f.bar.children[0];
  assert.equal(button.dataset.state, "unknown");
  await settle();
  assert.equal(button.dataset.state, "enabled");
  assert.match(button.attributes["aria-label"], /Jev enabled.*Open Jev settings/);
  button.click(); assert.deepEqual(f.calls, ["settings"]);
  enabled = false; await status.refresh();
  assert.equal(button.dataset.state, "disabled");
  assert.match(button.title, /Jev disabled/);
  status.close();
});

test("a failed status read is unavailable rather than disabled and remains clickable", async () => {
  const f = fixture(); let fail = true;
  const status = f.api.mountStatus(f.config, async () => { if (fail) throw Error("Offline"); return {enabled: true}; });
  await settle();
  const button = f.bar.children[0];
  assert.equal(button.dataset.state, "unknown");
  assert.match(button.title, /unavailable/);
  button.click(); assert.deepEqual(f.calls, ["settings"]);
  fail = false; await status.refresh();
  assert.equal(button.dataset.state, "enabled");
  status.close();
});

test("Settings changes refresh the overview indicator immediately across windows", async () => {
  const f = fixture(); let enabled = true;
  const request = async body => {
    if (body) enabled = body.action === "test_enable";
    return {enabled, configured: true, verified: true};
  };
  const status = f.api.mountStatus(f.config, request);
  await f.api.mountSettings(f.container, f.config, request);
  await f.$("jev-disable").click(); await settle();
  assert.equal(f.bar.children[0].dataset.state, "disabled");
  await f.$("jev-form").onsubmit({preventDefault() {}}); await settle();
  assert.equal(f.bar.children[0].dataset.state, "enabled");
  status.close();
});

test("status refreshes on return to the app and cleans up when the page leaves", async () => {
  const f = fixture(); let count = 0;
  f.api.mountStatus(f.config, async () => { count++; return {enabled: true}; });
  await settle();
  f.document.hidden = true; f.timers[0](); await settle();
  assert.equal(count, 1);
  f.document.hidden = false; f.document.dispatchEvent(new Event("visibilitychange")); await settle();
  assert.equal(count, 2);
  f.env.dispatchEvent(new Event("focus")); await settle();
  assert.equal(count, 3);
  f.dialog.dispatchEvent(new Event("close")); await settle();
  assert.equal(count, 4);
  f.env.dispatchEvent(new Event("pagehide"));
  assert.equal(f.timers[0].cleared, true);
  assert.equal(f.channels[0].closed, true);
  f.env.dispatchEvent(new Event("focus")); await settle();
  assert.equal(count, 4);
});

test("remote, embedded, and non-overview pages do not create an indicator or request", () => {
  for (const options of [{remote: true}, {embedded: true}, {overview: false}]) {
    const f = fixture(options);
    f.api.mountStatus(f.config, async () => assert.fail("No request expected"));
    assert.equal(f.bar.children.length, 0);
    assert.equal(f.timers.length, 0);
  }
});

test("a Settings change during an in-flight read gets a fresh status afterward", async () => {
  const f = fixture(); let resolveFirst, count = 0;
  const status = f.api.mountStatus(f.config, async () => {
    count++;
    if (count === 1) return new Promise(resolve => { resolveFirst = resolve; });
    return {enabled: false};
  });
  f.channels[0].onmessage({data: "changed"});
  assert.equal(count, 1);
  resolveFirst({enabled: true}); await settle();
  assert.equal(count, 2);
  assert.equal(f.bar.children[0].dataset.state, "disabled");
  status.close();
});
