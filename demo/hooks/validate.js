#!/usr/bin/env node
/* Validate composition bounds/overflow, generate poster.png + phone previews, and verify MP4 playback for all 3 hook demos:
 *   node demo/hooks/validate.js compose
 *   node demo/hooks/validate.js playback
 */
"use strict";
const {chromium} = require("playwright");
const fs = require("node:fs");
const path = require("node:path");

const DEMO_DIR = path.resolve(__dirname, "..");
const mode = process.argv[2] || "all";

const HOOKS = [
  {
    slug: "collision",
    dir: path.join(DEMO_DIR, "collision"),
    mp4: "ribbon-field-collision.mp4",
    posterTime: 22.0,
    keyTimes: [1.5, 5.5, 12.5, 22.0, 25.5, 32.5, 42.0],
    selectors: ["#brand", "#headline .l1", "#headline .l2", "#subhead", "#workspace-label", ".agent-meta h2", ".cmd-box", ".scope-pill", ".agent-status", ".file-name", "#lease-badge", ".code-line", "#file-footer", "#peer-pill", "#bottom-banner", "#film-note", ".end-promise", ".end-detail"]
  },
  {
    slug: "amnesia",
    dir: path.join(DEMO_DIR, "amnesia"),
    mp4: "ribbon-field-amnesia.mp4",
    posterTime: 14.0,
    keyTimes: [1.5, 5.5, 14.0, 21.0, 26.0, 33.0, 42.0],
    selectors: ["#brand", "#headline .l1", "#headline .l2", "#subhead", "#triage-counter", ".lane-h", ".pane-id", ".pane-title", ".pane-line", ".pane-badge", "#lens-title", ".ckpt-v", "#lens-action", "#bottom-banner", "#film-note", ".end-promise", ".end-detail"]
  },
  {
    slug: "delegation",
    dir: path.join(DEMO_DIR, "delegation"),
    mp4: "ribbon-field-delegation.mp4",
    duration: 45,
    posterTime: 24.0,
    keyTimes: [1.5, 5.5, 14.0, 24.0, 32.0, 35.5, 42.0],
    selectors: ["#brand", "#headline .l1", "#headline .l2", "#subhead", "#thread-bar", ".col-head h2", ".cli-line", ".art-title", ".art-body", ".col-status", "#pill-top", "#pill-bot", "#bottom-banner", "#film-note", ".end-promise", ".end-detail"]
  },
  {
    slug: "autowake",
    dir: path.join(DEMO_DIR, "autowake"),
    mp4: "ribbon-field-autowake.mp4",
    duration: 30,
    posterTime: 18.5,
    keyTimes: [1.5, 4.5, 9.5, 12.0, 18.5, 22.0, 27.5],
    selectors: ["#brand", "#headline .l1", "#headline .l2", "#subhead", "#top-pill", ".state-tag", ".col-head h2", ".cli-box", ".log-title", ".log-body", ".col-status", "#pill-top", "#pill-bot", "#bottom-banner", "#film-note", ".end-promise", ".end-detail"]
  }
];

async function composeOne(browser, hook) {
  const outDir = path.join(hook.dir, "out");
  fs.mkdirSync(outDir, {recursive: true});
  const page = await browser.newPage({viewport: {width: 1080, height: 1080}});
  const errors = [];
  page.on("pageerror", e => errors.push("pageerror: " + e.message));
  await page.goto("file://" + path.join(hook.dir, "index.html"));
  await page.evaluate(() => window.__ready);

  const checks = [];
  for (const t of hook.keyTimes) {
    await page.evaluate(tt => window.__render(tt), t);
    const result = await page.evaluate(selectors => {
      const visible = el => {
        for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
          const cs = getComputedStyle(n);
          if (cs.visibility === "hidden" || Number(cs.opacity) < 0.05) return false;
        }
        return true;
      };
      const bounds = [];
      for (const selector of selectors) {
        for (const el of document.querySelectorAll(selector)) {
          if (!visible(el) || !el.textContent.trim()) continue;
          const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
          bounds.push({
            selector,
            text: el.textContent.trim().slice(0, 70),
            x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
            overflow: cs.whiteSpace === "nowrap" && el.scrollWidth > el.clientWidth + 2,
            outside: r.x < 0 || r.y < 0 || r.right > 1080 || r.bottom > 1080
          });
        }
      }
      return {state: window.__sceneState, bounds};
    }, hook.selectors);

    for (const b of result.bounds) {
      if (b.overflow) errors.push(`[${hook.slug}] t=${t} ${b.selector} overflows: ${b.text}`);
      if (b.outside) errors.push(`[${hook.slug}] t=${t} ${b.selector} outside stage (${b.x},${b.y},${b.w}x${b.h}): ${b.text}`);
    }
    const footer = result.bounds.find(b => b.selector === "#film-note");
    for (const b of result.bounds) {
      if (footer && b.selector !== "#film-note" && b.y + b.h > footer.y && b.y < footer.y + footer.h && !b.selector.startsWith(".end")) {
        errors.push(`[${hook.slug}] t=${t} ${b.selector} collides with footer: ${b.text}`);
      }
    }
    checks.push({time: t, state: result.state, count: result.bounds.length});
  }

  await page.evaluate(tt => window.__render(tt), hook.posterTime);
  await page.screenshot({path: path.join(outDir, "poster.png"), type: "png"});
  await page.close();

  const phone = await browser.newPage({viewport: {width: 1080, height: 1080}, deviceScaleFactor: 390 / 1080});
  await phone.goto("file://" + path.join(hook.dir, "index.html"));
  await phone.evaluate(() => window.__ready);
  await phone.evaluate(tt => window.__render(tt), hook.posterTime);
  await phone.screenshot({path: path.join(outDir, "phone-check.png"), type: "png"});
  await phone.close();

  fs.writeFileSync(path.join(outDir, "composition-checks.json"), JSON.stringify({errors, checks}, null, 2));
  console.log(`[${hook.slug}] compose checks: ${checks.length} key times, ${errors.length} errors`);
  errors.forEach(e => console.error("  " + e));
  return errors.length === 0;
}

async function playbackOne(browser, hook) {
  const outDir = path.join(hook.dir, "out");
  const page = await browser.newPage({viewport: {width: 820, height: 900}});
  const errors = [];
  page.on("pageerror", e => errors.push("pageerror: " + e.message));
  await page.goto("file://" + path.join(hook.dir, "player.html"));
  const meta = await page.evaluate(() => new Promise((resolve, reject) => {
    const v = document.querySelector("video");
    const done = () => resolve({width: v.videoWidth, height: v.videoHeight, duration: v.duration});
    if (v.readyState >= 1) done();
    else {
      v.addEventListener("loadedmetadata", done, {once: true});
      v.addEventListener("error", () => reject(new Error("video load error")), {once: true});
    }
  }));
  await page.evaluate(tt => new Promise(resolve => {
    const v = document.querySelector("video");
    v.addEventListener("seeked", () => resolve(true), {once: true});
    v.currentTime = tt;
  }), hook.posterTime);
  await page.screenshot({path: path.join(outDir, "player-check.png"), type: "png"});
  await page.close();
  const expectedDuration = hook.duration || 45;
  if (meta.width !== 1080 || meta.height !== 1080 || Math.abs(meta.duration - expectedDuration) > 1.5) {
    errors.push(`[${hook.slug}] unexpected video metadata: ${JSON.stringify(meta)}`);
  }
  fs.writeFileSync(path.join(outDir, "playback-checks.json"), JSON.stringify({meta, errors}, null, 2));
  console.log(`[${hook.slug}] playback check: ${JSON.stringify(meta)} (${errors.length} errors)`);
  return errors.length === 0;
}

(async () => {
  let ok = true;
  if (mode === "compose" || mode === "all") {
    const browser = await chromium.launch();
    for (const h of HOOKS) ok = (await composeOne(browser, h)) && ok;
    await browser.close();
  }
  if (mode === "playback" || mode === "all") {
    const browser = await chromium.launch({channel: "chrome", args: ["--autoplay-policy=no-user-gesture-required"]});
    for (const h of HOOKS) ok = (await playbackOne(browser, h)) && ok;
    await browser.close();
  }
  process.exit(ok ? 0 : 1);
})().catch(e => { console.error(e); process.exit(1); });
