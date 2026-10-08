"use strict";
const assert = require("node:assert/strict");
const {test} = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const {randomUUID} = require("node:crypto");
const TurnNotifications = require("../plugins/agent-coord/scripts/agent_coord/web/notifications.js");
const ChatImageAttachments = require("../plugins/agent-coord/scripts/agent_coord/web/image-attachments.js");
const source = fs.readFileSync(require.resolve("../desktop/macos/bridge.js"), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));

function fixture(preferences = {}, permission = "default", subframe = false) {
  const messages = [];
  const replies = {};
  const palettes = [];
  class Storage {
    constructor() { this.values = new Map(); }
    getItem(key) { return this.values.get(String(key)) ?? null; }
    setItem(key, value) { this.values.set(String(key), String(value)); }
    removeItem(key) { this.values.delete(String(key)); }
  }
  const env = new EventTarget();
  const nodes = new Map(), document = new EventTarget();
  class Element extends EventTarget {
    constructor(id = "") {
      super(); Object.assign(this, {id, hidden: false, disabled: false, value: "", textContent: "", dataset: {},
        maxLength: 100000, selectionStart: 0, selectionEnd: 0, clicks: 0, submits: 0, children: new Map()});
      const classes = new Set();
      this.classList = {add: value => classes.add(value), remove: value => classes.delete(value),
        toggle: (value, on) => on ? classes.add(value) : classes.delete(value)};
    }
    closest(selector) {
      if (selector === "[hidden]") return this.hidden ? this : this.parent?.closest(selector) || null;
      if (selector === "button[data-thread]") return this.dataset.thread ? this : this.parent?.closest(selector) || null;
      return null;
    }
    contains(element) { return element === this || element?.parent === this; }
    focus() { document.activeElement = this; }
    select() { this.selectionStart = 0; this.selectionEnd = this.value.length; }
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
    setRangeText(text, start, end) {
      this.value = this.value.slice(0, start) + text + this.value.slice(end);
      this.selectionStart = this.selectionEnd = start + text.length;
    }
    click() { this.clicks++; this.onclick?.(new Event("click")); }
    requestSubmit() { this.submits++; }
    setAttribute() {}
    before(element) { nodes.set(element.id, element); }
    querySelector(selector) {
      if (!this.children.has(selector)) this.children.set(selector, new Element());
      return this.children.get(selector);
    }
    dispatchEvent(event) {
      if (event.type === "drop" && event.bubbles) {
        Object.defineProperty(event, "target", {value: this});
        return document.dispatchEvent(event);
      }
      return super.dispatchEvent(event);
    }
  }
  for (const id of ["home", "new-session", "message", "composer", "conversation", "search", "send", "menu-toggle",
    "expand-chat", "session-name", "image-drop-hint", "error", "new-cwd", "workspace-choice", "workspace-path-label",
    "create-dialog", "permissions-dialog"]) nodes.set(id, new Element(id));
  nodes.get("error").hidden = true;
  Object.assign(document, {visibilityState: "hidden", hasFocus: () => false, body: new Element(),
    getElementById: id => nodes.get(id), createElement: () => new Element(),
    querySelector: selector => selector === "dialog[open]" ?
      [...nodes.values()].find(el => el.id.endsWith("-dialog") && el.open) || null : null});
  class DataTransfer {
    constructor() { this.files = []; this.types = ["Files"]; this.items = {add: file => this.files.push(file)}; }
  }
  class DragEvent extends Event {
    constructor(type, options) { super(type, options); this.dataTransfer = options.dataTransfer; }
  }
  Object.assign(env, {
    Event, EventTarget, Storage, crypto: {randomUUID}, localStorage: new Storage(),
    document, DataTransfer, DragEvent, queueMicrotask,
    MutationObserver: class { observe() {} disconnect() {} },
    DesktopCommandPalette: class {
      constructor(options) {
        Object.assign(this, options); this.dialog = new Element("desktop-command-dialog");
        nodes.set(this.dialog.id, this.dialog); palettes.push(this);
      }
      toggle() { this.dialog.open = !this.dialog.open; return true; }
    },
    location: {hash: "#thread-one", pathname: "/", search: "", href: "http://127.0.0.1:1234/#thread-one"},
    setInterval: () => 1, clearInterval() {}, focus() {}, isSecureContext: true,
    webkit: {messageHandlers: {desktop: {async postMessage(body) {
      messages.push(JSON.parse(JSON.stringify(body)));
      if (Object.hasOwn(replies, body.action)) return replies[body.action];
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
  return {env, messages, preferences, nodes, replies, palettes};
}

test("session commands cycle the filtered display order with wraparound and overview entry", async () => {
  const f = fixture(), opened = [];
  const buttons = ["third", "first", "second"].map(thread => ({dataset: {thread}}));
  let current = null;
  f.nodes.set("sessions", {id: "sessions", querySelectorAll: () => buttons,
    querySelector: () => buttons.find(button => button.dataset.thread === current)});
  f.env.select = async id => { current = id; opened.push(id); };
  const command = f.env.agentCoordDesktop.command;
  assert.equal(command("nextSession"), true);
  command("nextSession"); command("nextSession"); command("nextSession");
  command("previousSession");
  assert.deepEqual(opened, ["third", "first", "second", "third", "second"]);
  current = null;
  command("previousSession");
  assert.equal(current, "second");
  current = "outside-current-view";
  command("nextSession");
  assert.equal(current, "third");
  f.nodes.get("permissions-dialog").open = true;
  assert.equal(command("nextSession"), false);
  f.nodes.get("permissions-dialog").open = false;
  buttons.length = 0;
  assert.equal(command("previousSession"), false);
  await settle();
});

test("session cycling includes stacked pane tabs and respects pane dialogs", () => {
  const f = fixture(), focused = [];
  const panes = f.env.agentCoordPanes = {active: true, selected: "a",
    layout: {groups: [["a", "stacked"], ["b"]]},
    focus(id) { this.selected = id; focused.push(id); }};
  const command = f.env.agentCoordDesktop.command;
  command("nextSession"); command("nextSession"); command("nextSession"); command("previousSession");
  assert.deepEqual(focused, ["stacked", "b", "a", "b"]);
  panes.focusedRecord = {frame: {contentDocument: {querySelector: () => ({open: true})}}};
  assert.equal(command("nextSession"), false);
});

test("session navigation failures use the existing error display", async () => {
  const f = fixture();
  f.nodes.set("sessions", {id: "sessions", querySelectorAll: () => [{dataset: {thread: "one"}}], querySelector: () => null});
  f.env.select = async () => { throw new Error("Could not open session"); };
  f.env.agentCoordDesktop.command("nextSession");
  await settle();
  assert.equal(f.nodes.get("error").hidden, false);
  assert.equal(f.nodes.get("error").querySelector("span").textContent, "Could not open session");
});

test("initialization checks permission without asking, and subframes receive no bridge", async () => {
  const {env, messages} = fixture();
  await settle();
  assert.equal(env.Notification.permission, "default");
  assert.deepEqual(messages.map(m => m.action), ["permission"]);
  const child = fixture({}, "default", true);
  assert.equal(child.env.Notification, undefined);
  assert.equal(child.messages.length, 0);
});

test("file browser follows the focused workspace and inserts a path without sending", async () => {
  const f = fixture();
  const workspace = f.env.document.createElement(); workspace.textContent = "/tmp/project";
  f.nodes.set("workspace", workspace);
  f.env.document.dispatchEvent(new Event("DOMContentLoaded"));
  assert.equal(f.messages.find(m => m.action === "windowState").workspace, "/tmp/project");
  const field = f.nodes.get("message"); field.value = "Review"; field.setSelectionRange(6, 6);
  assert.equal(f.env.agentCoordDesktop.insertPaths(["/tmp/project/a file.swift"]), true);
  assert.equal(field.value, 'Review\n"/tmp/project/a file.swift"');
  assert.equal(f.nodes.get("composer").submits, 0);
  assert.equal(f.env.agentCoordDesktop.insertPaths(["/tmp/wrong"], "another-thread"), false);
  assert.equal(field.value.includes("wrong"), false);
  f.env.agentCoordDesktop.command("toggleFiles");
  assert.equal(f.messages.at(-1).action, "toggleFiles");
  f.nodes.get("permissions-dialog").open = true;
  assert.equal(f.env.agentCoordDesktop.insertPaths(["/tmp/other"]), false);
});

test("Roll up command and palette route to the current window and respect dialogs", () => {
  const one = fixture(), two = fixture();
  for (const f of [one, two]) {
    f.nodes.set("roll-up-start", f.env.document.createElement("button"));
    f.env.document.dispatchEvent(new Event("DOMContentLoaded"));
  }
  assert.equal(one.env.agentCoordDesktop.command("rollUp"), true);
  assert.equal(one.nodes.get("roll-up-start").clicks, 1);
  assert.equal(two.nodes.get("roll-up-start").clicks, 0);
  const command = one.palettes[0].actions().find(action => action.id === "rollUp");
  assert.equal(command.shortcut, "⌥⌘R");
  command.run();
  assert.equal(one.nodes.get("roll-up-start").clicks, 2);
  one.nodes.get("permissions-dialog").open = true;
  assert.equal(one.env.agentCoordDesktop.command("rollUp"), false);
});

test("native edit commands and file insertion target the focused conversation pane", async () => {
  const parent = fixture(), child = fixture({}, "default", true);
  const field = child.nodes.get("message"); field.ownerDocument = child.env.document;
  field.value = "Pane draft"; field.setSelectionRange(field.value.length, field.value.length);
  parent.nodes.get("message").value = "Outer draft";
  let maximized = 0, tiled = 0;
  parent.nodes.set("tile-threads", parent.env.document.createElement("button"));
  parent.env.agentCoordPanes = {active: true, selected: "pane-thread",
    focusedRecord: {frame: {contentDocument: child.env.document}},
    maximize: () => maximized++, toggleCurrent: () => { tiled++; return true; },
    tileCurrent: () => { throw new Error("The shortcut must toggle, not rearrange panes"); }};
  parent.env.document.dispatchEvent(new Event("DOMContentLoaded"));
  assert.equal(parent.env.agentCoordDesktop.command("focusMessage"), true);
  assert.equal(child.env.document.activeElement, field);
  assert.equal(parent.env.agentCoordDesktop.command("sendMessage"), true);
  assert.equal(child.nodes.get("composer").submits, 1);
  assert.equal(parent.nodes.get("composer").submits, 0);
  parent.env.agentCoordDesktop.command("expandConversation"); assert.equal(maximized, 1);
  parent.env.agentCoordDesktop.command("tileThreads"); assert.equal(tiled, 1);
  parent.replies.chooseFiles = [{path: "/tmp/pane notes.md"}];
  parent.env.agentCoordDesktop.command("insertFiles"); await settle();
  assert.equal(field.value, 'Pane draft\n"/tmp/pane notes.md"');
  assert.equal(parent.nodes.get("message").value, "Outer draft");
  child.nodes.get("permissions-dialog").open = true;
  assert.equal(parent.env.agentCoordDesktop.command("sendMessage"), false);
  assert.equal(parent.env.agentCoordDesktop.command("commandPalette"), false);
  assert.equal(parent.env.agentCoordDesktop.command("tileThreads"), false);
});

test("switching pane during a native file panel cannot insert into the new target", async () => {
  const parent = fixture(), first = fixture({}, "default", true), second = fixture({}, "default", true);
  const firstRecord = {frame: {contentDocument: first.env.document}}, secondRecord = {frame: {contentDocument: second.env.document}};
  parent.env.agentCoordPanes = {active: true, selected: "first", focusedRecord: firstRecord};
  let resolve;
  parent.replies.chooseFiles = new Promise(done => { resolve = done; });
  parent.env.agentCoordDesktop.command("insertFiles");
  parent.env.agentCoordPanes.selected = "second"; parent.env.agentCoordPanes.focusedRecord = secondRecord;
  resolve([{path: "/tmp/notes.md"}]); await settle();
  assert.equal(first.nodes.get("message").value, "");
  assert.equal(second.nodes.get("message").value, "");
  assert.match(parent.nodes.get("error").querySelector("span").textContent, /destination changed/);
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
  assert.equal(message.title, "Ribbon Field · Turn finished");
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

function drop(fixture, files, entries = []) {
  const event = new fixture.env.DragEvent("drop", {cancelable: true,
    dataTransfer: {files, types: ["Files"], items: entries.map(entry => ({webkitGetAsEntry: () => entry}))}});
  Object.defineProperty(event, "target", {value: fixture.nodes.get("conversation")});
  fixture.env.document.dispatchEvent(event);
  return event;
}

test("mixed drops preserve the draft and attach images through the existing attachment controller", async () => {
  const f = fixture(), field = f.nodes.get("message");
  field.value = "Review  please"; field.setSelectionRange(7, 7);
  f.replies.dropFiles = [{path: '/tmp/my "notes".md', directory: false}, {path: "/tmp/folder", directory: true}];
  const attachments = new ChatImageAttachments({document: f.env.document, getThread: () => "thread-one",
    canAttach: () => true, onChange() {}, onError: error => { throw error; }, readFile: async () => "data:image/png;base64,YQ=="});
  attachments.bind();
  const event = drop(f, [{name: 'my "notes".md', type: "text/markdown"}, {name: "folder", type: ""},
    {name: "preview.png", type: "image/png", size: 1}]);
  await settle();
  assert.equal(event.defaultPrevented, true);
  assert.equal(field.value, 'Review "/tmp/my \\"notes\\".md"\n"/tmp/folder" please');
  assert.equal(attachments.snapshot()[0].name, "preview.png");
  assert.equal(f.nodes.get("composer").submits, 0);
  assert.deepEqual(f.messages.find(m => m.action === "dropFiles").names, ['my "notes".md', "folder"]);
});

test("image-only drops stay on the existing web path and a directory with an image extension is a reference", async () => {
  const f = fixture();
  assert.equal(drop(f, [{name: "photo.png", type: "image/png", size: 1}]).defaultPrevented, false);
  assert.equal(f.messages.some(m => m.action === "dropFiles"), false);
  f.replies.dropFiles = [{path: "/tmp/folder.png", directory: true}];
  drop(f, [{name: "folder.png", type: ""}], [{name: "folder.png", isDirectory: true}]);
  await settle();
  assert.equal(f.nodes.get("message").value, '"/tmp/folder.png"');
});

test("an asynchronous drop cannot insert into another conversation", async () => {
  const f = fixture();
  let resolve;
  f.replies.dropFiles = new Promise(done => { resolve = done; });
  drop(f, [{name: "notes.md", type: "text/markdown"}]);
  f.env.location.hash = "#thread-two";
  f.nodes.get("message").value = "Second conversation draft";
  resolve([{path: "/tmp/notes.md", directory: false}]);
  await settle();
  assert.equal(f.nodes.get("message").value, "Second conversation draft");
  assert.match(f.nodes.get("error").querySelector("span").textContent, /destination changed/);
});

test("typing during a file panel is preserved and cancelling the panel leaves the draft alone", async () => {
  const f = fixture(), field = f.nodes.get("message");
  let resolve;
  f.replies.chooseFiles = new Promise(done => { resolve = done; });
  f.env.agentCoordDesktop.command("insertFiles");
  field.value = "New text"; field.setSelectionRange(8, 8);
  resolve([{path: "/tmp/a.md", directory: false}]);
  await settle();
  assert.equal(field.value, 'New text\n"/tmp/a.md"');
  f.replies.chooseFiles = [];
  f.env.agentCoordDesktop.command("insertFiles");
  await settle();
  assert.equal(field.value, 'New text\n"/tmp/a.md"');
  assert.equal(f.nodes.get("error").hidden, true);
});

test("folder drops supply a quoted path to the cd command without sending it", async () => {
  const f = fixture(), field = f.nodes.get("message");
  field.value = "/cd "; field.setSelectionRange(4, 4);
  f.replies.dropFiles = [{path: "/tmp/Some Project", directory: true}];
  drop(f, [{name: "Some Project", type: ""}]);
  await settle();
  assert.equal(field.value, '/cd "/tmp/Some Project"');
  assert.equal(f.nodes.get("composer").submits, 0);
});

test("drops respect closed conversations, dialogs, and message length limits", async () => {
  const f = fixture(), field = f.nodes.get("message");
  field.disabled = true;
  assert(drop(f, [{name: "a.txt", type: "text/plain"}]).defaultPrevented);
  assert(!f.messages.some(m => m.action === "dropFiles"));
  field.disabled = false; field.value = "Draft"; field.maxLength = 6;
  f.replies.dropFiles = [{path: "/tmp/a.txt", directory: false}];
  drop(f, [{name: "a.txt", type: "text/plain"}]);
  await settle();
  assert.equal(field.value, "Draft");
  assert.match(f.nodes.get("error").querySelector("span").textContent, /length limit/);
  f.nodes.get("permissions-dialog").open = true;
  assert.equal(f.env.agentCoordDesktop.command("insertFiles"), false);
});

test("menu commands route to controls, respect disabled send, and do not escape modal dialogs", () => {
  const f = fixture(), command = f.env.agentCoordDesktop.command;
  assert(command("newSession")); assert.equal(f.nodes.get("new-session").clicks, 1);
  assert(command("findThread")); assert.equal(f.env.document.activeElement.id, "search");
  assert(command("focusMessage")); assert.equal(f.env.document.activeElement.id, "message");
  assert(command("toggleSidebar")); assert.equal(f.nodes.get("menu-toggle").clicks, 1);
  assert(command("expandConversation")); assert.equal(f.nodes.get("expand-chat").clicks, 1);
  assert(command("sendMessage")); assert.equal(f.nodes.get("composer").submits, 1);
  f.nodes.get("send").disabled = true;
  assert.equal(command("sendMessage"), false);
  f.nodes.get("permissions-dialog").open = true;
  for (const name of ["workspace", "findThread", "newSession", "focusMessage", "toggleSidebar", "sendMessage"]) {
    assert.equal(command(name), false, name);
  }
});

test("desktop controls publish window state and request a new window without navigating the original", async () => {
  const f = fixture();
  f.nodes.get("session-name").textContent = "Build native app";
  f.env.document.dispatchEvent(new Event("DOMContentLoaded"));
  const state = f.messages.find(m => m.action === "windowState");
  assert.equal(state.title, "Build native app");
  assert(state.commands.includes("focusMessage"));
  f.nodes.get("desktop-new-window").click();
  assert.equal(f.messages.find(m => m.action === "newWindow").url, f.env.location.href);
  const thread = f.nodes.get("session-name"); thread.dataset.thread = "another thread";
  const event = new Event("click", {cancelable: true});
  Object.assign(event, {metaKey: true, button: 0});
  Object.defineProperty(event, "target", {value: thread});
  f.env.document.dispatchEvent(event);
  assert.equal(f.messages.filter(m => m.action === "newWindow").at(-1).url, "/#another%20thread");
  assert.equal(f.env.location.hash, "#thread-one");
});

test("the palette uses existing navigation and available commands, and Command K toggles only its own dialog", async () => {
  const f = fixture(), opened = [];
  f.env.select = async id => opened.push(id);
  f.env.fetch = async (path, options) => {
    assert.equal(path, "/api/browser/threads?archived=false");
    assert.equal(options.cache, "no-store");
    return {ok: true, json: async () => ({data: [{thread_id: "outside-current-view", title: "Another thread"}]})};
  };
  f.env.document.dispatchEvent(new Event("DOMContentLoaded"));
  const palette = f.palettes[0];
  const actions = palette.actions();
  assert(actions.some(item => item.id === "focusMessage"));
  assert(!actions.some(item => item.id === "sendMessage"));
  actions.find(item => item.id === "newWindow").run();
  assert.equal(f.messages.filter(m => m.action === "newWindow").at(-1).url, "/");
  const threads = await palette.loadThreads(new AbortController().signal);
  await palette.selectThread(threads[0].thread_id);
  assert.deepEqual(opened, ["outside-current-view"]);
  assert(f.env.agentCoordDesktop.command("commandPalette"));
  assert.equal(palette.dialog.open, true);
  assert.equal(f.env.agentCoordDesktop.command("newSession"), false);
  assert(f.env.agentCoordDesktop.command("commandPalette"));
  assert.equal(palette.dialog.open, false);
  f.nodes.get("permissions-dialog").open = true;
  assert.equal(f.env.agentCoordDesktop.command("commandPalette"), false);
});
