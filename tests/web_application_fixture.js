"use strict";

const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const vm = require("node:vm");

const web = join(__dirname, "../plugins/agent-coord/scripts/agent_coord/web");
const application = readFileSync(join(web, "app.js"), "utf8");

function range(source, first, last) {
  const start = source.indexOf(first);
  const end = source.indexOf(last, start + first.length);
  assert(start >= 0 && end > start, `Expected source range ${first} through ${last}.`);
  return source.slice(start, end);
}

function loadWebModule(context, name) {
  const filename = join(web, name);
  vm.runInContext(readFileSync(filename, "utf8"), context, {filename});
}

function loadTimelineReplacement(context) {
  if (!context.replaceTimelineContent) {
    vm.runInContext(range(application, "function replaceTimelineContent(", "function retainConversation("), context);
  }
}

function loadComposer(context) {
  loadWebModule(context, "outgoing-messages.js");
  context.crypto ??= {randomUUID};
  context.clearTimeout ??= clearTimeout;
  context.document ??= require("./web_dom_stub.js").makeDocument();
  if (!context.showError) {
    context.fixtureErrors = [];
    context.showError = error => context.fixtureErrors.push(error);
  }
  if (!context.conversationEntry) {
    vm.runInContext(range(application, "function conversationEntry(", "function retainConversation("), context);
  }
  loadTimelineReplacement(context);
  context.state.submissionQueue = vm.runInContext("new ConversationSubmissionQueue()", context);
  if (!context.state.outgoing) {
    const outgoing = range(application, "class OutgoingMessages {", "class UpdateSubmissionChanges");
    context.state.outgoing = vm.runInContext(
      `(() => {${outgoing}\nreturn new OutgoingMessages({itemText: item => item.text || ""});})()`, context);
  }
  if (!context.state.attachments) {
    const ChatImageAttachments = require(join(web, "image-attachments.js"));
    context.state.attachments = new ChatImageAttachments({getThread: () => context.state.selected, onChange() {}});
    context.state.attachments.render = () => {}; // These fixtures provide no attachment region.
    context.state.attachments.highlight = () => {};
  }
  context.state.attachments.drafts ??= new Map();
}

function threadListFixture(callbacks = {}) {
  const {Element} = require("./web_dom_stub.js");
  class Node extends Element {
    constructor(tag) {
      super(tag);
      this.style = {setProperty() {}};
      const original = this.classList;
      this.classList = {...original, add: name => original.toggle(name, true)};
    }
    append(...children) {
      super.append(...children.map(child => typeof child === "string"
        ? Object.assign(new Node("#text"), {textContent: child}) : child));
    }
  }
  const document = {activeElement: null, createElement: tag => new Node(tag)};
  const WorkingSet = require(join(web, "working-set.js"));
  const view = WorkingSet.createList({document, ...callbacks});
  const target = new Node("section");
  return {
    render(threads, options) { view.render(threads, target, options); },
    section(name) { return target.children.find(node => node.dataset.section === name); },
    rows(container = target) { return (container?.byClass("tl-row") || []).map(row => row.dataset.thread); },
    control(id, action) { return target.byTag("button").find(button => button.dataset.thread === id && button.dataset.action === action); },
    target,
  };
}

module.exports = {loadComposer, loadTimelineReplacement, loadWebModule, range, threadListFixture};
