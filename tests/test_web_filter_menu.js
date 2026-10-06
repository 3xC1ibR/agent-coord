"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const createFilterMenu = require("../plugins/agent-coord/scripts/agent_coord/web/filter-menu.js");

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
