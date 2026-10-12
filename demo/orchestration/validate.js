#!/usr/bin/env node
/* Artifact validation for the orchestration film.
 *   node demo/orchestration/validate.js compose    # bounds/overflow at key times, poster, phone preview (bundled Chromium)
 *   node demo/orchestration/validate.js playback   # encoded MP4 seeks and playback in installed Google Chrome
 * Nothing here launches a provider, runs the depicted projects, or touches the coordination database. */
"use strict";
const {chromium} = require("playwright");
const fs = require("node:fs");
const path = require("node:path");

const HERE = __dirname, OUT = path.join(HERE, "out");
const mode = process.argv[2] || "all";
const KEY_TIMES = [0.6, 2.4, 3.5, 5.0, 6.2, 7.9, 9.5, 12.6, 13.6, 15.6, 17.8, 20.0, 20.8, 23.2, 25.3, 27.5, 32.0, 34.0, 36.0, 38.0, 41.5, 42.8, 45.5];
const SELECTORS = ["#brand", "#headline", "#request-text", "#caption", "#report strong", "#report span", ".hub-label", "#hub .chip", ".agent h2", ".agent .chip",
  ".agent .status", ".agent .initial", ".agent .revision", ".arrival", ".pill", "#film-note", ".end-promise", ".end-detail", "#endcard h2"];
const WRAP_LIMIT = {"#headline": 2, "#request-text": 2, "#caption": 2, ".end-promise": 2, ".end-detail": 3};

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
        const lineHeight = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.2;
        const lines = Math.round(r.height / lineHeight);
        const limit = wrapLimit[selector];
        bounds.push({selector, text: el.textContent.trim().slice(0, 60), x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), lines,
          overflow: cs.whiteSpace === "nowrap" && el.scrollWidth > el.clientWidth + 1,
          outside: r.x < 0 || r.y < 0 || r.right > 1080 || r.bottom > 1080,
          wrapped: limit ? lines > limit : false});
      }
      return {state: window.__sceneState, bounds};
    }, [SELECTORS, WRAP_LIMIT]);
    for (const b of result.bounds) {
      if (b.overflow) errors.push(`t=${t} ${b.selector} overflows: ${b.text}`);
      if (b.outside) errors.push(`t=${t} ${b.selector} outside stage: ${b.text}`);
      if (b.wrapped) errors.push(`t=${t} ${b.selector} wraps to ${b.lines} lines: ${b.text}`);
    }
    checks.push({time: t, state: result.state, bounds: result.bounds});
  }
  // Pills must never overlap the dispatcher label, and agent cards must stay clear of the footer.
  for (const c of checks) {
    const hub = c.bounds.find(b => b.selector === ".hub-label"), footer = c.bounds.find(b => b.selector === "#film-note");
    for (const b of c.bounds) {
      if (b.selector === ".pill" && hub && b.x < hub.x + hub.w && b.x + b.w > hub.x && b.y < hub.y + hub.h && b.y + b.h > hub.y) errors.push(`t=${c.time} pill overlaps dispatcher label: ${b.text}`);
      if (b.selector.startsWith(".agent") && footer && b.y + b.h > footer.y) errors.push(`t=${c.time} ${b.selector} collides with footer: ${b.text}`);
    }
  }
  await page.evaluate(() => window.__render(10.0));
  await page.screenshot({path: path.join(OUT, "poster.png"), type: "png"});
  const phone = await browser.newPage({viewport: {width: 1080, height: 1080}, deviceScaleFactor: 390 / 1080});
  await phone.goto("file://" + path.join(HERE, "index.html"));
  await phone.evaluate(() => window.__ready);
  const phoneTimes = [13.6, 23.2, 34.0];
  for (const [k, t] of phoneTimes.entries()) { await phone.evaluate(tt => window.__render(tt), t); await phone.screenshot({path: path.join(OUT, `phone-check-${k}.png`), type: "png"}); }
  await browser.close();
  fs.writeFileSync(path.join(OUT, "composition-checks.json"), JSON.stringify({errors, keyTimes: KEY_TIMES, phonePreviews: phoneTimes, checks}, null, 1));
  console.log(`composition checks: ${checks.length} times, ${errors.length} errors`);
  errors.forEach(e => console.log("  " + e));
  return errors.length === 0;
}

async function playback() {
  const browser = await chromium.launch({channel: "chrome"});
  const page = await browser.newPage({viewport: {width: 820, height: 900}});
  const errors = [];
  page.on("pageerror", e => errors.push("page error: " + e.message));
  await page.goto("file://" + path.join(HERE, "player.html"));
  const metadata = await page.evaluate(() => new Promise((resolve, reject) => {
    const v = document.querySelector("video");
    const done = () => resolve({width: v.videoWidth, height: v.videoHeight, duration: v.duration, canPlayH264: v.canPlayType('video/mp4; codecs="avc1.640028"')});
    if (v.readyState >= 1) done(); else { v.addEventListener("loadedmetadata", done, {once: true}); v.addEventListener("error", () => reject(new Error("video error " + (v.error && v.error.code))), {once: true}); }
  }));
  const seeks = [];
  for (const t of [0.6, 5.0, 9.5, 13.6, 20.8, 25.3, 27.5, 34.0, 38.0, 42.8, 46.5]) {
    seeks.push(await page.evaluate(tt => new Promise(resolve => {
      const v = document.querySelector("video");
      v.addEventListener("seeked", () => resolve({time: tt, currentTime: v.currentTime, readyState: v.readyState, error: v.error ? v.error.code : null}), {once: true});
      v.currentTime = tt;
    }), t));
  }
  await page.evaluate(() => { const v = document.querySelector("video"); v.currentTime = 12.5; });
  await page.waitForTimeout(400);
  await page.screenshot({path: path.join(OUT, "player-check.png"), type: "png"});
  const played = await page.evaluate(() => new Promise(async resolve => {
    const v = document.querySelector("video"); v.muted = true; v.currentTime = 0;
    try { await v.play(); } catch (e) { resolve({error: String(e)}); return; }
    setTimeout(() => { const q = v.getVideoPlaybackQuality(); v.pause(); resolve({time: v.currentTime, frames: q.totalVideoFrames, dropped: q.droppedVideoFrames, error: v.error ? v.error.code : null}); }, 6000);
  }));
  await browser.close();
  const problems = [...errors, ...seeks.filter(s => s.readyState < 2 || s.error).map(s => `seek ${s.time} readyState=${s.readyState} error=${s.error}`)];
  if (!played.frames || played.frames < 120 || played.error) problems.push(`playback: ${JSON.stringify(played)}`);
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
