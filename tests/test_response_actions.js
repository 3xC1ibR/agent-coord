"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {ResponseActions} = require("../plugins/agent-coord/scripts/agent_coord/web/response-actions.js");

function setup() {
  const actions = Object.create(ResponseActions.prototype), events = [], errors = [];
  Object.assign(actions, {thread: "one", selection: {thread: "one", text: "First line\n  indented\n\nCopy is valid prose"},
    input: {value: "Existing draft", maxLength: -1, dispatchEvent: event => events.push(event.type),
      focus() {}, setSelectionRange(start, end) { this.caret = [start, end]; }},
    view: {Event, getSelection: () => ({removeAllRanges() {}}), requestAnimationFrame: fn => fn()},
    status: {}, button: {}, canDiscuss: () => true, onError: error => errors.push(error.message), update() {},
  });
  return {actions, events, errors};
}

test("Discuss appends a multiline quote without altering the draft or submitting", () => {
  for (const draft of ["", "Existing draft", "Ends in newline\n", "Two newlines\n\n"]) {
    const {actions, events} = setup(); actions.input.value = draft;
    actions.discuss();
    const separator = !draft || draft.endsWith("\n\n") ? "" : draft.endsWith("\n") ? "\n" : "\n\n";
    assert.equal(actions.input.value, draft + separator + "> First line\n>   indented\n> \n> Copy is valid prose\n\n");
    assert.deepEqual(events, ["input"]);
    assert.deepEqual(actions.input.caret, [actions.input.value.length, actions.input.value.length]);
  }
});

test("unavailable composers and stale selections never change the draft", () => {
  for (const mode of ["unavailable", "stale"]) {
    const {actions, events} = setup();
    if (mode === "unavailable") actions.canDiscuss = () => false;
    else actions.selection.thread = "previous";
    actions.discuss();
    assert.equal(actions.input.value, "Existing draft"); assert.deepEqual(events, []);
  }
});

test("an oversized selection preserves the draft and reports the problem", () => {
  const {actions, errors, events} = setup(); actions.input.maxLength = 20;
  actions.discuss(); assert.equal(actions.input.value, "Existing draft");
  assert.match(errors[0], /too long/); assert.deepEqual(events, []);
});

test("streaming paints coalesce during a selection and flush when it clears", () => {
  const {actions} = setup(); let selecting = true, holds = 0, renders = 0;
  actions.selecting = () => selecting; actions.selected = () => null; actions.onHold = () => holds++;
  assert.equal(actions.deferRender("one", () => renders += 100), true);
  assert.equal(actions.deferRender("one", () => renders++), true);
  assert.equal(renders, 0); assert.equal(holds, 2);
  selecting = false; ResponseActions.prototype.update.call(actions);
  assert.equal(renders, 1); assert.equal(actions.pending, null);
});

test("switching threads drops a queued paint and stale selection state", () => {
  const {actions} = setup(); actions.selecting = () => true;
  actions.deferRender("one", () => assert.fail("Old paint"));
  assert.equal(actions.deferRender("two", () => {}), false);
  assert.equal(actions.pending, null); assert.equal(actions.selection, null); assert.equal(actions.button.hidden, true);
});

test("Copy preserves code whitespace and reports clipboard rejection without false success", async () => {
  const {actions, errors} = setup(); const code = {textContent: "  <tag>\n\nline\t& end\n"};
  const button = {dataset: {}, closest: () => ({querySelector: () => code})}; let copied;
  actions.view.navigator = {clipboard: {writeText: async text => {copied = text;}}};
  actions.view.setTimeout = () => {};
  await actions.copy(button); assert.equal(copied, code.textContent); assert.equal(button.dataset.copyLabel, "Copied");
  actions.view.navigator.clipboard.writeText = async () => {throw Error("Denied");};
  actions.copyFallback = () => {throw Error("Unavailable");};
  await actions.copy(button); assert.equal(button.dataset.copyLabel, "Try again");
  assert.equal(button.disabled, false); assert.match(errors[0], /Couldn’t copy/);
});

test("the render buffer keeps live nodes attached and commit leaves unchanged nodes in place", () => {
  const first = {}, second = {}, desired = ResponseActions.buffer();
  const container = {childNodes: [first, second], firstChild: first, insertBefore() {assert.fail("Unchanged nodes must not move");}};
  first.nextSibling = second; second.nextSibling = null;
  first.parentNode = second.parentNode = container;
  desired.append(first, second); assert.equal(first.parentNode, container);
  ResponseActions.commit(container, desired);
});
