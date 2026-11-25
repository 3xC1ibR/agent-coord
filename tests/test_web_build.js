"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {execFileSync} = require("node:child_process");
const {existsSync, readFileSync} = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const webRoot = path.join(root, "plugins/agent-coord/scripts/agent_coord/web");

test("dist/app.js, dist/app.css, and dist/manifest.json are built, fresh, and syntactically valid", () => {
  execFileSync("python3", [path.join(root, "scripts/build_web.py"), "--check"], {cwd: root, stdio: "pipe"});
  const manifest = JSON.parse(readFileSync(path.join(webRoot, "dist/manifest.json"), "utf8"));
  assert.equal(manifest.version, 1);
  assert.equal(manifest.css_order[0], "tokens.css");
  assert.equal(manifest.css_order.at(-1), "theme.css");
  assert.ok(manifest.js_order.includes("app.js"));
  assert.ok(existsSync(path.join(webRoot, "dist/app.js")));
  assert.ok(existsSync(path.join(webRoot, "dist/app.css")));
  const bundleJs = readFileSync(path.join(webRoot, "dist/app.js"), "utf8");
  assert.doesNotThrow(() => new vm.Script(bundleJs, {filename: "dist/app.js"}));
});
