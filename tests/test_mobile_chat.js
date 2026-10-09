"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {MobileChat} = require("../plugins/agent-coord/scripts/agent_coord/web/mobile-chat.js");

class Element {
  constructor(name) {
    this.name = name; this.children = []; this.events = {}; this.dataset = {}; this.attributes = {};
    this.hidden = false; this.open = false;
    const classes = new Set();
    this.classList = {add: value => classes.add(value), contains: value => classes.has(value),
      toggle: (value, enabled) => enabled ? classes.add(value) : classes.delete(value)};
    const properties = new Map();
    this.style = {setProperty: (key, value) => properties.set(key, value),
      removeProperty: key => properties.delete(key), getPropertyValue: key => properties.get(key)};
  }
  append(element) {
    element.parentElement?.children.splice(element.parentElement.children.indexOf(element), 1);
    this.children.push(element); element.parentElement = this;
  }
  before(marker) {
    const parent = this.parentElement;
    parent.children.splice(parent.children.indexOf(this), 0, marker); marker.parentElement = parent;
  }
  after(element) {
    element.parentElement?.children.splice(element.parentElement.children.indexOf(element), 1);
    this.parentElement.children.splice(this.parentElement.children.indexOf(this) + 1, 0, element);
    element.parentElement = this.parentElement;
  }
  contains(element) { return this === element || this.children.some(child => child.contains(element)); }
  closest(selector) { return selector === "button" && this.name.startsWith("button") ? this : null; }
  addEventListener(type, callback, options = {}) {
    (this.events[type] ||= []).push({callback, capture: options.capture});
  }
  fire(type, event = {}) { for (const {callback} of this.events[type] || []) callback({target:this, ...event}); }
  setAttribute(name, value) { this.attributes[name] = value; }
  showModal() { this.open = true; }
  close() { this.open = false; this.fire("close"); }
  focus() { this.focused = true; }
}

function setup(mobile = true) {
  const ids = ["mobile-thread-sheet", "mobile-chat-actions", "context-panel", "mobile-chat-back", "mobile-show-context",
    "mobile-sheet-back", "mobile-sheet-actions", "mobile-sheet-context", "mobile-sheet-info", "mobile-sheet-title",
    "thread-context", "conversation", "mobile-chat-status"];
  const elements = Object.fromEntries(ids.map(id => [id,new Element(id)]));
  const body = new Element("body"), header = new Element("header"), actions = new Element("actions"), info = new Element("info");
  body.append(header); header.append(actions); body.append(elements["context-panel"]); body.append(info);
  elements["context-panel"].append(elements["thread-context"]);
  for (const id of ["mobile-sheet-actions","mobile-sheet-context","mobile-show-context","mobile-sheet-info"]) elements["mobile-thread-sheet"].append(elements[id]);
  const doc = {body, getElementById: id => elements[id], createComment: () => new Element("marker"),
    querySelector: selector => selector === ".session-actions" ? actions : info};
  const media = {matches:mobile, addEventListener: (_, callback) => { media.change = callback; }};
  const viewport = {height:800, offsetTop:0, scale:1, events:{}, addEventListener: (type, callback) => { viewport.events[type]=callback; }};
  let back = 0, layouts = 0, desktopContext = 0, frame;
  const view = {matchMedia: () => media, visualViewport:viewport,
    requestAnimationFrame: callback => {frame=callback; return 1;}, cancelAnimationFrame: () => {frame=null;}};
  const chat = new MobileChat({document:doc, view, onBack: () => {back++;}, onLayout: () => {layouts++;}, onContextLayout: () => {desktopContext++;}});
  return {chat, elements, body, header, actions, info, media, viewport,
    counts: () => ({back,layouts,desktopContext}), flush: () => {const callback=frame; frame=null; callback?.();}};
}

test("mobile sheets reuse original controls and restore their desktop positions and handlers", () => {
  const s=setup(), original=s.actions;
  original.onclick=() => "original handler";
  assert.equal(s.actions.parentElement,s.elements["mobile-sheet-actions"]);
  assert.equal(s.info.parentElement,s.elements["mobile-sheet-info"]);
  assert.equal(s.elements["context-panel"].hidden,true);
  s.chat.open("actions");
  s.media.matches=false; s.media.change();
  assert.equal(s.elements["mobile-thread-sheet"].open,false);
  assert.equal(s.actions.parentElement,s.header);
  assert.equal(s.elements["context-panel"].parentElement,s.body);
  assert.equal(s.info.parentElement,s.body);
  assert.equal(s.actions.onclick(),"original handler");
  assert.equal(s.counts().desktopContext,1);
  s.media.matches=true; s.media.change();
  assert.equal(s.actions,original);
  assert.equal(s.actions.parentElement,s.elements["mobile-sheet-actions"]);
});

test("thread context appears only on demand and closes without changing desktop preferences", () => {
  const {chat,elements,counts}=setup();
  elements["mobile-chat-actions"].onclick();
  assert.equal(elements["mobile-thread-sheet"].open,true);
  assert.equal(elements["context-panel"].hidden,true);
  assert.equal(elements["mobile-chat-actions"].attributes["aria-expanded"],"true");
  elements["mobile-show-context"].onclick();
  assert.equal(elements["context-panel"].hidden,false);
  assert.equal(elements["thread-context"].open,true);
  assert.equal(elements["mobile-sheet-title"].textContent,"Thread context");
  assert.equal(elements["mobile-sheet-actions"].hidden,true);
  assert.equal(elements["mobile-sheet-back"].focused,true);
  elements["mobile-sheet-back"].onclick();
  assert.equal(elements["context-panel"].hidden,true);
  assert.equal(elements["mobile-sheet-actions"].hidden,false);
  chat.close();
  assert.equal(elements["mobile-chat-actions"].attributes["aria-expanded"],"false");
  assert.equal(counts().desktopContext,0);
});

test("an action closes the sheet before opening its own dialog; disabled actions do nothing", () => {
  const {chat,elements,actions}=setup();
  const button=new Element("button-snooze"); actions.append(button);
  chat.open("actions"); button.disabled=true;
  elements["mobile-thread-sheet"].fire("click",{target:button});
  assert.equal(elements["mobile-thread-sheet"].open,true);
  button.disabled=false;
  elements["mobile-thread-sheet"].fire("click",{target:button});
  assert.equal(elements["mobile-thread-sheet"].open,false);
});

test("keyboard viewport changes resize the frame, preserve pinch zoom, and clear on desktop", () => {
  const s=setup();
  assert.equal(s.body.style.getPropertyValue("--mobile-viewport-height"),"800px");
  s.viewport.height=420; s.viewport.offsetTop=15; s.viewport.events.resize(); s.flush();
  assert.equal(s.body.style.getPropertyValue("--mobile-viewport-height"),"420px");
  assert.equal(s.body.style.getPropertyValue("--mobile-viewport-top"),"15px");
  assert.equal(s.counts().layouts,1);
  s.viewport.scale=2; s.viewport.height=200; s.viewport.events.resize();
  assert.equal(s.body.style.getPropertyValue("--mobile-viewport-height"),"420px");
  s.media.matches=false; s.media.change();
  assert.equal(s.body.style.getPropertyValue("--mobile-viewport-height"),undefined);
  assert.equal(s.body.style.getPropertyValue("--mobile-viewport-top"),undefined);
});

test("header back navigation and accessible live status remain available", () => {
  const s=setup(); s.elements["mobile-chat-back"].onclick();
  assert.equal(s.counts().back,1);
  s.chat.status("Needs input","input");
  assert.equal(s.elements["mobile-chat-status"].attributes["aria-label"],"Needs input");
  assert.equal(s.elements["mobile-chat-status"].dataset.status,"input");
});
