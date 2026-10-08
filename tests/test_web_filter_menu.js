"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const createFilterMenu = require("../plugins/agent-coord/scripts/agent_coord/web/filter-menu.js");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");

function setup() {
  const controls = Object.fromEntries(["filter-menu", "filter-toggle", "clear-filters", "view", "phase-filter", "repository", "project"]
    .map(id => [id, {value: "", open: false, listeners: {}, focusCount: 0,
      addEventListener(type, handler) { this.listeners[type] = handler; },
      dispatchEvent(event) { this.listeners[event.type]?.(event); },
      focus() { this.focusCount++; },
      contains(target) { return target.insideMenu === true; }}]));
  const document = {listeners: {}, getElementById: id => controls[id],
    addEventListener(type, handler) { this.listeners[type] = handler; }};
  createFilterMenu(document);
  return {controls, document};
}

test("clearing filters reloads closed threads and restores the default selections", () => {
  const {controls} = setup();
  controls.view.value = "archived";
  controls["phase-filter"].value = "implementation";
  controls.repository.value = "repo-1";
  controls.project.value = "project-1";
  controls["filter-menu"].open = true;
  let changes = 0;
  controls.view.addEventListener("change", () => changes++);
  controls["clear-filters"].dispatchEvent(new Event("click"));
  assert.equal(controls.view.value, "active");
  assert.equal(controls["phase-filter"].value, "");
  assert.equal(controls.repository.value, "");
  assert.equal(controls.project.value, "");
  assert.equal(changes, 1);
  assert.equal(controls["filter-menu"].open, false);
  assert.equal(controls["filter-toggle"].focusCount, 1);
});

test("clearing open-thread filters updates the current list", () => {
  const {controls} = setup();
  controls.view.value = "active";
  controls.repository.value = "repo-1";
  let changes = 0;
  controls["phase-filter"].addEventListener("change", () => changes++);
  controls["clear-filters"].dispatchEvent(new Event("click"));
  assert.equal(changes, 1);
});

test("outside pointer and Escape close the filter menu", () => {
  const {controls, document} = setup();
  const menu = controls["filter-menu"];
  menu.open = true;
  document.listeners.pointerdown({target: {insideMenu: true}});
  assert.equal(menu.open, true);
  document.listeners.pointerdown({target: {insideMenu: false}});
  assert.equal(menu.open, false);
  menu.open = true;
  let prevented = false;
  document.listeners.keydown({key: "Escape", preventDefault() { prevented = true; }});
  assert.equal(menu.open, false);
  assert.equal(controls["filter-toggle"].focusCount, 1);
  assert.equal(prevented, true);
});

function closedToggle() {
  const {controls} = setup();
  for (const id of ["show-now", "show-later", "show-closed"])
    controls[id] = {attributes: {}, setAttribute(key, value) { this.attributes[key] = value; }};
  const calls = [];
  const context = {$: id => controls[id], action: fn => fn(),
    savedViews: {persist: async () => calls.push(["persist", controls.view.value])},
    refreshList: async () => calls.push(["refresh", controls.view.value])};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function renderClosedToggle("), source.indexOf("function renderList(")), context);
  vm.runInContext(source.slice(source.indexOf('$("view").onchange ='), source.indexOf('$("repository").onchange =')), context);
  return {controls, calls, context};
}

test("visible Closed toggle reloads history and returns to open threads while retaining other filters", async () => {
  const {controls, calls} = closedToggle();
  controls.view.value = "attention";
  controls.repository.value = "repo-1";
  controls.project.value = "project-1";
  controls["phase-filter"].value = "finished";
  await controls["show-closed"].onclick();
  assert.equal(controls.view.value, "archived");
  assert.equal(controls["show-closed"].attributes["aria-pressed"], "true");
  assert.equal(controls["show-closed"].title, "Return to open threads");
  assert.deepEqual(calls, [["persist", "archived"], ["refresh", "archived"]]);
  await controls["show-closed"].onclick();
  assert.equal(controls.view.value, "active");
  assert.equal(controls["show-closed"].attributes["aria-pressed"], "false");
  assert.equal(controls.repository.value, "repo-1");
  assert.equal(controls.project.value, "project-1");
  assert.equal(controls["phase-filter"].value, "finished");
  assert.deepEqual(calls.slice(2), [["persist", "active"], ["refresh", "active"]]);
});

test("Closed toggle reflects history selected through filters or a saved view", () => {
  const {controls, context} = closedToggle();
  controls.view.value = "archived";
  context.renderClosedToggle();
  assert.equal(controls["show-closed"].attributes["aria-pressed"], "true");
  controls.view.value = "completed";
  context.renderClosedToggle();
  assert.equal(controls["show-closed"].attributes["aria-pressed"], "false");
  assert.equal(controls["show-closed"].title, "Show closed threads");
});

test("Now and Later controls retain view scope and reload the selected placement", async () => {
  const {controls, calls} = closedToggle();
  controls.project.value = "project-1";
  await controls["show-later"].onclick();
  assert.equal(controls.view.value, "later");
  assert.equal(controls["show-later"].attributes["aria-pressed"], "true");
  assert.equal(controls["show-now"].attributes["aria-pressed"], "false");
  await controls["show-now"].onclick();
  assert.equal(controls.view.value, "active");
  assert.equal(controls["show-now"].attributes["aria-pressed"], "true");
  assert.equal(controls.project.value, "project-1");
  assert.deepEqual(calls, [["persist", "later"], ["refresh", "later"], ["persist", "active"], ["refresh", "active"]]);
});
