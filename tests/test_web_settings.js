"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const root = "../plugins/agent-coord/scripts/agent_coord/web/";
const settingsSource = fs.readFileSync(require.resolve(root + "settings.js"), "utf8");
const appSource = fs.readFileSync(require.resolve(root + "app.js"), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));

function fixture({remote = false, desktop, embedded = false, nativePhone = false, ok = true} = {}) {
  const nodes = new Map(), calls = [], notices = [], panels = [], speech = [], messages = [];
  class Element extends EventTarget {
    constructor() { super(); this.hidden = true; this.textContent = ""; this.disabled = false; this.children = []; }
    click() { return this.onclick?.({preventDefault() {}}); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
  }
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const document = Object.assign(new EventTarget(), {getElementById: $, createElement: () => new Element(),
    querySelector: selector => nativePhone && selector === "#native-notification-style" ? {} : null});
  const keyListeners = [], addListener = document.addEventListener.bind(document);
  document.addEventListener = (type, listener, options) => {
    if (type === "keydown") keyListeners.push({listener, capture: options === true || options?.capture === true});
    addListener(type, listener, options);
  };
  // Model the document capture -> input -> document bubble event path.
  const keyDown = (target, consume = () => {}, properties = {}) => {
    const event = Object.assign(new Event("keydown", {bubbles: true, cancelable: true}), {key: "Escape", ...properties});
    Object.defineProperty(event, "target", {value: target});
    for (const item of keyListeners.filter(item => item.capture)) item.listener(event);
    if (!event.cancelBubble) consume(event);
    if (!event.cancelBubble) for (const item of keyListeners.filter(item => !item.capture)) item.listener(event);
    return event;
  };
  const env = Object.assign(new EventTarget(), {document, location: {origin: "https://ribbon.test", href: "/settings"},
    agentCoordDesktop: desktop, navigator: {clipboard: {}},
    fetch: async (url, options) => {
      calls.push({url, options});
      return {ok, json: async () => url.endsWith("config") ? {remote, token: "secret-token"} : {enabled: true}};
    },
    agentCoordSpeech: {mountSettings: async (...args) => speech.push(args)},
    agentCoordJev: {mountSettings: () => {}},
    RemoteAccessPanel: class {
      constructor(options) { this.options = options; panels.push(this); }
      open() { this.opened = true; }
    },
  });
  for (const name of ["TurnNotifications", "PhoneNotifications"]) {
    env[name] = class {
      constructor(options) { this.options = options; this.name = name; notices.push(this); }
      async start() { this.started = true; }
      ready() { assert.fail("Settings must not consume notification destinations"); }
    };
  }
  env.window = env; env.parent = embedded ? {document, postMessage: (...args) => messages.push(args)} : env;
  vm.runInNewContext(settingsSource, env);
  return {env, $, calls, notices, panels, speech, messages, keyDown};
}

test("local Settings mounts global preferences and uses authenticated mutation requests", async () => {
  const f = fixture(); await settle();
  assert.equal(f.notices[0].name, "TurnNotifications");
  assert.equal(f.speech.length, 1);
  assert.equal(f.speech[0][0], f.$("speech-settings"));
  assert.equal(f.$("remote-settings").hidden, false);
  assert.equal(f.panels[0].opened, true);
  assert.equal(f.panels[0].options.container, f.$("remote-settings"));
  await f.panels[0].options.request("disable", {});
  const request = f.calls.at(-1);
  assert.equal(request.url, "/api/remote/disable");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.headers["X-Agent-Coord-Token"], "secret-token");
  assert.equal(f.$("file-settings").hidden, true);
});

test("remote Settings exposes device push controls without consuming click routes or exposing Mac administration", async () => {
  const f = fixture({remote: true}); await settle();
  assert.equal(f.notices[0].name, "PhoneNotifications");
  assert.equal(f.notices[0].options.listen, false);
  assert.equal(f.notices[0].started, true);
  assert.equal(f.$("remote-settings").hidden, true);
  assert.equal(f.panels.length, 0);
  await f.notices[0].options.api("push/subscribe", {subscription: {}});
  assert.equal(f.calls.at(-1).options.headers["X-Agent-Coord-Token"], "secret-token");
});

test("native Settings routes editor choice and Done through the native window", async () => {
  const operations = []; let closed = false;
  const desktop = {closeSettings: async () => { closed = true; }, fileEditor: async action => {
    operations.push(action);
    return action === "choose" ? {name: "Editor", path: "/Applications/Editor.app"} : {name: "System default", path: ""};
  }};
  const f = fixture({desktop}); await settle();
  assert.equal(f.$("file-settings").hidden, false);
  assert.equal(f.$("reset-file-editor").disabled, true);
  await f.$("choose-file-editor").click();
  assert.equal(f.$("file-editor-name").textContent, "Editor");
  assert.equal(f.$("reset-file-editor").disabled, false);
  await f.$("reset-file-editor").click();
  assert.equal(f.$("file-editor-name").textContent, "System default");
  assert.deepEqual(operations, ["get", "choose", "reset"]);
  f.$("close-settings").click(); await settle();
  assert.equal(closed, true);
});

test("iPhone app keeps its existing native push controls", async () => {
  const f = fixture({remote: true, embedded: true, nativePhone: true}); await settle();
  assert.equal(f.notices.length, 0);
  assert.equal(f.$("notifications").hidden, true);
  assert.match(f.$("notification-description").textContent, /Connection settings/);
  assert.equal(f.speech.length, 1);
});

test("embedded Settings closes on Escape and reports configuration errors", async () => {
  const f = fixture({embedded: true, ok: false}); await settle();
  assert.equal(f.$("settings-error").hidden, false);
  assert.match(f.$("settings-error").textContent, /Could not load Settings/);
  const event = Object.assign(new Event("keydown", {cancelable: true}), {key: "Escape"});
  f.env.document.dispatchEvent(event);
  assert.equal(event.defaultPrevented, true);
  assert.equal(f.messages[0][0].type, "ribbon-field-close-settings");
  assert.equal(f.messages[0][1], "https://ribbon.test");
});

test("Escape from a text input closes Settings before the field can consume the key", async () => {
  const f = fixture({embedded: true}); await settle();
  const input = {tagName: "INPUT", type: "url", value: "http://localhost/unsaved", closest: () => null};
  const event = f.keyDown(input, event => { event.preventDefault(); event.stopPropagation(); });
  assert.equal(event.defaultPrevented, true);
  assert.equal(f.messages.length, 1);
  assert.equal(f.messages[0][0].type, "ribbon-field-close-settings");
  assert.equal(input.value, "http://localhost/unsaved");
});

test("Escape retains composition and nested control cancellation", async () => {
  const f = fixture({embedded: true}); await settle();
  const input = {tagName: "INPUT", closest: () => null};
  assert.equal(f.keyDown(input, undefined, {isComposing: true}).defaultPrevented, false);
  for (const target of [{tagName: "SELECT"}, {role: "combobox", expanded: true}]) {
    target.closest = () => target;
    let reachedControl = false;
    f.keyDown(target, event => { reachedControl = true; event.preventDefault(); event.stopPropagation(); });
    assert.equal(reachedControl, true);
  }
  f.env.document.querySelector = selector => selector === "dialog[open]" ? {} : null;
  assert.equal(f.keyDown(input).defaultPrevented, false);
  assert.equal(f.messages.length, 0);
  f.env.document.querySelector = () => null;
  f.keyDown(input);
  assert.equal(f.messages.length, 1);
});

test("browser Settings preserves the workspace and unloads previews when its dialog closes", () => {
  const nodes = new Map(), env = new EventTarget(), navigation = [];
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, Object.assign(new EventTarget(), {
      attributes: {}, visible: true, getClientRects() { return this.visible ? [1] : []; },
      getAttribute(key) { return this.attributes[key] || null; },
      removeAttribute(key) { delete this.attributes[key]; },
      focus() { env.focused = id; },
    }));
    return nodes.get(id);
  };
  const frame = $("settings-frame"), dialog = $("settings-dialog");
  frame.contentWindow = {};
  Object.defineProperty(frame, "src", {set(value) { this.attributes.src = value; }});
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.dispatchEvent(new Event("close")); };
  const context = {window: env, $, location: {origin: "https://ribbon.test"},
    setNavigation: open => navigation.push(open), showError: assert.fail};
  vm.runInNewContext(appSource.slice(appSource.indexOf('$("open-settings").onclick'), appSource.indexOf('const mobileNavigation')), context);
  const open = () => $("open-settings").onclick({preventDefault() {}});
  open();
  assert.equal(dialog.open, true);
  assert.equal(frame.getAttribute("src"), "/settings?embedded=1");
  assert.deepEqual(navigation, [false]);
  const message = (origin, source) => env.dispatchEvent(Object.assign(new Event("message"),
    {origin, source, data: {type: "ribbon-field-close-settings"}}));
  message("https://evil.test", frame.contentWindow);
  message("https://ribbon.test", {});
  assert.equal(dialog.open, true);
  $("open-settings").visible = false;
  message("https://ribbon.test", frame.contentWindow);
  assert.equal(dialog.open, false);
  assert.equal(frame.getAttribute("src"), null);
  assert.equal(env.focused, "menu-toggle");
  open();
  assert.equal(frame.getAttribute("src"), "/settings?embedded=1", "reopening loads fresh preference state");
  dialog.close();
  let nativeOpened = false;
  env.agentCoordDesktop = {openSettings: async () => { nativeOpened = true; }};
  open();
  assert.equal(nativeOpened, true);
  assert.equal(dialog.open, false);
});
