"use strict";
const assert = require("node:assert/strict");
const {test} = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const {randomUUID} = require("node:crypto");
const TurnNotifications = require("../plugins/agent-coord/scripts/agent_coord/web/notifications.js");
const source = fs.readFileSync(require.resolve("../desktop/macos/bridge.js"), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));

function fixture(preferences = {}, permission = "default", subframe = false) {
  const messages = [];
  class Storage {
    constructor() { this.values = new Map(); }
    getItem(key) { return this.values.get(String(key)) ?? null; }
    setItem(key, value) { this.values.set(String(key), String(value)); }
    removeItem(key) { this.values.delete(String(key)); }
  }
  const env = new EventTarget();
  Object.assign(env, {
    Event, EventTarget, Storage, crypto: {randomUUID}, localStorage: new Storage(),
    document: Object.assign(new EventTarget(), {visibilityState: "hidden", hasFocus: () => false}),
    setInterval: () => 1, clearInterval() {}, focus() {}, isSecureContext: true,
    webkit: {messageHandlers: {desktop: {async postMessage(body) {
      messages.push(JSON.parse(JSON.stringify(body)));
      if (body.action === "permission") return permission;
      if (body.action === "requestPermission") return permission = "granted";
      if (body.action === "preference") {
        if (body.value === null) delete preferences[body.key];
        else preferences[body.key] = body.value;
      }
      return true;
    }}}},
  });
  env.window = env;
  env.top = subframe ? {} : env;
  vm.runInContext(source.replace("__AGENT_COORD_PREFERENCES__", JSON.stringify(preferences)), vm.createContext(env));
  return {env, messages, preferences};
}

test("initialization checks permission without asking, and subframes receive no bridge", async () => {
  const {env, messages} = fixture();
  await settle();
  assert.equal(env.Notification.permission, "default");
  assert.deepEqual(messages.map(m => m.action), ["permission"]);
  const child = fixture({}, "default", true);
  assert.equal(child.env.Notification, undefined);
  assert.equal(child.messages.length, 0);
});

test("preferences survive a new origin, and unrelated storage and focus are not persisted", async () => {
  const saved = {"agent-coord-group-by": "project", "agent-coord.notifications.focus": "stale", unrelated: "secret"};
  const first = fixture(saved);
  assert.equal(first.env.localStorage.getItem("agent-coord-group-by"), "project");
  assert.equal(first.env.localStorage.getItem("unrelated"), null);
  assert.equal(first.env.localStorage.getItem("agent-coord.notifications.focus"), null);
  first.env.localStorage.setItem("agent-coord.sidebar-collapsed", true);
  first.env.localStorage.setItem("unrelated", "value");
  first.env.localStorage.setItem("agent-coord.notifications.focus", "current");
  first.env.localStorage.removeItem("agent-coord-group-by");
  await settle();
  const reopened = fixture(saved);
  assert.equal(reopened.env.localStorage.getItem("agent-coord.sidebar-collapsed"), "true");
  assert.equal(reopened.env.localStorage.getItem("agent-coord-group-by"), null);
  assert.equal(saved.unrelated, "secret");
  assert.equal(saved["agent-coord.notifications.focus"], "stale");
});

test("existing turn notifications request native permission and keep thread click behavior", async () => {
  const {env, messages} = fixture();
  await settle();
  const opened = [], errors = [];
  const button = {setAttribute() {}};
  const client = new TurnNotifications({button, claim: async () => true, selected: () => null,
    openThread: async id => opened.push(id), onError: error => errors.push(error)}, env);
  await button.onclick();
  assert.equal(env.Notification.permission, "granted");
  await client.receive([{id: 42, thread_id: "thread-one", title: "Finish build", status: "completed"}]);
  await settle();
  const message = messages.find(m => m.action === "notify");
  assert.equal(message.title, "Agent Coord · Turn finished");
  assert.equal(message.body, "Finish build");
  env.__agentCoordNotificationClick(message.id);
  await settle();
  assert.deepEqual(opened, ["thread-one"]);
  assert(messages.some(m => m.action === "closeNotification" && m.id === message.id));
  assert.deepEqual(errors, []);
  client.destroy();
});

test("denied notifications never reach the native delivery handler", async () => {
  const {env, messages} = fixture({}, "denied");
  await settle();
  new env.Notification("ignored");
  assert.equal(messages.filter(m => m.action === "notify").length, 0);
});
