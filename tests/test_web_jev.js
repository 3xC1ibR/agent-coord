"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/jev.js"), "utf8");

function fixture({remote = false, embedded = false, search = ""} = {}) {
  class Element {
    constructor() { this.nodes = new Map(); this.value = ""; this.disabled = false; this.hidden = false; }
    set innerHTML(text) { this.html = text; }
    get innerHTML() { return this.html || ""; }
    querySelector(selector) { if (!this.nodes.has(selector)) this.nodes.set(selector, new Element()); return this.nodes.get(selector); }
    querySelectorAll() { return [...this.nodes.values()]; }
    setAttribute() {}
    focus() { this.focused = true; }
    click() { return this.onclick?.(); }
    remove() { this.removed = true; }
  }
  const cards = [], requests = [], calls = [];
  const settings = new Element(); settings.onclick = () => calls.push("settings");
  const document = {body: {classList: {contains: () => true}, append: node => cards.push(node)},
    createElement: () => new Element(), getElementById: () => settings};
  const env = {document, URLSearchParams, location: {search}, fetch: async (...args) => {
    requests.push(args); return {ok: true, json: async () => ({enabled: true, configured: true, verified: true})};
  }};
  env.window = env; env.parent = embedded ? {} : env;
  vm.runInNewContext(source, env);
  const container = new Element(), config = {remote, token: "csrf-token"};
  return {api: env.agentCoordJev, container, config, cards, calls, requests, $: id => container.querySelector("#" + id)};
}

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
