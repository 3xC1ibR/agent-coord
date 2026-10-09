"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {render} = require("../plugins/agent-coord/scripts/agent_coord/web/markdown.js");
const fs = require("node:fs");
const vm = require("node:vm");

test("agent prose formats headings, emphasis, paragraphs, and code", () => {
  const html = render("## Result\n\n**Done** with *care*, ~~old~~ and `a < b`.\nNext line.");
  assert.match(html, /<h2>Result<\/h2>/);
  assert.match(html, /<strong>Done<\/strong>/);
  assert.match(html, /<em>care<\/em>/);
  assert.match(html, /<del>old<\/del>/);
  assert.match(html, /<code>a &lt; b<\/code>/);
  assert.match(html, /<br>\nNext line/);
  assert.equal(render("some_file_name and \\*literal\\*"), "<p>some_file_name and *literal*</p>");
});

test("fenced code is literal and unfinished streamed fences remain readable", () => {
  const text = '```html\n<script>alert("x")</script>\n**literal**';
  const expected = '<pre><code>&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;\n**literal**</code></pre>';
  assert.equal(render(text), expected);
  assert.equal(render(text + "\n```"), expected);
  assert.equal(render("~~~~\n```\n~~~~"), "<pre><code>```</code></pre>");
});

test("lists retain ordering, nested items, and task state", () => {
  const html = render("3. First\n   - Nested\n   - More\n4. Second\n\n- [x] Checked\n- [ ] Pending");
  assert.match(html, /<ol start="3"><li><p>First<\/p><ul>/);
  assert.match(html, /<\/ul><\/li><li><p>Second<\/p><\/li><\/ol>/);
  assert.match(html, /disabled aria-label="Task" checked/);
  assert.match(html, /disabled aria-label="Task"> <p>Pending/);
});

test("quotes and tables use semantic elements and escape their content", () => {
  const html = render("> **Quote**\n\n| Name | Value |\n| --- | ---: |\n| `<x>` | **2** |\n\n---");
  assert.match(html, /<blockquote><p><strong>Quote<\/strong><\/p><\/blockquote>/);
  assert.match(html, /<thead><tr><th style="text-align:left">Name/);
  assert.match(html, /<td style="text-align:right"><strong>2<\/strong>/);
  assert.match(html, /<code>&lt;x&gt;<\/code>/);
  assert.ok(html.endsWith("<hr>"));
});

test("links allow web and relative targets without enabling executable schemes", () => {
  const html = render('[Docs](https://example.com/a_(b)?x=1&y=2) [File](/src/app.py:12)');
  assert.match(html, /href="https:\/\/example.com\/a_\(b\)\?x=1&amp;y=2"/);
  assert.match(html, /href="\/src\/app.py:12"/);
  assert.match(html, /rel="noopener noreferrer"/);
  for (const url of ["javascript:alert", "JaVaScRiPt:alert", "data:text/html,evil", "vbscript:evil", "//evil.test", "\\evil.test", "java&#x73;cript:evil"]) {
    const result = render("[click](" + url + ")");
    assert.doesNotMatch(result, /href="(?:javascript:|data:|vbscript:|\/\/|\\)/i);
    assert.doesNotMatch(result, /href="java&#x73;/);
  }
});

test("HTML, entities, image syntax, and attribute injection cannot execute", () => {
  const html = render('<img src=x onerror=alert(1)> <script>bad()</script> &lt;svg&gt;\n\n[x](https://example.com/"onclick="bad)');
  assert.doesNotMatch(html, /<(?:img|script|svg)\b/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&amp;lt;svg&amp;gt;/);
  assert.match(html, /&quot;onclick=&quot;/);
  assert.doesNotMatch(render("![alt](https://example.com/image.png)"), /<img/);
});

test("empty and incomplete streamed syntax is stable and deeply nested content is bounded", () => {
  assert.equal(render(null), "");
  assert.equal(render("**working"), "<p>**working</p>");
  assert.equal(render("[link](https://"), "<p>[link](https://</p>");
  assert.ok(render("> ".repeat(100) + "text").includes("text"));
});

test("conversation renders and refreshes agent Markdown while user text stays literal", () => {
  const source = fs.readFileSync(require.resolve("../plugins/agent-coord/scripts/agent_coord/web/app.js"), "utf8");
  const element = (tag, text, className) => ({tag, textContent: text, className, dataset: {}, childNodes: [],
    append(...children) { this.childNodes.push(...children); },
    replaceChildren(fragment) { this.childNodes = fragment.childNodes; },
    setAttribute(name, value) { this[name] = value; },
    querySelectorAll() { return []; }, scrollHeight: 100, scrollTop: 0, clientHeight: 100});
  const timeline = element("div"), user = {type: "userMessage", content: [{text: "**literal**"}]};
  const agent = {type: "agentMessage", text: "**Result**\n\n```js\nconst x = 1;"};
  const context = {state: {selected: "test-thread", detail: {thread: {turns: [{items: [user, agent]}]}}},
    $: () => timeline, node: element, document: {createDocumentFragment: () => element("fragment")}, messageMarkdown: {render}};
  const disclosureEntry = {};
  context.conversationEntry = () => disclosureEntry;
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function itemText("), source.indexOf("function requestButton(")), context);
  vm.runInContext("renderTimeline()", context);
  assert.equal(timeline.childNodes[0].childNodes[0].textContent, "**literal**");
  assert.equal(timeline.childNodes[0].childNodes[0].innerHTML, undefined);
  assert.match(timeline.childNodes[1].childNodes[0].innerHTML, /<strong>Result<\/strong>/);
  agent.text += "\n```\n\nFinished.";
  vm.runInContext("renderTimeline()", context);
  assert.match(timeline.childNodes[1].childNodes[0].innerHTML, /<\/pre><p>Finished\.<\/p>/);
  assert.equal(timeline.scrollTop, 100);
});
