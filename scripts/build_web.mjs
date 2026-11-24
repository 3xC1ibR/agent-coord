// Keep classic declarations visible to the native host while compiling the
// complete workspace program and its stylesheet with esbuild.
import {build, context} from "esbuild";
import {createHash} from "node:crypto";
import {mkdir, readFile, rename, stat, writeFile} from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const [web, output, ...options] = process.argv.slice(2);
const development = options.includes("--development");
const watch = options.includes("--watch");
const manifest = path.join(web, "web-entry.js");
const template = path.join(web, "index.html");
const files = new Map();
const emitted = new Map();
let previousProgram;
let previousIndex;

const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
function vlq(value) {
  let remaining = (Math.abs(value) << 1) + Number(value < 0);
  let encoded = "";
  do {
    const digit = remaining & 31;
    remaining >>>= 5;
    encoded += alphabet[digit | (remaining ? 32 : 0)];
  } while (remaining);
  return encoded;
}

async function sourceRecord(name) {
  const filename = path.join(web, name);
  const attributes = await stat(filename, {bigint: true});
  const cached = files.get(name);
  if (cached && cached.modified === attributes.mtimeNs && cached.size === attributes.size) return cached;
  const contents = (await readFile(filename, "utf8")).trimEnd();
  const record = {filename, modified: attributes.mtimeNs, size: attributes.size, contents,
    lines: development ? contents.split("\n").length : 0};
  files.set(name, record);
  return record;
}

async function loadProgram() {
  const entry = await readFile(manifest, "utf8");
  const names = [...entry.matchAll(/^import "\.\/([\w-]+\.js)";$/gm)].map(match => match[1]);
  const records = await Promise.all(names.map(sourceRecord));
  if (previousProgram?.manifest === entry && records.every((record, i) => record === previousProgram.records[i]))
    return previousProgram.input;
  // Explicit ESM input removes Node-only test exports. Identifiers remain
  // unmangled because the native shell calls individual classic declarations.
  const parts = ['export {};\nimport "./web-entry.css";\n'];
  const mappings = [";;"];
  let lastLine = 0;
  for (const [index, record] of records.entries()) {
    parts.push(record.contents, "\n;\n");
    if (development) {
      // Each source line starts at column zero and maps to the next original
      // line. Only the first line has a non-unit source delta.
      mappings.push("A", vlq(Number(index > 0)), vlq(-lastLine), "A;", "AACA;".repeat(record.lines - 1), ";");
      lastLine = record.lines - 1;
    }
  }
  if (development) {
    const map = {version: 3, sources: names, sourcesContent: records.map(row => row.contents),
      names: [], mappings: mappings.join("")};
    parts.push("//# sourceMappingURL=data:application/json;base64,", Buffer.from(JSON.stringify(map)).toString("base64"));
  }
  const input = {contents: parts.join(""), resolveDir: web, loader: "js",
    watchFiles: [template, manifest, ...records.map(row => row.filename)]};
  previousProgram = {manifest: entry, records, input};
  return input;
}

async function replaceFile(name, contents) {
  const temporary = path.join(output, "." + name + "." + process.pid);
  await writeFile(temporary, contents);
  await rename(temporary, path.join(output, name));
}

async function publishAssets(result) {
  if (result.errors.length) return;
  await mkdir(output, {recursive: true});
  let index = await readFile(template, "utf8");
  for (const file of result.outputFiles) {
    const extension = path.extname(file.path);
    let asset = emitted.get(extension);
    if (!asset || asset.hash !== file.hash) {
      const digest = createHash("sha256").update(file.contents).digest("hex").slice(0, 20);
      asset = {name: "app-" + digest + extension, hash: file.hash};
      await replaceFile(asset.name, file.contents);
      emitted.set(extension, asset);
    }
    index = index.replaceAll("/dist/app" + extension, "/dist/" + asset.name);
  }
  if (index !== previousIndex) {
    // Readers can still finish loading the previous document during a rebuild.
    await replaceFile("index.html", index);
    previousIndex = index;
  }
}

const workspace = {
  name: "workspace",
  setup(builder) {
    let started;
    builder.onStart(() => { started = performance.now(); });
    builder.onResolve({filter: /^workspace$/}, () => ({path: "workspace", namespace: "workspace"}));
    builder.onLoad({filter: /.*/, namespace: "workspace"}, loadProgram);
    builder.onEnd(async result => {
      await publishAssets(result);
      if (process.env.RIBBON_BUILD_TIMING)
        process.stdout.write(JSON.stringify({buildMilliseconds: performance.now() - started}) + "\n");
    });
  },
};
const configuration = {absWorkingDir: web, entryPoints: {app: "workspace"}, outdir: output, bundle: true,
  platform: "browser", format: "esm", treeShaking: false, define: {module: "undefined", require: "undefined"},
  minifySyntax: !development, minifyWhitespace: !development, target: ["safari15", "chrome105"],
  sourcemap: development ? "inline" : false, write: false, plugins: [workspace], logLevel: "warning"};
if (watch) {
  const builder = await context(configuration);
  await builder.watch();
} else await build(configuration);
