// Exercise the shipped builder with classic globals, a CSS import and an active
// watch. An invalid edit must keep the previous matched document and resources.
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {createHash} from "node:crypto";
import {once} from "node:events";
import {mkdtemp, mkdir, readFile, rm, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
import {createInterface} from "node:readline";
import {test} from "node:test";
import {fileURLToPath} from "node:url";
import vm from "node:vm";

const root = fileURLToPath(new URL("../", import.meta.url));

test("the workspace watch publishes matching assets and indexed development maps after valid edits", {timeout: 15000}, async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "ribbon-workspace-build-"));
  const web = path.join(directory, "source"), output = path.join(directory, "output");
  const write = (name, source) => writeFile(path.join(web, name), source);
  let child, lines;
  async function published() {
    const index = await readFile(path.join(output, "index.html"), "utf8");
    const assets = {};
    for (const [, digest, extension] of index.matchAll(/\/dist\/app-([0-9a-f]{20})\.(css|js)/g)) {
      const code = await readFile(path.join(output, "app-" + digest + "." + extension), "utf8");
      assert.equal(createHash("sha256").update(code).digest("hex").slice(0, 20), digest);
      const sourceMap = /sourceMappingURL=data:application\/json;base64,([A-Za-z0-9+/=]+)/.exec(code);
      assets[extension] = {code, digest, map: JSON.parse(Buffer.from(sourceMap[1], "base64"))};
    }
    return assets;
  }
  try {
    await mkdir(web);
    await Promise.all([
      write("index.html", '<link href="/dist/app.css"><script src="/dist/app.js"></script>'),
      write("web-entry.js", 'import "./definitions.js";\nimport "./main.js";\n'),
      write("definitions.js", 'const month = "November";'),
      write("main.js", 'globalThis.displayed = month;'),
      write("web-entry.css", '@import "./palette.css";'),
      write("palette.css", 'body { color: red }'),
    ]);
    child = spawn("python3", [path.join(root, "scripts/build_web.py"), "--development", "--watch",
      "--web-root", web, "--output", output], {env: {...process.env, RIBBON_BUILD_TIMING: "1"}, stdio: ["ignore", "pipe", "pipe"]});
    lines = createInterface({input: child.stdout});
    const generated = () => once(lines, "line");
    await generated();
    const first = await published(), context = {};
    vm.runInNewContext(first.js.code, context);
    assert.equal(context.displayed, "November");
    assert.deepEqual(first.js.map.sections.map(section => section.map.sources[0]), ["definitions.js", "main.js"]);
    for (const section of first.js.map.sections) {
      const line = first.js.code.split("\n")[section.offset.line];
      assert.match(line, /definitions\.js|main\.js/);
    }
    assert.ok(first.css.map.sources.some(source => source.endsWith("palette.css")));
    const error = once(child.stderr, "data");
    await write("definitions.js", "const month = ;");
    await error;
    assert.equal((await published()).js.digest, first.js.digest);
    const updatedJS = generated();
    await write("definitions.js", 'const month = "December";');
    await updatedJS;
    const second = await published(), secondContext = {};
    vm.runInNewContext(second.js.code, secondContext);
    assert.equal(secondContext.displayed, "December");
    assert.notEqual(second.js.digest, first.js.digest);
    assert.equal(second.css.digest, first.css.digest);
    const updatedCSS = generated();
    await write("palette.css", "body { color: blue }");
    await updatedCSS;
    const third = await published();
    assert.equal(third.js.digest, second.js.digest);
    assert.notEqual(third.css.digest, second.css.digest);
    assert.match(third.css.code, /blue/);
  } finally {
    lines?.close();
    if (child && child.exitCode === null && child.signalCode === null) {
      const exit = once(child, "exit"); child.kill("SIGTERM"); await exit;
    }
    await rm(directory, {recursive: true, force: true});
  }
});
