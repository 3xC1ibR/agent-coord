"use strict";
/* A minimal DOM for unit-testing view scripts under node: enough structure to build, find and trigger controls. */
class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.listeners = {};
    this.value = ""; this.hidden = false; this.disabled = false; this.textContent = ""; this.className = "";
    const names = new Set();
    this.classList = {toggle: (name, on) => { on ? names.add(name) : names.delete(name); }, contains: name => names.has(name)};
  }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; if (key === "src") delete this.src; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatch(type, event = {}) { for (const listener of this.listeners[type] || []) listener(event); }
  showModal() { this.open = true; }
  close() { this.open = false; this.dispatch("close"); }
  focus() { this.focused = true; }
  getClientRects() { return [1]; }
  contains(other) { return this.all().includes(other); }
  /* Every descendant, depth first. */
  all() { return this.children.flatMap(child => [child, ...child.all()]); }
  byClass(name) { return this.all().filter(node => node.className.split(" ").includes(name)); }
  byTag(tag) { return this.all().filter(node => node.tagName === tag); }
  /* Rendered text of this subtree, for assertions on what the user reads. */
  get text() { return [this.textContent, ...this.children.filter(child => !child.hidden).map(child => child.text)].filter(Boolean).join(" "); }
}
function makeDocument(ids = []) {
  const elements = new Map(ids.map(id => [id, Object.assign(new Element("div"), {id})]));
  return {body: new Element("body"), hidden: false, title: "", activeElement: null, listeners: {},
    createElement: tag => new Element(tag), getElementById: id => elements.get(id) || null,
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }, elements};
}
module.exports = {Element, makeDocument};
