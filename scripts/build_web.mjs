// Build one classic browser program, preserving the globals used by WKWebView.
// Source positions are mapped back to the individual input scripts in development.
import {build, context} from "esbuild";
import {createHash} from "node:crypto";
import {mkdir, readFile, rename, writeFile} from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const [web, output, ...options] = process.argv.slice(2);
const development = options.includes("--development"), watch = options.includes("--watch");
const manifest = path.join(web,"web-entry.js"), template = path.join(web,"index.html");
const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
function vlq(value) {
  let n = (Math.abs(value) << 1) + Number(value < 0), encoded = "";
  do {
    const digit = n & 31; n >>>= 5;
    encoded += alphabet[digit | (n ? 32 : 0)];
  } while (n);
  return encoded;
}
const workspace = {
  name: "workspace",
  setup(builder) {
    builder.onResolve({filter:/^workspace$/}, () => ({path:"workspace",namespace:"workspace"}));
    builder.onLoad({filter:/.*/,namespace:"workspace"}, async () => {
      const sources = [...(await readFile(manifest,"utf8")).matchAll(/^import "\.\/([\w-]+\.js)";$/gm)].map(match => match[1]);
      const contents = await Promise.all(sources.map(file => readFile(path.join(web,file),"utf8")));
      // Explicit ESM input makes esbuild eliminate Node-only test exports.
      // No identifier mangling: the native host invokes classic declarations.
      let code = 'export {};\nimport "./web-entry.css";\n', mappings = ";;";
      let lastFile = 0, lastLine = 0;
      for (let file=0; file<sources.length; file++) {
        const source = contents[file].trimEnd();
        code += source + "\n;\n";
        if (development) {
          const lines = source.split("\n").length;
          for (let line=0; line<lines; line++) {
            mappings += "A" + vlq(file-lastFile) + vlq(line-lastLine) + "A;";
            lastFile=file;lastLine=line;
          }
          mappings += ";";
        }
      }
      if (development) {
        const map = {version:3,sources,sourcesContent:contents,names:[],mappings};
        code += "//# sourceMappingURL=data:application/json;base64,"+Buffer.from(JSON.stringify(map)).toString("base64");
      }
      return {contents:code,resolveDir:web,loader:"js",watchFiles:[template,manifest,...sources.map(file=>path.join(web,file))]};
    });
    builder.onEnd(async result => {
      if(result.errors.length) return;
      let index = await readFile(template,"utf8");
      await mkdir(output,{recursive:true});
      for (const file of result.outputFiles) {
        const extension=path.extname(file.path);
        const digest=createHash("sha256").update(file.contents).digest("hex").slice(0,20);
        const name="app-"+digest+extension;
        // Readers may still hold the previous document during a rebuild.
        const temporary=path.join(output,"."+name+"."+process.pid);
        await writeFile(temporary,file.contents);
        await rename(temporary,path.join(output,name));
        index=index.replace("/dist/app"+extension,"/dist/"+name);
      }
      const temp=path.join(output,".index."+process.pid);
      await writeFile(temp,index);
      await rename(temp,path.join(output,"index.html"));
    });
  }
};
const config={absWorkingDir:web,entryPoints:{app:"workspace"},outdir:output,bundle:true,platform:"browser",format:"esm",
  treeShaking:false,define:{module:"undefined",require:"undefined"},minifySyntax:!development,minifyWhitespace:!development,
  target:["safari16","chrome105"],sourcemap:development?"inline":false,write:false,plugins:[workspace],logLevel:"warning"};
if(watch) {
  const builder=await context(config);
  await builder.watch();
} else await build(config);
