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
  test(`${layout}: short conversations never need a jump button`, () => {
    const {controller, scroller, button} = setup(mobile);
    scroller.scrollHeight = scroller.clientHeight;
    controller.afterRender(controller.beforeRender());
    assert.equal(button.hidden, true);
  });
}
