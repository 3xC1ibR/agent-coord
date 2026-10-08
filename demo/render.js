#!/usr/bin/env node
/* Render the composition frame by frame with headless Chromium and encode it
 * with ffmpeg. Frames are split across parallel browser contexts, each piping
 * JPEG frames into its own ffmpeg segment; segments are concatenated losslessly.
 *
 *   node render.js                       # full 1080p60 render + soundtrack + mux
 *   node render.js --stills 3            # one PNG every 3 s into out/stills (review)
 *   node render.js --preview             # 540p24 quick render
 *   node render.js --from 20 --to 32     # time window
 *   node render.js --workers 4           # parallel renderers (default 4)
 */
"use strict";
const {chromium} = require("playwright");
const {spawn, spawnSync} = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const HERE = __dirname;
const OUT = path.join(HERE, "out");
const argv = process.argv.slice(2);
const opt = (name, fallback) => { const i = argv.indexOf("--" + name); return i >= 0 ? argv[i + 1] : fallback; };
const flag = name => argv.includes("--" + name);
const preview = flag("preview");
const stillsEvery = opt("stills", null);
const fps = Number(opt("fps", preview ? 24 : 60));
const scale = preview ? 0.5 : 1;
const from = Number(opt("from", 0));
const toArg = opt("to", null);
const workers = Math.max(1, Number(opt("workers", Math.min(4, Math.max(1, os.cpus().length - 2)))));
const quality = Number(opt("quality", 93));

async function openPage(browser) {
  const context = await browser.newContext({viewport: {width: 1920, height: 1080}, deviceScaleFactor: scale});
  const page = await context.newPage();
  page.on("pageerror", error => console.error("page error:", error.message));
  page.on("console", message => { if (message.type() === "error") console.error("console:", message.text()); });
  const boxes = fs.readFileSync(path.join(OUT, "shots", "boxes.json"), "utf8");
  await page.addInitScript({content: "window.BOXES = " + boxes + ";"});
  await page.goto("file://" + path.join(HERE, "video", "index.html"));
  await page.evaluate(() => window.__ready);
  return page;
}

function encoder(outputPath) {
  return spawn("ffmpeg", ["-y", "-loglevel", "error", "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", String(fps), "-i", "-",
    "-c:v", "libx264", "-preset", preview ? "veryfast" : "medium", "-crf", preview ? "23" : "17",
    "-pix_fmt", "yuv420p", "-g", String(fps * 2), "-movflags", "+faststart", outputPath], {stdio: ["pipe", "inherit", "inherit"]});
}

async function renderRange(browser, index, startFrame, endFrame, segmentPath, progress) {
  const page = await openPage(browser);
  const ffmpeg = encoder(segmentPath);
  for (let i = startFrame; i < endFrame; i++) {
    const t = from + i / fps;
    await page.evaluate(tt => window.__render(tt), t);
    const jpeg = await page.screenshot({type: "jpeg", quality});
    if (!ffmpeg.stdin.write(jpeg)) await new Promise(resolve => ffmpeg.stdin.once("drain", resolve));
    progress();
  }
  ffmpeg.stdin.end();
  await new Promise((resolve, reject) => ffmpeg.on("close", code => code === 0 ? resolve() : reject(new Error(`ffmpeg segment ${index} exit ${code}`))));
  await page.context().close();
}

async function main() {
  fs.mkdirSync(OUT, {recursive: true});
  const browser = await chromium.launch();
  const probe = await openPage(browser);
  const duration = await probe.evaluate(() => window.__duration());
  const cues = await probe.evaluate(() => window.__cues());
  fs.writeFileSync(path.join(OUT, "cues.json"), JSON.stringify(cues, null, 1));
  const to = toArg ? Number(toArg) : duration;
  console.log(`composition ${duration.toFixed(2)}s, rendering ${from}-${to.toFixed(2)}s at ${fps} fps`);

  if (stillsEvery) {
    const dir = path.join(OUT, "stills");
    fs.rmSync(dir, {recursive: true, force: true});
    fs.mkdirSync(dir, {recursive: true});
    for (let t = from; t <= to + 1e-9; t += Number(stillsEvery)) {
      await probe.evaluate(tt => window.__render(tt), t);
      await probe.screenshot({path: path.join(dir, `t_${String(t.toFixed(1)).padStart(6, "0")}.png`), type: "png"});
    }
    console.log("stills written to", dir);
    await browser.close();
    return;
  }
  await probe.context().close();

  const total = Math.ceil((to - from) * fps);
  const segmentDir = path.join(OUT, "segments");
  fs.rmSync(segmentDir, {recursive: true, force: true});
  fs.mkdirSync(segmentDir, {recursive: true});
  const started = Date.now();
  let done = 0;
  const progress = () => {
    done++;
    if (done % (fps * 10) === 0 || done === total) {
      const elapsed = (Date.now() - started) / 1000;
      const rate = done / Math.max(elapsed, 0.001);
      console.log(`frames ${done}/${total}  ${rate.toFixed(1)} fps  eta ${((total - done) / Math.max(rate, 0.01)).toFixed(0)}s`);
    }
  };
  const per = Math.ceil(total / workers);
  const jobs = [];
  const segments = [];
  for (let w = 0; w < workers; w++) {
    const a = w * per, b = Math.min(total, a + per);
    if (a >= b) break;
    const segmentPath = path.join(segmentDir, `segment-${String(w).padStart(2, "0")}.mp4`);
    segments.push(segmentPath);
    jobs.push(renderRange(browser, w, a, b, segmentPath, progress));
  }
  await Promise.all(jobs);
  await browser.close();

  const videoPath = path.join(OUT, preview ? "preview-noaudio.mp4" : "video-noaudio.mp4");
  const listPath = path.join(segmentDir, "segments.txt");
  fs.writeFileSync(listPath, segments.map(p => `file '${p}'`).join("\n") + "\n");
  const concat = spawnSync("ffmpeg", ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listPath, "-c", "copy", "-movflags", "+faststart", videoPath], {stdio: "inherit"});
  if (concat.status !== 0) throw new Error("concat failed");

  const wav = path.join(OUT, "soundtrack.wav");
  const audio = spawnSync("node", [path.join(HERE, "audio.js"), String(duration), path.join(OUT, "cues.json"), wav], {stdio: "inherit"});
  if (audio.status !== 0) throw new Error("audio generation failed");
  const finalPath = path.join(OUT, preview ? "preview.mp4" : "ribbon-field-demo.mp4");
  const mux = spawnSync("ffmpeg", ["-y", "-loglevel", "error", "-i", videoPath, "-ss", String(from), "-i", wav,
    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", finalPath], {stdio: "inherit"});
  if (mux.status !== 0) throw new Error("mux failed");
  console.log("wrote", finalPath);
}

main().catch(error => { console.error(error); process.exit(1); });
