const test = require("node:test");
const assert = require("node:assert/strict");
const preview = require("../plugins/agent-coord/scripts/agent_coord/web/html-preview.js");

test("HTML paths use the selected thread and survive spaces and line suffixes", () => {
  for (const path of ["/opt/project/report.html", "./report.htm", "report.HTML:10:2", "docs/a%20b.html"]) {
    const url = new URL(preview.target(path, "thread / one"), "https://example.test");
    assert.equal(url.pathname, "/html-preview");
    assert.equal(url.searchParams.get("thread"), "thread / one");
    assert.ok(url.searchParams.get("path").endsWith(".html") || /\.html?$/i.test(url.searchParams.get("path")));
  }
  assert.equal(new URL(preview.target("docs/a%20b.html", "t"), "https://example.test").searchParams.get("path"), "docs/a b.html");
});

test("ordinary navigation and unsupported paths keep their existing behavior", () => {
  for (const path of ["https://example.test/report.html", "file:///report.html", "//example.test/report.html", "javascript:evil.html", "#part.html", "?page.html", "/api/report.html", "report.md", "bad%zz.html", "bad%00.html"]) {
    assert.equal(preview.target(path, "thread"), null, path);
  }
  assert.equal(preview.target("report.html", null), null);
});

test("artifact query and fragment deep links survive the viewer", async () => {
  const url = new URL(preview.target("drawing.html?trace=A&step=3#sheet-id", "t"), "https://example.test");
  assert.equal(url.searchParams.get("path"), "drawing.html");
  assert.equal(url.searchParams.get("search"), "?trace=A&step=3");
  assert.equal(url.hash, "#sheet-id");
  const f = fixture({ok: true, status: 200, json: async () => ({name: "drawing.html"})});
  f.win.location.search = url.search;
  f.win.location.hash = url.hash;
  await preview.show(f.doc, f.win);
  assert.equal(f.nodes.get("preview-frame").src, "/api/browser/html-preview-content/t/drawing.html?trace=A&step=3#sheet-id");
});

test("a phone tap stays in the app; a native-handled click is untouched", () => {
  let listener, destination, prevented = false;
  const doc = {getElementById: () => null, addEventListener: (_, value) => listener = value, defaultView: {location: {assign: value => destination = value}}};
  preview.bind(doc, () => "thread");
  const link = {getAttribute: () => "report.html"};
  const event = {button: 0, target: {closest: () => link}, preventDefault: () => prevented = true};
  listener({...event, defaultPrevented: true});
  assert.equal(destination, undefined);
  listener(event);
  assert.equal(prevented, true);
  assert.equal(destination, "/html-preview?thread=thread&path=report.html");
});

test("embedded preview preserves the conversation and accepts close only from its own viewer", () => {
  let click, message, onClose, opens = 0, closes = 0;
  const frame = {contentWindow: {}, removeAttribute: key => { delete frame[key]; }};
  const dialog = {showModal: () => opens++, close: () => { closes++; onClose(); }, addEventListener: (_, fn) => onClose = fn};
  const doc = {getElementById: id => id === "html-preview-dialog" ? dialog : frame,
    addEventListener: (_, fn) => click = fn,
    defaultView: {location: {origin: "https://phone.example"}, addEventListener: (_, fn) => message = fn}};
  preview.bind(doc, () => "thread");
  const link = {dataset: {}, href: "report.html#result", getAttribute() { return this.href; }};
  const event = {button: 0, target: {closest: () => link}, preventDefault() {}};
  click(event);
  assert.equal(opens, 1);
  assert.equal(frame.src, "/html-preview?thread=thread&path=report.html&embedded=1#result");
  message({origin: "null", source: frame.contentWindow, data: {type: "ribbon-field-close-html-preview"}});
  message({origin: "https://phone.example", source: {}, data: {type: "ribbon-field-close-html-preview"}});
  assert.equal(closes, 0);
  message({origin: "https://phone.example", source: frame.contentWindow, data: {type: "ribbon-field-close-html-preview"}});
  assert.equal(closes, 1);
  assert.equal(frame.src, undefined);
  click(event);
  assert.equal(opens, 2);
  assert.equal(frame.src, "/html-preview?thread=thread&path=report.html&embedded=1#result");
});

function fixture(response) {
  const nodes = new Map();
  for (const id of ["preview-status", "preview-frame", "preview-title", "preview-retry", "preview-back"]) {
    nodes.set(id, {hidden: false, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; }});
  }
  const doc = {getElementById: id => nodes.get(id)};
  let requested, destination;
  const win = {location: {search: "?thread=t&path=report.html", assign: value => destination = value}, fetch: async value => { requested = value; return response; }};
  return {doc, win, nodes, requested: () => requested, destination: () => destination};
}

test("viewer uses an HTTP sandbox frame and has a working return action", async () => {
  const f = fixture({ok: true, status: 200, json: async () => ({name: "report.html"})});
  await preview.show(f.doc, f.win);
  const frame = f.nodes.get("preview-frame");
  assert.equal(frame.hidden, false);
  assert.equal(frame.attrs.sandbox, "allow-scripts");
  assert.equal(frame.src, "/api/browser/html-preview-content/t/report.html");
  assert.equal(f.nodes.get("preview-status").hidden, true);
  f.nodes.get("preview-back").onclick();
  assert.equal(f.destination(), "/#t");
});

test("missing files, expired pairing and an older runtime show useful errors", async () => {
  for (const [status, expected] of [[400, "missing"], [401, "Pair this device"], [404, "updated Ribbon Field backend"]]) {
    const f = fixture({ok: false, status, json: async () => ({error: "File missing"})});
    await preview.show(f.doc, f.win);
    assert.ok(f.nodes.get("preview-status").textContent.includes(expected));
    assert.equal(f.nodes.get("preview-frame").hidden, true);
    assert.equal(f.nodes.get("preview-retry").hidden, false);
  }
});
