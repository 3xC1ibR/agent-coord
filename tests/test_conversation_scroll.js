"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {ConversationScroll} = require("../plugins/agent-coord/scripts/agent_coord/web/conversation-scroll.js");

function setup(mobile = false) {
  const element = () => ({scrollHeight: 2000, clientHeight: 500, top: 0, children: [], events: {},
    get scrollTop() { return this.top; },
    set scrollTop(value) { this.top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); },
    addEventListener(name, callback) { this.events[name] = callback; },
    focus() { this.focused = true; },
    fire(name) { this.events[name]?.({target: this}); }});
  const content = element(), timeline = element(), button = element();
  timeline.parentElement = content;
  const view = {getComputedStyle: () => ({overflowY: mobile ? "visible" : "auto"}),
    ResizeObserver: class { constructor(callback) { this.fire = callback; } observe() {} disconnect() {} }};
  const controller = new ConversationScroll(timeline, button, view);
  return {controller, timeline, content, button, scroller: mobile ? content : timeline};
}

for (const mobile of [false, true]) {
  const layout = mobile ? "narrow" : "desktop";
  test(`${layout}: opening and reopening a long conversation starts at its latest message`, () => {
    const {controller, scroller, button} = setup(mobile);
    controller.afterRender(controller.beforeRender());
    assert.equal(controller.distance(), 0);
    assert.equal(button.hidden, true);
    scroller.scrollTop = 0; scroller.fire("scroll");
    controller.reset();
    controller.afterRender(controller.beforeRender());
    assert.equal(controller.distance(), 0);
  });
  test(`${layout}: scrolling up reveals the button, preserves reading during updates, and jumps back`, () => {
    const {controller, scroller, button} = setup(mobile);
    controller.afterRender(controller.beforeRender());
    scroller.scrollTop -= 200; scroller.fire("scroll");
    assert.equal(button.hidden, true);
    scroller.scrollTop -= 200; scroller.fire("scroll");
    assert.equal(button.hidden, false);
    const top = scroller.scrollTop, follow = controller.beforeRender();
    scroller.scrollHeight += 700;
    controller.afterRender(follow);
    controller.resize.fire();
    assert.equal(scroller.scrollTop, top);
    button.fire("click");
    assert.equal(controller.distance(), 0);
    assert.equal(button.hidden, true);
    scroller.scrollHeight += 300;
    controller.resize.fire();
    assert.equal(controller.distance(), 0);
  });
  test(`${layout}: delayed content and viewport changes follow the bottom until the reader scrolls up`, () => {
    const {controller, scroller} = setup(mobile);
    controller.afterRender(controller.beforeRender());
    scroller.scrollHeight += 800; scroller.clientHeight -= 200;
    scroller.fire("scroll"); // queued event from the initial programmatic jump
    controller.resize.fire();
    assert.equal(controller.distance(), 0);
    controller.restore(0);
    controller.resize.fire();
    assert.equal(scroller.scrollTop, 0, "saved pane position at the top must survive layout changes");
    assert.equal(controller.beforeRender(), false);
  });
  test(`${layout}: browser clamping at the bottom preserves following across repeated layout changes`, () => {
    const {controller, scroller} = setup(mobile);
    controller.afterRender(controller.beforeRender());
    for (const change of [() => scroller.clientHeight += 40, () => scroller.scrollHeight -= 60]) {
      change();
      // WebKit delivers this clamped scroll event before ResizeObserver.
      scroller.scrollTop = scroller.scrollHeight;
      scroller.fire("scroll");
      assert.equal(controller.beforeRender(), true, "layout clamping must not look like reading upward");
      controller.resize.fire();
      scroller.scrollHeight += 80;
      controller.afterRender(controller.beforeRender());
      assert.equal(controller.distance(), 0, "new output must remain visible");
    }
  });
  test(`${layout}: fractional movement at the bottom does not detach following`, () => {
    const {controller, scroller} = setup(mobile);
    controller.afterRender(controller.beforeRender());
    scroller.scrollTop -= 0.5;
    scroller.fire("scroll");
    assert.equal(controller.beforeRender(), true);
    scroller.scrollHeight += 40;
    controller.resize.fire();
    assert.equal(controller.distance(), 0);
  });
  test(`${layout}: short conversations never need a jump button`, () => {
    const {controller, scroller, button} = setup(mobile);
    scroller.scrollHeight = scroller.clientHeight;
    controller.afterRender(controller.beforeRender());
    assert.equal(button.hidden, true);
  });
}


test("reading just above the latest message remains detached until scrolling to the bottom", () => {
  const {controller, scroller} = setup();
  controller.afterRender(controller.beforeRender());
  scroller.scrollTop -= 40; scroller.fire("scroll");
  assert.equal(controller.beforeRender(), false);
  const top = scroller.scrollTop;
  scroller.scrollHeight += 100; controller.afterRender(false);
  assert.equal(scroller.scrollTop, top);
  scroller.scrollTop = scroller.scrollHeight - scroller.clientHeight - 20; scroller.fire("scroll");
  assert.equal(controller.beforeRender(), false);
  controller.restore(scroller.scrollTop);
  assert.equal(controller.beforeRender(), false, "restoring a nearby reading position must not follow");
  scroller.scrollTop = scroller.scrollHeight; scroller.fire("scroll");
  assert.equal(controller.beforeRender(), true);
});

test("a text selection at the bottom keeps its reading position during layout changes", () => {
  const {controller, scroller, timeline} = setup();
  controller.afterRender(controller.beforeRender());
  const selected = {};
  timeline.contains = node => node === selected;
  controller.view.getSelection = () => ({isCollapsed: false, anchorNode: selected, focusNode: selected});
  assert.equal(controller.beforeRender(), false);
  scroller.clientHeight += 40;
  scroller.scrollTop = scroller.scrollHeight;
  scroller.fire("scroll");
  assert.equal(controller.beforeRender(), false);
  const top = scroller.scrollTop;
  scroller.scrollHeight += 80;
  controller.afterRender(false);
  assert.equal(scroller.scrollTop, top);
});
