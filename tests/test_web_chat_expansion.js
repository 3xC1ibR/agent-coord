"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup({contextCollapsed = false, sidebarCollapsed = false, mode = "context", compact = false} = {}) {
  const elements = new Map(), handlers = {};
  function element() {
    const classes = new Set();
    return {hidden: false, attrs: {},
      classList: {contains: name => classes.has(name), toggle(name, value) { value ? classes.add(name) : classes.delete(name); }},
      setAttribute(name, value) { this.attrs[name] = value; },
      contains(node) { return node === this; },
      focus() { c.document.activeElement = this; },
    };
  }
  const c = {
    $: id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    document: {body: element(), querySelector: () => null, addEventListener: (name, fn) => { handlers[name] = fn; }},
    state: {selected: "thread", agentTreeController: {mode}},
    contextCollapsed, sidebarCollapsed,
    compactContext: {matches: compact}, mobileNavigation: {matches: false},
    isSessionDraft: () => false,
  };
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf("function setNavigation("), source.indexOf('$("menu-toggle").onclick')), c);
  vm.runInContext(source.slice(source.indexOf("function layoutContext("), source.indexOf("function setContextCollapsed(")), c);
  vm.runInContext(source.slice(source.indexOf("function setChatExpanded("), source.indexOf("// Desktop lists the header")), c);
  const keydown = source.indexOf('document.addEventListener("keydown", event => {', source.indexOf("state.mobileChat = new MobileChat"));
  vm.runInContext(source.slice(keydown, source.indexOf('  if (document.body.classList.contains("navigation-open"))', keydown)) + "});", c);
  c.setNavigation(false); c.layoutContext();
  return {c, handlers};
}

for (const sidebarCollapsed of [false, true]) for (const contextCollapsed of [false, true]) for (const mode of ["context", "agents"]) {
  test(`expand/collapse restores ${mode}, context collapsed=${contextCollapsed}, sidebar collapsed=${sidebarCollapsed}`, () => {
    const {c} = setup({contextCollapsed, sidebarCollapsed, mode});
    const button = c.$("expand-chat");
    button.onclick();
    assert.equal(c.document.body.classList.contains("chat-expanded"), true);
    assert.equal(c.$("context-panel").hidden, true);
    assert.equal(c.$("conversation").classList.contains("context-collapsed"), true);
    assert.equal(c.$("conversation").classList.contains("agents-visible"), false);
    assert.equal(button.attrs["aria-pressed"], "true");
    assert.equal(button.attrs["aria-label"], "Collapse chat");
    c.layoutContext(); // Resize or refreshed thread metadata must not reopen context.
    assert.equal(c.$("context-panel").hidden, true);
    button.onclick();
    assert.equal(c.document.body.classList.contains("chat-expanded"), false);
    assert.equal(c.$("context-panel").hidden, contextCollapsed);
    assert.equal(c.$("conversation").classList.contains("agents-visible"), !contextCollapsed && mode === "agents");
    assert.equal(c.contextCollapsed, contextCollapsed);
    assert.equal(c.document.body.classList.contains("sidebar-collapsed"), sidebarCollapsed);
    assert.equal(button.attrs["aria-pressed"], "false");
    assert.equal(button.attrs["aria-label"], "Expand chat");
  });
}

test("Escape restores context and focuses the expansion control", () => {
  const {c, handlers} = setup();
  c.$("context-panel").focus();
  c.setChatExpanded(true);
  assert.equal(c.document.activeElement, c.$("expand-chat"));
  let prevented = false;
  handlers.keydown({key: "Escape", preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(c.$("context-panel").hidden, false);
  assert.equal(c.document.activeElement, c.$("expand-chat"));
});

test("expansion preserves automatic context sizing across a viewport change", () => {
  const {c} = setup({contextCollapsed: null, compact: true});
  c.setChatExpanded(true);
  c.compactContext.matches = false;
  c.layoutContext();
  assert.equal(c.$("context-panel").hidden, true);
  c.setChatExpanded(false);
  assert.equal(c.$("context-panel").hidden, false);
  assert.equal(c.contextCollapsed, null);
});
