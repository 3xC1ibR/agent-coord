"use strict";
const assert = require("node:assert/strict");
const {test} = require("node:test");
const TurnNotifications = require("../plugins/agent-coord/scripts/agent_coord/web/notifications.js");

function fixture(shared = {}) {
  shared.values ||= new Map(); shared.claims ||= new Set(); shared.approvalClaims ||= new Set(); shared.snoozeClaims ||= new Set(); shared.shown ||= [];
  const listeners = new Map(), opened = [], errors = [];
  let selected = null, focused = false, permissionRequests = 0;
  const button = {textContent: "", disabled: false, title: "", setAttribute(name, value) { this[name] = value; }};
  class Notification {
    static permission = "default";
    static async requestPermission() { permissionRequests++; return this.permission = "granted"; }
    constructor(title, options) { this.title = title; this.options = options; shared.shown.push(this); }
    close() { this.closed = true; }
  }
  const env = {
    Notification, isSecureContext: true,
    crypto: {randomUUID: () => Math.random().toString(36)},
    localStorage: {getItem: key => shared.values.get(key) ?? null, setItem: (key, value) => shared.values.set(key, value), removeItem: key => shared.values.delete(key)},
    document: {visibilityState: "visible", hasFocus: () => focused, addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener() {}},
    addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener() {},
    setInterval: () => 1, clearInterval() {}, focus: () => { focused = true; },
  };
  const client = new TurnNotifications({
    button,
    claim: async id => { if (shared.claims.has(id)) return false; shared.claims.add(id); return true; },
    claimApproval: async key => { if (shared.approvalClaims.has(key)) return false; shared.approvalClaims.add(key); return true; },
    claimSnooze: async id => { if (shared.snoozeClaims.has(id)) return false; shared.snoozeClaims.add(id); return true; },
    selected: () => selected, openThread: id => opened.push(id), onError: error => errors.push(error),
  }, env);
  return {client, env, button, opened, errors, shared, listeners,
    permissionRequests: () => permissionRequests,
    focus: id => { selected = id; focused = true; client.syncFocus(); },
    blur: () => { focused = false; client.syncFocus(); },
  };
}
const completion = (id = 1, values = {}) => ({id, thread_id: "thread-one", title: "Check the build", project_name: "agent-coord", status: "completed", ...values});
const approval = (key = "request-one", values = {}) => ({request_key: key, thread_id: "thread-one", title: "Check the build", project_name: "agent-coord", status: "approval", ...values});
const snooze = (id = 1, values = {}) => ({id, thread_id: "thread-one", title: "Check the build", project_name: "agent-coord", status: "snooze", ...values});

test("snooze expiry alerts once across tabs with thread context and opens its conversation", async () => {
  const shared = {}, first = fixture(shared), second = fixture(shared);
  await first.button.onclick();
  second.env.Notification.permission = "granted";
  await Promise.all([first.client.receive([snooze()]), second.client.receive([snooze()])]);
  await first.client.receive([snooze()]);
  assert.equal(shared.shown.length, 1);
  const notice = shared.shown[0];
  assert.equal(notice.title, "agent-coord · Snooze ended");
  assert.equal(notice.options.body, "Check the build");
  assert.equal(notice.options.tag, "agent-coord-snooze-1");
  await notice.onclick();
  assert.deepEqual(first.opened, ["thread-one"]);
  assert.equal(notice.closed, true);
  await first.client.receive([completion(1), snooze(2)]);
  assert.equal(shared.shown.length, 3, "snooze and turn receipts have independent identities");
  first.client.destroy(); second.client.destroy();
});

test("snooze expiry respects opt-in, focused-thread suppression and cancelled claims", async () => {
  const f = fixture();
  await f.client.receive([snooze()]);
  assert.equal(f.shared.snoozeClaims.size, 0);
  await f.button.onclick();
  f.focus("thread-one");
  await f.client.receive([snooze()]);
  f.blur();
  await f.client.receive([snooze()]);
  assert.equal(f.shared.shown.length, 0);
  await f.client.receive([snooze(2, {project_name: null, repository_name: "repo"})]);
  assert.equal(f.shared.shown[0].title, "repo · Snooze ended");
  f.client.claimSnooze = async () => false;
  await f.client.receive([snooze(3)]);
  assert.equal(f.shared.shown.length, 1);
  f.client.destroy();
});

test("approval requests notify once across tabs and clicking opens their conversation", async () => {
  const shared = {}, first = fixture(shared), second = fixture(shared);
  await first.button.onclick();
  second.env.Notification.permission = "granted";
  await Promise.all([first.client.receive([approval()]), second.client.receive([approval()])]);
  await first.client.receive([approval()]);
  assert.equal(shared.shown.length, 1);
  const notice = shared.shown[0];
  assert.equal(notice.title, "agent-coord · Codex is requesting approval");
  assert.equal(notice.options.body, "Check the build");
  assert.equal(notice.options.tag, "agent-coord-approval-request-one");
  await notice.onclick();
  assert.deepEqual(first.opened, ["thread-one"]);
  assert.equal(notice.closed, true);
  await first.client.receive([approval("request-two"), completion()]);
  assert.equal(shared.shown.length, 3, "new approvals and turn completion get independent alerts");
  first.client.destroy(); second.client.destroy();
});

test("approval notifications respect opt-in and focused conversations", async () => {
  const f = fixture();
  await f.client.receive([approval()]);
  assert.equal(f.shared.approvalClaims.size, 0);
  await f.button.onclick();
  f.focus("thread-one");
  await f.client.receive([approval()]);
  f.blur();
  await f.client.receive([approval()]);
  assert.equal(f.shared.shown.length, 0, "a focused approval stays consumed");
  await f.client.receive([approval("new-request")]);
  assert.equal(f.shared.shown.length, 1);
  f.client.destroy();
});

test("resolved approval requests remain quiet when their claim fails", async () => {
  const f = fixture();
  await f.button.onclick();
  f.client.claimApproval = async () => false;
  await f.client.receive([approval()]);
  assert.equal(f.shared.shown.length, 0);
  f.client.destroy();
});

test("permission is requested only by the enable action, and can be turned off", async () => {
  const f = fixture();
  assert.equal(f.permissionRequests(), 0);
  await f.client.receive([completion()]);
  assert.equal(f.shared.shown.length, 0);
  await f.button.onclick();
  assert.equal(f.permissionRequests(), 1);
  assert.equal(f.button.textContent, "Notifications on");
  await f.client.receive([completion()]);
  assert.equal(f.shared.shown.length, 1);
  await f.button.onclick();
  assert.equal(f.button.textContent, "Enable notifications");
  await f.client.receive([completion(2)]);
  assert.equal(f.shared.shown.length, 1);
  f.client.destroy();
});

test("a background tab notifies and clicking opens the matching thread", async () => {
  const f = fixture();
  await f.button.onclick();
  f.env.document.visibilityState = "hidden";
  await f.client.receive([completion()]);
  const notice = f.shared.shown[0];
  assert.equal(notice.title, "agent-coord · Turn finished");
  assert.equal(notice.options.body, "Check the build");
  await notice.onclick();
  assert.deepEqual(f.opened, ["thread-one"]);
  assert.equal(notice.closed, true);
  f.client.destroy();
});

test("multiple tabs and repeated event delivery claim only one desktop alert", async () => {
  const shared = {}, first = fixture(shared), second = fixture(shared);
  await first.button.onclick();
  second.env.Notification.permission = "granted";
  await Promise.all([first.client.receive([completion()]), second.client.receive([completion()])]);
  await first.client.receive([completion()]);
  assert.equal(shared.shown.length, 1);
  first.client.destroy(); second.client.destroy();
});

test("viewing the conversation in another focused tab suppresses all alerts", async () => {
  const shared = {}, first = fixture(shared), second = fixture(shared);
  await first.button.onclick();
  second.env.Notification.permission = "granted";
  second.focus("thread-one");
  await first.client.receive([completion()]);
  await second.client.receive([completion()]);
  assert.equal(shared.shown.length, 0);
  second.blur();
  await first.client.receive([completion()]);
  assert.equal(shared.shown.length, 0, "a suppressed completion stays consumed");
  await first.client.receive([completion(2)]);
  assert.equal(shared.shown.length, 1);
  first.client.destroy(); second.client.destroy();
});

test("visible overview and other conversations still receive notifications", async () => {
  const f = fixture();
  await f.button.onclick();
  f.focus(null);
  await f.client.receive([completion()]);
  f.focus("different-thread");
  await f.client.receive([completion(2)]);
  assert.equal(f.shared.shown.length, 2);
  f.client.destroy();
});

test("failed turns are labeled accurately and interrupted turns stay quiet", async () => {
  const f = fixture();
  await f.button.onclick();
  await f.client.receive([completion(1, {status: "failed"}), completion(2, {status: "interrupted"})]);
  assert.equal(f.shared.shown.length, 1);
  assert.equal(f.shared.shown[0].title, "agent-coord · Turn failed");
  f.client.destroy();
});

test("threads without a named project use their repository or the app name", async () => {
  const f = fixture();
  await f.button.onclick();
  await f.client.receive([completion(1, {project_name: null, repository_name: "repository"}), completion(2, {project_name: null})]);
  assert.equal(f.shared.shown[0].title, "repository · Turn finished");
  assert.equal(f.shared.shown[1].title, "Ribbon Field · Turn finished");
  f.client.destroy();
});

test("blocked or unsupported notifications never request permission on load", async () => {
  const f = fixture();
  f.env.Notification.permission = "denied";
  f.client.render();
  assert.equal(f.button.disabled, true);
  assert.equal(f.button.textContent, "Notifications blocked");
  assert.equal(f.permissionRequests(), 0);
  f.env.Notification = undefined;
  f.client.render();
  assert.equal(f.button.textContent, "Notifications unavailable");
  await f.client.receive([completion()]);
  assert.equal(f.shared.shown.length, 0);
  f.client.destroy();
});

test("declining the permission prompt leaves notifications off", async () => {
  const f = fixture();
  f.env.Notification.requestPermission = async () => f.env.Notification.permission = "denied";
  await f.button.onclick();
  assert.equal(f.button.textContent, "Notifications blocked");
  assert.equal(f.button["aria-pressed"], "false");
  await f.client.receive([completion()]);
  assert.equal(f.shared.shown.length, 0);
  f.client.destroy();
});

test("opening the thread while its claim is in flight suppresses the alert", async () => {
  const f = fixture();
  await f.button.onclick();
  let resolve;
  f.client.claim = () => new Promise(done => { resolve = done; });
  const delivery = f.client.receive([completion()]);
  await Promise.resolve();
  f.focus("thread-one");
  resolve(true);
  await delivery;
  assert.equal(f.shared.shown.length, 0);
  f.client.destroy();
});

test("turning notifications off in another tab is respected immediately", async () => {
  const shared = {}, first = fixture(shared), second = fixture(shared);
  await first.button.onclick();
  second.env.Notification.permission = "granted";
  await second.button.onclick();
  await first.client.receive([completion()]);
  assert.equal(shared.shown.length, 0);
  first.client.destroy(); second.client.destroy();
});

test("restricted storage still permits notifications for the current page", async () => {
  const f = fixture();
  f.env.localStorage.getItem = f.env.localStorage.setItem = () => { throw new Error("Storage unavailable"); };
  await f.button.onclick();
  await f.client.receive([completion()]);
  assert.equal(f.shared.shown.length, 1);
  assert.deepEqual(f.errors, []);
  f.client.destroy();
});

test("notification delivery failures do not break subsequent completion handling", async () => {
  const f = fixture();
  await f.button.onclick();
  f.client.claim = async () => { throw new Error("Connection lost"); };
  await f.client.receive([completion()]);
  assert.equal(f.errors.length, 1);
  f.client.claim = async () => true;
  await f.client.receive([completion(2)]);
  assert.equal(f.shared.shown.length, 1);
  f.client.destroy();
});
