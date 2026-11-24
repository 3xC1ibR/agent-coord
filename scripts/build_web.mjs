// Compile the classic browser modules independently, then publish one ordered
// program. In watch mode an edit recompiles only entries that use that file.
import {build} from "esbuild";
import {createHash} from "node:crypto";
import {watch} from "node:fs";
import {mkdir, readFile, rename, writeFile} from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const [web, output, ...options] = process.argv.slice(2);
const development = options.includes("--development");
const manifest = "web-entry.js";
const stylesheet = "web-entry.css";
const template = "index.html";
const inputs = new Map();
const published = new Map();
let order = [];
let previousIndex;

async function compile(name) {
  const javascript = name.endsWith(".js");
  // ESM input eliminates Node-only test exports while preserving classic global
  // declarations. Identifiers are shared across entries and must keep their names.
  const entry = javascript ? {stdin: {
    contents: await readFile(path.join(web, name), "utf8") + "\nexport {};\n",
    sourcefile: name, resolveDir: web, loader: "js",
  }} : {entryPoints: [name]};
  const result = await build({...entry, absWorkingDir: web, outfile: path.join(web, javascript ? "workspace.js" : "workspace.css"),
    bundle: true, write: false, platform: "browser", format: "esm", treeShaking: false, metafile: true,
    define: {module: "undefined", require: "undefined"}, target: ["safari15", "chrome105"],
    minifySyntax: !development, minifyWhitespace: !development, sourcemap: development ? "external" : false,
    logLevel: "warning"});
  const code = result.outputFiles.find(file => !file.path.endsWith(".map")).text;
  const map = development ? result.outputFiles.find(file => file.path.endsWith(".map")).text : null;
  let lines = 0;
  if (development && javascript) for (let i = code.indexOf("\n"); i !== -1; i = code.indexOf("\n", i + 1)) lines++;
  return {code, map, lines, dependencies: new Set(Object.keys(result.metafile.inputs))};
}

function program() {
  // esbuild omits each module's implicit strict directive in ESM output.
  // The published program executes as a classic script on retained clients.
  const parts = ['"use strict";\n'], sections = [];
  let line = 1;
  for (const name of order) {
    const input = inputs.get(name);
    parts.push(input.code, ";\n");
    if (development) {
      sections.push('{"offset":{"line":' + line + ',"column":0},"map":' + input.map + "}");
      line += input.lines + 1;
    }
  }
  if (development) {
    const map = '{"version":3,"sections":[' + sections.join(",") + "]}";
    parts.push("//# sourceMappingURL=data:application/json;base64,", Buffer.from(map).toString("base64"), "\n");
  }
  return parts.join("");
}

async function replaceFile(name, contents) {
  const temporary = path.join(output, "." + name + "." + process.pid);
  await writeFile(temporary, contents);
  await rename(temporary, path.join(output, name));
}

async function publish(extension, contents) {
  const digest = createHash("sha256").update(contents).digest("hex").slice(0, 20);
  const asset = "app-" + digest + "." + extension;
  if (published.get(extension) !== asset) {
    await replaceFile(asset, contents);
    published.set(extension, asset);
  }
}

async function rebuild(changed) {
  const started = performance.now();
  const readManifest = !order.length || changed.has(manifest);
  if (readManifest) {
    const declarations = await readFile(path.join(web, manifest), "utf8");
    const imports = [...declarations.matchAll(/^import "\.\/([\w-]+\.js)";$/gm)].map(match => match[1]);
    if (!imports.length || new Set(imports).size !== imports.length) throw Error("Invalid workspace script manifest.");
    order = imports;
  }
  const entries = [...order, stylesheet];
  const affected = entries.filter(name => !inputs.has(name) || changed.has(name) ||
    [...changed].some(file => inputs.get(name).dependencies.has(file)));
  const compiled = await Promise.all(affected.map(async name => [name, await compile(name)]));
  for (const [name, result] of compiled) inputs.set(name, result);
  for (const name of inputs.keys()) if (!entries.includes(name)) inputs.delete(name);
  const assets = [];
  if (readManifest || affected.some(name => name !== stylesheet)) assets.push(publish("js", program()));
  if (affected.includes(stylesheet)) {
    const css = inputs.get(stylesheet);
    const sourceMap = development ? "\n/*# sourceMappingURL=data:application/json;base64," +
      Buffer.from(css.map).toString("base64") + " */\n" : "";
    assets.push(publish("css", css.code + sourceMap));
  }
  await Promise.all(assets);
  if (assets.length || changed.has(template)) {
    const index = (await readFile(path.join(web, template), "utf8"))
      .replaceAll("/dist/app.js", "/dist/" + published.get("js")).replaceAll("/dist/app.css", "/dist/" + published.get("css"));
    if (index !== previousIndex) { await replaceFile("index.html", index); previousIndex = index; }
  }
  if (process.env.RIBBON_BUILD_TIMING) process.stdout.write(JSON.stringify({buildMilliseconds: performance.now() - started}) + "\n");
}

await mkdir(output, {recursive: true});
if (!options.includes("--watch")) await rebuild(new Set());
else {
  let running, timer, closed = false;
  const changes = new Set([manifest, template]);
  const failedInputs = new Set();
  const outputDirectory = path.relative(web, output);
  const drain = async () => {
    while (changes.size && !closed) {
      const batch = new Set([...failedInputs, ...changes]);
      changes.clear();
      try { await rebuild(batch); failedInputs.clear(); }
      catch (error) {
        for (const file of batch) failedInputs.add(file);
        process.stderr.write(String(error) + "\n");
      }
    }
  };
  const schedule = () => {
    if (!running && !closed) running = drain().finally(() => {
      running = null;
      if (changes.size && !closed) schedule();
    });
  };
  // The inputs and their imported style sheets share this source directory;
  // packaged output lives under dist and never invalidates the compiler.
  const watcher = watch(web, {recursive: true}, (_event, filename) => {
    if (!filename || filename.startsWith(outputDirectory + path.sep) || filename.startsWith(".")) return;
    if (filename !== template && filename !== manifest && !/\.(?:js|css)$/.test(filename)) return;
    changes.add(filename);
    clearTimeout(timer); timer = setTimeout(schedule, 4);
  });
  schedule();
  await new Promise(resolve => { process.once("SIGINT", resolve); process.once("SIGTERM", resolve); });
  closed = true; watcher.close(); clearTimeout(timer); await running;
}
