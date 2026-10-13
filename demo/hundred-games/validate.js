#!/usr/bin/env node
/* Artifact validation for the 100 games film.
 *   node demo/hundred-games/validate.js compose    # bounds/overflow at key times, poster, phone previews (bundled Chromium)
 *   node demo/hundred-games/validate.js playback   # encoded MP4 seeks, playback, and audio track in installed Google Chrome
 * Nothing here launches a provider, builds a game, or touches the coordination database. */
"use strict";
const {chromium} = require("playwright");
const fs = require("node:fs");
const path = require("node:path");

const HERE = __dirname, OUT = path.join(HERE, "out");
const mode = process.argv[2] || "all";
const KEY_TIMES = [0.6, 2.0, 3.4, 5.2, 6.4, 8.6, 10.5, 13.8, 15.2, 18.0, 20.2, 22.8, 24.6, 26.0, 27.2, 29.0, 32.4, 34.0, 37.6, 39.6, 42.2, 45.4, 48.8, 50.9, 53.0, 56.5];
const SELECTORS = ["#brand", "#headline .l1", "#headline .l2", "#request-text", "#caption", "#report strong", "#report span", ".hub-line", "#counter", "#legend .chip",
  "#banner", "#lens h2", "#lens .chip", "#lens-status", "#lens-direct", "#lens .arrival", "#duo h2", "#duo .lens-status", ".file-token", "#duo .pill", "#film-note", ".end-promise", ".end-detail", "#endcard h2"];
const WRAP_LIMIT = {"#headline .l1": 1, "#headline .l2": 1, "#request-text": 3, "#caption": 2, ".end-promise": 2, ".end-detail": 4};

async function compose() {
  const browser = await chromium.launch();
  const page = await browser.newPage({viewport: {width: 1080, height: 1080}});
  page.on("pageerror", e => { throw new Error("page error: " + e.message); });
  await page.goto("file://" + path.join(HERE, "index.html"));
  await page.evaluate(() => window.__ready);
  const checks = [], errors = [];
  for (const t of KEY_TIMES) {
    await page.evaluate(tt => window.__render(tt), t);
    const result = await page.evaluate(([selectors, wrapLimit]) => {
      const visible = el => { for (let n = el; n && n.nodeType === 1; n = n.parentElement) { const cs = getComputedStyle(n); if (cs.visibility === "hidden" || Number(cs.opacity) < .05) return false; } return true; };
      const bounds = [];
      for (const selector of selectors) for (const el of document.querySelectorAll(selector)) {
        if (!visible(el) || !el.textContent.trim()) continue;
        const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
        const lineHeight = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.2, lines = Math.round(r.height / lineHeight), limit = wrapLimit[selector];
        bounds.push({selector, text: el.textContent.trim().slice(0, 70), x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), lines,
          overflow: cs.whiteSpace === "nowrap" && el.scrollWidth > el.clientWidth + 1, outside: r.x < 0 || r.y < 0 || r.right > 1080 || r.bottom > 1080, wrapped: limit ? lines > limit : false});
      }
      const field = Array.from(document.querySelectorAll(".tile")).filter(visible).length;
      return {state: window.__sceneState, bounds, visibleTiles: field};
    }, [SELECTORS, WRAP_LIMIT]);
    for (const b of result.bounds) {
      if (b.overflow) errors.push(`t=${t} ${b.selector} overflows: ${b.text}`);
      if (b.outside) errors.push(`t=${t} ${b.selector} outside stage: ${b.text}`);
      if (b.wrapped) errors.push(`t=${t} ${b.selector} wraps to ${b.lines} lines: ${b.text}`);
    }
    const footer = result.bounds.find(b => b.selector === "#film-note");
    for (const b of result.bounds) if (footer && b.selector !== "#film-note" && b.y + b.h > footer.y && b.y < footer.y + footer.h && !b.selector.startsWith("#endcard") && !b.selector.startsWith(".end")) errors.push(`t=${t} ${b.selector} collides with footer: ${b.text}`);
    checks.push({time: t, state: result.state, visibleTiles: result.visibleTiles, bounds: result.bounds});
  }
  const expect = [[10.5, s => s.landed === 100, "all 100 agents landed by 10.5 s"], [20.2, s => s.playable === 100, "all 100 games playable by 20.2 s"],
    [34.0, s => s.closed === 100, "all 100 chats closed by 34 s"], [42.2, s => s.awake === 3, "three agents awake by 42.2 s"], [56.5, s => s.endcard, "end card at 56.5 s"]];
  for (const [t, fn, label] of expect) { const c = checks.find(c => c.time === t); if (!c || !fn(c.state)) errors.push(`story check failed: ${label}`); }
  await page.evaluate(() => window.__render(18.0));
  await page.screenshot({path: path.join(OUT, "poster.png"), type: "png"});
  const phone = await browser.newPage({viewport: {width: 1080, height: 1080}, deviceScaleFactor: 390 / 1080});
  await phone.goto("file://" + path.join(HERE, "index.html"));
  await phone.evaluate(() => window.__ready);
  const phoneTimes = [8.6, 15.2, 24.6, 48.8];
  for (const [k, t] of phoneTimes.entries()) { await phone.evaluate(tt => window.__render(tt), t); await phone.screenshot({path: path.join(OUT, `phone-check-${k}.png`), type: "png"}); }
  await browser.close();
  fs.writeFileSync(path.join(OUT, "composition-checks.json"), JSON.stringify({errors, keyTimes: KEY_TIMES, phonePreviews: phoneTimes, checks}, null, 1));
  console.log(`composition checks: ${checks.length} times, ${errors.length} errors`);
  errors.forEach(e => console.log("  " + e));
  return errors.length === 0;
}

async function playback() {
  const browser = await chromium.launch({channel: "chrome", args: ["--autoplay-policy=no-user-gesture-required"]});
  const page = await browser.newPage({viewport: {width: 820, height: 900}});
  const errors = [];
  page.on("pageerror", e => errors.push("page error: " + e.message));
  await page.goto("file://" + path.join(HERE, "player.html"));
  const metadata = await page.evaluate(() => new Promise((resolve, reject) => {
    const v = document.querySelector("video");
    const done = () => resolve({width: v.videoWidth, height: v.videoHeight, duration: v.duration, canPlayH264Aac: v.canPlayType('video/mp4; codecs="avc1.640028, mp4a.40.2"'),
      audioTracks: v.audioTracks ? v.audioTracks.length : null, mozHasAudio: v.mozHasAudio ?? null, webkitAudioDecodedByteCount: v.webkitAudioDecodedByteCount ?? null});
    if (v.readyState >= 1) done(); else { v.addEventListener("loadedmetadata", done, {once: true}); v.addEventListener("error", () => reject(new Error("video error " + (v.error && v.error.code))), {once: true}); }
  }));
  const seeks = [];
  for (const t of [0.6, 5.2, 10.5, 15.2, 22.8, 27.2, 32.4, 39.6, 45.4, 50.9, 56.5]) {
    seeks.push(await page.evaluate(tt => new Promise(resolve => { const v = document.querySelector("video");
      v.addEventListener("seeked", () => resolve({time: tt, currentTime: v.currentTime, readyState: v.readyState, error: v.error ? v.error.code : null}), {once: true}); v.currentTime = tt; }), t));
  }
  await page.evaluate(() => { document.querySelector("video").currentTime = 15.2; });
  await page.waitForTimeout(400);
  await page.screenshot({path: path.join(OUT, "player-check.png"), type: "png"});
  const played = await page.evaluate(() => new Promise(async resolve => {
    const v = document.querySelector("video"); v.muted = false; v.volume = 1; v.currentTime = 3.0;
    try { await v.play(); } catch (e) { resolve({error: String(e)}); return; }
    setTimeout(() => { const q = v.getVideoPlaybackQuality(); const bytes = v.webkitAudioDecodedByteCount ?? null; v.pause();
      resolve({time: v.currentTime, frames: q.totalVideoFrames, dropped: q.droppedVideoFrames, audioDecodedBytes: bytes, error: v.error ? v.error.code : null}); }, 6000);
  }));
  await browser.close();
  const problems = [...errors, ...seeks.filter(s => s.readyState < 2 || s.error).map(s => `seek ${s.time} readyState=${s.readyState} error=${s.error}`)];
  if (!played.frames || played.frames < 120 || played.error) problems.push(`playback: ${JSON.stringify(played)}`);
  if (played.audioDecodedBytes !== null && played.audioDecodedBytes !== undefined && played.audioDecodedBytes <= 0) problems.push("no audio bytes decoded during playback");
  fs.writeFileSync(path.join(OUT, "playback-checks.json"), JSON.stringify({browser: "Google Chrome (installed, channel=chrome)", metadata, seeks, playback: played, errors: problems}, null, 1));
  console.log("playback:", JSON.stringify({metadata, playback: played, problems}));
  return problems.length === 0;
}

(async () => {
  let ok = true;
  if (mode === "compose" || mode === "all") ok = (await compose()) && ok;
  if (mode === "playback" || mode === "all") ok = (await playback()) && ok;
  process.exit(ok ? 0 : 1);
})().catch(e => { console.error(e); process.exit(1); });
